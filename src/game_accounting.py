from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any, Iterable

from .ledger_adapters import LedgerAdapter, functional_rate, get_ledger_adapter


PRESENTATIONS = {"总额法", "净额法"}
RECOGNITION_METHODS = {
    "即时确认", "按消耗确认", "按服务期直线确认", "按预计玩家受益期确认", "按广告交付确认",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _number(value: Any) -> float:
    try:
        return round(float(value or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def _period(value: str) -> str:
    value = str(value or "").strip()[:7]
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value):
        raise ValueError("生效期间必须为 YYYY-MM")
    return value


def _shift(period: str, months: int) -> str:
    year, month = map(int, _period(period).split("-"))
    index = year * 12 + month - 1 + months
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def _index(period: str) -> int:
    year, month = map(int, _period(period).split("-"))
    return year * 12 + month - 1


def create_revenue_policy(
    game: str, channel: str, revenue_stream: str, presentation: str,
    recognition_method: str, effective_from: str, actor: str,
    evidence: Iterable[str], *, service_months: int | None = None,
    role_facts: dict | None = None, entity_id: str = "",
) -> dict:
    game, channel = str(game or "").strip(), str(channel or "").strip()
    actor = str(actor or "").strip()
    if not game or not channel or not actor:
        raise ValueError("游戏、渠道和提交人不能为空")
    if presentation not in PRESENTATIONS:
        raise ValueError("收入展示口径必须是总额法或净额法")
    if recognition_method not in RECOGNITION_METHODS:
        raise ValueError("收入确认方法无效")
    effective_from = _period(effective_from)
    months = int(service_months or 0)
    if recognition_method in {"按服务期直线确认", "按预计玩家受益期确认"} and not 1 <= months <= 120:
        raise ValueError("服务期或预计玩家受益期应在1至120个月")
    evidence_items = [str(item).strip()[:500] for item in evidence if str(item).strip()]
    role_facts = role_facts or {}
    blockers = []
    for field, label in (
        ("controls_pricing", "谁控制定价"), ("responsible_for_fulfillment", "谁主要负责履约"),
        ("bears_refund_risk", "谁承担退款/信用风险"), ("controls_virtual_goods", "谁控制虚拟商品或服务"),
    ):
        if role_facts.get(field) not in {True, False}:
            blockers.append(f"待确认：{label}")
    if not evidence_items:
        blockers.append("缺少渠道协议、用户条款、后台数据或既有会计口径证据")
    policy_id = "REV-POL-" + hashlib.sha1(
        f"{entity_id}|{game}|{channel}|{revenue_stream}|{effective_from}|{_now()}".encode()
    ).hexdigest()[:12].upper()
    principal_score = sum(bool(role_facts.get(field)) for field in (
        "controls_pricing", "responsible_for_fulfillment", "bears_refund_risk", "controls_virtual_goods",
    ))
    recommended_presentation = "总额法" if principal_score >= 3 else "净额法" if principal_score <= 1 else "需进一步判断"
    return {
        "id": policy_id, "entity_id": str(entity_id or "").strip(),
        "game": game[:120], "channel": channel[:160],
        "revenue_stream": str(revenue_stream or "游戏内购")[:120],
        "presentation": presentation, "recognition_method": recognition_method,
        "service_months": months or None, "effective_from": effective_from,
        "role_facts": role_facts, "evidence": evidence_items,
        "status": "阻塞" if blockers else "待会计复核", "blockers": blockers,
        "submitted_by": actor[:80], "submitted_at": _now(), "review": None,
        "agent_judgement": {
            "recommendation": (
                f"当前业务事实更倾向 {recommended_presentation}；用户选择为 {presentation}，请会计重点复核不一致之处。"
                if recommended_presentation != "需进一步判断" else
                "主要责任人指标结论分散，不能仅按平台名称决定总额或净额，建议逐条核对协议和实际履约。"
            ),
            "recommended_presentation": recommended_presentation,
            "impact": "总额法会同时放大收入和渠道成本，净额法只列公司所得净额；通常不改变相同交易下的毛利额，但会显著影响收入规模和毛利率。",
            "recognition_impact": {
                "即时确认": "当期收入最高，需证明履约已在当期完成。",
                "按消耗确认": "与玩家实际消耗匹配，但必须持续取得消耗数据。",
                "按服务期直线确认": "将订阅或持续服务收入分摊至服务期。",
                "按预计玩家受益期确认": "适用于耐久型虚拟物品，依赖玩家生命周期估计并需定期复核。",
                "按广告交付确认": "只确认已交付的曝光、点击或约定广告服务。",
            }[recognition_method],
        },
    }


def review_revenue_policy(policy: dict, decision: str, actor: str, rationale: str) -> dict:
    if decision not in {"批准", "退回"}:
        raise ValueError("复核决定只能是批准或退回")
    actor, rationale = str(actor or "").strip(), str(rationale or "").strip()
    if not actor or len(rationale) < 4:
        raise ValueError("请填写会计复核人和至少4个字的判断依据")
    if decision == "批准" and policy.get("blockers"):
        raise ValueError("角色事实或证据仍不完整，不能批准收入政策")
    updated = dict(policy)
    updated["status"] = "已批准" if decision == "批准" else "已退回"
    updated["review"] = {"decision": decision, "actor": actor[:80], "rationale": rationale[:1000], "timestamp": _now()}
    return updated


def _policy_for(record: dict, policies: list[dict]) -> dict | None:
    candidates = [
        policy for policy in policies if policy.get("status") == "已批准"
        and policy.get("game") == record.get("game") and policy.get("channel") == record.get("channel")
        and str(policy.get("effective_from") or "") <= str(record.get("period") or "")
    ]
    return max(candidates, key=lambda item: item.get("effective_from") or "", default=None)


def build_revenue_recognition(
    settlements: Iterable[dict], policies: Iterable[dict], target_period: str,
) -> dict:
    target_period = _period(target_period)
    policies = list(policies)
    rows, blockers = [], []
    for record in settlements:
        if record.get("release_status") not in {None, "", "released"}:
            continue
        source_period = str(record.get("period") or "")
        if not re.fullmatch(r"\d{4}-\d{2}", source_period) or source_period > target_period:
            continue
        policy = _policy_for(record, policies)
        if not policy:
            if source_period == target_period:
                missing = {
                    "settlement_id": record.get("id"), "game": record.get("game"),
                    "channel": record.get("channel"), "reason": "缺少已批准的游戏收入会计政策",
                }
                blockers.append(missing)
                rows.append({
                    "id": f"REVREC-{target_period.replace('-', '')}-{record.get('id')}",
                    "settlement_id": record.get("id"), "source_period": source_period,
                    "period": target_period, "game": record.get("game"), "channel": record.get("channel"),
                    "currency": record.get("currency") or "CNY", "policy_id": None,
                    "presentation": None, "method": None, "billed_basis": None,
                    "recognized_revenue": None, "deferred_from_source": None, "channel_cost": None,
                    "recognition_evidence": "待补已批准收入政策",
                    "status": "阻塞", "blockers": [missing["reason"]],
                })
            continue
        presentation = policy["presentation"]
        gross = _number(record.get("gross")) - _number(record.get("refunds"))
        net = _number(record.get("net_receivable") or record.get("settlement_amount"))
        billed_basis = gross if presentation == "总额法" else net
        method = policy["recognition_method"]
        months = int(policy.get("service_months") or 1)
        recognized = 0.0
        recognition_evidence = ""
        record_blockers = []
        if method == "即时确认":
            recognized = billed_basis if source_period == target_period else 0.0
            recognition_evidence = "已批准政策：履约在交易期间完成"
        elif method in {"按服务期直线确认", "按预计玩家受益期确认"}:
            offset = _index(target_period) - _index(source_period)
            if 0 <= offset < months:
                regular = round(billed_basis / months, 2)
                recognized = regular if offset < months - 1 else round(billed_basis - regular * (months - 1), 2)
            recognition_evidence = f"按 {months} 个月直线分摊"
        elif method == "按消耗确认":
            ratios = record.get("consumption_ratios") or {}
            ratio = ratios.get(target_period)
            if ratio is None:
                record_blockers.append("缺少本期玩家消耗比例")
            elif not 0 <= float(ratio) <= 1:
                record_blockers.append("玩家消耗比例必须在0至1之间")
            else:
                recognized = round(billed_basis * float(ratio), 2)
                recognition_evidence = f"本期消耗比例 {float(ratio):.2%}"
        else:  # 广告交付
            delivered = _number((record.get("advertising_delivery") or {}).get(target_period))
            if not delivered and source_period == target_period:
                record_blockers.append("缺少本期已交付广告金额或平台报告")
            recognized = min(billed_basis, delivered)
            recognition_evidence = "按平台或客户确认的已交付广告金额"
        if record_blockers:
            blockers.append({"settlement_id": record.get("id"), "reason": "；".join(record_blockers)})
        if recognized or source_period == target_period:
            rows.append({
                "id": f"REVREC-{target_period.replace('-', '')}-{record.get('id')}",
                "settlement_id": record.get("id"), "source_period": source_period,
                "period": target_period, "game": record.get("game"), "channel": record.get("channel"),
                "currency": record.get("currency") or "CNY", "policy_id": policy["id"],
                "presentation": presentation, "method": method,
                "billed_basis": billed_basis, "recognized_revenue": round(recognized, 2),
                "deferred_from_source": round(max(0, billed_basis - recognized), 2) if source_period == target_period else None,
                "channel_cost": round(max(0, gross - net), 2) if presentation == "总额法" else 0.0,
                "recognition_evidence": recognition_evidence,
                "status": "阻塞" if record_blockers else "待凭证复核", "blockers": record_blockers,
            })
    by_currency = {}
    for row in rows:
        bucket = by_currency.setdefault(row["currency"], {"billed_basis": 0.0, "recognized_revenue": 0.0, "channel_cost": 0.0})
        for key in bucket:
            bucket[key] += _number(row.get(key))
    return {
        "period": target_period, "rows": rows, "blockers": blockers,
        "summary_by_currency": [
            {"currency": currency, **{key: round(value, 2) for key, value in values.items()}}
            for currency, values in sorted(by_currency.items())
        ],
        "guardrail": "总额/净额和履约时点来自已复核政策；渠道名称、币种和到账时间都不能单独决定收入确认。",
    }


def build_game_revenue_vouchers(
    recognition: dict, period: str, fx_rates: dict[str, float] | None = None,
    adapter: LedgerAdapter | None = None,
) -> list[dict]:
    period = _period(period)
    fx_rates = fx_rates or {}
    adapter = adapter or get_ledger_adapter({})
    vouchers = []
    for index, row in enumerate(recognition.get("rows") or [], 1):
        amount = _number(row.get("recognized_revenue"))
        blockers = list(row.get("blockers") or [])
        currency = str(row.get("currency") or "CNY").upper()
        rate = functional_rate(currency, adapter, fx_rates)
        if not rate:
            blockers.append(f"游戏收入需先按已批准汇率政策折算为 {adapter.functional_currency} 本位币")
        functional_amount = round(amount * rate, 2) if rate else None
        if amount <= 0 and not blockers:
            continue
        vouchers.append({
            "id": f"GREV-{period.replace('-', '')}-{index:03d}", "date": f"{period}-28",
            "type": "游戏收入确认", "summary": f"确认{period} {row.get('game')} / {row.get('channel')}游戏收入",
            "source": "已批准游戏收入政策及渠道结算", "source_count": 1,
            "original_currency": currency, "original_amount": amount, "fx_rate": rate or None,
            "functional_currency": adapter.functional_currency,
            "functional_amount": functional_amount,
            "ledger_adapter_id": adapter.id,
            "debit": [adapter.line("trade_receivable", functional_amount if not blockers else None, row.get("channel"))],
            "credit": [adapter.line("game_revenue", functional_amount if not blockers else None, row.get("game"))],
            "balanced": not blockers, "status": "阻塞" if blockers else "待复核",
            "review_role": "会计服务机构", "blockers": blockers,
            "judgement": {
                "question": "已批准收入政策、本期履约/消耗数据和总额净额展示是否仍适用？",
                "agent_recommendation": (
                    f"按 {row.get('method')}、{row.get('presentation')}确认；{row.get('recognition_evidence')}"
                    if row.get("policy_id") else "先补业务事实并批准收入政策；在此之前不确认金额。"
                ),
                "impact": "决定本期收入、递延余额、渠道成本和毛利率展示。",
            },
            "evidence": ["渠道结算单", "已批准游戏收入政策", row.get("recognition_evidence")],
            "authority_ids": [source["id"] for source in adapter.sources[:2]],
        })
    return vouchers
