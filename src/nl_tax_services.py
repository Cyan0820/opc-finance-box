from __future__ import annotations

from collections import Counter
from datetime import date
from typing import Any

from .pack_services import ServiceContext
from .tax_calendar import build_tax_calendar
from .tax_pack_lifecycle import source_freshness_from_bundle


NL_EVIDENCE_BY_RULE = {
    "nl.company.bv.scope.evidence": [
        "kvk_company_status_reference", "bv_legal_form_review",
        "legal_name_match_review", "dutch_tax_residency_review",
    ],
    "nl.vpb.registration.evidence": [
        "corporate_income_tax_registration_reference", "fiscal_year_review",
        "vpb_filing_access_review", "fiscal_unity_and_innovation_box_review",
    ],
    "nl.vat.registration.evidence": [
        "vat_registration_reference", "establishment_status_review",
        "reporting_frequency_review", "eu_transactions_and_oss_kor_review",
    ],
    "nl.vpb.return.calendar": [
        "confirmed_fiscal_year_type", "tax_return_invitation_reference",
        "extension_status_review", "approved_vpb_due_date",
    ],
    "nl.vat.return_payment.calendar": [
        "confirmed_vat_registration", "reporting_frequency",
        "taxable_period_end", "tax_account_due_date_reference",
        "approved_vat_return_and_payment_due_date",
    ],
    "nl.kvk.financial_statements.calendar": [
        "approved_financial_statements", "statements_adoption_status_and_date",
        "eight_day_deadline_review", "twelve_month_cap_review",
        "provisional_statements_review", "sbr_filing_channel_review",
        "approved_kvk_filing_due_date",
    ],
    "nl.return.evidence": [
        "approved_financial_statements", "vpb_tax_adjustment_workpaper",
        "vat_return_workpaper", "authorized_declarations",
        "payment_plan", "kvk_sbr_plan", "submission_receipt_plan",
    ],
}
IN_SCOPE = {
    "nl_private_limited_company", "bv_company", "kvk_registered",
    "corporate_income_tax", "vpb_filer",
}


