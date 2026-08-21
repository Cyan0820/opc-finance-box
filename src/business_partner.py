from __future__ import annotations

import re
from calendar import monthrange
from collections import defaultdict
from typing import Any

from .game_kpis import enrich_kpis
from .planning import build_planning_analysis
from .management_insights import build_change_attribution, build_proactive_insights
from .business_flows import build_flow_overview
from .game_portfolio import build_game_collection_portfolio
from .project_costs import build_project_procurement_cost_view
from .project_labor import build_project_labor_cost_view
from .game_prepaid_costs import build_game_prepaid_cost_view, is_special_cost_managed


UNASSIGNED = "公司公共/待分配"


def _previous_period(period: str) -> str:
    year, month = map(int, period.split("-"))
    month -= 1
    if month == 0:
        year, month = year - 1, 12
    return f"{year:04d}-{month:02d}"


def _record_period(record: dict, field: str = "period") -> str:
    value = str(record.get(field) or "")
    return value[:7] if re.fullmatch(r"\d{4}-\d{2}.*", value) else ""


def _project_resolver(master_records: list[dict]):
    games = [row for row in master_records if row.get("record_type") == "game" and row.get("active", True)]
    aliases = {}
    for row in games:
        code, name = str(row.get("code") or "").strip(), str(row.get("name") or "").strip()
        if code:
            aliases[code.lower()] = code
        if name:
            aliases[name.lower()] = code or name
    def resolve(value: Any) -> str:
        text = str(value or "").strip()
        if not text or any(token in text for token in ("待分配", "公司公共", "非研发")):
            return UNASSIGNED
        return aliases.get(text.lower(), text)
    return resolve, {str(row.get("code")): row for row in games if row.get("code")}


def _fx_rate(currency: str, period: str, profile: dict) -> float | None:
    currency = (currency or "CNY").upper()
    if currency == "CNY":
        return 1.0
    rates = ((profile.get("fx_policy") or {}).get("month_end_rates") or {}).get(period) or {}
    rate = rates.get(currency)
    try:
        return float(rate) if rate not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _cny(amount: Any, currency: str, period: str, profile: dict) -> float | None:
    rate = _fx_rate(currency, period, profile)
    return float(amount or 0) * rate if rate is not None else None


def _purchase_class(category: str, item: str) -> str:
    text = f"{category} {item}".lower()
    if any(word in text for word in ("广告", "投放", "买量", "推广", "marketing")):
        return "投放"
    if any(word in text for word in ("外包", "美术", "音频", "本地化", "技术服务", "测试", "素材")):
        return "研发及内容外包"
    if any(word in text for word in ("云", "服务器", "带宽", "软件", "saas")):
        return "技术与云"
    return "其他采购"


def _periods(datasets: dict) -> list[str]:
    values = set()
    for name, field in (("settlements", "period"), ("payroll_rows", "period"), ("plan_lines", "period"), ("game_kpis", "period")):
        values.update(_record_period(row, field) for row in datasets.get(name, []))
    values.update(_record_period(row, "order_date") for row in datasets.get("purchases", []))
    values.update(_record_period(row) for row in datasets.get("purchase_deliveries", []))
    return sorted(value for value in values if value)


