from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timezone
from typing import Any, Iterable

from .ledger_adapters import LedgerAdapter, functional_rate, get_ledger_adapter


ASSET_TYPES = {"固定资产", "无形资产", "长期待摊费用", "预付费用"}
VALID_REVIEW_DECISIONS = {"批准", "退回"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _number(value: Any) -> float:
    try:
        return round(float(value or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def _rate(value: Any) -> float:
    try:
        rate = float(value or 0)
        return rate if rate > 0 else 0.0
    except (TypeError, ValueError):
        return 0.0


def _period(value: str) -> str:
    value = str(value or "").strip()[:7]
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value):
        raise ValueError("期间必须为 YYYY-MM")
    return value


def _shift_period(period: str, months: int) -> str:
    year, month = map(int, _period(period).split("-"))
    absolute = year * 12 + month - 1 + months
    return f"{absolute // 12:04d}-{absolute % 12 + 1:02d}"


def _period_index(period: str) -> int:
    year, month = map(int, _period(period).split("-"))
    return year * 12 + month - 1


def _id(prefix: str, values: Iterable[Any]) -> str:
    seed = "|".join(str(value) for value in values)
    return f"{prefix}-{hashlib.sha1(seed.encode()).hexdigest()[:14].upper()}"


def create_asset_card(
    name: str, asset_type: str, acquisition_date: str, original_cost: float,
    useful_months: int, residual_value: float, project: str, vendor: str,
    evidence: Iterable[str], actor: str, *, currency: str = "CNY", cny_cost: float | None = None,
    functional_currency: str = "CNY", functional_cost: float | None = None,
    fx_rate: float | None = None, ledger_adapter_id: str = "",
) -> dict:
    if asset_type not in ASSET_TYPES:
        raise ValueError("资产类别必须是固定资产、无形资产、长期待摊费用或预付费用")
    try:
        acquired = date.fromisoformat(acquisition_date)
    except ValueError as error:
        raise ValueError("取得日期必须为 YYYY-MM-DD") from error
    actor, name = str(actor or "").strip(), str(name or "").strip()
    if not actor or not name:
        raise ValueError("资产名称和提交人不能为空")
    cost, residual = _number(original_cost), _number(residual_value)
    try:
        months = int(useful_months)
    except (TypeError, ValueError) as error:
        raise ValueError("预计使用或受益月数必须是整数") from error
    if cost <= 0 or months <= 0 or months > 600:
        raise ValueError("资产成本必须大于0，使用或受益月数应在1至600个月")
    if residual < 0 or residual >= cost:
        raise ValueError("预计净残值必须大于等于0且小于资产成本")
    currency = str(currency or functional_currency or "CNY").upper()
    functional_currency = str(functional_currency or "CNY").upper()
    rate = 1.0 if currency == functional_currency else _rate(fx_rate)
    base_cost = _number(functional_cost if functional_cost is not None else cny_cost)
    if not base_cost and rate:
        base_cost = round(cost * rate, 2)
    if base_cost <= 0:
        raise ValueError(f"外币资产必须填写有来源的 {functional_currency} 本位币入账成本或折算汇率")
    functional_residual = round(residual * rate, 2) if rate else residual
    if functional_residual < 0 or functional_residual >= base_cost:
        raise ValueError("折算后的预计净残值必须大于等于0且小于本位币资产成本")
    evidence_items = [str(item).strip()[:500] for item in evidence if str(item).strip()]
    blockers = []
    if not evidence_items:
        blockers.append("缺少合同、发票、付款或验收证据")
    if not project:
        blockers.append("缺少资产归属项目或成本中心")
    start_period = acquired.strftime("%Y-%m")
    if asset_type in {"固定资产", "无形资产"}:
        start_period = _shift_period(start_period, 1)
    monthly_amount = round((base_cost - functional_residual) / months, 2)
    card_id = _id("AST", [name, asset_type, acquisition_date, base_cost, vendor, _now()])
    return {
        "id": card_id, "name": name[:160], "asset_type": asset_type,
        "acquisition_date": acquisition_date, "start_period": start_period,
        "original_currency": currency, "original_cost": cost, "fx_rate": rate or None,
        "functional_currency": functional_currency, "functional_cost": base_cost,
        "functional_residual_value": functional_residual,
        "cny_cost": base_cost if functional_currency == "CNY" else None,
        "ledger_adapter_id": str(ledger_adapter_id or ""),
        "residual_value": residual, "useful_months": months, "monthly_amount": monthly_amount,
        "project": str(project or "")[:120], "vendor": str(vendor or "")[:160],
        "evidence": evidence_items, "submitted_by": actor[:80], "submitted_at": _now(),
        "status": "阻塞" if blockers else "待会计复核", "blockers": blockers,
        "review": None,
        "agent_judgement": {
            "recommendation": "按预计受益期直线法生成月度折旧/摊销草稿；类别和年限由会计结合金额重要性、实际用途及公司政策复核。",
            "impact": "年限越短，本期及近期费用越高；年限越长，利润更高但资产余额更大。",
            "options": [
                {"name": f"{months}个月直线法", "recommended": True, "benefit": "与当前业务提供的受益期一致", "cost": "需每年复核剩余受益期"},
                {"name": "一次性费用化", "recommended": False, "benefit": "处理简单", "cost": "若金额重大或跨期受益，会使当期利润偏低"},
            ],
        },
    }


def review_accounting_item(item: dict, decision: str, actor: str, rationale: str) -> dict:
    if decision not in VALID_REVIEW_DECISIONS:
        raise ValueError("复核决定只能是批准或退回")
    actor, rationale = str(actor or "").strip(), str(rationale or "").strip()
    if not actor or len(rationale) < 4:
        raise ValueError("请填写复核人和至少4个字的判断依据")
    if decision == "批准" and item.get("blockers"):
        raise ValueError("仍有资料或配置缺口，不能批准")
    updated = dict(item)
    updated["status"] = "已批准" if decision == "批准" else "已退回"
    updated["review"] = {
        "decision": decision, "actor": actor[:80], "rationale": rationale[:1000], "timestamp": _now(),
    }
    return updated


def asset_schedule(card: dict) -> list[dict]:
    if card.get("status") != "已批准":
        return []
    months = int(card["useful_months"])
    functional_cost = _number(card.get("functional_cost") or card.get("cny_cost"))
    functional_residual = _number(card.get("functional_residual_value", card.get("residual_value")))
    depreciable = round(functional_cost - functional_residual, 2)
    regular = _number(card["monthly_amount"])
    rows, accumulated = [], 0.0
    for index in range(months):
        amount = regular if index < months - 1 else round(depreciable - accumulated, 2)
        accumulated = round(accumulated + amount, 2)
        rows.append({
            "period": _shift_period(card["start_period"], index), "sequence": index + 1,
            "amount": amount, "accumulated": accumulated,
            "closing_net_value": round(functional_cost - accumulated, 2),
        })
    return rows


def create_accrual(
    period: str, description: str, amount: float, expense_account: str,
    counterparty: str, project: str, evidence: Iterable[str], actor: str,
    *, source_id: str = "", auto_reverse: bool = True, currency: str = "CNY",
    functional_currency: str = "CNY", functional_amount: float | None = None,
    fx_rate: float | None = None, expense_role: str = "",
    ledger_adapter_id: str = "",
) -> dict:
    period = _period(period)
    actor, description = str(actor or "").strip(), str(description or "").strip()
    if not actor or not description:
        raise ValueError("暂估事项和提交人不能为空")
    amount = _number(amount)
    if amount <= 0:
        raise ValueError("暂估金额必须大于0")
    if not expense_role and not re.match(r"^5\d{3}\s+", str(expense_account or "")):
        raise ValueError("暂估借方必须选择成本费用科目编码和名称")
    currency = str(currency or functional_currency or "CNY").upper()
    functional_currency = str(functional_currency or "CNY").upper()
    rate = 1.0 if currency == functional_currency else _rate(fx_rate)
    base_amount = _number(functional_amount)
    if not base_amount and rate:
        base_amount = round(amount * rate, 2)
    if base_amount <= 0:
        raise ValueError(f"外币暂估必须填写有来源的 {functional_currency} 本位币金额或折算汇率")
    evidence_items = [str(item).strip()[:500] for item in evidence if str(item).strip()]
    blockers = [] if evidence_items else ["缺少合同、订单、验收或服务进度证据"]
    accrual_id = _id("ACR", [period, description, amount, counterparty, source_id, _now()])
    return {
        "id": accrual_id, "period": period, "description": description[:300], "amount": amount,
        "currency": currency, "fx_rate": rate or None,
        "functional_currency": functional_currency, "functional_amount": base_amount,
        "ledger_adapter_id": str(ledger_adapter_id or ""), "expense_role": expense_role,
        "expense_account": str(expense_account)[:120], "credit_account": "2202 应付账款",
        "counterparty": str(counterparty or "待识别供应商")[:160],
        "project": str(project or "待分配项目")[:120], "source_id": str(source_id or "")[:120],
        "evidence": evidence_items, "auto_reverse": bool(auto_reverse),
        "reversal_period": _shift_period(period, 1) if auto_reverse else None,
        "submitted_by": actor[:80], "submitted_at": _now(),
        "status": "阻塞" if blockers else "待会计复核", "blockers": blockers, "review": None,
        "agent_judgement": {
            "recommendation": "服务已发生但发票未到时先按可靠金额暂估，下月自动冲回并用发票或最终结算重记。",
            "impact": "不暂估会使本期成本偏低、利润虚高；暂估过高则相反，因此需保留验收或进度证据。",
        },
    }


def build_adjustment_vouchers(
    asset_cards: Iterable[dict], accruals: Iterable[dict], period: str,
    adapter: LedgerAdapter | None = None,
) -> list[dict]:
    period = _period(period)
    adapter = adapter or get_ledger_adapter({})
    vouchers = []
    for card in asset_cards:
        schedule_row = next((row for row in asset_schedule(card) if row["period"] == period), None)
        if not schedule_row:
            continue
        asset_type = card["asset_type"]
        credit_role = {
            "固定资产": "accumulated_depreciation", "无形资产": "accumulated_amortization",
            "长期待摊费用": "deferred_cost", "预付费用": "prepayment",
        }[asset_type]
        amount = schedule_row["amount"]
        vouchers.append({
            "id": f"ADJ-{period.replace('-', '')}-{card['id']}", "date": f"{period}-28",
            "type": "折旧摊销", "summary": f"计提{card['name']}本月折旧/摊销",
            "source": "已复核资产卡片", "source_count": 1,
            "original_currency": card.get("functional_currency") or adapter.functional_currency,
            "original_amount": amount, "functional_currency": adapter.functional_currency,
            "functional_amount": amount, "ledger_adapter_id": adapter.id,
            "debit": [adapter.line("operating_expense", amount, card["project"])],
            "credit": [adapter.line(credit_role, amount, card["name"])],
            "balanced": True, "status": "待复核", "review_role": "会计服务机构",
            "blockers": ["请确认本月资产仍在使用且未发生减值、处置或受益期变化"],
            "judgement": card["agent_judgement"], "evidence": card.get("evidence") or [],
            "authority_ids": [source["id"] for source in adapter.sources[:2]],
        })
    for accrual in accruals:
        if accrual.get("status") != "已批准":
            continue
        reverse = accrual.get("auto_reverse") and accrual.get("reversal_period") == period
        if accrual.get("period") != period and not reverse:
            continue
        amount = _number(accrual.get("functional_amount") or accrual.get("amount"))
        expense_role = accrual.get("expense_role") or (
            "cost_of_sales" if str(accrual.get("expense_account") or "").startswith("5401")
            else "operating_expense"
        )
        debit = adapter.line(expense_role, amount, accrual["project"])
        credit = adapter.line("accrued_expense", amount, accrual["counterparty"])
        summary = f"暂估{accrual['description']}"
        if reverse:
            debit, credit = credit, debit
            summary = f"冲回上月暂估：{accrual['description']}"
        vouchers.append({
            "id": f"{'REV' if reverse else 'ACR'}-{period.replace('-', '')}-{accrual['id']}",
            "date": f"{period}-01" if reverse else f"{period}-28", "type": "暂估冲回" if reverse else "费用暂估",
            "summary": summary, "source": "已复核暂估单", "source_count": 1,
            "original_currency": accrual.get("currency") or adapter.functional_currency,
            "original_amount": _number(accrual.get("amount")), "fx_rate": accrual.get("fx_rate"),
            "functional_currency": adapter.functional_currency, "functional_amount": amount,
            "ledger_adapter_id": adapter.id, "debit": [debit], "credit": [credit],
            "balanced": True, "status": "待复核", "review_role": "会计服务机构", "blockers": [],
            "judgement": accrual["agent_judgement"], "evidence": accrual.get("evidence") or [],
            "authority_ids": [source["id"] for source in adapter.sources[:2]],
        })
    return vouchers


def build_expense_vouchers(
    expense_claims: Iterable[dict], period: str, adapter: LedgerAdapter | None = None,
    fx_rates: dict[str, float] | None = None,
) -> list[dict]:
    period = _period(period)
    adapter = adapter or get_ledger_adapter({})
    fx_rates = fx_rates or {}
    vouchers = []
    role_by_category = {
        "差旅": "operating_expense", "办公": "operating_expense", "招待": "operating_expense",
        "广告投放": "operating_expense", "素材制作": "cost_of_sales",
    }
    for index, claim in enumerate(expense_claims, 1):
        if not str(claim.get("claim_date") or "").startswith(period) or claim.get("status") not in {"已批准待付款", "已支付"}:
            continue
        amount = _number(claim.get("approved_amount"))
        if amount <= 0:
            continue
        category = str(claim.get("category") or "待分类费用")
        expense_role = role_by_category.get(category, "operating_expense")
        currency = str(claim.get("currency") or adapter.functional_currency).upper()
        rate = functional_rate(currency, adapter, fx_rates)
        functional_amount = round(amount * rate, 2) if rate else None
        blockers = [] if functional_amount is not None else [
            f"缺少 {currency}/{adapter.functional_currency} 本位币折算汇率"
        ]
        vouchers.append({
            "id": f"EXP-{period.replace('-', '')}-{index:03d}-{claim.get('id')}",
            "date": claim.get("claim_date"), "type": "费用报销",
            "summary": f"确认{claim.get('project') or '项目'} {category}报销",
            "source": "已批准费用报销单", "source_count": 1,
            "original_currency": currency, "original_amount": amount, "fx_rate": rate or None,
            "functional_currency": adapter.functional_currency, "functional_amount": functional_amount,
            "ledger_adapter_id": adapter.id,
            "debit": [adapter.line(expense_role, functional_amount, claim.get("project") or "待分配项目")],
            "credit": [adapter.line("other_payable", functional_amount, claim.get("claimant") or "报销人")],
            "balanced": functional_amount is not None,
            "status": "阻塞" if blockers else "待复核", "review_role": "财务负责人", "blockers": blockers,
            "judgement": {
                "question": "费用用途、项目归属和批准金额是否与原始单据一致？",
                "agent_recommendation": "按已批准金额确认费用及员工往来，实际付款后再核销其他应付款。",
                "impact": "影响项目成本、本期利润和员工往来余额。",
            },
            "evidence": claim.get("evidence") or [],
            "authority_ids": [source["id"] for source in adapter.sources[:2]],
        })
    return vouchers


def post_reviewed_vouchers(
    vouchers: Iterable[dict], reviews: dict[str, dict], existing: Iterable[dict],
    period: str, actor: str, entity_id: str = "",
) -> dict:
    period = _period(period)
    actor = str(actor or "").strip()
    if not actor:
        raise ValueError("请填写过账执行人")
    entity_id = str(entity_id or "").strip()
    posted = list(existing)
    by_source = {
        item.get("source_voucher_id"): item for item in posted
        if item.get("status") == "已过账" and str(item.get("entity_id") or "") == entity_id
    }
    created, skipped = [], []
    for voucher in vouchers:
        voucher_id = voucher.get("id")
        if reviews.get(voucher_id, {}).get("decision") != "接受":
            skipped.append({"voucher_id": voucher_id, "reason": "尚未接受"})
            continue
        if voucher.get("status") == "阻塞" or not voucher.get("balanced"):
            skipped.append({"voucher_id": voucher_id, "reason": "凭证阻塞或借贷不平"})
            continue
        canonical = json.dumps({
            "date": voucher.get("date"), "summary": voucher.get("summary"),
            "debit": voucher.get("debit"), "credit": voucher.get("credit"),
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        content_hash = hashlib.sha256(canonical.encode()).hexdigest()
        if voucher_id in by_source:
            if by_source[voucher_id].get("content_hash") != content_hash:
                raise ValueError(f"已过账凭证 {voucher_id} 的内容发生变化；请生成冲销或调整凭证，不能覆盖")
            skipped.append({"voucher_id": voucher_id, "reason": "已经过账"})
            continue
        entry = {
            "id": _id("POST", [entity_id, period, voucher_id, content_hash]), "period": period,
            "entity_id": entity_id,
            "source_voucher_id": voucher_id, "date": voucher.get("date"),
            "type": voucher.get("type"), "summary": voucher.get("summary"),
            "debit": voucher.get("debit") or [], "credit": voucher.get("credit") or [],
            "content_hash": content_hash, "status": "已过账", "posted_by": actor[:80], "posted_at": _now(),
            "review": reviews[voucher_id], "source": voucher.get("source"), "evidence": voucher.get("evidence") or [],
        }
        posted.append(entry)
        by_source[voucher_id] = entry
        created.append(entry)
    return {"records": posted, "created": created, "skipped": skipped}


def posted_trial_balance(posted_vouchers: Iterable[dict], period: str, entity_id: str = "") -> dict:
    period = _period(period)
    entity_id = str(entity_id or "").strip()
    accounts: dict[str, dict[str, float]] = {}
    count = 0
    source_voucher_ids = []
    for voucher in posted_vouchers:
        if voucher.get("period") != period or voucher.get("status") != "已过账":
            continue
        if entity_id and str(voucher.get("entity_id") or "") != entity_id:
            continue
        count += 1
        source_voucher_ids.append(voucher.get("source_voucher_id"))
        for side, sign in (("debit", 1), ("credit", -1)):
            for line in voucher.get(side) or []:
                account = str(line.get("account") or "待识别科目")
                bucket = accounts.setdefault(account, {"debit": 0.0, "credit": 0.0})
                bucket[side] += _number(line.get("amount"))
                bucket.update({
                    key: line.get(key) for key in
                    ("account_code", "account_name", "category", "role") if line.get(key)
                })
    rows = [{"account": account, "debit": round(v["debit"], 2), "credit": round(v["credit"], 2),
             "net": round(v["debit"] - v["credit"], 2),
             **{key: v.get(key) for key in ("account_code", "account_name", "category", "role") if v.get(key)}}
            for account, v in sorted(accounts.items())]
    debit = round(sum(row["debit"] for row in rows), 2)
    credit = round(sum(row["credit"] for row in rows), 2)
    return {"period": period, "entity_id": entity_id, "rows": rows, "debit": debit, "credit": credit,
            "difference": round(debit - credit, 2), "balanced": abs(debit - credit) < 0.01,
            "posted_voucher_count": count, "source_voucher_ids": source_voucher_ids}


def roll_forward_opening_balances(
    current_opening: Iterable[dict], posted_balance: dict, period: str, actor: str,
    profit_account: str = "3103 本年利润",
) -> dict:
    period = _period(period)
    if not posted_balance.get("balanced"):
        raise ValueError("已过账试算不平，不能结转期末余额")
    actor = str(actor or "").strip()
    if not actor:
        raise ValueError("请填写结转执行人")
    next_period = _shift_period(period, 1)
    balances: dict[str, dict] = {}
    current_profit = 0.0
    for row in current_opening:
        if row.get("period") != period:
            continue
        account = row.get("account") or f"{row.get('account_code', '')} {row.get('account_name', '')}".strip()
        balances[account] = {
            "debit": _number(row.get("opening_debit")), "credit": _number(row.get("opening_credit")),
            "category": row.get("category") or "待映射",
        }
    for row in posted_balance.get("rows") or []:
        account = row["account"]
        code = account.split(" ", 1)[0]
        category = row.get("category")
        if category == "收入" or code.startswith(("5001", "5051")):
            current_profit += _number(row.get("credit")) - _number(row.get("debit"))
            continue
        if category == "成本费用" or code.startswith(("5401", "5602", "5603", "5711", "5801")):
            current_profit -= _number(row.get("debit")) - _number(row.get("credit"))
            continue
        bucket = balances.setdefault(account, {"debit": 0.0, "credit": 0.0, "category": category or "待映射"})
        net = bucket["debit"] - bucket["credit"] + _number(row.get("debit")) - _number(row.get("credit"))
        bucket["debit"], bucket["credit"] = round(max(0, net), 2), round(max(0, -net), 2)
    if abs(current_profit) >= 0.01:
        profit = balances.setdefault(profit_account, {"debit": 0.0, "credit": 0.0, "category": "权益"})
        net = profit["debit"] - profit["credit"] - current_profit
        profit["debit"], profit["credit"] = round(max(0, net), 2), round(max(0, -net), 2)
    records = []
    for account, balance in sorted(balances.items()):
        code, _, name = account.partition(" ")
        if balance["debit"] == 0 and balance["credit"] == 0:
            continue
        records.append({
            "id": _id("OPEN", [next_period, account]), "source_file": "系统关账结转",
            "source_sheet": period, "source_row": len(records) + 1, "period": next_period,
            "account_code": code, "account_name": name, "account": account,
            "category": balance["category"], "opening_debit": balance["debit"],
            "opening_credit": balance["credit"], "status": "可用", "anomalies": [],
            "roll_forward": {"from_period": period, "actor": actor[:80], "timestamp": _now()},
        })
    debit = round(sum(row["opening_debit"] for row in records), 2)
    credit = round(sum(row["opening_credit"] for row in records), 2)
    if abs(debit - credit) >= 0.01:
        raise ValueError(f"结转后期初借贷不平，差额 {debit - credit:,.2f}")
    return {
        "period": next_period, "records": records, "debit": debit, "credit": credit, "balanced": True,
        "current_profit_rolled_to_equity": round(current_profit, 2),
    }
