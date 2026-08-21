from __future__ import annotations

from collections import Counter
from datetime import date
from typing import Any

from .pack_services import ServiceContext
from .tax_calendar import build_tax_calendar
from .tax_pack_lifecycle import source_freshness_from_bundle


AU_EVIDENCE_BY_RULE = {
    "au.company.proprietary.scope.evidence": [
        "asic_company_extract_reference", "legal_name_match_review",
        "proprietary_company_type_review", "registered_status_review",
    ],
    "au.tax.abn_tfn.registration.evidence": [
        "abn_entitlement_review", "abn_registration_confirmation_reference",
        "company_tfn_confirmation_reference", "legal_name_registration_match",
    ],
    "au.gst.registration.monitor": [
        "current_gst_turnover_workpaper", "projected_gst_turnover_workpaper",
        "supply_connection_and_classification_review", "gst_registration_decision",
    ],
    "au.company_tax.return_and_payment.calendar": [
        "income_year_and_balancing_date_confirmation", "lodgment_channel_review",
        "ato_entity_category_review", "approved_return_due_date",
        "approved_payment_due_date",
    ],
    "au.gst.bas.calendar": [
        "gst_registration_evidence", "bas_reporting_cycle_evidence",
        "actual_bas_statement_reference", "approved_bas_due_date",
        "payment_clearance_plan",
    ],
    "au.asic.annual_review.calendar": [
        "annual_review_date_evidence", "annual_statement_reference",
        "company_details_review", "annual_fee_due_date_review",
        "solvency_resolution_plan",
    ],
    "au.company_tax.return_evidence": [
        "approved_financial_statements", "tax_adjustment_workpaper",
        "company_return_schedule_review", "authorized_declaration",
        "payment_plan", "submission_receipt_plan",
    ],
}
ENTITY_TYPES = {
    "proprietary_company", "public_company", "foreign_company", "sole_trader",
    "partnership", "trust", "other", "unknown",
}
REGISTRATION_STATUSES = {"confirmed", "pending", "not_registered", "unknown"}
IN_SCOPE_REGISTRATIONS = {
    "au_proprietary_company", "proprietary_company", "asic_company",
    "company_tax", "company_tax_return_filer",
}


