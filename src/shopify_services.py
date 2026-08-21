from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from .pack_services import ServiceContext


def _rows(payload: dict[str, Any], field: str, context: ServiceContext) -> list[dict[str, Any]]:
    value = payload.get(field) or []
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise ValueError(f"{field} must be a list of objects")
    invalid = [
        str(row.get("order_id") or row.get("transaction_id") or row.get("refund_id") or index)
        for index, row in enumerate(value, 1)
        if row.get("entity_id") != context.entity_id
    ]
    if invalid:
        raise ValueError(
            f"{field} contains records outside statutory entity {context.entity_id}: {', '.join(invalid)}"
        )
    for index, row in enumerate(value, 1):
        evidence = row.get("evidence")
        if not isinstance(evidence, dict) or not evidence.get("source_file") or not evidence.get("batch_id"):
            raise ValueError(f"{field}[{index}] requires source_file and batch_id evidence")
    return [dict(row) for row in value]


def _duplicates(rows: list[dict[str, Any]], field: str) -> list[str]:
    values = [str(row.get(field) or "") for row in rows]
    if any(not value for value in values):
        raise ValueError(f"{field} must be present on every row")
    counts = Counter(values)
    return sorted(value for value, count in counts.items() if count > 1)


def _decimal(value: Any, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field} must be a finite decimal string") from exc
    if not result.is_finite():
        raise ValueError(f"{field} must be a finite decimal string")
    return result


def _money(value: Any, field: str) -> tuple[Decimal, str]:
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be a money object")
    amount = _decimal(value.get("amount"), f"{field}.amount")
    currency = str(value.get("currency") or "").upper()
    if len(currency) != 3 or not currency.isalpha():
        raise ValueError(f"{field}.currency must be a three-letter code")
    return amount, currency


def _bag(row: dict[str, Any], field: str, view: str) -> tuple[Decimal, str]:
    money = row.get("money")
    if not isinstance(money, dict) or not isinstance(money.get(field), dict):
        raise ValueError(f"order {row.get('order_id')} requires money.{field}")
    return _money(money[field].get(view), f"money.{field}.{view}")


def _amount_set(row: dict[str, Any], view: str, field: str) -> tuple[Decimal, str]:
    value = row.get(field)
    if not isinstance(value, dict):
        raise ValueError(f"{field} is required")
    return _money(value.get(view), f"{field}.{view}")


def _as_text(value: Decimal) -> str:
    return format(value, "f")