def build_bp_analysis(
    datasets: dict[str, list[dict]], profile: dict, period: str | None = None, scenario: str = "基准",
    include_previous: bool = True,
) -> dict:
    periods = _periods(datasets)
    period = period if period in periods else (periods[-1] if periods else "")
    previous = _previous_period(period) if period else ""
    resolve, game_master = _project_resolver(datasets.get("master_records") or [])
    rows: dict[str, dict] = defaultdict(lambda: {
        "revenue": 0.0, "gross_bookings": 0.0, "refunds": 0.0, "payroll": 0.0,
        "outsourcing": 0.0, "marketing": 0.0, "cloud": 0.0, "other_cost": 0.0,
        "procurement_actual": 0.0, "procurement_open_commitment": 0.0,
        "special_cost_release_candidate": 0.0,
        "unconverted": [], "evidence": [],
    })

    def add(project: str, key: str, amount: Any, currency: str, row_period: str, source: str):
        project = resolve(project)
        converted = _cny(amount, currency, row_period, profile)
        if converted is None:
            rows[project]["unconverted"].append(f"{source} {currency} {float(amount or 0):,.2f}")
            return
        rows[project][key] += converted
        rows[project]["evidence"].append(source)

    for row in datasets.get("settlements", []):
        if row.get("release_status") not in {None, "", "released"}:
            continue
        if _record_period(row) != period:
            continue
        currency = row.get("currency") or "CNY"
        add(row.get("game"), "revenue", row.get("settlement_amount"), currency, period, f"结算 {row.get('id')}")
        add(row.get("game"), "gross_bookings", row.get("gross"), currency, period, f"流水 {row.get('id')}")
        add(row.get("game"), "refunds", row.get("refunds"), currency, period, f"退款 {row.get('id')}")
    project_procurement_costs = build_project_procurement_cost_view(datasets, period)
    managed_special_purchase_ids = {
        str(row.get("id") or "") for row in datasets.get("purchases") or [] if is_special_cost_managed(row)
    }
    for row in project_procurement_costs["actual_cost_lines"]:
        if str(row.get("purchase_id") or "") in managed_special_purchase_ids:
            continue
        purchase_type = _purchase_class(str(row.get("category") or ""), str(row.get("item") or ""))
        target = {"研发及内容外包": "outsourcing", "投放": "marketing", "技术与云": "cloud", "其他采购": "other_cost"}[purchase_type]
        add(row.get("project"), target, row.get("amount"), row.get("currency") or "CNY", period, str(row.get("source") or "采购验收"))
        converted = _cny(row.get("amount"), row.get("currency") or "CNY", period, profile)
        if converted is not None:
            rows[resolve(row.get("project"))]["procurement_actual"] += converted
    for row in project_procurement_costs["rows"]:
        converted = _cny(row.get("open_commitment"), row.get("currency") or "CNY", period, profile)
        project = resolve(row.get("project"))
        if converted is None and row.get("open_commitment"):
            rows[project]["unconverted"].append(
                f"未履约采购承诺 {row.get('entity_id')} {row.get('currency')} {float(row.get('open_commitment') or 0):,.2f}"
            )
        elif converted is not None:
            rows[project]["procurement_open_commitment"] += converted
    project_labor_costs = build_project_labor_cost_view(datasets.get("payroll_rows") or [], period)
    for row in project_labor_costs["rows"]:
        add(
            row.get("project"), "payroll", row.get("project_cost_candidate"), row.get("currency") or "CNY", period,
            "项目级人力成本分摊证据",
        )
    for scope in project_labor_costs["summary"]["by_entity_currency"]:
        if scope.get("unallocated_project_cost"):
            add(
                UNASSIGNED, "payroll", scope.get("unallocated_project_cost"),
                scope.get("currency") or "CNY", period, "未分配人员成本池",
            )
    game_prepaid_costs = build_game_prepaid_cost_view(datasets, period)
    for impact in game_prepaid_costs["project_impacts"]:
        amount = impact.get("project_cost_impact_candidate")
        add(
            impact.get("project"), "other_cost", amount, impact.get("currency") or "CNY", period,
            "授权/云资源期间释放候选",
        )
        converted = _cny(amount, impact.get("currency") or "CNY", period, profile)
        if converted is not None:
            rows[resolve(impact.get("project"))]["special_cost_release_candidate"] += converted

    kpis = enrich_kpis([row for row in datasets.get("game_kpis", []) if _record_period(row) == period and row.get("status") != "异常"])
    kpi_by_project: dict[str, dict] = defaultdict(dict)
    for row in kpis:
        project = resolve(row.get("project_code"))
        bucket = kpi_by_project[project]
        for key in ("dau", "mau", "new_users", "payers", "installs", "gross_bookings", "marketing_spend"):
            if row.get(key) is not None:
                bucket[key] = bucket.get(key, 0) + float(row[key])
        for key in ("retention_d1", "retention_d7", "retention_d30"):
            if row.get(key) is not None:
                bucket.setdefault(key, []).append(float(row[key]))
    for bucket in kpi_by_project.values():
        for key in ("retention_d1", "retention_d7", "retention_d30"):
            values = bucket.get(key)
            bucket[key] = sum(values) / len(values) if values else None
        gross, mau, payers = bucket.get("gross_bookings"), bucket.get("mau"), bucket.get("payers")
        spend, installs = bucket.get("marketing_spend"), bucket.get("installs")
        bucket["arpu"] = gross / mau if gross is not None and mau else None
        bucket["arppu"] = gross / payers if gross is not None and payers else None
        bucket["payer_rate"] = payers / mau if payers is not None and mau else None
        bucket["cpi"] = spend / installs if spend is not None and installs else None
        bucket["gross_roas"] = gross / spend if gross is not None and spend else None

    project_rows = []
    for project in sorted(set(rows) | set(kpi_by_project) | set(game_master)):
        value = rows[project]
        direct_cost = value["payroll"] + value["outsourcing"] + value["marketing"] + value["cloud"] + value["other_cost"]
        contribution = value["revenue"] - direct_cost
        committed_direct_cost = direct_cost + value["procurement_open_commitment"]
        committed_contribution = value["revenue"] - committed_direct_cost
        project_rows.append({
            "project_code": project,
            "project_name": (game_master.get(project) or {}).get("name") or project,
            "stage": (game_master.get(project) or {}).get("stage") or "待配置",
            "owner": (game_master.get(project) or {}).get("owner") or "待配置",
            **{key: round(value[key], 2) for key in ("revenue", "gross_bookings", "refunds", "payroll", "outsourcing", "marketing", "cloud", "other_cost", "procurement_actual", "procurement_open_commitment", "special_cost_release_candidate")},
            "direct_cost": round(direct_cost, 2), "contribution": round(contribution, 2),
            "committed_direct_cost": round(committed_direct_cost, 2),
            "committed_contribution": round(committed_contribution, 2),
            "contribution_margin": round(contribution / value["revenue"], 4) if value["revenue"] else None,
            "kpis": kpi_by_project.get(project) or {}, "unconverted": value["unconverted"],
            "evidence_count": len(set(value["evidence"])),
        })
    project_rows.sort(key=lambda row: row["contribution"], reverse=True)

    totals = {key: round(sum(row[key] for row in project_rows), 2) for key in (
        "revenue", "gross_bookings", "payroll", "outsourcing", "marketing", "cloud", "other_cost",
        "procurement_actual", "procurement_open_commitment", "special_cost_release_candidate", "direct_cost", "contribution",
        "committed_direct_cost", "committed_contribution"
    )}
    totals["contribution_margin"] = round(totals["contribution"] / totals["revenue"], 4) if totals["revenue"] else None

    previous_bp = None
    if include_previous and previous in periods:
        previous_datasets = {name: list(values) for name, values in datasets.items()}
        previous_bp = build_bp_analysis(previous_datasets, profile, previous, scenario, False) if previous != period else None
    prev_totals = (previous_bp or {}).get("totals") or {}
    change = {
        "revenue": round(totals["revenue"] - float(prev_totals.get("revenue") or 0), 2) if previous_bp else None,
        "contribution": round(totals["contribution"] - float(prev_totals.get("contribution") or 0), 2) if previous_bp else None,
    }

    planning = build_planning_analysis(
        datasets.get("plan_lines") or [], datasets.get("settlements") or [], datasets.get("purchases") or [],
        datasets.get("bank_transactions") or [], datasets.get("payroll_rows") or [], profile, period, scenario,
        datasets.get("collection_actions") or [], datasets.get("cash_allocations") or [],
    )
    attribution = build_change_attribution(datasets, period, profile)
    analysis_as_of = (
        f"{period}-{monthrange(int(period[:4]), int(period[5:7]))[1]:02d}" if period else None
    )
    business_flows = build_flow_overview(datasets, analysis_as_of)
    game_collection_portfolio = build_game_collection_portfolio(datasets, as_of=analysis_as_of)
    overdue_receivables = sorted(
        [row for row in business_flows["receivables"]["rows"] if (row.get("days_overdue") or 0) > 0 and row.get("outstanding", 0) > 0],
        key=lambda row: (row.get("days_overdue") or 0, row.get("outstanding") or 0), reverse=True,
    )
    pending_payment_requests = [
        row for row in datasets.get("payment_requests") or [] if row.get("status") == "待批准"
    ]
    period_variance = [row for row in planning["variance"] if row.get("period") == period]
    marketing_budget_rows = [
        row for row in period_variance
        if any(token in str(row.get("category") or "").lower() for token in ("投放", "买量", "广告", "推广", "marketing"))
    ]
    marketing_rows = []
    for row in project_rows:
        if row["project_code"] == UNASSIGNED:
            continue
        kpi = row.get("kpis") or {}
        media_spend = float(kpi.get("marketing_spend") or 0)
        finance_spend = float(row.get("marketing") or 0)
        payers = float(kpi.get("payers") or 0)
        installs = float(kpi.get("installs") or 0)
        new_users = float(kpi.get("new_users") or 0)
        gross = float(kpi.get("gross_bookings") or row.get("gross_bookings") or 0)
        gap = media_spend - finance_spend
        tolerance = max(1000.0, abs(finance_spend) * 0.05)
        if media_spend and finance_spend and abs(gap) > tolerance:
            gate_status, gate_reason = "先对账", "媒体消耗与财务已验收/入账投放存在明显差额"
        elif not media_spend and finance_spend:
            gate_status, gate_reason = "待媒体数据", "财务已记录投放支出，但未接入媒体消耗与获客结果"
        elif media_spend and not (payers or installs or new_users):
            gate_status, gate_reason = "待效果数据", "已有媒体消耗，但缺少可与成本对应的用户口径"
        elif media_spend:
            gate_status, gate_reason = "门槛待配置", "可计算早期信号，但尚未维护分平台/地区的 CPA、LTV360 和回收期目标"
        else:
            gate_status, gate_reason = "无可评估投放", "本期未识别到可勾稽的投放成本"
        marketing_rows.append({
            "project_code": row["project_code"], "project_name": row["project_name"],
            "finance_spend": round(finance_spend, 2), "media_spend": round(media_spend, 2),
            "reconciliation_gap": round(gap, 2), "gross_bookings": round(gross, 2),
            "new_users": round(new_users, 2), "payers": round(payers, 2),
            "payer_cpa": round(media_spend / payers, 2) if media_spend and payers else None,
            "cpi": round(media_spend / installs, 2) if media_spend and installs else None,
            "gross_roas": round(gross / media_spend, 4) if media_spend else None,
            "management_recovery_ratio": round(float(row.get("revenue") or 0) / media_spend, 4) if media_spend else None,
            "contribution_after_finance_spend": row.get("contribution"),
            "incrementality_status": "待自然量/重叠归因证据" if media_spend else "不适用",
            "ltv_status": "待 cohort LTV 或成熟曲线" if media_spend else "不适用",
            "gate_status": gate_status, "gate_reason": gate_reason,
        })
    total_media_spend = round(sum(row["media_spend"] for row in marketing_rows), 2)
    total_finance_spend = round(sum(row["finance_spend"] for row in marketing_rows), 2)
    total_marketing_gross = round(sum(row["gross_bookings"] for row in marketing_rows), 2)
    total_payers = sum(row["payers"] for row in marketing_rows)
    marketing_finance = {
        "boundary": {
            "owned": ["预算与承诺", "支出对账与预提", "经济性与回收", "资源追加/回撤门控", "利润和现金预测", "验收发票付款"],
            "not_owned": ["实时出价", "素材优化", "定向与人群包", "媒体账户日常运营"],
        },
        "totals": {
            "budget": round(sum(float(row.get("budget") or 0) for row in marketing_budget_rows), 2),
            "finance_spend": total_finance_spend, "media_spend": total_media_spend,
            "reconciliation_gap": round(total_media_spend - total_finance_spend, 2),
            "gross_bookings": total_marketing_gross,
            "gross_roas": round(total_marketing_gross / total_media_spend, 4) if total_media_spend else None,
            "payer_cpa": round(total_media_spend / total_payers, 2) if total_media_spend and total_payers else None,
        },
        "projects": marketing_rows,
        "decision_questions": [
            "媒体消耗、订单验收、预提、发票和付款是否同期同口径？",
            "扣除渠道费、税、分成后的回收和项目利润是否达标？",
            "cohort 是否成熟，LTV 预测版本和回测误差是否足以支持追加？",
            "自然量、重复归因和回流用户是否被误算为买量增量？",
        ],
        "guardrail": "财务 Agent 不代替投手做媒体运营；媒体侧指标只作为资源经济性、预算门控和利润现金预测的证据。",
    }
    unassigned = next((row for row in project_rows if row["project_code"] == UNASSIGNED), None)
    messages = []
    if project_rows and totals["revenue"]:
        best = max((row for row in project_rows if row["project_code"] != UNASSIGNED), key=lambda row: row["contribution"], default=None)
        if best:
            messages.append({
                "severity": "机会", "title": f"{best['project_name']} 是本期直接利润第一",
                "finding": f"管理口径收入 {best['revenue']:,.0f} 元，直接贡献 {best['contribution']:,.0f} 元。",
                "recommendation": "先确认版本、渠道和回款可持续性，再决定追加投放或人力；不以单月利润直接扩编。",
                "tradeoff": "追加投入可能放大增长，也会把固定成本和回款风险提前。",
            })
    if unassigned and unassigned["direct_cost"]:
        messages.append({
            "severity": "高", "title": "有成本尚未归属到具体游戏",
            "finding": f"公司公共/待分配直接成本 {unassigned['direct_cost']:,.0f} 元。",
            "recommendation": "先补人员工时/分摊证据和采购项目绑定；在证据完成前不要用项目利润做绩效或砍项目。",
            "tradeoff": "暂缓决策会慢一些，但能避免把公共成本错误压到某个项目。",
        })
    labor_gaps = project_labor_costs["summary"].get("gaps") or {}
    if sum(int(value or 0) for value in labor_gaps.values()):
        messages.append({
            "severity": "高", "title": "项目人力成本仍有证据缺口",
            "finding": "存在未获显式项目代码、期间和工时/比例证据共同支持的人员成本池，已保留在公司公共/待分配。",
            "recommendation": "由项目负责人补已批准工时表或分摊比例依据；不要按部门、姓名或工资金额倒推项目。",
            "tradeoff": "保留待分配会暂时降低单项目利润精度，但能保护薪酬隐私并避免错误摊销。",
        })
    if any(row["unconverted"] for row in project_rows):
        messages.append({
            "severity": "阻塞", "title": "外币项目尚未全部折算",
            "finding": "存在缺少本期汇率的收入或成本，组合总额未包含这些项目。",
            "recommendation": "在公司财务档案维护本期月末汇率和来源，再比较项目利润。",
            "tradeoff": "先保留原币可避免假精确，但当前组合排名可能不完整。",
        })
    if not kpis:
        messages.append({
            "severity": "建议", "title": "目前只能解释财务结果，不能解释用户驱动",
            "finding": "本期没有经营KPI，收入变化无法拆到活跃、付费率、ARPPU、留存和投放。",
            "recommendation": "从运营后台补经营KPI；不要求一次填满，先填流水、MAU、付费人数和投放。",
            "tradeoff": "增加一次月度数据导出，但能显著提高预算、投放和招人建议质量。",
        })
    if not datasets.get("payroll_rows") or not datasets.get("purchases"):
        missing = [name for name, records in (("工资", datasets.get("payroll_rows")), ("采购", datasets.get("purchases"))) if not records]
        messages.append({
            "severity": "阻塞", "title": "项目直接成本尚不完整",
            "finding": f"缺少{'、'.join(missing)}台账，当前贡献率不能视为完整项目利润率。",
            "recommendation": "可以先看收入排名，但在补齐项目人力和采购/外包前，不据此做扩编、奖金或关停决策。",
            "tradeoff": "暂时降低结论强度会少一些“确定感”，但能避免把缺数据误当成高利润。",
        })
    return {
        "period": period, "previous_period": previous if previous_bp else None, "available_periods": periods,
        "scenario": scenario, "totals": totals, "change_vs_previous": change,
        "projects": project_rows, "variance": period_variance, "planning": planning,
        "marketing_finance": marketing_finance,
        "game_collection_portfolio": game_collection_portfolio,
        "project_procurement_costs": project_procurement_costs,
        "project_labor_costs": project_labor_costs,
        "game_prepaid_costs": game_prepaid_costs,
        "change_attribution": attribution,
        "proactive_insights": build_proactive_insights(
            {"totals": totals, "projects": project_rows}, business_flows, attribution,
        ),
        "business_flow_status": {
            "overdue_receivables": business_flows["receivables"]["overdue_count"],
            "missed_collection_promises": business_flows["receivables"].get("missed_promise_count", 0),
            "disputed_receivables": business_flows["receivables"].get("disputed_count", 0),
            "promised_by_currency": business_flows["receivables"].get("promised_by_currency", []),
            "bank_unallocated": business_flows["bank_unallocated_count"],
            "pending_payment_approvals": business_flows["payment_requests"]["pending_approval"],
            "overdue_receivable_items": overdue_receivables[:20],
            "pending_payment_items": pending_payment_requests[:20],
        },
        "management_messages": messages,
        "definitions": {
            "revenue": "管理口径结算收入：平台/发行对账单的我方结算金额；不等同于未经会计复核的法定营业收入。",
            "direct_cost": "已明确归属项目的人力、外包/内容、投放、技术云及其他采购；公共费用不强行分摊。",
            "contribution": "管理口径结算收入减项目直接成本；用于经营判断，不替代会计利润。",
            "procurement_actual": "只有逐事件验收进入本期管理实际成本；历史明确验收汇总保留并提示补证据。交付、发票和付款均不替代验收。",
            "committed_contribution": "管理口径结算收入减已发生成本，再减已批准订单尚未验收的承诺；用于观察全部履约后的项目空间。",
            "payroll": "按显式主体、期间、币种、项目代码和工时/比例证据分配的工资与雇主承担成本；员工扣款及代扣税不重复增加成本。未分配部分保留为公司公共/待分配。",
            "research_labor": "研发工时只形成项目管理候选，不代表已满足研发资本化、研发费用归集或税收优惠条件。",
            "special_cost_release": "游戏授权、IP license 与服务器/云资源只按已批准政策和逐期间权利/服务证据形成费用释放候选；付款不决定成本确认，也不自动形成无形资产。",
            "kpi": "运营后台指标仅用于驱动分析；投放金额不会在已有采购费用之外重复计入项目损益。",
        },
        "data_quality": {
            "unassigned_cost": round(unassigned["direct_cost"], 2) if unassigned else 0,
            "unconverted_count": sum(len(row["unconverted"]) for row in project_rows),
            "kpi_record_count": len(kpis),
            "settlement_record_count": len(datasets.get("settlements") or []),
            "payroll_record_count": len(datasets.get("payroll_rows") or []),
            "purchase_record_count": len(datasets.get("purchases") or []),
            "labor_evidence_gap_count": sum(int(value or 0) for value in labor_gaps.values()),
            "labor_unallocated_by_entity_currency": [
                {
                    "entity_id": scope.get("entity_id"), "currency": scope.get("currency"),
                    "amount": scope.get("unallocated_project_cost"),
                }
                for scope in project_labor_costs["summary"].get("by_entity_currency") or []
                if scope.get("unallocated_project_cost")
            ],
            "plan_record_count": len(datasets.get("plan_lines") or []),
            "master_record_count": len(datasets.get("master_records") or []),
            "attribution_confidence": attribution["confidence"],
        },
    }
