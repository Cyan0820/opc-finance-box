from __future__ import annotations

from collections import Counter
from datetime import date
from typing import Any

from .pack_services import ServiceContext
from .tax_calendar import build_tax_calendar
from .tax_pack_lifecycle import source_freshness_from_bundle


JP_EVIDENCE_BY_RULE = {
    "jp.company.domestic.scope.evidence": [
        "commercial_registry_status_reference",
        "legal_form_review",
        "legal_name_and_head_office_match_review",
        "japanese_tax_residency_review",
    ],
    "jp.corporation_tax.registration.evidence": [
        "corporation_establishment_notification_reference",
        "corporation_tax_registration_reference",
        "fiscal_year_review",
        "blue_return_and_group_taxation_review",
        "etax_access_review",
    ],
    "jp.local_corporate_tax.registration.evidence": [
        "prefecture_and_municipality_review",
        "local_office_and_establishment_review",
        "local_corporate_tax_registration_reference",
        "eltax_or_local_channel_review",
    ],
    "jp.consumption_tax.invoice.evidence": [
        "consumption_tax_status_reference",
        "taxable_period_review",
        "base_and_specified_period_review",
        "new_corporation_and_simplified_tax_review",
        "qualified_invoice_issuer_status_reference",
    ],
    "jp.withholding.registration.evidence": [
        "payroll_office_status_reference",
        "withholding_obligation_review",
        "payment_type_review",
        "special_payment_deadline_approval_reference",
    ],
    "jp.corporation_tax.return_payment.calendar": [
        "confirmed_fiscal_year_end",
        "extension_status_review",
        "interim_return_applicability_review",
        "defense_special_corporation_tax_review",
        "etax_access_review",
        "approved_corporation_tax_return_and_payment_due_date",
    ],
    "jp.local_corporate_tax.return_payment.calendar": [
        "confirmed_prefecture_and_municipality",
        "local_office_and_establishment_review",
        "local_tax_registration_review",
        "extension_status_review",
        "eltax_or_local_channel_review",
        "approved_local_corporate_tax_return_and_payment_due_date",
    ],
    "jp.consumption_tax.return_payment.calendar": [
        "confirmed_consumption_taxable_person_status",
        "confirmed_taxable_period",
        "shortened_period_and_interim_return_review",
        "simplified_tax_system_review",
        "etax_account_due_date_reference",
        "approved_consumption_tax_return_and_payment_due_date",
    ],
    "jp.withholding_tax.payment.calendar": [
        "confirmed_withholding_obligation",
        "payment_types_and_dates_review",
        "special_payment_deadline_approval_review",
        "withholding_payment_statement_review",
        "approved_withholding_payment_due_date",
    ],
    "jp.return.evidence": [
        "approved_financial_statements",
        "corporation_tax_workpaper",
        "local_corporate_tax_workpaper",
        "consumption_tax_workpaper",
        "withholding_tax_workpaper",
        "authorized_etax_and_eltax_declarations",
        "payment_plan",
        "submission_receipt_plan",
    ],
}

IN_SCOPE = {
    "jp_domestic_corporation",
    "kabushiki_kaisha",
    "godo_kaisha",
    "corporation_tax",
}