def summarize_shopify_order_activity(payload: dict[str, Any], context: ServiceContext) -> dict[str, Any]:
    orders = _rows(payload, "orders", context)
    transactions = _rows(payload, "transactions", context)
    refunds = _rows(payload, "refunds", context)
    duplicate_inputs = {
        "order_ids": _duplicates(orders, "order_id"),
        "transaction_ids": _duplicates(transactions, "transaction_id"),
        "refund_ids": _duplicates(refunds, "refund_id"),
    }
    include_test = payload.get("include_test_orders") is True
    if include_test and context.runtime.snapshot()["data_mode"] != "demo":
        raise ValueError("include_test_orders is allowed only in demo data_mode")
    order_ids = {str(order["order_id"]) for order in orders}
    orphan_transactions = sorted({
        str(row["transaction_id"]) for row in transactions if str(row.get("order_id") or "") not in order_ids
    })
    orphan_refunds = sorted({
        str(row["refund_id"]) for row in refunds if str(row.get("order_id") or "") not in order_ids
    })
    tx_by_order: dict[str, list[dict[str, Any]]] = defaultdict(list)
    refunds_by_order: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in transactions:
        tx_by_order[str(row.get("order_id") or "")].append(row)
    for row in refunds:
        refunds_by_order[str(row.get("order_id") or "")].append(row)

    active_orders = [order for order in orders if include_test or order.get("test") is not True]
    excluded_test_order_ids = sorted(
        str(order["order_id"]) for order in orders if order.get("test") is True and not include_test
    )
    order_reviews = []
    aggregates: dict[tuple[str, str], dict[str, Decimal | int]] = defaultdict(
        lambda: {
            "order_count": 0, "original_order_total": Decimal("0"),
            "current_order_total": Decimal("0"), "reported_received": Decimal("0"),
            "reported_refunded": Decimal("0"), "successful_collections": Decimal("0"),
            "successful_refund_transactions": Decimal("0"), "refund_object_total": Decimal("0"),
        }
    )
    risk_signals = []
    for order in active_orders:
        order_id = str(order["order_id"])
        review = {
            "order_id": order_id,
            "order_name": order.get("order_name"),
            "destination_country": order.get("destination_country"),
            "financial_status": order.get("financial_status"),
            "fulfillment_status": order.get("fulfillment_status"),
            "cancelled_at": order.get("cancelled_at"),
            "money_views": {},
            "exceptions": [],
        }
        for view in ("shop_money", "presentment_money"):
            original_total, currency = _bag(order, "totalPriceSet", view)
            current_total, current_currency = _bag(order, "currentTotalPriceSet", view)
            reported_received, received_currency = _bag(order, "totalReceivedSet", view)
            reported_refunded, refunded_currency = _bag(order, "totalRefundedSet", view)
            currencies = {currency, current_currency, received_currency, refunded_currency}
            if len(currencies) != 1:
                review["exceptions"].append(f"{view}: inconsistent order currencies")
                continue
            successful_collections = Decimal("0")
            successful_refunds = Decimal("0")
            pending_or_failed = []
            for tx in tx_by_order[order_id]:
                amount, tx_currency = _amount_set(tx, view, "amount_set")
                if tx_currency != currency:
                    review["exceptions"].append(f"{view}: transaction currency mismatch")
                    continue
                status = str(tx.get("status") or "")
                kind = str(tx.get("kind") or "")
                if status == "SUCCESS" and kind in {"SALE", "CAPTURE"}:
                    successful_collections += amount
                elif status == "SUCCESS" and kind == "REFUND":
                    successful_refunds += amount
                elif status in {"PENDING", "AWAITING_RESPONSE", "FAILURE", "ERROR", "UNKNOWN"}:
                    pending_or_failed.append(str(tx["transaction_id"]))
            refund_total = Decimal("0")
            for refund in refunds_by_order[order_id]:
                amount, refund_currency = _amount_set(refund, view, "total_refunded_set")
                if refund_currency != currency:
                    review["exceptions"].append(f"{view}: refund currency mismatch")
                    continue
                refund_total += amount
            collection_difference = successful_collections - reported_received
            refund_transaction_difference = successful_refunds - reported_refunded
            refund_object_difference = refund_total - reported_refunded
            for code, difference in (
                ("successful_collection_vs_total_received", collection_difference),
                ("successful_refund_transaction_vs_total_refunded", refund_transaction_difference),
                ("refund_objects_vs_total_refunded", refund_object_difference),
            ):
                if difference != 0:
                    review["exceptions"].append(f"{view}: {code}={_as_text(difference)} {currency}")
            if pending_or_failed:
                review["exceptions"].append(f"{view}: pending_or_failed_transactions={len(pending_or_failed)}")
            review["money_views"][view] = {
                "currency": currency,
                "original_order_total": _as_text(original_total),
                "current_order_total": _as_text(current_total),
                "reported_received": _as_text(reported_received),
                "reported_refunded": _as_text(reported_refunded),
                "successful_collections": _as_text(successful_collections),
                "successful_refund_transactions": _as_text(successful_refunds),
                "refund_object_total": _as_text(refund_total),
                "collection_difference": _as_text(collection_difference),
                "refund_transaction_difference": _as_text(refund_transaction_difference),
                "refund_object_difference": _as_text(refund_object_difference),
            }
            aggregate = aggregates[(view, currency)]
            aggregate["order_count"] = int(aggregate["order_count"]) + 1
            aggregate["original_order_total"] = Decimal(aggregate["original_order_total"]) + original_total
            aggregate["current_order_total"] = Decimal(aggregate["current_order_total"]) + current_total
            aggregate["reported_received"] = Decimal(aggregate["reported_received"]) + reported_received
            aggregate["reported_refunded"] = Decimal(aggregate["reported_refunded"]) + reported_refunded
            aggregate["successful_collections"] = Decimal(aggregate["successful_collections"]) + successful_collections
            aggregate["successful_refund_transactions"] = Decimal(aggregate["successful_refund_transactions"]) + successful_refunds
            aggregate["refund_object_total"] = Decimal(aggregate["refund_object_total"]) + refund_total
        if not order.get("destination_country"):
            review["exceptions"].append("missing destination_country")
        if order.get("cancelled_at"):
            review["exceptions"].append("order is canceled; cutoff review required")
        if review["exceptions"]:
            risk_signals.append({
                "code": "shopify_order_exception", "order_id": order_id,
                "exception_count": len(review["exceptions"]),
            })
        order_reviews.append(review)

    aggregate_rows = []
    for (view, currency), values in sorted(aggregates.items()):
        aggregate_rows.append({
            "money_view": view,
            "currency": currency,
            **{
                field: int(value) if field == "order_count" else _as_text(Decimal(value))
                for field, value in values.items()
            },
        })
    blockers = []
    if any(duplicate_inputs.values()):
        blockers.append("duplicate Shopify business keys")
    if orphan_transactions or orphan_refunds:
        blockers.append("orphan transaction or refund records")
    if any(review["exceptions"] for review in order_reviews):
        blockers.append("Shopify order/payment/refund facts contain exceptions")
    if not active_orders:
        blockers.append("no non-test Shopify orders available for review")
    enrichment = {
        "required_for_commerce_margin": [
            "approved revenue presentation and cutoff policy",
            "merchandise amount excluding tax under the approved policy",
            "refund allocation between merchandise, tax, shipping and adjustments",
            "COGS from an approved inventory valuation method",
            "fulfillment and shipping costs",
            "processor settlement fees and bank evidence",
        ],
        "missing_values_are_not_zero": True,
    }
    return {
        "ready": not blockers,
        "ready_for_order_to_cash_review": not blockers,
        "ready_for_commerce_margin": False,
        "entity_id": context.entity_id,
        "order_reviews": order_reviews,
        "currency_summary": aggregate_rows,
        "duplicate_inputs": duplicate_inputs,
        "orphan_transaction_ids": orphan_transactions,
        "orphan_refund_ids": orphan_refunds,
        "excluded_test_order_ids": excluded_test_order_ids,
        "blockers": blockers,
        "enrichment_required": enrichment,
        "founder_briefing": {
            "facts_by_money_view_and_currency": aggregate_rows,
            "risk_signals": risk_signals,
            "excluded_test_order_count": len(excluded_test_order_ids),
            "cross_currency_total_prohibited": True,
            "margin_claim_prohibited": True,
        },
        "revenue_recognition_performed": False,
        "margin_calculation_performed": False,
        "posting_performed": False,
        "guardrail": (
            "Shopify order totals, successful transactions and refunds are source facts only. "
            "Costs, revenue policy, processor settlement and bank evidence require separate approved inputs."
        ),
    }


