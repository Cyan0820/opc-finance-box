from __future__ import annotations

import re
from typing import Any

from .pack_services import ServiceContext
from .tax_returns import build_tax_returns


FORM_RULES = {
    "VAT-RETURN": "cn.vat.return_workpaper.v2026_02",
    "A200000": "cn.cit.prepaid_form_a.v2025_10",
    "A01103": "cn.stamp.tax_source_workpaper.v2022_07",
    "IIT-WITHHOLD": "cn.iit.withholding_workpaper.current",
}


def _statutory_rows(
    payload: dict[str, Any],
    field: str,
    context: ServiceContext,
) -> list[dict[str, Any]]:
    rows = payload.get(field) or []
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError(f"{field} must be a list of objects")
    outside = [
        str(row.get("id") or index + 1)
        for index, row in enumerate(rows)
        if row.get("entity_id") != context.entity_id
    ]
    if outside:
        raise ValueError(f"{field} contains records outside entity {context.entity_id}: {', '.join(outside)}")
    return [dict(row) for row in rows]


def _cn_workspace(payload: dict[str, Any], context: ServiceContext) -> tuple[dict[str, Any], dict[str, Any]]:
    if not context.entity_id:
        raise ValueError("CN tax workpaper requires entity_id")
    entity = context.runtime.entities.get(context.entity_id)
    if entity.jurisdiction != "CN" or entity.tax_pack != "jurisdiction.cn_mainland":
        raise ValueError(f"Entity {entity.entity_id} does not use jurisdiction.cn_mainland")
    if entity.functional_currency != "CNY":
        raise ValueError("CN workpaper adapter requires statutory facts translated to CNY")
    period = str(payload.get("period") or "")
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", period):
        raise ValueError("period must use YYYY-MM")
    profile = dict(payload.get("tax_profile") or {})
    if not isinstance(payload.get("tax_profile") or {}, dict):
        raise ValueError("tax_profile must be an object")
    registrations = {item.lower() for item in entity.tax_registrations}
    if "vat_general_taxpayer" in registrations:
        inferred_vat_type = "一般纳税人"
    elif "vat_small_scale_taxpayer" in registrations:
        inferred_vat_type = "小规模纳税人"
    else:
        inferred_vat_type = "待配置"
    profile.update({
        "company_name": entity.legal_name,
        "jurisdiction": entity.jurisdiction,
        "base_currency": entity.functional_currency,
        "fiscal_year_end": entity.fiscal_year_end,
        "vat_taxpayer_type": profile.get("vat_taxpayer_type") or inferred_vat_type,
        "accounting_standard": profile.get("accounting_standard") or entity.accounting_basis,
    })
    workspace = build_tax_returns(
        _statutory_rows(payload, "settlements", context),
        period,
        company_profile=profile,
        purchases=_statutory_rows(payload, "purchases", context),
        invoices=_statutory_rows(payload, "invoices", context),
        payroll_rows=_statutory_rows(payload, "payroll_rows", context),
        period_report=payload.get("period_report") or {},
        financial_statements=payload.get("financial_statements") or {},
        cross_border=payload.get("cross_border") or {},
    )
    bundle = context.runtime.tax_rules(entity.entity_id)
    source_index = {source["id"]: source for source in bundle["rules"]["sources"]}
    rule_index = {rule["id"]: rule for rule in bundle["rules"]["rules"]}
    for form in workspace["returns"]:
        rule_id = FORM_RULES.get(form["form_code"])
        if not rule_id:
            continue
        rule = rule_index[rule_id]
        sources = [source_index[source_id] for source_id in rule["source_ids"]]
        form["rule_id"] = rule_id
        form["official_sources"] = sources
        form["official_source"] = sources[0]["url"]
        form["rules_verified_at"] = bundle["rules"]["verified_at"]
    workspace.update({
        "entity_id": entity.entity_id,
        "tax_pack": bundle["pack_id"],
        "tax_pack_version": bundle["pack_version"],
        "tax_readiness": entity.tax_readiness,
        "rules_verified_at": bundle["rules"]["verified_at"],
        "filing_performed": False,
        "external_submission_enabled": False,
    })
    return workspace, bundle


def _form_service(
    payload: dict[str, Any],
    context: ServiceContext,
    form_code: str,
) -> dict[str, Any]:
    workspace, _ = _cn_workspace(payload, context)
    form = next(item for item in workspace["returns"] if item["form_code"] == form_code)
    return {
        "ready_for_review": form["status"] == "待复核" and not form["blockers"],
        "entity_id": workspace["entity_id"],
        "period": workspace["period"],
        "tax_pack": workspace["tax_pack"],
        "tax_pack_version": workspace["tax_pack_version"],
        "rules_verified_at": workspace["rules_verified_at"],
        "form": form,
        "review_gate": "tax_workpaper_approval",
        "human_review_required": True,
        "filing_performed": False,
        "external_submission_enabled": False,
    }


def build_cn_vat_workpaper(payload: dict[str, Any], context: ServiceContext) -> dict[str, Any]:
    return _form_service(payload, context, "VAT-RETURN")


def build_cn_cit_prepaid_workpaper(payload: dict[str, Any], context: ServiceContext) -> dict[str, Any]:
    return _form_service(payload, context, "A200000")


def build_cn_stamp_tax_workpaper(payload: dict[str, Any], context: ServiceContext) -> dict[str, Any]:
    return _form_service(payload, context, "A01103")


def build_cn_iit_withholding_workpaper(payload: dict[str, Any], context: ServiceContext) -> dict[str, Any]:
    return _form_service(payload, context, "IIT-WITHHOLD")
