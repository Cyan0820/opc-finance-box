from __future__ import annotations

from collections import Counter
from datetime import date
from typing import Any

from .pack_services import ServiceContext
from .tax_calendar import build_tax_calendar
from .tax_pack_lifecycle import source_freshness_from_bundle


NZ_EVIDENCE_BY_RULE = {
    "nz.company.limited.scope.evidence": [
        "companies_register_status_reference", "limited_company_type_review",
        "legal_name_match_review", "new_zealand_tax_residency_review",
    ],
    "nz.tax.ird_ir4.registration.evidence": [
        "company_ird_registration_reference", "ir4_filing_scope_review",
        "legal_name_registration_match", "myir_access_owner_review",
    ],
    "nz.gst.registration.monitor": [
        "past_12_month_taxable_turnover_workpaper",
        "next_12_month_taxable_turnover_forecast",
        "taxable_activity_and_supply_classification_review",
        "gst_inclusive_pricing_review", "gst_registration_decision",
    ],
    "nz.ir4.return.calendar": [
        "confirmed_balance_date", "tax_agent_status_evidence",
        "extension_of_time_status", "myir_return_due_date_reference",
        "approved_ir4_due_date",
    ],
    "nz.income_tax.payment.calendar": [
        "residual_income_tax_workpaper", "provisional_tax_method_review",
        "installment_schedule_reference", "approved_income_tax_payment_dates",
    ],
    "nz.gst.return_payment.calendar": [
        "gst_registration_evidence", "filing_frequency_evidence",
        "accounting_basis_evidence", "taxable_period_reference",
        "approved_gst_return_and_payment_due_date",
    ],
    "nz.companies_office.annual_return.calendar": [
        "active_company_status_reference", "incorporation_year_evidence",
        "assigned_filing_month_reference", "company_details_review",
        "approved_annual_return_due_date",
    ],
    "nz.ir4.return.evidence": [
        "approved_financial_statements", "ir4_ir10_tax_adjustment_workpaper",
        "imputation_and_provisional_tax_review", "authorized_declaration",
        "payment_plan", "submission_receipt_plan",
    ],
}
ENTITY_TYPES = {
    "limited_company", "look_through_company", "overseas_company",
    "sole_trader", "partnership", "trust", "other", "unknown",
}
REGISTRATION_STATUSES = {"confirmed", "pending", "not_registered", "unknown"}
IN_SCOPE_REGISTRATIONS = {
    "nz_limited_company", "companies_register_company", "ir4_filer",
    "company_income_tax",
}


