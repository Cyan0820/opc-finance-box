from __future__ import annotations

import math
import re
from collections import defaultdict
from datetime import date
from typing import Any, Iterable


POLICY_CLASSIFICATIONS = {"费用化", "预付", "递延", "待判断"}
SUPPORTED_METHODS = {"按服务期间直线释放", "指定期间一次费用化"}
VERIFIED_INVOICE_STATUSES = {"已查验", "查验通过", "有效"}
IN_SCOPE_TYPES = {"游戏授权费", "IP license", "服务器/云资源", "云资源预付", "服务器资源预付"}


def _number(value: Any) -> float:
    try:
        result = float(value or 0)
        return 0.0 if math.isnan(result) or math.isinf(result) else result
    except (TypeError, ValueError):
        return 0.0


def _text(value: Any) -> str:
    return str(value or "").strip()


def _period(value: Any) -> str:
    text = _text(value)[:7]
    return text if re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", text) else ""


def _currency(row: dict) -> str:
    return _text(row.get("currency") or row.get("original_currency") or "CNY").upper()


def _explicit_project(row: dict) -> str:
    project = _text(row.get("project"))
    if not project or any(token in project for token in ("待分配", "公司公共", "未知项目")):
        return ""
    return project


def _evidence(value: Any) -> list[str]:
    if isinstance(value, str):
        values = re.split(r"[；;\n]+", value)
    elif isinstance(value, Iterable) and not isinstance(value, (dict, bytes)):
        values = list(value)
    else:
        values = []
    return list(dict.fromkeys(_text(item)[:300] for item in values if _text(item)))


def _shift_period(period: str, months: int) -> str:
    year, month = map(int, period.split("-"))
    absolute = year * 12 + month - 1 + months
    return f"{absolute // 12:04d}-{absolute % 12 + 1:02d}"


def _months(start: str, end: str) -> list[str]:
    start_period, end_period = _period(start), _period(end)
    if not start_period or not end_period or end_period < start_period:
        return []
    rows, current = [], start_period
    while current <= end_period:
        rows.append(current)
        current = _shift_period(current, 1)
    return rows


def is_special_cost_managed(row: dict) -> bool:
    """True only when the source explicitly opts into this candidate bridge."""
    facts = row.get("contract_facts") or {}
    return bool(row.get("cost_policy") or facts.get("cost_type") in IN_SCOPE_TYPES)


def _period_evidence(facts: dict) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    raw = facts.get("period_evidence") or []
    if isinstance(raw, dict):
        raw = [{"period": key, "evidence": value} for key, value in raw.items()]
    for item in raw if isinstance(raw, list) else []:
        period = _period((item or {}).get("period")) if isinstance(item, dict) else ""
        evidence = _evidence((item or {}).get("evidence")) if isinstance(item, dict) else []
        if period and evidence:
            result.setdefault(period, []).extend(evidence)
    return result


def _purchase_evidence(purchase: dict, datasets: dict[str, list[dict]]) -> dict:
    entity_id, purchase_id = _text(purchase.get("entity_id")), _text(purchase.get("id"))
    deliveries = [
        item for item in datasets.get("purchase_deliveries") or []
        if _text(item.get("entity_id")) == entity_id and _text(item.get("purchase_id")) == purchase_id
    ]
    accepted = sum(
        _number(item.get("accepted_amount")) for item in deliveries
        if item.get("status") in {"已验收", "部分验收"} and _evidence(item.get("acceptance_evidence"))
    )
    if not deliveries and purchase.get("acceptance_history"):
        accepted = _number(purchase.get("accepted_amount"))
    invoices = [
        item for item in datasets.get("invoices") or []
        if _text(item.get("entity_id")) == entity_id
        and _text((item.get("purchase_match") or {}).get("purchase_id")) == purchase_id
        and item.get("verification_status") in VERIFIED_INVOICE_STATUSES
        and not item.get("anomalies")
    ]
    invoice_amount = sum(_number(item.get("total_amount")) for item in invoices)
    allocations = [
        item for item in datasets.get("cash_allocations") or []
        if _text(item.get("entity_id")) == entity_id and item.get("target_type") == "payable"
        and _text(item.get("target_id")) == purchase_id and item.get("status") not in {"已撤销", "已退回"}
    ]
    allocated_paid = sum(_number(item.get("amount")) for item in allocations)
    paid = max(_number(purchase.get("paid_amount")), allocated_paid)
    return {
        "accepted_amount": round(accepted, 2),
        "invoice_amount": round(invoice_amount, 2),
        "paid_amount": round(paid, 2),
        "acceptance_evidence_count": sum(len(_evidence(item.get("acceptance_evidence"))) for item in deliveries),
        "invoice_evidence_count": len(invoices),
        "payment_evidence_count": len(allocations) + len(_evidence(purchase.get("payment_evidence"))),
    }


