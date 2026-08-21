from __future__ import annotations

from typing import Any


SUGGESTED_QUESTIONS = [
    "这个月哪个游戏最赚钱？",
    "为什么本月收入变化？",
    "哪些钱该收还没收？",
    "今天有哪些付款等我审批？",
    "预算哪里超了？",
    "现金还能撑多久？",
    "要不要给表现最好的项目加投放？",
    "哪些成本还没分到项目？",
    "本月能不能关账？",
    "海外收入税务要怎么处理？",
]


def _money(value: Any) -> str:
    return f"{float(value or 0):,.0f} 元"


def answer_finance_question(question: str, bp: dict, finance_ops: dict | None = None, onboarding: dict | None = None) -> dict:
    question = str(question or "").strip()
    if not question:
        raise ValueError("请输入一个财务或经营问题")
    projects = [row for row in bp.get("projects") or [] if row.get("project_code") != "公司公共/待分配"]
    totals = bp.get("totals") or {}
    planning = bp.get("planning") or {}
    evidence, options, gaps, actions = [], [], [], []
    confidence = 0.72
    q = question.lower()

    if any(word in q for word in ("最赚钱", "利润最高", "贡献最高", "最好")):
        best = max(projects, key=lambda row: row.get("contribution") or 0, default=None)
        if not best:
            answer, recommendation = "当前没有足够的项目收入和成本数据做排名。", "先完成游戏项目主数据，并导入结算、工资和采购。"
            gaps.append("项目损益数据不足")
            confidence = 0.25
        else:
            answer = f"{best['project_name']} 本期管理口径直接贡献最高，为 {_money(best['contribution'])}，贡献率 {((best.get('contribution_margin') or 0)*100):.1f}%。"
            recommendation = "把它作为优先复盘对象，但先检查收入是否可持续、回款是否到账、KPI是否同步改善，再决定追加资源。"
            evidence = [
                {"metric": "管理口径结算收入", "value": _money(best["revenue"]), "source": "渠道/发行对账单"},
                {"metric": "项目直接成本", "value": _money(best["direct_cost"]), "source": "工资与采购台账"},
                {"metric": "项目直接贡献", "value": _money(best["contribution"]), "source": "收入减直接成本"},
            ]
            options = [
                {"name": "小步追加", "benefit": "用一小段预算验证增长是否可复制", "cost": "增长速度较慢", "recommended": True},
                {"name": "立即扩张", "benefit": "抢占窗口期", "cost": "固定成本、投放和回款风险同步放大", "recommended": False},
                {"name": "维持现状", "benefit": "保留现金", "cost": "可能错过增长窗口", "recommended": False},
            ]
            actions = ["复盘该项目渠道/区域收入构成", "查看MAU、付费率、ARPPU和留存", "做追加投入的保守/基准情景"]
            confidence = 0.9 if best.get("evidence_count", 0) >= 3 else 0.7
    elif any(word in q for word in ("为什么", "原因", "收入变化", "利润变化", "下降", "增长")):
        change = bp.get("change_vs_previous") or {}
        attribution = bp.get("change_attribution") or {}
        if change.get("revenue") is None:
            answer = "系统还没有可比的上月数据，因此不能把变化归因给某个项目或经营指标。"
            recommendation = "先补至少连续两个月结算；若要回答经营原因，再补MAU、付费人数、流水和投放。"
            gaps.extend(["缺少连续期间", "可能缺少经营KPI"])
            confidence = 0.3
        else:
            direction = "增加" if change["revenue"] >= 0 else "减少"
            answer = f"本期管理口径结算收入较上期{direction} {_money(abs(change['revenue']))}；直接贡献变化 {_money(change.get('contribution') or 0)}。"
            contributors = (attribution.get("dimension_contributors") or [])[:3]
            drivers = (attribution.get("operating_drivers") or [])[:3]
            if contributors:
                top = contributors[0]
                answer += f" 最大金额贡献来自 {top['game']} / {top['channel']}，变化 {_money(top['change'])}。"
            if drivers:
                driver = drivers[0]
                parts = sorted([
                    ("MAU", driver["mau_effect"]), ("付费率", driver["payer_rate_effect"]),
                    ("ARPPU", driver["arppu_effect"]),
                ], key=lambda item: abs(item[1]), reverse=True)
                answer += f" 运营流水中，{driver['project']} 最大驱动是{parts[0][0]}，约解释 {_money(parts[0][1])}；未解释差异 {_money(driver['residual'])}。"
                recommendation = "先处理最大可验证驱动；若投放增加但留存走弱，优先降低质量渠道，而不是继续按总量扩投。"
            else:
                recommendation = "金额变化已按游戏和渠道定位；因缺少连续两个月同口径KPI，暂不对活跃、付费率或ARPPU作原因断言。"
                gaps.extend(attribution.get("limitations") or ["缺少连续经营KPI"])
            evidence = [
                {"metric": f"{row['game']} / {row['channel']}", "value": _money(row["change"]), "source": "结算金额月环比桥接"}
                for row in contributors
            ] + [
                {"metric": f"{row['project']} 运营流水变化", "value": _money(row["gross_change"]), "source": "MAU×付费率×ARPPU分解"}
                for row in drivers
            ]
            actions = ["核对最大金额贡献项的版本和渠道事件", "处理最大用户驱动", "补充剩余未解释差异的业务事件"]
            confidence = attribution.get("confidence") or confidence
    elif any(word in q for word in ("该收", "没收", "应收", "回款", "逾期", "催收")):
        flow = bp.get("business_flow_status") or {}
        overdue = flow.get("overdue_receivable_items") or []
        if not overdue:
            answer = "当前没有识别到逾期未收款；这不代表所有渠道都已结清，还要确认结算单和银行流水是否已经完整导入。"
            recommendation = "保持按渠道账期核销；新增结算或银行流水后让 Agent 重新检查。"
            confidence = 0.7 if bp.get("data_quality", {}).get("settlement_record_count") else 0.3
        else:
            top = overdue[0]
            missed = int(flow.get("missed_collection_promises") or 0)
            disputed = int(flow.get("disputed_receivables") or 0)
            answer = (
                f"有 {len(overdue)} 笔应收已逾期。最先处理 {top.get('game')} / {top.get('channel')}："
                f"尚未收回 {float(top.get('outstanding') or 0):,.2f} {top.get('currency')}，逾期 {top.get('days_overdue')} 天。"
            )
            if missed or disputed:
                answer += f" 其中承诺逾期 {missed} 笔、争议处理中 {disputed} 笔。"
            recommendation = "先核对渠道账期、回款承诺、争议和银行未认领入账，再对真实逾期项催收；不要把不同币种合计成一个金额。"
            evidence = [{
                "metric": f"{row.get('game')} / {row.get('channel')}",
                "value": f"{float(row.get('outstanding') or 0):,.2f} {row.get('currency')}，逾期 {row.get('days_overdue')} 天，{row.get('collection_status') or '待跟进'}",
                "source": "结算应收、催收记录与银行核销台账",
            } for row in overdue[:8]]
            actions = ["核对最老逾期项的渠道账期", "检查未认领银行入账", "记录催收责任人与承诺回款日"]
            confidence = 0.95
    elif any(word in q for word in ("等我审批", "待审批", "待我批", "付款审批", "哪些付款", "要批")):
        items = (bp.get("business_flow_status") or {}).get("pending_payment_items") or []
        if not items:
            answer = "当前没有待批准的付款申请。"
            recommendation = "如业务确有待付款事项，先从已验收采购、已批准报销或工资批次生成付款申请，不要直接从银行流水倒推审批。"
            confidence = 0.9
        else:
            answer = f"当前有 {len(items)} 笔付款待批准；Agent 已按证据完整性列出，批准仍由有权人完成。"
            recommendation = "优先处理影响交付或到期的申请；验收、发票、预算或收款信息缺失的先退回补证，不因金额大就自动否决。"
            evidence = [{
                "metric": row.get("purpose") or row.get("target_id") or row.get("id"),
                "value": f"{float(row.get('amount') or 0):,.2f} {row.get('currency') or 'CNY'}",
                "source": "付款申请及其关联证据",
            } for row in items[:8]]
            actions = ["打开付款证据摘要", "确认验收与付款条件", "批准或退回并记录理由"]
            confidence = 0.96
    elif any(word in q for word in ("预算", "超支", "超了", "差异")):
        unfavorable = [row for row in bp.get("variance") or [] if not row.get("favorable")]
        if not unfavorable:
            answer = "当前期间没有发现不利预算差异，或尚未导入本期预算。"
            recommendation = "确认预算版本和情景一致；没有预算时先导入基准预算，不要把0预算当作节约。"
            confidence = 0.55 if bp.get("variance") else 0.3
        else:
            unfavorable.sort(key=lambda row: abs(row.get("variance") or 0), reverse=True)
            top = unfavorable[0]
            answer = f"最大不利差异在“{top['category']}”：实际 {_money(top['actual'])}，预算 {_money(top['budget'])}，差异 {_money(top['variance'])}。"
            recommendation = "先区分永久超支、跨月错位和未纳入预算的已承诺支出；只有永久超支才需要砍预算或补预算。"
            evidence = [{"metric": row["category"], "value": _money(row["variance"]), "source": "预算与实际差异"} for row in unfavorable[:5]]
            options = [
                {"name": "冻结新增支出", "benefit": "最快控制现金", "cost": "可能影响版本交付", "recommended": False},
                {"name": "逐项纠偏", "benefit": "保留关键投入并处理真正超支", "cost": "需要负责人逐项确认", "recommended": True},
            ]
            actions = ["查看超支明细及责任项目", "确认是否仅为付款时点差", "刷新滚动预测"]
            confidence = 0.88
    elif any(word in q for word in ("现金", "跑道", "撑多久", "没钱")):
        runway, breach = planning.get("runway_months"), planning.get("buffer_breach_period")
        if planning.get("opening_cash_cny") is None:
            answer = "目前不能可靠计算现金跑道，因为缺少真实期初现金或银行余额。"
            recommendation = "优先导入带余额的银行流水，或在公司财务档案填写预测起点现金。"
            gaps.append("缺少真实期初现金")
            confidence = 0.2
        else:
            answer = "基准情景内现金未转负。" if runway is None else f"按当前基准情景，现金约在 {runway} 个月后转负。"
            if breach:
                answer += f" {breach} 会先跌破最低现金缓冲。"
            recommendation = "同时看保守情景，并把工资、税款、核心服务器和已签采购设为刚性支出；不要只看银行余额。"
            last = (planning.get("forecast") or [{}])[-1]
            evidence = [
                {"metric": "预测起点现金", "value": _money(planning.get("opening_cash_cny")), "source": "银行余额/公司财务档案"},
                {"metric": "未付采购承诺", "value": _money(planning.get("unpaid_purchase_commitments")), "source": "采购台账"},
                {"metric": "预测期末现金", "value": _money(last.get("ending_cash")), "source": "基准滚动预测"},
            ]
            options = [
                {"name": "保现金", "benefit": "延长跑道", "cost": "增长和交付可能放缓", "recommended": breach is not None},
                {"name": "维持投入", "benefit": "保留增长速度", "cost": "需要更确定的回款和备用资金", "recommended": breach is None},
            ]
            confidence = 0.85
    elif any(word in q for word in ("加投放", "追加投放", "扩大投放", "招人", "扩编", "外包")):
        best = max(projects, key=lambda row: row.get("contribution") or 0, default=None)
        if not best:
            answer, recommendation = "目前还没有足够项目损益支持追加资源。", "先完成项目收入、直接成本和经营KPI闭环。"
            confidence = 0.25
        else:
            roas = (best.get("kpis") or {}).get("gross_roas")
            answer = f"财务结果上，{best['project_name']} 有 {_money(best['contribution'])} 的本期直接贡献。"
            if roas is not None:
                answer += f" 运营口径流水ROAS约 {roas:.2f}x。"
            else:
                answer += " 但缺少可用的投放效率数据，不能直接推导应该扩投。"
                gaps.append("缺少投放、安装和回收数据")
            recommendation = "倾向小步追加并设置止损线；固定扩编应晚于外包/短周期投放试验，除非未来两个版本的工作量已锁定。"
            options = [
                {"name": "小步投放试验", "benefit": "可快速验证边际回报", "cost": "需要按渠道追踪回收", "recommended": True},
                {"name": "先用外包", "benefit": "成本可变、适合版本高峰", "cost": "知识沉淀和质量控制较弱", "recommended": False},
                {"name": "直接招人", "benefit": "长期能力沉淀", "cost": "固定成本高且退出慢", "recommended": False},
            ]
            actions = ["定义追加金额和止损指标", "跟踪7/30日回收与留存", "纳入保守现金情景"]
            confidence = 0.75 if roas is not None else 0.55
    elif any(word in q for word in ("分到项目", "待分配", "归属", "公共成本")):
        amount = bp.get("data_quality", {}).get("unassigned_cost") or 0
        answer = f"本期有 {_money(amount)} 的成本处于公司公共/待分配。"
        recommendation = "先补人员项目比例和采购项目编码；无法合理归属的真正公共费用保持公共，不做武断分摊。"
        evidence = [{"metric": "未归属直接成本", "value": _money(amount), "source": "工资与采购项目字段"}]
        actions = ["检查工资项目字段", "检查采购项目编码", "复核跨项目人员分摊比例合计为100%"]
        confidence = 0.92
    elif any(word in q for word in ("关账", "月结")):
        close = (finance_ops or {}).get("close") or {}
        blockers = [task for task in close.get("tasks") or [] if task.get("status") == "阻塞"]
        answer = "本月可以进入人工复核关账。" if close and not blockers else f"本月暂不建议关账，有 {len(blockers)} 个阻塞事项。"
        recommendation = "先处理阻塞任务并接受可入账凭证草稿；最终关账由公司负责人或受权会计确认。"
        evidence = [{"metric": task.get("name"), "value": "阻塞", "source": "月结任务台"} for task in blockers[:8]]
        actions = [task.get("blockers", ["补证据"])[0] if task.get("blockers") else task.get("name") for task in blockers[:5]]
        confidence = 0.95 if finance_ops else 0.3
    elif any(word in q for word in ("海外", "跨境", "税务", "增值税")):
        answer = "海外收款不等于自动免税或零税率；需要按合同主体、服务内容、消费地/履约事实和凭证链判断。"
        recommendation = "Agent倾向先标记“待补证据”，整理渠道合同业务事实、结算单、收款和消费地证据，再交会计/税务服务机构复核具体申报口径。"
        evidence = [{"metric": "判断原则", "value": "不按币种直接判税", "source": "税务资料包与跨境渠道复核"}]
        options = [
            {"name": "先按待补证据", "benefit": "避免错误申报口径", "cost": "需要补资料并人工复核", "recommended": True},
            {"name": "凭收款币种直接判断", "benefit": "操作快", "cost": "税务风险高且不可审计", "recommended": False},
        ]
        actions = ["打开税务资料包查看缺口", "逐渠道记录复核结论与证据", "让服务机构确认后再申报"]
        confidence = 0.82
    else:
        answer = f"我暂时没有把这个问题归到已支持的财务BP场景。当前 {bp.get('period') or '所选期间'} 的管理口径收入是 {_money(totals.get('revenue'))}，直接贡献是 {_money(totals.get('contribution'))}。"
        recommendation = "可换一种问法：哪个游戏最赚钱、哪些钱没收、哪些付款待批、预算哪里超了、现金还能撑多久、要不要追加投放、哪些成本没分到项目，或本月能否关账。"
        gaps.append("当前QA为数据驱动的财务意图路由，不回答法务或开放域问题")
        confidence = 0.35

    quality = bp.get("data_quality") or {}
    if not quality.get("payroll_record_count"):
        gaps.append("缺少工资/人力台账，项目直接成本不完整")
        confidence = min(confidence, 0.6)
    if not quality.get("purchase_record_count"):
        gaps.append("缺少采购/外包台账，项目直接成本不完整")
        confidence = min(confidence, 0.6)
    if onboarding and not onboarding.get("ready_for_bp"):
        gaps.append(f"上线就绪度 {onboarding.get('readiness_score', 0)}%，BP结论可能不完整")
        confidence = min(confidence, 0.55)
    return {
        "question": question, "period": bp.get("period"), "answer": answer,
        "recommendation": recommendation, "confidence": round(confidence, 2),
        "evidence": evidence, "options": options, "data_gaps": list(dict.fromkeys(gaps)),
        "next_actions": actions, "scope": "公司内部管理口径；不替代法定账务、税务申报或法务意见",
        "suggested_questions": SUGGESTED_QUESTIONS,
    }