def _event_in_month(value: Any, start: datetime, end: datetime, field: str) -> bool:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone offset")
    return start <= parsed.astimezone(timezone.utc) < end


def _component_amount(
    component: dict[str, Any], field: str, view: str,
) -> tuple[Decimal, str]:
    value = component.get(field)
    if not isinstance(value, dict):
        raise ValueError(f"refund component requires {field}")
    return _money(value.get(view), f"{field}.{view}")


def _refund_ex_tax(
    refund: dict[str, Any], view: str, expected_currency: str,
) -> tuple[Decimal, dict[str, Any]]:
    total, currency = _amount_set(refund, view, "total_refunded_set")
    if currency != expected_currency:
        raise ValueError(f"refund {refund['refund_id']} currency does not match its order")
    if refund.get("component_contract_complete") is not True:
        raise ValueError(f"refund {refund['refund_id']} lacks complete component connections")
    ex_tax = Decimal("0")
    tax = Decimal("0")
    for field, subtotal_field, tax_field in (
        ("refund_line_items", "subtotal_set", "total_tax_set"),
        ("refund_shipping_lines", "subtotal_amount_set", "tax_amount_set"),
    ):
        components = refund.get(field)
        if not isinstance(components, list):
            raise ValueError(f"refund {refund['refund_id']} requires {field}")
        for component in components:
            subtotal, subtotal_currency = _component_amount(component, subtotal_field, view)
            component_tax, tax_currency = _component_amount(component, tax_field, view)
            if {subtotal_currency, tax_currency} != {currency}:
                raise ValueError(f"refund {refund['refund_id']} component currency mismatch")
            ex_tax += subtotal
            tax += component_tax

    adjustment_amount = Decimal("0")
    adjustment_tax = Decimal("0")
    adjustments = refund.get("order_adjustments")
    if not isinstance(adjustments, list):
        raise ValueError(f"refund {refund['refund_id']} requires order_adjustments")
    for component in adjustments:
        amount, amount_currency = _component_amount(component, "amount_set", view)
        component_tax, tax_currency = _component_amount(component, "tax_amount_set", view)
        if {amount_currency, tax_currency} != {currency}:
            raise ValueError(f"refund {refund['refund_id']} adjustment currency mismatch")
        adjustment_amount += amount
        adjustment_tax += component_tax

    inclusive_candidate = ex_tax + tax + adjustment_amount
    exclusive_candidate = ex_tax + tax + adjustment_amount + adjustment_tax
    inclusive_matches = abs(inclusive_candidate - total) <= Decimal("0.01")
    exclusive_matches = abs(exclusive_candidate - total) <= Decimal("0.01")
    if not inclusive_matches and not exclusive_matches:
        raise ValueError(
            f"refund {refund['refund_id']} components do not reconcile to total_refunded_set"
        )
    if inclusive_matches:
        adjustment_ex_tax = adjustment_amount - adjustment_tax
        adjustment_semantics = "amount_includes_tax"
    else:
        adjustment_ex_tax = adjustment_amount
        adjustment_semantics = "amount_excludes_tax"
    ex_tax += adjustment_ex_tax

    successful = Decimal("0")
    transactions = refund.get("refund_transactions")
    if not isinstance(transactions, list) or not transactions:
        raise ValueError(f"refund {refund['refund_id']} requires associated transactions")
    successful_transaction_ids = []
    for transaction in transactions:
        transaction_id = str(transaction.get("transaction_id") or "")
        if not transaction_id:
            raise ValueError(f"refund {refund['refund_id']} transaction requires id")
        if transaction.get("status") == "SUCCESS" and transaction.get("kind") == "REFUND":
            amount, transaction_currency = _component_amount(transaction, "amount_set", view)
            if transaction_currency != currency:
                raise ValueError(f"refund {refund['refund_id']} transaction currency mismatch")
            successful += amount
            successful_transaction_ids.append(transaction_id)
    if abs(successful - total) > Decimal("0.01"):
        raise ValueError(
            f"refund {refund['refund_id']} successful transactions do not reconcile to total_refunded_set"
        )
    return ex_tax, {
        "refund_id": str(refund["refund_id"]),
        "currency": currency,
        "refund_total": _as_text(total),
        "refund_ex_tax": _as_text(ex_tax),
        "refund_tax": _as_text(total - ex_tax),
        "adjustment_semantics": adjustment_semantics,
        "successful_transaction_ids": sorted(successful_transaction_ids),
    }


