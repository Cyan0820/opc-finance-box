from __future__ import annotations

from collections import Counter
from datetime import date
from typing import Any

from .pack_services import ServiceContext
from .tax_calendar import build_tax_calendar
from .tax_pack_lifecycle import source_freshness_from_bundle


IE_EVIDENCE_BY_RULE = {
    "ie.company.ltd.scope.evidence": ["cro_company_status_reference", "ltd_company_type_review", "legal_name_match_review", "irish_tax_residency_review"],
    "ie.tax.ct.registration.evidence": ["revenue_tax_registration_reference", "corporation_tax_registration_reference", "ct1_filing_scope_review", "ros_access_owner_review"],
    "ie.vat.registration.monitor": ["goods_services_mix_workpaper", "calendar_year_turnover_workpaper", "eu_distance_sales_and_tbe_review", "vat_registration_decision"],
    "ie.ct1.return.calendar": ["confirmed_accounting_period_end", "e_filing_status_review", "nine_month_day_rule_review", "approved_ct1_due_date"],
    "ie.corporation_tax.payment.calendar": ["company_size_review", "preliminary_ct_workpaper", "ros_payment_reference", "approved_ct_payment_dates"],
    "ie.vat.return_payment.calendar": ["vat_registration_evidence", "taxable_period_frequency", "ros_status_review", "approved_vat3_due_date"],
    "ie.cro.annual_return.calendar": ["cro_annual_return_date_evidence", "first_return_review", "ard_change_review", "financial_statements_requirement_review"],
    "ie.ct1.return.evidence": ["approved_financial_statements", "ct1_ixbrl_tax_adjustment_workpaper", "authorized_declaration", "payment_plan", "submission_receipt_plan"],
}
IN_SCOPE = {"ie_private_limited_company", "cro_company", "corporation_tax", "ct1_filer"}


def _context(context: ServiceContext) -> tuple[Any, dict[str, Any]]:
    if not context.entity_id:
        raise ValueError("Ireland tax service requires entity_id")
    entity = context.runtime.entities.get(context.entity_id)
    if entity.jurisdiction != "IE" or entity.tax_pack != "jurisdiction.ie_private_limited_company":
        raise ValueError(f"Entity {entity.entity_id} does not use jurisdiction.ie_private_limited_company")
    return entity, context.runtime.tax_rules(entity.entity_id)


