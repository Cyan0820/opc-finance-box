from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, timezone
import re
from typing import Any

from .pack_services import ServiceContext


def _rows(payload: dict[str, Any], field: str, context: ServiceContext) -> list[dict[str, Any]]:
    rows = payload.get(field) or []
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError(f"{field} must be a list of objects")
    invalid = [
        str(row.get("balance_transaction_id") or row.get("payout_id") or row.get("bank_transaction_id") or index)
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


def _minor(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{field} must be an integer in the currency's smallest unit")
    return value


def _currency(value: Any, field: str) -> str:
    currency = str(value or "").upper()
    if len(currency) != 3 or not currency.isalpha():
        raise ValueError(f"{field} must be a three-letter currency code")
    return currency


def _duplicate_ids(rows: list[dict[str, Any]], field: str) -> list[str]:
    values = [str(row.get(field) or "") for row in rows]
    missing = [str(index) for index, value in enumerate(values, 1) if not value]
    if missing:
        raise ValueError(f"missing {field} at rows: {', '.join(missing)}")
    counts = Counter(values)
    return sorted(value for value, count in counts.items() if count > 1)


def _date_value(value: Any, field: str) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc).date()
        except (OverflowError, OSError, ValueError) as exc:
            raise ValueError(f"{field} is outside the supported timestamp range") from exc
    text = str(value)
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(text[:10])
        except ValueError as exc:
            raise ValueError(f"{field} must be a Unix timestamp or ISO date") from exc


def summarize_stripe_balance_activity(payload: dict[str, Any], context: ServiceContext) -> dict[str, Any]:
    transactions = _rows(payload, "balance_transactions", context)
    duplicates = _duplicate_ids(transactions, "balance_transaction_id")
    buckets: dict[tuple[str, str], dict[str, int]] = defaultdict(
        lambda: {"transaction_count": 0, "amount_minor": 0, "fee_minor": 0, "net_minor": 0}
    )
    arithmetic_exceptions = []
    status_counts: Counter[str] = Counter()
    for index, row in enumerate(transactions, 1):
        currency = _currency(row.get("currency"), f"balance_transactions[{index}].currency")
        category = str(row.get("reporting_category") or "unclassified")
        amount = _minor(row.get("amount_minor"), f"balance_transactions[{index}].amount_minor")
        fee = _minor(row.get("fee_minor"), f"balance_transactions[{index}].fee_minor")
        net = _minor(row.get("net_minor"), f"balance_transactions[{index}].net_minor")
        bucket = buckets[(currency, category)]
        bucket["transaction_count"] += 1
        bucket["amount_minor"] += amount
        bucket["fee_minor"] += fee
        bucket["net_minor"] += net
        status_counts[str(row.get("status") or "unknown")] += 1
        if amount - fee != net:
            arithmetic_exceptions.append({
                "balance_transaction_id": row["balance_transaction_id"],
                "currency": currency,
                "amount_minor": amount,
                "fee_minor": fee,
                "reported_net_minor": net,
                "expected_net_minor": amount - fee,
            })
    categories = [{
        "currency": currency,
        "reporting_category": category,
        **totals,
    } for (currency, category), totals in sorted(buckets.items())]
    currency_summary: dict[str, dict[str, int]] = defaultdict(
        lambda: {"transaction_count": 0, "amount_minor": 0, "fee_minor": 0, "net_minor": 0,
                 "refund_outflow_minor": 0}
    )
    for row in categories:
        summary = currency_summary[row["currency"]]
        for field in ("transaction_count", "amount_minor", "fee_minor", "net_minor"):
            summary[field] += row[field]
        if row["reporting_category"] in {"refund", "payment_refund"} and row["amount_minor"] < 0:
            summary["refund_outflow_minor"] += -row["amount_minor"]
    risks = []
    if duplicates:
        risks.append({"code": "duplicate_balance_transaction", "count": len(duplicates), "severity": "blocking"})
    if arithmetic_exceptions:
        risks.append({"code": "stripe_net_arithmetic_mismatch", "count": len(arithmetic_exceptions), "severity": "blocking"})
    if status_counts.get("pending"):
        risks.append({"code": "pending_stripe_balance", "count": status_counts["pending"], "severity": "watch"})
    return {
        "ready": bool(transactions) and not duplicates and not arithmetic_exceptions,
        "entity_id": context.entity_id,
        "amount_unit": "currency_minor_unit_integer",
        "category_summary": categories,
        "currency_summary": [
            {"currency": currency, **totals} for currency, totals in sorted(currency_summary.items())
        ],
        "status_counts": dict(sorted(status_counts.items())),
        "duplicate_balance_transaction_ids": duplicates,
        "arithmetic_exceptions": arithmetic_exceptions,
        "founder_briefing": {
            "facts_by_currency": [
                {"currency": currency, **totals} for currency, totals in sorted(currency_summary.items())
            ],
            "risk_signals": risks,
            "cross_currency_total_prohibited": True,
        },
        "posting_performed": False,
        "revenue_recognition_performed": False,
        "guardrail": (
            "Stripe reporting categories summarize processor activity only; revenue, tax, FX and ledger treatment "
            "remain separate approved workflows."
        ),
    }


def reconcile_stripe_payouts(payload: dict[str, Any], context: ServiceContext) -> dict[str, Any]:
    payouts = _rows(payload, "payouts", context)
    balance_transactions = _rows(payload, "balance_transactions", context)
    bank_transactions = _rows(payload, "bank_transactions", context)
    duplicate_payouts = _duplicate_ids(payouts, "payout_id")
    duplicate_balances = _duplicate_ids(balance_transactions, "balance_transaction_id")
    duplicate_banks = _duplicate_ids(bank_transactions, "bank_transaction_id")
    tolerance = payload.get("arrival_date_tolerance_days", 3)
    if not isinstance(tolerance, int) or isinstance(tolerance, bool) or not 0 <= tolerance <= 14:
        raise ValueError("arrival_date_tolerance_days must be an integer from 0 to 14")

    balances = {str(row["balance_transaction_id"]): row for row in balance_transactions}
    bank_by_id = {str(row["bank_transaction_id"]): row for row in bank_transactions}
    candidate_map: dict[str, list[dict[str, str]]] = {}
    base_status: dict[str, dict[str, Any]] = {}
    for index, payout in enumerate(payouts, 1):
        payout_id = str(payout["payout_id"])
        currency = _currency(payout.get("currency"), f"payouts[{index}].currency")
        amount = _minor(payout.get("amount_minor"), f"payouts[{index}].amount_minor")
        if amount <= 0:
            raise ValueError(f"payouts[{index}].amount_minor must be positive")
        balance_id = str(payout.get("balance_transaction_id") or "")
        balance = balances.get(balance_id)
        status = str(payout.get("status") or "unknown")
        item = {
            "payout_id": payout_id,
            "currency": currency,
            "amount_minor": amount,
            "payout_status": status,
            "balance_transaction_id": balance_id or None,
            "balance_check": "matched",
        }
        if status != "paid":
            item["balance_check"] = "not_due_to_bank" if status in {"pending", "in_transit"} else "payout_failed_or_canceled"
            candidate_map[payout_id] = []
            base_status[payout_id] = item
            continue
        if balance is None:
            item["balance_check"] = "missing_balance_transaction"
        else:
            balance_currency = _currency(balance.get("currency"), f"balance_transactions[{balance_id}].currency")
            balance_amount = _minor(balance.get("amount_minor"), f"balance_transactions[{balance_id}].amount_minor")
            if (
                balance_currency != currency
                or balance_amount != -amount
                or balance.get("reporting_category") != "payout"
            ):
                item["balance_check"] = "balance_amount_currency_or_category_mismatch"

        arrival_date = _date_value(payout.get("arrival_date"), f"payouts[{index}].arrival_date")
        candidates = []
        for bank_index, bank in enumerate(bank_transactions, 1):
            direction = str(bank.get("direction") or "").lower()
            if direction not in {"inflow", "receipt", "credit", "收入"}:
                continue
            bank_currency = _currency(bank.get("currency"), f"bank_transactions[{bank_index}].currency")
            bank_amount = _minor(bank.get("amount_minor"), f"bank_transactions[{bank_index}].amount_minor")
            if bank_amount <= 0:
                raise ValueError(f"bank_transactions[{bank_index}].amount_minor must be positive")
            if bank_currency != currency or bank_amount != amount:
                continue
            reference = " ".join(str(bank.get(field) or "") for field in (
                "stripe_payout_id", "reference", "transaction_id", "summary",
            ))
            exact_reference = payout_id in re.findall(r"[A-Za-z0-9_]+", reference)
            bank_date = _date_value(
                bank.get("transaction_date"), f"bank_transactions[{bank_index}].transaction_date",
            )
            within_window = bool(
                arrival_date and bank_date and abs((bank_date - arrival_date).days) <= tolerance
            )
            if exact_reference or within_window:
                candidates.append({
                    "bank_transaction_id": str(bank["bank_transaction_id"]),
                    "basis": "exact_payout_reference" if exact_reference else "amount_currency_arrival_window",
                })
        candidate_map[payout_id] = candidates
        base_status[payout_id] = item

    single_claims: dict[str, list[str]] = defaultdict(list)
    for payout_id, candidates in candidate_map.items():
        if len(candidates) == 1:
            single_claims[candidates[0]["bank_transaction_id"]].append(payout_id)

    reconciliation = []
    matched_banks: set[str] = set()
    for payout in payouts:
        payout_id = str(payout["payout_id"])
        item = dict(base_status[payout_id])
        candidates = candidate_map[payout_id]
        item["bank_candidate_count"] = len(candidates)
        item["bank_transaction_id"] = None
        item["match_basis"] = None
        if item["payout_status"] != "paid":
            item["reconciliation_status"] = "not_expected_in_bank"
        elif not candidates:
            item["reconciliation_status"] = "bank_receipt_missing"
        elif len(candidates) > 1:
            item["reconciliation_status"] = "ambiguous_bank_candidates"
        elif len(single_claims[candidates[0]["bank_transaction_id"]]) > 1:
            item["reconciliation_status"] = "bank_candidate_reused"
        else:
            candidate = candidates[0]
            item["bank_transaction_id"] = candidate["bank_transaction_id"]
            item["match_basis"] = candidate["basis"]
            item["reconciliation_status"] = (
                "high_confidence_candidate" if candidate["basis"] == "exact_payout_reference"
                else "review_candidate"
            )
            matched_banks.add(candidate["bank_transaction_id"])
        item["human_confirmation_required"] = item["reconciliation_status"] not in {"not_expected_in_bank"}
        reconciliation.append(item)

    blocking_statuses = {
        "bank_receipt_missing", "ambiguous_bank_candidates", "bank_candidate_reused",
    }
    exceptions = [
        item for item in reconciliation
        if item["reconciliation_status"] in blocking_statuses
        or item["balance_check"] in {"missing_balance_transaction", "balance_amount_currency_or_category_mismatch", "payout_failed_or_canceled"}
    ]
    summaries: dict[str, dict[str, int]] = defaultdict(
        lambda: {"payout_count": 0, "paid_payout_count": 0, "payout_amount_minor": 0,
                 "candidate_matched_amount_minor": 0, "exception_count": 0}
    )
    for item in reconciliation:
        summary = summaries[item["currency"]]
        summary["payout_count"] += 1
        if item["payout_status"] == "paid":
            summary["paid_payout_count"] += 1
            summary["payout_amount_minor"] += item["amount_minor"]
        if item["bank_transaction_id"]:
            summary["candidate_matched_amount_minor"] += item["amount_minor"]
        if item in exceptions:
            summary["exception_count"] += 1

    duplicate_inputs = {
        "payout_ids": duplicate_payouts,
        "balance_transaction_ids": duplicate_balances,
        "bank_transaction_ids": duplicate_banks,
    }
    risk_signals = [{
        "code": item["reconciliation_status"] if item["reconciliation_status"] in blocking_statuses else item["balance_check"],
        "payout_id": item["payout_id"],
        "currency": item["currency"],
        "amount_minor": item["amount_minor"],
    } for item in exceptions]
    if any(duplicate_inputs.values()):
        risk_signals.append({"code": "duplicate_input_business_keys", "severity": "blocking"})
    return {
        "ready": bool(payouts) and not any(duplicate_inputs.values()) and not exceptions,
        "ready_for_review": bool(payouts) and not any(duplicate_inputs.values()) and not exceptions,
        "entity_id": context.entity_id,
        "amount_unit": "currency_minor_unit_integer",
        "reconciliation": reconciliation,
        "currency_summary": [
            {"currency": currency, **summary} for currency, summary in sorted(summaries.items())
        ],
        "exceptions": exceptions,
        "duplicate_inputs": duplicate_inputs,
        "unmatched_bank_transaction_ids": sorted(set(bank_by_id) - matched_banks),
        "founder_briefing": {
            "facts_by_currency": [
                {"currency": currency, **summary} for currency, summary in sorted(summaries.items())
            ],
            "risk_signals": risk_signals,
            "cross_currency_total_prohibited": True,
        },
        "candidate_only": True,
        "bank_reconciliation_completed": False,
        "posting_performed": False,
        "period_close_performed": False,
        "review_gate": "stripe_mapping_approval",
        "guardrail": (
            "Matches are deterministic candidates only. A reviewer must confirm bank ownership, timing, FX, "
            "fees and ledger treatment before reconciliation or posting."
        ),
    }
