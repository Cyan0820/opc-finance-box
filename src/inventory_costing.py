from __future__ import annotations

from collections import defaultdict, deque
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

from .pack_services import ServiceContext


MONEY = Decimal("0.01")
UNIT_COST = Decimal("0.0001")
QUANTITY = Decimal("0.0001")


def _decimal(value: Any, field: str, precision: Decimal) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field} must be numeric") from exc
    if not number.is_finite():
        raise ValueError(f"{field} must be finite")
    return number.quantize(precision, rounding=ROUND_HALF_UP)


def _events(payload: dict[str, Any], field: str) -> list[dict[str, Any]]:
    rows = payload.get(field) or []
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError(f"{field} must be a list of objects")
    return [dict(row) for row in rows]


def _key(row: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(row.get("entity_id") or ""),
        str(row.get("sku") or ""),
        str(row.get("warehouse") or ""),
        str(row.get("currency") or "").upper(),
    )


def _validate_common(
    rows: list[dict[str, Any]],
    field: str,
    context: ServiceContext,
    event_ids: set[str],
) -> list[dict[str, Any]]:
    entity = context.runtime.entities.get(str(context.entity_id))
    accepted = []
    for index, row in enumerate(rows, 1):
        event_id = str(row.get("id") or "")
        if not event_id:
            raise ValueError(f"{field}[{index}] requires id")
        if event_id in event_ids:
            raise ValueError(f"duplicate inventory event id: {event_id}")
        event_ids.add(event_id)
        if row.get("entity_id") != context.entity_id:
            raise ValueError(f"{field}[{index}] is outside statutory entity {context.entity_id}")
        if not str(row.get("sku") or "") or not str(row.get("warehouse") or ""):
            raise ValueError(f"{field}[{index}] requires sku and warehouse")
        currency = str(row.get("currency") or "").upper()
        if currency != entity.functional_currency:
            raise ValueError(
                f"{field}[{index}] currency {currency} requires approved translation to {entity.functional_currency}"
            )
        if not row.get("evidence"):
            raise ValueError(f"{field}[{index}] requires evidence")
        accepted.append(row)
    return accepted


