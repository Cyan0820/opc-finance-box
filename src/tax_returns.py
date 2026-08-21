from __future__ import annotations

from calendar import monthrange
from typing import Any, Iterable


VAT_SOURCE = "https://fgk.chinatax.gov.cn/zcfgk/c100012/c5247507/content.html"
VAT_2026_SOURCE = "https://guizhou.chinatax.gov.cn/wjjb/zcfgk/szfl/zzs/202602/t20260203_89367768.html"
CIT_SOURCE = "https://fgk.chinatax.gov.cn/zcfgk/c100012/c5241820/content.html"
STAMP_SOURCE = "https://fgk.chinatax.gov.cn/zcfgk/c100015/c5200988/content.html"
IIT_SOURCE = "https://12366.chinatax.gov.cn/bzds/068/068.html"


FORM_VERSIONS = {
    "VAT-RETURN": [
        {
            "effective_from": "2026-02", "version": "国家税务总局公告2026年第6号填报要求",
            "source": VAT_2026_SOURCE,
            "note": "2026年2月1日起暂沿用现有申报表及附列资料，并按第6号公告调整后的栏次要求填报。",
        },
        {
            "effective_from": "2025-02", "version": "国家税务总局公告2025年第2号填报要求",
            "source": VAT_SOURCE, "note": "适用于2025年2月至2026年1月税款所属期。",
        },
    ],
    "A200000": [{
        "effective_from": "2025-10", "version": "国家税务总局公告2025年第17号",
        "source": CIT_SOURCE, "note": "新增职工薪酬、出口方式等附报事项，并调整预缴税款计算项目。",
    }],
}


def _form_version(form_code: str, period: str) -> dict[str, str]:
    candidates = [item for item in FORM_VERSIONS.get(form_code, []) if item["effective_from"] <= period]
    if not candidates:
        return {"effective_from": "", "version": "待核对当前官方版本", "source": "", "note": "系统未配置该期间表单版本"}
    return max(candidates, key=lambda item: item["effective_from"])


def _period_bounds(period: str) -> tuple[str, str]:
    year, month = (int(value) for value in period.split("-"))
    return f"{year:04d}-{month:02d}-01", f"{year:04d}-{month:02d}-{monthrange(year, month)[1]:02d}"


