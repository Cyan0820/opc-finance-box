from __future__ import annotations

from collections import Counter
from datetime import date
from typing import Any

from .pack_services import ServiceContext
from .tax_calendar import build_tax_calendar
from .tax_pack_lifecycle import source_freshness_from_bundle


AE_EVIDENCE_BY_RULE = {
    "ae.company.domestic.scope.evidence": [
        "incorporation_or_formation_reference",
        "legal_form_review",
        "trade_licence_status_reference",
        "licensing_authority_and_emirate_review",
        "registered_address_review",
        "uae_tax_residency_review",
    ],
    "ae.corporate_tax.registration.evidence": [
        "corporate_tax_registration_status_reference",
        "tax_period_review",
        "emaratax_access_review",
        "branch_and_head_office_scope_review",
        "exempt_person_and_small_business_relief_review",
    ],
    "ae.free_zone.status.evidence": [
        "free_zone_licensing_authority_review",
        "free_zone_person_status_review",
        "qualifying_free_zone_person_status_review",
        "qualifying_and_excluded_activity_review",
        "substance_and_audited_financial_statements_review",
        "qualifying_income_and_de_minimis_review",
    ],
    "ae.vat.registration.evidence": [
        "vat_registration_status_reference",
        "fta_vat_tax_period_review",
        "supply_and_import_classification_review",
        "input_tax_recovery_review",
    ],
    "ae.accounting.records.evidence": [
        "approved_financial_statements",
        "accounting_standard_and_method_review",
        "audit_requirement_review",
        "transaction_asset_liability_inventory_records",
        "bank_invoice_and_business_correspondence_records",
        "record_retention_review",
    ],
    "ae.corporate_tax.return_payment.calendar": [
        "confirmed_tax_period_end",
        "corporate_tax_registration_and_return_obligation_review",
        "free_zone_or_exempt_person_status_review",
        "extension_or_special_deadline_review",
        "emaratax_due_date_reference",
        "approved_corporate_tax_return_and_payment_due_date",
    ],
    "ae.vat.return_payment.calendar": [
        "confirmed_vat_registration_status",
        "confirmed_fta_vat_tax_period",
        "holiday_and_extension_review",
        "emaratax_vat_due_date_reference",
        "approved_vat_return_and_payment_due_date",
    ],
    "ae.return.evidence": [
        "approved_financial_statements",
        "corporate_tax_workpaper",
        "vat_workpaper",
        "free_zone_status_workpaper",
        "authorized_emaratax_declarations",
        "payment_plan",
        "submission_receipt_plan",
    ],
}

IN_SCOPE = {
    "ae_domestic_juridical_person",
    "mainland_llc",
    "single_person_llc",
    "free_zone_juridical_person",
    "corporate_tax",
}


def _context(context: ServiceContext) -> tuple[Any, dict[str, Any]]:
    if not context.entity_id:
        raise ValueError("UAE tax service requires entity_id")
    entity = context.runtime.entities.get(context.entity_id)
    if (
        entity.jurisdiction != "AE"
        or entity.tax_pack != "jurisdiction.ae_domestic_juridical_person"
    ):
        raise ValueError(
            f"Entity {entity.entity_id} does not use "
            "jurisdiction.ae_domestic_juridical_person"
        )
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


def _guardrails() -> dict[str, bool]:
    return {
        "uae_tax_residency_determined": False,
        "effective_management_and_control_determined": False,
        "entity_type_determined_by_system": False,
        "corporate_tax_registration_or_liability_determined": False,
        "corporate_tax_rate_or_amount_determined": False,
        "qualifying_free_zone_person_status_determined": False,
        "qualifying_or_excluded_income_determined": False,
        "free_zone_substance_or_de_minimis_determined": False,
        "small_business_relief_or_exempt_status_determined": False,
        "tax_group_eligibility_determined": False,
        "accounting_standard_method_or_audit_requirement_determined": False,
        "vat_registration_liability_determined": False,
        "vat_supply_classification_performed": False,
        "vat_rate_input_recovery_or_amount_determined": False,
        "cross_border_or_permanent_establishment_determined": False,
        "transfer_pricing_or_customs_determined": False,
        "withholding_tax_treatment_determined": False,
        "payroll_or_social_insurance_determined": False,
        "tax_calculation_performed": False,
        "filing_performed": False,
        "payment_performed": False,
        "external_submission_enabled": False,
    }


