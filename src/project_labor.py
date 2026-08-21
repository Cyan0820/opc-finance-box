from __future__ import annotations

import math
import re
from collections import defaultdict
from typing import Any, Iterable


RESEARCH_ACTIVITY_TYPES = {"研发活动", "research", "r&d", "rd"}


def _number(value: Any) -> float:
    try:
        result = float(value or 0)
        return 0.0 if math.isnan(result) or math.isinf(result) else result
    except (TypeError, ValueError):
        return 0.0


def _text(value: Any) -> str:
    return str(value or "").strip()


def _period(value: Any) -> str:
    text = _text(value)
    return text[:7] if re.match(r"^\d{4}-\d{2}", text) else ""


def _currency(row: dict) -> str:
    return _text(row.get("currency") or "CNY").upper()


def _explicit_project(value: Any) -> str:
    project = _text(value)
    if not project or any(token in project for token in ("待分配", "公司公共", "未知项目", "非研发")):
        return ""
    return project


def _evidence_items(value: Any) -> list[str]:
    if isinstance(value, str):
        values = re.split(r"[；;\n]+", value)
    elif isinstance(value, Iterable) and not isinstance(value, (dict, bytes)):
        values = list(value)
    else:
        values = []
    return list(dict.fromkeys(_text(item)[:200] for item in values if _text(item)))


def _cost_pool(row: dict) -> dict:
    gross = max(0.0, _number(row.get("gross_salary")))
    employer_components = sum(max(0.0, _number(row.get(name))) for name in (
        "employer_contributions", "employer_levies", "other_employer_cost",
    ))
    stated_total = max(0.0, _number(row.get("total_employer_cost")))
    employer_extra = employer_components if employer_components else max(0.0, stated_total - gross)
    total = gross + employer_extra
    return {
        "gross_salary": gross,
        "employer_cost": employer_extra,
        "total_employer_cost": total,
        "employee_deductions": max(0.0, _number(row.get("employee_deductions"))),
        "withholding_tax": max(0.0, _number(row.get("withholding_tax") or row.get("calculated_iit"))),
    }


def _raw_allocations(row: dict) -> list[dict]:
    explicit = row.get("project_allocations")
    if isinstance(explicit, list):
        return [dict(item) for item in explicit if isinstance(item, dict)]
    project = _explicit_project(row.get("project"))
    ratio_is_explicit = row.get("allocation_ratio") not in (None, "")
    ratio = row.get("allocation_ratio") if ratio_is_explicit else row.get("rd_ratio")
    evidence = row.get("allocation_evidence") or row.get("timesheet_evidence") or []
    if not project and not ratio and not evidence:
        return []
    return [{
        "project": project,
        "ratio": ratio,
        "hours": row.get("timesheet_hours"),
        "total_hours": row.get("total_hours"),
        "evidence": evidence,
        "evidence_type": row.get("allocation_evidence_type") or row.get("evidence_type"),
        "activity_type": row.get("activity_type") or ("研发活动" if not ratio_is_explicit and _number(row.get("rd_ratio")) > 0 else ""),
        "research_ratio": row.get("research_ratio") or row.get("rd_ratio"),
        "method": row.get("allocation_method") or ("工时比例" if row.get("timesheet_hours") not in (None, "") else "显式分摊比例"),
    }]


def _validated_allocations(row: dict) -> tuple[list[dict], list[str]]:
    allocations = []
    issues = []
    for index, raw in enumerate(_raw_allocations(row), 1):
        project = _explicit_project(raw.get("project") or raw.get("project_code"))
        hours = _number(raw.get("hours"))
        total_hours = _number(raw.get("total_hours"))
        ratio = _number(raw.get("ratio"))
        if ratio > 1 and ratio <= 100:
            ratio /= 100
        if ratio <= 0 and hours > 0 and total_hours > 0:
            ratio = hours / total_hours
        evidence = _evidence_items(raw.get("evidence"))
        if not project:
            issues.append(f"第{index}项分摊缺少显式项目代码")
            continue
        if ratio <= 0 or ratio > 1:
            issues.append(f"{project} 缺少有效分摊比例或工时比例")
            continue
        if not evidence:
            issues.append(f"{project} 缺少工时或分摊证据")
            continue
        if hours > 0 and total_hours > 0 and abs(ratio - hours / total_hours) > 0.01:
            issues.append(f"{project} 分摊比例与工时比例不一致")
            continue
        research_ratio = _number(raw.get("research_ratio"))
        if research_ratio > 1 and research_ratio <= 100:
            research_ratio /= 100
        research_ratio = max(0.0, min(ratio, research_ratio))
        allocations.append({
            "project": project,
            "ratio": ratio,
            "hours": hours,
            "total_hours": total_hours,
            "evidence_count": len(evidence),
            "evidence_type": _text(raw.get("evidence_type")) or "分摊证据",
            "activity_type": _text(raw.get("activity_type")) or "未标注活动性质",
            "research_ratio": research_ratio,
            "method": _text(raw.get("method")) or "显式分摊比例",
        })
    if sum(item["ratio"] for item in allocations) > 1.0001:
        return [], [*issues, "同一人员成本池项目分摊比例合计超过100%"]
    return allocations, issues


def _blank_row(entity_id: str, project: str, currency: str, period: str) -> dict:
    return {
        "entity_id": entity_id,
        "project": project,
        "currency": currency,
        "period": period,
        "gross_salary": 0.0,
        "employer_cost": 0.0,
        "project_cost_candidate": 0.0,
        "employee_deductions": 0.0,
        "withholding_tax": 0.0,
        "research_cost_candidate": 0.0,
        "evidenced_hours": 0.0,
        "evidenced_ratio": 0.0,
        "allocation_record_count": 0,
        "evidence_count": 0,
        "evidence_types": set(),
        "allocation_methods": set(),
        "activity_types": set(),
        "issues": set(),
    }