def _asset_evidence(card: dict) -> dict:
    evidence = card.get("cost_evidence") or {}
    return {
        "accepted_amount": round(_number(evidence.get("accepted_amount")), 2),
        "invoice_amount": round(_number(evidence.get("verified_invoice_amount")), 2),
        "paid_amount": round(_number(evidence.get("paid_amount")), 2),
        "acceptance_evidence_count": len(_evidence(evidence.get("acceptance_evidence"))),
        "invoice_evidence_count": len(_evidence(evidence.get("invoice_evidence"))),
        "payment_evidence_count": len(_evidence(evidence.get("payment_evidence"))),
    }


def _source_rows(datasets: dict[str, list[dict]]) -> list[tuple[str, dict]]:
    output = []
    for purchase in datasets.get("purchases") or []:
        if is_special_cost_managed(purchase):
            output.append(("purchase", purchase))
    for card in datasets.get("asset_cards") or []:
        if is_special_cost_managed(card):
            output.append(("asset_card", card))
    return output


def build_game_prepaid_cost_view(datasets: dict[str, list[dict]], analysis_period: str) -> dict:
    """Build read-only expense/prepaid/deferred candidates and evidence-gated releases."""
    analysis_period = _period(analysis_period)
    candidates = []
    release_schedule = []
    impact_groups = defaultdict(float)
    gap_counts = defaultdict(int)

    for source_type, source in _source_rows(datasets):
        source_id = _text(source.get("id"))
        entity_id = _text(source.get("entity_id"))
        project = _explicit_project(source)
        currency = _currency(source)
        facts = source.get("contract_facts") or {}
        policy = source.get("cost_policy") or {}
        cost_type = _text(facts.get("cost_type"))
        contract_reference = _text(facts.get("contract_reference"))
        contract_evidence = _evidence(facts.get("contract_evidence"))
        service_start = _text(facts.get("service_start"))
        service_end = _text(facts.get("service_end"))
        service_periods = _months(service_start, service_end)
        period_evidence = _period_evidence(facts)
        classification = _text(policy.get("classification"))
        method = _text(policy.get("allocation_method"))
        policy_evidence = _evidence(policy.get("evidence"))
        policy_approved = policy.get("status") == "已批准" and bool(_text(policy.get("approved_by"))) and bool(policy_evidence)
        amount = round(_number(policy.get("cost_basis_amount")), 2)
        expense_period = _period(policy.get("expense_period"))
        proof = _purchase_evidence(source, datasets) if source_type == "purchase" else _asset_evidence(source)
        issues = []
        if not entity_id:
            issues.append("缺少显式法律主体")
        if not project:
            issues.append("缺少显式项目代码")
        if not analysis_period or not currency:
            issues.append("缺少显式期间或币种")
        if cost_type not in IN_SCOPE_TYPES:
            issues.append("成本类型未明确为游戏授权、IP license 或服务器/云资源")
        if not contract_reference or not contract_evidence:
            issues.append("缺少合同编号或合同证据")
        if not service_periods:
            issues.append("缺少有效合同期限/服务期间")
        if not policy_approved:
            issues.append("缺少已批准且有证据的会计政策")
        if classification not in POLICY_CLASSIFICATIONS:
            issues.append("会计政策未明确费用化、预付、递延或待判断")
        if classification != "待判断" and method not in SUPPORTED_METHODS:
            issues.append("会计政策未明确支持的期间释放方法")
        if amount <= 0:
            issues.append("会计政策缺少显式成本基础金额")
        if proof["accepted_amount"] + 0.01 < amount or proof["acceptance_evidence_count"] <= 0:
            issues.append("成本基础缺少足额验收及验收证据")
        if proof["invoice_amount"] + 0.01 < amount or proof["invoice_evidence_count"] <= 0:
            issues.append("成本基础缺少足额已查验发票证据")
        if classification == "费用化" and not expense_period:
            issues.append("一次费用化缺少显式费用期间")
        if classification == "费用化" and expense_period not in service_periods:
            issues.append("费用期间不在合同服务期间内")

        can_schedule = not issues and classification != "待判断"
        planned_periods = [expense_period] if classification == "费用化" else service_periods
        planned_amounts = []
        if can_schedule:
            regular = round(amount / len(planned_periods), 2)
            accumulated = 0.0
            for index, period in enumerate(planned_periods):
                planned = regular if index < len(planned_periods) - 1 else round(amount - accumulated, 2)
                accumulated = round(accumulated + planned, 2)
                planned_amounts.append((period, planned))

        released = 0.0
        source_schedule = []
        for period, planned in planned_amounts:
            evidence = period_evidence.get(period) or []
            release = planned if evidence else 0.0
            status = "可形成候选" if evidence else "缺本期服务/权利证据"
            if not evidence:
                gap_counts["release_period_without_evidence"] += 1
            released = round(released + release, 2)
            item = {
                "source_id": source_id, "source_type": source_type, "entity_id": entity_id,
                "project": project, "currency": currency, "cost_type": cost_type,
                "classification": classification, "period": period, "planned_release": planned,
                "release_candidate": release, "unreleased_candidate_balance": round(amount - released, 2),
                "period_evidence_count": len(evidence), "status": status,
                "posting_status": "candidate_only_not_posted",
            }
            source_schedule.append(item)
            release_schedule.append(item)
            if period == analysis_period and release:
                impact_groups[(entity_id, project, currency)] += release

        if issues:
            gap_counts["blocked_candidate"] += 1
        current_release = sum(item["release_candidate"] for item in source_schedule if item["period"] == analysis_period)
        candidates.append({
            "source_id": source_id, "source_type": source_type, "entity_id": entity_id,
            "project": project, "currency": currency, "analysis_period": analysis_period,
            "cost_type": cost_type or "待明确", "contract_reference": contract_reference,
            "service_start": service_start, "service_end": service_end,
            "classification_candidate": classification if policy_approved and classification in POLICY_CLASSIFICATIONS else "待判断",
            "cost_basis_amount": amount, "current_period_release_candidate": round(current_release, 2),
            "unreleased_candidate_balance": round(amount - sum(item["release_candidate"] for item in source_schedule), 2),
            "accepted_amount": proof["accepted_amount"], "verified_invoice_amount": proof["invoice_amount"],
            "paid_amount": proof["paid_amount"], "payment_evidence_count": proof["payment_evidence_count"],
            "payment_effect": "仅表示资金流出，不决定费用化、预付、递延或资本化",
            "policy_status": "已批准" if policy_approved else "未形成有效批准政策",
            "policy_approved_by": _text(policy.get("approved_by")), "allocation_method": method,
            "schedule_status": "可生成期间候选" if can_schedule else "待判断/阻塞",
            "issues": issues, "status": "阻塞" if issues else "候选待会计复核",
            "accounting_boundary": "不判断无形资产、研发资本化或税务处理；不改账、不过账",
        })

    impacts = [{
        "entity_id": key[0], "project": key[1], "currency": key[2], "period": analysis_period,
        "project_cost_impact_candidate": round(amount, 2),
        "contribution_effect": round(-amount, 2),
    } for key, amount in sorted(impact_groups.items())]
    return {
        "period": analysis_period,
        "candidates": candidates,
        "release_schedule": release_schedule,
        "project_impacts": impacts,
        "summary": {
            "candidate_count": len(candidates),
            "blocked_count": sum(bool(row["issues"]) for row in candidates),
            "release_candidate_count": sum(row["release_candidate"] > 0 for row in release_schedule),
            "gaps": dict(sorted(gap_counts.items())),
            "scopes": sorted({(row["entity_id"], row["currency"]) for row in candidates}),
        },
        "guardrails": [
            "主体、项目、期间和币种必须显式填写；不跨主体、不跨币种，不从供应商、付款或金额猜归属。",
            "付款只表示资金流出，不自动形成预付、递延、费用或资产候选。",
            "只有已批准会计政策、足额验收、已查验发票、合同服务期间和逐期间服务/权利证据共同支持时才释放候选。",
            "缺少本期服务/权利证据的月份不摊销，也不追补到其他月份。",
            "候选不判断无形资产、研发资本化或税务处理，不写总账、不过账。",
        ],
    }
