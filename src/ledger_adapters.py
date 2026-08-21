from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True)
class LedgerAdapter:
    id: str
    jurisdiction: str
    accounting_basis: str
    functional_currency: str
    chart_version: str
    chart: tuple[dict[str, str], ...]
    sources: tuple[dict[str, Any], ...]
    maturity: str
    guardrail: str

    def account(self, role: str) -> dict[str, str]:
        for item in self.chart:
            if item["role"] == role:
                return dict(item)
        raise ValueError(f"{self.id} 未配置总账角色：{role}")

    def line(self, role: str, amount: float | None, dimension: str = "") -> dict[str, Any]:
        account = self.account(role)
        return {
            "account": f"{account['code']} {account['name']}",
            "account_code": account["code"],
            "account_name": account["name"],
            "category": account["category"],
            "role": role,
            "amount": amount,
            "dimension": dimension,
        }

    @property
    def fingerprint(self) -> str:
        payload = {
            "id": self.id,
            "accounting_basis": self.accounting_basis,
            "functional_currency": self.functional_currency,
            "chart_version": self.chart_version,
            "chart": self.chart,
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def public_payload(self, reviews: list[dict] | None = None) -> dict[str, Any]:
        review = current_adapter_review(reviews or [], self)
        return {
            "id": self.id,
            "jurisdiction": self.jurisdiction,
            "accounting_basis": self.accounting_basis,
            "functional_currency": self.functional_currency,
            "chart_version": self.chart_version,
            "fingerprint": self.fingerprint,
            "maturity": self.maturity,
            "posting_ready": self.jurisdiction == "CN" or bool(review),
            "review": review,
            "guardrail": self.guardrail,
        }


def _account(code: str, name: str, category: str, role: str) -> dict[str, str]:
    return {"code": code, "name": name, "category": category, "role": role}


CN_SOURCES = (
    {
        "id": "mof-accounting-basic-2024",
        "authority": "财政部",
        "title": "会计基础工作规范（2024）",
        "url": "https://m.mof.gov.cn/tzgg/202408/P020240801612534470745.pdf",
        "applies_to": ["原始凭证", "记账凭证", "会计账簿", "对账", "结账"],
    },
    {
        "id": "mof-small-enterprise-standard",
        "authority": "财政部",
        "title": "小企业会计准则——会计科目、主要账务处理和财务报表",
        "url": "https://kjs.mof.gov.cn/zhengcefabu/201111/P020111118325852734144.pdf",
        "applies_to": ["会计科目", "账务处理", "财务报表"],
    },
)

SG_SOURCES = (
    {
        "id": "acra-accounting-standards",
        "authority": "ACRA",
        "title": "Accounting Standards",
        "url": "https://www.acra.gov.sg/regulations/accounting-standards-financial-reporting-surveillance/accounting-standards/",
        "applies_to": ["适用财务报告框架", "财务报表"],
    },
    {
        "id": "acra-directors-duties",
        "authority": "ACRA",
        "title": "Directors' duties",
        "url": "https://www.acra.gov.sg/manage/companies/legal-requirements-common-offences/directors-duties/",
        "applies_to": ["会计记录", "董事责任"],
    },
    {
        "id": "iras-record-keeping",
        "authority": "IRAS",
        "title": "Record Keeping Requirements",
        "url": "https://www.iras.gov.sg/taxes/corporate-income-tax/basics-of-corporate-income-tax/record-keeping-requirements",
        "applies_to": ["原始单据", "账簿", "银行记录", "留存"],
    },
)


CN_ADAPTER = LedgerAdapter(
    id="cn-small-enterprise-v1",
    jurisdiction="CN",
    accounting_basis="小企业会计准则",
    functional_currency="CNY",
    chart_version="2026.1",
    chart=(
        _account("1002", "银行存款", "资产", "cash"),
        _account("1012", "其他货币资金", "资产", "other_cash"),
        _account("1122", "应收账款", "资产", "trade_receivable"),
        _account("1123", "预付账款", "资产", "prepayment"),
        _account("1221", "其他应收款", "资产", "other_receivable"),
        _account("1601", "固定资产", "资产", "ppe"),
        _account("1602", "累计折旧", "资产", "accumulated_depreciation"),
        _account("1701", "无形资产", "资产", "intangible"),
        _account("1702", "累计摊销", "资产", "accumulated_amortization"),
        _account("1801", "长期待摊费用", "资产", "deferred_cost"),
        _account("2202", "应付账款", "负债", "trade_payable"),
        _account("2202", "应付账款", "负债", "accrued_expense"),
        _account("2211", "应付职工薪酬", "负债", "payroll_payable"),
        _account("2221", "应交税费", "负债", "tax_payable"),
        _account("2241", "其他应付款", "负债", "other_payable"),
        _account("3001", "实收资本", "权益", "share_capital"),
        _account("3103", "本年利润", "权益", "current_profit"),
        _account("5001", "主营业务收入", "收入", "game_revenue"),
        _account("5051", "其他业务收入", "收入", "other_revenue"),
        _account("5401", "主营业务成本", "成本费用", "cost_of_sales"),
        _account("5602", "管理费用", "成本费用", "operating_expense"),
        _account("5603", "财务费用", "成本费用", "finance_cost"),
        _account("5711", "营业外支出", "成本费用", "other_expense"),
        _account("5801", "所得税费用", "成本费用", "income_tax_expense"),
    ),
    sources=CN_SOURCES,
    maturity="posting_assist",
    guardrail="中国主体采用小企业会计准则参考映射；凭证仍需会计复核后正式过账。",
)


SG_ADAPTER = LedgerAdapter(
    id="sg-internal-ledger-v1",
    jurisdiction="SG",
    accounting_basis="SFRS",
    functional_currency="USD",
    chart_version="2026.1",
    chart=(
        _account("1100", "Cash at bank / 银行存款", "资产", "cash"),
        _account("1200", "Trade receivables / 贸易应收", "资产", "trade_receivable"),
        _account("1300", "Prepayments / 预付款", "资产", "prepayment"),
        _account("1600", "Property, plant and equipment / 固定资产", "资产", "ppe"),
        _account("1650", "Accumulated depreciation / 累计折旧", "资产", "accumulated_depreciation"),
        _account("1700", "Intangible assets / 无形资产", "资产", "intangible"),
        _account("1750", "Accumulated amortisation / 累计摊销", "资产", "accumulated_amortization"),
        _account("1800", "Deferred costs / 递延成本", "资产", "deferred_cost"),
        _account("2100", "Trade payables / 贸易应付", "负债", "trade_payable"),
        _account("2200", "Accrued expenses / 应计费用", "负债", "accrued_expense"),
        _account("2300", "Payroll payable / 应付薪酬", "负债", "payroll_payable"),
        _account("2400", "Tax payable / 应付税款", "负债", "tax_payable"),
        _account("2500", "Other payables / 其他应付", "负债", "other_payable"),
        _account("3000", "Share capital / 股本", "权益", "share_capital"),
        _account("3100", "Retained earnings / 留存收益", "权益", "current_profit"),
        _account("4000", "Game revenue / 游戏收入", "收入", "game_revenue"),
        _account("4100", "Other revenue / 其他收入", "收入", "other_revenue"),
        _account("5000", "Cost of sales / 销售成本", "成本费用", "cost_of_sales"),
        _account("6100", "Operating expenses / 经营费用", "成本费用", "operating_expense"),
        _account("6200", "Finance costs / 财务费用", "成本费用", "finance_cost"),
        _account("6300", "Other expenses / 其他费用", "成本费用", "other_expense"),
        _account("7000", "Income tax expense / 所得税费用", "成本费用", "income_tax_expense"),
    ),
    sources=SG_SOURCES,
    maturity="bookkeeping_workpaper",
    guardrail=(
        "这是新加坡主体的内部账簿角色映射，不是 ACRA 规定的统一科目表，也不代表已满足 SFRS、"
        "年度财务报表或 XBRL 申报要求；首次过账前必须由当地会计复核并批准该版本映射。"
    ),
)


def get_ledger_adapter(company_profile: dict | None) -> LedgerAdapter:
    profile = company_profile or {}
    jurisdiction = str(profile.get("jurisdiction") or "CN").upper()
    if jurisdiction == "SG":
        functional = str(profile.get("functional_currency") or "USD").upper()
        basis = str(profile.get("accounting_basis") or "SFRS")
        return LedgerAdapter(
            **{**SG_ADAPTER.__dict__, "functional_currency": functional, "accounting_basis": basis}
        )
    return CN_ADAPTER


def functional_rate(currency: str, adapter: LedgerAdapter, fx_rates: dict[str, float] | None) -> float:
    """Return functional-currency units for one unit of transaction currency.

    Supports explicit `FROM/TO`, inverse `TO/FROM`, and the legacy CN shorthand
    where `USD: 7.2` means CNY per USD. Ambiguous shorthand is not used outside CN.
    """
    currency = str(currency or adapter.functional_currency).upper()
    functional = adapter.functional_currency.upper()
    if currency == functional:
        return 1.0
    rates = fx_rates or {}
    direct = rates.get(f"{currency}/{functional}")
    if direct:
        return float(direct)
    inverse = rates.get(f"{functional}/{currency}")
    if inverse:
        return 1.0 / float(inverse)
    if functional == "CNY" and rates.get(currency):
        return float(rates[currency])
    return 0.0


def current_adapter_review(reviews: list[dict], adapter: LedgerAdapter) -> dict | None:
    candidates = [
        item for item in reviews
        if item.get("adapter_id") == adapter.id
        and item.get("adapter_fingerprint") == adapter.fingerprint
        and item.get("decision") == "批准"
    ]
    return max(candidates, key=lambda item: item.get("reviewed_at") or "", default=None)


def create_adapter_review(
    adapter: LedgerAdapter, entity_id: str, decision: str, actor: str,
    rationale: str, evidence: list[str] | None = None,
) -> dict[str, Any]:
    decision = str(decision or "").strip()
    actor = str(actor or "").strip()
    rationale = str(rationale or "").strip()
    evidence_items = [str(item).strip() for item in (evidence or []) if str(item).strip()]
    if decision not in {"批准", "退回"}:
        raise ValueError("总账适配器复核决定只能是批准或退回")
    if not actor or len(rationale) < 8:
        raise ValueError("请填写当地会计复核人和至少8个字的复核依据")
    if decision == "批准" and not evidence_items:
        raise ValueError("批准科目映射必须至少附一项会计政策或科目映射证据")
    reviewed_at = datetime.now(timezone.utc).isoformat()
    identity = f"{entity_id}|{adapter.fingerprint}|{decision}|{reviewed_at}"
    return {
        "id": f"LAR-{hashlib.sha1(identity.encode('utf-8')).hexdigest()[:12].upper()}",
        "entity_id": str(entity_id or "").strip(),
        "adapter_id": adapter.id,
        "adapter_fingerprint": adapter.fingerprint,
        "chart_version": adapter.chart_version,
        "accounting_basis": adapter.accounting_basis,
        "functional_currency": adapter.functional_currency,
        "decision": decision,
        "actor": actor[:80],
        "rationale": rationale[:2000],
        "evidence": evidence_items[:50],
        "reviewed_at": reviewed_at,
    }
