from __future__ import annotations

from collections import Counter
from datetime import date
from typing import Any

from .pack_services import ServiceContext
from .tax_calendar import build_tax_calendar
from .tax_pack_lifecycle import source_freshness_from_bundle


HK_EVIDENCE_BY_RULE = {
    "hk.business_registration.brn.evidence": [
        "business_registration_certificate_reference",
        "legal_name_match_review",
        "brn_ubi_mapping_review",
    ],
    "hk.profits_tax.corporation_scope.evidence": [
        "incorporation_or_registration_evidence",
        "entity_type_review",
        "profits_tax_scope_review",
        "territorial_source_review_plan",
    ],
    "hk.profits_tax.two_tier_eligibility.evidence": [
        "connected_entity_register",
        "two_tier_nomination_review",
        "concessionary_regime_review",
    ],
    "hk.profits_tax.bir51.calendar": [
        "bir51_issue_date",
        "year_of_assessment_confirmation",
        "approved_filing_deadline",
        "tax_representative_extension_review",
        "electronic_filing_requirement_review",
    ],
    "hk.profits_tax.provisional.calendar": [
        "assessment_and_demand_note_reference",
        "approved_payment_schedule",
        "provisional_tax_basis_review",
        "holding_over_applicability_review",
    ],
    "hk.profits_tax.bir51.return_evidence": [
        "approved_financial_statements",
        "audit_report_applicability_review",
        "tax_computation_workpaper",
        "supporting_forms_assessment",
        "authorized_signer_confirmation",
        "submission_receipt_plan",
    ],
}
ENTITY_TYPES = {
    "hong_kong_corporation", "foreign_corporation", "unincorporated_business", "unknown",
}
IN_SCOPE_REGISTRATIONS = {"hk_corporation", "profits_tax", "bir51_filer"}
OUTSIDE_SCOPE_REGISTRATIONS = {"unincorporated_business", "bir52_filer", "sole_proprietorship"}
REGISTRATION_STATUSES = {"confirmed", "pending", "not_registered", "unknown"}


def _hk_context(context: ServiceContext) -> tuple[Any, dict[str, Any]]:
    if not context.entity_id:
        raise ValueError("Hong Kong tax service requires entity_id")
    entity = context.runtime.entities.get(context.entity_id)
    if entity.jurisdiction != "HK" or entity.tax_pack != "jurisdiction.hk":
        raise ValueError(f"Entity {entity.entity_id} does not use jurisdiction.hk")
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


