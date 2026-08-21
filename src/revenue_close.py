from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any, Iterable


FORMULA_MODES = {
    "declared": "按渠道结算单声明金额",
    "share_base_x_rate": "分成基数 × 我方分成比例",
    "channel_net_x_rate": "渠道后净额 × 我方分成比例",
}
REVIEW_DECISIONS = {"批准", "退回"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text(value: Any) -> str:
    return str(value or "").strip()


def _number(value: Any) -> float | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        return round(float(value), 4)
    except (TypeError, ValueError):
        return None


def _normalized(value: Any) -> str:
    return "".join(char.lower() for char in _text(value) if char.isalnum())


def _game_codes(record: dict, master_records: Iterable[dict]) -> set[str]:
    game = _normalized(record.get("game"))
    codes = {game} if game else set()
    for item in master_records:
        if item.get("record_type") != "game" or not item.get("active", True):
            continue
        if game in {_normalized(item.get("code")), _normalized(item.get("name"))}:
            codes.update({_normalized(item.get("code")), _normalized(item.get("name"))})
    return {value for value in codes if value}


def _candidate_channel_rules(record: dict, master_records: list[dict]) -> list[dict]:
    channel = _normalized(record.get("channel"))
    platform = _normalized(record.get("platform"))
    currency = _text(record.get("currency")).upper()
    period = _text(record.get("period"))
    game_codes = _game_codes(record, master_records)
    matches = []
    for rule in master_records:
        if rule.get("record_type") != "channel" or not rule.get("active", True):
            continue
        if channel not in {_normalized(rule.get("code")), _normalized(rule.get("name"))}:
            continue
        effective = _text(rule.get("effective_period"))
        if effective and period and effective > period:
            continue
        project = _normalized(rule.get("project_code"))
        if project and game_codes and project not in game_codes:
            continue
        score = 10
        if project and project in game_codes:
            score += 5
        if platform and _normalized(rule.get("platform")) == platform:
            score += 2
        if currency and _text(rule.get("currency")).upper() == currency:
            score += 2
        if effective:
            score += 1
        matches.append((score, rule))
    if not matches:
        return []
    best = max(score for score, _ in matches)
    return [rule for score, rule in matches if score == best]


def _contract_assessment(record: dict, rules: list[dict]) -> dict:
    if not rules:
        return {
            "status": "missing", "rule_id": None, "rule_name": None,
            "reason": "找不到同主体、同游戏和同渠道的已生效渠道规则",
        }
    if len(rules) > 1:
        return {
            "status": "ambiguous", "rule_id": None, "rule_name": None,
            "reason": f"找到 {len(rules)} 条同等匹配的渠道规则，请先停用重复规则或补项目映射",
            "candidate_rule_ids": [item.get("id") for item in rules],
        }
    rule = rules[0]
    formula_mode = _text(rule.get("settlement_formula")) or "declared"
    return {
        "status": "matched", "rule_id": rule.get("id"),
        "rule_name": rule.get("name") or rule.get("code"),
        "contract_reference": rule.get("contract_reference") or "",
        "formula_mode": formula_mode,
        "formula_label": FORMULA_MODES.get(formula_mode, formula_mode),
        "share_rate": _number(rule.get("share_rate")),
        "currency": _text(rule.get("currency")).upper(),
        "payment_days": _number(rule.get("payment_days")),
        "effective_period": _text(rule.get("effective_period")),
        "reason": "按法律主体、游戏、渠道、平台、币种和生效期匹配",
    }


def _reconciliation(record: dict, contract: dict) -> dict:
    declared = _number(record.get("settlement_amount"))
    formula_mode = contract.get("formula_mode") or "declared"
    rate = contract.get("share_rate")
    evidence = record.get("evidence") or {}
    channel_net = _number(evidence.get("channel_net"))
    share_base = _number(record.get("share_base"))
    expected = declared
    basis = "结算单声明金额"
    if formula_mode == "share_base_x_rate":
        expected = round(share_base * rate, 4) if share_base is not None and rate is not None else None
        basis = "分成基数 × 渠道规则分成比例"
    elif formula_mode == "channel_net_x_rate":
        expected = round(channel_net * rate, 4) if channel_net is not None and rate is not None else None
        basis = "渠道后净额 × 渠道规则分成比例"
    delta = round(declared - expected, 4) if declared is not None and expected is not None else None
    tolerance = max(1.0, abs(declared or 0) * 0.001)
    passed = declared is not None and expected is not None and abs(delta or 0) <= tolerance
    return {
        "basis": basis, "declared_amount": declared, "expected_amount": expected,
        "delta": delta, "tolerance": round(tolerance, 4), "passed": passed,
    }


def prepare_settlement_candidates(
    records: Iterable[dict], master_records: Iterable[dict], *,
    existing_candidates: Iterable[dict] = (), existing_settlements: Iterable[dict] = (),
) -> list[dict]:
    master_records = list(master_records)
    prior = {str(item.get("id") or ""): item for item in existing_candidates}
    released = {str(item.get("id") or "") for item in existing_settlements}
    candidates = []
    for source in records:
        record = dict(source)
        candidate_id = str(record.get("id") or "")
        contract = _contract_assessment(record, _candidate_channel_rules(record, master_records))
        reconciliation = _reconciliation(record, contract)
        blockers = list(record.get("anomalies") or [])
        if contract["status"] == "missing":
            blockers.append("缺少已生效渠道规则：请在首次上线中补渠道、游戏、币种、公式和证据引用")
        elif contract["status"] == "ambiguous":
            blockers.append(contract["reason"])
        else:
            if not contract.get("contract_reference"):
                blockers.append("渠道规则缺少合同/平台政策证据引用")
            if contract.get("formula_mode") not in FORMULA_MODES:
                blockers.append("渠道规则的结算公式无效")
            rule_currency = contract.get("currency")
            if rule_currency and rule_currency != _text(record.get("currency")).upper():
                blockers.append(f"账单币种与渠道规则不一致：账单 {_text(record.get('currency')).upper()} / 规则 {rule_currency}")
            statement_rate = _number(record.get("share_rate"))
            rule_rate = contract.get("share_rate")
            if statement_rate is not None and rule_rate is not None and abs(statement_rate - rule_rate) > 0.0001:
                blockers.append(f"账单分成比例 {statement_rate:.2%} 与渠道规则 {rule_rate:.2%} 不一致")
            if not reconciliation["passed"]:
                if reconciliation["expected_amount"] is None:
                    blockers.append(f"缺少计算“{reconciliation['basis']}”所需金额或比例")
                else:
                    blockers.append(f"结算公式差异 {reconciliation['delta']:,.2f}，超过容差 {reconciliation['tolerance']:,.2f}")
        previous = prior.get(candidate_id) or {}
        already_released = candidate_id in released or previous.get("release_status") == "released"
        candidate = {
            **record,
            "status": "已释放" if already_released else "阻塞" if blockers else "待业务复核",
            "contract_match": contract,
            "commercial_reconciliation": reconciliation,
            "release_status": "released" if already_released else "blocked" if blockers else "ready_for_review",
            "release_blockers": list(dict.fromkeys(blockers)),
            "review_history": list(previous.get("review_history") or []),
            "released_at": previous.get("released_at"),
            "released_by": previous.get("released_by"),
            "agent_judgement": {
                "recommendation": (
                    "商业口径已勾稽，建议业务负责人复核后释放为应收。"
                    if not blockers else "暂不形成应收；按阻塞清单补渠道规则或解释差异。"
                ),
                "boundary": "这里判断商业应收，不决定总额/净额或收入确认时点。",
            },
        }
        candidates.append(candidate)
    return candidates


def review_settlement_candidates(
    candidates: Iterable[dict], candidate_ids: Iterable[str], decision: str,
    actor: str, rationale: str,
) -> tuple[list[dict], list[dict]]:
    decision, actor, rationale = _text(decision), _text(actor), _text(rationale)
    if decision not in REVIEW_DECISIONS:
        raise ValueError("复核决定只能是批准或退回")
    if not actor or len(rationale) < 4:
        raise ValueError("请填写复核人和至少4个字的判断依据")
    ids = {_text(value) for value in candidate_ids if _text(value)}
    if not ids:
        raise ValueError("请选择至少一条收入结算候选")
    rows = [dict(item) for item in candidates]
    found = {str(item.get("id") or "") for item in rows if str(item.get("id") or "") in ids}
    missing = sorted(ids - found)
    if missing:
        raise ValueError(f"找不到收入结算候选：{'、'.join(missing[:5])}")
    settlements = []
    reviewed_at = _now()
    for item in rows:
        if str(item.get("id") or "") not in ids:
            continue
        if item.get("release_status") == "released":
            raise ValueError(f"候选 {item.get('id')} 已释放，不能重复批准")
        if decision == "批准" and item.get("release_blockers"):
            raise ValueError(f"候选 {item.get('id')} 仍有阻塞：{'；'.join(item['release_blockers'][:3])}")
        review = {
            "decision": decision, "actor": actor[:80], "rationale": rationale[:1000],
            "reviewed_at": reviewed_at,
        }
        item.setdefault("review_history", []).append(review)
        if decision == "退回":
            item["status"] = "已退回"
            item["release_status"] = "rejected"
            continue
        item["status"] = "已释放"
        item["release_status"] = "released"
        item["released_at"] = reviewed_at
        item["released_by"] = actor[:80]
        settlement = {
            key: value for key, value in item.items()
            if key not in {"release_blockers", "review_history", "agent_judgement"}
        }
        settlement["status"] = "已核对"
        settlement["release_status"] = "released"
        settlement["commercial_review"] = review
        settlement["commercial_control_fingerprint"] = hashlib.sha256(
            repr((item.get("entity_id"), item.get("id"), item.get("contract_match"), item.get("commercial_reconciliation"))).encode()
        ).hexdigest()
        settlements.append(settlement)
    return rows, settlements


def revenue_close_payload(candidates: Iterable[dict], settlements: Iterable[dict]) -> dict:
    candidates, settlements = list(candidates), list(settlements)
    by_currency: dict[str, dict[str, float | int]] = {}
    for row in settlements:
        currency = _text(row.get("currency")).upper() or "未知"
        bucket = by_currency.setdefault(currency, {"released_amount": 0.0, "released_receivable": 0.0, "count": 0})
        bucket["released_amount"] += float(row.get("settlement_amount") or 0)
        bucket["released_receivable"] += float(row.get("net_receivable") or 0)
        bucket["count"] += 1
    return {
        "candidates": candidates,
        "summary": {
            "candidate_count": len(candidates),
            "ready_for_review": sum(item.get("release_status") == "ready_for_review" for item in candidates),
            "blocked": sum(item.get("release_status") == "blocked" for item in candidates),
            "rejected": sum(item.get("release_status") == "rejected" for item in candidates),
            "released": sum(item.get("release_status") == "released" for item in candidates),
            "released_by_currency": [
                {"currency": currency, **{key: round(value, 2) if isinstance(value, float) else value for key, value in values.items()}}
                for currency, values in sorted(by_currency.items())
            ],
        },
        "guardrail": "只有主体级渠道规则、商业公式勾稽和人工复核全部通过，才释放为应收；会计收入政策另行复核。",
    }
