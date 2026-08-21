from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timezone
import hashlib
import json
import math
from typing import Any, Iterable

from .general_ledger import build_financial_statements
from .tax_returns import build_tax_returns
from .tax_filing_assist import build_filing_assist
from .accounting_engine import build_adjustment_vouchers, build_expense_vouchers, posted_trial_balance
from .game_accounting import build_game_revenue_vouchers, build_revenue_recognition
from .ledger_adapters import LedgerAdapter, functional_rate, get_ledger_adapter


OFFICIAL_SOURCES = [
    {
        "id": "mof-accounting-basic-2024",
        "authority": "财政部",
        "title": "会计基础工作规范（2024）",
        "url": "https://m.mof.gov.cn/tzgg/202408/P020240801612534470745.pdf",
        "effective_date": "2024-01-01",
        "applies_to": ["原始凭证", "记账凭证", "会计账簿", "对账", "结账"],
    },
    {
        "id": "mof-small-enterprise-standard",
        "authority": "财政部",
        "title": "小企业会计准则——会计科目、主要账务处理和财务报表",
        "url": "https://kjs.mof.gov.cn/zhengcefabu/201111/P020111118325852734144.pdf",
        "effective_date": "2013-01-01",
        "applies_to": ["会计科目", "账务处理", "财务报表"],
    },
    {
        "id": "vat-law-2026",
        "authority": "全国人大常委会",
        "title": "中华人民共和国增值税法",
        "url": "https://flk.npc.gov.cn/detail?fileId=&id=ff808181927b083b0193fd65a0eb02cb",
        "effective_date": "2026-01-01",
        "applies_to": ["增值税", "跨境交易", "应税交易"],
    },
    {
        "id": "vat-transition-2026",
        "authority": "财政部、税务总局",
        "title": "增值税法施行后增值税优惠政策衔接事项公告（2026年第10号）",
        "url": "https://fgk.chinatax.gov.cn/zcfgk/c102416/c5247434/content.html",
        "effective_date": "2026-01-01",
        "applies_to": ["小规模纳税人", "起征点", "增值税优惠"],
    },
    {
        "id": "digital-invoice-2024",
        "authority": "国家税务总局",
        "title": "关于推广应用全面数字化电子发票的公告（2024年第11号）",
        "url": "https://fgk.chinatax.gov.cn/zcfgk/c100012/c5236067/content.html",
        "effective_date": "2024-12-01",
        "applies_to": ["数电发票", "发票归集", "发票查验"],
    },
    {
        "id": "cit-return-2025",
        "authority": "国家税务总局",
        "title": "关于优化企业所得税预缴纳税申报有关事项的公告（2025年第17号）",
        "url": "https://shanghai.chinatax.gov.cn/tax/zcfw/zcfgk/qysds/202507/t477034.html",
        "effective_date": "2025-10-01",
        "applies_to": ["企业所得税", "月季预缴", "查账征收"],
    },
    {
        "id": "iit-withholding-2018",
        "authority": "国家税务总局",
        "title": "个人所得税扣缴申报管理办法（试行）（2018年第61号）",
        "url": "https://www.chinatax.gov.cn/chinatax/n810341/n810765/n3359382/201812/c4182700/content.html",
        "effective_date": "2019-01-01",
        "applies_to": ["个人所得税", "全员全额申报", "次月十五日"],
    },
    {
        "id": "micro-tax-2023",
        "authority": "财政部、税务总局",
        "title": "进一步支持小微企业和个体工商户发展有关税费政策公告（2023年第12号）",
        "url": "https://fgk.chinatax.gov.cn/zcfgk/c102416/c5210453/content.html",
        "effective_date": "2023-01-01",
        "expires_date": "2027-12-31",
        "applies_to": ["小型微利企业", "六税两费", "企业所得税优惠"],
    },
    {
        "id": "electronic-voucher-2020",
        "authority": "财政部、国家档案局",
        "title": "关于规范电子会计凭证报销入账归档的通知（财会〔2020〕6号）",
        "url": "https://m.mof.gov.cn/zcfb/202005/t20200513_3512876.htm",
        "effective_date": "2020-03-23",
        "applies_to": ["电子发票", "银行回单", "报销入账", "电子归档", "防重复入账"],
    },
    {
        "id": "rd-super-deduction",
        "authority": "财政部、税务总局",
        "title": "研发费用税前加计扣除政策（长期制度性安排）",
        "url": "https://www.chinatax.gov.cn/chinatax/n810356/n3010387/c5222730/content.html",
        "effective_date": "2023-01-01",
        "applies_to": ["研发费用", "加计扣除", "委外研发", "留存备查"],
    },
]


CHART_OF_ACCOUNTS = [
    ("1002", "银行存款", "资产"),
    ("1012", "其他货币资金", "资产"),
    ("1122", "应收账款", "资产"),
    ("1123", "预付账款", "资产"),
    ("1221", "其他应收款", "资产"),
    ("1601", "固定资产", "资产"),
    ("1701", "无形资产", "资产"),
    ("1801", "长期待摊费用", "资产"),
    ("2202", "应付账款", "负债"),
    ("2211", "应付职工薪酬", "负债"),
    ("2221", "应交税费", "负债"),
    ("2241", "其他应付款", "负债"),
    ("3001", "实收资本", "权益"),
    ("3103", "本年利润", "权益"),
    ("5001", "主营业务收入", "损益"),
    ("5051", "其他业务收入", "损益"),
    ("5401", "主营业务成本", "损益"),
    ("5602", "管理费用", "损益"),
    ("5603", "财务费用", "损益"),
    ("5711", "营业外支出", "损益"),
    ("5801", "所得税费用", "损益"),
]


def _active_source(source_id: str) -> dict[str, Any]:
    return next(source for source in OFFICIAL_SOURCES if source["id"] == source_id)


def _month_end(period: str) -> str:
    year, month = (int(value) for value in period.split("-"))
    if month == 12:
        return f"{year}-12-31"
    next_month = date(year + (month == 12), 1 if month == 12 else month + 1, 1)
    return date.fromordinal(next_month.toordinal() - 1).isoformat()


def build_voucher_drafts(
    records: Iterable[dict], period: str, fx_rates: dict[str, float] | None = None,
    adapter: LedgerAdapter | None = None,
) -> list[dict]:
    records = list(records)
    fx_rates = fx_rates or {}
    adapter = adapter or get_ledger_adapter({})
    grouped: dict[tuple[str, str, str], float] = defaultdict(float)
    for record in records:
        if record.get("period") != period:
            continue
        key = (
            record.get("game") or "待识别游戏",
            record.get("channel") or "待识别渠道",
            record.get("currency") or "未知",
        )
        grouped[key] += float(record.get("settlement_amount") or 0)

    drafts = []
    for index, ((game, channel, currency), amount) in enumerate(sorted(grouped.items()), 1):
        blockers = []
        rate = functional_rate(currency, adapter, fx_rates)
        if not rate:
            blockers.append(f"缺少月末 {currency}/{adapter.functional_currency} 本位币折算汇率")
        blockers.append("需确认公司采用总额法还是净额法确认该渠道收入")
        functional_amount = round(amount * rate, 2) if rate else None
        drafts.append({
            "id": f"REV-{period.replace('-', '')}-{index:03d}",
            "date": _month_end(period),
            "type": "收入结算",
            "summary": f"确认{period} {game} / {channel}游戏结算收入",
            "source": "游戏结算标准明细",
            "source_count": sum(
                1 for record in records
                if record.get("period") == period
                and (record.get("game") or "待识别游戏") == game
                and (record.get("channel") or "待识别渠道") == channel
                and (record.get("currency") or "未知") == currency
            ),
            "original_currency": currency,
            "original_amount": round(amount, 2),
            "fx_rate": rate or None,
            "functional_currency": adapter.functional_currency,
            "functional_amount": functional_amount,
            "ledger_adapter_id": adapter.id,
            "debit": [adapter.line("trade_receivable", functional_amount, channel)],
            "credit": [adapter.line("game_revenue", functional_amount, game)],
            "balanced": functional_amount is not None,
            "status": "阻塞" if functional_amount is None else "待复核",
            "review_role": "会计服务机构",
            "blockers": blockers,
            "judgement": {
                "question": "平台/发行方是主要责任人还是代理人，收入按总额还是净额列报？",
                "agent_recommendation": "当前先按结算单应收金额生成净额口径草稿，不自动过账。",
                "impact": "会影响营业收入与渠道成本的列报，但在同一结算净额下通常不改变毛利额。",
            },
            "evidence": ["结算单", "合同分成条款", "渠道后台汇总", "收入确认口径备忘录"],
            "authority_ids": [source["id"] for source in adapter.sources[:2]],
        })
    return drafts


