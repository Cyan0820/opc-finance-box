from __future__ import annotations

from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
from typing import Any

from .pack_services import ServiceContext


def _rows(payload: dict[str, Any], field: str, context: ServiceContext) -> list[dict[str, Any]]:
    rows = payload.get(field) or []
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError(f"{field} must be a list of objects")
    invalid = [
        str(row.get("transaction_id") or row.get("balance_transaction_id") or row.get("shopify_transaction_id") or index)
        for index, row in enumerate(rows, 1)
        if row.get("entity_id") != context.entity_id
    ]
    if invalid:
        raise ValueError(
            f"{field} contains records outside statutory entity {context.entity_id}: {', '.join(invalid)}"
        )
    for index, row in enumerate(rows, 1):
        evidence = row.get("evidence")
        if not isinstance(evidence, dict) or not evidence.get("source_file") or not evidence.get("batch_id"):
            raise ValueError(f"{field}[{index}] requires source_file and batch_id evidence")
    return [dict(row) for row in rows]


def _duplicates(rows: list[dict[str, Any]], field: str) -> list[str]:
    values = [str(row.get(field) or "") for row in rows]
    if any(not value for value in values):
        raise ValueError(f"{field} must be present on every row")
    counts = Counter(values)
    return sorted(value for value, count in counts.items() if count > 1)


def _currency_exponents(value: Any) -> dict[str, int]:
    if not isinstance(value, dict) or not value:
        raise ValueError("currency_minor_units must be a non-empty object")
    output = {}
    for key, exponent in value.items():
        currency = str(key).upper()
        if len(currency) != 3 or not currency.isalpha():
            raise ValueError("currency_minor_units keys must be three-letter currency codes")
        if not isinstance(exponent, int) or isinstance(exponent, bool) or not 0 <= exponent <= 4:
            raise ValueError(f"currency_minor_units.{currency} must be an integer from 0 to 4")
        output[currency] = exponent
    return output


def _shop_money_minor(row: dict[str, Any], exponents: dict[str, int], field: str) -> tuple[int, str]:
    amount_set = row.get("amount_set")
    if not isinstance(amount_set, dict) or not isinstance(amount_set.get("shop_money"), dict):
        raise ValueError(f"{field}.amount_set.shop_money is required")
    money = amount_set["shop_money"]
    currency = str(money.get("currency") or "").upper()
    if currency not in exponents:
        raise ValueError(f"currency_minor_units is missing {currency}")
    try:
        amount = Decimal(str(money.get("amount")))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field}.amount must be a finite decimal") from exc
    if not amount.is_finite():
        raise ValueError(f"{field}.amount must be a finite decimal")
    scale = Decimal(10) ** exponents[currency]
    minor = amount * scale
    if minor != minor.to_integral_value():
        raise ValueError(f"{field}.amount has more precision than configured for {currency}")
    return int(minor), currency