def _context(context: ServiceContext) -> tuple[Any, dict[str, Any]]:
    if not context.entity_id:
        raise ValueError("Netherlands tax service requires entity_id")
    entity = context.runtime.entities.get(context.entity_id)
    if (
        entity.jurisdiction != "NL"
        or entity.tax_pack != "jurisdiction.nl_private_limited_company"
    ):
        raise ValueError(
            f"Entity {entity.entity_id} does not use "
            "jurisdiction.nl_private_limited_company"
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
        "dutch_tax_residency_determined": False,
        "corporate_income_tax_rate_determined": False,
        "fiscal_unity_determined": False,
        "innovation_box_eligibility_determined": False,
        "vat_registration_liability_determined": False,
        "vat_supply_classification_performed": False,
        "oss_or_ioss_scheme_determined": False,
        "kor_eligibility_determined": False,
        "payroll_or_dga_salary_determined": False,
        "dividend_withholding_determined": False,
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


def build_nl_registration_profile(
    payload: dict[str, Any], context: ServiceContext,
) -> dict[str, Any]:
    entity, bundle = _context(context)
    forbidden = {
        "kvk_number", "company_number", "rsin", "tax_number", "tax_id",
        "vat_number", "btw_number", "btw_id",
    }
    if forbidden & set(payload):
        raise ValueError(
            "Do not pass a raw KVK number, RSIN, tax number or VAT number; "
            "provide an evidence reference"
        )
    registrations = {item.lower() for item in entity.tax_registrations}
    entity_type = str(payload.get("entity_type") or (
        "private_limited_company_bv"
        if registrations & {"nl_private_limited_company", "bv_company"}
        else "unknown"
    )).strip().lower()
    allowed_types = {
        "private_limited_company_bv", "public_limited_company_nv",
        "foundation", "association", "cooperative", "branch", "other", "unknown",
    }
    if entity_type not in allowed_types:
        raise ValueError("entity_type is not supported by this Netherlands BV Pack")
    entity_evidence = _ref(payload.get("entity_type_evidence"), "entity_type_evidence")
    residency_evidence = _ref(
        payload.get("tax_residency_evidence"), "tax_residency_evidence",
    )
    if entity_type == "private_limited_company_bv":
        applicability = "in_scope_private_limited_company_bv"
        entity_status = "confirmed" if entity_evidence else "needs_evidence"
    elif entity_type == "unknown":
        applicability, entity_status = "unknown", "needs_confirmation"
    else:
        applicability, entity_status = "outside_pack_scope", "outside_pack_scope"

    fields = {
        "kvk": ("kvk_status", "kvk_evidence", {"kvk_registered"}),
        "corporate_income_tax": (
            "corporate_income_tax_status", "corporate_income_tax_evidence",
            {"corporate_income_tax", "vpb_filer"},
        ),
        "vat": ("vat_status", "vat_evidence", {"vat", "vat_registered", "nl_vat"}),
        "payroll": (
            "payroll_status", "payroll_evidence",
            {"payroll", "payroll_registered", "nl_payroll"},
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
            "evidence": evidence, "raw_identifier_collected": False,
        }

    freshness = _freshness(bundle, payload.get("as_of"))
    blockers = []
    if entity_status != "confirmed":
        blockers.append(f"entity type: {entity_status}")
    if not residency_evidence:
        blockers.append("Dutch tax residency: needs_evidence")
    for label in ("kvk", "corporate_income_tax"):
        if statuses[label]["review_status"] != "confirmed":
            blockers.append(f"{label} evidence: {statuses[label]['review_status']}")
    for label in ("vat", "payroll"):
        if statuses[label]["review_status"] in {"needs_evidence", "needs_confirmation"}:
            blockers.append(f"{label} evidence: {statuses[label]['review_status']}")
    if not freshness["current"]:
        blockers.append("official source review has expired")
    sources = {item["id"]: item for item in bundle["rules"]["sources"]}
    return {
        "ready": not blockers, "entity_id": entity.entity_id, "jurisdiction": "NL",
        "tax_pack": bundle["pack_id"], "tax_pack_version": bundle["pack_version"],
        "tax_readiness": entity.tax_readiness,
        "scope": "Netherlands besloten vennootschap registration evidence only",
        "applicability": applicability,
        "entity_type": {"value": entity_type, "status": entity_status, "evidence": entity_evidence},
        "dutch_tax_residency": {
            "status": "confirmed" if residency_evidence else "needs_evidence",
            "evidence": residency_evidence, "determined_by_system": False,
        },
        "registrations": statuses, "raw_company_identifier_collected": False,
        "source_freshness": freshness,
        "official_sources": [
            sources["nl_business_gov_bv_current"],
            sources["nl_business_gov_vpb_current"],
            sources["nl_belastingdienst_vat_return_current"],
            sources["nl_business_gov_financial_statements_current"],
        ],
        "blockers": blockers, "review_gate": "tax_registration_confirmation",
        "human_review_required": True, **_guardrails(),
    }


def build_nl_evidence_checklist(
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
    required_all = {item for values in NL_EVIDENCE_BY_RULE.values() for item in values}
    unknown = sorted(set(provided) - required_all)
    source_index = {item["id"]: item for item in bundle["rules"]["sources"]}
    items = []
    for rule in bundle["rules"]["rules"]:
        required = NL_EVIDENCE_BY_RULE[rule["id"]]
        missing = [item for item in required if item not in provided]
        items.append({
            "rule_id": rule["id"], "summary": rule["summary"],
            "automation_level": rule["automation_level"],
            "required_evidence": required,
            "provided_evidence": {item: provided[item] for item in required if item in provided},
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
        "human_review_required": True, "scope_note": bundle["rules"].get("scope_note"),
        **_guardrails(),
    }


def build_nl_tax_calendar(
    payload: dict[str, Any], context: ServiceContext,
) -> dict[str, Any]:
    entity, bundle = _context(context)
    result = build_tax_calendar(
        context.runtime, entity.entity_id, period_year=payload.get("period_year"),
        anchors=payload.get("anchors"), as_of=payload.get("as_of"),
    )
    freshness = _freshness(bundle, payload.get("as_of"))
    scope = bool({item.lower() for item in entity.tax_registrations} & IN_SCOPE)
    blockers = list(result.get("warnings") or [])
    if not scope:
        blockers.append("Netherlands BV / VPB scope is not confirmed")
    if not freshness["current"]:
        blockers.append("official source review has expired")
    return {
        **result,
        "ready": bool(result["ready"] and freshness["current"] and scope),
        "private_limited_company_bv_scope_confirmed": scope,
        "source_freshness": freshness, "blockers": sorted(set(blockers)),
        **_guardrails(),
    }