def _purchase_account_role(category: str) -> str:
    if category in {"广告投放", "素材制作", "线下活动"}:
        return "operating_expense"
    if category == "软件与云服务":
        return "operating_expense"
    if category == "定制周边":
        return "cost_of_sales"
    return "operating_expense"


def build_purchase_voucher_drafts(
    purchases: Iterable[dict], period: str, fx_rates: dict[str, float] | None = None,
    adapter: LedgerAdapter | None = None,
) -> list[dict]:
    fx_rates = fx_rates or {}
    adapter = adapter or get_ledger_adapter({})
    drafts = []
    for index, record in enumerate(purchases, 1):
        order_date = str(record.get("order_date") or "")
        if order_date and not order_date.startswith(period):
            continue
        amount = record.get("accepted_amount")
        based_on_order = False
        if amount in (None, 0) and record.get("delivery_status") == "已交付待确认":
            amount = record.get("ordered_amount")
            based_on_order = amount not in (None, 0)
        if amount in (None, 0):
            continue
        currency = record.get("currency") or adapter.functional_currency
        blockers = []
        rate = functional_rate(currency, adapter, fx_rates)
        if not rate:
            blockers.append(f"缺少 {currency}/{adapter.functional_currency} 本位币折算汇率")
        if based_on_order:
            blockers.append("缺少正式验收金额，当前仅按PO金额给出暂估建议")
        if not record.get("vendor") or record.get("vendor") == "待识别供应商":
            blockers.append("缺少供应商名称")
        if record.get("anomalies") and not (
            len(record["anomalies"]) == 1 and "暂估" in record["anomalies"][0]
        ):
            blockers.append("采购勾稽异常尚未关闭")
        functional_amount = round(float(amount) * rate, 2) if rate else None
        account_role = _purchase_account_role(record.get("category") or "")
        drafts.append({
            "id": f"PUR-{period.replace('-', '')}-{index:03d}",
            "date": f"{period}-28",
            "type": "采购暂估" if not record.get("invoice_amount") else "采购入账",
            "summary": f"确认{record.get('project') or '项目'} {record.get('item') or '采购'}成本",
            "source": "采购与应付标准明细",
            "source_count": 1,
            "original_currency": currency,
            "original_amount": round(float(amount), 2),
            "fx_rate": rate or None,
            "functional_currency": adapter.functional_currency,
            "functional_amount": functional_amount,
            "ledger_adapter_id": adapter.id,
            "debit": [adapter.line(account_role, functional_amount, record.get("project") or "待分配项目")],
            "credit": [adapter.line("trade_payable", functional_amount, record.get("vendor") or "待识别供应商")],
            "balanced": functional_amount is not None,
            "status": "阻塞" if blockers else "待复核",
            "review_role": "业务验收人" if based_on_order else "会计服务机构",
            "blockers": blockers or ["需确认费用归属项目和会计科目"],
            "judgement": {
                "question": "服务或货物是否已在本月实际交付并由业务接受？",
                "agent_recommendation": "已交付的采购优先在本月确认成本；未取得发票时先暂估，下月收到发票后冲回重记。",
                "impact": "不暂估会让本月成本偏低、利润虚高；暂估后需在取得发票时冲回，避免重复入账。",
            },
            "evidence": ["PO/合同", "报价单", "交付成果", "验收记录", "发票（如已取得）"],
            "authority_ids": [source["id"] for source in adapter.sources[:2]],
        })
    return drafts


def build_trial_balance(vouchers: Iterable[dict]) -> dict:
    accounts: dict[str, dict[str, Any]] = defaultdict(lambda: {"debit": 0.0, "credit": 0.0})
    included = []
    excluded = []
    for voucher in vouchers:
        if not voucher.get("balanced") or voucher.get("status") == "阻塞":
            excluded.append(voucher.get("id"))
            continue
        included.append(voucher.get("id"))
        for line in voucher.get("debit") or []:
            accounts[line["account"]]["debit"] += float(line.get("amount") or 0)
            accounts[line["account"]].update({key: line.get(key) for key in ("account_code", "account_name", "category", "role")})
        for line in voucher.get("credit") or []:
            accounts[line["account"]]["credit"] += float(line.get("amount") or 0)
            accounts[line["account"]].update({key: line.get(key) for key in ("account_code", "account_name", "category", "role")})
    rows = []
    for account, amounts in sorted(accounts.items()):
        rows.append({
            "account": account,
            "debit": round(amounts["debit"], 2),
            "credit": round(amounts["credit"], 2),
            "net": round(amounts["debit"] - amounts["credit"], 2),
            **{key: amounts.get(key) for key in ("account_code", "account_name", "category", "role") if amounts.get(key)},
        })
    total_debit = round(sum(row["debit"] for row in rows), 2)
    total_credit = round(sum(row["credit"] for row in rows), 2)
    return {
        "rows": rows,
        "included_vouchers": included,
        "excluded_vouchers": excluded,
        "total_debit": total_debit,
        "total_credit": total_credit,
        "difference": round(total_debit - total_credit, 2),
        "balanced": abs(total_debit - total_credit) < 0.01,
        "status": "草稿试算" if included else "暂无可试算凭证",
    }


def build_bank_voucher_drafts(
    transactions: Iterable[dict], period: str, fx_rates: dict[str, float] | None = None,
    adapter: LedgerAdapter | None = None,
) -> list[dict]:
    fx_rates = fx_rates or {}
    adapter = adapter or get_ledger_adapter({})
    drafts = []
    for index, transaction in enumerate(transactions, 1):
        tx_date = str(transaction.get("transaction_date") or "")
        if tx_date and not tx_date.startswith(period):
            continue
        match = transaction.get("suggested_match") or {}
        if not match:
            continue
        amount = float(transaction.get("amount") or 0)
        currency = transaction.get("currency") or adapter.functional_currency
        rate = functional_rate(currency, adapter, fx_rates)
        functional_amount = round(amount * rate, 2) if rate else None
        high_confidence = transaction.get("status") == "高置信匹配"
        blockers = []
        if not rate:
            blockers.append(f"缺少 {currency}/{adapter.functional_currency} 本位币折算汇率")
        if not high_confidence:
            blockers.append("匹配置信度不足，需要确认款项用途")
        if abs(float(match.get("difference") or 0)) >= 0.01:
            blockers.append(f"银行金额与应收/应付差异 {float(match.get('difference')):,.2f} {currency}")
        if match.get("type") == "应收到账":
            debit = [adapter.line("cash", functional_amount, currency)]
            credit = [adapter.line("trade_receivable", functional_amount, match.get("target") or "待匹配应收")]
            summary = f"收到{match.get('target') or '游戏渠道'}结算款"
        elif match.get("type") == "应付付款":
            debit = [adapter.line("trade_payable", functional_amount, match.get("target") or "待匹配应付")]
            credit = [adapter.line("cash", functional_amount, currency)]
            summary = f"支付{match.get('target') or '供应商'}采购款"
        else:
            continue
        drafts.append({
            "id": f"BNK-{period.replace('-', '')}-{index:03d}",
            "date": tx_date or f"{period}-28",
            "type": match["type"],
            "summary": summary,
            "source": "银行流水与匹配记录",
            "source_count": 1,
            "original_currency": currency,
            "original_amount": round(amount, 2),
            "fx_rate": rate or None,
            "functional_currency": adapter.functional_currency,
            "functional_amount": functional_amount,
            "ledger_adapter_id": adapter.id,
            "debit": debit,
            "credit": credit,
            "balanced": functional_amount is not None,
            "status": "阻塞" if blockers else "待复核",
            "review_role": "财务负责人",
            "blockers": blockers or ["高置信匹配：建议异常复核模式批量确认"],
            "judgement": {
                "question": "交易对手和摘要是否与系统推荐的游戏渠道/供应商一致？",
                "agent_recommendation": match.get("recommendation") or "按匹配结果认领，并保留银行流水到业务底稿的链接。",
                "impact": "确认后会结清对应应收或应付；误认领会导致客户/供应商往来余额错误。",
            },
            "evidence": ["银行流水", "结算单或采购单", "匹配评分与金额差异"],
            "authority_ids": [source["id"] for source in adapter.sources[:2]],
        })
    return drafts