def build_hk_registration_profile(
    payload: dict[str, Any], context: ServiceContext,
) -> dict[str, Any]:
    entity, bundle = _hk_context(context)
    if any(field in payload for field in (
        "brn", "business_registration_number", "tax_id", "ubi_number",
    )):
        raise ValueError("Do not pass a raw BRN or tax identifier; provide an evidence reference")
    registrations = {item.strip().lower() for item in entity.tax_registrations}
    raw_entity_type = payload.get("entity_type")
    if raw_entity_type is None:
        if "hk_corporation" in registrations:
            entity_type = "hong_kong_corporation"
        elif registrations & OUTSIDE_SCOPE_REGISTRATIONS:
            entity_type = "unincorporated_business"
        else:
            entity_type = "unknown"
    else:
        entity_type = str(raw_entity_type).strip().lower()
    if entity_type not in ENTITY_TYPES:
        raise ValueError("entity_type is not supported by this Hong Kong corporation Pack")
    entity_evidence = _evidence_reference(payload.get("entity_type_evidence"), "entity_type_evidence")
    if entity_type == "hong_kong_corporation":
        entity_type_status = "confirmed" if entity_evidence else "needs_evidence"
        applicability = "in_scope_hong_kong_corporation"
    elif entity_type == "unknown":
        entity_type_status = "needs_confirmation"
        applicability = "unknown"
    else:
        entity_type_status = "outside_pack_scope"
        applicability = "outside_pack_scope"

    brn_status = str(payload.get("brn_status") or (
        "confirmed" if "business_registration_confirmed" in registrations else "unknown"
    )).strip().lower()
    if brn_status not in REGISTRATION_STATUSES:
        raise ValueError("brn_status must be confirmed, pending, not_registered or unknown")
    brn_evidence = _evidence_reference(payload.get("brn_evidence"), "brn_evidence")
    brn_review_status = (
        "confirmed" if brn_status == "confirmed" and brn_evidence
        else "needs_evidence" if brn_status == "confirmed"
        else "needs_confirmation"
    )

    profits_tax_status = str(payload.get("profits_tax_status") or (
        "confirmed" if registrations & {"profits_tax", "bir51_filer"} else "unknown"
    )).strip().lower()
    if profits_tax_status not in REGISTRATION_STATUSES:
        raise ValueError(
            "profits_tax_status must be confirmed, pending, not_registered or unknown"
        )
    profits_tax_evidence = _evidence_reference(
        payload.get("profits_tax_evidence"), "profits_tax_evidence",
    )
    profits_tax_review_status = (
        "confirmed" if profits_tax_status == "confirmed" and profits_tax_evidence
        else "needs_evidence" if profits_tax_status == "confirmed"
        else "needs_confirmation"
    )
    freshness = _freshness(bundle, payload.get("as_of"))
    blockers = []
    if entity_type_status != "confirmed":
        blockers.append(f"entity type: {entity_type_status}")
    if brn_review_status != "confirmed":
        blockers.append(f"BRN / UBI evidence: {brn_review_status}")
    if profits_tax_review_status != "confirmed":
        blockers.append(f"profits tax / BIR51 evidence: {profits_tax_review_status}")
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
        "scope": "Hong Kong corporation registration and Profits Tax evidence only",
        "applicability": applicability,
        "entity_type": {
            "value": entity_type,
            "status": entity_type_status,
            "evidence": entity_evidence,
        },
        "business_registration": {
            "declared_status": brn_status,
            "review_status": brn_review_status,
            "evidence": brn_evidence,
            "raw_identifier_collected": False,
        },
        "profits_tax_registration": {
            "declared_status": profits_tax_status,
            "review_status": profits_tax_review_status,
            "evidence": profits_tax_evidence,
        },
        "source_freshness": freshness,
        "official_sources": [
            source_index["hk_ird_unique_business_identifier"],
            source_index["hk_ird_profits_tax_current"],
            source_index["hk_ird_bir51_notes_2025_26"],
        ],
        "blockers": blockers,
        "review_gate": "tax_registration_confirmation",
        "human_review_required": True,
        "territorial_source_determination_performed": False,
        "two_tier_eligibility_determined": False,
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


def build_hk_evidence_checklist(
    payload: dict[str, Any], context: ServiceContext,
) -> dict[str, Any]:
    entity, bundle = _hk_context(context)
    provided, duplicates = _provided_evidence(payload)
    required_ids = {
        evidence_id for values in HK_EVIDENCE_BY_RULE.values() for evidence_id in values
    }
    unknown = sorted(set(provided) - required_ids)
    source_index = {source["id"]: source for source in bundle["rules"]["sources"]}
    items = []
    for rule in bundle["rules"]["rules"]:
        required = HK_EVIDENCE_BY_RULE[rule["id"]]
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
        "territorial_source_determination_performed": False,
        "two_tier_eligibility_determined": False,
        "tax_calculation_performed": False,
        "filing_performed": False,
        "external_submission_enabled": False,
        "scope_note": bundle["rules"].get("scope_note"),
    }


def build_hk_tax_calendar(payload: dict[str, Any], context: ServiceContext) -> dict[str, Any]:
    entity, bundle = _hk_context(context)
    result = build_tax_calendar(
        context.runtime,
        entity.entity_id,
        period_year=payload.get("period_year"),
        anchors=payload.get("anchors"),
        as_of=payload.get("as_of"),
    )
    freshness = _freshness(bundle, payload.get("as_of"))
    registrations = {item.strip().lower() for item in entity.tax_registrations}
    corporation_scope_confirmed = bool(registrations & IN_SCOPE_REGISTRATIONS)
    blockers = list(result.get("warnings") or [])
    if not corporation_scope_confirmed:
        blockers.append("Hong Kong corporation / BIR51 scope is not confirmed")
    if not freshness["current"]:
        blockers.append("official source review has expired")
    return {
        **result,
        "ready": bool(result["ready"] and freshness["current"] and corporation_scope_confirmed),
        "corporation_scope_confirmed": corporation_scope_confirmed,
        "source_freshness": freshness,
        "blockers": sorted(set(blockers)),
        "territorial_source_determination_performed": False,
        "two_tier_eligibility_determined": False,
        "tax_calculation_performed": False,
        "filing_performed": False,
        "external_submission_enabled": False,
    }
