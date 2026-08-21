from __future__ import annotations

from collections import Counter
from datetime import date
from typing import Any

from .pack_services import ServiceContext
from .tax_calendar import build_tax_calendar
from .tax_pack_lifecycle import source_freshness_from_bundle


US_EVIDENCE_BY_RULE = {
    "us.federal.c_corp.classification.evidence": [
        "formation_documents",
        "federal_tax_classification_evidence",
        "domestic_entity_confirmation",
        "classification_reviewer_decision",
    ],
    "us.federal.ein.registration.evidence": [
        "ein_confirmation_reference",
        "legal_name_match_review",
        "responsible_party_review",
    ],
    "us.federal.form_1120.calendar.2025": [
        "confirmed_form_1120_filer",
        "tax_year_end",
        "tax_year_form_instructions",
        "approved_due_date_review",
    ],
    "us.federal.corporate_estimated_tax.calendar": [
        "estimated_tax_applicability_decision",
        "current_year_estimated_tax_support",
        "approved_installment_schedule",
        "eftps_enrollment_and_payment_evidence",
    ],
    "us.federal.form_1120.return_evidence.2025": [
        "approved_financial_statements",
        "book_tax_adjustment_workpaper",
        "estimated_and_other_tax_payment_evidence",
        "required_schedule_assessment",
        "authorized_signer_confirmation",
        "efile_applicability_review",
    ],
}
CLASSIFICATIONS = {
    "c_corporation", "domestic_c_corporation", "s_corporation", "partnership",
    "disregarded_entity", "unknown",
}
C_CORP_REGISTRATIONS = {"us_c_corporation", "c_corporation", "form_1120_filer"}
OUTSIDE_SCOPE_REGISTRATIONS = {
    "s_corporation", "form_1120_s_filer", "partnership", "form_1065_filer",
    "disregarded_entity",
}
EIN_STATUSES = {"confirmed", "pending", "not_applied", "unknown"}


def _us_context(context: ServiceContext) -> tuple[Any, dict[str, Any]]:
    if not context.entity_id:
        raise ValueError("US federal tax service requires entity_id")
    entity = context.runtime.entities.get(context.entity_id)
    if entity.jurisdiction != "US" or entity.tax_pack != "jurisdiction.us_federal":
        raise ValueError(f"Entity {entity.entity_id} does not use jurisdiction.us_federal")
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


def build_us_federal_registration_profile(
    payload: dict[str, Any], context: ServiceContext,
) -> dict[str, Any]:
    entity, bundle = _us_context(context)
    if any(field in payload for field in ("ein", "ein_number", "tax_id")):
        raise ValueError("Do not pass raw EIN or tax identifiers; provide an evidence reference")
    registrations = {item.strip().lower() for item in entity.tax_registrations}
    raw_classification = payload.get("federal_tax_classification")
    if raw_classification is None:
        if registrations & C_CORP_REGISTRATIONS:
            classification = "c_corporation"
        elif registrations & OUTSIDE_SCOPE_REGISTRATIONS:
            classification = sorted(registrations & OUTSIDE_SCOPE_REGISTRATIONS)[0]
        else:
            classification = "unknown"
    else:
        classification = str(raw_classification).strip().lower()
    if classification not in CLASSIFICATIONS:
        raise ValueError("federal_tax_classification is not a supported explicit classification")
    classification_evidence = _evidence_reference(
        payload.get("classification_evidence"), "classification_evidence",
    )
    if classification in {"c_corporation", "domestic_c_corporation"}:
        classification_status = "confirmed" if classification_evidence else "needs_evidence"
        applicability = "in_scope_c_corporation"
    elif classification == "unknown":
        classification_status = "needs_confirmation"
        applicability = "unknown"
    else:
        classification_status = "outside_pack_scope"
        applicability = "outside_pack_scope"

    raw_ein_status = payload.get("ein_status")
    if raw_ein_status is None:
        ein_status = "confirmed" if "ein_confirmed" in registrations else "unknown"
    else:
        ein_status = str(raw_ein_status).strip().lower()
    if ein_status not in EIN_STATUSES:
        raise ValueError("ein_status must be confirmed, pending, not_applied or unknown")
    ein_evidence = _evidence_reference(payload.get("ein_evidence"), "ein_evidence")
    ein_review_status = (
        "confirmed" if ein_status == "confirmed" and ein_evidence
        else "needs_evidence" if ein_status == "confirmed"
        else "needs_confirmation"
    )
    freshness = _freshness(bundle, payload.get("as_of"))
    blockers = []
    if classification_status != "confirmed":
        blockers.append(f"federal tax classification: {classification_status}")
    if ein_review_status != "confirmed":
        blockers.append(f"EIN registration evidence: {ein_review_status}")
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
        "scope": "US federal domestic C corporation registration evidence only",
        "applicability": applicability,
        "federal_tax_classification": {
            "value": classification,
            "status": classification_status,
            "evidence": classification_evidence,
        },
        "ein_registration": {
            "declared_status": ein_status,
            "review_status": ein_review_status,
            "evidence": ein_evidence,
            "raw_identifier_collected": False,
        },
        "source_freshness": freshness,
        "official_sources": [
            source_index["us_irs_topic_407_entity_classification"],
            source_index["us_irs_ein_current"],
        ],
        "blockers": blockers,
        "review_gate": "tax_registration_confirmation",
        "human_review_required": True,
        "classification_determination_performed": False,
        "tax_calculation_performed": False,
        "filing_performed": False,
        "external_submission_enabled": False,
    }