def _bank_source_fingerprint(rows: Iterable[dict]) -> str:
    payload = [{
        "id": row.get("id"), "transaction_id": row.get("transaction_id"),
        "transaction_date": row.get("transaction_date"), "account_masked": row.get("account_masked"),
        "currency": row.get("currency"), "direction": row.get("direction"),
        "amount": row.get("amount"), "balance": row.get("balance"), "status": row.get("status"),
    } for row in sorted(rows, key=lambda item: (
        str(item.get("transaction_date") or ""), str(item.get("source_row") or ""),
        str(item.get("id") or item.get("transaction_id") or ""),
    ))]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def build_bank_reconciliation(
    transactions: Iterable[dict], period: str, reviews: Iterable[dict] | None = None,
) -> dict:
    rows = [
        transaction for transaction in transactions
        if not transaction.get("transaction_date") or str(transaction.get("transaction_date")).startswith(period)
    ]
    reviews = list(reviews or [])
    by_account: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for transaction in rows:
        key = (transaction.get("account_masked") or "未识别账户", transaction.get("currency") or "未知")
        by_account[key].append(transaction)
    accounts = []
    for (account_masked, currency), account_rows in sorted(by_account.items()):
        bucket: dict[str, Any] = {
            "account_masked": account_masked, "currency": currency,
            "receipts": 0.0, "payments": 0.0, "statement_ending_balance": None,
            "ending_balance": None, "matched": 0, "pending": 0,
        }
        dated = sorted(account_rows, key=lambda item: (
            str(item.get("transaction_date") or ""), int(item.get("source_row") or 0),
        ))
        for transaction in dated:
            if transaction.get("direction") == "收入":
                bucket["receipts"] += float(transaction.get("amount") or 0)
            elif transaction.get("direction") == "支出":
                bucket["payments"] += float(transaction.get("amount") or 0)
            if transaction.get("balance") is not None:
                bucket["statement_ending_balance"] = float(transaction.get("balance"))
                bucket["ending_balance"] = float(transaction.get("balance"))
            if transaction.get("status") in {"高置信匹配", "已核销"}:
                bucket["matched"] += 1
            else:
                bucket["pending"] += 1
        source_fingerprint = _bank_source_fingerprint(account_rows)
        matching_reviews = [review for review in reviews if (
            review.get("period") == period
            and review.get("account_masked") == account_masked
            and review.get("currency") == currency
        )]
        matching_reviews.sort(key=lambda item: str(item.get("reviewed_at") or item.get("updated_at") or ""), reverse=True)
        latest = matching_reviews[0] if matching_reviews else None
        current = bool(latest and latest.get("source_fingerprint") == source_fingerprint)
        confirmed = bool(current and latest.get("decision") == "确认" and abs(float(latest.get("difference") or 0)) < 0.01)
        bucket.update({
            "source_fingerprint": source_fingerprint,
            "review": latest,
            "review_current": current,
            "confirmed": confirmed,
            "ledger_ending_balance": latest.get("ledger_ending_balance") if current else None,
            "deposits_in_transit": latest.get("deposits_in_transit") if current else 0.0,
            "outstanding_payments": latest.get("outstanding_payments") if current else 0.0,
            "bank_adjustments": latest.get("bank_adjustments") if current else 0.0,
            "ledger_adjustments": latest.get("ledger_adjustments") if current else 0.0,
            "difference": latest.get("difference") if current else None,
            "status": (
                "已调节并确认" if confirmed else
                "复核已失效" if latest and not current else
                "已退回待重做" if current and latest.get("decision") == "退回" else
                "存在未认领流水" if bucket["pending"] else
                "待补银行期末余额" if bucket["statement_ending_balance"] is None else
                "待补账面余额及未达项"
            ),
        })
        accounts.append({key: round(value, 2) if isinstance(value, float) else value for key, value in bucket.items()})

    by_currency: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"receipts": 0.0, "payments": 0.0, "ending_balance": None, "matched": 0, "pending": 0}
    )
    for account in accounts:
        bucket = by_currency[account["currency"]]
        bucket["receipts"] += account["receipts"]
        bucket["payments"] += account["payments"]
        bucket["matched"] += account["matched"]
        bucket["pending"] += account["pending"]
        if account["ending_balance"] is not None:
            bucket["ending_balance"] = round(float(bucket["ending_balance"] or 0) + float(account["ending_balance"]), 2)
    complete = bool(accounts) and all(account["confirmed"] for account in accounts)
    pending_count = sum(account["pending"] for account in accounts)
    return {
        "period": period,
        "accounts": accounts,
        "currencies": [
            {"currency": currency, **{
                key: round(value, 2) if isinstance(value, float) else value
                for key, value in bucket.items()
            }}
            for currency, bucket in sorted(by_currency.items())
        ],
        "pending_count": pending_count,
        "complete": complete,
        "status": (
            "银行余额调节已确认" if complete else
            "待补银行对账单" if not accounts else
            "存在未认领流水" if pending_count else
            "待完成账户级余额调节"
        ),
        "note": "每个银行账户和币种独立调节；管理汇总不能替代主体级银行余额确认。",
    }


