from __future__ import annotations

from collections import Counter
from datetime import date
from typing import Any

from .pack_services import ServiceContext
from .tax_calendar import build_tax_calendar
from .tax_pack_lifecycle import source_freshness_from_bundle


UK_EVIDENCE_BY_RULE = {
    "uk.company.private_limited.scope.evidence": [
        "companies_house_entity_reference", "legal_name_match_review",
        "company_type_review", "accounting_reference_date_evidence",
    ],
    "uk.corporation_tax.registration.evidence": [
        "active_trading_date_evidence", "corporation_tax_service_registration_reference",
        "accounting_period_confirmation", "utr_reference_location_review",
    ],
    "uk.corporation_tax.ct600.calendar": [
        "notice_to_deliver_reference", "accounting_period_end_confirmation",
        "approved_ct600_due_date", "authorized_filer_confirmation",
    ],
    "uk.corporation_tax.payment.calendar": [
        "profit_band_review", "instalment_payment_applicability_review",
        "approved_payment_due_date", "payment_reference_evidence",
    ],
    "uk.vat.registration.monitor": [
        "rolling_taxable_turnover", "next_30_day_turnover_forecast",
        "taxable_supply_classification", "establishment_status_review",
        "vat_registration_decision",
    ],
    "uk.vat.return.calendar": [
        "vat_registration_evidence", "vat_accounting_period_evidence",
        "online_account_due_date", "payment_clearance_plan",
    ],
    "uk.companies_house.private_accounts.calendar": [
        "accounting_reference_date_evidence", "first_accounts_review",
        "approved_accounts_due_date", "director_approval_plan",
    ],
    "uk.ct600.return_evidence": [
        "approved_financial_statements", "tax_computation_workpaper",
        "ct600_supplementary_pages_review", "ixbrl_software_and_tagging_review",
        "authorized_declaration", "submission_receipt_plan",
    ],
}
ENTITY_TYPES = {
    "private_limited_company", "public_limited_company", "sole_trader",
    "partnership", "other", "unknown",
}
REGISTRATION_STATUSES = {"confirmed", "pending", "not_registered", "unknown"}
IN_SCOPE_REGISTRATIONS = {
    "uk_limited_company", "private_limited_company", "corporation_tax", "ct600_filer",
}


def _uk_context(context: ServiceContext) -> tuple[Any, dict[str, Any]]:
    if not context.entity_id:
        raise ValueError("UK tax service requires entity_id")
    entity = context.runtime.entities.get(context.entity_id)
    if entity.jurisdiction != "GB" or entity.tax_pack != "jurisdiction.uk_limited_company":
        raise ValueError(
            f"Entity {entity.entity_id} does not use jurisdiction.uk_limited_company"
        )
    return entity, context.runtime.tax_rules(entity.entity_id)