def build_project_labor_cost_view(payroll_rows: Iterable[dict], period: str) -> dict:
    """Aggregate privacy-safe project labor candidates from explicit allocation evidence.

    The output never returns a person identifier, department, source row, or individual
    compensation amount. Withholding and employee deductions are informational parts of
    payroll settlement, not additional project cost.
    """
    period = _period(period)
    buckets: dict[tuple[str, str, str], dict] = {}
    scope_totals: dict[tuple[str, str], dict] = {}
    gap_counts = defaultdict(int)

    def bucket_for(entity_id: str, project: str, currency: str) -> dict:
        key = (entity_id, project, currency)
        if key not in buckets:
            buckets[key] = _blank_row(entity_id, project, currency, period)
        return buckets[key]

    for payroll in payroll_rows:
        if _period(payroll.get("period")) != period:
            continue
        entity_id = _text(payroll.get("entity_id"))
        currency = _currency(payroll)
        costs = _cost_pool(payroll)
        scope = scope_totals.setdefault((entity_id, currency), {
            "entity_id": entity_id,
            "currency": currency,
            "gross_salary": 0.0,
            "employer_cost": 0.0,
            "total_employer_cost": 0.0,
            "allocated_project_cost": 0.0,
            "unallocated_project_cost": 0.0,
            "withholding_tax": 0.0,
            "employee_deductions": 0.0,
            "evidence_gap_count": 0,
        })
        for name in ("gross_salary", "employer_cost", "total_employer_cost", "withholding_tax", "employee_deductions"):
            scope[name] += costs[name]

        allocations, issues = _validated_allocations(payroll)
        allocated_ratio = sum(item["ratio"] for item in allocations)
        if issues:
            scope["evidence_gap_count"] += len(issues)
            gap_counts["invalid_or_missing_allocation_evidence"] += len(issues)
        if not allocations:
            gap_counts["unallocated_cost_pool"] += 1

        for allocation in allocations:
            ratio = allocation["ratio"]
            row = bucket_for(entity_id, allocation["project"], currency)
            gross = costs["gross_salary"] * ratio
            employer = costs["employer_cost"] * ratio
            candidate = gross + employer
            row["gross_salary"] += gross
            row["employer_cost"] += employer
            row["project_cost_candidate"] += candidate
            row["employee_deductions"] += costs["employee_deductions"] * ratio
            row["withholding_tax"] += costs["withholding_tax"] * ratio
            row["evidenced_hours"] += allocation["hours"]
            row["evidenced_ratio"] += ratio
            row["allocation_record_count"] += 1
            row["evidence_count"] += allocation["evidence_count"]
            row["evidence_types"].add(allocation["evidence_type"])
            row["allocation_methods"].add(allocation["method"])
            row["activity_types"].add(allocation["activity_type"])
            research_ratio = allocation["research_ratio"]
            if research_ratio <= 0 and allocation["activity_type"].casefold() in RESEARCH_ACTIVITY_TYPES:
                research_ratio = ratio
            row["research_cost_candidate"] += costs["total_employer_cost"] * research_ratio
            scope["allocated_project_cost"] += candidate

        unallocated_ratio = max(0.0, 1 - allocated_ratio)
        unallocated = costs["total_employer_cost"] * unallocated_ratio
        scope["unallocated_project_cost"] += unallocated

    rows = []
    for row in buckets.values():
        for name in (
            "gross_salary", "employer_cost", "project_cost_candidate", "employee_deductions",
            "withholding_tax", "research_cost_candidate", "evidenced_hours", "evidenced_ratio",
        ):
            row[name] = round(row[name], 4 if name == "evidenced_ratio" else 2)
        row["evidence_types"] = sorted(row["evidence_types"])
        row["allocation_methods"] = sorted(row["allocation_methods"])
        row["activity_types"] = sorted(row["activity_types"])
        row["issues"] = sorted(row["issues"])
        row["control_status"] = "证据可追溯" if row["evidence_count"] else "证据待补"
        row["research_treatment"] = "仅研发工时管理候选；资本化及税收优惠未判断"
        rows.append(row)
    rows.sort(key=lambda item: (item["project"], item["entity_id"], item["currency"]))

    scopes = []
    for scope in scope_totals.values():
        for name in (
            "gross_salary", "employer_cost", "total_employer_cost", "allocated_project_cost",
            "unallocated_project_cost", "withholding_tax", "employee_deductions",
        ):
            scope[name] = round(scope[name], 2)
        scopes.append(scope)
    scopes.sort(key=lambda item: (item["entity_id"], item["currency"]))
    return {
        "period": period,
        "rows": rows,
        "summary": {
            "row_count": len(rows),
            "project_count": len({row["project"] for row in rows}),
            "gaps": dict(sorted(gap_counts.items())),
            "by_entity_currency": scopes,
        },
        "privacy": {
            "personal_rows_returned": False,
            "excluded_fields": ["姓名", "工号", "匿名员工标识", "部门", "个人工资明细", "个人税费明细"],
        },
        "guardrails": [
            "只使用显式主体、期间、币种、人员成本池、项目代码和工时/分摊比例证据；不从部门、姓名或金额猜项目。",
            "工资与雇主成本构成项目成本候选；员工扣款和代扣代缴单独展示，不重复增加项目成本。",
            "未获项目证据支持的人员成本留在公司公共/待分配，不强行摊入游戏项目。",
            "研发工时只形成管理候选，不自动判断研发资本化、研发费用归集或税收优惠资格。",
            "输出只含项目级汇总，不返回个人身份或个人薪酬明细。",
        ],
    }