def create_bank_reconciliation_review(
    transactions: Iterable[dict], period: str, account_masked: str, currency: str,
    ledger_ending_balance: Any, deposits_in_transit: Any = 0, outstanding_payments: Any = 0,
    bank_adjustments: Any = 0, ledger_adjustments: Any = 0, decision: str = "确认",
    actor: str = "", rationale: str = "", evidence: Iterable[str] | None = None,
) -> dict:
    if decision not in {"确认", "退回"}:
        raise ValueError("银行余额调节决定只能是确认或退回")
    actor, rationale = str(actor or "").strip(), str(rationale or "").strip()
    evidence = [str(item).strip() for item in (evidence or []) if str(item).strip()]
    if not actor:
        raise ValueError("请填写银行余额调节复核人")
    if len(rationale) < 8:
        raise ValueError("请填写至少8个字的复核说明")
    if not evidence:
        raise ValueError("请至少附一项银行对账单或总账余额证据")
    reconciliation = build_bank_reconciliation(transactions, period)
    account = next((item for item in reconciliation["accounts"] if (
        item["account_masked"] == account_masked and item["currency"] == currency
    )), None)
    if not account:
        raise ValueError("当前主体和账期不存在该银行账户及币种")
    if account["statement_ending_balance"] is None:
        raise ValueError("银行流水缺少期末余额，不能完成余额调节确认")
    values = {
        "ledger_ending_balance": round(float(ledger_ending_balance), 2),
        "deposits_in_transit": round(float(deposits_in_transit or 0), 2),
        "outstanding_payments": round(float(outstanding_payments or 0), 2),
        "bank_adjustments": round(float(bank_adjustments or 0), 2),
        "ledger_adjustments": round(float(ledger_adjustments or 0), 2),
    }
    if any(not math.isfinite(value) for value in values.values()):
        raise ValueError("银行余额调节金额必须是有限数字")
    if values["deposits_in_transit"] < 0 or values["outstanding_payments"] < 0:
        raise ValueError("在途存款和未兑现付款请填写非负金额；方向由表单公式处理")
    adjusted_bank = round(
        float(account["statement_ending_balance"]) + values["deposits_in_transit"]
        - values["outstanding_payments"] + values["bank_adjustments"], 2,
    )
    adjusted_ledger = round(values["ledger_ending_balance"] + values["ledger_adjustments"], 2)
    difference = round(adjusted_bank - adjusted_ledger, 2)
    if decision == "确认" and account["pending"]:
        raise ValueError(f"仍有 {account['pending']} 条流水待认领，不能确认余额调节")
    if decision == "确认" and abs(difference) >= 0.01:
        raise ValueError(f"调整后银行与账面余额仍相差 {difference:,.2f}，不能确认")
    reviewed_at = datetime.now(timezone.utc).isoformat()
    review_key = (
        f"{period}|{account_masked}|{currency}|{account['source_fingerprint']}|"
        f"{decision}|{actor}|{reviewed_at}"
    )
    return {
        "id": "BRR-" + hashlib.sha1(review_key.encode("utf-8")).hexdigest()[:12],
        "period": period, "account_masked": account_masked, "currency": currency,
        "statement_ending_balance": account["statement_ending_balance"], **values,
        "adjusted_bank_balance": adjusted_bank, "adjusted_ledger_balance": adjusted_ledger,
        "difference": difference, "pending_transaction_count": account["pending"],
        "source_fingerprint": account["source_fingerprint"], "decision": decision,
        "actor": actor, "rationale": rationale, "evidence": evidence,
        "reviewed_at": reviewed_at,
    }


def build_period_report(trial_balance: dict) -> dict:
    revenue = 0.0
    expenses = 0.0
    cash_movement = 0.0
    receivable_movement = 0.0
    payable_movement = 0.0
    for row in trial_balance.get("rows") or []:
        account = row["account"]
        role = row.get("role")
        category = row.get("category")
        net = float(row["net"])
        if category == "收入" or role in {"game_revenue", "other_revenue"}:
            revenue += -net
        elif category == "成本费用":
            expenses += net
        elif role == "cash" or account.startswith("1002"):
            cash_movement += net
        elif role == "trade_receivable" or account.startswith("1122"):
            receivable_movement += net
        elif role == "trade_payable" or account.startswith("2202"):
            payable_movement += -net
    return {
        "revenue": round(revenue, 2),
        "expenses": round(expenses, 2),
        "profit_before_tax_draft": round(revenue - expenses, 2),
        "cash_movement": round(cash_movement, 2),
        "receivable_movement": round(receivable_movement, 2),
        "payable_movement": round(payable_movement, 2),
        "scope": "仅包含已进入草稿试算且未阻塞的本期凭证，不是法定财务报表。",
    }


def build_payroll_voucher_drafts(
    payroll_rows: Iterable[dict], period: str, adapter: LedgerAdapter | None = None,
    fx_rates: dict[str, float] | None = None,
) -> list[dict]:
    adapter = adapter or get_ledger_adapter({})
    fx_rates = fx_rates or {}
    rows = [row for row in payroll_rows if not row.get("period") or row.get("period") == period]
    if not rows:
        return []
    by_dimension: dict[tuple[str, str, str], dict[str, float]] = defaultdict(
        lambda: {
            "gross": 0.0, "employee_deductions": 0.0, "withholding": 0.0, "net": 0.0,
            "employer_contributions": 0.0, "employer_levies": 0.0, "other_employer_cost": 0.0,
            "rd": 0.0, "count": 0.0,
        }
    )
    for row in rows:
        currency = str(row.get("currency") or adapter.functional_currency).upper()
        key = (row.get("department") or "待分配部门", row.get("project") or "非研发/项目待补", currency)
        bucket = by_dimension[key]
        bucket["gross"] += float(row.get("gross_salary") or 0)
        bucket["employee_deductions"] += float(
            row.get("employee_deductions")
            if "employee_deductions" in row else
            float(row.get("social_security") or 0) + float(row.get("housing_fund") or 0)
        )
        bucket["withholding"] += float(
            row.get("withholding_tax")
            if "withholding_tax" in row else row.get("calculated_iit") or 0
        )
        bucket["net"] += float(row.get("net_salary") or 0)
        bucket["employer_contributions"] += float(row.get("employer_contributions") or 0)
        bucket["employer_levies"] += float(row.get("employer_levies") or 0)
        bucket["other_employer_cost"] += float(row.get("other_employer_cost") or 0)
        bucket["rd"] += float(row.get("rd_salary_candidate") or 0)
        bucket["count"] += 1
    drafts = []
    for index, ((department, project, currency), values) in enumerate(sorted(by_dimension.items()), 1):
        implied_net = values["gross"] - values["employee_deductions"] - values["withholding"]
        employer_on_costs = values["employer_contributions"] + values["employer_levies"] + values["other_employer_cost"]
        total_employer_cost = values["gross"] + employer_on_costs
        blockers = []
        if abs(values["net"] - implied_net) > max(1, abs(implied_net) * 0.001):
            blockers.append(f"实发工资与应发减个人扣款差异 {values['net'] - implied_net:,.2f}")
        if project != "非研发/项目待补" and values["rd"]:
            blockers.append("研发人工仅为候选额，需补项目立项、人员角色和工时证据")
        rate = functional_rate(currency, adapter, fx_rates)
        if not rate:
            blockers.append(f"缺少 {currency}/{adapter.functional_currency} 本位币折算汇率")
        functional_cost = round(total_employer_cost * rate, 2) if rate else None
        functional_net = round(values["net"] * rate, 2) if rate else None
        functional_withholding = round(values["withholding"] * rate, 2) if rate else None
        functional_other_payable = round((values["employee_deductions"] + employer_on_costs) * rate, 2) if rate else None
        functional_credit = None if not rate else round(functional_net + functional_withholding + functional_other_payable, 2)
        balanced = functional_cost is not None and abs(functional_cost - functional_credit) < 0.01
        if adapter.jurisdiction != "CN" and any(
            row.get("jurisdiction") not in (None, "", adapter.jurisdiction)
            for row in rows if (row.get("department") or "待分配部门", row.get("project") or "非研发/项目待补", str(row.get("currency") or adapter.functional_currency).upper()) == (department, project, currency)
        ):
            blockers.append("工资底稿司法辖区与主体配置不一致")
        drafts.append({
            "id": f"PAY-{period.replace('-', '')}-{index:03d}",
            "date": f"{period}-28", "type": "工资计提",
            "summary": f"计提{period} {department}工资薪金",
            "source": "工资与法定扣款标准明细", "source_count": int(values["count"]),
            "original_currency": currency, "original_amount": round(total_employer_cost, 2),
            "fx_rate": rate or None,
            "functional_currency": adapter.functional_currency, "functional_amount": functional_cost,
            "ledger_adapter_id": adapter.id,
            "debit": [adapter.line("operating_expense", functional_cost, f"{department} / {project}")],
            "credit": [
                adapter.line("payroll_payable", functional_net, department),
                adapter.line("tax_payable", functional_withholding, "个人所得税" if adapter.jurisdiction == "CN" else "withholding tax"),
                adapter.line(
                    "other_payable", functional_other_payable,
                    "个人社保公积金及单位成本" if adapter.jurisdiction == "CN" else "employee deductions and employer statutory costs",
                ),
            ],
            "balanced": balanced,
            "status": "阻塞" if blockers else "待复核", "review_role": "财务负责人",
            "blockers": blockers or ["需确认人员在职、工资审批及付款批次"],
            "judgement": {
                "question": "人员、本月应发、员工扣款、雇主成本及研发参与事实是否正确？",
                "agent_recommendation": (
                    "工资按部门/项目批量计提；个人明细保留在工资底稿，不在总账摘要暴露姓名。"
                    if adapter.jurisdiction == "CN" else
                    "按当地已核准 payroll 报告导入应发、实发、员工扣款及雇主成本；系统只做勾稽和入账候选，不推算法定比例。"
                ),
                "impact": "影响本月人员成本、应付工资和个税负债；研发标签还会影响所得税加计扣除资料。",
            },
            "evidence": ["工资审批表", "人员在职清单", "当地 payroll 报告/个税试算", "银行代发结果", "研发工时（如适用）"],
            "authority_ids": (
                ["mof-accounting-basic-2024", "iit-withholding-2018", "rd-super-deduction"]
                if adapter.jurisdiction == "CN" else [source["id"] for source in adapter.sources]
            ),
        })
    return drafts


