from __future__ import annotations

import hashlib
import re
from calendar import monthrange
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable


ALLOCATION_TARGETS = {"receivable": "收入结算应收", "payable": "采购应付", "expense": "费用报销", "payroll": "薪酬付款"}
PAYMENT_DECISIONS = {"批准", "退回", "取消"}
COLLECTION_ACTION_TYPES = {"跟进记录", "回款承诺", "争议登记", "争议解除"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _number(value: Any) -> float:
    try:
        return round(float(value or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def _allocation_id(transaction_id: str, target_type: str, target_id: str, amount: float) -> str:
    seed = f"{transaction_id}|{target_type}|{target_id}|{amount}|{_now()}"
    return f"ALLOC-{hashlib.sha1(seed.encode()).hexdigest()[:14].upper()}"


def _request_id(target_type: str, target_id: str, amount: float) -> str:
    seed = f"{target_type}|{target_id}|{amount}|{_now()}"
    return f"PAY-{hashlib.sha1(seed.encode()).hexdigest()[:14].upper()}"


def _claim_id(claimant: str, claim_date: str, amount: float) -> str:
    seed = f"{claimant}|{claim_date}|{amount}|{_now()}"
    return f"EXP-{hashlib.sha1(seed.encode()).hexdigest()[:14].upper()}"


def _collection_id(settlement_id: str, action_type: str, owner: str) -> str:
    seed = f"{settlement_id}|{action_type}|{owner}|{_now()}"
    return f"COL-{hashlib.sha1(seed.encode()).hexdigest()[:14].upper()}"


def _confirmed_allocations(allocations: Iterable[dict]) -> list[dict]:
    return [item for item in allocations if item.get("status") not in {"已撤销", "已退回"}]


def _same_entity(left: dict, right: dict) -> bool:
    """Empty legacy ownership only matches another empty owner; never cross legal entities."""
    return str(left.get("entity_id") or "") == str(right.get("entity_id") or "")


def create_cash_allocation(
    transaction: dict,
    target_type: str,
    target: dict,
    amount: float,
    existing_allocations: Iterable[dict],
    actor: str,
    *,
    note: str = "",
    difference_reason: str = "",
    authorization_reference: str = "",
    authorization: dict | None = None,
) -> dict:
    """Create one auditable allocation without modifying the source transaction or document."""
    if target_type not in ALLOCATION_TARGETS:
        raise ValueError("核销目标类型无效")
    actor = str(actor or "").strip()
    if not actor:
        raise ValueError("请填写核销确认人")
    tx_id = str(transaction.get("id") or "").strip()
    target_id = str(target.get("id") or "").strip()
    if not tx_id or not target_id:
        raise ValueError("银行流水和核销目标必须有稳定编号")
    transaction_entity = str(transaction.get("entity_id") or "").strip()
    target_entity = str(target.get("entity_id") or "").strip()
    if transaction_entity and target_entity and transaction_entity != target_entity:
        raise ValueError("银行流水与核销目标不属于同一法律主体，禁止跨主体核销")
    amount = _number(amount)
    if amount <= 0:
        raise ValueError("核销金额必须大于0")
    required_direction = "收入" if target_type == "receivable" else "支出"
    if transaction.get("direction") != required_direction:
        raise ValueError(f"{ALLOCATION_TARGETS[target_type]}只能使用{required_direction}流水核销")
    tx_currency = str(transaction.get("currency") or "CNY").upper()
    target_currency = str(target.get("currency") or "CNY").upper()
    if tx_currency != target_currency:
        raise ValueError("银行流水与核销目标币种不同，必须先记录换汇或折算事实")
    active = _confirmed_allocations(existing_allocations)
    allocated_on_transaction = sum(
        _number(item.get("amount")) for item in active
        if item.get("transaction_id") == tx_id and _same_entity(item, transaction)
    )
    if allocated_on_transaction + amount > _number(transaction.get("amount")) + 0.01:
        raise ValueError("本次核销会超过该银行流水金额，可能造成重复核销")

    expected_field = {
        "receivable": "net_receivable", "payable": "payable_amount",
        "expense": "approved_amount", "payroll": "approved_amount",
    }[target_type]
    expected = _number(target.get(expected_field))
    allocated_on_target = sum(
        _number(item.get("amount")) for item in active
        if item.get("target_type") == target_type and item.get("target_id") == target_id
        and _same_entity(item, target)
    )
    if expected and allocated_on_target + amount > expected + 0.01:
        raise ValueError("本次核销会超过目标应收/应付金额，请先确认退款、预收预付或差异处理")
    residual_after = round(expected - allocated_on_target - amount, 2) if expected else None
    if residual_after not in {None, 0.0} and abs(residual_after) <= 10 and not str(difference_reason or "").strip():
        # 小额尾差可以继续作为部分核销，但必须显式提醒，不能静默结清。
        settlement_status = "部分核销，有小额尾差待处理"
    else:
        settlement_status = "已核销" if residual_after == 0 else "部分核销"
    authorization_reference = str(authorization_reference or "").strip()[:120]
    authorization_gap = required_direction == "支出" and not authorization_reference
    if required_direction == "支出" and authorization_reference:
        if not authorization or str(authorization.get("id") or "") != authorization_reference:
            raise ValueError("付款授权编号不存在；必须引用系统内已批准的付款申请")
        if authorization.get("status") != "已批准":
            raise ValueError("付款申请尚未批准，不能作为银行支出核销授权")
        for field, label in (("entity_id", "法律主体"), ("target_type", "付款目标类型"), ("target_id", "付款目标")):
            expected_value = transaction_entity or target_entity if field == "entity_id" else (
                target_type if field == "target_type" else target_id
            )
            if str(authorization.get(field) or "") != str(expected_value or ""):
                raise ValueError(f"付款授权的{label}与本次银行核销不一致")
        if str(authorization.get("currency") or "CNY").upper() != tx_currency:
            raise ValueError("付款授权币种与银行流水币种不一致")
        used_authorization = sum(
            _number(item.get("amount")) for item in active
            if item.get("authorization_reference") == authorization_reference
            and _same_entity(item, authorization)
        )
        if used_authorization + amount > _number(authorization.get("amount")) + 0.01:
            raise ValueError("本次核销会超过该付款授权的批准金额")
        authorization_gap = False
    return {
        "id": _allocation_id(tx_id, target_type, target_id, amount),
        "transaction_id": tx_id,
        "target_type": target_type,
        "target_id": target_id,
        "amount": amount,
        "currency": tx_currency,
        "entity_id": transaction_entity or target_entity,
        "status": f"{settlement_status}，待补付款授权" if authorization_gap else settlement_status,
        "authorization_reference": authorization_reference,
        "authorization_gap": authorization_gap,
        "difference_reason": str(difference_reason or "").strip()[:200],
        "note": str(note or "").strip()[:1000],
        "actor": actor[:80],
        "timestamp": _now(),
        "evidence": {
            "bank_source_file": transaction.get("source_file"),
            "transaction_id": transaction.get("transaction_id"),
            "target_source_file": target.get("source_file"),
        },
    }


def _channel_payment_days(settlement: dict, master_records: Iterable[dict]) -> tuple[int | None, str]:
    direct = _number(settlement.get("payment_days"))
    if direct > 0:
        return int(direct), "结算记录账期"
    channel = str(settlement.get("channel") or "").strip().casefold()
    platform = str(settlement.get("platform") or "").strip().casefold()
    for record in master_records:
        if record.get("record_type") != "channel" or record.get("active") is False:
            continue
        candidates = {
            str(record.get(field) or "").strip().casefold()
            for field in ("code", "name", "platform")
        }
        if (channel and channel in candidates) or (not channel and platform and platform in candidates):
            days = _number(record.get("payment_days"))
            if days > 0:
                return int(days), f"渠道主数据：{record.get('name') or record.get('code') or settlement.get('channel')}"
    return None, "默认账期（次次月1日）"


def _collection_state(settlement: dict, actions: Iterable[dict], as_of_date: date) -> dict:
    related = [
        action for action in actions
        if action.get("settlement_id") == settlement.get("id") and _same_entity(action, settlement)
    ]
    related.sort(key=lambda item: str(item.get("recorded_at") or item.get("timestamp") or ""))
    latest_promise = next((item for item in reversed(related) if item.get("action_type") == "回款承诺"), None)
    latest_dispute_event = next(
        (item for item in reversed(related) if item.get("action_type") in {"争议登记", "争议解除"}), None,
    )
    latest = related[-1] if related else None
    promise_is_current = bool(
        latest_promise and (
            not latest_dispute_event
            or str(latest_promise.get("recorded_at") or "") > str(latest_dispute_event.get("recorded_at") or "")
        )
    )
    promise_date = None
    if promise_is_current and latest_promise.get("promised_date"):
        try:
            promise_date = date.fromisoformat(str(latest_promise["promised_date"]))
        except ValueError:
            promise_date = None
    disputed = bool(latest_dispute_event and latest_dispute_event.get("action_type") == "争议登记")
    return {
        "collection_action_count": len(related),
        "collection_owner": str((latest or {}).get("owner") or ""),
        "latest_contact_at": (latest or {}).get("action_date"),
        "promised_date": promise_date.isoformat() if promise_date else None,
        "promised_amount": _number((latest_promise or {}).get("promised_amount")) if promise_is_current else 0.0,
        "promise_action_id": (latest_promise or {}).get("id") if promise_is_current else None,
        "last_promised_date": (latest_promise or {}).get("promised_date"),
        "last_promised_amount": _number((latest_promise or {}).get("promised_amount")),
        "promise_suspended_by_dispute": bool(latest_promise and not promise_is_current),
        "promise_missed": bool(promise_date and promise_date < as_of_date),
        "disputed": disputed,
        "dispute_reason": str((latest_dispute_event or {}).get("dispute_reason") or "") if disputed else "",
        "latest_collection_note": str((latest or {}).get("note") or ""),
    }


def create_collection_action(
    receivable: dict, action_type: str, owner: str, actor: str, *, action_date: str,
    note: str, promised_date: str = "", promised_amount: float = 0,
    dispute_reason: str = "", existing_actions: Iterable[dict] = (),
) -> dict:
    action_type = str(action_type or "").strip()
    if action_type not in COLLECTION_ACTION_TYPES:
        raise ValueError("催收动作类型无效")
    owner = str(owner or "").strip()
    if not owner:
        raise ValueError("请填写催收责任人")
    actor = str(actor or "").strip()
    if not actor:
        raise ValueError("请填写记录人")
    note = str(note or "").strip()
    if len(note) < 4:
        raise ValueError("请记录本次联系事实或处理依据（至少4个字）")
    try:
        action_day = date.fromisoformat(str(action_date or ""))
    except ValueError as exc:
        raise ValueError("跟进日期必须为 YYYY-MM-DD") from exc
    outstanding = _number(receivable.get("outstanding"))
    if outstanding <= 0:
        raise ValueError("该应收已无待回款余额，不需要新增催收记录")
    promised_day = None
    amount = 0.0
    if action_type == "回款承诺":
        try:
            promised_day = date.fromisoformat(str(promised_date or ""))
        except ValueError as exc:
            raise ValueError("回款承诺日必须为 YYYY-MM-DD") from exc
        if promised_day < action_day:
            raise ValueError("回款承诺日不能早于本次跟进日")
        amount = _number(promised_amount)
        if amount <= 0:
            raise ValueError("承诺回款金额必须大于0")
        if amount > outstanding + 0.01:
            raise ValueError("承诺回款金额不能超过当前未回款余额")
    dispute_reason = str(dispute_reason or "").strip()
    if action_type == "争议登记" and len(dispute_reason) < 4:
        raise ValueError("请填写明确的争议原因（至少4个字）")
    entity_id = str(receivable.get("entity_id") or "")
    ordered_actions = sorted(existing_actions, key=lambda item: str(item.get("recorded_at") or ""))
    previous_promise = next((
        item for item in reversed(ordered_actions)
        if item.get("settlement_id") == receivable.get("id")
        and item.get("action_type") == "回款承诺" and _same_entity(item, receivable)
    ), None)
    return {
        "id": _collection_id(str(receivable.get("id") or ""), action_type, owner),
        "entity_id": entity_id,
        "settlement_id": receivable.get("id"),
        "game": receivable.get("game"),
        "channel": receivable.get("channel"),
        "currency": str(receivable.get("currency") or "CNY").upper(),
        "outstanding_snapshot": outstanding,
        "action_type": action_type,
        "action_date": action_day.isoformat(),
        "owner": owner[:80],
        "note": note[:1000],
        "promised_date": promised_day.isoformat() if promised_day else None,
        "promised_amount": amount,
        "dispute_reason": dispute_reason[:500],
        "supersedes_action_id": previous_promise.get("id") if previous_promise and action_type == "回款承诺" else None,
        "recorded_by": actor[:80],
        "recorded_at": _now(),
        "period": action_day.strftime("%Y-%m"),
    }


def build_receivables_register(
    settlements: Iterable[dict], allocations: Iterable[dict], as_of: str | None = None,
    master_records: Iterable[dict] = (), collection_actions: Iterable[dict] = (),
) -> dict:
    rows = []
    allocations = _confirmed_allocations(allocations)
    as_of_date = date.fromisoformat(as_of) if as_of else date.today()
    for settlement in settlements:
        if settlement.get("release_status") not in {None, "", "released"}:
            continue
        expected = _number(settlement.get("net_receivable"))
        allocated = sum(
            _number(item.get("amount")) for item in allocations
            if item.get("target_type") == "receivable" and item.get("target_id") == settlement.get("id")
            and _same_entity(item, settlement)
        )
        outstanding = round(expected - allocated, 2)
        period = str(settlement.get("period") or "")
        due_date = None
        days_overdue = None
        if re.fullmatch(r"\d{4}-\d{2}", period):
            year, month = map(int, period.split("-"))
            payment_days, due_date_basis = _channel_payment_days(settlement, master_records)
            if payment_days is not None:
                due_date = date(year, month, monthrange(year, month)[1]) + timedelta(days=payment_days)
            else:
                due_month = month + 2
                due_year = year + (due_month - 1) // 12
                due_month = (due_month - 1) % 12 + 1
                due_date = date(due_year, due_month, 1)
            days_overdue = max(0, (as_of_date - due_date).days) if outstanding > 0 else 0
        else:
            due_date_basis = "缺少有效结算期间"
        anomalies = list(settlement.get("anomalies") or [])
        if outstanding < -0.01:
            anomalies.append("累计回款核销超过应收金额")
        collection = _collection_state(settlement, collection_actions, as_of_date)
        collection["promise_missed"] = bool(collection["promise_missed"] and outstanding > 0)
        days_until_due = (due_date - as_of_date).days if due_date and outstanding > 0 else None
        effective_promised_amount = (
            min(_number(collection.get("promised_amount")), outstanding)
            if collection.get("promised_date") and not collection.get("promise_missed")
            and not collection.get("disputed") and outstanding > 0 else 0.0
        )
        if outstanding <= 0.01:
            priority_code, priority_label, priority_score = "CLOSED", "已结清", 0
            priority_reason, next_action = "应收已核销结清", "无需催收，保留核销依据"
        elif collection.get("disputed"):
            priority_code, priority_label, priority_score = "P0", "立即升级", 100
            priority_reason, next_action = "回款争议未解决", "升级到渠道负责人，补齐合同、对账与扣款证据"
        elif collection.get("promise_missed"):
            priority_code, priority_label, priority_score = "P0", "立即升级", 95
            priority_reason, next_action = "渠道已超过承诺回款日", "当日联系渠道，确认未付原因并记录新承诺或争议"
        elif (days_overdue or 0) >= 30:
            priority_code, priority_label, priority_score = "P1", "高优先级", 85
            priority_reason, next_action = f"应收已逾期 {days_overdue} 天", "本周升级催收，确认付款批次、阻塞点和责任人"
        elif (days_overdue or 0) > 0:
            priority_code, priority_label, priority_score = "P1", "高优先级", 75
            priority_reason, next_action = f"应收已逾期 {days_overdue} 天", "在本周内完成首次催收并记录回款承诺"
        elif days_until_due is not None and days_until_due <= 7:
            priority_code, priority_label, priority_score = "P2", "到期提醒", 55
            priority_reason, next_action = f"距到期日 {max(0, days_until_due)} 天", "到期前确认渠道已安排付款和正确收款账户"
        elif days_until_due is not None and days_until_due <= 14:
            priority_code, priority_label, priority_score = "P2", "到期提醒", 40
            priority_reason, next_action = f"距到期日 {days_until_due} 天", "检查对账差异、付款资料和渠道联系人"
        else:
            priority_code, priority_label, priority_score = "P3", "常规跟进", 20
            priority_reason, next_action = "应收尚未到催收节点", "保持对账证据完整，按渠道账期跟进"
        collection_status = (
            "已回款" if abs(outstanding) <= 0.01 else "争议处理中" if collection["disputed"]
            else "承诺逾期" if collection["promise_missed"] else "已承诺回款" if collection["promised_date"]
            else "跟进中" if collection["collection_action_count"] else "待跟进" if (days_overdue or 0) > 0 else "未到期"
        )
        row = {
            "id": settlement.get("id"), "period": period,
            "entity_id": settlement.get("entity_id") or "",
            "game": settlement.get("game"), "channel": settlement.get("channel"),
            "currency": settlement.get("currency") or "CNY", "expected_receivable": expected,
            "allocated_receipts": round(allocated, 2), "outstanding": outstanding,
            "due_date": due_date.isoformat() if due_date else None, "days_overdue": days_overdue,
            "days_until_due": days_until_due, "due_date_basis": due_date_basis,
            "collection_status": collection_status, **collection,
            "effective_promised_amount": round(effective_promised_amount, 2),
            "promise_scenario_outstanding": round(max(0.0, outstanding - effective_promised_amount), 2),
            "collection_priority": priority_code, "collection_priority_label": priority_label,
            "collection_priority_score": priority_score, "collection_priority_reason": priority_reason,
            "recommended_collection_action": next_action,
            "priority_scope": "entity_currency_only",
            "status": (
                "异常" if anomalies else "已回款" if abs(outstanding) <= 0.01
                else "部分回款" if allocated > 0 else "待回款"
            ),
            "anomalies": anomalies,
        }
        rows.append(row)
    priority_queues = []
    queue_groups: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        if row["outstanding"] > 0:
            queue_groups.setdefault((str(row.get("entity_id") or ""), row["currency"]), []).append(row)
    for (entity_id, currency), items in sorted(queue_groups.items()):
        items.sort(key=lambda item: (
            -int(item.get("collection_priority_score") or 0),
            -(item.get("days_overdue") or 0), -float(item.get("outstanding") or 0),
            str(item.get("due_date") or "9999-12-31"), str(item.get("id") or ""),
        ))
        for rank, item in enumerate(items, 1):
            item["collection_priority_rank"] = rank
        priority_queues.append({
            "entity_id": entity_id, "currency": currency, "count": len(items),
            "items": items,
        })
    by_currency = {}
    for row in rows:
        bucket = by_currency.setdefault(row["currency"], {"receivable": 0.0, "received": 0.0, "outstanding": 0.0})
        bucket["receivable"] += row["expected_receivable"]
        bucket["received"] += row["allocated_receipts"]
        bucket["outstanding"] += row["outstanding"]
    promise_by_currency: dict[str, float] = {}
    for row in rows:
        if row.get("effective_promised_amount", 0) > 0:
            promise_by_currency[row["currency"]] = promise_by_currency.get(row["currency"], 0) + row["effective_promised_amount"]
    return {
        "rows": rows,
        "summary_by_currency": [
            {"currency": currency, **{key: round(value, 2) for key, value in values.items()}}
            for currency, values in sorted(by_currency.items())
        ],
        "overdue_count": sum((row.get("days_overdue") or 0) > 0 and row["outstanding"] > 0 for row in rows),
        "promised_by_currency": [
            {"currency": currency, "amount": round(amount, 2)}
            for currency, amount in sorted(promise_by_currency.items())
        ],
        "missed_promise_count": sum(bool(row.get("promise_missed")) for row in rows),
        "disputed_count": sum(bool(row.get("disputed")) for row in rows),
        "due_soon_count": sum(row.get("collection_priority") == "P2" for row in rows),
        "priority_counts": {
            code: sum(row.get("collection_priority") == code for row in rows)
            for code in ("P0", "P1", "P2", "P3")
        },
        "priority_queues": priority_queues,
        "exception_count": sum(bool(row["anomalies"]) for row in rows),
        "guardrail": "不同币种独立汇总；回款核销不改写平台原始结算额。",
    }


def _verified_invoice_amount(purchase_id: str, invoices: Iterable[dict]) -> float:
    return round(sum(
        _number(invoice.get("total_amount")) for invoice in invoices
        if (invoice.get("purchase_match") or {}).get("purchase_id") == purchase_id
        and invoice.get("verification_status") in {"已查验", "查验通过", "有效"}
        and not invoice.get("anomalies")
    ), 2)


def build_payables_register(
    purchases: Iterable[dict], invoices: Iterable[dict], allocations: Iterable[dict],
) -> dict:
    invoices = list(invoices)
    allocations = _confirmed_allocations(allocations)
    rows = []
    for purchase in purchases:
        accepted = _number(purchase.get("accepted_amount"))
        invoiced = _number(purchase.get("invoice_amount"))
        verified = _verified_invoice_amount(str(purchase.get("id")), invoices)
        liability = min(accepted, invoiced) if invoiced else accepted
        declared_paid = _number(purchase.get("paid_amount"))
        allocated_paid = sum(
            _number(item.get("amount")) for item in allocations
            if item.get("target_type") == "payable" and item.get("target_id") == purchase.get("id")
            and _same_entity(item, purchase)
        )
        unauthorized_paid = sum(
            _number(item.get("amount")) for item in allocations
            if item.get("target_type") == "payable" and item.get("target_id") == purchase.get("id")
            and item.get("authorization_gap") and _same_entity(item, purchase)
        )
        reconciled_paid = max(declared_paid, allocated_paid)
        outstanding = round(liability - reconciled_paid, 2)
        payment_eligible = round(min(accepted, verified), 2)
        payment_available = round(max(0.0, payment_eligible - reconciled_paid), 2)
        anomalies = list(purchase.get("anomalies") or [])
        if declared_paid and allocated_paid and abs(declared_paid - allocated_paid) > 0.01:
            anomalies.append(f"采购台账已付 {declared_paid:,.2f} 与银行核销 {allocated_paid:,.2f} 不一致")
        if reconciled_paid > accepted + 0.01:
            anomalies.append("累计付款超过验收金额")
        if reconciled_paid > invoiced + 0.01 and invoiced:
            anomalies.append("累计付款超过开票金额")
        if reconciled_paid > payment_eligible + 0.01:
            anomalies.append("累计付款超过已验收且已查验发票共同支持的额度")
        if unauthorized_paid:
            anomalies.append(f"银行已支付 {unauthorized_paid:,.2f}，但缺少已批准付款申请或历史授权证据")
        payable = {
            **purchase,
            "entity_id": purchase.get("entity_id") or "",
            "payable_amount": liability,
            "verified_invoice_amount": verified,
            "payment_eligible_amount": payment_eligible,
            "payment_available_amount": payment_available,
            "declared_paid_amount": declared_paid,
            "allocated_paid_amount": round(allocated_paid, 2),
            "unauthorized_paid_amount": round(unauthorized_paid, 2),
            "reconciled_paid_amount": round(reconciled_paid, 2),
            "outstanding": outstanding,
            "status": (
                "异常" if anomalies else "已付清" if liability and abs(outstanding) <= 0.01
                else "部分付款" if reconciled_paid > 0 else "待付款" if payment_available > 0
                else "待发票/查验" if liability else "待验收"
            ),
            "anomalies": anomalies,
        }
        rows.append(payable)
    return {
        "rows": rows,
        "pending_payment_count": sum(row["payment_available_amount"] > 0 for row in rows),
        "exception_count": sum(bool(row["anomalies"]) for row in rows),
        "guardrail": "付款以验收、发票及授权为上限；采购台账已付金额与银行核销并行保存并勾稽。",
    }


def build_payroll_payables(payroll_rows: Iterable[dict], allocations: Iterable[dict]) -> dict:
    allocations = _confirmed_allocations(allocations)
    grouped: dict[tuple[str, str, str], dict] = {}
    for row in payroll_rows:
        entity_id = str(row.get("entity_id") or "")
        period = str(row.get("period") or "")
        currency = str(row.get("currency") or "CNY").upper()
        if not period:
            continue
        batch_id = f"PAYROLL-{entity_id}-{period}-{currency}" if entity_id else f"PAYROLL-{period}-{currency}"
        bucket = grouped.setdefault((entity_id, period, currency), {
            "id": batch_id, "entity_id": entity_id, "period": period, "currency": currency,
            "approved_amount": 0.0, "employee_count": 0, "anomalies": [],
        })
        bucket["approved_amount"] += _number(row.get("net_salary"))
        bucket["employee_count"] += 1
        bucket["anomalies"].extend(row.get("anomalies") or [])
    rows = []
    for bucket in grouped.values():
        bucket["approved_amount"] = round(bucket["approved_amount"], 2)
        paid = sum(
            _number(item.get("amount")) for item in allocations
            if item.get("target_type") == "payroll" and item.get("target_id") == bucket["id"]
            and _same_entity(item, bucket)
        )
        bucket["allocated_paid_amount"] = round(paid, 2)
        bucket["outstanding"] = round(bucket["approved_amount"] - paid, 2)
        if paid > bucket["approved_amount"] + 0.01:
            bucket["anomalies"].append("工资付款核销超过应发净额")
        bucket["status"] = (
            "异常" if bucket["anomalies"] else "已支付" if abs(bucket["outstanding"]) <= 0.01
            else "部分支付" if paid else "待支付"
        )
        rows.append(bucket)
    rows.sort(key=lambda item: (item["period"], item["currency"]), reverse=True)
    return {
        "rows": rows,
        "pending_count": sum(item["outstanding"] > 0 for item in rows),
        "exception_count": sum(bool(item["anomalies"]) for item in rows),
        "guardrail": "工资付款按期间和币种形成批次；个人明细不进入付款审批摘要。",
    }


def build_flow_overview(datasets: dict[str, list[dict]], as_of: str | None = None) -> dict:
    allocations = datasets.get("cash_allocations") or []
    active_allocations = _confirmed_allocations(allocations)
    receivables = build_receivables_register(
        datasets.get("settlements") or [], allocations, as_of,
        datasets.get("master_records") or [], datasets.get("collection_actions") or [],
    )
    payables = build_payables_register(
        datasets.get("purchases") or [], datasets.get("invoices") or [], allocations,
    )
    payroll = build_payroll_payables(datasets.get("payroll_rows") or [], allocations)
    bank_rows = []
    receivable_targets = {
        (str(item.get("entity_id") or ""), str(item.get("id") or "")): item
        for item in receivables["rows"]
    }
    payable_targets = {
        (str(item.get("entity_id") or ""), str(item.get("id") or "")): item
        for item in payables["rows"]
    }
    for source in datasets.get("bank_transactions") or []:
        item = dict(source)
        allocated = sum(
            _number(allocation.get("amount")) for allocation in active_allocations
            if allocation.get("transaction_id") == item.get("id") and _same_entity(allocation, item)
        )
        amount = _number(item.get("amount"))
        remaining = round(max(0.0, amount - allocated), 2)
        item.update({
            "allocated_amount": round(allocated, 2), "remaining_amount": remaining,
            "allocation_status": "金额待补" if amount <= 0 else "已核销" if remaining <= 0.01 else "部分核销" if allocated > 0 else "未核销",
        })
        if amount > 0 and remaining <= 0.01:
            item["status"] = "已核销"
        elif allocated > 0:
            item["status"] = "部分核销"
        suggestion = dict(item.get("suggested_match") or {})
        targets = receivable_targets if item.get("direction") == "收入" else payable_targets
        target = targets.get((str(item.get("entity_id") or ""), str(suggestion.get("target_id") or "")))
        target_outstanding = _number((target or {}).get("outstanding"))
        if suggestion and target and target_outstanding > 0 and remaining > 0:
            suggestion.update({
                "entity_id": item.get("entity_id") or "",
                "expected_amount": target_outstanding,
                "suggested_allocation_amount": round(min(remaining, target_outstanding), 2),
            })
            item["suggested_match"] = suggestion
        elif suggestion and (not target or target_outstanding <= 0):
            item["suggested_match"] = None
        bank_rows.append(item)
    bank_pending = [
        item for item in bank_rows
        if _number(item.get("amount")) <= 0 or item.get("remaining_amount", 0) > 0.01
    ]
    payment_requests = datasets.get("payment_requests") or []
    claims = datasets.get("expense_claims") or []
    alerts = []
    if receivables["overdue_count"]:
        alerts.append({"severity": "高", "type": "逾期应收", "count": receivables["overdue_count"], "action": "核对渠道账期并发起催收"})
    if receivables["missed_promise_count"]:
        alerts.append({"severity": "高", "type": "承诺回款逾期", "count": receivables["missed_promise_count"], "action": "升级催收并更新短期资金情景"})
    if receivables["disputed_count"]:
        alerts.append({"severity": "中", "type": "回款争议", "count": receivables["disputed_count"], "action": "补充对账证据并明确解决责任人"})
    if bank_pending:
        alerts.append({"severity": "高", "type": "未认领流水", "count": len(bank_pending), "action": "先认领收付款用途，避免重复记账"})
    exceptions = receivables["exception_count"] + payables["exception_count"] + payroll["exception_count"]
    if exceptions:
        alerts.append({"severity": "高", "type": "金额或状态异常", "count": exceptions, "action": "停止自动结清并逐笔复核"})
    pending_approvals = sum(item.get("status") == "待批准" for item in payment_requests) + sum(
        item.get("status") == "待审批" for item in claims
    )
    if pending_approvals:
        alerts.append({"severity": "中", "type": "等待审批", "count": pending_approvals, "action": "由业务负责人处理 Agent 给出的证据摘要"})
    return {
        "receivables": receivables,
        "payables": payables,
        "payroll_payables": payroll,
        "expense_claims": {
            "count": len(claims),
            "pending_approval": sum(item.get("status") == "待审批" for item in claims),
            "approved_unpaid": sum(item.get("status") == "已批准待付款" for item in claims),
        },
        "payment_requests": {
            "count": len(payment_requests),
            "pending_approval": sum(item.get("status") == "待批准" for item in payment_requests),
            "approved": sum(item.get("status") == "已批准" for item in payment_requests),
            "blocked": sum(item.get("status") == "阻塞" for item in payment_requests),
        },
        "payment_request_records": payment_requests,
        "expense_claim_records": claims,
        "cash_allocation_records": allocations,
        "collection_action_records": datasets.get("collection_actions") or [],
        "bank_transactions": bank_rows,
        "bank_unallocated_count": len(bank_pending),
        "alerts": alerts,
        "control_principle": "原始业务单据、应收应付、付款申请、银行流水和核销关系分层保存；任何一层都不覆盖另一层。",
    }


def create_payment_request(
    target_type: str,
    target: dict,
    amount: float,
    actor: str,
    *,
    purpose: str = "",
    evidence: Iterable[str] = (),
    prepayment: bool = False,
    existing_requests: Iterable[dict] = (),
    vendor_bank_accounts: Iterable[dict] = (),
    bank_account_id: str = "",
    require_approved_vendor_account: bool = False,
) -> dict:
    if target_type not in {"payable", "expense", "payroll"}:
        raise ValueError("付款申请目标类型无效")
    actor = str(actor or "").strip()
    if not actor:
        raise ValueError("请填写申请人")
    amount = _number(amount)
    if amount <= 0:
        raise ValueError("付款申请金额必须大于0")
    currency = str(target.get("currency") or "CNY").upper()
    entity_id = str(target.get("entity_id") or "")
    target_id = str(target.get("id") or "")
    blockers = []
    bank_binding = None
    evidence_items = [str(item).strip()[:500] for item in evidence if str(item).strip()]
    recommendation = "证据齐全后由有权审批人确认；系统不直接操作银行付款。"
    if target_type == "payable":
        accepted = _number(target.get("accepted_amount"))
        verified = _number(target.get("verified_invoice_amount"))
        outstanding = _number(target.get("outstanding"))
        payment_available = _number(target.get("payment_available_amount")) if "payment_available_amount" in target else min(accepted, verified, outstanding)
        if amount > outstanding + 0.01:
            blockers.append("申请金额超过当前未付余额")
        if not prepayment and amount > accepted + 0.01:
            blockers.append("申请金额超过已验收金额")
        if not prepayment and verified < amount:
            blockers.append("已查验且匹配的发票金额不足")
        if not prepayment and amount > payment_available + 0.01:
            blockers.append("申请金额超过验收与已查验发票共同支持的可付款额度")
        if prepayment:
            recommendation = "这是预付款：建议确认合同预付条款、收款账户和退款保障，再由负责人单独批准。"
            if len(evidence_items) < 2:
                blockers.append("预付款至少需要合同/订单和收款账户两类证据")
        vendor = str(target.get("vendor") or "").strip()
        approved_accounts = [
            row for row in vendor_bank_accounts
            if row.get("status") == "已批准"
            and str(row.get("entity_id") or "") == entity_id
            and str(row.get("vendor") or "").strip().casefold() == vendor.casefold()
            and str(row.get("currency") or "").upper() == currency
        ]
        selected = next((row for row in approved_accounts if row.get("id") == bank_account_id), None)
        if bank_account_id and not selected:
            blockers.append("所选供应商收款账户未批准、已停用或与主体/供应商/币种不一致")
        elif require_approved_vendor_account and not bank_account_id:
            blockers.append("付款申请必须选择经独立复核的供应商收款账户")
        if selected:
            bank_binding = {
                "account_id": selected.get("id"), "account_masked": selected.get("account_masked"),
                "account_fingerprint": selected.get("account_fingerprint"),
                "beneficiary_name": selected.get("beneficiary_name"), "bank_name": selected.get("bank_name"),
            }
    reserved = sum(
        _number(item.get("amount")) for item in existing_requests
        if item.get("status") in {"待批准", "已批准"}
        and item.get("target_type") == target_type and item.get("target_id") == target_id
        and str(item.get("entity_id") or "") == entity_id
        and str(item.get("currency") or "CNY").upper() == currency
    )
    available = _number(
        target.get("payment_available_amount")
        if target_type == "payable" and not prepayment and "payment_available_amount" in target
        else target.get("outstanding") or target.get("approved_amount")
    )
    if available and reserved + amount > available + 0.01:
        blockers.append(f"已有待批/已批申请占用 {reserved:,.2f}，本次申请会超过当前未付余额")
    if not evidence_items:
        blockers.append("缺少付款依据")
    return {
        "id": _request_id(target_type, target_id, amount),
        "target_type": target_type, "target_id": target_id,
        "entity_id": entity_id,
        "amount": amount, "currency": currency, "purpose": str(purpose or "")[:500],
        "prepayment": bool(prepayment), "evidence": evidence_items,
        "vendor_bank_binding": bank_binding,
        "requested_by": actor[:80], "requested_at": _now(),
        "status": "阻塞" if blockers else "待批准", "blockers": blockers,
        "agent_recommendation": recommendation,
        "approval": None,
    }


def decide_payment_request(
    request: dict, decision: str, actor: str, rationale: str,
    vendor_bank_accounts: Iterable[dict] = (),
) -> dict:
    if decision not in PAYMENT_DECISIONS:
        raise ValueError("付款决定只能是批准、退回或取消")
    actor = str(actor or "").strip()
    rationale = str(rationale or "").strip()
    if not actor or len(rationale) < 4:
        raise ValueError("请填写审批人和至少4个字的决定依据")
    if decision == "批准" and request.get("blockers"):
        raise ValueError("付款申请仍有阻塞项，不能批准")
    if decision == "批准" and (binding := request.get("vendor_bank_binding")):
        current = next((
            row for row in vendor_bank_accounts
            if row.get("id") == binding.get("account_id")
            and row.get("status") == "已批准"
            and str(row.get("entity_id") or "") == str(request.get("entity_id") or "")
        ), None)
        if not current or current.get("account_fingerprint") != binding.get("account_fingerprint"):
            raise ValueError("供应商收款账户已停用或变更；请作废本申请并按当前已批准账户重新申请")
    if decision == "批准" and actor == str(request.get("requested_by") or "").strip():
        raise ValueError("付款申请人不能审批自己的付款申请")
    if request.get("status") in {"已批准", "已取消"}:
        raise ValueError("该付款申请已完成决定，不能重复审批")
    updated = dict(request)
    updated["status"] = {"批准": "已批准", "退回": "已退回", "取消": "已取消"}[decision]
    updated["approval"] = {
        "decision": decision, "actor": actor[:80], "rationale": rationale[:1000], "timestamp": _now(),
    }
    return updated


def create_expense_claim(
    claimant: str, claim_date: str, amount: float, currency: str, project: str,
    category: str, purpose: str, evidence: Iterable[str], actor: str,
    entity_id: str = "",
) -> dict:
    claimant, actor = str(claimant or "").strip(), str(actor or "").strip()
    if not claimant or not actor:
        raise ValueError("报销人和提交人不能为空")
    try:
        date.fromisoformat(claim_date)
    except ValueError as error:
        raise ValueError("报销日期必须为 YYYY-MM-DD") from error
    amount = _number(amount)
    if amount <= 0:
        raise ValueError("报销金额必须大于0")
    evidence_items = [str(item).strip()[:500] for item in evidence if str(item).strip()]
    blockers = []
    if not project:
        blockers.append("缺少费用归属项目")
    if len(str(purpose or "").strip()) < 4:
        blockers.append("费用用途说明不足")
    if not evidence_items:
        blockers.append("缺少发票、付款记录或业务证明")
    return {
        "id": _claim_id(claimant, claim_date, amount),
        "claimant": claimant[:80], "claim_date": claim_date,
        "entity_id": str(entity_id or "").strip(),
        "amount": amount, "approved_amount": 0.0, "currency": str(currency or "CNY").upper(),
        "project": str(project or "")[:120], "category": str(category or "待分类费用")[:120],
        "purpose": str(purpose or "")[:500], "evidence": evidence_items,
        "submitted_by": actor[:80], "submitted_at": _now(),
        "status": "阻塞" if blockers else "待审批", "blockers": blockers, "approval_history": [],
    }


def decide_expense_claim(claim: dict, decision: str, actor: str, rationale: str, approved_amount: float | None = None) -> dict:
    if decision not in {"批准", "退回"}:
        raise ValueError("报销决定只能是批准或退回")
    actor, rationale = str(actor or "").strip(), str(rationale or "").strip()
    if not actor or len(rationale) < 4:
        raise ValueError("请填写审批人和至少4个字的决定依据")
    if decision == "批准" and claim.get("blockers"):
        raise ValueError("报销单仍缺证据或归属信息，不能批准")
    if decision == "批准" and actor in {
        str(claim.get("claimant") or "").strip(), str(claim.get("submitted_by") or "").strip(),
    }:
        raise ValueError("报销人或提交人不能审批自己的报销单")
    amount = _number(approved_amount if approved_amount is not None else claim.get("amount"))
    if decision == "批准" and (amount <= 0 or amount > _number(claim.get("amount")) + 0.01):
        raise ValueError("批准金额必须大于0且不能超过申请金额")
    updated = dict(claim)
    updated["status"] = "已批准待付款" if decision == "批准" else "已退回"
    updated["approved_amount"] = amount if decision == "批准" else 0.0
    event = {
        "decision": decision, "actor": actor[:80], "rationale": rationale[:1000],
        "approved_amount": updated["approved_amount"], "timestamp": _now(),
    }
    updated["approval_history"] = [*(claim.get("approval_history") or []), event]
    return updated