def _context(context: ServiceContext) -> tuple[Any, dict[str, Any]]:
    if not context.entity_id:
        raise ValueError("Japan tax service requires entity_id")
    entity = context.runtime.entities.get(context.entity_id)
    if (
        entity.jurisdiction != "JP"
        or entity.tax_pack != "jurisdiction.jp_domestic_corporation"
    ):
        raise ValueError(
            f"Entity {entity.entity_id} does not use "
            "jurisdiction.jp_domestic_corporation"
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
        "japanese_tax_residency_determined": False,
        "entity_type_determined_by_system": False,
        "corporation_tax_liability_determined": False,
        "corporation_tax_rate_or_amount_determined": False,
        "defense_special_corporation_tax_determined": False,
        "local_tax_authority_determined": False,
        "local_corporate_tax_rate_or_amount_determined": False,
        "blue_return_eligibility_determined": False,
        "group_taxation_determined": False,
        "consumption_taxable_person_status_determined": False,
        "consumption_tax_supply_classification_performed": False,
        "simplified_tax_system_eligibility_determined": False,
        "qualified_invoice_issuer_status_determined": False,
        "withholding_obligation_determined": False,
        "withholding_special_deadline_determined": False,
        "cross_border_or_permanent_establishment_determined": False,
        "transfer_pricing_or_customs_determined": False,
        "payroll_or_social_insurance_determined": False,
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


def build_jp_registration_profile(
    payload: dict[str, Any], context: ServiceContext,
) -> dict[str, Any]:
    entity, bundle = _context(context)
    forbidden = {
        "corporate_number",
        "houjin_bangou",
        "company_number",
        "tax_number",
        "tax_id",
        "qualified_invoice_issuer_number",
        "invoice_registration_number",
        "etax_user_identification_number",
        "eltax_user_id",
    }
    if forbidden & set(payload):
        raise ValueError(
            "Do not pass a raw Japanese corporate, tax, invoice or e-Tax/eLTAX "
            "identifier; provide an evidence reference"
        )
    registrations = {item.lower() for item in entity.tax_registrations}
    if registrations & {"kabushiki_kaisha", "stock_company_kk"}:
        default_type = "stock_company_kabushiki_kaisha"
    elif registrations & {"godo_kaisha", "limited_liability_company_gk"}:
        default_type = "limited_liability_company_godo_kaisha"
    elif "jp_domestic_corporation" in registrations:
        default_type = "unknown"
    else:
        default_type = "unknown"
    entity_type = str(payload.get("entity_type") or default_type).strip().lower()
    allowed_types = {
        "stock_company_kabushiki_kaisha",
        "limited_liability_company_godo_kaisha",
        "general_partnership_gomei_kaisha",
        "limited_partnership_goshi_kaisha",
        "foreign_corporation",
        "branch",
        "sole_proprietor",
        "other",
        "unknown",
    }
    if entity_type not in allowed_types:
        raise ValueError("entity_type is not supported by this Japan corporation Pack")
    entity_evidence = _ref(payload.get("entity_type_evidence"), "entity_type_evidence")
    residency_evidence = _ref(
        payload.get("tax_residency_evidence"), "tax_residency_evidence",
    )
    if entity_type in {
        "stock_company_kabushiki_kaisha",
        "limited_liability_company_godo_kaisha",
    }:
        applicability = "in_scope_japan_domestic_kk_or_gk"
        entity_status = "confirmed" if entity_evidence else "needs_evidence"
    elif entity_type == "unknown":
        applicability, entity_status = "unknown", "needs_confirmation"
    else:
        applicability, entity_status = "outside_pack_scope", "outside_pack_scope"

    fields = {
        "commercial_registry": (
            "commercial_registry_status",
            "commercial_registry_evidence",
            {"jp_domestic_corporation", "kabushiki_kaisha", "godo_kaisha"},
        ),
        "corporation_tax": (
            "corporation_tax_status",
            "corporation_tax_evidence",
            {"corporation_tax", "jp_corporation_tax"},
        ),
        "local_corporate_tax": (
            "local_corporate_tax_status",
            "local_corporate_tax_evidence",
            {"local_corporate_tax", "corporate_inhabitant_tax", "corporate_enterprise_tax"},
        ),
        "consumption_tax": (
            "consumption_tax_status",
            "consumption_tax_evidence",
            {"consumption_tax", "consumption_taxable_person"},
        ),
        "qualified_invoice_issuer": (
            "qualified_invoice_issuer_status",
            "qualified_invoice_issuer_evidence",
            {"qualified_invoice_issuer"},
        ),
        "withholding": (
            "withholding_status",
            "withholding_evidence",
            {"withholding_tax", "withholding_agent", "payroll_office"},
        ),
        "blue_return": (
            "blue_return_status",
            "blue_return_evidence",
            {"blue_return_approved"},
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
        blockers.append("Japanese tax residency: needs_evidence")
    for label in ("commercial_registry", "corporation_tax", "local_corporate_tax"):
        if statuses[label]["review_status"] != "confirmed":
            blockers.append(f"{label} evidence: {statuses[label]['review_status']}")
    for label in (
        "consumption_tax", "qualified_invoice_issuer", "withholding", "blue_return",
    ):
        if statuses[label]["review_status"] in {"needs_evidence", "needs_confirmation"}:
            blockers.append(f"{label} evidence: {statuses[label]['review_status']}")
    if not freshness["current"]:
        blockers.append("official source review has expired")
    sources = {item["id"]: item for item in bundle["rules"]["sources"]}
    return {
        "ready": not blockers,
        "entity_id": entity.entity_id,
        "jurisdiction": "JP",
        "tax_pack": bundle["pack_id"],
        "tax_pack_version": bundle["pack_version"],
        "tax_readiness": entity.tax_readiness,
        "scope": "Japan domestic Kabushiki Kaisha / Godo Kaisha registration evidence only",
        "applicability": applicability,
        "entity_type": {
            "value": entity_type,
            "status": entity_status,
            "evidence": entity_evidence,
            "determined_by_system": False,
        },
        "japanese_tax_residency": {
            "status": "confirmed" if residency_evidence else "needs_evidence",
            "evidence": residency_evidence,
            "determined_by_system": False,
        },
        "registrations": statuses,
        "raw_company_or_tax_identifier_collected": False,
        "source_freshness": freshness,
        "official_sources": [sources[item] for item in (
            "jp_moj_commercial_registration_current",
            "jp_nta_corporate_number_current",
            "jp_nta_corporation_return_current",
            "jp_nta_corporation_startup_current",
            "jp_nta_consumption_tax_current",
            "jp_nta_invoice_system_current",
            "jp_nta_withholding_current",
            "jp_etax_local_forms_current",
        )],
        "blockers": blockers,
        "review_gate": "tax_registration_confirmation",
        "human_review_required": True,
        **_guardrails(),
    }


def build_jp_evidence_checklist(
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
    required_all = {item for values in JP_EVIDENCE_BY_RULE.values() for item in values}
    unknown = sorted(set(provided) - required_all)
    source_index = {item["id"]: item for item in bundle["rules"]["sources"]}
    items = []
    for rule in bundle["rules"]["rules"]:
        required = JP_EVIDENCE_BY_RULE[rule["id"]]
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


def build_jp_tax_calendar(
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
        blockers.append("Japan domestic KK/GK corporation tax scope is not confirmed")
    if not freshness["current"]:
        blockers.append("official source review has expired")
    return {
        **result,
        "ready": bool(result["ready"] and freshness["current"] and scope),
        "domestic_kk_or_gk_scope_confirmed": scope,
        "source_freshness": freshness,
        "blockers": sorted(set(blockers)),
        **_guardrails(),
    }