def build_close_tasks(records: Iterable[dict], period: str) -> list[dict]:
    period_records = [record for record in records if record.get("period") == period]
    has_settlements = bool(period_records)
    tasks = [
        ("C01", "锁定账期与资料清单", "月结准备", "Agent", True, [], "D+1"),
        ("C02", "导入平台及发行渠道结算单", "收入", "Agent", has_settlements,
         [] if has_settlements else ["缺少本期渠道结算单"], "D+2"),
        ("C03", "完成游戏、平台、渠道、流水及分成勾稽", "收入", "Agent", has_settlements,
         [] if has_settlements else ["等待收入结算数据"], "D+3"),
        ("C04", "归集销项、进项和数电发票", "发票与税务", "Agent", False,
         ["尚未接入数电发票台账"], "D+3"),
        ("C05", "归集采购、外包、报销及待摊费用", "成本费用", "业务负责人", False,
         ["尚未接入采购和费用单据"], "D+3"),
        ("C06", "生成工资、社保、公积金及个税底稿", "薪酬", "Agent", False,
         ["尚未配置员工与工资数据"], "D+4"),
        ("C07", "银行流水认领及银行余额调节", "资金", "Agent", False,
         ["尚未导入银行流水及银行对账单"], "D+4"),
        ("C08", "应收、应付和预付款账龄核对", "往来", "Agent", False,
         ["缺少总账期初及采购台账"], "D+4"),
        ("C09", "固定资产、无形资产及待摊费用计提", "资产", "Agent", False,
         ["尚未建立资产卡片"], "D+4"),
        ("C10", "外币余额折算与汇兑损益", "外币", "会计服务机构", False,
         ["缺少月末汇率及外币账户余额"], "D+5"),
        ("C11", "生成并复核记账凭证草稿", "总账", "会计服务机构", has_settlements,
         [] if has_settlements else ["没有可生成凭证的业务底稿"], "D+5"),
        ("C12", "完成科目、往来、银行及税务勾稽", "总账", "Agent", False,
         ["总账和税务模块尚未全部接入"], "D+6"),
        ("C13", "生成资产负债表、利润表及现金流摘要", "报表", "Agent", False,
         ["总账尚未完成"], "D+6"),
        ("C14", "形成税务申报资料包和差异说明", "税务", "Agent", False,
         ["税务档案、发票、工资和总账数据不完整"], "D+7"),
        ("C15", "负责人确认关账并冻结账期", "关账", "公司负责人", False,
         ["前置月结任务未完成"], "D+7"),
    ]
    task_guidance = {
        "C05": {
            "plain_language": "把本月已经发生、但发票或付款流程还没走完的外包和采购找出来，避免成本漏记导致利润虚高。",
            "recommendation": "先按验收或实际服务进度暂估入账，下月取得发票后冲回并按发票重记。",
            "confidence": 0.82,
            "business_questions": ["服务是否已经实际完成？", "业务负责人是否验收？", "合同或报价金额是多少？"],
            "options": [
                {"name": "本月暂估", "recommended": True, "benefit": "利润和项目成本更接近真实情况", "cost": "下月需要冲回并核对发票"},
                {"name": "等发票再记", "recommended": False, "benefit": "操作更省事", "cost": "本月利润可能虚高，跨月成本失真"},
            ],
        },
        "C10": {
            "plain_language": "把月底仍未收回或未支付的美元、港币余额，按月末人民币汇率重新换算。",
            "recommendation": "采用公司一贯使用且可追溯的月末即期汇率；首次建立后每月保持来源一致。",
            "confidence": 0.9,
            "business_questions": ["月底还有多少外币应收和银行余额？", "以前月份使用哪个汇率来源？"],
            "options": [
                {"name": "统一月末汇率重估", "recommended": True, "benefit": "资产负债表口径一致、汇兑损益可追溯", "cost": "需维护汇率来源和外币余额"},
                {"name": "沿用交易日汇率", "recommended": False, "benefit": "少一步计算", "cost": "月底人民币余额失真，不建议作为期末计量"},
            ],
        },
        "C11": {
            "plain_language": "Agent 已经把业务翻译成借贷分录；会计只需确认科目和收入按总额还是净额展示。",
            "recommendation": "优先批量通过证据完整、借贷平衡且沿用既有口径的凭证，只逐笔检查异常。",
            "confidence": 0.88,
            "business_questions": ["这笔收入对应哪个游戏和渠道？", "公司承担定价、履约和退款的主要责任吗？"],
            "options": [
                {"name": "异常复核模式", "recommended": True, "benefit": "业务同学工作量小，风险集中处理", "cost": "需先建立稳定的凭证规则"},
                {"name": "全部逐笔复核", "recommended": False, "benefit": "初期更谨慎", "cost": "月结耗时，难以规模化"},
            ],
        },
        "C15": {
            "plain_language": "关账相当于给这个月盖章：数据不再随意改动，后续调整必须留下记录。",
            "recommendation": "只有银行、往来、税务和凭证关键勾稽通过后才关账；轻微非金额问题可列入下月待办。",
            "confidence": 0.95,
            "business_questions": ["是否还有会显著影响利润或现金的未入账事项？", "负责人是否认可本月经营结果？"],
            "options": [
                {"name": "完成关键勾稽后关账", "recommended": True, "benefit": "报表稳定且保留少量弹性", "cost": "需要负责人处理关键异常"},
                {"name": "所有事项清零后关账", "recommended": False, "benefit": "形式上最完整", "cost": "容易因小问题长期拖延月结"},
            ],
        },
    }
    return [{
        "id": task_id,
        "name": name,
        "stream": stream,
        "owner": owner,
        "status": "已完成" if done else ("阻塞" if blockers else "待处理"),
        "blockers": blockers,
        "deadline": deadline,
        "evidence_required": owner != "Agent" or bool(blockers),
        "authority_ids": ["mof-accounting-basic-2024"] if stream in {"总账", "关账", "报表"} else [],
        "decision_support": task_guidance.get(task_id),
    } for task_id, name, stream, owner, done, blockers, deadline in tasks]


