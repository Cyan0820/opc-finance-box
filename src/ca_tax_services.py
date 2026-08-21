from __future__ import annotations

from collections import Counter
from datetime import date
from typing import Any

from .pack_services import ServiceContext
from .tax_calendar import build_tax_calendar
from .tax_pack_lifecycle import source_freshness_from_bundle


CA_EVIDENCE_BY_RULE = {
    "ca.company.federal.scope.evidence": [
        "corporations_canada_status_reference",
        "federal_corporation_type_review",
        "legal_name_match_review",
        "canadian_tax_residency_review",
    ],
    "ca.tax.bn_t2.registration.evidence": [
        "business_number_confirmation_reference",
        "corporation_income_tax_account_reference",
        "t2_filing_scope_review",
        "legal_name_registration_match",
    ],
    "ca.gst_hst.registration.monitor": [
        "single_calendar_quarter_revenue_workpaper",
        "four_consecutive_calendar_quarters_workpaper",
        "taxable_exempt_supply_classification_review",
        "canada_supply_connection_review",
        "gst_hst_registration_decision",
    ],
    "ca.t2.return.calendar": [
        "confirmed_tax_year_end",
        "t2_filing_requirement_review",
        "approved_t2_due_date",
        "weekend_public_holiday_review",
    ],
    "ca.corporation_tax.balance.calendar": [
        "ccpc_status_evidence",
        "small_business_deduction_claim_review",
        "prior_taxable_income_and_business_limit_review",
        "associated_corporation_review",
        "approved_balance_due_date",
    ],
    "ca.gst_hst.return_payment.calendar": [
        "gst_hst_registration_evidence",
        "reporting_period_frequency_evidence",
        "cra_account_due_date_reference",
        "approved_return_due_date",
        "approved_payment_due_date",
    ],
    "ca.corporations_canada.annual_return.calendar": [
        "active_federal_corporation_status_reference",
        "anniversary_date_evidence",
        "annual_return_window_review",
        "isc_information_review",
        "approved_annual_return_due_date",
    ],
    "ca.t2.return.evidence": [
        "approved_financial_statements",
        "gifi_and_tax_adjustment_workpaper",
        "applicable_schedule_review",
        "authorized_declaration",
        "payment_plan",
        "submission_receipt_plan",
    ],
}
ENTITY_TYPES = {
    "federal_corporation", "provincial_corporation", "foreign_corporation",
    "sole_proprietorship", "partnership", "trust", "other", "unknown",
}
REGISTRATION_STATUSES = {"confirmed", "pending", "not_registered", "unknown"}
IN_SCOPE_REGISTRATIONS = {
    "ca_federal_corporation", "cbca_corporation", "t2_filer",
    "corporation_income_tax",
}


