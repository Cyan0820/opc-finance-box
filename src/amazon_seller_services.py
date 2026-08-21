from __future__ import annotations

from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
import re
from typing import Any

from .pack_services import ServiceContext


def _decimal(value: Any, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field} must be a finite decimal string") from exc
    if not result.is_finite():
        raise ValueError(f"{field} must be a finite decimal string")
    return result


def summarize_amazon_seller_transaction_activity(
    payload: dict[str, Any], context: ServiceContext,
) -> dict[str, Any]:
    transactions = payload.get("transactions") or []
    if not isinstance(transactions, list) or any(not isinstance(row, dict) for row in transactions):
        raise ValueError("transactions must be a list of objects")
    keys: list[str] = []
    status_counts: Counter[str] = Counter()
    type_counts: Counter[str] = Counter()
    account_type_counts: Counter[str] = Counter()
    marketplace_counts: Counter[str] = Counter()
    related_identifier_counts: Counter[str] = Counter()
    currency_totals: dict[str, dict[str, Decimal | int]] = defaultdict(
        lambda: {
            "transaction_count": 0,
            "net_activity": Decimal("0"),
            "released_activity": Decimal("0"),
            "deferred_activity": Decimal("0"),
        }
    )
    component_totals: dict[tuple[str, str, str], dict[str, Decimal | int]] = defaultdict(
        lambda: {"component_count": 0, "amount": Decimal("0")}
    )
    settlement_linked_keys: list[str] = []
    refund_candidate_keys: list[str] = []
    fee_candidate_keys: list[str] = []
    for index, row in enumerate(transactions, 1):
        if row.get("entity_id") != context.entity_id:
            raise ValueError(f"transactions[{index}] is outside statutory entity {context.entity_id}")
        key = str(row.get("amazon_transaction_key") or "")
        if not key:
            raise ValueError(f"transactions[{index}] requires amazon_transaction_key")
        evidence = row.get("evidence")
        if not isinstance(evidence, dict) or not evidence.get("source_file") or not evidence.get("batch_id"):
            raise ValueError(f"transactions[{index}] requires source_file and batch_id evidence")
        currency = str(row.get("currency") or "")
        if len(currency) != 3 or not currency.isalpha() or not currency.isupper():
            raise ValueError(f"transactions[{index}] currency must be a three-letter code")
        amount = _decimal(row.get("amount"), f"transactions[{index}].amount")
        status = str(row.get("transaction_status") or "UNKNOWN")
        transaction_type = str(row.get("transaction_type") or "UNKNOWN")
        account_type = str(row.get("account_type") or "UNKNOWN")
        marketplace_id = str(row.get("marketplace_id") or "UNKNOWN")
        keys.append(key)
        status_counts[status] += 1
        type_counts[transaction_type] += 1
        account_type_counts[account_type] += 1
        marketplace_counts[marketplace_id] += 1
        totals = currency_totals[currency]
        totals["transaction_count"] = int(totals["transaction_count"]) + 1
        totals["net_activity"] = Decimal(totals["net_activity"]) + amount
        if status in {"RELEASED", "DEFERRED_RELEASED"}:
            totals["released_activity"] = Decimal(totals["released_activity"]) + amount
        else:
            totals["deferred_activity"] = Decimal(totals["deferred_activity"]) + amount

        related = row.get("related_keys") or []
        if not isinstance(related, list) or any(not isinstance(item, dict) for item in related):
            raise ValueError(f"transactions[{index}].related_keys must be an object list")
        related_types = set()
        for item in related:
            kind = str(item.get("type") or "UNKNOWN")
            if not item.get("key"):
                raise ValueError(f"transactions[{index}] related key is missing")
            related_identifier_counts[kind] += 1
            related_types.add(kind)
        if related_types & {"SETTLEMENT_ID", "FINANCIAL_EVENT_GROUP_ID", "DISBURSEMENT_ID"}:
            settlement_linked_keys.append(key)
        if "REFUND" in transaction_type or "REFUND_ID" in related_types:
            refund_candidate_keys.append(key)

        components = row.get("financial_components") or []
        if not isinstance(components, list) or any(not isinstance(item, dict) for item in components):
            raise ValueError(f"transactions[{index}].financial_components must be an object list")
        component_fee = False
        for item in components:
            scope = str(item.get("scope") or "")
            path = str(item.get("path") or "")
            component_currency = str(item.get("currency") or "")
            if scope not in {"transaction", "item"} or not path or len(component_currency) != 3:
                raise ValueError(f"transactions[{index}] contains an invalid financial component")
            component_amount = _decimal(
                item.get("amount"), f"transactions[{index}].financial_components.amount",
            )
            component = component_totals[(scope, path, component_currency)]
            component["component_count"] = int(component["component_count"]) + 1
            component["amount"] = Decimal(component["amount"]) + component_amount
            if any(marker in path for marker in ("FEE", "COMMISSION", "EXPENSE")):
                component_fee = True
        if component_fee or any(marker in transaction_type for marker in ("FEE", "COMMISSION")):
            fee_candidate_keys.append(key)

    duplicate_keys = sorted(key for key, count in Counter(keys).items() if count > 1)
    blockers = [{"code": "duplicate_amazon_transaction_key"}] if duplicate_keys else []
    return {
        "ready": not blockers,
        "entity_id": context.entity_id,
        "transaction_count": len(transactions),
        "status_counts": dict(sorted(status_counts.items())),
        "transaction_type_counts": dict(sorted(type_counts.items())),
        "account_type_counts": dict(sorted(account_type_counts.items())),
        "marketplace_counts": dict(sorted(marketplace_counts.items())),
        "related_identifier_counts": dict(sorted(related_identifier_counts.items())),
        "currency_summary": [
            {
                "currency": currency,
                "transaction_count": int(values["transaction_count"]),
                "net_activity": format(Decimal(values["net_activity"]), "f"),
                "released_activity": format(Decimal(values["released_activity"]), "f"),
                "deferred_activity": format(Decimal(values["deferred_activity"]), "f"),
            }
            for currency, values in sorted(currency_totals.items())
        ],
        "financial_component_summary": [
            {
                "scope": scope,
                "path": path,
                "currency": currency,
                "component_count": int(values["component_count"]),
                "amount": format(Decimal(values["amount"]), "f"),
            }
            for (scope, path, currency), values in sorted(component_totals.items())
        ],
        "refund_candidate_keys": sorted(refund_candidate_keys),
        "fee_candidate_keys": sorted(fee_candidate_keys),
        "settlement_linked_keys": sorted(settlement_linked_keys),
        "settlement_reference_missing_count": len(transactions) - len(set(settlement_linked_keys)),
        "deferred_transaction_count": status_counts["DEFERRED"],
        "duplicate_transaction_keys": duplicate_keys,
        "blockers": blockers,
        "candidate_only": True,
        "cross_currency_total_prohibited": True,
        "nested_component_double_counting_prohibited": True,
        "revenue_recognition_performed": False,
        "tax_liability_determined": False,
        "settlement_or_bank_reconciliation_performed": False,
        "inventory_or_cogs_modified": False,
        "posting_performed": False,
        "external_actions_performed": False,
        "review_boundaries": [
            "Amazon financial transactions can lag recent orders and do not prove order or settlement completeness.",
            "Deferred transactions are separately reported and must not be treated as released cash activity.",
            "Nested financial components are hierarchical evidence and must not be summed across levels.",
            "Marketplace-collected tax and fee labels require local tax and contract review before accounting use.",
        ],
    }