def _nz_context(context: ServiceContext) -> tuple[Any, dict[str, Any]]:
    if not context.entity_id:
        raise ValueError("New Zealand tax service requires entity_id")
    entity = context.runtime.entities.get(context.entity_id)
    if entity.jurisdiction != "NZ" or entity.tax_pack != "jurisdiction.nz_limited_company":
        raise ValueError(
            f"Entity {entity.entity_id} does not use jurisdiction.nz_limited_company"
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


def _registration_review(
    declared_status: str, evidence: dict[str, str] | None,
) -> str:
    if declared_status == "confirmed" and evidence:
        return "confirmed"
    if declared_status == "confirmed":
        return "needs_evidence"
    if declared_status == "not_registered":
        return "not_registered"
    return "needs_confirmation"


def build_nz_registration_profile(
    payload: dict[str, Any], context: ServiceContext,
) -> dict[str, Any]:
    entity, bundle = _nz_context(context)
    forbidden = (
        "company_number", "nzbn", "nzbn_number", "ird", "ird_number",
        "tax_id", "tax_identifier", "gst_number", "gst_registration_number",
    )
    if any(field in payload for field in forbidden):
        raise ValueError(
            "Do not pass a raw company number, NZBN, IRD or GST number; "
            "provide an evidence reference"
        )
    registrations = {item.strip().lower() for item in entity.tax_registrations}
    entity_type = str(payload.get("entity_type") or (
        "limited_company"
        if registrations & {"nz_limited_company", "companies_register_company"}
        else "unknown"
    )).strip().lower()
    if entity_type not in ENTITY_TYPES:
        raise ValueError(
            "entity_type is not supported by this New Zealand limited-company Pack"
        )
    entity_evidence = _evidence_reference(
        payload.get("entity_type_evidence"), "entity_type_evidence",
    )
    residency_evidence = _evidence_reference(
        payload.get("tax_residency_evidence"), "tax_residency_evidence",
    )
    if entity_type == "limited_company":
        applicability = "in_scope_limited_company"
        entity_status = "confirmed" if entity_evidence else "needs_evidence"
    elif entity_type == "unknown":
        applicability = "unknown"
        entity_status = "needs_confirmation"
    else:
        applicability = "outside_pack_scope"
        entity_status = "outside_pack_scope"
    residency_status = "confirmed" if residency_evidence else "needs_evidence"

    statuses: dict[str, dict[str, Any]] = {}
    registration_fields = {
        "company_ird": (
            "company_ird_status", "company_ird_evidence",
            {"company_ird_confirmed", "ird_confirmed"},
        ),
        "company_income_tax": (
            "company_income_tax_status", "company_income_tax_evidence",
            {"company_income_tax", "ir4_filer"},
        ),
        "gst": (
            "gst_status", "gst_evidence", {"gst", "gst_registered", "nz_gst"},
        ),
        "employer": (
            "employer_status", "employer_evidence",
            {"employer", "employer_registered", "nz_employer"},
        ),
    }
    for label, (status_field, evidence_field, configured_codes) in registration_fields.items():
        default_status = "confirmed" if registrations & configured_codes else "not_registered"
        declared = str(payload.get(status_field) or default_status).strip().lower()
        if declared not in REGISTRATION_STATUSES:
            raise ValueError(
                f"{status_field} must be confirmed, pending, not_registered or unknown"
            )
        evidence = _evidence_reference(payload.get(evidence_field), evidence_field)
        statuses[label] = {
            "declared_status": declared,
            "review_status": _registration_review(declared, evidence),
            "evidence": evidence,
            "raw_identifier_collected": False,
        }

    freshness = _freshness(bundle, payload.get("as_of"))
    blockers = []
    if entity_status != "confirmed":
        blockers.append(f"entity type: {entity_status}")
    if residency_status != "confirmed":
        blockers.append(f"New Zealand tax residency: {residency_status}")
    for label in ("company_ird", "company_income_tax"):
        if statuses[label]["review_status"] != "confirmed":
            blockers.append(f"{label} evidence: {statuses[label]['review_status']}")
    for label in ("gst", "employer"):
        if statuses[label]["review_status"] in {"needs_evidence", "needs_confirmation"}:
            blockers.append(f"{label} evidence: {statuses[label]['review_status']}")
    if not freshness["current"]:
        blockers.append("official source review has expired")
    source_index = {source["id"]: source for source in bundle["rules"]["sources"]}
    return {
        "ready": not blockers, "entity_id": entity.entity_id,
        "jurisdiction": entity.jurisdiction, "tax_pack": bundle["pack_id"],
        "tax_pack_version": bundle["pack_version"],
        "tax_readiness": entity.tax_readiness,
        "scope": "New Zealand limited company registration evidence only",
        "applicability": applicability,
        "entity_type": {
            "value": entity_type, "status": entity_status, "evidence": entity_evidence,
        },
        "new_zealand_tax_residency": {
            "status": residency_status, "evidence": residency_evidence,
            "determined_by_system": False,
        },
        "registrations": statuses,
        "raw_company_identifier_collected": False,
        "source_freshness": freshness,
        "official_sources": [
            source_index["nz_companies_incorporation_current"],
            source_index["nz_ird_business_number_2026"],
            source_index["nz_ird_company_residency_current"],
            source_index["nz_ird_gst_registration_current"],
        ],
        "blockers": blockers, "review_gate": "tax_registration_confirmation",
        "human_review_required": True,
        "new_zealand_tax_residency_determined": False,
        "look_through_company_status_determined": False,
        "gst_registration_liability_determined": False,
        "gst_supply_classification_performed": False,
        "corporation_tax_rate_determined": False,
        "provisional_tax_determined": False,
        "tax_calculation_performed": False, "filing_performed": False,
        "payment_performed": False, "external_submission_enabled": False,
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
    duplicates = sorted(
        evidence_id for evidence_id, count in Counter(ids).items() if count > 1
    )
    provided: dict[str, dict[str, str]] = {}
    for index, item in enumerate(raw, 1):
        evidence_id = str(item["evidence_id"]).strip()
        reference = _evidence_reference(item, f"provided_evidence[{index}]")
        if evidence_id not in provided and reference is not None:
            provided[evidence_id] = reference
    return provided, duplicates


def build_nz_evidence_checklist(
    payload: dict[str, Any], context: ServiceContext,
) -> dict[str, Any]:
    entity, bundle = _nz_context(context)
    provided, duplicates = _provided_evidence(payload)
    required_ids = {
        evidence_id for values in NZ_EVIDENCE_BY_RULE.values()
        for evidence_id in values
    }
    unknown = sorted(set(provided) - required_ids)
    source_index = {source["id"]: source for source in bundle["rules"]["sources"]}
    items = []
    for rule in bundle["rules"]["rules"]:
        required = NZ_EVIDENCE_BY_RULE[rule["id"]]
        missing = [evidence_id for evidence_id in required if evidence_id not in provided]
        items.append({
            "rule_id": rule["id"], "summary": rule["summary"],
            "automation_level": rule["automation_level"],
            "required_evidence": required,
            "provided_evidence": {
                evidence_id: provided[evidence_id]
                for evidence_id in required if evidence_id in provided
            },
            "missing_evidence": missing, "complete": not missing,
            "official_sources": [source_index[source_id] for source_id in rule["source_ids"]],
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
        "human_review_required": True,
        "new_zealand_tax_residency_determined": False,
        "look_through_company_status_determined": False,
        "gst_registration_liability_determined": False,
        "gst_supply_classification_performed": False,
        "corporation_tax_rate_determined": False,
        "provisional_tax_determined": False,
        "tax_calculation_performed": False, "filing_performed": False,
        "payment_performed": False, "external_submission_enabled": False,
        "scope_note": bundle["rules"].get("scope_note"),
    }


def build_nz_tax_calendar(
    payload: dict[str, Any], context: ServiceContext,
) -> dict[str, Any]:
    entity, bundle = _nz_context(context)
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
        blockers.append("New Zealand limited company / IR4 scope is not confirmed")
    if not freshness["current"]:
        blockers.append("official source review has expired")
    return {
        **result,
        "ready": bool(
            result["ready"] and freshness["current"] and limited_company_scope_confirmed
        ),
        "limited_company_scope_confirmed": limited_company_scope_confirmed,
        "source_freshness": freshness, "blockers": sorted(set(blockers)),
        "new_zealand_tax_residency_determined": False,
        "look_through_company_status_determined": False,
        "gst_registration_liability_determined": False,
        "gst_supply_classification_performed": False,
        "corporation_tax_rate_determined": False,
        "provisional_tax_determined": False,
        "tax_calculation_performed": False, "filing_performed": False,
        "payment_performed": False, "external_submission_enabled": False,
    }
