from __future__ import annotations

import re
from typing import Any, Iterable

from .accounting_import import validate_trial_balance_lines
from .ledger_import import validate_general_ledger_lines


def _money(value: Any) -> float:
    return round(float(value or 0), 2)


def discover_first_close_configuration(
    bank_reconciliation: dict[str, Any],
    general_ledger_lines: Iterable[dict[str, Any]],
    trial_balance_lines: Iterable[dict[str, Any]],
    *,
    entity_id: str,
    period: str,
) -> dict[str, Any]:
    """Inventory exact source scopes and emit fail-closed mapping starters without guessing."""
    if not re.fullmatch(r"20\d{2}-(0[1-9]|1[0-2])", str(period or "")):
        raise ValueError("period must use YYYY-MM")
    if bank_reconciliation.get("entity_id") != entity_id:
        raise ValueError("bank reconciliation is outside the requested legal entity")
    if bank_reconciliation.get("period") != period:
        raise ValueError("bank reconciliation does not match the requested period")

    ledger = list(general_ledger_lines)
    trial = list(trial_balance_lines)
    if any(
        row.get("entity_id") != entity_id or row.get("period") != period
        for row in ledger + trial
    ):
        raise ValueError("accounting exports cross the requested entity-period scope")

    ledger_validation = validate_general_ledger_lines(ledger, entity_id=entity_id)
    trial_validation = validate_trial_balance_lines(trial, entity_id=entity_id)
    issues: list[dict[str, Any]] = [
        *ledger_validation.get("issues", []), *trial_validation.get("issues", []),
    ]

    ledger_accounts: dict[tuple[str, str], dict[str, Any]] = {}
    for row in ledger:
        key = (str(row.get("currency") or ""), str(row.get("account_code") or ""))
        bucket = ledger_accounts.setdefault(key, {
            "account_names": set(), "period_debit": 0.0,
            "period_credit": 0.0, "line_count": 0,
        })
        bucket["account_names"].add(str(row.get("account_name") or ""))
        bucket["period_debit"] += _money(row.get("debit"))
        bucket["period_credit"] += _money(row.get("credit"))
        bucket["line_count"] += 1

    trial_accounts: dict[tuple[str, str], dict[str, Any]] = {}
    for row in trial:
        key = (str(row.get("currency") or ""), str(row.get("account_code") or ""))
        if key in trial_accounts:
            issues.append({
                "severity": "blocking", "type": "duplicate_trial_balance_account",
                "currency": key[0], "account_code": key[1],
            })
            continue
        trial_accounts[key] = row

    movement_reconciliation = []
    for currency, account_code in sorted(set(ledger_accounts) | set(trial_accounts)):
        ledger_account = ledger_accounts.get((currency, account_code), {})
        trial_account = trial_accounts.get((currency, account_code), {})
        ledger_debit = round(float(ledger_account.get("period_debit") or 0), 2)
        ledger_credit = round(float(ledger_account.get("period_credit") or 0), 2)
        trial_debit = _money(trial_account.get("period_debit"))
        trial_credit = _money(trial_account.get("period_credit"))
        debit_difference = round(ledger_debit - trial_debit, 2)
        credit_difference = round(ledger_credit - trial_credit, 2)
        matched = abs(debit_difference) < 0.01 and abs(credit_difference) < 0.01
        ledger_names = sorted(
            name for name in ledger_account.get("account_names", set()) if name
        )
        trial_name = str(trial_account.get("account_name") or "")
        names_consistent = bool(trial_name) and (
            not ledger_names or ledger_names == [trial_name]
        )
        if not matched:
            issues.append({
                "severity": "blocking", "type": "ledger_trial_balance_mismatch",
                "currency": currency, "account_code": account_code,
                "debit_difference": debit_difference,
                "credit_difference": credit_difference,
            })
        if not names_consistent:
            issues.append({
                "severity": "blocking", "type": "account_name_inconsistent",
                "currency": currency, "account_code": account_code,
                "general_ledger_account_names": ledger_names,
                "trial_balance_account_name": trial_name,
            })
        movement_reconciliation.append({
            "entity_id": entity_id, "period": period, "currency": currency,
            "account_code": account_code, "account_name": trial_name,
            "general_ledger_period_debit": ledger_debit,
            "trial_balance_period_debit": trial_debit,
            "debit_difference": debit_difference,
            "general_ledger_period_credit": ledger_credit,
            "trial_balance_period_credit": trial_credit,
            "credit_difference": credit_difference,
            "general_ledger_line_count": int(ledger_account.get("line_count") or 0),
            "matched": matched, "account_names_consistent": names_consistent,
        })

    active_trial = [
        row for row in trial
        if any(abs(_money(row.get(field))) >= 0.01 for field in (
            "opening_debit", "opening_credit", "period_debit", "period_credit",
            "closing_debit", "closing_credit",
        ))
    ]
    account_inventory = []
    account_mapping_starters = []
    gl_options_by_currency: dict[str, list[dict[str, str]]] = {}
    for row in sorted(
        active_trial,
        key=lambda item: (str(item.get("currency") or ""), str(item.get("account_code") or "")),
    ):
        currency = str(row.get("currency") or "")
        account_code = str(row.get("account_code") or "")
        account_name = str(row.get("account_name") or "")
        account_inventory.append({
            "entity_id": entity_id, "period": period, "currency": currency,
            "account_code": account_code, "account_name": account_name,
            "opening_net_debit": round(
                _money(row.get("opening_debit")) - _money(row.get("opening_credit")), 2,
            ),
            "period_debit": _money(row.get("period_debit")),
            "period_credit": _money(row.get("period_credit")),
            "closing_net_debit": round(
                _money(row.get("closing_debit")) - _money(row.get("closing_credit")), 2,
            ),
            "statement_mapping_status": "review_required",
            "cash_account_classification_inferred": False,
        })
        account_mapping_starters.append({
            "account_code": account_code,
            "source_account_name": account_name,
            "statement_group": "REPLACE_WITH_REVIEWED_STATEMENT_GROUP",
            "statement_line_id": "REPLACE_WITH_REVIEWED_STATEMENT_LINE_ID",
            "statement_line_name": "REPLACE_WITH_REVIEWED_STATEMENT_LINE_NAME",
        })
        gl_options_by_currency.setdefault(currency, []).append({
            "account_code": account_code, "account_name": account_name,
        })

    bank_inventory = []
    bank_mapping_starters = []
    for account in bank_reconciliation.get("accounts") or []:
        account_masked = str(account.get("account_masked") or "")
        currency = str(account.get("currency") or "")
        source_fingerprint = str(account.get("source_fingerprint") or "")
        statement_balance = account.get("statement_ending_balance")
        if statement_balance is None:
            issues.append({
                "severity": "blocking", "type": "missing_statement_ending_balance",
                "account_masked": account_masked, "currency": currency,
            })
        if not re.fullmatch(r"[a-f0-9]{64}", source_fingerprint):
            issues.append({
                "severity": "blocking", "type": "invalid_bank_source_fingerprint",
                "account_masked": account_masked, "currency": currency,
            })
        transaction_count = int(account.get("matched") or 0) + int(account.get("pending") or 0)
        bank_inventory.append({
            "entity_id": entity_id, "period": period,
            "account_masked": account_masked, "currency": currency,
            "statement_ending_balance": statement_balance,
            "transaction_count": transaction_count,
            "source_pending_transaction_count": int(account.get("pending") or 0),
            "bank_source_fingerprint": source_fingerprint,
            "gl_cash_account_mapping_status": "review_required",
            "cash_account_classification_inferred": False,
        })
        bank_mapping_starters.append({
            "entity_id": entity_id, "period": period,
            "account_masked": account_masked, "currency": currency,
            "gl_account_code": "REPLACE_WITH_REVIEWED_GL_CASH_ACCOUNT_CODE",
            "bank_source_fingerprint": source_fingerprint,
            "transaction_review": {
                "status": "pending",
                "reviewer_role": "REPLACE_WITH_REVIEWER_ROLE",
                "rationale": "REPLACE_WITH_AT_LEAST_8_CHAR_REVIEW_RATIONALE",
                "evidence": ["REPLACE_WITH_TRANSACTION_REVIEW_EVIDENCE"],
            },
            "reconciling_items": [],
        })

    if not bank_inventory:
        issues.append({"severity": "blocking", "type": "missing_bank_accounts"})
    if not active_trial:
        issues.append({"severity": "blocking", "type": "missing_active_trial_balance_accounts"})
    unique_issues = []
    seen: set[str] = set()
    for issue in issues:
        fingerprint = repr(sorted(issue.items()))
        if fingerprint not in seen:
            seen.add(fingerprint)
            unique_issues.append(issue)
    ready = bool(bank_inventory and active_trial and ledger) and not unique_issues
    return {
        "ready": ready,
        "entity_id": entity_id,
        "period": period,
        "configuration_status": "review_required" if ready else "source_correction_required",
        "bank_account_inventory": bank_inventory,
        "account_inventory": account_inventory,
        "general_ledger_validation": ledger_validation,
        "trial_balance_validation": trial_validation,
        "ledger_trial_balance_movement_reconciliation": movement_reconciliation,
        "account_mapping_starters": account_mapping_starters,
        "bank_gl_mapping_starters": bank_mapping_starters,
        "gl_account_options_by_currency": [
            {"currency": currency, "accounts": accounts, "suggestion_performed": False}
            for currency, accounts in sorted(gl_options_by_currency.items())
        ],
        "configuration_tasks": {
            "statement_account_mappings_to_review": len(account_mapping_starters),
            "bank_gl_mappings_to_review": len(bank_mapping_starters),
            "transaction_reviews_to_complete": len(bank_mapping_starters),
            "tax_or_accounting_policy_determined": False,
        },
        "issues": unique_issues,
        "candidate_only": True,
        "account_classification_inferred": False,
        "bank_gl_mapping_inferred": False,
        "transaction_matching_performed": False,
        "ledger_modified": False,
        "posting_performed": False,
        "period_close_performed": False,
        "external_filing_performed": False,
        "guardrail": (
            "The starters preserve exact source identities and intentionally contain fail-closed "
            "placeholders. Review every statement and bank-to-GL mapping before month-close control."
        ),
    }