def build_shopify_monthly_commerce_scope(
    payload: dict[str, Any], context: ServiceContext,
) -> dict[str, Any]:
    """Build value-bearing monthly DTC operands only from a close-captured Shopify source batch."""
    orders = _rows(payload, "orders", context)
    refunds = _rows(payload, "refunds", context)
    source_scope = payload.get("source_scope")
    if not isinstance(source_scope, dict):
        raise ValueError("source_scope must be the Shopify monthly connector source contract")
    period = str(source_scope.get("canonical_month_period") or "")
    interval_start = str(source_scope.get("interval_start") or "")
    interval_end = str(source_scope.get("interval_end") or "")
    if source_scope.get("interval_semantics") != "half_open_utc_calendar_month":
        raise ValueError("source_scope must declare half_open_utc_calendar_month semantics")
    try:
        start = datetime.fromisoformat(interval_start.replace("Z", "+00:00")).astimezone(timezone.utc)
        end = datetime.fromisoformat(interval_end.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError as exc:
        raise ValueError("source_scope interval bounds must be ISO-8601 timestamps") from exc
    if period != start.strftime("%Y-%m") or not start < end:
        raise ValueError("source_scope period and interval bounds do not agree")
    if source_scope.get("refund_event_membership_uses_processed_at") is not True:
        raise ValueError("source_scope must bind refund membership to processed_at")
    if _duplicates(orders, "order_id") or _duplicates(refunds, "refund_id"):
        raise ValueError("Shopify monthly source contains duplicate business keys")

    include_test = payload.get("include_test_orders") is True
    if include_test and context.runtime.snapshot()["data_mode"] != "demo":
        raise ValueError("include_test_orders is allowed only in demo data_mode")
    order_by_id = {str(row["order_id"]): row for row in orders}
    created_orders = [
        row for row in orders
        if "created" in (row.get("source_populations") or [])
        and (include_test or row.get("test") is not True)
    ]
    monthly_refunds = [
        row for row in refunds
        if _event_in_month(row.get("processed_at"), start, end, f"refund {row.get('refund_id')}.processed_at")
        and (
            include_test
            or (order_by_id.get(str(row.get("order_id") or "")) or {}).get("test") is not True
        )
    ]
    blockers: list[str] = []
    if not created_orders and not monthly_refunds:
        blockers.append("no created-order or processed-refund evidence exists for the canonical month")
    if any(row.get("taxes_included") is not False for row in created_orders):
        blockers.append("tax-inclusive or unknown-tax Shopify orders require an approved allocation policy")
    orphan_refunds = sorted(
        str(row.get("refund_id")) for row in monthly_refunds
        if str(row.get("order_id") or "") not in order_by_id
    )
    if orphan_refunds:
        blockers.append("monthly refunds are missing their parent order snapshot")

    buckets: dict[str, dict[str, Decimal | int]] = defaultdict(
        lambda: {
            "created_order_count": 0,
            "refund_event_count": 0,
            "gross_order_sales_ex_tax_including_shipping": Decimal("0"),
            "discounts_and_refunds_ex_tax": Decimal("0"),
            "gross_merchandise_sales_ex_tax": Decimal("0"),
            "refunds_ex_tax": Decimal("0"),
        }
    )
    refund_reviews: list[dict[str, Any]] = []
    if not blockers:
        try:
            for order in created_orders:
                subtotal, currency = _bag(order, "subtotalPriceSet", "shop_money")
                discounts, discount_currency = _bag(order, "totalDiscountsSet", "shop_money")
                shipping, shipping_currency = _bag(order, "totalShippingPriceSet", "shop_money")
                if {currency, discount_currency, shipping_currency} != {currency}:
                    raise ValueError(f"order {order['order_id']} original amount currencies disagree")
                bucket = buckets[currency]
                bucket["created_order_count"] = int(bucket["created_order_count"]) + 1
                bucket["gross_order_sales_ex_tax_including_shipping"] = Decimal(
                    bucket["gross_order_sales_ex_tax_including_shipping"]
                ) + subtotal + discounts + shipping
                bucket["discounts_and_refunds_ex_tax"] = Decimal(
                    bucket["discounts_and_refunds_ex_tax"]
                ) + discounts
                bucket["gross_merchandise_sales_ex_tax"] = Decimal(
                    bucket["gross_merchandise_sales_ex_tax"]
                ) + subtotal + discounts
            for refund in monthly_refunds:
                parent = order_by_id[str(refund["order_id"])]
                if parent.get("taxes_included") is not False:
                    raise ValueError(
                        f"refund {refund['refund_id']} belongs to a tax-inclusive or unknown-tax order"
                    )
                currency = str(parent.get("shop_currency") or "")
                ex_tax, review = _refund_ex_tax(refund, "shop_money", currency)
                refund_reviews.append(review)
                bucket = buckets[currency]
                bucket["refund_event_count"] = int(bucket["refund_event_count"]) + 1
                bucket["discounts_and_refunds_ex_tax"] = Decimal(
                    bucket["discounts_and_refunds_ex_tax"]
                ) + ex_tax
                bucket["refunds_ex_tax"] = Decimal(bucket["refunds_ex_tax"]) + ex_tax
        except ValueError as exc:
            blockers.append(str(exc))

    rows = [] if blockers else [{
        "entity_id": context.entity_id,
        "period": period,
        "currency": currency,
        **{
            key: int(value) if key.endswith("_count") else _as_text(Decimal(value))
            for key, value in values.items()
        },
    } for currency, values in sorted(buckets.items())]
    return {
        "ready": not blockers,
        "entity_id": context.entity_id,
        "period": period,
        "monthly_commerce_scope": rows,
        "refund_reviews": refund_reviews,
        "excluded_test_order_count": len([
            row for row in orders if row.get("test") is True and not include_test
        ]),
        "blockers": blockers,
        "canonical_month_scope": not blockers,
        "order_and_refund_period_scope_aligned": not blockers,
        "tax_inclusive_policy_auto_confirmed": False,
        "return_authorization_and_receipt_scope_auto_confirmed": False,
        "historical_snapshot_contract": "close_capture_within_72_hours_after_month_end",
        "raw_source_records_returned": False,
        "revenue_recognition_performed": False,
        "posting_performed": False,
        "guardrail": (
            "Only original tax-exclusive order amounts and successfully paid refund events are aggregated. "
            "Tax-inclusive allocation and physical return authorization/receipt remain human controls."
        ),
    }
