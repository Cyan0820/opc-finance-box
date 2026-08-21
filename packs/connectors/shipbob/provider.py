from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from src.connector_http import fetch_shipbob_fulfillment_pages, urllib_transport
from src.connector_entity_credentials import (
    ConnectorEntityCredentialError,
    resolve_shipbob_entity_credentials,
)
from src.connector_sdk import (
    ConnectorContext, ConnectorDefinition, ConnectorError, ConnectorRegistry,
    ConnectorSyncWindow,
)


SHIPBOB_API_VERSION = "2026-07"
SHIPBOB_TOKEN_ENV = "OPC_SHIPBOB_ACCESS_TOKEN"
HTTP_TRANSPORT = urllib_transport
HTTP_SLEEPER = time.sleep
_INLINE_SECRET_FIELDS = {
    "token", "api_key", "secret", "password", "authorization", "access_token",
}


def _timestamp(value: Any, field: str, *, required: bool = False) -> tuple[str | None, datetime | None]:
    if value in (None, "") and not required:
        return None, None
    text = str(value or "")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone offset")
    if not re.fullmatch(r"[0-9T:+.Z-]+", text):
        raise ValueError(f"{field} contains unsupported characters")
    return text, parsed


def _window(request: dict[str, Any]) -> tuple[str, datetime, str, datetime]:
    try:
        start_text, start = _timestamp(
            request.get("interval_start"), "interval_start", required=True,
        )
        end_text, end = _timestamp(
            request.get("interval_end"), "interval_end", required=True,
        )
    except ValueError as exc:
        raise ConnectorError(str(exc)) from exc
    assert start_text is not None and start is not None and end_text is not None and end is not None
    if start >= end:
        raise ConnectorError("interval_start must be earlier than interval_end")
    if end - start > timedelta(days=31):
        raise ConnectorError("ShipBob evidence window must not exceed 31 days")
    return start_text, start, end_text, end


def _safe_text(value: Any, field: str, *, required: bool = False) -> str | None:
    if value in (None, "") and not required:
        return None
    text = str(value or "").strip()
    if not text or len(text) > 160 or any(ord(character) < 32 or ord(character) == 127 for character in text):
        raise ValueError(f"{field} contains unsupported or unbounded text")
    return text


def _raw_id(value: Any, field: str) -> str:
    text = str(value if value is not None else "").strip()
    if not text or len(text) > 160 or any(ord(character) < 32 or ord(character) == 127 for character in text):
        raise ValueError(f"{field} requires a bounded source identifier")
    return text


def _hash_reference(kind: str, value: Any, *, required: bool = False) -> str | None:
    if value in (None, "") and not required:
        return None
    raw = _raw_id(value, kind)
    return hashlib.sha256(f"shipbob|{kind}|{raw}".encode()).hexdigest()


