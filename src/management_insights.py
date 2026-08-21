from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable


def _number(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _previous(period: str) -> str:
    year, month = map(int, period.split("-"))
    month -= 1
    if month == 0:
        year, month = year - 1, 12
    return f"{year:04d}-{month:02d}"


def _settlement_dimensions(
    rows: Iterable[dict], period: str, profile: dict | None,
) -> tuple[dict[tuple[str, str], float], list[dict]]:
    result: dict[tuple[str, str], float] = defaultdict(float)
    unconverted = []
    rates = ((((profile or {}).get("fx_policy") or {}).get("month_end_rates") or {}).get(period) or {})
    for row in rows:
        if row.get("period") == period:
            currency = str(row.get("currency") or "CNY").upper()
            rate = 1.0 if currency == "CNY" else _number(rates.get(currency))
            if not rate:
                unconverted.append({
                    "settlement_id": row.get("id"), "period": period, "currency": currency,
                    "amount": _number(row.get("settlement_amount")), "reason": "缺少该期间批准的人民币折算汇率",
                })
                continue
            result[(row.get("game") or "待识别游戏", row.get("channel") or "待识别渠道")] += (
                _number(row.get("settlement_amount")) * rate
            )
    return result, unconverted


def _kpi_by_project(rows: Iterable[dict], period: str) -> dict[str, dict]:
    result: dict[str, dict] = defaultdict(lambda: defaultdict(float))
    retention_values: dict[tuple[str, str], list[float]] = defaultdict(list)
    for row in rows:
        if row.get("period") != period or row.get("status") == "异常":
            continue
        project = row.get("project_code") or "待识别项目"
        for key in ("mau", "payers", "gross_bookings", "marketing_spend", "installs"):
            if row.get(key) is not None:
                result[project][key] += _number(row.get(key))
        for key in ("retention_d1", "retention_d7", "retention_d30"):
            if row.get(key) is not None:
                retention_values[(project, key)].append(_number(row[key]))
    for (project, key), values in retention_values.items():
        result[project][key] = sum(values) / len(values)
    return {project: dict(values) for project, values in result.items()}


def build_change_attribution(
    datasets: dict[str, list[dict]], period: str, profile: dict | None = None,
) -> dict:
    previous = _previous(period)
    current_dims, current_unconverted = _settlement_dimensions(datasets.get("settlements") or [], period, profile)
    previous_dims, previous_unconverted = _settlement_dimensions(datasets.get("settlements") or [], previous, profile)
    dimension_rows = []
    for key in sorted(set(current_dims) | set(previous_dims)):
        current, prior = current_dims.get(key, 0.0), previous_dims.get(key, 0.0)
        dimension_rows.append({
            "game": key[0], "channel": key[1], "currency": "CNY", "current": round(current, 2),
            "previous": round(prior, 2), "change": round(current - prior, 2),
        })
    dimension_rows.sort(key=lambda item: abs(item["change"]), reverse=True)
    total_change = round(sum(item["change"] for item in dimension_rows), 2)

    current_kpis = _kpi_by_project(datasets.get("game_kpis") or [], period)
    previous_kpis = _kpi_by_project(datasets.get("game_kpis") or [], previous)
    driver_rows = []
    for project in sorted(set(current_kpis) & set(previous_kpis)):
        current, prior = current_kpis[project], previous_kpis[project]
        current_mau, prior_mau = current.get("mau"), prior.get("mau")
        current_payers, prior_payers = current.get("payers"), prior.get("payers")
        current_gross, prior_gross = current.get("gross_bookings"), prior.get("gross_bookings")
        if not all(value not in {None, 0} for value in (current_mau, prior_mau, current_payers, prior_payers, current_gross, prior_gross)):
            continue
        prior_rate = prior_payers / prior_mau
        current_rate = current_payers / current_mau
        prior_arppu = prior_gross / prior_payers
        current_arppu = current_gross / current_payers
        mau_effect = (current_mau - prior_mau) * prior_rate * prior_arppu
        payer_rate_effect = current_mau * (current_rate - prior_rate) * prior_arppu
        arppu_effect = current_mau * current_rate * (current_arppu - prior_arppu)
        explained = mau_effect + payer_rate_effect + arppu_effect
        driver_rows.append({
            "project": project, "gross_change": round(current_gross - prior_gross, 2),
            "mau_effect": round(mau_effect, 2), "payer_rate_effect": round(payer_rate_effect, 2),
            "arppu_effect": round(arppu_effect, 2), "explained": round(explained, 2),
            "residual": round(current_gross - prior_gross - explained, 2),
            "marketing_change": round(current.get("marketing_spend", 0) - prior.get("marketing_spend", 0), 2),
            "retention_d7_change": (
                round(current["retention_d7"] - prior["retention_d7"], 4)
                if current.get("retention_d7") is not None and prior.get("retention_d7") is not None else None
            ),
        })
    coverage = {
        "comparable_settlement_dimensions": len(dimension_rows),
        "projects_with_full_kpi_decomposition": len(driver_rows),
        "current_kpi_records": sum(row.get("period") == period for row in datasets.get("game_kpis") or []),
        "previous_kpi_records": sum(row.get("period") == previous for row in datasets.get("game_kpis") or []),
    }
    unconverted = current_unconverted + previous_unconverted
    limitations = [] if driver_rows else ["缺少连续两个月同项目的MAU、付费人数和流水，不能完成量价归因"]
    if unconverted:
        limitations.append(f"有 {len(unconverted)} 笔外币结算因缺少对应期间汇率未纳入人民币变化桥接")
    return {
        "period": period, "previous_period": previous, "settlement_change": total_change,
        "settlement_change_currency": "CNY", "unconverted_settlements": unconverted,
        "dimension_contributors": dimension_rows, "operating_drivers": driver_rows,
        "coverage": coverage,
        "confidence": round(min(0.95, 0.45 + 0.08 * min(len(dimension_rows), 3) + 0.15 * min(len(driver_rows), 2)), 2),
        "limitations": limitations,
        "method": "结算金额先按各期批准汇率折算为人民币，再按游戏/渠道桥接；运营流水按 MAU × 付费率 × ARPPU 的顺序分解，剩余差异单独保留。",
    }


def build_proactive_insights(
    bp: dict, business_flows: dict, attribution: dict,
) -> list[dict]:
    signals = []
    projects = [item for item in bp.get("projects") or [] if item.get("revenue", 0) > 0]
    total_revenue = _number((bp.get("totals") or {}).get("revenue"))
    if projects and total_revenue:
        largest = max(projects, key=lambda item: item.get("revenue") or 0)
        share = _number(largest.get("revenue")) / total_revenue
        if share >= 0.65:
            signals.append({
                "severity": "高" if share >= 0.8 else "中", "type": "收入集中",
                "title": f"{largest.get('project_name')} 占管理口径收入 {share:.1%}",
                "finding": "公司人少不等于金额风险小；单一项目波动会直接影响现金和利润。",
                "recommendation": "分别做该项目收入下降20%与回款延迟30天情景，确认现金缓冲。",
                "confidence": 0.92, "evidence": ["项目结算收入", "公司管理口径总收入"],
            })
    overdue = (business_flows.get("receivables") or {}).get("overdue_count", 0)
    if overdue:
        signals.append({
            "severity": "高", "type": "回款", "title": f"有 {overdue} 笔应收已逾期",
            "finding": "收入成立不代表现金已到账，逾期会放大高流水公司的资金错配。",
            "recommendation": "先核对渠道账期、争议和扣款，再按金额及逾期天数排序催收。",
            "confidence": 0.95, "evidence": ["结算应收", "资金核销台账"],
        })
    for row in attribution.get("operating_drivers") or []:
        if row.get("marketing_change", 0) > 0 and row.get("retention_d7_change") is not None and row["retention_d7_change"] < -0.02:
            signals.append({
                "severity": "中", "type": "投放质量", "title": f"{row['project']} 投放增加但7日留存下降",
                "finding": f"投放变化 {row['marketing_change']:,.0f}，7日留存变化 {row['retention_d7_change']:.1%}。",
                "recommendation": "不要只看新增和流水；按渠道/素材拆分留存与回收，低质量来源先降量。",
                "confidence": 0.82, "evidence": ["经营KPI连续期间"],
            })
    signals.extend({
        "severity": item.get("severity"), "type": item.get("type"), "title": item.get("type"),
        "finding": f"共 {item.get('count')} 项", "recommendation": item.get("action"),
        "confidence": 0.95, "evidence": ["业务闭环台账"],
    } for item in business_flows.get("alerts") or [])
    rank = {"高": 0, "中": 1, "机会": 2, "正常": 3}
    signals.sort(key=lambda item: rank.get(item.get("severity"), 9))
    return signals