def _registration_review(declared: str, evidence: dict[str, str] | None) -> str:
    if declared == "confirmed" and evidence:
        return "confirmed"
    if declared == "confirmed":
        return "needs_evidence"
    if declared in {"not_registered", "not_applicable"}:
        return declared
    return "needs_confirmation"


def build_ae_registration_profile(
    payload: dict[str, Any], context: ServiceContext,
) -> dict[str, Any]:
    entity, bundle = _context(context)
    forbidden = {
        "trade_licence_number", "trade_license_number", "company_number",
        "corporate_tax_trn", "vat_trn", "tax_registration_number", "tax_id",
        "emaratax_user_id", "electronic_certificate", "emirates_id",
        "passport_number",
    }
    if forbidden & set(payload):
        raise ValueError(
            "Do not pass a raw UAE licence, tax, identity or EmaraTax identifier; "
            "provide an evidence reference"
        )
    registrations = {item.lower() for item in entity.tax_registrations}
    if "mainland_llc" in registrations:
        default_type = "mainland_limited_liability_company"
    elif "single_person_llc" in registrations:
        default_type = "mainland_single_person_limited_liability_company"
    elif "free_zone_juridical_person" in registrations:
        default_type = "free_zone_juridical_person"
    else:
        default_type = "unknown"
    entity_type = str(payload.get("entity_type") or default_type).strip().lower()
    in_scope_types = {
        "mainland_limited_liability_company",
        "mainland_single_person_limited_liability_company",
        "free_zone_juridical_person",
    }
    allowed_types = in_scope_types | {
        "public_joint_stock_company", "private_joint_stock_company",
        "partnership", "foreign_juridical_person", "branch", "natural_person",
        "sole_establishment", "nonprofit", "other", "unknown",
    }
    if entity_type not in allowed_types:
        raise ValueError("entity_type is not supported by this UAE juridical person Pack")
    entity_evidence = _ref(payload.get("entity_type_evidence"), "entity_type_evidence")
    residency_evidence = _ref(
        payload.get("tax_residency_evidence"), "tax_residency_evidence",
    )
    if entity_type in in_scope_types:
        applicability = "in_scope_uae_domestic_juridical_person"
        entity_status = "confirmed" if entity_evidence else "needs_evidence"
    elif entity_type == "unknown":
        applicability, entity_status = "unknown", "needs_confirmation"
    else:
        applicability, entity_status = "outside_pack_scope", "outside_pack_scope"

    fields = {
        "trade_licence": (
            "trade_licence_status", "trade_licence_evidence",
            {"ae_domestic_juridical_person", "mainland_llc", "single_person_llc", "free_zone_juridical_person"},
        ),
        "corporate_tax": (
            "corporate_tax_status", "corporate_tax_evidence",
            {"corporate_tax", "uae_corporate_tax"},
        ),
        "free_zone": (
            "free_zone_status", "free_zone_evidence",
            {"free_zone_juridical_person", "free_zone_person"},
        ),
        "vat": (
            "vat_status", "vat_evidence", {"vat", "uae_vat", "vat_registrant"},
        ),
    }
    statuses: dict[str, dict[str, Any]] = {}
    for label, (status_field, evidence_field, codes) in fields.items():
        if label == "free_zone" and entity_type != "free_zone_juridical_person":
            default = "not_applicable"
        else:
            default = "confirmed" if registrations & codes else "not_registered"
        declared = str(payload.get(status_field) or default).strip().lower()
        if declared not in {
            "confirmed", "pending", "not_registered", "not_applicable", "unknown",
        }:
            raise ValueError(f"{status_field} has unsupported status")
        evidence = _ref(payload.get(evidence_field), evidence_field)
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
    if not residency_evidence:
        blockers.append("UAE tax residency: needs_evidence")
    for label in ("trade_licence", "corporate_tax"):
        if statuses[label]["review_status"] != "confirmed":
            blockers.append(f"{label} evidence: {statuses[label]['review_status']}")
    if entity_type == "free_zone_juridical_person":
        if statuses["free_zone"]["review_status"] != "confirmed":
            blockers.append(
                f"free_zone evidence: {statuses['free_zone']['review_status']}"
            )
    for label in ("vat",):
        if statuses[label]["review_status"] in {"needs_evidence", "needs_confirmation"}:
            blockers.append(f"{label} evidence: {statuses[label]['review_status']}")
    if not freshness["current"]:
        blockers.append("official source review has expired")
    sources = {item["id"]: item for item in bundle["rules"]["sources"]}
    return {
        "ready": not blockers,
        "entity_id": entity.entity_id,
        "jurisdiction": "AE",
        "tax_pack": bundle["pack_id"],
        "tax_pack_version": bundle["pack_version"],
        "tax_readiness": entity.tax_readiness,
        "scope": "UAE domestic mainland/free-zone juridical person evidence only",
        "applicability": applicability,
        "entity_type": {
            "value": entity_type, "status": entity_status,
            "evidence": entity_evidence, "determined_by_system": False,
        },
        "uae_tax_residency": {
            "status": "confirmed" if residency_evidence else "needs_evidence",
            "evidence": residency_evidence,
            "determined_by_system": False,
        },
        "registrations": statuses,
        "raw_licence_identity_or_tax_identifier_collected": False,
        "source_freshness": freshness,
        "official_sources": [sources[item] for item in (
            "ae_government_mainland_legal_forms_current",
            "ae_fta_corporate_tax_registration_current",
            "ae_mof_corporate_tax_current",
            "ae_fta_corporate_return_current",
            "ae_fta_free_zone_guidance_current",
            "ae_mof_free_zone_2025_current",
            "ae_fta_vat_current",
            "ae_fta_tax_residency_certificate_current",
            "ae_fta_records_current",
        )],
        "blockers": blockers,
        "review_gate": "tax_registration_confirmation",
        "human_review_required": True,
        **_guardrails(),
    }


