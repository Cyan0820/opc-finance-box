from __future__ import annotations

from collections import Counter
from datetime import date
from typing import Any

from .pack_services import ServiceContext
from .tax_calendar import build_tax_calendar
from .tax_pack_lifecycle import source_freshness_from_bundle


FR_EVIDENCE_BY_RULE = {
    "fr.company.sasu.scope.evidence": [
        "rne_company_status_reference", "sasu_single_shareholder_form_review",
        "legal_name_match_review", "french_tax_residency_review",
    ],
    "fr.profit_tax.regime.evidence": [
        "profit_tax_regime_reference", "reporting_regime_review",
        "financial_year_review", "efi_or_edi_access_review",
    ],
    "fr.vat.registration.evidence": [
        "vat_registration_reference", "vat_regime_review",
        "reporting_frequency_review", "transactions_and_oss_ioss_review",
    ],
    "fr.annual_accounts.evidence": [
        "approved_annual_accounts", "single_shareholder_approval_decision",
        "company_size_and_confidentiality_review", "audit_status_review",
        "filing_channel_review",
    ],
    "fr.profit_tax.return.calendar": [
        "confirmed_profit_tax_regime", "confirmed_reporting_regime",
        "confirmed_financial_year_end", "first_period_and_no_close_review",
        "efi_or_edi_channel_review", "approved_profit_tax_return_due_date",
    ],
    "fr.corporate_income_tax.payment.calendar": [
        "confirmed_corporate_income_tax_is_regime", "confirmed_financial_year_end",
        "first_period_and_prior_tax_review", "installment_exemption_review",
        "professional_account_due_dates_reference", "approved_is_payment_schedule",
    ],
    "fr.vat.return_payment.calendar": [
        "confirmed_vat_registration_and_regime", "reporting_frequency",
        "first_period_and_prior_vat_review", "taxable_period_end",
        "professional_account_due_date_reference",
        "approved_vat_return_and_payment_due_date",
    ],
    "fr.annual_accounts.filing.calendar": [
        "confirmed_financial_year_end", "single_shareholder_approval_date",
        "filing_channel_review", "company_size_confidentiality_and_audit_review",
        "required_documents_review", "approved_annual_accounts_filing_due_date",
    ],
    "fr.return.evidence": [
        "approved_annual_accounts", "profit_tax_return_workpaper",
        "vat_return_workpaper", "profit_tax_regime_review",
        "authorized_declarations", "payment_plan",
        "guichet_submission_plan", "submission_receipt_plan",
    ],
}
IN_SCOPE = {
    "fr_single_member_simplified_joint_stock_company", "sasu_company",
    "rne_registered", "corporate_income_tax_is", "income_tax_ir_option",
}


