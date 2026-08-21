from __future__ import annotations

from collections import Counter
from datetime import date
from typing import Any

from .pack_services import ServiceContext
from .tax_calendar import build_tax_calendar
from .tax_pack_lifecycle import source_freshness_from_bundle


KR_EVIDENCE_BY_RULE = {
    "kr.company.domestic.scope.evidence": [
        "corporate_registry_status_reference",
        "legal_form_review",
        "legal_name_and_head_office_match_review",
        "korean_tax_residency_review",
    ],
    "kr.business_registration.evidence": [
        "business_registration_status_reference",
        "business_place_and_lease_review",
        "business_scope_and_permit_review",
        "hometax_access_review",
    ],
    "kr.corporate_income_tax.registration.evidence": [
        "corporate_income_tax_registration_reference",
        "fiscal_year_review",
        "interim_payment_applicability_review",
        "incentive_and_sme_status_review",
    ],
    "kr.local_corporate_income_tax.evidence": [
        "local_corporate_income_tax_registration_reference",
        "local_government_and_business_places_review",
        "local_allocation_review",
        "wetax_access_review",
    ],
    "kr.vat.etax_invoice.evidence": [
        "vat_status_reference",
        "vat_period_review",
        "supply_classification_review",
        "etax_invoice_obligation_review",
        "hometax_invoice_access_review",
    ],
    "kr.withholding.registration.evidence": [
        "withholding_obligation_review",
        "income_type_review",
        "payroll_withholding_review",
        "semiannual_payment_approval_reference",
    ],
    "kr.corporate_income_tax.return_payment.calendar": [
        "confirmed_fiscal_year_end",
        "extension_status_review",
        "interim_payment_applicability_review",
        "hometax_account_due_date_reference",
        "approved_corporate_income_tax_return_and_payment_due_date",
    ],
    "kr.local_corporate_income_tax.return_payment.calendar": [
        "confirmed_fiscal_year_end",
        "confirmed_local_government_and_business_places",
        "local_allocation_review",
        "extension_status_review",
        "wetax_account_due_date_reference",
        "approved_local_corporate_income_tax_return_and_payment_due_date",
    ],
    "kr.vat.return_payment.calendar": [
        "confirmed_vat_taxpayer_status",
        "confirmed_vat_period",
        "preliminary_and_final_return_review",
        "holiday_and_extension_review",
        "hometax_account_due_date_reference",
        "approved_vat_return_and_payment_due_date",
    ],
    "kr.withholding.payment.calendar": [
        "confirmed_withholding_obligation",
        "income_types_and_payment_dates_review",
        "semiannual_payment_approval_review",
        "payment_statement_review",
        "approved_withholding_return_and_payment_due_date",
    ],
    "kr.etax_invoice.issue_transmit.calendar": [
        "confirmed_etax_invoice_obligation",
        "supply_time_and_aggregate_issue_review",
        "holiday_review",
        "hometax_issue_and_transmit_evidence",
        "approved_etax_invoice_issue_due_date",
        "approved_etax_invoice_transmit_due_date",
    ],
    "kr.return.evidence": [
        "approved_financial_statements",
        "corporate_income_tax_workpaper",
        "local_corporate_income_tax_workpaper",
        "vat_workpaper",
        "withholding_tax_workpaper",
        "etax_invoice_control_evidence",
        "authorized_hometax_and_wetax_declarations",
        "payment_plan",
        "submission_receipt_plan",
    ],
}

IN_SCOPE = {
    "kr_domestic_corporation",
    "jusik_hoesa",
    "yuhan_hoesa",
    "yuhan_chaegim_hoesa",
    "corporate_income_tax",
}


