from __future__ import annotations

import re
from typing import Any

from .company_profile import profile_gaps
from .game_kpis import kpi_quality
from .master_data import master_quality


def _valid_period(value: Any) -> bool:
    return bool(re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", str(value or "")))


def build_onboarding(profile: dict, datasets: dict[str, list[dict]]) -> dict:
    masters = datasets.get("master_records") or []
    master_check = master_quality(masters)
    game_codes = {row.get("code") for row in masters if row.get("record_type") == "game" and row.get("code")}
    kpi_check = kpi_quality(datasets.get("game_kpis") or [], game_codes)
    games = [row for row in masters if row.get("record_type") == "game" and row.get("status") == "可用"]
    channels = [row for row in masters if row.get("record_type") == "channel" and row.get("status") == "可用"]
    org = [row for row in masters if row.get("record_type") == "organization" and row.get("status") == "可用"]
    periods = sorted({
        str(row.get("period")) for name in ("settlements", "payroll_rows", "plan_lines", "game_kpis")
        for row in datasets.get(name, []) if _valid_period(row.get("period"))
    })
    profile_missing = profile_gaps(profile)

    def step(step_id: str, name: str, status: str, why: str, next_action: str, evidence: str, required: bool = True):
        return {
            "id": step_id, "name": name, "status": status, "why": why,
            "next_action": next_action, "evidence": evidence, "required": required,
        }

    steps = [
        step("S01", "主体与核算规则", "完成" if not profile_missing else "待补",
             "决定记账、申报、汇率与谁负责复核。",
             "在上线包的“主体配置”填写已知事实；不懂的税务字段交给服务机构确认。",
             f"{len(profile_missing)} 个配置缺口"),
        step("S02", "游戏/项目主数据", "完成" if games else "待补",
             "所有收入、人力、采购和预算必须汇总到同一项目编码。",
             "先建立项目编码、阶段、负责人和预算单元，再导业务明细。",
             f"{len(games)} 个可用项目"),
        step("S03", "渠道与结算规则", "完成" if channels else "待补",
             "同一游戏的苹果、谷歌、国内联运和发行商口径不同，需要稳定映射。",
             "维护平台、区域、币种、分成比例、结算周期和回款天数；只填业务事实。",
             f"{len(channels)} 条可用渠道规则"),
        step("S04", "组织/人力归属映射", "完成" if org else "待补",
             "人力通常是游戏公司最大投入；部门、预算单元、成本中心、项目必须一致。",
             "用匿名人员/岗位编码维护项目分摊；跨项目人员按比例拆行。",
             f"{len(org)} 条可用组织映射"),
        step("S05", "期初余额与真实现金", "完成" if datasets.get("opening_balances") else "待补",
             "没有期初资产负债和现金，就无法得到可靠资产负债表和现金跑道。",
             "导入上一期经会计确认的期末科目余额，并导入带余额的银行流水。",
             f"{len(datasets.get('opening_balances') or [])} 条期初余额"),
        step("S06", "历史实际与未结事项", "完成" if periods and datasets.get("settlements") else "待补",
             "BP需要趋势、预算差异、回款和应付承诺，而不仅是一个月流水。",
             "优先导入近12个月对账单，再补采购、银行、发票和工资；最少先有最近3个月。",
             f"覆盖 {len(periods)} 个期间；结算 {len(datasets.get('settlements') or [])} 条"),
        step("S07", "预算与滚动预测", "完成" if datasets.get("plan_lines") else "待补",
             "Actual没有目标就无法判断偏差，现金也不能向前看。",
             "导入按月、项目、类别、收支方向、情景拆分的预算/预测。",
             f"{len(datasets.get('plan_lines') or [])} 条计划明细"),
        step("S08", "经营KPI驱动", "完成" if datasets.get("game_kpis") and not kpi_check["issue_count"] else "建议补充",
             "流水变化需要拆到活跃、付费、ARPPU、留存和投放效率，才能给经营建议。",
             "从运营后台按月导DAU、MAU、新增、付费人数、流水、投放和留存；未知可留空。",
             f"{kpi_check['usable_count']} 条可用KPI；{kpi_check['issue_count']} 个问题", False),
        step("S09", "映射与质量体检", "完成" if not master_check["issue_count"] and not kpi_check["issue_count"] and games else "待补",
             "重复编码、孤儿项目和缺失归属会让项目损益悄悄算错。",
             "处理体检问题后再把BP结果用于奖金、投放或招聘决策。",
             f"主数据 {master_check['issue_count']} 个问题；KPI {kpi_check['issue_count']} 个问题"),
    ]
    required_steps = [item for item in steps if item["required"]]
    complete = sum(item["status"] == "完成" for item in required_steps)
    readiness = round(complete / len(required_steps) * 100) if required_steps else 0
    blockers = [item["name"] for item in required_steps if item["status"] != "完成"]
    return {
        "readiness_score": readiness,
        "ready_for_bp": all(item["status"] == "完成" for item in steps if item["id"] in {"S02", "S03", "S04", "S06"}),
        "ready_for_close": not any(item["id"] in {"S01", "S05", "S09"} and item["status"] != "完成" for item in steps),
        "steps": steps, "blockers": blockers,
        "master_quality": master_check, "kpi_quality": kpi_check,
        "import_order": [
            {"order": 1, "sheet": "主体配置", "destination": "公司财务档案", "mode": "覆盖已填字段"},
            {"order": 2, "sheet": "游戏项目/渠道规则/组织映射/供应商", "destination": "主数据", "mode": "按唯一编码更新"},
            {"order": 3, "sheet": "期初科目余额", "destination": "草稿总账", "mode": "按期间+科目更新"},
            {"order": 4, "sheet": "经营KPI", "destination": "财务BP", "mode": "按月+项目+渠道+区域更新"},
            {"order": 5, "sheet": "对账/采购/银行/发票/工资/预算", "destination": "各业务台账", "mode": "按稳定ID增量导入"},
        ],
        "principles": [
            "先编码与映射，后导交易；项目名称不是稳定主键。",
            "先保存原始业务事实，再由Agent计算口径；不让业务同学填会计结论。",
            "导入可重复执行：相同ID更新、不同ID追加，并保留审计记录。",
            "法务条款不进入本系统；渠道规则只保存分成、结算、币种等业务事实。",
        ],
    }
