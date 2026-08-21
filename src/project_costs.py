from __future__ import annotations

import re
from collections import defaultdict
from typing import Any, Iterable

from .business_flows import build_payables_register
from .game_prepaid_costs import is_special_cost_managed


APPROVED_REQUEST_STATUSES = {"已批准", "已下单"}
ACCEPTED_DELIVERY_STATUSES = {"已验收", "部分验收"}
VERIFIED_INVOICE_STATUSES = {"已查验", "查验通过", "有效"}


def _number(value: Any) -> float:
    try:
        return round(float(value or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def _text(value: Any) -> str:
    return str(value or "").strip()


def _period(value: Any) -> str:
    text = _text(value)
    return text[:7] if re.match(r"^\d{4}-\d{2}", text) else ""


def _currency(row: dict) -> str:
    return _text(row.get("currency") or "CNY").upper()


def _explicit_project(row: dict) -> str:
    project = _text(row.get("project"))
    if not project or any(token in project for token in ("待分配", "公司公共", "未知项目")):
        return ""
    return project


def _same_owner(left: dict, right: dict) -> bool:
    return _text(left.get("entity_id")) == _text(right.get("entity_id"))


def _active_verified_invoice_amount(purchase: dict, invoices: Iterable[dict]) -> float:
    purchase_id = _text(purchase.get("id"))
    return round(sum(
        _number(invoice.get("total_amount"))
        for invoice in invoices
        if _text((invoice.get("purchase_match") or {}).get("purchase_id")) == purchase_id
        and _same_owner(invoice, purchase)
        and invoice.get("verification_status") in VERIFIED_INVOICE_STATUSES
        and not invoice.get("anomalies")
    ), 2)


def _blank_bucket(entity_id: str, project: str, currency: str, period: str) -> dict:
    return {
        "entity_id": entity_id,
        "project": project,
        "currency": currency,
        "period": period,
        "categories": set(),
        "budget_amount": 0.0,
        "budget_source_ids": set(),
        "approved_request_amount": 0.0,
        "approved_request_ids": set(),
        "committed_order_amount": 0.0,
        "open_commitment": 0.0,
        "order_ids": set(),
        "delivered_pending_acceptance": 0.0,
        "accepted_actual": 0.0,
        "valid_invoice_amount": 0.0,
        "pending_invoice": 0.0,
        "pending_payment": 0.0,
        "paid_amount": 0.0,
        "delivery_evidence_count": 0,
        "acceptance_evidence_count": 0,
        "invoice_evidence_count": 0,
        "issues": set(),
    }


def build_project_procurement_cost_view(datasets: dict[str, list[dict]], period: str) -> dict:
    """Build a management-only project cost bridge without mutating source records.

    Orders become commitments only through an approved request with matching entity,
    project and currency. Delivery is not cost. Accepted delivery events are current
    management cost; invoice and payment only change evidence/exposure states.
    Explicit legacy accepted amounts remain visible as actual cost with an evidence gap,
    but their orders do not become approved commitments or consume a budget.
    """
    period = _period(period)
    requests = list(datasets.get("procurement_requests") or [])
    purchases = list(datasets.get("purchases") or [])
    deliveries = list(datasets.get("purchase_deliveries") or [])
    invoices = list(datasets.get("invoices") or [])
    allocations = list(datasets.get("cash_allocations") or [])
    payables = {
        (_text(row.get("entity_id")), _text(row.get("id"))): row
        for row in build_payables_register(purchases, invoices, allocations)["rows"]
    }
    buckets: dict[tuple[str, str, str], dict] = {}
    gaps = defaultdict(int)
    actual_cost_lines: list[dict] = []

    def bucket_for(entity_id: str, project: str, currency: str) -> dict:
        key = (entity_id, project, currency)
        if key not in buckets:
            buckets[key] = _blank_bucket(entity_id, project, currency, period)
        return buckets[key]

    # Budget is never inferred from an order amount: only exact, explicit plan ownership.
    for line in datasets.get("plan_lines") or []:
        if _period(line.get("period")) != period or line.get("direction") == "收入":
            continue
        if (line.get("scenario") or "基准") != "基准" or line.get("anomalies"):
            continue
        project = _explicit_project(line)
        entity_id = _text(line.get("entity_id"))
        if not project:
            gaps["budget_missing_project"] += 1
            continue
        row = bucket_for(entity_id, project, _currency(line))
        row["budget_amount"] += _number(line.get("amount"))
        row["categories"].add(_text(line.get("category")) or "未分类")
        if line.get("id"):
            row["budget_source_ids"].add(_text(line.get("id")))

    request_index: dict[tuple[str, str], dict] = {}
    for request in requests:
        entity_id = _text(request.get("entity_id"))
        request_id = _text(request.get("id"))
        if request_id:
            request_index[(entity_id, request_id)] = request
        if _period(request.get("period")) != period or request.get("status") not in APPROVED_REQUEST_STATUSES:
            continue
        project = _explicit_project(request)
        if not project:
            gaps["approved_request_missing_project"] += 1
            continue
        row = bucket_for(entity_id, project, _currency(request))
        row["approved_request_amount"] += _number(request.get("amount"))
        row["approved_request_ids"].add(request_id)
        row["categories"].add(_text(request.get("category")) or "未分类")
        snapshot = request.get("budget_snapshot") or {}
        if not snapshot.get("budget_found"):
            row["issues"].add("已批准申请缺少同口径预算绑定")

    deliveries_by_order: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for delivery in deliveries:
        deliveries_by_order[(_text(delivery.get("entity_id")), _text(delivery.get("purchase_id")))].append(delivery)

    for purchase in purchases:
        entity_id = _text(purchase.get("entity_id"))
        purchase_id = _text(purchase.get("id"))
        currency = _currency(purchase)
        project = _explicit_project(purchase)
        request_id = _text(purchase.get("procurement_request_id"))
        request = request_index.get((entity_id, request_id)) if request_id else None
        linked = bool(
            request
            and request.get("status") in APPROVED_REQUEST_STATUSES
            and _explicit_project(request) == project
            and _currency(request) == currency
        )
        if not project:
            gaps["order_missing_project"] += 1
            continue
        row = bucket_for(entity_id, project, currency)
        row["categories"].add(_text(purchase.get("category")) or "未分类")
        special_cost_managed = is_special_cost_managed(purchase)
        if special_cost_managed:
            row["issues"].add("授权/云资源由已批准政策的期间释放候选桥确认，不在采购验收时确认成本")
        order_deliveries = deliveries_by_order.get((entity_id, purchase_id), [])
        milestones = {_text(item.get("id")) for item in purchase.get("milestones") or [] if item.get("id")}
        valid_deliveries = []
        for delivery in order_deliveries:
            if not _same_owner(delivery, purchase):
                gaps["cross_entity_delivery"] += 1
                continue
            if _text(delivery.get("purchase_id")) != purchase_id:
                continue
            if not milestones or _text(delivery.get("milestone_id")) not in milestones:
                gaps["delivery_missing_milestone"] += 1
                row["issues"].add("交付记录缺少有效里程碑绑定")
                continue
            valid_deliveries.append(delivery)
            row["delivery_evidence_count"] += len(delivery.get("evidence") or [])

        accepted_total = round(sum(
            _number(item.get("accepted_amount"))
            for item in valid_deliveries
            if item.get("status") in ACCEPTED_DELIVERY_STATUSES
        ), 2)
        for delivery in valid_deliveries:
            delivered = _number(delivery.get("delivered_amount"))
            accepted = _number(delivery.get("accepted_amount"))
            if delivery.get("status") == "已交付待验收":
                row["delivered_pending_acceptance"] += max(0.0, delivered - accepted)
            if delivery.get("status") not in ACCEPTED_DELIVERY_STATUSES or accepted <= 0:
                continue
            row["acceptance_evidence_count"] += len(delivery.get("acceptance_evidence") or [])
            acceptance_period = _period(delivery.get("period"))
            if not acceptance_period:
                gaps["acceptance_missing_period"] += 1
                row["issues"].add("验收事件缺少明确成本期间")
                continue
            if acceptance_period == period and not special_cost_managed:
                row["accepted_actual"] += accepted
                actual_cost_lines.append({
                    "entity_id": entity_id,
                    "project": project,
                    "category": _text(purchase.get("category")),
                    "item": _text(purchase.get("item")),
                    "currency": currency,
                    "period": acceptance_period,
                    "amount": accepted,
                    "source": f"验收事件 {delivery.get('id')}",
                    "basis": "accepted_delivery_event",
                    "purchase_id": purchase_id,
                    "delivery_id": delivery.get("id"),
                })

        request_period = _period((request or {}).get("period"))
        if linked and request_period == period:
            ordered = _number(purchase.get("ordered_amount"))
            row["committed_order_amount"] += ordered
            row["open_commitment"] += max(0.0, ordered - accepted_total)
            row["order_ids"].add(purchase_id)
        elif request_id:
            gaps["order_invalid_request_binding"] += 1
            row["issues"].add("订单未通过同主体、项目、币种的已批准申请校验")
        else:
            gaps["legacy_order_without_request"] += 1
            row["issues"].add("历史订单缺少采购申请及预算绑定")

        # Legacy accepted amounts are management actual only when there are no event
        # records at all. They never create a budget reservation or approved commitment.
        if not valid_deliveries and not request_id and not special_cost_managed:
            legacy_accepted = _number(purchase.get("accepted_amount"))
            legacy_period = _period(purchase.get("order_date"))
            if legacy_accepted > 0 and legacy_period == period:
                row["accepted_actual"] += legacy_accepted
                row["issues"].add("历史验收金额缺逐事件验收证据")
                actual_cost_lines.append({
                    "entity_id": entity_id,
                    "project": project,
                    "category": _text(purchase.get("category")),
                    "item": _text(purchase.get("item")),
                    "currency": currency,
                    "period": legacy_period,
                    "amount": legacy_accepted,
                    "source": f"历史验收汇总 {purchase_id}",
                    "basis": "legacy_explicit_acceptance",
                    "purchase_id": purchase_id,
                    "delivery_id": None,
                })
                accepted_total = legacy_accepted

        verified_invoice = _active_verified_invoice_amount(purchase, invoices)
        payable = payables.get((entity_id, purchase_id)) or {}
        reconciled_paid = _number(payable.get("reconciled_paid_amount"))
        payment_eligible = min(accepted_total, verified_invoice)
        row["valid_invoice_amount"] += verified_invoice
        row["pending_invoice"] += max(0.0, accepted_total - verified_invoice)
        row["pending_payment"] += max(0.0, payment_eligible - reconciled_paid)
        row["paid_amount"] += reconciled_paid
        row["invoice_evidence_count"] += sum(
            1 for invoice in invoices
            if _text((invoice.get("purchase_match") or {}).get("purchase_id")) == purchase_id
            and _same_owner(invoice, purchase)
            and invoice.get("verification_status") in VERIFIED_INVOICE_STATUSES
            and not invoice.get("anomalies")
        )

    rows = []
    for row in buckets.values():
        row["budget_remaining"] = row["budget_amount"] - row["approved_request_amount"]
        row["categories"] = sorted(value for value in row["categories"] if value)
        row["budget_source_ids"] = sorted(row["budget_source_ids"])
        row["approved_request_ids"] = sorted(row["approved_request_ids"])
        row["order_ids"] = sorted(row["order_ids"])
        row["issues"] = sorted(row["issues"])
        for key in (
            "budget_amount", "approved_request_amount", "committed_order_amount", "open_commitment",
            "delivered_pending_acceptance", "accepted_actual", "valid_invoice_amount", "pending_invoice",
            "pending_payment", "paid_amount", "budget_remaining",
        ):
            row[key] = round(row[key], 2)
        if row["issues"]:
            row["control_status"] = "证据待补"
        elif row["delivered_pending_acceptance"] > 0:
            row["control_status"] = "待验收"
        elif row["pending_invoice"] > 0:
            row["control_status"] = "待开票/查验"
        elif row["pending_payment"] > 0:
            row["control_status"] = "待付款"
        else:
            row["control_status"] = "链路完整"
        rows.append(row)
    rows.sort(key=lambda item: (item["project"], item["entity_id"], item["currency"]))

    by_scope: dict[tuple[str, str], dict] = {}
    for row in rows:
        key = (row["entity_id"], row["currency"])
        scope = by_scope.setdefault(key, {
            "entity_id": row["entity_id"], "currency": row["currency"],
            "committed_order_amount": 0.0, "accepted_actual": 0.0,
            "pending_invoice": 0.0, "pending_payment": 0.0,
        })
        for name in ("committed_order_amount", "accepted_actual", "pending_invoice", "pending_payment"):
            scope[name] += row[name]
    summary_by_entity_currency = []
    for scope in by_scope.values():
        for name in ("committed_order_amount", "accepted_actual", "pending_invoice", "pending_payment"):
            scope[name] = round(scope[name], 2)
        summary_by_entity_currency.append(scope)
    summary_by_entity_currency.sort(key=lambda item: (item["entity_id"], item["currency"]))

    return {
        "period": period,
        "rows": rows,
        "actual_cost_lines": actual_cost_lines,
        "summary": {
            "row_count": len(rows),
            "project_count": len({row["project"] for row in rows}),
            "evidence_gap_count": sum(gaps.values()),
            "gaps": dict(sorted(gaps.items())),
            "by_entity_currency": summary_by_entity_currency,
        },
        "guardrails": [
            "主体和币种分别展示，不直接相加。",
            "只有逐事件验收金额进入管理实际成本；交付、发票和付款均不替代验收。",
            "付款只减少待付款，不改变项目成本；订单只形成承诺，不直接进入已实现贡献。",
            "项目、预算、采购申请和里程碑必须显式绑定，不按金额、供应商或相似名称猜测归属。",
        ],
    }