def _iso(value: Any, field: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError(f"{field} must use YYYY-MM-DD") from exc


def _freshness(bundle: dict[str, Any], value: Any) -> dict[str, Any]:
    return source_freshness_from_bundle(bundle, value)


def _ref(value: Any, field: str) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an evidence reference object")
    source = str(value.get("source_reference") or "").strip()
    captured = str(value.get("captured_at") or "").strip()
    if not source or not captured:
        raise ValueError(f"{field} requires source_reference and captured_at")
    _iso(captured[:10], f"{field}.captured_at")
    return {"source_reference": source, "captured_at": captured}


def build_ie_registration_profile(payload: dict[str, Any], context: ServiceContext) -> dict[str, Any]:
    entity, bundle = _context(context)
    if any(field in payload for field in ("company_number", "tax_reference", "tax_id", "vat_number", "vat_registration_number")):
        raise ValueError("Do not pass a raw company number, tax reference or VAT number; provide an evidence reference")
    registrations = {item.lower() for item in entity.tax_registrations}
    entity_type = str(payload.get("entity_type") or ("private_company_limited_by_shares" if registrations & {"ie_private_limited_company", "cro_company"} else "unknown")).lower()
    if entity_type not in {"private_company_limited_by_shares", "dac", "clg", "plc", "overseas_company", "other", "unknown"}:
        raise ValueError("entity_type is not supported by this Ireland LTD Pack")
    entity_evidence = _ref(payload.get("entity_type_evidence"), "entity_type_evidence")
    residency_evidence = _ref(payload.get("tax_residency_evidence"), "tax_residency_evidence")
    if entity_type == "private_company_limited_by_shares":
        applicability, entity_status = "in_scope_private_limited_company", "confirmed" if entity_evidence else "needs_evidence"
    elif entity_type == "unknown":
        applicability, entity_status = "unknown", "needs_confirmation"
    else:
        applicability, entity_status = "outside_pack_scope", "outside_pack_scope"
    fields = {
        "tax_reference": ("tax_reference_status", "tax_reference_evidence", {"tax_reference_confirmed"}),
        "corporation_tax": ("corporation_tax_status", "corporation_tax_evidence", {"corporation_tax", "ct1_filer"}),
        "vat": ("vat_status", "vat_evidence", {"vat", "vat_registered", "ie_vat"}),
        "employer": ("employer_status", "employer_evidence", {"employer", "paye_registered"}),
    }
    statuses = {}
    for label, (status_field, evidence_field, codes) in fields.items():
        declared = str(payload.get(status_field) or ("confirmed" if registrations & codes else "not_registered")).lower()
        if declared not in {"confirmed", "pending", "not_registered", "unknown"}:
            raise ValueError(f"{status_field} has unsupported status")
        evidence = _ref(payload.get(evidence_field), evidence_field)
        review = "confirmed" if declared == "confirmed" and evidence else "needs_evidence" if declared == "confirmed" else "not_registered" if declared == "not_registered" else "needs_confirmation"
        statuses[label] = {"declared_status": declared, "review_status": review, "evidence": evidence, "raw_identifier_collected": False}
    freshness = _freshness(bundle, payload.get("as_of"))
    blockers = []
    if entity_status != "confirmed": blockers.append(f"entity type: {entity_status}")
    if not residency_evidence: blockers.append("Irish tax residency: needs_evidence")
    for label in ("tax_reference", "corporation_tax"):
        if statuses[label]["review_status"] != "confirmed": blockers.append(f"{label} evidence: {statuses[label]['review_status']}")
    for label in ("vat", "employer"):
        if statuses[label]["review_status"] in {"needs_evidence", "needs_confirmation"}: blockers.append(f"{label} evidence: {statuses[label]['review_status']}")
    if not freshness["current"]: blockers.append("official source review has expired")
    sources = {item["id"]: item for item in bundle["rules"]["sources"]}
    return {"ready": not blockers, "entity_id": entity.entity_id, "jurisdiction": "IE", "tax_pack": bundle["pack_id"], "tax_pack_version": bundle["pack_version"], "tax_readiness": entity.tax_readiness, "scope": "Ireland private company limited by shares registration evidence only", "applicability": applicability, "entity_type": {"value": entity_type, "status": entity_status, "evidence": entity_evidence}, "irish_tax_residency": {"status": "confirmed" if residency_evidence else "needs_evidence", "evidence": residency_evidence, "determined_by_system": False}, "registrations": statuses, "raw_company_identifier_collected": False, "source_freshness": freshness, "official_sources": [sources["ie_cro_ltd_company_type_current"], sources["ie_revenue_tax_registration_2026"], sources["ie_revenue_company_residence_2026"], sources["ie_revenue_vat_thresholds_2026"]], "blockers": blockers, "review_gate": "tax_registration_confirmation", "human_review_required": True, **_guardrails()}


def _guardrails() -> dict[str, bool]:
    return {"irish_tax_residency_determined": False, "vat_registration_liability_determined": False, "vat_supply_classification_performed": False, "corporation_tax_rate_determined": False, "preliminary_tax_determined": False, "tax_calculation_performed": False, "filing_performed": False, "payment_performed": False, "external_submission_enabled": False}


def build_ie_evidence_checklist(payload: dict[str, Any], context: ServiceContext) -> dict[str, Any]:
    entity, bundle = _context(context)
    raw = payload.get("provided_evidence") or []
    if not isinstance(raw, list) or any(not isinstance(item, dict) for item in raw):
        raise ValueError("provided_evidence must be a list of evidence reference objects")
    ids = [str(item.get("evidence_id") or "").strip() for item in raw]
    if any(not item for item in ids): raise ValueError("every provided evidence item requires evidence_id")
    duplicates = sorted(key for key, count in Counter(ids).items() if count > 1)
    provided = {}
    for index, item in enumerate(raw, 1):
        reference = _ref(item, f"provided_evidence[{index}]")
        provided.setdefault(ids[index - 1], reference)
    required_all = {item for values in IE_EVIDENCE_BY_RULE.values() for item in values}
    unknown = sorted(set(provided) - required_all)
    source_index = {item["id"]: item for item in bundle["rules"]["sources"]}
    items = []
    for rule in bundle["rules"]["rules"]:
        required = IE_EVIDENCE_BY_RULE[rule["id"]]
        missing = [item for item in required if item not in provided]
        items.append({"rule_id": rule["id"], "summary": rule["summary"], "automation_level": rule["automation_level"], "required_evidence": required, "provided_evidence": {item: provided[item] for item in required if item in provided}, "missing_evidence": missing, "complete": not missing, "official_sources": [source_index[item] for item in rule["source_ids"]], "human_review_required": True})
    freshness = _freshness(bundle, payload.get("as_of"))
    blockers = (["duplicate evidence ids"] if duplicates else []) + (["unknown evidence ids"] if unknown else []) + (["required evidence is missing"] if any(not item["complete"] for item in items) else []) + (["official source review has expired"] if not freshness["current"] else [])
    return {"ready": not blockers, "entity_id": entity.entity_id, "tax_pack": bundle["pack_id"], "tax_pack_version": bundle["pack_version"], "source_freshness": freshness, "items": items, "duplicate_evidence_ids": duplicates, "unknown_evidence_ids": unknown, "blockers": blockers, "review_gate": "tax_advisor_review", "human_review_required": True, "scope_note": bundle["rules"].get("scope_note"), **_guardrails()}


def build_ie_tax_calendar(payload: dict[str, Any], context: ServiceContext) -> dict[str, Any]:
    entity, bundle = _context(context)
    result = build_tax_calendar(context.runtime, entity.entity_id, period_year=payload.get("period_year"), anchors=payload.get("anchors"), as_of=payload.get("as_of"))
    freshness = _freshness(bundle, payload.get("as_of"))
    scope = bool({item.lower() for item in entity.tax_registrations} & IN_SCOPE)
    blockers = list(result.get("warnings") or [])
    if not scope: blockers.append("Ireland private limited company / CT1 scope is not confirmed")
    if not freshness["current"]: blockers.append("official source review has expired")
    return {**result, "ready": bool(result["ready"] and freshness["current"] and scope), "private_limited_company_scope_confirmed": scope, "source_freshness": freshness, "blockers": sorted(set(blockers)), **_guardrails()}
