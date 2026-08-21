from __future__ import annotations

from collections import Counter
from datetime import date
from typing import Any

from .pack_services import ServiceContext
from .tax_calendar import build_tax_calendar
from .tax_pack_lifecycle import source_freshness_from_bundle


DE_EVIDENCE_BY_RULE = {
    "de.company.gmbh.scope.evidence": [
        "commercial_register_status_reference", "gmbh_legal_form_review",
        "legal_name_match_review", "german_tax_residency_review",
    ],
    "de.corporation_tax.registration.evidence": [
        "corporation_tax_registration_reference", "tax_period_review",
        "elster_or_advisor_access_review", "e_bilanz_readiness_review",
    ],
    "de.trade_tax.registration.evidence": [
        "trade_tax_registration_reference", "business_location_review",
        "competent_municipality_review", "municipal_rate_exclusion_acknowledgement",
    ],
    "de.vat.registration.evidence": [
        "vat_registration_reference", "reporting_frequency_review",
        "permanent_extension_status_review", "transactions_and_oss_ioss_review",
    ],
    "de.corporation_tax.return.calendar": [
        "confirmed_tax_period_end", "tax_advisor_representation_status",
        "extension_status_review", "elster_access_review",
        "approved_corporation_tax_due_date",
    ],
    "de.trade_tax.return.calendar": [
        "confirmed_trade_tax_registration", "competent_municipality_review",
        "tax_advisor_representation_status", "extension_and_assessment_notice_review",
        "approved_trade_tax_due_date",
    ],
    "de.vat.advance_return_payment.calendar": [
        "confirmed_vat_registration", "reporting_frequency",
        "startup_and_prior_tax_review", "permanent_extension_status",
        "taxable_period_end", "approved_vat_return_and_payment_due_date",
    ],
    "de.company_register.financial_statements.calendar": [
        "confirmed_financial_year_end", "company_size_class_review",
        "statements_approval_and_audit_status",
        "disclosure_or_deposit_scope_review", "company_register_channel_review",
        "approved_financial_statements_disclosure_due_date",
    ],
    "de.return.evidence": [
        "approved_financial_statements", "e_bilanz_workpaper",
        "corporation_tax_workpaper", "trade_tax_workpaper", "vat_return_workpaper",
        "authorized_declarations", "payment_plan",
        "company_register_submission_plan", "submission_receipt_plan",
    ],
}
IN_SCOPE = {
    "de_limited_liability_company", "gmbh_company", "commercial_register",
    "corporation_tax", "koerperschaftsteuer", "trade_tax", "gewerbesteuer",
}


def _context(context: ServiceContext) -> tuple[Any, dict[str, Any]]:
    if not context.entity_id:
        raise ValueError("Germany tax service requires entity_id")
    entity = context.runtime.entities.get(context.entity_id)
    if (
        entity.jurisdiction != "DE"
        or entity.tax_pack != "jurisdiction.de_limited_liability_company"
    ):
        raise ValueError(
            f"Entity {entity.entity_id} does not use "
            "jurisdiction.de_limited_liability_company"
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
        "german_tax_residency_determined": False,
        "corporation_tax_rate_determined": False,
        "solidarity_surcharge_determined": False,
        "trade_tax_base_determined": False,
        "municipal_trade_tax_rate_determined": False,
        "vat_registration_liability_determined": False,
        "vat_supply_classification_performed": False,
        "oss_or_ioss_scheme_determined": False,
        "payroll_tax_determined": False,
        "dividend_tax_determined": False,
        "tax_calculation_performed": False,
        "filing_performed": False,
        "payment_performed": False,
        "external_submission_enabled": False,
    }


def _registration_review(
    declared: str, evidence: dict[str, str] | None,
) -> str:
    if declared == "confirmed" and evidence:
        return "confirmed"
    if declared == "confirmed":
        return "needs_evidence"
    if declared == "not_registered":
        return "not_registered"
    return "needs_confirmation"