def _context(context: ServiceContext) -> tuple[Any, dict[str, Any]]:
    if not context.entity_id:
        raise ValueError("Korea tax service requires entity_id")
    entity = context.runtime.entities.get(context.entity_id)
    if (
        entity.jurisdiction != "KR"
        or entity.tax_pack != "jurisdiction.kr_domestic_corporation"
    ):
        raise ValueError(
            f"Entity {entity.entity_id} does not use "
            "jurisdiction.kr_domestic_corporation"
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
        "korean_tax_residency_determined": False,
        "entity_type_determined_by_system": False,
        "corporate_income_tax_liability_determined": False,
        "corporate_income_tax_rate_or_amount_determined": False,
        "interim_payment_applicability_determined": False,
        "local_tax_authority_or_allocation_determined": False,
        "local_corporate_income_tax_rate_or_amount_determined": False,
        "vat_taxpayer_status_determined": False,
        "vat_supply_classification_performed": False,
        "vat_rate_or_amount_determined": False,
        "etax_invoice_obligation_or_deadline_determined": False,
        "withholding_obligation_or_deadline_determined": False,
        "sme_or_tax_incentive_eligibility_determined": False,
        "foreign_investment_or_group_taxation_determined": False,
        "cross_border_or_permanent_establishment_determined": False,
        "transfer_pricing_or_customs_determined": False,
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
    if declared == "not_registered":
        return "not_registered"
    return "needs_confirmation"


def build_kr_registration_profile(
    payload: dict[str, Any], context: ServiceContext,
) -> dict[str, Any]:
    entity, bundle = _context(context)
    forbidden = {
        "corporate_registration_number",
        "company_registration_number",
        "business_registration_number",
        "taxpayer_id",
        "tax_id",
        "hometax_user_id",
        "wetax_user_id",
        "electronic_certificate",
    }
    if forbidden & set(payload):
        raise ValueError(
            "Do not pass a raw Korean corporate, business, tax or HomeTax/WeTax "
            "identifier; provide an evidence reference"
        )
    registrations = {item.lower() for item in entity.tax_registrations}
    if registrations & {"jusik_hoesa", "stock_company"}:
        default_type = "stock_company_jusik_hoesa"
    elif registrations & {"yuhan_hoesa", "limited_company"}:
        default_type = "limited_company_yuhan_hoesa"
    elif registrations & {"yuhan_chaegim_hoesa", "limited_liability_company"}:
        default_type = "limited_liability_company_yuhan_chaegim_hoesa"
    else:
        default_type = "unknown"
    entity_type = str(payload.get("entity_type") or default_type).strip().lower()
    in_scope_types = {
        "stock_company_jusik_hoesa",
        "limited_company_yuhan_hoesa",
        "limited_liability_company_yuhan_chaegim_hoesa",
    }
    allowed_types = in_scope_types | {
        "general_partnership_hapmyeong_hoesa",
        "limited_partnership_hapja_hoesa",
        "foreign_corporation",
        "branch",
        "sole_proprietor",
        "nonprofit",
        "other",
        "unknown",
    }
    if entity_type not in allowed_types:
        raise ValueError("entity_type is not supported by this Korea corporation Pack")
    entity_evidence = _ref(payload.get("entity_type_evidence"), "entity_type_evidence")
    residency_evidence = _ref(
        payload.get("tax_residency_evidence"), "tax_residency_evidence",
    )
    if entity_type in in_scope_types:
        applicability = "in_scope_korea_domestic_for_profit_corporation"
        entity_status = "confirmed" if entity_evidence else "needs_evidence"
    elif entity_type == "unknown":
        applicability, entity_status = "unknown", "needs_confirmation"
    else:
        applicability, entity_status = "outside_pack_scope", "outside_pack_scope"

    fields = {
        "corporate_registry": (
            "corporate_registry_status", "corporate_registry_evidence",
            {"kr_domestic_corporation", "jusik_hoesa", "yuhan_hoesa", "yuhan_chaegim_hoesa"},
        ),
        "business_registration": (
            "business_registration_status", "business_registration_evidence",
            {"business_registration", "kr_business_registration"},
        ),
        "corporate_income_tax": (
            "corporate_income_tax_status", "corporate_income_tax_evidence",
            {"corporate_income_tax", "kr_corporate_income_tax"},
        ),
        "local_corporate_income_tax": (
            "local_corporate_income_tax_status", "local_corporate_income_tax_evidence",
            {"local_corporate_income_tax"},
        ),
        "vat": (
            "vat_status", "vat_evidence", {"vat", "kr_vat", "vat_taxpayer"},
        ),
        "etax_invoice": (
            "etax_invoice_status", "etax_invoice_evidence",
            {"etax_invoice", "electronic_tax_invoice"},
        ),
        "withholding": (
            "withholding_status", "withholding_evidence",
            {"withholding_tax", "withholding_agent", "payroll_withholding"},
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
        blockers.append("Korean tax residency: needs_evidence")
    for label in (
        "corporate_registry", "business_registration", "corporate_income_tax",
        "local_corporate_income_tax",
    ):
        if statuses[label]["review_status"] != "confirmed":
            blockers.append(f"{label} evidence: {statuses[label]['review_status']}")
    for label in ("vat", "etax_invoice", "withholding"):
        if statuses[label]["review_status"] in {"needs_evidence", "needs_confirmation"}:
            blockers.append(f"{label} evidence: {statuses[label]['review_status']}")
    if not freshness["current"]:
        blockers.append("official source review has expired")
    sources = {item["id"]: item for item in bundle["rules"]["sources"]}
    return {
        "ready": not blockers,
        "entity_id": entity.entity_id,
        "jurisdiction": "KR",
        "tax_pack": bundle["pack_id"],
        "tax_pack_version": bundle["pack_version"],
        "tax_readiness": entity.tax_readiness,
        "scope": "Korea domestic for-profit corporation registration evidence only",
        "applicability": applicability,
        "entity_type": {
            "value": entity_type,
            "status": entity_status,
            "evidence": entity_evidence,
            "determined_by_system": False,
        },
        "korean_tax_residency": {
            "status": "confirmed" if residency_evidence else "needs_evidence",
            "evidence": residency_evidence,
            "determined_by_system": False,
        },
        "registrations": statuses,
        "raw_company_or_tax_identifier_collected": False,
        "source_freshness": freshness,
        "official_sources": [sources[item] for item in (
            "kr_investkorea_company_types_current",
            "kr_nts_business_registration_current",
            "kr_nts_corporate_income_tax_current",
            "kr_nts_filing_returns_current",
            "kr_nts_etax_invoice_current",
            "kr_nts_withholding_current",
            "kr_nts_tax_calendar_current",
            "kr_mois_local_corporate_income_tax_2026",
        )],
        "blockers": blockers,
        "review_gate": "tax_registration_confirmation",
        "human_review_required": True,
        **_guardrails(),
    }


def build_kr_evidence_checklist(
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
    required_all = {item for values in KR_EVIDENCE_BY_RULE.values() for item in values}
    unknown = sorted(set(provided) - required_all)
    source_index = {item["id"]: item for item in bundle["rules"]["sources"]}
    items = []
    for rule in bundle["rules"]["rules"]:
        required = KR_EVIDENCE_BY_RULE[rule["id"]]
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


def build_kr_tax_calendar(
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
        blockers.append("Korea domestic for-profit corporation tax scope is not confirmed")
    if not freshness["current"]:
        blockers.append("official source review has expired")
    return {
        **result,
        "ready": bool(result["ready"] and freshness["current"] and scope),
        "domestic_for_profit_corporation_scope_confirmed": scope,
        "source_freshness": freshness,
        "blockers": sorted(set(blockers)),
        **_guardrails(),
    }
