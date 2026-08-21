from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Iterable


MONEY = Decimal("0.01")


def _money(value: Any, field: str, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool) or value in (None, ""):
        raise ValueError(f"{field} must be a finite decimal")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field} must be a finite decimal") from exc
    if not number.is_finite() or (positive and number <= 0):
        qualifier = "positive " if positive else ""
        raise ValueError(f"{field} must be a finite {qualifier}decimal")
    return number.quantize(MONEY, rounding=ROUND_HALF_UP)


def _text(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field} must be a non-empty string")
    return text


def _evidence(value: Any, field: str) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a non-empty list")
    evidence = [str(item).strip() for item in value if str(item).strip()]
    if not evidence:
        raise ValueError(f"{field} must be a non-empty list")
    return evidence


def _signed_items(
    raw_items: Any,
    *,
    mapping_index: int,
) -> tuple[dict[str, Decimal], list[dict[str, Any]], list[dict[str, Any]]]:
    if raw_items is None:
        raw_items = []
    if not isinstance(raw_items, list):
        raise ValueError(f"bank_gl_mappings[{mapping_index}].reconciling_items must be a list")
    totals = {"bank": Decimal("0"), "ledger": Decimal("0")}
    normalized: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item_index, raw in enumerate(raw_items):
        field = f"bank_gl_mappings[{mapping_index}].reconciling_items[{item_index}]"
        if not isinstance(raw, dict):
            raise ValueError(f"{field} must be an object")
        item_id = _text(raw.get("item_id"), f"{field}.item_id")
        if item_id in seen:
            raise ValueError(f"duplicate reconciling item id: {item_id}")
        seen.add(item_id)
        side = str(raw.get("side") or "")
        direction = str(raw.get("direction") or "")
        status = str(raw.get("review_status") or "")
        if side not in {"bank", "ledger"}:
            raise ValueError(f"{field}.side must be bank or ledger")
        if direction not in {"increase", "decrease"}:
            raise ValueError(f"{field}.direction must be increase or decrease")
        if status not in {"approved", "pending", "rejected"}:
            raise ValueError(f"{field}.review_status must be approved, pending or rejected")
        amount = _money(raw.get("amount"), f"{field}.amount", positive=True)
        item = {
            "item_id": item_id,
            "side": side,
            "direction": direction,
            "amount": float(amount),
            "reason": _text(raw.get("reason"), f"{field}.reason"),
            "evidence": _evidence(raw.get("evidence"), f"{field}.evidence"),
            "review_status": status,
        }
        normalized.append(item)
        if status == "approved":
            totals[side] += amount if direction == "increase" else -amount
        elif status == "pending":
            pending.append(item)
    return totals, normalized, pending