def _provided_evidence(payload: dict[str, Any]) -> tuple[dict[str, dict[str, str]], list[str]]:
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


def build_us_federal_evidence_checklist(
    payload: dict[str, Any], context: ServiceContext,
) -> dict[str, Any]:
    entity, bundle = _us_context(context)
    provided, duplicates = _provided_evidence(payload)
    required_ids = {
        evidence_id for values in US_EVIDENCE_BY_RULE.values() for evidence_id in values
    }
    unknown = sorted(set(provided) - required_ids)
    source_index = {source["id"]: source for source in bundle["rules"]["sources"]}
    items = []
    for rule in bundle["rules"]["rules"]:
        required = US_EVIDENCE_BY_RULE[rule["id"]]
        missing = [evidence_id for evidence_id in required if evidence_id not in provided]
        items.append({
            "rule_id": rule["id"],
            "summary": rule["summary"],
            "automation_level": rule["automation_level"],
            "required_evidence": required,
            "provided_evidence": {
                evidence_id: provided[evidence_id]
                for evidence_id in required if evidence_id in provided
            },
            "missing_evidence": missing,
            "complete": not missing,
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
        "ready": not blockers,
        "entity_id": entity.entity_id,
        "tax_pack": bundle["pack_id"],
        "tax_pack_version": bundle["pack_version"],
        "rules_verified_at": bundle["rules"]["verified_at"],
        "source_freshness": freshness,
        "items": items,
        "duplicate_evidence_ids": duplicates,
        "unknown_evidence_ids": unknown,
        "blockers": blockers,
        "review_gate": "tax_advisor_review",
        "human_review_required": True,
        "tax_calculation_performed": False,
        "filing_performed": False,
        "external_submission_enabled": False,
        "scope_note": bundle["rules"].get("scope_note"),
    }


def build_us_federal_calendar(payload: dict[str, Any], context: ServiceContext) -> dict[str, Any]:
    entity, bundle = _us_context(context)
    result = build_tax_calendar(
        context.runtime,
        entity.entity_id,
        period_year=payload.get("period_year"),
        anchors=payload.get("anchors"),
        as_of=payload.get("as_of"),
    )
    freshness = _freshness(bundle, payload.get("as_of"))
    blockers = list(result.get("warnings") or [])
    registrations = {item.strip().lower() for item in entity.tax_registrations}
    c_corp_scope_confirmed = bool(registrations & C_CORP_REGISTRATIONS)
    if not c_corp_scope_confirmed:
        blockers.append("C corporation / Form 1120 registration scope is not confirmed")
    if not freshness["current"]:
        blockers.append("official source review has expired")
    return {
        **result,
        "ready": bool(result["ready"] and freshness["current"] and c_corp_scope_confirmed),
        "c_corporation_scope_confirmed": c_corp_scope_confirmed,
        "source_freshness": freshness,
        "blockers": sorted(set(blockers)),
        "tax_calculation_performed": False,
        "filing_performed": False,
        "external_submission_enabled": False,
    }
