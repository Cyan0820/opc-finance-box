from __future__ import annotations

from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
from typing import Any

from .pack_services import ServiceContext


def _rows(payload: dict[str, Any], field: str) -> list[dict[str, Any]]:
    value = payload.get(field) or []
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ValueError(f"{field} must be an object list")
    return value


def _same_entity(rows: list[dict[str, Any]], entity_id: str, field: str) -> None:
    actual = sorted({str(row.get("entity_id") or "") for row in rows})
    if actual and actual != [entity_id]:
        raise ValueError(f"{field} must contain only the statutory entity {entity_id}")


def _amount(value: Any, field: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field} must be a finite decimal") from exc
    if not parsed.is_finite() or parsed < 0:
        raise ValueError(f"{field} must be a finite non-negative decimal")
    return parsed


def summarize_shipbob_fulfillment_evidence(
    payload: dict[str, Any], context: ServiceContext,
) -> dict[str, Any]:
    """Build a deterministic, non-posting ShipBob fulfillment and return review candidate."""
    if not context.entity_id:
        raise ValueError("ShipBob fulfillment evidence requires one entity_id")
    entity_id = context.entity_id
    orders = _rows(payload, "orders")
    shipments = _rows(payload, "shipments")
    returns = _rows(payload, "returns")
    return_items = _rows(payload, "return_items")
    for field, rows in (
        ("orders", orders), ("shipments", shipments),
        ("returns", returns), ("return_items", return_items),
    ):
        _same_entity(rows, entity_id, field)

    order_keys = {str(row.get("order_key") or "") for row in orders}
    shipment_keys = {str(row.get("shipment_key") or "") for row in shipments}
    return_keys = {str(row.get("return_key") or "") for row in returns}
    missing_order_keys = sorted({
        str(row.get("order_key") or "") for row in shipments
        if str(row.get("order_key") or "") not in order_keys
    })
    missing_shipment_keys = sorted({
        str(row.get("original_shipment_key") or "") for row in returns
        if row.get("original_shipment_key")
        and str(row.get("original_shipment_key")) not in shipment_keys
    })
    missing_return_keys = sorted({
        str(row.get("return_key") or "") for row in return_items
        if str(row.get("return_key") or "") not in return_keys
    })
    blockers = []
    if missing_order_keys:
        blockers.append("ShipBob shipment references an order absent from the same evidence window")
    if missing_return_keys:
        blockers.append("ShipBob return item references a return absent from the same evidence window")

    shipments_by_order: Counter[str] = Counter(str(row.get("order_key") or "") for row in shipments)
    unfulfilled_orders = sorted(
        key for key in order_keys if key and shipments_by_order[key] == 0
    )
    shipment_status = Counter(str(row.get("status") or "unknown") for row in shipments)
    return_status = Counter(str(row.get("status") or "unknown") for row in returns)
    fulfillment_invoice_by_currency: dict[str, Decimal] = defaultdict(Decimal)
    for index, row in enumerate(shipments, 1):
        invoice = row.get("fulfillment_invoice")
        if invoice is None:
            continue
        if not isinstance(invoice, dict):
            raise ValueError(f"shipments[{index}].fulfillment_invoice must be an object")
        currency = str(invoice.get("currency") or "")
        if len(currency) != 3 or not currency.isalpha() or currency.upper() != currency:
            raise ValueError(f"shipments[{index}].fulfillment_invoice.currency is invalid")
        fulfillment_invoice_by_currency[currency] += _amount(
            invoice.get("amount"), f"shipments[{index}].fulfillment_invoice.amount",
        )

    return_lookup = {str(row.get("return_key") or ""): row for row in returns}
    disposition: dict[tuple[str, str, str], int] = defaultdict(int)
    unprocessed_return_items = []
    for row in return_items:
        return_row = return_lookup.get(str(row.get("return_key") or ""), {})
        warehouse = str(return_row.get("fulfillment_center_label") or "unassigned")
        sku = str(row.get("sku") or "unmapped")
        actions = row.get("action_summary") or []
        if not isinstance(actions, list) or any(not isinstance(action, dict) for action in actions):
            raise ValueError("return_items.action_summary must be an object list")
        processed = 0
        for action in actions:
            action_name = str(action.get("action") or "unknown")
            quantity = action.get("quantity_processed", 0)
            if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity < 0:
                raise ValueError("return action quantity_processed must be a non-negative integer")
            disposition[(warehouse, sku, action_name)] += quantity
            processed += quantity
        expected = row.get("quantity", 0)
        if not isinstance(expected, int) or isinstance(expected, bool) or expected < 0:
            raise ValueError("return item quantity must be a non-negative integer")
        if processed < expected:
            unprocessed_return_items.append({
                "return_key": row.get("return_key"),
                "inventory_key": row.get("inventory_key"),
                "expected_quantity": expected,
                "processed_quantity": processed,
            })

    return {
        "ready": bool(orders or returns) and not blockers,
        "entity_id": entity_id,
        "counts": {
            "orders": len(orders),
            "shipments": len(shipments),
            "returns": len(returns),
            "return_items": len(return_items),
        },
        "order_fulfillment": {
            "orders_with_shipments": sum(1 for key in order_keys if shipments_by_order[key] > 0),
            "orders_without_shipments": len(unfulfilled_orders),
            "unfulfilled_order_keys": unfulfilled_orders,
        },
        "shipment_status_summary": [
            {"status": status, "count": count}
            for status, count in sorted(shipment_status.items())
        ],
        "fulfillment_invoice_summary": [
            {"currency": currency, "amount": format(amount, "f")}
            for currency, amount in sorted(fulfillment_invoice_by_currency.items())
        ],
        "return_status_summary": [
            {"status": status, "count": count}
            for status, count in sorted(return_status.items())
        ],
        "return_disposition_candidates": [
            {
                "warehouse": warehouse,
                "sku": sku,
                "action": action,
                "quantity": quantity,
            }
            for (warehouse, sku, action), quantity in sorted(disposition.items())
        ],
        "unprocessed_return_items": unprocessed_return_items,
        "structural_exceptions": {
            "missing_order_keys": missing_order_keys,
            "missing_shipment_keys": missing_shipment_keys,
            "missing_return_keys": missing_return_keys,
        },
        "cross_window_return_references": missing_shipment_keys,
        "blockers": blockers,
        "candidate_only": True,
        "customer_pii_required": False,
        "revenue_recognition_performed": False,
        "inventory_adjustment_performed": False,
        "posting_performed": False,
        "external_actions_performed": False,
        "cross_currency_total_prohibited": True,
        "review_boundaries": [
            "Order/reference mapping must be reviewed against the upstream commerce source.",
            "ShipBob invoice amounts are fulfillment-cost evidence, not approved ledger postings.",
            "Restock, quarantine and disposal remain inventory-action review candidates.",
        ],
    }