def build_month_close_control(
    bank_reconciliation: dict[str, Any],
    accounting_close: dict[str, Any],
    trial_balance_lines: Iterable[dict[str, Any]],
    bank_gl_mappings: Iterable[dict[str, Any]],
    *,
    entity_id: str,
    period: str,
) -> dict[str, Any]:
    """Bind statement balances to reviewed GL cash accounts without posting or matching cash."""
    if not re.fullmatch(r"20\d{2}-(0[1-9]|1[0-2])", str(period or "")):
        raise ValueError("period must use YYYY-MM")
    if bank_reconciliation.get("entity_id") != entity_id:
        raise ValueError("bank reconciliation is outside the requested legal entity")
    if accounting_close.get("entity_id") != entity_id:
        raise ValueError("accounting close is outside the requested legal entity")
    if bank_reconciliation.get("period") != period or accounting_close.get("period") != period:
        raise ValueError("bank and accounting evidence must use the requested period")

    trial = list(trial_balance_lines)
    if any(
        row.get("entity_id") != entity_id or row.get("period") != period
        for row in trial
    ):
        raise ValueError("trial balance lines cross the requested entity-period scope")
    trial_by_key = {
        (str(row.get("currency") or ""), str(row.get("account_code") or "")): row
        for row in trial
    }
    if len(trial_by_key) != len(trial):
        raise ValueError("trial balance contains duplicate currency and account-code scopes")

    mappings = list(bank_gl_mappings)
    mapping_by_key: dict[tuple[str, str], tuple[int, dict[str, Any]]] = {}
    issues: list[dict[str, Any]] = []
    for index, raw in enumerate(mappings):
        if not isinstance(raw, dict):
            raise ValueError(f"bank_gl_mappings[{index}] must be an object")
        mapping_entity = _text(raw.get("entity_id"), f"bank_gl_mappings[{index}].entity_id")
        mapping_period = _text(raw.get("period"), f"bank_gl_mappings[{index}].period")
        if mapping_entity != entity_id or mapping_period != period:
            raise ValueError(f"bank_gl_mappings[{index}] crosses the requested entity-period scope")
        account_masked = _text(
            raw.get("account_masked"), f"bank_gl_mappings[{index}].account_masked"
        )
        currency = _text(raw.get("currency"), f"bank_gl_mappings[{index}].currency").upper()
        if not re.fullmatch(r"[A-Z]{3}", currency):
            raise ValueError(f"bank_gl_mappings[{index}].currency must be a three-letter code")
        key = (account_masked, currency)
        if key in mapping_by_key:
            raise ValueError(f"duplicate bank-to-GL mapping for {account_masked} {currency}")
        mapping_by_key[key] = (index, raw)

    account_controls = []
    used_gl_keys: set[tuple[str, str]] = set()
    bank_accounts = bank_reconciliation.get("accounts") or []
    for bank_account in bank_accounts:
        account_masked = str(bank_account.get("account_masked") or "")
        currency = str(bank_account.get("currency") or "")
        key = (account_masked, currency)
        mapping_entry = mapping_by_key.get(key)
        if mapping_entry is None:
            issues.append({
                "severity": "blocking", "type": "missing_bank_gl_mapping",
                "account_masked": account_masked, "currency": currency,
            })
            continue
        index, mapping = mapping_entry
        gl_account_code = _text(
            mapping.get("gl_account_code"), f"bank_gl_mappings[{index}].gl_account_code"
        )
        gl_key = (currency, gl_account_code)
        if gl_key in used_gl_keys:
            issues.append({
                "severity": "blocking", "type": "gl_cash_account_mapped_more_than_once",
                "currency": currency, "gl_account_code": gl_account_code,
            })
        used_gl_keys.add(gl_key)
        trial_line = trial_by_key.get(gl_key)
        if trial_line is None:
            issues.append({
                "severity": "blocking", "type": "mapped_gl_account_not_in_trial_balance",
                "account_masked": account_masked, "currency": currency,
                "gl_account_code": gl_account_code,
            })
            continue

        expected_fingerprint = _text(
            mapping.get("bank_source_fingerprint"),
            f"bank_gl_mappings[{index}].bank_source_fingerprint",
        )
        source_current = expected_fingerprint == bank_account.get("source_fingerprint")
        review = mapping.get("transaction_review")
        if not isinstance(review, dict):
            raise ValueError(f"bank_gl_mappings[{index}].transaction_review must be an object")
        review_status = str(review.get("status") or "")
        if review_status not in {"complete", "pending", "rejected"}:
            raise ValueError(
                f"bank_gl_mappings[{index}].transaction_review.status must be complete, pending or rejected"
            )
        reviewer_role = _text(
            review.get("reviewer_role"),
            f"bank_gl_mappings[{index}].transaction_review.reviewer_role",
        )
        rationale = _text(
            review.get("rationale"),
            f"bank_gl_mappings[{index}].transaction_review.rationale",
        )
        if len(rationale) < 8:
            raise ValueError(
                f"bank_gl_mappings[{index}].transaction_review.rationale must be at least 8 characters"
            )
        review_evidence = _evidence(
            review.get("evidence"),
            f"bank_gl_mappings[{index}].transaction_review.evidence",
        )
        totals, reconciling_items, pending_items = _signed_items(
            mapping.get("reconciling_items"), mapping_index=index,
        )
        statement_balance_value = bank_account.get("statement_ending_balance")
        statement_balance = (
            _money(statement_balance_value, "statement_ending_balance")
            if statement_balance_value is not None else None
        )
        ledger_balance = (
            _money(trial_line.get("closing_debit", 0), "closing_debit")
            - _money(trial_line.get("closing_credit", 0), "closing_credit")
        ).quantize(MONEY)
        adjusted_bank = (
            (statement_balance + totals["bank"]).quantize(MONEY)
            if statement_balance is not None else None
        )
        adjusted_ledger = (ledger_balance + totals["ledger"]).quantize(MONEY)
        difference = (
            (adjusted_bank - adjusted_ledger).quantize(MONEY)
            if adjusted_bank is not None else None
        )
        control_ready = bool(
            source_current
            and review_status == "complete"
            and not pending_items
            and statement_balance is not None
            and difference is not None
            and abs(difference) < MONEY
        )
        if not source_current:
            issues.append({
                "severity": "blocking", "type": "stale_bank_source_review",
                "account_masked": account_masked, "currency": currency,
            })
        if statement_balance is None:
            issues.append({
                "severity": "blocking", "type": "missing_statement_ending_balance",
                "account_masked": account_masked, "currency": currency,
            })
        if review_status != "complete":
            issues.append({
                "severity": "blocking", "type": "bank_transaction_review_incomplete",
                "account_masked": account_masked, "currency": currency,
                "review_status": review_status,
            })
        if pending_items:
            issues.append({
                "severity": "blocking", "type": "pending_reconciling_items",
                "account_masked": account_masked, "currency": currency,
                "item_ids": [item["item_id"] for item in pending_items],
            })
        if difference is not None and abs(difference) >= MONEY:
            issues.append({
                "severity": "blocking", "type": "bank_gl_balance_difference",
                "account_masked": account_masked, "currency": currency,
                "gl_account_code": gl_account_code, "difference": float(difference),
            })
        account_controls.append({
            "entity_id": entity_id, "period": period,
            "account_masked": account_masked, "currency": currency,
            "gl_account_code": gl_account_code,
            "gl_account_name": str(trial_line.get("account_name") or ""),
            "bank_source_fingerprint": bank_account.get("source_fingerprint"),
            "source_review_current": source_current,
            "statement_ending_balance": float(statement_balance) if statement_balance is not None else None,
            "ledger_ending_balance": float(ledger_balance),
            "approved_bank_adjustments": float(totals["bank"]),
            "approved_ledger_adjustments": float(totals["ledger"]),
            "adjusted_bank_balance": float(adjusted_bank) if adjusted_bank is not None else None,
            "adjusted_ledger_balance": float(adjusted_ledger),
            "difference": float(difference) if difference is not None else None,
            "transaction_review": {
                "status": review_status, "reviewer_role": reviewer_role,
                "rationale": rationale, "evidence": review_evidence,
                "source_transaction_count": bank_account.get("matched", 0) + bank_account.get("pending", 0),
            },
            "reconciling_items": reconciling_items,
            "ready_for_month_close_review": control_ready,
        })

    bank_keys = {
        (str(item.get("account_masked") or ""), str(item.get("currency") or ""))
        for item in bank_accounts
    }
    for account_masked, currency in sorted(set(mapping_by_key) - bank_keys):
        issues.append({
            "severity": "blocking", "type": "mapping_account_not_in_bank_statement",
            "account_masked": account_masked, "currency": currency,
        })
    if not bank_accounts:
        issues.append({"severity": "blocking", "type": "missing_bank_accounts"})
    if not accounting_close.get("ready"):
        issues.append({"severity": "blocking", "type": "accounting_close_not_reconciled"})

    ready = bool(account_controls) and not issues
    statements = accounting_close.get("financial_statement_candidates") or []
    currency_briefing = []
    for statement in statements:
        currency = str(statement.get("currency") or "")
        currency_controls = [item for item in account_controls if item["currency"] == currency]
        currency_briefing.append({
            "currency": currency,
            "bank_account_count": len(currency_controls),
            "statement_cash_total": round(sum(
                item["adjusted_bank_balance"] or 0 for item in currency_controls
            ), 2),
            "ledger_cash_total": round(sum(
                item["adjusted_ledger_balance"] for item in currency_controls
            ), 2),
            "assets": statement.get("balance_sheet", {}).get("assets"),
            "liabilities": statement.get("balance_sheet", {}).get("liabilities"),
            "revenue": statement.get("income_statement", {}).get("revenue"),
            "expenses": statement.get("income_statement", {}).get("expenses"),
            "profit_before_tax_candidate": statement.get(
                "income_statement", {}
            ).get("profit_before_tax_candidate"),
        })
    return {
        "ready": ready,
        "entity_id": entity_id,
        "period": period,
        "account_controls": account_controls,
        "currency_briefing": currency_briefing,
        "mapping_coverage": {
            "bank_account_count": len(bank_accounts),
            "mapped_bank_account_count": len(account_controls),
            "all_bank_accounts_explicitly_mapped": bool(bank_accounts) and len(account_controls) == len(bank_accounts),
        },
        "issues": issues,
        "candidate_only": True,
        "transaction_matching_performed": False,
        "cash_allocation_performed": False,
        "ledger_modified": False,
        "opening_balances_modified": False,
        "posting_performed": False,
        "period_close_performed": False,
        "external_filing_performed": False,
        "guardrail": (
            "Equal adjusted balances prove only this explicit bank-to-GL control. They do not "
            "match individual transactions, authorize adjustments, post journals or close the period."
        ),
    }