def _au_context(context: ServiceContext) -> tuple[Any, dict[str, Any]]:
    if not context.entity_id:
        raise ValueError("Australia tax service requires entity_id")
    entity = context.runtime.entities.get(context.entity_id)
    if entity.jurisdiction != "AU" or entity.tax_pack != "jurisdiction.au_proprietary_company":
        raise ValueError(
            f"Entity {entity.entity_id} does not use "
            "jurisdiction.au_proprietary_company"
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


def build_au_registration_profile(
    payload: dict[str, Any], context: ServiceContext,
) -> dict[str, Any]:
    entity, bundle = _au_context(context)
    forbidden = (
        "acn", "acn_number", "company_number", "abn", "abn_number", "tfn",
        "tfn_number", "tax_file_number", "tax_id", "gst_registration_number",
    )
    if any(field in payload for field in forbidden):
        raise ValueError(
            "Do not pass a raw ACN, ABN, TFN or tax identifier; provide an "
            "evidence reference"
        )
    registrations = {item.strip().lower() for item in entity.tax_registrations}
    entity_type = str(payload.get("entity_type") or (
        "proprietary_company"
        if registrations & {"au_proprietary_company", "proprietary_company"}
        else "unknown"
    )).strip().lower()
    if entity_type not in ENTITY_TYPES:
        raise ValueError(
            "entity_type is not supported by this Australia proprietary-company Pack"
        )
    entity_evidence = _evidence_reference(
        payload.get("entity_type_evidence"), "entity_type_evidence",
    )
    if entity_type == "proprietary_company":
        applicability = "in_scope_proprietary_company"
        entity_status = "confirmed" if entity_evidence else "needs_evidence"
    elif entity_type == "unknown":
        applicability = "unknown"
        entity_status = "needs_confirmation"
    else:
        applicability = "outside_pack_scope"
        entity_status = "outside_pack_scope"

    statuses: dict[str, dict[str, Any]] = {}
    registration_fields = {
        "abn": ("abn_status", "abn_evidence", {"abn_confirmed", "abn_registered"}),
        "company_tfn": (
            "company_tfn_status", "company_tfn_evidence",
            {"company_tfn_confirmed", "company_tax", "company_tax_return_filer"},
        ),
        "gst": ("gst_status", "gst_evidence", {"gst", "gst_registered", "au_gst"}),
        "payg_withholding": (
            "payg_withholding_status", "payg_withholding_evidence",
            {"payg_withholding", "paygw_registered"},
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
    for label in ("abn", "company_tfn"):
        if statuses[label]["review_status"] != "confirmed":
            blockers.append(f"{label} evidence: {statuses[label]['review_status']}")
    for label in ("gst", "payg_withholding"):
        if statuses[label]["review_status"] in {"needs_evidence", "needs_confirmation"}:
            blockers.append(f"{label} evidence: {statuses[label]['review_status']}")
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
        "scope": "Australian proprietary company registration evidence only",
        "applicability": applicability,
        "entity_type": {"value": entity_type, "status": entity_status, "evidence": entity_evidence},
        "registrations": statuses,
        "raw_company_identifier_collected": False,
        "source_freshness": freshness,
        "official_sources": [
            source_index["au_asic_company_annual_review_2026"],
            source_index["au_abr_abn_application_2026"],
            source_index["au_government_tax_registration_current"],
            source_index["au_ato_gst_registration_2025"],
        ],
        "blockers": blockers,
        "review_gate": "tax_registration_confirmation",
        "human_review_required": True,
        "gst_registration_liability_determined": False,
        "gst_supply_classification_performed": False,
        "company_tax_rate_determined": False,
        "tax_calculation_performed": False,
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


def build_au_evidence_checklist(
    payload: dict[str, Any], context: ServiceContext,
) -> dict[str, Any]:
    entity, bundle = _au_context(context)
    provided, duplicates = _provided_evidence(payload)
    required_ids = {
        evidence_id for values in AU_EVIDENCE_BY_RULE.values() for evidence_id in values
    }
    unknown = sorted(set(provided) - required_ids)
    source_index = {source["id"]: source for source in bundle["rules"]["sources"]}
    items = []
    for rule in bundle["rules"]["rules"]:
        required = AU_EVIDENCE_BY_RULE[rule["id"]]
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
        "gst_registration_liability_determined": False,
        "gst_supply_classification_performed": False,
        "company_tax_rate_determined": False,
        "tax_calculation_performed": False, "filing_performed": False,
        "payment_performed": False, "external_submission_enabled": False,
        "scope_note": bundle["rules"].get("scope_note"),
    }


def build_au_tax_calendar(
    payload: dict[str, Any], context: ServiceContext,
) -> dict[str, Any]:
    entity, bundle = _au_context(context)
    result = build_tax_calendar(
        context.runtime, entity.entity_id,
        period_year=payload.get("period_year"), anchors=payload.get("anchors"),
        as_of=payload.get("as_of"),
    )
    freshness = _freshness(bundle, payload.get("as_of"))
    registrations = {item.strip().lower() for item in entity.tax_registrations}
    proprietary_company_scope_confirmed = bool(registrations & IN_SCOPE_REGISTRATIONS)
    blockers = list(result.get("warnings") or [])
    if not proprietary_company_scope_confirmed:
        blockers.append("Australian proprietary company / company tax scope is not confirmed")
    if not freshness["current"]:
        blockers.append("official source review has expired")
    return {
        **result,
        "ready": bool(
            result["ready"] and freshness["current"]
            and proprietary_company_scope_confirmed
        ),
        "proprietary_company_scope_confirmed": proprietary_company_scope_confirmed,
        "source_freshness": freshness, "blockers": sorted(set(blockers)),
        "gst_registration_liability_determined": False,
        "gst_supply_classification_performed": False,
        "company_tax_rate_determined": False,
        "tax_calculation_performed": False, "filing_performed": False,
        "payment_performed": False, "external_submission_enabled": False,
    }