def build_tax_pack(
    records: Iterable[dict], period: str, company_profile: dict[str, Any] | None = None,
    invoices: Iterable[dict] | None = None, payroll_rows: Iterable[dict] | None = None,
    trial_balance: dict | None = None,
) -> dict:
    company_profile = company_profile or {}
    invoices = list(invoices or [])
    payroll_rows = list(payroll_rows or [])
    trial_balance = trial_balance or {}
    period_records = [record for record in records if record.get("period") == period]
    domestic = [record for record in period_records if record.get("scope") == "国内"]
    overseas = [record for record in period_records if record.get("scope") == "海外"]
    cross_border_reviews = (company_profile.get("tax_policy") or {}).get("cross_border_reviews") or {}
    overseas_channels = sorted({record.get("channel") or "待识别渠道" for record in overseas})
    missing_channel_reviews = [
        channel for channel in overseas_channels
        if (cross_border_reviews.get(channel) or {}).get("decision") in {None, "", "待补证据"}
        or not (cross_border_reviews.get(channel) or {}).get("reviewer")
        or not (cross_border_reviews.get(channel) or {}).get("evidence")
    ]
    vat_configured = company_profile.get("vat_taxpayer_type") not in {None, "", "待配置"} and (
        company_profile.get("vat_filing_frequency") not in {None, "", "待配置"}
    )
    invoice_ready = bool(invoices) and not any(invoice.get("anomalies") for invoice in invoices)
    payroll_ready = bool(payroll_rows) and not any(row.get("anomalies") for row in payroll_rows)
    ledger_ready = bool(trial_balance.get("included_vouchers")) and trial_balance.get("balanced")
    tax_items = [
        {
            "tax": "增值税及附加",
            "frequency": "按主管税务机关核定的月/季",
            "status": "待复核" if vat_configured and invoice_ready and not missing_channel_reviews else "阻塞",
            "agent_output": "收入分类表、销项/进项发票勾稽、申报草表、税会差异表",
            "missing": (
                ([] if vat_configured else ["纳税人类型及申报周期"])
                + ([] if invoice_ready else ["完整且无异常的进销项发票台账"])
                + ([f"海外渠道复核：{'、'.join(missing_channel_reviews)}"] if missing_channel_reviews else [])
            ),
            "risk": "高",
            "review_role": "税务服务机构",
            "authority_ids": ["vat-law-2026", "vat-transition-2026", "digital-invoice-2024"],
            "decision_support": {
                "plain_language": "先判断公司是小规模还是一般纳税人，再判断每类游戏收入到底是在境内提供服务、跨境服务还是IP授权；这决定税率和是否可享受零税率/优惠。",
                "recommendation": "不要按收款币种直接判断税务。先按合同交易对手、用户/服务消费地和履约角色逐渠道分类；证据不够的海外收入先列为待判断。",
                "confidence": 0.86,
                "options": [
                    {"name": "逐渠道建立税务口径", "recommended": True, "benefit": "后续每月可自动复用，跨境风险最低", "cost": "首次需要阅读合同和补消费地证据"},
                    {"name": "所有流水统一按一种税率", "recommended": False, "benefit": "短期简单", "cost": "容易多缴或少缴，无法解释跨境与平台差异"},
                ],
            },
        },
        {
            "tax": "企业所得税预缴",
            "frequency": "月度或季度",
            "status": "待复核" if ledger_ready else "阻塞",
            "agent_output": "利润总额、纳税调整预估、优惠资格检查、预缴申报草表",
            "missing": ([] if ledger_ready else ["完整利润草稿及试算平衡"])
                       + ["税会差异台账", "资产总额与从业人数", "以前年度亏损"],
            "risk": "中",
            "review_role": "税务服务机构",
            "authority_ids": ["cit-return-2025", "micro-tax-2023"],
            "decision_support": {
                "plain_language": "企业所得税不是简单拿收入乘税率，而是从会计利润出发，加减税法不认可或允许额外扣除的项目。",
                "recommendation": "先按实际利润建立季度预缴底稿；同时持续维护招待费、福利费、无票支出、资产折旧和研发费用等税会差异。",
                "confidence": 0.9,
                "options": [
                    {"name": "每月维护、季度申报", "recommended": True, "benefit": "季度不会临时找数据，税负预测更准", "cost": "每月多维护少量税会标签"},
                    {"name": "季度末一次整理", "recommended": False, "benefit": "平时省事", "cost": "容易漏优惠、漏调整并拖延申报"},
                ],
            },
        },
        {
            "tax": "个人所得税扣缴",
            "frequency": "按月，通常次月十五日内",
            "status": "待复核" if payroll_ready else "阻塞",
            "agent_output": "工资薪金与劳务报酬扣缴底稿、人员增减和异常清单",
            "missing": ([] if payroll_ready else ["无异常的工资与个税底稿"])
                       + ["专项附加扣除申报系统确认", "个人劳务支付台账（如有）"],
            "risk": "高",
            "review_role": "公司负责人",
            "authority_ids": ["iit-withholding-2018"],
            "decision_support": {
                "plain_language": "公司向员工或个人外包支付报酬时，通常需要按人员、所得类型计算并申报个税。",
                "recommendation": "工资与个人劳务分开管理；业务同学只确认人员身份、服务关系和本月应付金额，税额由 Agent 计算并交负责人复核。",
                "confidence": 0.93,
                "options": [
                    {"name": "按真实关系分类申报", "recommended": True, "benefit": "个税、社保和成本证据一致", "cost": "需要完整人员及合同档案"},
                    {"name": "全部按报销或公司间采购处理", "recommended": False, "benefit": "表面流程简单", "cost": "实质不符时存在补税和用工风险"},
                ],
            },
        },
        {
            "tax": "印花税",
            "frequency": "依主管税务机关核定，常见按季/年/次",
            "status": "阻塞",
            "agent_output": "应税合同识别、税源明细和申报提醒",
            "missing": ["合同台账", "合同不含税金额", "属地纳税期限配置"],
            "risk": "中",
            "review_role": "税务服务机构",
            "authority_ids": [],
            "decision_support": {
                "plain_language": "部分采购、技术、许可等合同在签署时可能产生印花税，不取决于是否已经付款或收到发票。",
                "recommendation": "建立合同台账并在签署时标记合同类型和不含税金额；由系统按属地配置生成季度或按次清单。",
                "confidence": 0.8,
                "options": [
                    {"name": "合同签署时识别", "recommended": True, "benefit": "不漏报，金额和合同可追溯", "cost": "采购流程需多填两个字段"},
                    {"name": "付款时再判断", "recommended": False, "benefit": "流程改动少", "cost": "纳税义务时点可能已经发生"},
                ],
            },
        },
    ]
    cross_border = {
        "count": len(overseas),
        "currencies": sorted({record.get("currency") or "未知" for record in overseas}),
        "status": "需专项判断" if overseas else "本期未识别",
        "questions": [
            "合同交易对手是否为境外单位？",
            "游戏发行、软件服务或无形资产是否完全在境外消费/使用？",
            "公司是服务提供者、IP授权方、联合运营方还是仅收取平台分成？",
            "是否具备合同、结算单、收款和境外消费地证明？",
        ] if overseas else [],
        "note": "跨境收入不能仅凭收款币种判断零税率、免税或应税。",
        "authority_url": "https://www.chinatax.gov.cn/chinatax/c102449/c5239191/content.html",
        "channels": [{
            "channel": channel,
            "review": cross_border_reviews.get(channel) or {},
            "status": "已完成" if channel not in missing_channel_reviews else "阻塞",
        } for channel in overseas_channels],
        "all_reviewed": not missing_channel_reviews,
    }
    return {
        "period": period,
        "domestic_settlement_records": len(domestic),
        "overseas_settlement_records": len(overseas),
        "items": tax_items,
        "cross_border": cross_border,
        "filing_guardrail": "系统只生成申报资料包和申报草稿；提交前要求有权人员确认。",
    }