def _nonnegative_integer(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return value


def _money(amount_value: Any, currency_value: Any, field: str) -> dict[str, str] | None:
    if amount_value in (None, "") and currency_value in (None, ""):
        return None
    try:
        amount = Decimal(str(amount_value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field}.amount must be a finite decimal") from exc
    if not amount.is_finite() or amount < 0:
        raise ValueError(f"{field}.amount must be a finite non-negative decimal")
    currency = str(currency_value or "").upper()
    if not re.fullmatch(r"[A-Z]{3}", currency):
        raise ValueError(f"{field}.currency must be a three-letter code")
    return {"amount": format(amount, "f"), "currency": currency}


def _warehouse(value: Any) -> tuple[str | None, str | None]:
    if value is None:
        return None, None
    if not isinstance(value, dict):
        raise ValueError("fulfillment_center must be an object")
    key = _hash_reference("fulfillment_center", value.get("id"))
    label = _safe_text(value.get("name"), "fulfillment_center.name")
    return key, label


def _evidence(
    batch_id: str, dataset: str, object_key: str, page: int, row: int,
) -> dict[str, Any]:
    return {
        "source_file": "api:shipbob",
        "source_sheet": dataset,
        "source_page": page,
        "source_row": row,
        "source_object_id": object_key,
        "batch_id": batch_id,
        "api_version": SHIPBOB_API_VERSION,
    }


def _inside_window(value: datetime | None, start: datetime, end: datetime, field: str) -> None:
    if value is None or not start <= value < end:
        raise ValueError(f"{field} must fall inside the requested half-open interval")


def _shipment_quantity(products: Any) -> tuple[int, int]:
    if products is None:
        return 0, 0
    if not isinstance(products, list):
        raise ValueError("shipment.products must be a list")
    sku_count = 0
    quantity = 0
    for product in products:
        if not isinstance(product, dict):
            raise ValueError("shipment product must be an object")
        sku_count += 1
        inventory_items = product.get("inventory_items") or []
        if not isinstance(inventory_items, list):
            raise ValueError("shipment inventory_items must be a list")
        for item in inventory_items:
            if not isinstance(item, dict):
                raise ValueError("shipment inventory item must be an object")
            quantity += _nonnegative_integer(item.get("quantity", 0), "shipment inventory quantity")
    return sku_count, quantity


def _normalize_order(
    raw: dict[str, Any], *, entity_id: str, batch_id: str, page: int, row: int,
    start: datetime, end: datetime,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    order_key = _hash_reference("order", raw.get("id"), required=True)
    assert order_key is not None
    created_at, created = _timestamp(raw.get("created_date"), "order.created_date", required=True)
    _inside_window(created, start, end, "order.created_date")
    purchase_at, _ = _timestamp(raw.get("purchase_date"), "order.purchase_date")
    recipient = raw.get("recipient") or {}
    if not isinstance(recipient, dict):
        raise ValueError("order.recipient must be an object when present")
    address = recipient.get("address") or {}
    if not isinstance(address, dict):
        raise ValueError("order.recipient.address must be an object when present")
    destination_country = str(address.get("country") or "").upper() or None
    if destination_country is not None and not re.fullmatch(r"[A-Z]{2,3}", destination_country):
        raise ValueError("order destination country must be a two- or three-letter code")
    products = raw.get("products") or []
    if not isinstance(products, list) or any(not isinstance(item, dict) for item in products):
        raise ValueError("order.products must be an object list")
    ordered_quantity = sum(
        _nonnegative_integer(product.get("quantity", 0), "order product quantity")
        for product in products
    )
    channel = raw.get("channel") or {}
    if not isinstance(channel, dict):
        raise ValueError("order.channel must be an object when present")
    order = {
        "order_key": order_key,
        "entity_id": entity_id,
        "created_at": created_at,
        "purchase_at": purchase_at,
        "status": _safe_text(raw.get("status"), "order.status"),
        "order_type": _safe_text(raw.get("type"), "order.type"),
        "channel_key": _hash_reference("channel", channel.get("id")),
        "reference_id_hash": _hash_reference("order_reference", raw.get("reference_id")),
        "order_number_hash": _hash_reference("order_number", raw.get("order_number")),
        "destination_country": destination_country,
        "ordered_line_count": len(products),
        "ordered_quantity": ordered_quantity,
        "evidence": _evidence(batch_id, "orders", order_key, page, row),
    }
    shipments_raw = raw.get("shipments") or []
    if not isinstance(shipments_raw, list) or any(not isinstance(item, dict) for item in shipments_raw):
        raise ValueError("order.shipments must be an object list")
    shipments: list[dict[str, Any]] = []
    for nested_row, shipment in enumerate(shipments_raw, 1):
        shipment_key = _hash_reference("shipment", shipment.get("id"), required=True)
        assert shipment_key is not None
        created, _ = _timestamp(shipment.get("created_date"), "shipment.created_date")
        fulfilled, _ = _timestamp(
            shipment.get("actual_fulfillment_date"), "shipment.actual_fulfillment_date",
        )
        delivered, _ = _timestamp(shipment.get("delivery_date"), "shipment.delivery_date")
        updated, _ = _timestamp(shipment.get("last_update_at"), "shipment.last_update_at")
        warehouse_key, warehouse_label = _warehouse(shipment.get("location"))
        tracking = shipment.get("tracking") or {}
        if not isinstance(tracking, dict):
            raise ValueError("shipment.tracking must be an object when present")
        sku_count, shipped_quantity = _shipment_quantity(shipment.get("products"))
        shipments.append({
            "shipment_key": shipment_key,
            "order_key": order_key,
            "entity_id": entity_id,
            "created_at": created,
            "fulfilled_at": fulfilled,
            "delivered_at": delivered,
            "last_updated_at": updated,
            "status": _safe_text(shipment.get("status"), "shipment.status"),
            "fulfillment_center_key": warehouse_key,
            "fulfillment_center_label": warehouse_label,
            "fulfillment_invoice": _money(
                shipment.get("invoice_amount"), shipment.get("invoice_currency_code"),
                "shipment.fulfillment_invoice",
            ),
            "tracking_number_hash": _hash_reference(
                "tracking_number", tracking.get("tracking_number"),
            ),
            "carrier": _safe_text(tracking.get("carrier"), "shipment.carrier"),
            "shipped_sku_count": sku_count,
            "shipped_quantity": shipped_quantity,
            "evidence": _evidence(
                batch_id, "shipments", shipment_key, page, row * 1000 + nested_row,
            ),
        })
    return order, shipments


def _normalize_return(
    raw: dict[str, Any], *, entity_id: str, batch_id: str, page: int, row: int,
    start: datetime, end: datetime,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    return_key = _hash_reference("return", raw.get("id"), required=True)
    assert return_key is not None
    inserted_at, inserted = _timestamp(raw.get("insert_date"), "return.insert_date", required=True)
    _inside_window(inserted, start, end, "return.insert_date")
    completed_at, _ = _timestamp(raw.get("completed_date"), "return.completed_date")
    arrived_at, _ = _timestamp(raw.get("arrived_date"), "return.arrived_date")
    processing_at, _ = _timestamp(raw.get("processing_date"), "return.processing_date")
    warehouse_key, warehouse_label = _warehouse(raw.get("fulfillment_center"))
    invoice = raw.get("invoice") or {}
    if not isinstance(invoice, dict):
        raise ValueError("return.invoice must be an object when present")
    result = {
        "return_key": return_key,
        "entity_id": entity_id,
        "inserted_at": inserted_at,
        "completed_at": completed_at,
        "arrived_at": arrived_at,
        "processing_at": processing_at,
        "status": _safe_text(raw.get("status"), "return.status"),
        "return_type": _safe_text(raw.get("return_type"), "return.return_type"),
        "fulfillment_center_key": warehouse_key,
        "fulfillment_center_label": warehouse_label,
        "original_shipment_key": _hash_reference(
            "shipment", raw.get("original_shipment_id"),
        ),
        "store_order_reference_hash": _hash_reference(
            "store_order", raw.get("store_order_id"),
        ),
        "return_invoice": _money(
            invoice.get("amount"), invoice.get("currency_code"), "return.invoice",
        ),
        "evidence": _evidence(batch_id, "returns", return_key, page, row),
    }
    inventory = raw.get("inventory") or []
    if not isinstance(inventory, list) or any(not isinstance(item, dict) for item in inventory):
        raise ValueError("return.inventory must be an object list")
    items: list[dict[str, Any]] = []
    for nested_row, item in enumerate(inventory, 1):
        inventory_key = _hash_reference("inventory", item.get("id"), required=True)
        assert inventory_key is not None
        requested = item.get("action_requested") or {}
        if not isinstance(requested, dict):
            raise ValueError("return action_requested must be an object")
        actions = item.get("action_taken") or []
        if not isinstance(actions, list) or any(not isinstance(action, dict) for action in actions):
            raise ValueError("return action_taken must be an object list")
        action_summary = [{
            "action": _safe_text(action.get("action"), "return action"),
            "reason": _safe_text(action.get("action_reason"), "return action reason"),
            "quantity_processed": _nonnegative_integer(
                action.get("quantity_processed", 0), "return quantity_processed",
            ),
        } for action in actions]
        items.append({
            "return_key": return_key,
            "inventory_key": inventory_key,
            "entity_id": entity_id,
            "sku": _safe_text(item.get("sku"), "return sku"),
            "quantity": _nonnegative_integer(item.get("quantity", 0), "return quantity"),
            "requested_action": _safe_text(
                requested.get("action_type") or requested.get("action"),
                "return requested action",
            ),
            "action_summary": action_summary,
            "evidence": _evidence(
                batch_id, "return_items", f"{return_key}:{inventory_key}",
                page, row * 1000 + nested_row,
            ),
        })
    return result, items


def _handler(request: dict[str, Any], context: ConnectorContext) -> dict[str, Any]:
    if any(str(key).lower() in _INLINE_SECRET_FIELDS for key in request):
        raise ConnectorError("ShipBob credentials must not be passed in connector requests")
    entity_id = request.get("default_entity_id")
    if entity_id not in context.allowed_entity_ids:
        raise ConnectorError("ShipBob connector requires a valid default_entity_id")
    start_text, start, end_text, end = _window(request)
    mode = request.get("mode", "fixture")
    if mode == "fixture":
        raw_orders = request.get("orders")
        raw_returns = request.get("returns")
        if not isinstance(raw_orders, list) or not isinstance(raw_returns, list):
            raise ConnectorError("ShipBob fixture mode requires orders and returns lists")
        order_pages = [raw_orders]
        return_pages = [raw_returns]
        canonical = json.dumps({
            "entity_id": entity_id,
            "interval_start": start_text,
            "interval_end": end_text,
            "orders": raw_orders,
            "returns": raw_returns,
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        batch_id = hashlib.sha256(f"shipbob|fixture|{canonical}".encode()).hexdigest()[:24]
        source = {
            "kind": "fixture",
            "name": "shipbob.fulfillment",
            "api_version": SHIPBOB_API_VERSION,
            "environment": "fixture",
            "interval_start": start_text,
            "interval_end": end_text,
            "order_page_count": 1,
            "return_page_count": 1,
            "network_access_performed": False,
            "retry_count": 0,
        }
    elif mode == "fetch":
        max_pages = request.get("max_pages", 50)
        page_size = request.get("page_size", 100)
        if not isinstance(max_pages, int) or isinstance(max_pages, bool):
            raise ConnectorError("max_pages must be an integer")
        if not isinstance(page_size, int) or isinstance(page_size, bool):
            raise ConnectorError("page_size must be an integer")
        environment = str(request.get("environment", "production"))
        try:
            credentials = resolve_shipbob_entity_credentials(
                str(entity_id),
                os.environ,
                legacy_environment=environment,
                require_entity_binding=len(context.allowed_entity_ids) > 1,
            )
        except ConnectorEntityCredentialError as exc:
            raise ConnectorError(str(exc)) from exc
        if not credentials["configured"]:
            raise ConnectorError("ShipBob credential is missing")
        try:
            fetched = fetch_shipbob_fulfillment_pages(
                access_token=credentials["access_token"],
                api_version=SHIPBOB_API_VERSION,
                environment=credentials["environment"],
                channel_id=credentials["channel_id"],
                interval_start=start_text,
                interval_end=end_text,
                max_pages=max_pages,
                page_size=page_size,
                transport=HTTP_TRANSPORT,
                sleeper=HTTP_SLEEPER,
            )
        except Exception as exc:
            if isinstance(exc, ConnectorError):
                raise
            raise ConnectorError(str(exc)) from exc
        order_pages = fetched["order_pages"]
        return_pages = fetched["return_pages"]
        batch_id = fetched["batch_id"]
        source = {
            "kind": "api",
            "name": "shipbob.fulfillment",
            "api_version": SHIPBOB_API_VERSION,
            "environment": fetched["environment"],
            "interval_start": start_text,
            "interval_end": end_text,
            "order_page_count": fetched["order_page_count"],
            "return_page_count": fetched["return_page_count"],
            "network_access_performed": True,
            "retry_count": fetched["retry_count"],
            "rate_limit_count": fetched["rate_limit_count"],
            "retry_delay_seconds_total": fetched["retry_delay_seconds_total"],
            "retry_after_honored": fetched["retry_after_honored"],
            "entity_credential_binding_used": credentials["entity_binding_used"],
            "channel_header_used": fetched["channel_header_used"],
            "channel_binding_sha256": (
                hashlib.sha256(
                    (
                        f"shipbob|{entity_id}|{credentials['environment']}|"
                        f"{credentials['channel_id']}"
                    ).encode()
                ).hexdigest()
                if credentials["channel_id"] is not None else None
            ),
        }
    else:
        raise ConnectorError("ShipBob connector mode must be fixture or fetch")

    orders: list[dict[str, Any]] = []
    shipments: list[dict[str, Any]] = []
    returns: list[dict[str, Any]] = []
    return_items: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for page, rows in enumerate(order_pages, 1):
        for row, raw in enumerate(rows, 1):
            try:
                normalized_order, normalized_shipments = _normalize_order(
                    raw, entity_id=str(entity_id), batch_id=batch_id, page=page, row=row,
                    start=start, end=end,
                )
                orders.append(normalized_order)
                shipments.extend(normalized_shipments)
            except (TypeError, ValueError) as exc:
                rejected.append({
                    "dataset_type": "commerce.shipbob_orders", "page": page,
                    "row": row, "reason": str(exc),
                })
    for page, rows in enumerate(return_pages, 1):
        for row, raw in enumerate(rows, 1):
            try:
                normalized_return, normalized_items = _normalize_return(
                    raw, entity_id=str(entity_id), batch_id=batch_id, page=page, row=row,
                    start=start, end=end,
                )
                returns.append(normalized_return)
                return_items.extend(normalized_items)
            except (TypeError, ValueError) as exc:
                rejected.append({
                    "dataset_type": "commerce.shipbob_returns", "page": page,
                    "row": row, "reason": str(exc),
                })
    source.update({
        "customer_identity_retained": False,
        "customer_address_retained": False,
        "raw_tracking_number_retained": False,
        "raw_source_ids_retained": False,
        "write_api_called": False,
    })
    return {
        "batch_id": batch_id,
        "source": source,
        "datasets": {
            "commerce.shipbob_orders": orders,
            "commerce.shipbob_shipments": shipments,
            "commerce.shipbob_returns": returns,
            "commerce.shipbob_return_items": return_items,
        },
        "rejected_rows": rejected,
    }


def register_connectors(registry: ConnectorRegistry) -> None:
    registry.register(ConnectorDefinition(
        connector_id="shipbob.fulfillment",
        pack_id="connector.shipbob",
        capability="connector.shipbob_fulfillment_evidence",
        display_name="ShipBob 订单、履约成本与退货处置证据（只读）",
        dataset_types=(
            "commerce.shipbob_orders", "commerce.shipbob_shipments",
            "commerce.shipbob_returns", "commerce.shipbob_return_items",
        ),
        handler=_handler,
        business_keys={
            "commerce.shipbob_orders": ("order_key",),
            "commerce.shipbob_shipments": ("shipment_key",),
            "commerce.shipbob_returns": ("return_key",),
            "commerce.shipbob_return_items": ("return_key", "inventory_key"),
        },
        credential_env=(SHIPBOB_TOKEN_ENV,),
        network_access=True,
        sync_window=ConnectorSyncWindow(
            start_field="interval_start",
            end_field="interval_end",
            value_format="iso8601",
            max_incremental_days=31,
            max_backfill_days=366,
        ),
    ))
