from __future__ import annotations

from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
from typing import Any

from .pack_services import ServiceContext


def _rows(payload: dict[str, Any], field: str, context: ServiceContext) -> list[dict[str, Any]]:
    value = payload.get(field) or []
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise ValueError(f"{field} must be a list of objects")
    invalid = [
        str(row.get("woocommerce_order_key") or row.get("woocommerce_refund_key") or index)
        for index, row in enumerate(value, 1) if row.get("entity_id") != context.entity_id
    ]
    if invalid:
        raise ValueError(
            f"{field} contains records outside statutory entity {context.entity_id}: {', '.join(invalid)}"
        )
    for index, row in enumerate(value, 1):
        evidence = row.get("evidence")
        if not isinstance(evidence, dict) or not evidence.get("source_file") or not evidence.get("batch_id"):
            raise ValueError(f"{field}[{index}] requires source_file and batch_id evidence")
    return [dict(row) for row in value]


def _duplicates(rows: list[dict[str, Any]], field: str) -> list[str]:
    values = [str(row.get(field) or "") for row in rows]
    if any(not value for value in values):
        raise ValueError(f"{field} must be present on every row")
    counts = Counter(values)
    return sorted(value for value, count in counts.items() if count > 1)


def _decimal(value: Any, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field} must be a finite decimal string") from exc
    if not result.is_finite():
        raise ValueError(f"{field} must be a finite decimal string")
    return result


def _text(value: Decimal) -> str:
    return format(value, "f")