def build_non_cn_tax_pack(period: str, company_profile: dict[str, Any]) -> dict:
    jurisdiction = str(company_profile.get("jurisdiction") or "").upper() or "UNCONFIGURED"
    readiness = str(company_profile.get("tax_readiness") or "design")
    pack_id = str(company_profile.get("tax_pack") or "")
    authority_scope = str(company_profile.get("tax_authority_scope") or "")
    mature = readiness == "filing_assist"
    item = {
        "tax": f"{jurisdiction} 主体税务工作区",
        "frequency": "按所选纳税地区 Pack 与登记事实",
        "status": "待复核" if mature else "阻塞",
        "agent_output": "登记事实、证据清单、候选日历和复核门",
        "missing": [] if mature else [f"Tax Pack 当前成熟度为 {readiness}，尚不能生成申报工作底稿"],
        "risk": "高", "review_role": "当地税务服务机构或有权申报人",
        "authority_ids": [],
        "decision_support": {
            "plain_language": "海外主体必须使用自身纳税地区规则，不能套用中国增值税、企业所得税或个税表。",
            "recommendation": "先确认税务登记、财年、申报类型和当地服务机构；在 Tax Pack 达到相应成熟度前只输出证据与候选日历。",
            "confidence": 1.0,
            "options": [],
        },
    }
    workspace = {
        "entity_id": company_profile.get("entity_id") or "",
        "company_name": company_profile.get("company_name") or "待配置",
        "period": period, "returns": [],
        "summary": {"form_count": 0, "ready_for_review": 0, "blocked": 1, "direct_upload_ready": 0},
        "workflow": ["确认登记事实", "核对当地规则", "准备证据", "当地专业复核", "有权人提交", "保存回执"],
        "guardrail": f"{pack_id or jurisdiction} 当前为 {readiness}；不会生成中国税表，也不会声称已经申报。",
    }
    return {
        "items": [item], "cross_border": {
            "status": "按当地 Pack 管理", "all_reviewed": mature, "channels": [],
            "note": "海外主体税务与中国主体完全分开。",
        },
        "returns_workspace": workspace,
        "filing_assist": {
            "entity_id": workspace["entity_id"], "entity_name": workspace["company_name"],
            "period": period, "forms": [],
            "summary": {"form_count": 0, "contract_locked": 0, "ready_for_release": 0, "blocked": 1, "direct_upload_ready": 0},
            "release_boundary": authority_scope or workspace["guardrail"],
        },
        "jurisdiction": jurisdiction, "tax_pack": pack_id, "tax_readiness": readiness,
        "filing_guardrail": workspace["guardrail"],
    }


