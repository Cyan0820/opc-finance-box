from __future__ import annotations

from collections import Counter
from typing import Any, Iterable


def review_expense_evidence(
    rows: Iterable[dict[str, Any]], *, entity_id: str,
    state_changes: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
    records = list(rows)
    changes = list(state_changes)
    if any(not isinstance(item, dict) for item in records):
        raise ValueError("expense evidence rows must be objects")
    if any(not isinstance(item, dict) for item in changes):
        raise ValueError("expense evidence state changes must be objects")
    if any(item.get("entity_id") != entity_id for item in records):
        raise ValueError("expense evidence must remain inside one legal entity")
    if any(item.get("entity_id") != entity_id for item in changes):
        raise ValueError("expense evidence state changes must remain inside one legal entity")
    if len({item.get("expense_evidence_id") for item in records}) != len(records):
        raise ValueError("expense evidence ids must be unique")
    if len({item.get("expense_evidence_id") for item in changes}) != len(changes):
        raise ValueError("expense evidence state change ids must be unique")
    currencies = Counter(str(item.get("billing_currency") or "") for item in records)
    blockers = []
    missing_receipts = [
        item["expense_evidence_id"] for item in records if item.get("receipt_count") == 0
    ]
    uncleared = [
        item["expense_evidence_id"] for item in records
        if item.get("card_transaction_status") != "CLEARED"
    ]
    missing_purpose = [
        item["expense_evidence_id"] for item in records
        if item.get("business_purpose_present") is not True
    ]
    unmapped = [
        item["expense_evidence_id"] for item in records
        if not item.get("accounting_field_types")
    ]
    if uncleared:
        blockers.append("one or more approved expenses are not backed by a cleared card transaction")
    if missing_purpose:
        blockers.append("one or more approved expenses lack a business-purpose description")
    if changes:
        blockers.append(
            "one or more previously observed expenses now have a non-approved state; "
            "a human must review downstream evidence before any accounting use"
        )
    candidates = [{
        "expense_evidence_id": item["expense_evidence_id"],
        "billing_currency": item["billing_currency"],
        "billing_amount_minor": item["billing_amount_minor"],
        "receipt_present": item.get("receipt_count", 0) > 0,
        "business_purpose_present": item.get("business_purpose_present") is True,
        "cleared": item.get("card_transaction_status") == "CLEARED",
        "accounting_mapping_present": bool(item.get("accounting_field_types")),
        "candidate_only": True,
    } for item in records]
    return {
        "ready_for_review": not blockers,
        "entity_id": entity_id,
        "record_count": len(records),
        "state_change_count": len(changes),
        "state_change_candidates": [{
            "expense_evidence_id": item["expense_evidence_id"],
            "current_status": item["current_status"],
            "updated_at": item["updated_at"],
            "invalidates_approved_evidence": item.get("invalidates_approved_evidence") is True,
            "candidate_only": True,
        } for item in changes],
        "currency_record_counts": [
            {"currency": currency, "record_count": count}
            for currency, count in sorted(currencies.items())
        ],
        "receipt_missing_count": len(missing_receipts),
        "business_purpose_missing_count": len(missing_purpose),
        "uncleared_count": len(uncleared),
        "accounting_mapping_missing_count": len(unmapped),
        "candidates": candidates,
        "blockers": blockers,
        "human_review_required": True,
        "expense_claims_created": False,
        "accounting_mapping_inferred": False,
        "posting_performed": False,
        "payment_performed": False,
        "external_actions_performed": False,
    }