def _fifo_cost(
    acquisitions: list[dict[str, Any]],
    fulfillments: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    event_stream = []
    for row in acquisitions:
        event_stream.append((str(row.get("occurred_at") or row.get("acquired_at") or ""), 0, str(row["id"]), "acquire", row))
    for row in fulfillments:
        event_stream.append((str(row.get("occurred_at") or ""), 1, str(row["id"]), "fulfill", row))
    event_stream.sort(key=lambda item: item[:3])
    layers: dict[tuple[str, str, str, str], deque[dict[str, Any]]] = defaultdict(deque)
    costs = []
    issues = []
    for occurred_at, _, _, kind, row in event_stream:
        key = _key(row)
        quantity = _decimal(row.get("quantity"), f"{row['id']}.quantity", QUANTITY)
        if quantity <= 0:
            issues.append({"event_id": row["id"], "reason": "quantity must be positive"})
            continue
        if kind == "acquire":
            unit_cost = _decimal(row.get("unit_cost"), f"{row['id']}.unit_cost", UNIT_COST)
            if unit_cost < 0:
                issues.append({"event_id": row["id"], "reason": "unit_cost must not be negative"})
                continue
            layers[key].append({
                "source_event_id": row["id"],
                "occurred_at": occurred_at,
                "remaining_quantity": quantity,
                "unit_cost": unit_cost,
                "evidence": row["evidence"],
            })
            continue
        required = quantity
        available = sum(layer["remaining_quantity"] for layer in layers[key])
        if available < required:
            issues.append({
                "event_id": row["id"],
                "reason": "negative inventory would result",
                "required_quantity": float(required),
                "available_quantity": float(available),
            })
            continue
        consumed = []
        total_cost = Decimal("0")
        while required > 0:
            layer = layers[key][0]
            used = min(required, layer["remaining_quantity"])
            cost = (used * layer["unit_cost"]).quantize(MONEY, rounding=ROUND_HALF_UP)
            consumed.append({
                "source_event_id": layer["source_event_id"],
                "quantity": float(used),
                "unit_cost": float(layer["unit_cost"]),
                "cost": float(cost),
            })
            total_cost += cost
            layer["remaining_quantity"] -= used
            required -= used
            if layer["remaining_quantity"] == 0:
                layers[key].popleft()
        costs.append({
            "fulfillment_id": row["id"],
            "order_id": row.get("order_id"),
            "entity_id": key[0],
            "sku": key[1],
            "warehouse": key[2],
            "currency": key[3],
            "quantity": float(quantity),
            "cost": float(total_cost.quantize(MONEY)),
            "consumed_layers": consumed,
            "evidence": row["evidence"],
        })
    ending = []
    for key, queue in sorted(layers.items()):
        for layer in queue:
            ending.append({
                "entity_id": key[0],
                "sku": key[1],
                "warehouse": key[2],
                "currency": key[3],
                "source_event_id": layer["source_event_id"],
                "remaining_quantity": float(layer["remaining_quantity"]),
                "unit_cost": float(layer["unit_cost"]),
                "ending_value": float(
                    (layer["remaining_quantity"] * layer["unit_cost"]).quantize(MONEY, rounding=ROUND_HALF_UP)
                ),
            })
    return costs, ending, issues


def _weighted_average_cost(
    acquisitions: list[dict[str, Any]],
    fulfillments: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    event_stream = []
    for row in acquisitions:
        event_stream.append((str(row.get("occurred_at") or row.get("acquired_at") or ""), 0, str(row["id"]), "acquire", row))
    for row in fulfillments:
        event_stream.append((str(row.get("occurred_at") or ""), 1, str(row["id"]), "fulfill", row))
    event_stream.sort(key=lambda item: item[:3])
    balances: dict[tuple[str, str, str, str], dict[str, Decimal]] = defaultdict(
        lambda: {"quantity": Decimal("0"), "value": Decimal("0")}
    )
    costs = []
    issues = []
    for _, _, _, kind, row in event_stream:
        key = _key(row)
        quantity = _decimal(row.get("quantity"), f"{row['id']}.quantity", QUANTITY)
        if quantity <= 0:
            issues.append({"event_id": row["id"], "reason": "quantity must be positive"})
            continue
        balance = balances[key]
        if kind == "acquire":
            unit_cost = _decimal(row.get("unit_cost"), f"{row['id']}.unit_cost", UNIT_COST)
            if unit_cost < 0:
                issues.append({"event_id": row["id"], "reason": "unit_cost must not be negative"})
                continue
            balance["quantity"] += quantity
            balance["value"] += quantity * unit_cost
            continue
        if balance["quantity"] < quantity:
            issues.append({
                "event_id": row["id"],
                "reason": "negative inventory would result",
                "required_quantity": float(quantity),
                "available_quantity": float(balance["quantity"]),
            })
            continue
        average_cost = (balance["value"] / balance["quantity"]).quantize(UNIT_COST, rounding=ROUND_HALF_UP)
        cost = (quantity * average_cost).quantize(MONEY, rounding=ROUND_HALF_UP)
        balance["quantity"] -= quantity
        balance["value"] -= cost
        if balance["quantity"] == 0:
            balance["value"] = Decimal("0")
        costs.append({
            "fulfillment_id": row["id"],
            "order_id": row.get("order_id"),
            "entity_id": key[0],
            "sku": key[1],
            "warehouse": key[2],
            "currency": key[3],
            "quantity": float(quantity),
            "average_unit_cost": float(average_cost),
            "cost": float(cost),
            "evidence": row["evidence"],
        })
    ending = []
    for key, balance in sorted(balances.items()):
        average = (
            (balance["value"] / balance["quantity"]).quantize(UNIT_COST, rounding=ROUND_HALF_UP)
            if balance["quantity"] else Decimal("0")
        )
        ending.append({
            "entity_id": key[0],
            "sku": key[1],
            "warehouse": key[2],
            "currency": key[3],
            "remaining_quantity": float(balance["quantity"]),
            "average_unit_cost": float(average),
            "ending_value": float(balance["value"].quantize(MONEY, rounding=ROUND_HALF_UP)),
        })
    return costs, ending, issues


def calculate_inventory_cost(payload: dict[str, Any], context: ServiceContext) -> dict[str, Any]:
    method = str(payload.get("method") or "FIFO").upper()
    if method not in {"FIFO", "WEIGHTED_AVERAGE"}:
        raise ValueError("method must be FIFO or WEIGHTED_AVERAGE")
    ids: set[str] = set()
    opening = _validate_common(_events(payload, "opening_layers"), "opening_layers", context, ids)
    receipts = _validate_common(_events(payload, "receipts"), "receipts", context, ids)
    fulfillments = _validate_common(_events(payload, "fulfillments"), "fulfillments", context, ids)
    acquisitions = opening + receipts
    if method == "FIFO":
        costs, ending, issues = _fifo_cost(acquisitions, fulfillments)
    else:
        costs, ending, issues = _weighted_average_cost(acquisitions, fulfillments)
    return {
        "ready": not issues and bool(acquisitions or fulfillments),
        "entity_id": context.entity_id,
        "method": method,
        "fulfillment_costs": costs,
        "ending_inventory": ending,
        "issues": issues,
        "posting_performed": False,
        "review_gate": "inventory_valuation_policy",
        "guardrails": [
            "Costing runs within one legal entity and functional currency.",
            "The selected method must match the approved accounting policy before posting.",
            "Negative inventory blocks costing and is not converted to zero cost.",
        ],
    }