def reconcile_amazon_seller_marketplace_evidence(
    payload: dict[str, Any], context: ServiceContext,
) -> dict[str, Any]:
    orders = payload.get("orders") or []
    inventory = payload.get("inventory") or []
    transactions = payload.get("transactions") or []
    for name, rows in (
        ("orders", orders), ("inventory", inventory), ("transactions", transactions),
    ):
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError(f"{name} must be a list of objects")
    source_scope = payload.get("source_scope") or {}
    if not isinstance(source_scope, dict):
        raise ValueError("source_scope must be an object")
    allowed_scope_fields = {
        "canonical_month_period", "canonical_month_scope", "marketplace_id",
        "interval_start", "interval_end", "orders_time_basis",
        "inventory_observed_at", "inventory_observation_type",
    }
    if set(source_scope) - allowed_scope_fields:
        raise ValueError("source_scope contains unsupported fields")
    canonical_period = source_scope.get("canonical_month_period")
    if canonical_period is not None and (
        not isinstance(canonical_period, str)
        or not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", canonical_period)
    ):
        raise ValueError("source_scope canonical_month_period must use YYYY-MM")
    if source_scope and bool(source_scope.get("canonical_month_scope")) != bool(canonical_period):
        raise ValueError("source_scope canonical month flag and period do not agree")
    expected_marketplace_id = source_scope.get("marketplace_id")
    transaction_summary = summarize_amazon_seller_transaction_activity(
        {"transactions": transactions}, context,
    )
    order_keys: list[str] = []
    order_status_counts: Counter[str] = Counter()
    fulfilled_by_counts: Counter[str] = Counter()
    marketplace_counts: Counter[str] = Counter()
    eligible_finance_order_keys: set[str] = set()
    fba_order_sku_keys: set[str] = set()
    eligible_three_way_order_skus: dict[str, set[str]] = {}
    eligible_three_way_order_missing_sku_keys: set[str] = set()
    total_order_items = 0
    total_order_quantity = 0
    for index, row in enumerate(orders, 1):
        if row.get("entity_id") != context.entity_id:
            raise ValueError(f"orders[{index}] is outside statutory entity {context.entity_id}")
        key = str(row.get("amazon_order_key") or "")
        if not key:
            raise ValueError(f"orders[{index}] requires amazon_order_key")
        evidence = row.get("evidence")
        if not isinstance(evidence, dict) or not evidence.get("source_file") or not evidence.get("batch_id"):
            raise ValueError(f"orders[{index}] requires source_file and batch_id evidence")
        status = str(row.get("fulfillment_status") or "UNKNOWN")
        fulfilled_by = str(row.get("fulfilled_by") or "UNKNOWN")
        marketplace = str(row.get("marketplace_id") or "UNKNOWN")
        if expected_marketplace_id and marketplace != expected_marketplace_id:
            raise ValueError(f"orders[{index}] marketplace does not match source_scope")
        items = row.get("items") or []
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise ValueError(f"orders[{index}].items must be an object list")
        order_keys.append(key)
        order_status_counts[status] += 1
        fulfilled_by_counts[fulfilled_by] += 1
        marketplace_counts[marketplace] += 1
        total_order_items += len(items)
        if status in {"SHIPPED", "PARTIALLY_SHIPPED"}:
            eligible_finance_order_keys.add(key)
            if fulfilled_by == "AMAZON":
                eligible_three_way_order_skus[key] = set()
        for item_index, item in enumerate(items, 1):
            item_key = str(item.get("amazon_order_item_key") or "")
            quantity = item.get("quantity_ordered")
            if not item_key or not isinstance(quantity, int) or isinstance(quantity, bool) or quantity < 0:
                raise ValueError(f"orders[{index}].items[{item_index}] has invalid key or quantity")
            total_order_quantity += quantity
            sku_key = item.get("amazon_sku_key")
            if fulfilled_by == "AMAZON" and status not in {"CANCELLED", "UNFULFILLABLE"} and sku_key:
                fba_order_sku_keys.add(str(sku_key))
            if key in eligible_three_way_order_skus:
                if sku_key:
                    eligible_three_way_order_skus[key].add(str(sku_key))
                else:
                    eligible_three_way_order_missing_sku_keys.add(key)

    inventory_keys: list[str] = []
    inventory_marketplace_counts: Counter[str] = Counter()
    inventory_quantity_field_missing_keys: list[str] = []
    optional_inventory_quantity_fields = {
        "fulfillable_quantity", "inbound_working_quantity", "inbound_shipped_quantity",
        "inbound_receiving_quantity", "reserved_quantity",
        "pending_customer_order_quantity", "researching_quantity",
        "unfulfillable_quantity",
    }
    inventory_totals = {
        "total_quantity": 0,
        "fulfillable_quantity": 0,
        "inbound_quantity": 0,
        "reserved_quantity": 0,
        "researching_quantity": 0,
        "unfulfillable_quantity": 0,
    }
    for index, row in enumerate(inventory, 1):
        if row.get("entity_id") != context.entity_id:
            raise ValueError(f"inventory[{index}] is outside statutory entity {context.entity_id}")
        key = str(row.get("amazon_sku_key") or "")
        if not key:
            raise ValueError(f"inventory[{index}] requires amazon_sku_key")
        evidence = row.get("evidence")
        if not isinstance(evidence, dict) or not evidence.get("source_file") or not evidence.get("batch_id"):
            raise ValueError(f"inventory[{index}] requires source_file and batch_id evidence")
        inventory_keys.append(key)
        inventory_marketplace_counts[str(row.get("marketplace_id") or "UNKNOWN")] += 1
        if expected_marketplace_id and row.get("marketplace_id") != expected_marketplace_id:
            raise ValueError(f"inventory[{index}] marketplace does not match source_scope")
        present = row.get("quantity_fields_present")
        if not isinstance(present, list) or any(
            not isinstance(field, str) or field not in optional_inventory_quantity_fields
            for field in present
        ):
            raise ValueError(
                f"inventory[{index}].quantity_fields_present must be a supported field list"
            )
        if set(present) != optional_inventory_quantity_fields:
            inventory_quantity_field_missing_keys.append(key)
        for field in (
            "total_quantity", "fulfillable_quantity", "inbound_working_quantity",
            "inbound_shipped_quantity", "inbound_receiving_quantity", "reserved_quantity",
            "researching_quantity", "unfulfillable_quantity",
        ):
            value = row.get(field)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"inventory[{index}].{field} must be a non-negative integer")
        inventory_totals["total_quantity"] += int(row["total_quantity"])
        inventory_totals["fulfillable_quantity"] += int(row["fulfillable_quantity"])
        inventory_totals["inbound_quantity"] += sum(int(row[field]) for field in (
            "inbound_working_quantity", "inbound_shipped_quantity", "inbound_receiving_quantity",
        ))
        inventory_totals["reserved_quantity"] += int(row["reserved_quantity"])
        inventory_totals["researching_quantity"] += int(row["researching_quantity"])
        inventory_totals["unfulfillable_quantity"] += int(row["unfulfillable_quantity"])

    financial_order_keys: set[str] = set()
    for row in transactions:
        if expected_marketplace_id and row.get("marketplace_id") != expected_marketplace_id:
            raise ValueError("transactions marketplace does not match source_scope")
        for related in row.get("related_keys") or []:
            if related.get("type") == "ORDER_ID" and related.get("key"):
                financial_order_keys.add(str(related["key"]))
    order_key_set = set(order_keys)
    inventory_key_set = set(inventory_keys)
    finance_without_order = sorted(financial_order_keys - order_key_set)
    shipped_order_without_finance = sorted(eligible_finance_order_keys - financial_order_keys)
    fba_order_sku_without_inventory = sorted(fba_order_sku_keys - inventory_key_set)
    inventory_sku_without_window_order = sorted(inventory_key_set - fba_order_sku_keys)
    duplicate_order_keys = sorted(key for key, count in Counter(order_keys).items() if count > 1)
    duplicate_inventory_keys = sorted(key for key, count in Counter(inventory_keys).items() if count > 1)
    matched_three_way_order_keys = sorted(
        order_key for order_key, sku_keys in eligible_three_way_order_skus.items()
        if order_key in financial_order_keys
        and order_key not in eligible_three_way_order_missing_sku_keys
        and bool(sku_keys)
        and sku_keys <= inventory_key_set
    )
    unmatched_three_way_order_keys = sorted(
        set(eligible_three_way_order_skus) - set(matched_three_way_order_keys)
    )
    all_marketplaces = set(marketplace_counts) | set(inventory_marketplace_counts) | set(
        transaction_summary["marketplace_counts"]
    )
    transaction_marketplace_counts = Counter(transaction_summary["marketplace_counts"])
    blockers = []
    if duplicate_order_keys:
        blockers.append({"code": "duplicate_amazon_order_key"})
    if duplicate_inventory_keys:
        blockers.append({"code": "duplicate_amazon_inventory_sku_key"})
    if len(all_marketplaces) != 1:
        blockers.append({"code": "cross_marketplace_evidence"})
    if not transaction_summary["ready"]:
        blockers.extend(transaction_summary["blockers"])
    return {
        "ready": not blockers,
        "entity_id": context.entity_id,
        "period": canonical_period,
        "canonical_month_scope": bool(canonical_period),
        "marketplace_id": expected_marketplace_id or (
            next(iter(all_marketplaces)) if len(all_marketplaces) == 1 else None
        ),
        "order_count": len(orders),
        "order_item_count": total_order_items,
        "order_quantity": total_order_quantity,
        "inventory_sku_count": len(inventory),
        "transaction_count": len(transactions),
        "order_status_counts": dict(sorted(order_status_counts.items())),
        "fulfilled_by_counts": dict(sorted(fulfilled_by_counts.items())),
        "marketplace_counts": dict(sorted(
            (
                key,
                marketplace_counts[key]
                + inventory_marketplace_counts[key]
                + transaction_marketplace_counts[key],
            )
            for key in all_marketplaces
        )),
        "inventory_quantity_summary": inventory_totals,
        "transaction_currency_summary": transaction_summary["currency_summary"],
        "finance_order_reference_count": len(financial_order_keys),
        "eligible_three_way_order_count": len(eligible_three_way_order_skus),
        "matched_three_way_order_count": len(matched_three_way_order_keys),
        "three_way_match_rate": (
            format(
                Decimal(len(matched_three_way_order_keys))
                / Decimal(len(eligible_three_way_order_skus)),
                "f",
            )
            if eligible_three_way_order_skus else None
        ),
        "unmatched_three_way_order_keys": unmatched_three_way_order_keys,
        "finance_without_order_keys": finance_without_order,
        "shipped_order_without_finance_keys": shipped_order_without_finance,
        "fba_order_sku_without_inventory_keys": fba_order_sku_without_inventory,
        "inventory_sku_without_window_order_keys": inventory_sku_without_window_order,
        "inventory_quantity_field_missing_keys": sorted(
            inventory_quantity_field_missing_keys
        ),
        "duplicate_order_keys": duplicate_order_keys,
        "duplicate_inventory_keys": duplicate_inventory_keys,
        "blockers": blockers,
        "candidate_only": True,
        "cross_source_difference_candidate_only": True,
        "three_way_scope_match_is_not_completeness_claim": True,
        "hashed_cross_source_keys_generated": True,
        "hashed_cross_source_keys_human_reviewed": False,
        "current_inventory_not_historical_period_end": True,
        "order_or_financial_completeness_proven": False,
        "inventory_valuation_or_cogs_performed": False,
        "revenue_recognition_performed": False,
        "tax_liability_determined": False,
        "settlement_or_bank_reconciliation_performed": False,
        "posting_or_inventory_adjustment_performed": False,
        "external_actions_performed": False,
        "review_boundaries": [
            "Orders and Finances use different event clocks and can legitimately differ across one window.",
            "The FBA Inventory source is a current observation, not a reconstructed historical period-end balance.",
            "Hashed order and SKU joins expose differences without retaining raw Amazon identifiers.",
            "Zero differences do not prove order, refund, settlement, bank, inventory or tax completeness.",
        ],
    }
