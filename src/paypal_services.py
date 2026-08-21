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
        raise ValueError(f"{field} must be a finite decimal") from exc
    if not result.is_finite():
        raise ValueError(f"{field} must be a finite decimal")
    return result


def _currency(value: Any, field: str) -> str:
    currency = str(value or "").upper()
    if not re.fullmatch(r"[A-Z]{3}", currency):
        raise ValueError(f"{field} must be a three-letter currency code")
    return currency


def _format(value: Decimal) -> str:
    return format(value, "f")


def summarize_paypal_transaction_activity(
    payload: dict[str, Any], context: ServiceContext,
) -> dict[str, Any]:
    rows = payload.get("transactions") or []
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("transactions must be a list of objects")
    foreign = [
        str(row.get("paypal_transaction_key") or index)
        for index, row in enumerate(rows, 1)
        if row.get("entity_id") != context.entity_id
    ]
    if foreign:
        raise ValueError(
            f"transactions contains records outside statutory entity {context.entity_id}: "
            + ", ".join(foreign)
        )

    keys: list[str] = []
    status_counts: Counter[str] = Counter()
    activity_class_counts: Counter[str] = Counter()
    currency_totals: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "transaction_count": 0,
        "amount": Decimal("0"),
        "fee": Decimal("0"),
        "net_when_same_currency": Decimal("0"),
        "refund_outflow": Decimal("0"),
        "reversal_outflow": Decimal("0"),
        "withdrawal_or_transfer_outflow": Decimal("0"),
    })
    event_totals: dict[tuple[str, str], dict[str, Any]] = defaultdict(lambda: {
        "transaction_count": 0, "amount": Decimal("0"), "fee_when_same_currency": Decimal("0"),
    })
    fee_currency_totals: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    cross_currency_fee_keys: list[str] = []
    arithmetic_exceptions: list[str] = []
    reference_review_keys: list[str] = []

    for index, row in enumerate(rows, 1):
        key = str(row.get("paypal_transaction_key") or "")
        if not re.fullmatch(r"[0-9a-f]{64}", key):
            raise ValueError(f"transactions[{index}].paypal_transaction_key must be a SHA-256 hex digest")
        keys.append(key)
        evidence = row.get("evidence")
        if not isinstance(evidence, dict) or not evidence.get("source_file") or not evidence.get("batch_id"):
            raise ValueError(f"transactions[{index}] requires source_file and batch_id evidence")
        event_code = str(row.get("event_code") or "")
        if not re.fullmatch(r"T[0-9]{4}", event_code):
            raise ValueError(f"transactions[{index}].event_code is invalid")
        amount_currency = _currency(row.get("amount_currency"), f"transactions[{index}].amount_currency")
        fee_currency = _currency(row.get("fee_currency"), f"transactions[{index}].fee_currency")
        amount = _decimal(row.get("amount"), f"transactions[{index}].amount")
        fee = _decimal(row.get("fee"), f"transactions[{index}].fee")
        status_counts[str(row.get("transaction_status") or "unknown")] += 1
        activity_class = str(row.get("activity_class") or "other_balance_activity")
        activity_class_counts[activity_class] += 1
        amount_bucket = currency_totals[amount_currency]
        amount_bucket["transaction_count"] += 1
        amount_bucket["amount"] += amount
        fee_currency_totals[fee_currency] += fee
        event_bucket = event_totals[(event_code, amount_currency)]
        event_bucket["transaction_count"] += 1
        event_bucket["amount"] += amount
        if fee_currency == amount_currency:
            amount_bucket["fee"] += fee
            amount_bucket["net_when_same_currency"] += amount + fee
            event_bucket["fee_when_same_currency"] += fee
            reported_net = row.get("net_when_same_currency")
            if reported_net is None or _decimal(
                reported_net, f"transactions[{index}].net_when_same_currency",
            ) != amount + fee:
                arithmetic_exceptions.append(key)
        else:
            cross_currency_fee_keys.append(key)
            if row.get("net_when_same_currency") is not None:
                arithmetic_exceptions.append(key)
        if row.get("refund_candidate") and amount < 0:
            amount_bucket["refund_outflow"] += -amount
        if row.get("reversal_candidate") and amount < 0:
            amount_bucket["reversal_outflow"] += -amount
        if activity_class == "balance_withdrawal_or_transfer" and amount < 0:
            amount_bucket["withdrawal_or_transfer_outflow"] += -amount
        if (row.get("refund_candidate") or row.get("reversal_candidate")) and not row.get(
            "reference_transaction_key"
        ):
            reference_review_keys.append(key)

    duplicate_keys = sorted(key for key, count in Counter(keys).items() if count > 1)
    blockers = []
    if duplicate_keys:
        blockers.append({"code": "duplicate_paypal_transaction", "count": len(duplicate_keys)})
    if arithmetic_exceptions:
        blockers.append({"code": "paypal_amount_fee_net_mismatch", "count": len(arithmetic_exceptions)})
    currency_summary = [{
        "currency": currency,
        "transaction_count": values["transaction_count"],
        "amount": _format(values["amount"]),
        "fee": _format(values["fee"]),
        "net_when_same_currency": _format(values["net_when_same_currency"]),
        "refund_outflow": _format(values["refund_outflow"]),
        "reversal_outflow": _format(values["reversal_outflow"]),
        "withdrawal_or_transfer_outflow": _format(values["withdrawal_or_transfer_outflow"]),
    } for currency, values in sorted(currency_totals.items())]
    event_summary = [{
        "event_code": event_code,
        "currency": currency,
        "transaction_count": values["transaction_count"],
        "amount": _format(values["amount"]),
        "fee_when_same_currency": _format(values["fee_when_same_currency"]),
    } for (event_code, currency), values in sorted(event_totals.items())]
    return {
        "ready": not blockers,
        "entity_id": context.entity_id,
        "transaction_count": len(rows),
        "empty_period": not rows,
        "currency_summary": currency_summary,
        "fee_totals_by_currency": [
            {"currency": currency, "fee": _format(value)}
            for currency, value in sorted(fee_currency_totals.items())
        ],
        "event_summary": event_summary,
        "status_counts": dict(sorted(status_counts.items())),
        "activity_class_counts": dict(sorted(activity_class_counts.items())),
        "refund_candidate_count": sum(bool(row.get("refund_candidate")) for row in rows),
        "reversal_candidate_count": sum(bool(row.get("reversal_candidate")) for row in rows),
        "fee_refund_or_reversal_count": sum(bool(row.get("fee_refund_or_reversal")) for row in rows),
        "reference_review_required_count": len(reference_review_keys),
        "cross_currency_fee_count": len(cross_currency_fee_keys),
        "duplicate_transaction_keys": duplicate_keys,
        "arithmetic_exception_keys": sorted(set(arithmetic_exceptions)),
        "blockers": blockers,
        "candidate_only": True,
        "cross_currency_total_prohibited": True,
        "revenue_recognition_performed": False,
        "refund_accounting_performed": False,
        "bank_reconciliation_performed": False,
        "cash_allocation_performed": False,
        "posting_performed": False,
        "external_actions_performed": False,
        "review_boundaries": [
            "PayPal T-codes classify processor money movement; they do not determine revenue or ledger accounts.",
            "Refund and reversal candidates require explicit source-document and original-transaction review.",
            "PayPal balance withdrawals are not bank receipts until independently matched to bank evidence.",
        ],
    }