def _money(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def _field(code: str, name: str, value: Any = None, source: str = "", status: str = "待补") -> dict[str, Any]:
    return {"code": code, "name": name, "value": _money(value) if isinstance(value, (int, float)) else value,
            "source": source, "status": status}


def _checks(*rows: tuple[str, bool | None, str]) -> list[dict[str, Any]]:
    return [{"name": name, "passed": passed, "note": note} for name, passed, note in rows]


def build_tax_returns(
    records: Iterable[dict], period: str, company_profile: dict[str, Any] | None = None,
    purchases: Iterable[dict] | None = None, invoices: Iterable[dict] | None = None,
    payroll_rows: Iterable[dict] | None = None, period_report: dict | None = None,
    financial_statements: dict | None = None, cross_border: dict | None = None,
) -> dict[str, Any]:
    """Build traceable filing workpapers without pretending management data is a filed tax position."""
    profile = company_profile or {}
    records = [row for row in records if row.get("period") == period]
    purchases = [row for row in (purchases or []) if not row.get("order_date") or str(row.get("order_date")).startswith(period)]
    invoices = [row for row in (invoices or []) if not row.get("invoice_date") or str(row.get("invoice_date")).startswith(period)]
    payroll = [row for row in (payroll_rows or []) if not row.get("period") or row.get("period") == period]
    payroll_total = sum(float(row.get("gross_salary") or 0) for row in payroll)
    iit_total = sum(float(row.get("calculated_iit") or 0) for row in payroll)
    period_report = period_report or {}
    statements = financial_statements or {}
    cross_border = cross_border or {}
    start, end = _period_bounds(period)
    vat_version = _form_version("VAT-RETURN", period)
    cit_version = _form_version("A200000", period)
    identity_missing = [name for key, name in (("credit_code", "统一社会信用代码"), ("registered_city", "主管税务地区"))
                        if not profile.get(key)]
    rates = ((profile.get("fx_policy") or {}).get("month_end_rates") or {}).get(period) or {}
    sales_candidate = 0.0
    missing_fx = set()
    for row in records:
        currency = row.get("currency") or "CNY"
        amount = float(row.get("settlement_amount") or 0)
        if currency == "CNY":
            sales_candidate += amount
        elif rates.get(currency):
            sales_candidate += amount * float(rates[currency])
        else:
            missing_fx.add(currency)
    deductible_invoice_rows = [row for row in invoices if row.get("deduction_status") in {"已抵扣", "已勾选", "勾选确认", "已确认抵扣"}
                               and not row.get("anomalies")]
    input_vat_candidate = sum(float(row.get("tax_amount") or 0) for row in deductible_invoice_rows)
    overseas_unreviewed = not cross_border.get("all_reviewed", True)
    vat_type = profile.get("vat_taxpayer_type") or "待配置"
    vat_blockers = identity_missing + ([] if vat_type != "待配置" else ["增值税纳税人类型"])
    vat_blockers += ([f"{','.join(sorted(missing_fx))}折算汇率"] if missing_fx else [])
    vat_blockers += (["海外渠道交易税务分类与证据"] if overseas_unreviewed else [])
    vat_blockers += ["按发票/交易性质拆分销售额及适用税率", "税务数字账户销项、进项数据"]
    vat_fields = [
        _field("VAT-SALES-CAND", "管理台账结算收入候选额", sales_candidate, "渠道结算台账（非申报口径）", "候选"),
        _field("VAT-OUTPUT", "销项税额/应纳税额", None, "需按应税交易分类后计算"),
        _field("VAT-INPUT", "已确认抵扣进项税额", input_vat_candidate, "无异常且抵扣状态已确认的发票", "候选" if deductible_invoice_rows else "待补"),
        _field("VAT-PREPAID", "本期预缴税额", None, "税务系统/缴款凭证"),
        _field("VAT-PAYABLE", "本期应补（退）税额", None, "申报表内计算"),
        _field("SURTAX-CITY", "城市维护建设税", None, "以确认的增值税税额及属地税率计算"),
        _field("SURTAX-EDU", "教育费附加", None, "以确认的增值税税额计算"),
        _field("SURTAX-LOCAL", "地方教育附加", None, "以确认的增值税税额及属地规则计算"),
    ]
    domestic_sales = sum(
        float(row.get("settlement_amount") or 0) * (1 if (row.get("currency") or "CNY") == "CNY" else float(rates.get(row.get("currency")) or 0))
        for row in records if row.get("scope") != "海外"
    )
    overseas_sales = max(0.0, sales_candidate - domestic_sales)
    sales_schedule = [
        {"category": "境内游戏服务/数字内容候选", "amount": round(domestic_sales, 2), "tax_treatment": "待按交易性质和发票拆分", "status": "候选"},
        {"category": "海外渠道候选", "amount": round(overseas_sales, 2), "tax_treatment": "待按消费地、履约角色及跨境证据复核", "status": "候选"},
    ]
    input_schedule = [{
        "invoice_number": row.get("invoice_number"), "seller": row.get("seller_name"),
        "amount_ex_tax": _money(row.get("amount_ex_tax")), "tax_amount": _money(row.get("tax_amount")),
        "deduction_status": row.get("deduction_status"), "status": "抵扣候选",
    } for row in deductible_invoice_rows]
    vat = {
        "id": "VAT-GENERAL" if vat_type == "一般纳税人" else "VAT-SMALL",
        "name": f"增值税及附加税费申报表（{vat_type if vat_type != '待配置' else '待确定适用表'}）",
        "form_code": "VAT-RETURN", "version": vat_version["version"], "effective_from": vat_version["effective_from"],
        "version_note": vat_version["note"], "period": period,
        "frequency": profile.get("vat_filing_frequency") or "待配置", "status": "待补资料" if vat_blockers else "待复核",
        "transport": "工作底稿，不可直接上传", "fields": vat_fields,
        "schedules": [{"name": "销售分类候选", "rows": sales_schedule}, {"name": "进项抵扣候选", "rows": input_schedule}],
        "blockers": vat_blockers,
        "checks": _checks(("主体信息完整", not identity_missing, "补齐税号和主管税务地区"),
                          ("海外渠道口径完成", not overseas_unreviewed, "不能按收款币种判断"),
                          ("申报销售额已分类", False, "结算收入不是当然的增值税销售额")),
        "agent_position": "先按游戏、渠道、交易性质和消费地建立税务映射；证据不足的海外收入保持待判断，不自动套税率。",
        "review_role": "税务服务机构/有权申报人", "official_source": vat_version["source"],
    }

    profit = _money(period_report.get("profit_before_tax_draft"))
    cit_method = profile.get("cit_collection_method") or "查账征收"
    cit_blockers = identity_missing + (["本年累计会计利润及税会差异"] if profit is not None else ["完整利润表草稿"])
    cit_blockers += ["季度平均从业人数与资产总额", "以前年度可弥补亏损", "优惠及附报事项确认"]
    cit = {
        "id": "CIT-A200000", "name": "A200000 企业所得税月（季）度预缴纳税申报表（A类）",
        "form_code": "A200000", "version": cit_version["version"], "effective_from": cit_version["effective_from"],
        "version_note": cit_version["note"], "period": period,
        "frequency": profile.get("cit_filing_frequency") or "季度", "status": "不适用" if cit_method != "查账征收" else "待补资料",
        "transport": "工作底稿，不可直接上传", "fields": [
            _field("CIT-PROFIT-PERIOD", "本月税前利润草稿（辅助核对）", profit, "总账试算/利润表草稿", "候选" if profit is not None else "待补"),
            _field("CIT-L01-REVENUE", "第1行 营业收入（本年累计）", None, "本年累计利润表"),
            _field("CIT-L02-COST", "第2行 营业成本（本年累计）", None, "本年累计利润表"),
            _field("CIT-L03-TAXES", "第3行 税金及附加（本年累计）", None, "本年累计利润表"),
            _field("CIT-L04-SALES", "第4行 销售费用（本年累计）", None, "本年累计利润表"),
            _field("CIT-L05-ADMIN", "第5行 管理费用（本年累计）", None, "本年累计利润表"),
            _field("CIT-L06-RD", "第6行 研发费用（本年累计）", None, "本年累计利润表/研发辅助账"),
            _field("CIT-L07-FINANCE", "第7行 财务费用（本年累计）", None, "本年累计利润表"),
            _field("CIT-L08-OTHER-INCOME", "第8行 其他收益（本年累计）", None, "本年累计利润表"),
            _field("CIT-L15-OPERATING", "第15行 营业利润（本年累计）", None, "本年累计利润表"),
            _field("CIT-L16-NONOP-INCOME", "第16行 营业外收入（本年累计）", None, "本年累计利润表"),
            _field("CIT-L17-NONOP-EXPENSE", "第17行 营业外支出（本年累计）", None, "本年累计利润表"),
            _field("CIT-PROFIT-YTD", "本年累计利润总额", None, "累计利润表"),
            _field("CIT-ADJUST", "特定业务计算的应纳税所得额", None, "税会差异台账"),
            _field("CIT-TAXABLE", "实际利润额/应纳税所得额", None, "A200000表内口径"),
            _field("CIT-RATE", "税率", None, "纳税主体及优惠资格"),
            _field("CIT-PAYABLE", "本期应补（退）所得税额", None, "A200000表内计算"),
            _field("CIT-PAYROLL-COST", "已计入成本费用的职工薪酬", payroll_total, "工资台账/已过账总账", "候选" if payroll else "待补"),
            _field("CIT-PAYROLL-PAID", "实际支付给职工的应付职工薪酬", None, "工资付款核销/银行流水"),
            _field("CIT-EXPORT-MODE", "出口方式附报事项", None, "自营/委托/代理出口业务事实（如适用）"),
        ], "schedules": [], "blockers": cit_blockers,
        "checks": _checks(("适用A类表", cit_method == "查账征收", f"当前征收方式：{cit_method}"),
                          ("累计利润可用", False, "预缴表使用累计口径，不能只取单月利润"),
                          ("小型微利企业资格已核验", profile.get("micro_enterprise_candidate") in {"是", "否", "已核验"}, "不能仅凭公司规模猜测")),
        "agent_position": "以累计会计利润为起点维护税会差异；小型微利、亏损弥补和研发优惠分别留证，不把单月利润直接当应纳税所得额。",
        "review_role": "税务服务机构/有权申报人", "official_source": cit_version["source"],
    }

    stamp_rows = [{
        "source_id": row.get("po_number") or row.get("id") or "待编号", "document_name": row.get("item") or "采购事项",
        "counterparty": row.get("vendor") or "待补", "signed_date": row.get("order_date") or "待补",
        "tax_item": "待按合同实质分类", "amount_ex_vat": None, "tax_rate": None, "tax_amount": None,
        "status": "候选—需合同台账确认",
    } for row in purchases]
    stamp = {
        "id": "STAMP-A01103", "name": "A01103 印花税税源明细表", "form_code": "A01103",
        "version": "国家税务总局公告2022年第14号", "period": period, "frequency": "按属地核定的季/年/次",
        "status": "待补资料", "transport": "可生成税源候选；取得属地电子税务局模板后才可生成导入文件",
        "fields": [_field("STAMP-COUNT", "采购事项候选数", len(stamp_rows), "采购台账", "候选")],
        "schedules": stamp_rows, "blockers": identity_missing + ["合同/应税凭证台账", "应税凭证类型与书立日期", "不含增值税计税金额", "属地申报期限和导入模板"],
        "checks": _checks(("存在合同税源台账", False, "采购PO不能代替应税凭证"),
                          ("属地期限已配置", False, "印花税期限由省级税务机关结合实际确定")),
        "agent_position": "采购台账只用于发现候选；是否属于买卖、承揽、技术、租赁或其他税目，要回到应税凭证事实确认。",
        "review_role": "税务服务机构/采购经办人", "official_source": STAMP_SOURCE,
    }

    iit_rows = [{"employee": row.get("employee_masked") or "匿名人员", "income_type": "工资薪金",
                 "current_income": _money(row.get("gross_salary")), "special_deduction": _money(row.get("special_deduction")),
                 "other_deduction": _money(row.get("other_deduction")), "tax_candidate": _money(row.get("calculated_iit")),
                 "status": "异常" if row.get("anomalies") else "待实名申报数据复核"} for row in payroll]
    payroll_issues = sum(bool(row.get("anomalies")) for row in payroll)
    iit = {
        "id": "IIT-WITHHOLD", "name": "个人所得税扣缴申报表（工资薪金底稿）", "form_code": "IIT-WITHHOLD",
        "version": "现行扣缴申报口径", "period": period, "frequency": "月度", "status": "待补资料" if not payroll or payroll_issues else "待复核",
        "transport": "隐私脱敏工作底稿；不可直接导入自然人电子税务局", "fields": [
            _field("IIT-PEOPLE", "本期人员数", len(payroll), "工资台账", "候选" if payroll else "待补"),
            _field("IIT-INCOME", "本期工资薪金收入", payroll_total, "工资台账", "候选" if payroll else "待补"),
            _field("IIT-TAX", "本期应扣缴税额试算", iit_total, "累计预扣法试算", "候选" if payroll else "待补"),
        ], "schedules": iit_rows,
        "blockers": ([] if payroll else ["工资及累计扣除台账"]) + ([f"{payroll_issues}名人员试算异常"] if payroll_issues else []) +
                    ["实名身份、居民身份和任职受雇信息", "专项附加扣除在申报端确认", "个人劳务等非工资所得台账（如有）"],
        "checks": _checks(("工资台账已导入", bool(payroll), "需全员全额申报"),
                          ("累计预扣试算无异常", bool(payroll) and not payroll_issues, "表内税额与Agent试算应核对")),
        "agent_position": "工资与个人劳务分开；系统前端保持匿名，提交前在受控环境补齐实名字段并核对累计扣除。",
        "review_role": "薪酬负责人/有权申报人", "official_source": IIT_SOURCE,
    }

    bs, inc = statements.get("balance_sheet") or {}, statements.get("income_statement") or {}
    fs_ready = bool(statements.get("opening_available")) and bool(bs.get("balanced"))
    financial = {
        "id": "FIN-STATEMENTS", "name": f"财务报表报送（{profile.get('accounting_standard') or '待配置'}）",
        "form_code": "FIN-STATEMENTS", "version": "按主管税务机关核定报送口径", "period": period,
        "frequency": "月/季/年（按税务认定）", "status": "待复核" if fs_ready else "待补资料",
        "transport": "报表草稿，不可直接上传", "fields": [
            _field("FS-ASSETS", "资产总额", bs.get("assets"), "资产负债表草稿", "候选" if fs_ready else "待补"),
            _field("FS-LIAB-EQUITY", "负债和所有者权益合计", bs.get("liabilities_and_equity"), "资产负债表草稿", "候选" if fs_ready else "待补"),
            _field("FS-REVENUE", "本期营业收入", inc.get("revenue"), "利润表草稿", "候选"),
            _field("FS-PROFIT", "本期利润总额", inc.get("profit_before_tax"), "利润表草稿", "候选"),
        ], "schedules": [], "blockers": ([] if fs_ready else ["经确认的期初余额和勾稽平衡资产负债表"]) + ["主管税务机关核定的报表种类与报送周期"],
        "checks": _checks(("资产负债表平衡", bool(bs.get("balanced")) if statements.get("opening_available") else None, "需期初余额"),
                          ("报表与申报利润勾稽", False, "企业所得税累计利润与财务报表需同口径核对")),
        "agent_position": "先保证账表一致，再做税会差异；税务报送表不能用经营分析数字代替。",
        "review_role": "会计服务机构/有权报送人", "official_source": "https://12366.chinatax.gov.cn/",
    }
    returns = [vat, cit, stamp, iit, financial]
    ready = sum(item["status"] == "待复核" for item in returns)
    return {
        "period": period, "period_start": start, "period_end": end, "company_name": profile.get("company_name") or "待配置",
        "entity_id": profile.get("entity_id") or "",
        "credit_code": profile.get("credit_code") or "", "returns": returns,
        "filing_profile": {
            "registered_city": profile.get("registered_city") or "",
            "vat_taxpayer_type": vat_type,
            "vat_filing_frequency": profile.get("vat_filing_frequency") or "待配置",
            "cit_collection_method": cit_method,
            "cit_filing_frequency": profile.get("cit_filing_frequency") or "季度",
            "shanghai_vat_pilot_status": ((profile.get("tax_policy") or {}).get("shanghai_vat_pilot_status") or "待确认"),
        },
        "summary": {"form_count": len(returns), "ready_for_review": ready,
                    "blocked": sum(item["status"] == "待补资料" for item in returns),
                    "direct_upload_ready": 0},
        "workflow": ["生成候选", "补证据/补字段", "Agent勾稽", "有权人复核", "进入申报端", "保存回执与缴款凭证"],
        "form_versions": {code: _form_version(code, period) for code in ("VAT-RETURN", "A200000")},
        "guardrail": "本工作台生成申报草稿和工作底稿；只有与主管税务机关当前表单/导入模板核对一致并经有权人确认后，才进入申报端。",
    }