def reconcile_shopify_stripe_activity(payload: dict[str, Any], context: ServiceContext) -> dict[str, Any]:
    transactions = _rows(payload, "shopify_transactions", context)
    balances = _rows(payload, "stripe_balance_transactions", context)
    links = _rows(payload, "processor_links", context)
    exponents = _currency_exponents(payload.get("currency_minor_units"))
    duplicate_inputs = {
        "shopify_transaction_ids": _duplicates(transactions, "transaction_id"),
        "stripe_balance_transaction_ids": _duplicates(balances, "balance_transaction_id"),
        "linked_shopify_transaction_ids": _duplicates(links, "shopify_transaction_id"),
        "linked_stripe_source_object_ids": _duplicates(links, "stripe_source_object_id"),
    }
    tx_by_id = {str(row["transaction_id"]): row for row in transactions}
    balances_by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in balances:
        source_id = str(row.get("source_object_id") or "")
        if source_id:
            balances_by_source[source_id].append(row)
    links_by_tx = {str(row["shopify_transaction_id"]): row for row in links}
    relevant = [
        row for row in transactions
        if row.get("status") == "SUCCESS" and row.get("kind") in {"SALE", "CAPTURE", "REFUND"}
    ]
    excluded_transactions = sorted(
        str(row["transaction_id"]) for row in transactions if row not in relevant
    )
    reconciliation = []
    used_balance_ids: set[str] = set()
    summaries: dict[str, dict[str, int]] = defaultdict(
        lambda: {
            "shopify_collection_minor": 0, "shopify_refund_minor": 0,
            "stripe_gross_minor": 0, "stripe_fee_minor": 0, "stripe_net_minor": 0,
            "matched_count": 0, "exception_count": 0,
        }
    )
    for tx in relevant:
        transaction_id = str(tx["transaction_id"])
        kind = str(tx["kind"])
        shopify_minor, currency = _shop_money_minor(tx, exponents, f"shopify transaction {transaction_id}")
        link = links_by_tx.get(transaction_id)
        item = {
            "shopify_transaction_id": transaction_id,
            "order_id": tx.get("order_id"),
            "kind": kind,
            "currency": currency,
            "shopify_amount_minor": shopify_minor,
            "stripe_source_object_id": None,
            "stripe_balance_transaction_id": None,
            "status": "matched",
            "exceptions": [],
        }
        if link is None:
            item["status"] = "missing_processor_link"
            item["exceptions"].append("explicit Shopify-to-Stripe link evidence is missing")
        else:
            source_id = str(link["stripe_source_object_id"])
            item["stripe_source_object_id"] = source_id
            candidates = balances_by_source.get(source_id, [])
            if len(candidates) != 1:
                item["status"] = "missing_or_ambiguous_stripe_source"
                item["exceptions"].append(f"expected one Stripe balance transaction for source {source_id}")
            else:
                balance = candidates[0]
                balance_id = str(balance["balance_transaction_id"])
                item["stripe_balance_transaction_id"] = balance_id
                if balance_id in used_balance_ids:
                    item["status"] = "stripe_balance_reused"
                    item["exceptions"].append("Stripe balance transaction is already linked")
                used_balance_ids.add(balance_id)
                stripe_currency = str(balance.get("currency") or "").upper()
                stripe_amount = balance.get("amount_minor")
                stripe_fee = balance.get("fee_minor")
                stripe_net = balance.get("net_minor")
                if any(not isinstance(value, int) or isinstance(value, bool) for value in (
                    stripe_amount, stripe_fee, stripe_net,
                )):
                    raise ValueError(f"Stripe balance {balance_id} amounts must be integer minor units")
                expected_sign = -1 if kind == "REFUND" else 1
                expected_categories = {"refund", "payment_refund"} if kind == "REFUND" else {"charge", "payment"}
                if stripe_currency != currency:
                    item["exceptions"].append("Shopify and Stripe currencies differ")
                if stripe_amount != expected_sign * shopify_minor:
                    item["exceptions"].append("Shopify and Stripe gross amounts differ")
                if balance.get("reporting_category") not in expected_categories:
                    item["exceptions"].append("Stripe reporting category does not match transaction kind")
                if stripe_amount - stripe_fee != stripe_net:
                    item["exceptions"].append("Stripe amount minus fee does not equal net")
                item.update({
                    "stripe_amount_minor": stripe_amount,
                    "stripe_fee_minor": stripe_fee,
                    "stripe_net_minor": stripe_net,
                    "stripe_reporting_category": balance.get("reporting_category"),
                })
                if item["exceptions"] and item["status"] == "matched":
                    item["status"] = "amount_currency_or_category_mismatch"
        summary = summaries[currency]
        if kind == "REFUND":
            summary["shopify_refund_minor"] += shopify_minor
        else:
            summary["shopify_collection_minor"] += shopify_minor
        if item["stripe_balance_transaction_id"]:
            summary["stripe_gross_minor"] += int(item.get("stripe_amount_minor") or 0)
            summary["stripe_fee_minor"] += int(item.get("stripe_fee_minor") or 0)
            summary["stripe_net_minor"] += int(item.get("stripe_net_minor") or 0)
        if item["status"] == "matched":
            summary["matched_count"] += 1
        else:
            summary["exception_count"] += 1
        reconciliation.append(item)

    unused_links = sorted(set(links_by_tx) - set(tx_by_id))
    linked_sources = {str(link["stripe_source_object_id"]) for link in links}
    unused_stripe_balance_ids = sorted(
        str(row["balance_transaction_id"]) for row in balances
        if str(row.get("source_object_id") or "") not in linked_sources
        and row.get("reporting_category") != "payout"
    )
    blockers = []
    if any(duplicate_inputs.values()):
        blockers.append("duplicate transaction, balance or link business keys")
    if any(item["status"] != "matched" for item in reconciliation):
        blockers.append("Shopify and Stripe activity contains unmatched or inconsistent records")
    if unused_links:
        blockers.append("processor links reference missing Shopify transactions")
    if not relevant:
        blockers.append("no successful Shopify financial transactions available")
    summary_rows = [{"currency": currency, **values} for currency, values in sorted(summaries.items())]
    return {
        "ready": not blockers,
        "ready_for_order_to_cash_review": not blockers,
        "ready_for_revenue_recognition": False,
        "entity_id": context.entity_id,
        "currency_minor_units": exponents,
        "reconciliation": reconciliation,
        "currency_summary": summary_rows,
        "duplicate_inputs": duplicate_inputs,
        "excluded_transaction_ids": excluded_transactions,
        "unused_processor_link_transaction_ids": unused_links,
        "unused_stripe_balance_transaction_ids": unused_stripe_balance_ids,
        "blockers": blockers,
        "founder_briefing": {
            "facts_by_currency": summary_rows,
            "risk_signals": [
                {"code": item["status"], "shopify_transaction_id": item["shopify_transaction_id"]}
                for item in reconciliation if item["status"] != "matched"
            ],
            "cross_currency_total_prohibited": True,
            "revenue_claim_prohibited": True,
        },
        "candidate_only": True,
        "revenue_recognition_performed": False,
        "posting_performed": False,
        "review_gate": "processor_link_mapping_approval",
        "guardrail": (
            "Cross-processor reconciliation requires explicit evidence links and configured currency exponents. "
            "It validates cash activity but does not decide revenue, tax, COGS or ledger treatment."
        ),
    }
