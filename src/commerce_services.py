from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Any

from .commerce import CommerceOrder
from .pack_services import ServiceContext


def _orders(payload: dict[str, Any], context: ServiceContext) -> list[CommerceOrder]:
    rows = payload.get("orders") or []
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("orders must be a list of objects")
    parsed = [CommerceOrder.from_dict(row) for row in rows]
    allowed = set(context.entity_ids)
    unknown = sorted({order.entity_id for order in parsed} - allowed)
    if unknown:
        raise ValueError(f"orders contain unknown legal entities: {', '.join(unknown)}")
    return parsed


def _duplicates(orders: list[CommerceOrder]) -> list[str]:
    return sorted({
        order.order_id for order in orders
        if sum(item.entity_id == order.entity_id and item.order_id == order.order_id for item in orders) > 1
    })


def summarize_commerce_refunds(payload: dict[str, Any], context: ServiceContext) -> dict[str, Any]:
    orders = _orders(payload, context)
    duplicates = _duplicates(orders)
    buckets: dict[tuple[str, str, str, str], dict[str, Decimal]] = defaultdict(
        lambda: defaultdict(Decimal)
    )
    for order in orders:
        key = (order.entity_id, order.period, order.channel, order.currency)
        bucket = buckets[key]
        bucket["order_count"] += 1
        bucket["gross_merchandise_sales_ex_tax"] += order.merchandise_gross_ex_tax
        bucket["shipping_income_ex_tax"] += order.shipping_income_ex_tax
        bucket["discounts_ex_tax"] += order.discounts_ex_tax
        bucket["refunds_ex_tax"] += order.refunds_ex_tax
        bucket["refunded_tax"] += order.refunded_tax
        if order.refunds_ex_tax or order.refunded_tax:
            bucket["refunded_order_count"] += 1
    rows = []
    for key, value in sorted(buckets.items()):
        gross = value["gross_merchandise_sales_ex_tax"]
        shipping = value["shipping_income_ex_tax"]
        discounts = value["discounts_ex_tax"]
        refunds = value["refunds_ex_tax"]
        gross_order_sales = gross + shipping
        discounts_and_refunds = discounts + refunds
        net_sales = gross_order_sales - discounts_and_refunds
        rows.append({
            "entity_id": key[0],
            "period": key[1],
            "channel": key[2],
            "currency": key[3],
            "order_count": int(value["order_count"]),
            "refunded_order_count": int(value["refunded_order_count"]),
            "gross_ex_tax": float(gross),
            "gross_merchandise_sales_ex_tax": float(gross),
            "shipping_income_ex_tax": float(shipping),
            "gross_order_sales_ex_tax_including_shipping": float(gross_order_sales),
            "discounts_ex_tax": float(discounts),
            "refunds_ex_tax": float(refunds),
            "discounts_and_refunds_ex_tax": float(discounts_and_refunds),
            "net_sales_ex_tax": float(net_sales),
            "refunded_tax": float(value["refunded_tax"]),
            "refund_rate_ex_tax": round(float(refunds / gross), 4) if gross else None,
        })
    return {
        "ready": not duplicates and bool(rows),
        "refund_summary": rows,
        "duplicate_order_ids": duplicates,
        "guardrail": "Refund facts are reconciled by entity, channel and currency; refund policy approval is not performed.",
    }


def summarize_fulfillment_costs(payload: dict[str, Any], context: ServiceContext) -> dict[str, Any]:
    orders = _orders(payload, context)
    duplicates = _duplicates(orders)
    buckets: dict[tuple[str, str, str, str], dict[str, Decimal]] = defaultdict(
        lambda: defaultdict(Decimal)
    )
    for order in orders:
        key = (order.entity_id, order.period, order.channel, order.currency)
        metrics = order.metrics()
        bucket = buckets[key]
        bucket["order_count"] += 1
        bucket["net_revenue_ex_tax"] += metrics["net_revenue_ex_tax"]
        bucket["cogs"] += order.cogs
        bucket["fulfillment_cost"] += order.fulfillment_cost
        bucket["shipping_cost"] += order.shipping_cost
        bucket["contribution_before_channel_fees"] += metrics["contribution_before_channel_fees"]
    rows = []
    for key, value in sorted(buckets.items()):
        revenue = value["net_revenue_ex_tax"]
        fulfillment_total = value["fulfillment_cost"] + value["shipping_cost"]
        rows.append({
            "entity_id": key[0],
            "period": key[1],
            "channel": key[2],
            "currency": key[3],
            "order_count": int(value["order_count"]),
            "net_revenue_ex_tax": float(revenue),
            "cogs": float(value["cogs"]),
            "fulfillment_cost": float(value["fulfillment_cost"]),
            "shipping_cost": float(value["shipping_cost"]),
            "fulfillment_and_shipping_rate": (
                round(float(fulfillment_total / revenue), 4) if revenue else None
            ),
            "contribution_before_channel_fees": float(value["contribution_before_channel_fees"]),
        })
    return {
        "ready": not duplicates and bool(rows),
        "fulfillment_summary": rows,
        "duplicate_order_ids": duplicates,
        "guardrail": "COGS is imported order evidence, not a replacement for an approved inventory valuation method.",
    }


def summarize_destination_evidence(payload: dict[str, Any], context: ServiceContext) -> dict[str, Any]:
    orders = _orders(payload, context)
    duplicates = _duplicates(orders)
    buckets: dict[tuple[str, str, str, str], dict[str, Decimal]] = defaultdict(
        lambda: defaultdict(Decimal)
    )
    for order in orders:
        key = (order.entity_id, order.destination_country, order.period, order.currency)
        metrics = order.metrics()
        bucket = buckets[key]
        bucket["order_count"] += 1
        bucket["net_revenue_ex_tax"] += metrics["net_revenue_ex_tax"]
        bucket["tax_evidence_net"] += metrics["tax_evidence_net"]
    rows = [{
        "entity_id": key[0],
        "destination_country": key[1],
        "period": key[2],
        "currency": key[3],
        "order_count": int(value["order_count"]),
        "net_revenue_ex_tax": float(value["net_revenue_ex_tax"]),
        "tax_evidence_net": float(value["tax_evidence_net"]),
        "tax_status": "evidence_only_not_registration_or_tax_due",
    } for key, value in sorted(buckets.items())]
    return {
        "ready": not duplicates and bool(rows),
        "destination_summary": rows,
        "duplicate_order_ids": duplicates,
        "guardrail": "Destination evidence does not determine registration, nexus, tax rate or tax due.",
    }