def build_de_registration_profile(
    payload: dict[str, Any], context: ServiceContext,
) -> dict[str, Any]:
    entity, bundle = _context(context)
    forbidden = {
        "commercial_register_number", "handelsregisternummer", "company_number",
        "tax_number", "steuernummer", "tax_id", "vat_number", "ust_idnr",
        "ust_id_nr", "business_identification_number", "wirtschafts_id",
    }
    if forbidden & set(payload):
        raise ValueError(
            "Do not pass a raw Commercial Register number, tax number or VAT ID; "
            "provide an evidence reference"
        )
    registrations = {item.lower() for item in entity.tax_registrations}
    entity_type = str(payload.get("entity_type") or (
        "limited_liability_company_gmbh"
        if registrations & {"de_limited_liability_company", "gmbh_company"}
        else "unknown"
    )).strip().lower()
    allowed_types = {
        "limited_liability_company_gmbh",
        "entrepreneurial_company_ug",
        "stock_corporation_ag",
        "gmbh_and_co_kg",
        "branch",
        "partnership",
        "sole_trader",
        "other",
        "unknown",
    }
    if entity_type not in allowed_types:
        raise ValueError("entity_type is not supported by this Germany GmbH Pack")
    entity_evidence = _ref(payload.get("entity_type_evidence"), "entity_type_evidence")
    residency_evidence = _ref(
        payload.get("tax_residency_evidence"), "tax_residency_evidence",
    )
    if entity_type == "limited_liability_company_gmbh":
        applicability = "in_scope_limited_liability_company_gmbh"
        entity_status = "confirmed" if entity_evidence else "needs_evidence"
    elif entity_type == "unknown":
        applicability, entity_status = "unknown", "needs_confirmation"
    else:
        applicability, entity_status = "outside_pack_scope", "outside_pack_scope"

    fields = {
        "commercial_register": (
            "commercial_register_status", "commercial_register_evidence",
            {"commercial_register", "gmbh_company"},
        ),
        "corporation_tax": (
            "corporation_tax_status", "corporation_tax_evidence",
            {"corporation_tax", "koerperschaftsteuer"},
        ),
        "trade_tax": (
            "trade_tax_status", "trade_tax_evidence",
            {"trade_tax", "gewerbesteuer"},
        ),
        "vat": (
            "vat_status", "vat_evidence", {"vat", "vat_registered", "de_vat"},
        ),
        "payroll": (
            "payroll_status", "payroll_evidence",
            {"payroll", "payroll_registered", "de_payroll"},
        ),
    }
    statuses: dict[str, dict[str, Any]] = {}
    for label, (status_field, evidence_field, codes) in fields.items():
        default = "confirmed" if registrations & codes else "not_registered"
        declared = str(payload.get(status_field) or default).strip().lower()
        if declared not in {"confirmed", "pending", "not_registered", "unknown"}:
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
        blockers.append("German tax residency: needs_evidence")
    for label in ("commercial_register", "corporation_tax", "trade_tax"):
        if statuses[label]["review_status"] != "confirmed":
            blockers.append(f"{label} evidence: {statuses[label]['review_status']}")
    for label in ("vat", "payroll"):
        if statuses[label]["review_status"] in {"needs_evidence", "needs_confirmation"}:
            blockers.append(f"{label} evidence: {statuses[label]['review_status']}")
    if not freshness["current"]:
        blockers.append("official source review has expired")
    sources = {item["id"]: item for item in bundle["rules"]["sources"]}
    return {
        "ready": not blockers,
        "entity_id": entity.entity_id,
        "jurisdiction": "DE",
        "tax_pack": bundle["pack_id"],
        "tax_pack_version": bundle["pack_version"],
        "tax_readiness": entity.tax_readiness,
        "scope": "Germany Gesellschaft mit beschränkter Haftung registration evidence only",
        "applicability": applicability,
        "entity_type": {
            "value": entity_type, "status": entity_status, "evidence": entity_evidence,
        },
        "german_tax_residency": {
            "status": "confirmed" if residency_evidence else "needs_evidence",
            "evidence": residency_evidence,
            "determined_by_system": False,
        },
        "registrations": statuses,
        "raw_company_identifier_collected": False,
        "source_freshness": freshness,
        "official_sources": [
            sources["de_bmwe_gmbh_current"],
            sources["de_bundesportal_corporate_tax_current"],
            sources["de_bundesportal_vat_advance_current"],
            sources["de_hgb_325_current"],
            sources["de_company_register_submit_current"],
        ],
        "blockers": blockers,
        "review_gate": "tax_registration_confirmation",
        "human_review_required": True,
        **_guardrails(),
    }


def build_de_evidence_checklist(
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
    required_all = {item for values in DE_EVIDENCE_BY_RULE.values() for item in values}
    unknown = sorted(set(provided) - required_all)
    source_index = {item["id"]: item for item in bundle["rules"]["sources"]}
    items = []
    for rule in bundle["rules"]["rules"]:
        required = DE_EVIDENCE_BY_RULE[rule["id"]]
        missing = [item for item in required if item not in provided]
        items.append({
            "rule_id": rule["id"],
            "summary": rule["summary"],
            "automation_level": rule["automation_level"],
            "required_evidence": required,
            "provided_evidence": {
                item: provided[item] for item in required if item in provided
            },
            "missing_evidence": missing,
            "complete": not missing,
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
        "ready": not blockers,
        "entity_id": entity.entity_id,
        "tax_pack": bundle["pack_id"],
        "tax_pack_version": bundle["pack_version"],
        "source_freshness": freshness,
        "items": items,
        "duplicate_evidence_ids": duplicates,
        "unknown_evidence_ids": unknown,
        "blockers": blockers,
        "review_gate": "tax_advisor_review",
        "human_review_required": True,
        "scope_note": bundle["rules"].get("scope_note"),
        **_guardrails(),
    }


def build_de_tax_calendar(
    payload: dict[str, Any], context: ServiceContext,
) -> dict[str, Any]:
    entity, bundle = _context(context)
    result = build_tax_calendar(
        context.runtime,
        entity.entity_id,
        period_year=payload.get("period_year"),
        anchors=payload.get("anchors"),
        as_of=payload.get("as_of"),
    )
    freshness = _freshness(bundle, payload.get("as_of"))
    scope = bool({item.lower() for item in entity.tax_registrations} & IN_SCOPE)
    blockers = list(result.get("warnings") or [])
    if not scope:
        blockers.append("Germany GmbH / corporation and trade tax scope is not confirmed")
    if not freshness["current"]:
        blockers.append("official source review has expired")
    return {
        **result,
        "ready": bool(result["ready"] and freshness["current"] and scope),
        "limited_liability_company_gmbh_scope_confirmed": scope,
        "source_freshness": freshness,
        "blockers": sorted(set(blockers)),
        **_guardrails(),
    }