def build_ae_evidence_checklist(
    payload: dict[str, Any], context: ServiceContext,
) -> dict[str, Any]:
    entity, bundle = _context(context)
    raw = payload.get("provided_evidence") or []
    if not isinstance(raw, list) or any(not isinstance(item, dict) for item in raw):
        raise ValueError("provided_evidence must be a list of evidence reference objects")
    ids = [str(item.get("evidence_id") or "").strip() for item in raw]
    if any(not item for item in ids):
        raise ValueError("every provided evidence item requires evidence_id")
    duplicates = sorted(key for key, count in Counter(ids).items() if count > 1)
    provided: dict[str, dict[str, str]] = {}
    for index, item in enumerate(raw, 1):
        reference = _ref(item, f"provided_evidence[{index}]")
        if reference:
            provided.setdefault(ids[index - 1], reference)
    required_all = {item for values in AE_EVIDENCE_BY_RULE.values() for item in values}
    unknown = sorted(set(provided) - required_all)
    source_index = {item["id"]: item for item in bundle["rules"]["sources"]}
    items = []
    for rule in bundle["rules"]["rules"]:
        required = AE_EVIDENCE_BY_RULE[rule["id"]]
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
    blockers = (
        (["duplicate evidence ids"] if duplicates else [])
        + (["unknown evidence ids"] if unknown else [])
        + (["required evidence is missing"] if any(not item["complete"] for item in items) else [])
        + (["official source review has expired"] if not freshness["current"] else [])
    )
    return {
        "ready": not blockers, "entity_id": entity.entity_id,
        "tax_pack": bundle["pack_id"], "tax_pack_version": bundle["pack_version"],
        "source_freshness": freshness, "items": items,
        "duplicate_evidence_ids": duplicates, "unknown_evidence_ids": unknown,
        "blockers": blockers, "review_gate": "tax_advisor_review",
        "human_review_required": True,
        "scope_note": bundle["rules"].get("scope_note"), **_guardrails(),
    }


def build_ae_tax_calendar(
    payload: dict[str, Any], context: ServiceContext,
) -> dict[str, Any]:
    entity, bundle = _context(context)
    result = build_tax_calendar(
        context.runtime, entity.entity_id,
        period_year=payload.get("period_year"), anchors=payload.get("anchors"),
        as_of=payload.get("as_of"),
    )
    freshness = _freshness(bundle, payload.get("as_of"))
    scope = bool({item.lower() for item in entity.tax_registrations} & IN_SCOPE)
    blockers = list(result.get("warnings") or [])
    if not scope:
        blockers.append("UAE domestic juridical person tax scope is not confirmed")
    if not freshness["current"]:
        blockers.append("official source review has expired")
    return {
        **result,
        "ready": bool(result["ready"] and freshness["current"] and scope),
        "domestic_juridical_person_scope_confirmed": scope,
        "source_freshness": freshness,
        "blockers": sorted(set(blockers)),
        **_guardrails(),
    }