def _context(context: ServiceContext) -> tuple[Any, dict[str, Any]]:
    if not context.entity_id:
        raise ValueError("France tax service requires entity_id")
    entity = context.runtime.entities.get(context.entity_id)
    if (
        entity.jurisdiction != "FR"
        or entity.tax_pack
        != "jurisdiction.fr_single_member_simplified_joint_stock_company"
    ):
        raise ValueError(
            f"Entity {entity.entity_id} does not use "
            "jurisdiction.fr_single_member_simplified_joint_stock_company"
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
        "french_tax_residency_determined": False,
        "profit_tax_regime_determined": False,
        "corporate_income_tax_rate_determined": False,
        "small_company_rate_eligibility_determined": False,
        "corporate_income_tax_installments_determined": False,
        "vat_registration_liability_determined": False,
        "vat_regime_determined": False,
        "vat_supply_classification_performed": False,
        "oss_or_ioss_scheme_determined": False,
        "cfe_or_cvae_liability_determined": False,
        "payroll_or_social_contributions_determined": False,
        "dividend_or_personal_tax_determined": False,
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


def build_fr_registration_profile(
    payload: dict[str, Any], context: ServiceContext,
) -> dict[str, Any]:
    entity, bundle = _context(context)
    forbidden = {
        "siren", "siret", "rne_number", "company_number", "tax_number",
        "numero_fiscal", "vat_number", "tva_number", "fr_vat_number",
        "intra_community_vat_number",
    }
    if forbidden & set(payload):
        raise ValueError(
            "Do not pass a raw SIREN, SIRET, tax number or VAT number; "
            "provide an evidence reference"
        )
    registrations = {item.lower() for item in entity.tax_registrations}
    entity_type = str(payload.get("entity_type") or (
        "single_member_simplified_joint_stock_company_sasu"
        if registrations & {
            "fr_single_member_simplified_joint_stock_company", "sasu_company",
        }
        else "unknown"
    )).strip().lower()
    allowed_types = {
        "single_member_simplified_joint_stock_company_sasu",
        "simplified_joint_stock_company_sas",
        "single_member_limited_liability_company_eurl",
        "limited_liability_company_sarl",
        "public_limited_company_sa",
        "branch",
        "sole_trader",
        "other",
        "unknown",
    }
    if entity_type not in allowed_types:
        raise ValueError("entity_type is not supported by this France SASU Pack")
    entity_evidence = _ref(payload.get("entity_type_evidence"), "entity_type_evidence")
    residency_evidence = _ref(
        payload.get("tax_residency_evidence"), "tax_residency_evidence",
    )
    if entity_type == "single_member_simplified_joint_stock_company_sasu":
        applicability = "in_scope_single_member_simplified_joint_stock_company_sasu"
        entity_status = "confirmed" if entity_evidence else "needs_evidence"
    elif entity_type == "unknown":
        applicability, entity_status = "unknown", "needs_confirmation"
    else:
        applicability, entity_status = "outside_pack_scope", "outside_pack_scope"

    default_regime = (
        "corporate_income_tax_is"
        if registrations & {"corporate_income_tax_is", "fr_corporate_income_tax"}
        else "income_tax_ir_option"
        if "income_tax_ir_option" in registrations
        else "unknown"
    )
    profit_tax_regime = str(
        payload.get("profit_tax_regime") or default_regime
    ).strip().lower()
    if profit_tax_regime not in {
        "corporate_income_tax_is", "income_tax_ir_option", "unknown",
    }:
        raise ValueError("profit_tax_regime is not supported by this France SASU Pack")
    regime_evidence = _ref(
        payload.get("profit_tax_regime_evidence"), "profit_tax_regime_evidence",
    )
    regime_status = (
        "confirmed" if profit_tax_regime != "unknown" and regime_evidence
        else "needs_evidence" if profit_tax_regime != "unknown"
        else "needs_confirmation"
    )

    fields = {
        "rne": (
            "rne_status", "rne_evidence", {"rne_registered", "sasu_company"},
        ),
        "corporate_income_tax": (
            "corporate_income_tax_status", "corporate_income_tax_evidence",
            {"corporate_income_tax_is", "fr_corporate_income_tax"},
        ),
        "vat": (
            "vat_status", "vat_evidence", {"vat", "vat_registered", "fr_vat"},
        ),
        "payroll": (
            "payroll_status", "payroll_evidence",
            {"payroll", "payroll_registered", "fr_payroll"},
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
        blockers.append("French tax residency: needs_evidence")
    if statuses["rne"]["review_status"] != "confirmed":
        blockers.append(f"rne evidence: {statuses['rne']['review_status']}")
    if regime_status != "confirmed":
        blockers.append(f"profit tax regime: {regime_status}")
    if (
        profit_tax_regime == "corporate_income_tax_is"
        and statuses["corporate_income_tax"]["review_status"] != "confirmed"
    ):
        blockers.append(
            "corporate_income_tax evidence: "
            f"{statuses['corporate_income_tax']['review_status']}"
        )
    for label in ("vat", "payroll"):
        if statuses[label]["review_status"] in {"needs_evidence", "needs_confirmation"}:
            blockers.append(f"{label} evidence: {statuses[label]['review_status']}")
    if not freshness["current"]:
        blockers.append("official source review has expired")
    sources = {item["id"]: item for item in bundle["rules"]["sources"]}
    return {
        "ready": not blockers,
        "entity_id": entity.entity_id,
        "jurisdiction": "FR",
        "tax_pack": bundle["pack_id"],
        "tax_pack_version": bundle["pack_version"],
        "tax_readiness": entity.tax_readiness,
        "scope": "France société par actions simplifiée unipersonnelle registration evidence only",
        "applicability": applicability,
        "entity_type": {
            "value": entity_type, "status": entity_status, "evidence": entity_evidence,
        },
        "french_tax_residency": {
            "status": "confirmed" if residency_evidence else "needs_evidence",
            "evidence": residency_evidence,
            "determined_by_system": False,
        },
        "profit_tax_regime": {
            "value": profit_tax_regime,
            "status": regime_status,
            "evidence": regime_evidence,
            "determined_by_system": False,
        },
        "registrations": statuses,
        "raw_company_identifier_collected": False,
        "source_freshness": freshness,
        "official_sources": [
            sources["fr_service_public_sasu_current"],
            sources["fr_service_public_sasu_tax_current"],
            sources["fr_service_public_annual_accounts_current"],
        ],
        "blockers": blockers,
        "review_gate": "tax_registration_confirmation",
        "human_review_required": True,
        **_guardrails(),
    }


def build_fr_evidence_checklist(
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
    required_all = {item for values in FR_EVIDENCE_BY_RULE.values() for item in values}
    unknown = sorted(set(provided) - required_all)
    source_index = {item["id"]: item for item in bundle["rules"]["sources"]}
    items = []
    for rule in bundle["rules"]["rules"]:
        required = FR_EVIDENCE_BY_RULE[rule["id"]]
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


def build_fr_tax_calendar(
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
        blockers.append("France SASU scope is not confirmed")
    if not freshness["current"]:
        blockers.append("official source review has expired")
    return {
        **result,
        "ready": bool(result["ready"] and freshness["current"] and scope),
        "single_member_simplified_joint_stock_company_sasu_scope_confirmed": scope,
        "source_freshness": freshness,
        "blockers": sorted(set(blockers)),
        **_guardrails(),
    }