def summarize_woocommerce_order_refund_activity(
    payload: dict[str, Any], context: ServiceContext,
) -> dict[str, Any]:
    orders = _rows(payload, "orders", context)
    refunds = _rows(payload, "refunds", context)
    duplicate_order_keys = _duplicates(orders, "woocommerce_order_key")
    duplicate_refund_keys = _duplicates(refunds, "woocommerce_refund_key")
    orders_by_key = {str(row["woocommerce_order_key"]): row for row in orders}
    refunds_by_order: dict[str, list[dict[str, Any]]] = defaultdict(list)
    orphan_refund_keys = []
    for refund in refunds:
        parent = str(refund.get("parent_order_key") or "")
        if parent not in orders_by_key:
            orphan_refund_keys.append(str(refund["woocommerce_refund_key"]))
        else:
            refunds_by_order[parent].append(refund)

    status_counts = Counter(str(row.get("status") or "unknown") for row in orders)
    payment_method_counts = Counter(str(row.get("payment_method") or "other") for row in orders)
    currency_totals: dict[str, dict[str, Decimal | int]] = defaultdict(
        lambda: {
            "order_count": 0, "order_total": Decimal("0"),
            "order_tax": Decimal("0"), "shipping_total": Decimal("0"),
            "discount_total": Decimal("0"), "lifetime_refund_total": Decimal("0"),
            "window_refund_event_total": Decimal("0"),
        }
    )
    order_reviews = []
    arithmetic_exception_keys = []
    for order in orders:
        key = str(order["woocommerce_order_key"])
        currency = str(order.get("currency") or "").upper()
        if len(currency) != 3 or not currency.isalpha():
            raise ValueError(f"order {key} currency must be a three-letter code")
        total = _decimal(order.get("total"), f"order {key} total")
        total_tax = _decimal(order.get("total_tax"), f"order {key} total_tax")
        shipping = _decimal(order.get("shipping_total"), f"order {key} shipping_total")
        discount = _decimal(order.get("discount_total"), f"order {key} discount_total")
        lifetime_refund = _decimal(
            order.get("lifetime_refund_total"), f"order {key} lifetime_refund_total",
        )
        current_refunds = sum(
            (_decimal(row.get("amount"), f"refund {row.get('woocommerce_refund_key')} amount")
             for row in refunds_by_order.get(key, [])),
            Decimal("0"),
        )
        arithmetic_ok = (
            min(total, total_tax, shipping, discount, lifetime_refund, current_refunds) >= 0
            and total_tax <= total
            and lifetime_refund <= total
            and current_refunds <= lifetime_refund
        )
        if not arithmetic_ok:
            arithmetic_exception_keys.append(key)
        review_flags = []
        if not order.get("destination_country"):
            review_flags.append("missing_destination_country")
        if not order.get("paid_at"):
            review_flags.append("no_paid_timestamp")
        if order.get("status") in {"cancelled", "refunded", "failed", "trash"}:
            review_flags.append("terminal_or_exception_order_status")
        if current_refunds != lifetime_refund:
            review_flags.append("window_refund_events_do_not_equal_lifetime_refund_total")
        order_reviews.append({
            "woocommerce_order_key": key,
            "status": order.get("status"),
            "currency": currency,
            "destination_country": order.get("destination_country"),
            "total": _text(total),
            "total_tax": _text(total_tax),
            "lifetime_refund_total": _text(lifetime_refund),
            "window_refund_event_total": _text(current_refunds),
            "arithmetic_ok": arithmetic_ok,
            "review_flags": review_flags,
        })
        aggregate = currency_totals[currency]
        aggregate["order_count"] = int(aggregate["order_count"]) + 1
        aggregate["order_total"] = Decimal(aggregate["order_total"]) + total
        aggregate["order_tax"] = Decimal(aggregate["order_tax"]) + total_tax
        aggregate["shipping_total"] = Decimal(aggregate["shipping_total"]) + shipping
        aggregate["discount_total"] = Decimal(aggregate["discount_total"]) + discount
        aggregate["lifetime_refund_total"] = Decimal(aggregate["lifetime_refund_total"]) + lifetime_refund
        aggregate["window_refund_event_total"] = (
            Decimal(aggregate["window_refund_event_total"]) + current_refunds
        )

    currency_summary = [
        {
            "currency": currency,
            **{
                field: int(value) if field == "order_count" else _text(Decimal(value))
                for field, value in values.items()
            },
        }
        for currency, values in sorted(currency_totals.items())
    ]
    blockers = []
    if duplicate_order_keys or duplicate_refund_keys:
        blockers.append({"code": "duplicate_woocommerce_business_key"})
    if orphan_refund_keys:
        blockers.append({"code": "woocommerce_refund_parent_order_missing"})
    if arithmetic_exception_keys:
        blockers.append({"code": "woocommerce_order_refund_arithmetic_invalid"})
    return {
        "ready": not blockers,
        "entity_id": context.entity_id,
        "order_count": len(orders),
        "refund_event_count": len(refunds),
        "status_counts": dict(sorted(status_counts.items())),
        "payment_method_counts": dict(sorted(payment_method_counts.items())),
        "currency_summary": currency_summary,
        "order_reviews": order_reviews,
        "duplicate_order_keys": duplicate_order_keys,
        "duplicate_refund_keys": duplicate_refund_keys,
        "orphan_refund_keys": sorted(orphan_refund_keys),
        "arithmetic_exception_keys": sorted(arithmetic_exception_keys),
        "destination_review_required_count": sum(
            1 for item in order_reviews if "missing_destination_country" in item["review_flags"]
        ),
        "unpaid_or_unconfirmed_order_count": sum(
            1 for item in order_reviews if "no_paid_timestamp" in item["review_flags"]
        ),
        "blockers": blockers,
        "candidate_only": True,
        "cross_currency_total_prohibited": True,
        "payment_settlement_inferred": False,
        "revenue_recognition_performed": False,
        "tax_liability_determined": False,
        "inventory_or_cogs_modified": False,
        "posting_performed": False,
        "external_actions_performed": False,
        "review_boundaries": [
            "WooCommerce order status and paid timestamp do not prove processor or bank settlement.",
            "Destination country and reported tax are evidence inputs, not a tax registration or liability decision.",
            "Refund events and lifetime order refunds require completeness review across the selected change window.",
        ],
    }
