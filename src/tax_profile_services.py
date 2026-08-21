from __future__ import annotations

from typing import Any

from .pack_services import ServiceContext


EVIDENCE_BY_RULE = {
    "sg.cit.calendar.eci": [
        "financial_year_end",
        "management_accounts",
        "estimated_chargeable_income_support",
        "eci_exemption_eligibility_facts",
    ],
    "sg.cit.calendar.annual_return": [
        "approved_or_final_financial_statements",
        "tax_computation_workpaper",
        "form_eligibility_assessment",
        "authorized_filer_confirmation",
    ],
    "sg.gst.calendar.registered_entity": [
        "confirmed_gst_registration",
        "gst_accounting_period",
        "sales_and_purchase_tax_evidence",
        "gst_payment_reconciliation",
    ],
    "sg.gst.registration.monitor": [
        "rolling_taxable_turnover",
        "next_12_month_turnover_forecast",
        "taxable_supply_classification",
        "registration_review_decision",
    ],
}


def _sg_context(context: ServiceContext) -> tuple[Any, dict[str, Any]]:
    if not context.entity_id:
        raise ValueError("SG tax service requires entity_id")
    entity = context.runtime.entities.get(context.entity_id)
    if entity.jurisdiction != "SG" or entity.tax_pack != "jurisdiction.sg":
        raise ValueError(f"Entity {entity.entity_id} does not use jurisdiction.sg")
    return entity, context.runtime.tax_rules(entity.entity_id)


def build_sg_registration_profile(payload: dict[str, Any], context: ServiceContext) -> dict[str, Any]:
    entity, bundle = _sg_context(context)
    registrations = {item.lower() for item in entity.tax_registrations}
    gst_confirmed = bool(registrations & {"gst", "gst_registered"})
    gst_mentioned = any("gst" in item for item in registrations)
    gst_status = "confirmed" if gst_confirmed else "needs_confirmation" if gst_mentioned else "not_registered"
    sources = bundle["rules"]["sources"]
    source_index = {source["id"]: source for source in sources}
    registration_rule = next(
        rule for rule in bundle["rules"]["rules"]
        if rule["id"] == "sg.gst.registration.monitor"
    )
    return {
        "ready": gst_status != "needs_confirmation",
        "entity_id": entity.entity_id,
        "jurisdiction": entity.jurisdiction,
        "tax_pack": entity.tax_pack,
        "tax_readiness": entity.tax_readiness,
        "registrations": {
            "corporate_income_tax": (
                "confirmed" if "corporate_income_tax" in registrations else "needs_confirmation"
            ),
            "gst": gst_status,
        },
        "raw_registration_codes": list(entity.tax_registrations),
        "gst_monitoring_inputs": {
            "rolling_taxable_turnover": payload.get("rolling_taxable_turnover"),
            "next_12_month_forecast": payload.get("next_12_month_forecast"),
            "currency": payload.get("currency") or entity.functional_currency,
            "taxable_supply_classification_confirmed": payload.get("taxable_supply_classification_confirmed") is True,
        },
        "official_sources": [source_index[source_id] for source_id in registration_rule["source_ids"]],
        "review_gate": "tax_registration_confirmation",
        "human_review_required": True,
        "determination_performed": False,
        "warnings": (
            ["GST registration status must be confirmed before calendar or filing work."]
            if gst_status == "needs_confirmation" else []
        ),
    }


def build_sg_evidence_checklist(payload: dict[str, Any], context: ServiceContext) -> dict[str, Any]:
    entity, bundle = _sg_context(context)
    provided = payload.get("provided_evidence") or {}
    if not isinstance(provided, dict):
        raise ValueError("provided_evidence must be an object keyed by evidence id")
    source_index = {source["id"]: source for source in bundle["rules"]["sources"]}
    items = []
    for rule in bundle["rules"]["rules"]:
        required = EVIDENCE_BY_RULE.get(rule["id"], ["rule_applicability_facts", "reviewer_decision"])
        missing = [evidence_id for evidence_id in required if not provided.get(evidence_id)]
        items.append({
            "rule_id": rule["id"],
            "summary": rule["summary"],
            "automation_level": rule["automation_level"],
            "required_evidence": required,
            "missing_evidence": missing,
            "complete": not missing,
            "official_sources": [source_index[source_id] for source_id in rule["source_ids"]],
            "human_review_required": True,
        })
    return {
        "ready": all(item["complete"] for item in items),
        "entity_id": entity.entity_id,
        "tax_pack": bundle["pack_id"],
        "tax_pack_version": bundle["pack_version"],
        "rules_verified_at": bundle["rules"]["verified_at"],
        "items": items,
        "review_gate": "tax_advisor_review",
        "filing_or_tax_calculation_performed": False,
        "scope_note": bundle["rules"].get("scope_note"),
    }