def _ca_context(context: ServiceContext) -> tuple[Any, dict[str, Any]]:
    if not context.entity_id:
        raise ValueError("Canada tax service requires entity_id")
    entity = context.runtime.entities.get(context.entity_id)
    if (
        entity.jurisdiction != "CA"
        or entity.tax_pack != "jurisdiction.ca_federal_corporation"
    ):
        raise ValueError(
            f"Entity {entity.entity_id} does not use "
            "jurisdiction.ca_federal_corporation"
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


def build_ca_registration_profile(
    payload: dict[str, Any], context: ServiceContext,
) -> dict[str, Any]:
    entity, bundle = _ca_context(context)
    forbidden = (
        "corporation_number", "company_number", "business_number", "bn",
        "tax_id", "tax_identifier", "program_account", "program_account_number",
        "gst_hst_number", "payroll_account_number",
    )
    if any(field in payload for field in forbidden):
        raise ValueError(
            "Do not pass a raw corporation number, BN or CRA program account; "
            "provide an evidence reference"
        )
    registrations = {item.strip().lower() for item in entity.tax_registrations}
    entity_type = str(payload.get("entity_type") or (
        "federal_corporation"
        if registrations & {"ca_federal_corporation", "cbca_corporation"}
        else "unknown"
    )).strip().lower()
    if entity_type not in ENTITY_TYPES:
        raise ValueError(
            "entity_type is not supported by this Canada federal-corporation Pack"
        )
    entity_evidence = _evidence_reference(
        payload.get("entity_type_evidence"), "entity_type_evidence",
    )
    residency_evidence = _evidence_reference(
        payload.get("tax_residency_evidence"), "tax_residency_evidence",
    )
    if entity_type == "federal_corporation":
        applicability = "in_scope_federal_corporation"
        entity_status = "confirmed" if entity_evidence else "needs_evidence"
    elif entity_type == "unknown":
        applicability = "unknown"
        entity_status = "needs_confirmation"
    else:
        applicability = "outside_pack_scope"
        entity_status = "outside_pack_scope"
    residency_status = "confirmed" if residency_evidence else "needs_evidence"

    statuses: dict[str, dict[str, Any]] = {}
    registration_fields = {
        "business_number": (
            "business_number_status", "business_number_evidence",
            {"business_number_confirmed", "bn_confirmed"},
        ),
        "corporation_income_tax": (
            "corporation_income_tax_status", "corporation_income_tax_evidence",
            {"corporation_income_tax", "t2_filer"},
        ),
        "gst_hst": (
            "gst_hst_status", "gst_hst_evidence",
            {"gst_hst", "gst_hst_registered", "ca_gst_hst"},
        ),
        "payroll": (
            "payroll_status", "payroll_evidence",
            {"payroll", "payroll_registered", "ca_payroll"},
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

    ccpc_evidence = _evidence_reference(
        payload.get("ccpc_status_evidence"), "ccpc_status_evidence",
    )
    freshness = _freshness(bundle, payload.get("as_of"))
    blockers = []
    if entity_status != "confirmed":
        blockers.append(f"entity type: {entity_status}")
    if residency_status != "confirmed":
        blockers.append(f"Canadian tax residency: {residency_status}")
    for label in ("business_number", "corporation_income_tax"):
        if statuses[label]["review_status"] != "confirmed":
            blockers.append(
                f"{label} evidence: {statuses[label]['review_status']}"
            )
    for label in ("gst_hst", "payroll"):
        if statuses[label]["review_status"] in {
            "needs_evidence", "needs_confirmation",
        }:
            blockers.append(
                f"{label} evidence: {statuses[label]['review_status']}"
            )
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
        "scope": "Canada federal corporation registration evidence only",
        "applicability": applicability,
        "entity_type": {
            "value": entity_type,
            "status": entity_status,
            "evidence": entity_evidence,
        },
        "canadian_tax_residency": {
            "status": residency_status,
            "evidence": residency_evidence,
            "determined_by_system": False,
        },
        "registrations": statuses,
        "ccpc_status": {
            "evidence": ccpc_evidence,
            "determined_by_system": False,
        },
        "raw_company_identifier_collected": False,
        "source_freshness": freshness,
        "official_sources": [
            source_index["ca_corporations_canada_annual_return_2026"],
            source_index["ca_cra_t2_scope_2026"],
            source_index["ca_cra_gst_hst_registration_2026"],
        ],
        "blockers": blockers,
        "review_gate": "tax_registration_confirmation",
        "human_review_required": True,
        "canadian_tax_residency_determined": False,
        "ccpc_status_determined": False,
        "small_business_deduction_eligibility_determined": False,
        "gst_hst_registration_liability_determined": False,
        "gst_hst_supply_classification_performed": False,
        "corporation_tax_rate_determined": False,
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


def build_ca_evidence_checklist(
    payload: dict[str, Any], context: ServiceContext,
) -> dict[str, Any]:
    entity, bundle = _ca_context(context)
    provided, duplicates = _provided_evidence(payload)
    required_ids = {
        evidence_id for values in CA_EVIDENCE_BY_RULE.values()
        for evidence_id in values
    }
    unknown = sorted(set(provided) - required_ids)
    source_index = {source["id"]: source for source in bundle["rules"]["sources"]}
    items = []
    for rule in bundle["rules"]["rules"]:
        required = CA_EVIDENCE_BY_RULE[rule["id"]]
        missing = [
            evidence_id for evidence_id in required if evidence_id not in provided
        ]
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
            "official_sources": [
                source_index[source_id] for source_id in rule["source_ids"]
            ],
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
        "canadian_tax_residency_determined": False,
        "ccpc_status_determined": False,
        "small_business_deduction_eligibility_determined": False,
        "gst_hst_registration_liability_determined": False,
        "gst_hst_supply_classification_performed": False,
        "corporation_tax_rate_determined": False,
        "tax_calculation_performed": False,
        "filing_performed": False,
        "payment_performed": False,
        "external_submission_enabled": False,
        "scope_note": bundle["rules"].get("scope_note"),
    }


def build_ca_tax_calendar(
    payload: dict[str, Any], context: ServiceContext,
) -> dict[str, Any]:
    entity, bundle = _ca_context(context)
    result = build_tax_calendar(
        context.runtime,
        entity.entity_id,
        period_year=payload.get("period_year"),
        anchors=payload.get("anchors"),
        as_of=payload.get("as_of"),
    )
    freshness = _freshness(bundle, payload.get("as_of"))
    registrations = {item.strip().lower() for item in entity.tax_registrations}
    federal_corporation_scope_confirmed = bool(
        registrations & IN_SCOPE_REGISTRATIONS
    )
    blockers = list(result.get("warnings") or [])
    if not federal_corporation_scope_confirmed:
        blockers.append(
            "Canada federal corporation / corporation income tax scope is not confirmed"
        )
    if not freshness["current"]:
        blockers.append("official source review has expired")
    return {
        **result,
        "ready": bool(
            result["ready"]
            and freshness["current"]
            and federal_corporation_scope_confirmed
        ),
        "federal_corporation_scope_confirmed": (
            federal_corporation_scope_confirmed
        ),
        "source_freshness": freshness,
        "blockers": sorted(set(blockers)),
        "canadian_tax_residency_determined": False,
        "ccpc_status_determined": False,
        "small_business_deduction_eligibility_determined": False,
        "gst_hst_registration_liability_determined": False,
        "gst_hst_supply_classification_performed": False,
        "corporation_tax_rate_determined": False,
        "tax_calculation_performed": False,
        "filing_performed": False,
        "payment_performed": False,
        "external_submission_enabled": False,
    }