def _iso_date(value: Any, field: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError(f"{field} must use YYYY-MM-DD") from exc


def _freshness(bundle: dict[str, Any], as_of_value: Any) -> dict[str, Any]:
    return source_freshness_from_bundle(bundle, as_of_value)


def _evidence_reference(value: Any, field: str) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an evidence reference object")
    source_reference = str(value.get("source_reference") or "").strip()
    captured_at = str(value.get("captured_at") or "").strip()
    if not source_reference or not captured_at:
        raise ValueError(f"{field} requires source_reference and captured_at")
    _iso_date(captured_at[:10], f"{field}.captured_at")
    return {"source_reference": source_reference, "captured_at": captured_at}


def build_uk_registration_profile(
    payload: dict[str, Any], context: ServiceContext,
) -> dict[str, Any]:
    entity, bundle = _uk_context(context)
    if any(field in payload for field in (
        "utr", "utr_number", "company_number", "company_registration_number",
        "vat_number", "tax_id",
    )):
        raise ValueError(
            "Do not pass a raw UTR, company number, VAT number or tax identifier; "
            "provide an evidence reference"
        )
    registrations = {item.strip().lower() for item in entity.tax_registrations}
    entity_type = str(payload.get("entity_type") or (
        "private_limited_company"
        if registrations & {"uk_limited_company", "private_limited_company"}
        else "unknown"
    )).strip().lower()
    if entity_type not in ENTITY_TYPES:
        raise ValueError("entity_type is not supported by this UK limited-company Pack")
    entity_evidence = _evidence_reference(
        payload.get("entity_type_evidence"), "entity_type_evidence",
    )
    if entity_type == "private_limited_company":
        applicability = "in_scope_private_limited_company"
        entity_status = "confirmed" if entity_evidence else "needs_evidence"
    elif entity_type == "unknown":
        applicability = "unknown"
        entity_status = "needs_confirmation"
    else:
        applicability = "outside_pack_scope"
        entity_status = "outside_pack_scope"

    corporation_tax_status = str(payload.get("corporation_tax_status") or (
        "confirmed" if registrations & {"corporation_tax", "ct600_filer"} else "unknown"
    )).strip().lower()
    if corporation_tax_status not in REGISTRATION_STATUSES:
        raise ValueError(
            "corporation_tax_status must be confirmed, pending, not_registered or unknown"
        )
    corporation_tax_evidence = _evidence_reference(
        payload.get("corporation_tax_evidence"), "corporation_tax_evidence",
    )
    corporation_tax_review = (
        "confirmed" if corporation_tax_status == "confirmed" and corporation_tax_evidence
        else "needs_evidence" if corporation_tax_status == "confirmed"
        else "needs_confirmation"
    )
    vat_status = str(payload.get("vat_status") or (
        "confirmed" if registrations & {"vat", "vat_registered", "uk_vat"}
        else "not_registered"
    )).strip().lower()
    if vat_status not in REGISTRATION_STATUSES:
        raise ValueError("vat_status must be confirmed, pending, not_registered or unknown")
    vat_evidence = _evidence_reference(payload.get("vat_evidence"), "vat_evidence")
    vat_review = (
        "confirmed" if vat_status == "confirmed" and vat_evidence
        else "needs_evidence" if vat_status == "confirmed"
        else "not_registered" if vat_status == "not_registered"
        else "needs_confirmation"
    )
    freshness = _freshness(bundle, payload.get("as_of"))
    blockers = []
    if entity_status != "confirmed":
        blockers.append(f"entity type: {entity_status}")
    if corporation_tax_review != "confirmed":
        blockers.append(f"Corporation Tax evidence: {corporation_tax_review}")
    if vat_review in {"needs_evidence", "needs_confirmation"}:
        blockers.append(f"VAT registration evidence: {vat_review}")
    if not freshness["current"]:
        blockers.append("official source review has expired")
    source_index = {source["id"]: source for source in bundle["rules"]["sources"]}
    return {
        "ready": not blockers,
        "entity_id": entity.entity_id,
        "jurisdiction": entity.jurisdiction,
        "tax_pack": bundle["pack_id"],
        "tax_pack_version": bundle["pack_version"],
        "tax_readiness": entity.tax_readiness,
        "scope": "UK private limited company registration evidence only",
        "applicability": applicability,
        "entity_type": {"value": entity_type, "status": entity_status, "evidence": entity_evidence},
        "corporation_tax_registration": {
            "declared_status": corporation_tax_status,
            "review_status": corporation_tax_review,
            "evidence": corporation_tax_evidence,
            "raw_utr_collected": False,
        },
        "vat_registration": {
            "declared_status": vat_status,
            "review_status": vat_review,
            "evidence": vat_evidence,
            "raw_vat_number_collected": False,
        },
        "company_identifier_collected": False,
        "source_freshness": freshness,
        "official_sources": [
            source_index["uk_hmrc_corporation_tax_active_registration"],
            source_index["uk_hmrc_company_tax_return_obligations_2026"],
            source_index["uk_hmrc_vat_registration_current"],
            source_index["uk_companies_house_accounts_2026"],
        ],
        "blockers": blockers,
        "review_gate": "tax_registration_confirmation",
        "human_review_required": True,
        "vat_liability_determined": False,
        "corporation_tax_calculated": False,
        "filing_performed": False,
        "payment_performed": False,
        "external_submission_enabled": False,
    }


def _provided_evidence(
    payload: dict[str, Any],
) -> tuple[dict[str, dict[str, str]], list[str]]:
    raw = payload.get("provided_evidence") or []
    if not isinstance(raw, list) or any(not isinstance(item, dict) for item in raw):
        raise ValueError("provided_evidence must be a list of evidence reference objects")
    ids = [str(item.get("evidence_id") or "").strip() for item in raw]
    if any(not evidence_id for evidence_id in ids):
        raise ValueError("every provided evidence item requires evidence_id")
    duplicates = sorted(evidence_id for evidence_id, count in Counter(ids).items() if count > 1)
    provided: dict[str, dict[str, str]] = {}
    for index, item in enumerate(raw, 1):
        evidence_id = str(item["evidence_id"]).strip()
        reference = _evidence_reference(item, f"provided_evidence[{index}]")
        if evidence_id not in provided and reference is not None:
            provided[evidence_id] = reference
    return provided, duplicates


def build_uk_evidence_checklist(
    payload: dict[str, Any], context: ServiceContext,
) -> dict[str, Any]:
    entity, bundle = _uk_context(context)
    provided, duplicates = _provided_evidence(payload)
    required_ids = {
        evidence_id for values in UK_EVIDENCE_BY_RULE.values() for evidence_id in values
    }
    unknown = sorted(set(provided) - required_ids)
    source_index = {source["id"]: source for source in bundle["rules"]["sources"]}
    items = []
    for rule in bundle["rules"]["rules"]:
        required = UK_EVIDENCE_BY_RULE[rule["id"]]
        missing = [item for item in required if item not in provided]
        items.append({
            "rule_id": rule["id"], "summary": rule["summary"],
            "automation_level": rule["automation_level"],
            "required_evidence": required,
            "provided_evidence": {
                item: provided[item] for item in required if item in provided
            },
            "missing_evidence": missing, "complete": not missing,
            "official_sources": [source_index[item] for item in rule["source_ids"]],
            "human_review_required": True,
        })
    freshness = _freshness(bundle, payload.get("as_of"))
    blockers = []
    if duplicates:
        blockers.append("duplicate evidence ids")
    if unknown:
        blockers.append("unknown evidence ids")
    if any(not item["complete"] for item in items):
        blockers.append("required evidence is missing")
    if not freshness["current"]:
        blockers.append("official source review has expired")
    return {
        "ready": not blockers, "entity_id": entity.entity_id,
        "tax_pack": bundle["pack_id"], "tax_pack_version": bundle["pack_version"],
        "rules_verified_at": bundle["rules"]["verified_at"],
        "source_freshness": freshness, "items": items,
        "duplicate_evidence_ids": duplicates, "unknown_evidence_ids": unknown,
        "blockers": blockers, "review_gate": "tax_advisor_review",
        "human_review_required": True, "vat_liability_determined": False,
        "corporation_tax_calculated": False, "filing_performed": False,
        "payment_performed": False, "external_submission_enabled": False,
        "scope_note": bundle["rules"].get("scope_note"),
    }


def build_uk_tax_calendar(payload: dict[str, Any], context: ServiceContext) -> dict[str, Any]:
    entity, bundle = _uk_context(context)
    result = build_tax_calendar(
        context.runtime, entity.entity_id,
        period_year=payload.get("period_year"), anchors=payload.get("anchors"),
        as_of=payload.get("as_of"),
    )
    freshness = _freshness(bundle, payload.get("as_of"))
    registrations = {item.strip().lower() for item in entity.tax_registrations}
    limited_company_scope_confirmed = bool(registrations & IN_SCOPE_REGISTRATIONS)
    blockers = list(result.get("warnings") or [])
    if not limited_company_scope_confirmed:
        blockers.append("UK private limited company / Corporation Tax scope is not confirmed")
    if not freshness["current"]:
        blockers.append("official source review has expired")
    return {
        **result,
        "ready": bool(
            result["ready"] and freshness["current"] and limited_company_scope_confirmed
        ),
        "limited_company_scope_confirmed": limited_company_scope_confirmed,
        "source_freshness": freshness, "blockers": sorted(set(blockers)),
        "vat_liability_determined": False, "corporation_tax_calculated": False,
        "filing_performed": False, "payment_performed": False,
        "external_submission_enabled": False,
    }