def build_finance_ops(
    records: Iterable[dict],
    period: str,
    purchases: Iterable[dict] | None = None,
    bank_transactions: Iterable[dict] | None = None,
    invoices: Iterable[dict] | None = None,
    payroll_rows: Iterable[dict] | None = None,
    company_profile: dict[str, Any] | None = None,
    opening_balances: Iterable[dict] | None = None,
    asset_cards: Iterable[dict] | None = None,
    accruals: Iterable[dict] | None = None,
    posted_vouchers: Iterable[dict] | None = None,
    game_revenue_policies: Iterable[dict] | None = None,
    expense_claims: Iterable[dict] | None = None,
    ledger_adapter_reviews: Iterable[dict] | None = None,
    bank_reconciliation_reviews: Iterable[dict] | None = None,
) -> dict:
    records = list(records)
    records = [record for record in records if record.get("release_status") in {None, "", "released"}]
    purchases = list(purchases or [])
    bank_transactions = list(bank_transactions or [])
    invoices = list(invoices or [])
    payroll_rows = list(payroll_rows or [])
    opening_balances = list(opening_balances or [])
    asset_cards = list(asset_cards or [])
    accruals = list(accruals or [])
    posted_records = list(posted_vouchers or [])
    posting_records_available = posted_vouchers is not None
    revenue_policies = list(game_revenue_policies or [])
    expense_claim_rows = list(expense_claims or [])
    company_profile = company_profile or {}
    adapter = get_ledger_adapter(company_profile)
    ledger_control = adapter.public_payload(list(ledger_adapter_reviews or []))
    bank_reconciliation = build_bank_reconciliation(
        bank_transactions, period, list(bank_reconciliation_reviews or []),
    )
    posting_enabled = posting_records_available and ledger_control["posting_ready"]
    entity_id = str(company_profile.get("entity_id") or "")
    all_rates = (company_profile.get("fx_policy") or {}).get("month_end_rates") or {}
    fx_rates = all_rates.get(period) or {}
    tasks = build_close_tasks(records, period)
    revenue_recognition = build_revenue_recognition(records, revenue_policies, period) if revenue_policies else None
    revenue_vouchers = (
        build_game_revenue_vouchers(revenue_recognition, period, fx_rates, adapter)
        if revenue_recognition is not None else build_voucher_drafts(records, period, fx_rates, adapter)
    )
    purchase_vouchers = build_purchase_voucher_drafts(purchases, period, fx_rates, adapter)
    bank_vouchers = build_bank_voucher_drafts(bank_transactions, period, fx_rates, adapter)
    payroll_vouchers = build_payroll_voucher_drafts(payroll_rows, period, adapter, fx_rates)
    adjustment_vouchers = build_adjustment_vouchers(asset_cards, accruals, period, adapter)
    expense_vouchers = build_expense_vouchers(expense_claim_rows, period, adapter, fx_rates)
    vouchers = revenue_vouchers + purchase_vouchers + bank_vouchers + payroll_vouchers + adjustment_vouchers + expense_vouchers
    if not ledger_control["posting_ready"]:
        mapping_blocker = (
            f"{adapter.id} {adapter.chart_version} 科目映射尚未经当地会计批准；"
            "可以生成本位币工作底稿，但不能进入正式过账。"
        )
        for voucher in vouchers:
            voucher["blockers"] = list(dict.fromkeys([*(voucher.get("blockers") or []), mapping_blocker]))
            voucher["status"] = "阻塞"
    tax_pack = None
    period_purchases = [
        row for row in purchases
        if not row.get("order_date") or str(row.get("order_date")).startswith(period)
    ]
    period_bank = [
        row for row in bank_transactions
        if not row.get("transaction_date") or str(row.get("transaction_date")).startswith(period)
    ]
    period_invoices = [
        row for row in invoices
        if not row.get("invoice_date") or str(row.get("invoice_date")).startswith(period)
    ]
    period_payroll = [row for row in payroll_rows if not row.get("period") or row.get("period") == period]
    period_records = [record for record in records if record.get("period") == period]
    if period_purchases:
        task = next(task for task in tasks if task["id"] == "C05")
        task.update(status="已完成", blockers=[])
    if period_bank:
        task = next(task for task in tasks if task["id"] == "C07")
        task.update(
            status="已完成" if bank_reconciliation["complete"] else "阻塞",
            blockers=[] if bank_reconciliation["complete"] else [
                f"仍有 {bank_reconciliation['pending_count']} 条银行流水待确认或认领"
                if bank_reconciliation["pending_count"] else
                "各银行账户仍需补账面余额、未达项并完成复核确认"
            ],
        )
    if period_invoices:
        task = next(task for task in tasks if task["id"] == "C04")
        invoice_issues = sum(bool(invoice.get("anomalies")) for invoice in period_invoices)
        task.update(
            status="已完成" if invoice_issues == 0 else "阻塞",
            blockers=[] if invoice_issues == 0 else [f"仍有 {invoice_issues} 张发票未查验、重复或勾稽异常"],
        )
    if period_payroll:
        task = next(task for task in tasks if task["id"] == "C06")
        payroll_issues = sum(bool(row.get("anomalies")) for row in period_payroll)
        task.update(
            status="已完成" if payroll_issues == 0 else "阻塞",
            blockers=[] if payroll_issues == 0 else [f"仍有 {payroll_issues} 人工资或个税试算异常"],
        )
    foreign_currencies = sorted({
        row.get("currency") for row in records + purchases + bank_transactions
        if (row.get("currency") or adapter.functional_currency) != adapter.functional_currency
    })
    fx_task = next(task for task in tasks if task["id"] == "C10")
    missing_fx = [currency for currency in foreign_currencies if not functional_rate(currency, adapter, fx_rates)]
    fx_task.update(
        status="已完成" if not missing_fx else "阻塞",
        blockers=[] if not missing_fx else [
            f"缺少 {period} 月末 {'、'.join(missing_fx)} 对 {adapter.functional_currency} 本位币折算汇率"
        ],
    )
    profile_ready = not any(
        not company_profile.get(field) or company_profile.get(field) == "待配置"
        for field in ("credit_code", "registered_city", "vat_taxpayer_type", "vat_filing_frequency")
    ) and bool((company_profile.get("external_accountant") or {}).get("provider"))
    asset_policy = company_profile.get("asset_policy") or {}
    asset_attested = (asset_policy.get("monthly_attestation") or {}).get(period) in {"无新增及处置", "已核对资产卡片"}
    task = next(task for task in tasks if task["id"] == "C08")
    ar_ap_ready = bool(period_records) and bool(period_purchases) and bool(period_bank)
    task.update(
        status="已完成" if ar_ap_ready else "阻塞",
        blockers=[] if ar_ap_ready else ["需同时具备本期结算、采购和银行台账才能核对往来"],
    )
    task = next(task for task in tasks if task["id"] == "C09")
    task.update(
        status="已完成" if asset_attested else "阻塞",
        blockers=[] if asset_attested else ["请确认本期无重大资产新增/处置，或完成资产卡片核对"],
    )
    eligible_vouchers = [voucher for voucher in vouchers if voucher.get("balanced") and voucher.get("status") != "阻塞"]
    blocked_vouchers = [voucher for voucher in vouchers if voucher.get("status") == "阻塞"]
    task = next(task for task in tasks if task["id"] == "C11")
    task.update(
        status="已完成" if eligible_vouchers and not blocked_vouchers else "阻塞",
        blockers=[] if eligible_vouchers and not blocked_vouchers else [f"仍有 {len(blocked_vouchers)} 张凭证草稿阻塞"],
    )
    trial_balance = build_trial_balance(vouchers)
    posted_balance = posted_trial_balance(posted_records, period, entity_id)
    financial_statements = build_financial_statements(opening_balances, trial_balance, period)
    posted_financial_statements = build_financial_statements(opening_balances, posted_balance, period)
    tax_trial = posted_balance if posting_enabled else trial_balance
    tax_statements = posted_financial_statements if posting_enabled else financial_statements
    jurisdiction = str(company_profile.get("jurisdiction") or "CN").upper()
    if jurisdiction == "CN":
        tax_pack = build_tax_pack(
            records, period, company_profile, period_invoices, period_payroll, tax_trial
        )
        tax_pack["returns_workspace"] = build_tax_returns(
            records, period, company_profile, period_purchases, period_invoices, period_payroll,
            build_period_report(tax_trial), tax_statements, tax_pack["cross_border"],
        )
        tax_pack["returns_workspace"]["accounting_basis"] = (
            "已过账总账及报表" if posting_enabled else "凭证草稿试算（不可作为最终申报账面数）"
        )
        tax_pack["filing_assist"] = build_filing_assist(tax_pack["returns_workspace"])
    else:
        tax_pack = build_non_cn_tax_pack(period, company_profile)
    reconciliation_ready = (
        ar_ap_ready and bank_reconciliation["complete"]
        and trial_balance["balanced"] and not blocked_vouchers
    )
    task = next(task for task in tasks if task["id"] == "C12")
    task.update(
        status="已完成" if reconciliation_ready else "阻塞",
        blockers=[] if reconciliation_ready else ["需先完成往来、银行、税务与凭证勾稽"],
    )
    task = next(task for task in tasks if task["id"] == "C13")
    statements_ready = financial_statements["opening_available"] and financial_statements["balance_sheet"]["balanced"]
    task.update(
        status="已完成" if statements_ready and bool(trial_balance["included_vouchers"]) else "阻塞",
        blockers=[] if statements_ready and trial_balance["included_vouchers"] else [
            "需导入上期经确认的期末余额，并确保资产负债表勾稽平衡"
        ],
    )
    tax_ready = jurisdiction == "CN" and (
        profile_ready and bool(period_invoices) and bool(period_payroll) and reconciliation_ready
        and tax_pack["cross_border"]["all_reviewed"]
    ) or jurisdiction != "CN" and tax_pack.get("tax_readiness") == "filing_assist" and reconciliation_ready
    task = next(task for task in tasks if task["id"] == "C14")
    task.update(
        status="已完成" if tax_ready else "阻塞",
        blockers=[] if tax_ready else [
            "公司税务档案、发票、工资、总账勾稽或海外渠道税务复核仍不完整"
            if jurisdiction == "CN" else
            f"{jurisdiction} Tax Pack 当前成熟度为 {tax_pack.get('tax_readiness')}，需当地税务服务机构完成复核链路"
        ],
    )
    preclose_ready = all(task["status"] == "已完成" for task in tasks if task["id"] != "C15")
    task = next(task for task in tasks if task["id"] == "C15")
    task.update(
        status="待处理" if preclose_ready else "阻塞",
        blockers=[] if preclose_ready else ["前置月结任务未完成"],
    )
    complete = sum(task["status"] == "已完成" for task in tasks)
    blocked = sum(task["status"] == "阻塞" for task in tasks)
    return {
        "period": period,
        "entity_id": entity_id,
        "close": {
            "status": "进行中" if complete else "未开始",
            "progress": round(complete / len(tasks), 4),
            "completed": complete,
            "blocked": blocked,
            "total": len(tasks),
            "tasks": tasks,
        },
        "vouchers": vouchers,
        "voucher_groups": {
            "revenue": len(revenue_vouchers),
            "purchase": len(purchase_vouchers),
            "bank": len(bank_vouchers),
            "payroll": len(payroll_vouchers),
            "adjustments": len(adjustment_vouchers),
            "expenses": len(expense_vouchers),
        },
        "trial_balance": trial_balance,
        "period_report": build_period_report(trial_balance),
        "financial_statements": financial_statements,
        "posted_trial_balance": posted_balance,
        "posted_financial_statements": posted_financial_statements,
        "posting": {
            "enabled": posting_enabled,
            "allowed": ledger_control["posting_ready"],
            "draft_count": len(vouchers),
            "posted_count": sum(
                item.get("period") == period and item.get("status") == "已过账" for item in posted_records
            ),
            "unposted_reviewed_count": 0,
            "reporting_basis": "已过账口径" if posting_enabled else "凭证草稿口径",
            "guardrail": "草稿试算用于工作检查；已过账试算与报表才代表当前账面记录。",
        },
        "bank_reconciliation": bank_reconciliation,
        "data_coverage": {
            "settlement_records": len([record for record in records if record.get("period") == period]),
            "purchase_records": len(period_purchases),
            "bank_transactions": len(period_bank),
            "invoices": len(period_invoices),
            "payroll_records": len(period_payroll),
            "opening_balance_rows": len([row for row in opening_balances if row.get("period") == period]),
        },
        "company_profile": company_profile,
        "tax_pack": tax_pack,
        "game_revenue_recognition": revenue_recognition or {
            "period": period, "rows": [],
            "blockers": [{"reason": "尚未启用已批准的游戏收入政策；当前收入凭证仅为结算口径草稿"}],
            "summary_by_currency": [],
            "guardrail": "结算口径草稿不能替代履约口径的收入确认。",
        },
        "ledger_adapter": ledger_control,
        "accounts": [dict(account) for account in adapter.chart],
        "sources": list(adapter.sources) if jurisdiction != "CN" else OFFICIAL_SOURCES,
        "statutory_ledger_guardrail": adapter.guardrail,
        "system_modules": [
            {"name": "收入与渠道结算", "status": "可用", "coverage": 80},
            {"name": "月结任务台", "status": "可用", "coverage": 72},
            {"name": "凭证草拟与复核", "status": "可用", "coverage": 65},
            {"name": "税务资料包", "status": "可用", "coverage": 55},
            {"name": "采购与费用", "status": "可用", "coverage": 65},
            {"name": "工资与个税", "status": "可用", "coverage": 60},
            {"name": "银行与资金", "status": "可用", "coverage": 65},
            {"name": "总账与财务报表", "status": "草稿可用", "coverage": 58},
            {"name": "预算、现金流与经营分析", "status": "可用", "coverage": 62},
        ],
    }
