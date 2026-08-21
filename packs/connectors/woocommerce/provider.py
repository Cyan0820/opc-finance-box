from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from src.connector_http import fetch_woocommerce_order_refund_pages, urllib_transport
from src.connector_entity_credentials import (
    WOOCOMMERCE_LEGACY_CONSUMER_KEY_ENV,
    WOOCOMMERCE_LEGACY_CONSUMER_SECRET_ENV,
    WOOCOMMERCE_LEGACY_SITE_ORIGIN_ENV,
    resolve_woocommerce_entity_credentials,
)
from src.connector_sdk import (
    ConnectorContext, ConnectorDefinition, ConnectorError, ConnectorRegistry,
    ConnectorSyncWindow,
)


WOOCOMMERCE_SITE_ORIGIN_ENV = WOOCOMMERCE_LEGACY_SITE_ORIGIN_ENV
WOOCOMMERCE_CONSUMER_KEY_ENV = WOOCOMMERCE_LEGACY_CONSUMER_KEY_ENV
WOOCOMMERCE_CONSUMER_SECRET_ENV = WOOCOMMERCE_LEGACY_CONSUMER_SECRET_ENV
WOOCOMMERCE_API_CONTRACT = "wc-rest-v3"
HTTP_TRANSPORT = urllib_transport
HTTP_SLEEPER = time.sleep
_INLINE_SECRET_FIELDS = {
    "token", "access_token", "api_key", "secret", "password", "authorization",
    "consumer_key", "consumer_secret", "site_origin", "endpoint", "url",
}
_ORDER_STATUSES = {
    "pending", "processing", "on-hold", "completed", "cancelled", "refunded",
    "failed", "trash",
}


def _window(request: dict[str, Any]) -> tuple[str, datetime, str, datetime]:
    values: list[tuple[str, datetime]] = []
    for field in ("interval_start", "interval_end"):
        text = str(request.get(field) or "")
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ConnectorError(f"{field} must be an ISO-8601 timestamp") from exc
        if parsed.tzinfo is None or not re.fullmatch(r"[0-9T:+.Z-]+", text):
            raise ConnectorError(f"{field} must include a timezone and supported characters")
        values.append((text, parsed.astimezone(timezone.utc)))
    (start_text, start), (end_text, end) = values
    if start >= end:
        raise ConnectorError("interval_start must be earlier than interval_end")
    if end - start > timedelta(days=31):
        raise ConnectorError("WooCommerce evidence window must not exceed 31 days")
    return start_text, start, end_text, end


def _api_after(start: datetime) -> str:
    return (start - timedelta(microseconds=1)).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _api_before(end: datetime) -> str:
    return end.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _gmt_timestamp(value: Any, field: str, *, required: bool = False) -> tuple[str | None, datetime | None]:
    if value in (None, "") and not required:
        return None, None
    text = str(value or "")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    else:
        parsed = parsed.astimezone(timezone.utc)
    normalized = parsed.isoformat(timespec="microseconds").replace("+00:00", "Z")
    return normalized, parsed


def _source_id(value: Any, field: str) -> str:
    text = str(value if value is not None else "").strip()
    if not text or len(text) > 255 or any(ord(character) < 33 or ord(character) == 127 for character in text):
        raise ValueError(f"{field} requires a bounded source identifier")
    return text


def _hash_reference(site_binding: str, kind: str, value: Any, *, required: bool = False) -> str | None:
    if value in (None, "") and not required:
        return None
    raw = _source_id(value, kind)
    return hashlib.sha256(f"woocommerce|{site_binding}|{kind}|{raw}".encode()).hexdigest()


def _money(value: Any, field: str, *, allow_negative: bool = False) -> str:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field} must be a finite decimal string") from exc
    if not amount.is_finite() or (amount < 0 and not allow_negative):
        raise ValueError(f"{field} must be a finite {'signed' if allow_negative else 'non-negative'} decimal string")
    return format(amount, "f")


def _country(raw: dict[str, Any]) -> str | None:
    for field in ("shipping", "billing"):
        value = raw.get(field)
        if isinstance(value, dict) and value.get("country"):
            country = str(value["country"]).upper()
            if re.fullmatch(r"[A-Z]{2}", country):
                return country
            raise ValueError(f"{field}.country must be an ISO alpha-2 code")
    return None


def _payment_method(value: Any) -> str:
    text = str(value or "").lower()
    return text if re.fullmatch(r"[a-z0-9_-]{1,64}", text) else "other"


def _evidence(batch_id: str, dataset: str, object_key: str, page: int, row: int) -> dict[str, Any]:
    return {
        "source_file": "api:woocommerce",
        "source_sheet": dataset,
        "source_page": page,
        "source_row": row,
        "source_object_id": object_key,
        "batch_id": batch_id,
        "api_contract": WOOCOMMERCE_API_CONTRACT,
    }


def _normalize_order(
    raw: dict[str, Any], *, site_binding: str, entity_id: str, batch_id: str,
    page: int, row: int, start: datetime, end: datetime,
) -> dict[str, Any]:
    order_key = _hash_reference(site_binding, "order", raw.get("id"), required=True)
    assert order_key is not None
    modified_at, modified = _gmt_timestamp(raw.get("date_modified_gmt"), "date_modified_gmt", required=True)
    if modified is None or not start <= modified < end:
        raise ValueError("date_modified_gmt must fall inside the requested half-open interval")
    created_at, _ = _gmt_timestamp(raw.get("date_created_gmt"), "date_created_gmt", required=True)
    paid_at, _ = _gmt_timestamp(raw.get("date_paid_gmt"), "date_paid_gmt")
    completed_at, _ = _gmt_timestamp(raw.get("date_completed_gmt"), "date_completed_gmt")
    status = str(raw.get("status") or "").lower()
    if status not in _ORDER_STATUSES:
        raise ValueError("status is outside the supported WooCommerce order statuses")
    currency = str(raw.get("currency") or "").upper()
    if not re.fullmatch(r"[A-Z]{3}", currency):
        raise ValueError("currency must be a three-letter code")
    line_items = raw.get("line_items") or []
    if not isinstance(line_items, list) or any(not isinstance(item, dict) for item in line_items):
        raise ValueError("line_items must be an object list")
    quantity_total = 0
    for item in line_items:
        quantity = item.get("quantity")
        if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity < 0:
            raise ValueError("line item quantity must be a non-negative integer")
        quantity_total += quantity
    nested_refunds = raw.get("refunds") or []
    if not isinstance(nested_refunds, list) or any(not isinstance(item, dict) for item in nested_refunds):
        raise ValueError("refunds must be an object list")
    lifetime_refund_total = Decimal("0")
    for item in nested_refunds:
        lifetime_refund_total += abs(Decimal(_money(item.get("total"), "refunds.total", allow_negative=True)))
    transaction_key = _hash_reference(site_binding, "transaction", raw.get("transaction_id"))
    return {
        "woocommerce_order_key": order_key,
        "entity_id": entity_id,
        "created_at": created_at,
        "modified_at": modified_at,
        "status": status,
        "currency": currency,
        "discount_total": _money(raw.get("discount_total"), "discount_total"),
        "discount_tax": _money(raw.get("discount_tax"), "discount_tax"),
        "shipping_total": _money(raw.get("shipping_total"), "shipping_total"),
        "shipping_tax": _money(raw.get("shipping_tax"), "shipping_tax"),
        "cart_tax": _money(raw.get("cart_tax"), "cart_tax"),
        "total": _money(raw.get("total"), "total"),
        "total_tax": _money(raw.get("total_tax"), "total_tax"),
        "prices_include_tax": raw.get("prices_include_tax") is True,
        "payment_method": _payment_method(raw.get("payment_method")),
        "transaction_key": transaction_key,
        "paid_at": paid_at,
        "completed_at": completed_at,
        "destination_country": _country(raw),
        "line_item_count": len(line_items),
        "quantity_total": quantity_total,
        "lifetime_refund_count": len(nested_refunds),
        "lifetime_refund_total": format(lifetime_refund_total, "f"),
        "evidence": _evidence(batch_id, "orders", order_key, page, row),
    }


def _normalize_refund(
    raw: dict[str, Any], *, site_binding: str, entity_id: str, batch_id: str,
    page: int, row: int, start: datetime, end: datetime,
) -> dict[str, Any]:
    refund_key = _hash_reference(site_binding, "refund", raw.get("id"), required=True)
    parent_order_key = _hash_reference(site_binding, "order", raw.get("parent_id"), required=True)
    assert refund_key is not None and parent_order_key is not None
    created_at, created = _gmt_timestamp(raw.get("date_created_gmt"), "date_created_gmt", required=True)
    if created is None or not start <= created < end:
        raise ValueError("date_created_gmt must fall inside the requested half-open interval")
    line_items = raw.get("line_items") or []
    if not isinstance(line_items, list) or any(not isinstance(item, dict) for item in line_items):
        raise ValueError("line_items must be an object list")
    return {
        "woocommerce_refund_key": refund_key,
        "parent_order_key": parent_order_key,
        "entity_id": entity_id,
        "created_at": created_at,
        "amount": _money(raw.get("amount"), "amount"),
        "refunded_payment": raw.get("refunded_payment") is True,
        "line_item_count": len(line_items),
        "evidence": _evidence(batch_id, "refunds", refund_key, page, row),
    }


def _handler(request: dict[str, Any], context: ConnectorContext) -> dict[str, Any]:
    if any(str(key).lower() in _INLINE_SECRET_FIELDS for key in request):
        raise ConnectorError("WooCommerce origin and credentials must not be passed in connector requests")
    entity_id = request.get("default_entity_id")
    if entity_id not in context.allowed_entity_ids:
        raise ConnectorError("WooCommerce connector requires a valid default_entity_id")
    start_text, start, end_text, end = _window(request)
    mode = request.get("mode", "fixture")
    if mode == "fixture":
        order_pages = request.get("order_pages")
        refund_pages = request.get("refund_pages")
        site_label = str(request.get("fixture_site_label") or "fixture-store")
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", site_label):
            raise ConnectorError("fixture_site_label must be a bounded non-secret label")
        if (
            not isinstance(order_pages, list) or any(not isinstance(page, list) for page in order_pages)
            or any(not isinstance(item, dict) for page in order_pages for item in page)
            or not isinstance(refund_pages, list) or any(not isinstance(page, list) for page in refund_pages)
            or any(not isinstance(item, dict) for page in refund_pages for item in page)
        ):
            raise ConnectorError("WooCommerce fixture mode requires object-list order_pages and refund_pages")
        site_binding = hashlib.sha256(f"woocommerce|fixture|{site_label}".encode()).hexdigest()
        canonical = json.dumps({
            "entity_id": entity_id, "interval_start": start_text, "interval_end": end_text,
            "site_binding": site_binding, "orders": order_pages, "refunds": refund_pages,
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        batch_id = hashlib.sha256(f"woocommerce|fixture|{canonical}".encode()).hexdigest()[:24]
        source = {
            "kind": "fixture", "name": "woocommerce.order_refund_activity",
            "api_contract": WOOCOMMERCE_API_CONTRACT, "site_binding_sha256": site_binding,
            "interval_start": start_text, "interval_end": end_text,
            "order_page_count": len(order_pages), "refund_page_count": len(refund_pages),
            "network_access_performed": False, "retry_count": 0,
        }
    elif mode == "fetch":
        max_pages = request.get("max_pages", 100)
        page_size = request.get("page_size", 100)
        if not isinstance(max_pages, int) or isinstance(max_pages, bool):
            raise ConnectorError("max_pages must be an integer")
        if not isinstance(page_size, int) or isinstance(page_size, bool):
            raise ConnectorError("page_size must be an integer")
        try:
            credentials = resolve_woocommerce_entity_credentials(
                str(entity_id), os.environ,
                require_entity_binding=len(context.allowed_entity_ids) > 1,
            )
            site_origin = credentials["site_origin"]
            site_binding = hashlib.sha256(
                f"woocommerce|site|{site_origin}".encode()
            ).hexdigest()
            fetched = fetch_woocommerce_order_refund_pages(
                site_origin=site_origin,
                consumer_key=credentials["consumer_key"],
                consumer_secret=credentials["consumer_secret"],
                modified_after=_api_after(start), modified_before=_api_before(end),
                refund_after=_api_after(start), refund_before=_api_before(end),
                page_size=page_size, max_pages=max_pages,
                transport=HTTP_TRANSPORT, sleeper=HTTP_SLEEPER,
            )
        except Exception as exc:
            if isinstance(exc, ConnectorError):
                raise
            raise ConnectorError(str(exc)) from exc
        order_pages = fetched["order_pages"]
        refund_pages = fetched["refund_pages"]
        batch_id = fetched["batch_id"]
        source = {
            "kind": "api", "name": "woocommerce.order_refund_activity",
            "api_contract": fetched["api_contract"], "site_binding_sha256": site_binding,
            "interval_start": start_text, "interval_end": end_text,
            "order_page_count": fetched["order_page_count"],
            "refund_page_count": fetched["refund_page_count"],
            "order_total": fetched["order_total"], "refund_total": fetched["refund_total"],
            "network_access_performed": True, "retry_count": fetched["retry_count"],
            "rate_limit_count": fetched["rate_limit_count"],
            "retry_delay_seconds_total": fetched["retry_delay_seconds_total"],
            "retry_after_honored": fetched["retry_after_honored"],
        }
    else:
        raise ConnectorError("WooCommerce connector mode must be fixture or fetch")

    orders: list[dict[str, Any]] = []
    refunds: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for page, payload in enumerate(order_pages, 1):
        for row, raw in enumerate(payload, 1):
            try:
                orders.append(_normalize_order(
                    raw, site_binding=site_binding, entity_id=str(entity_id), batch_id=batch_id,
                    page=page, row=row, start=start, end=end,
                ))
            except (TypeError, ValueError, InvalidOperation) as exc:
                rejected.append({
                    "dataset_type": "commerce.woocommerce_orders", "page": page,
                    "row": row, "reason": str(exc),
                })
    for page, payload in enumerate(refund_pages, 1):
        for row, raw in enumerate(payload, 1):
            try:
                refunds.append(_normalize_refund(
                    raw, site_binding=site_binding, entity_id=str(entity_id), batch_id=batch_id,
                    page=page, row=row, start=start, end=end,
                ))
            except (TypeError, ValueError, InvalidOperation) as exc:
                rejected.append({
                    "dataset_type": "commerce.woocommerce_refunds", "page": page,
                    "row": row, "reason": str(exc),
                })
    source.update({
        "read_only_key_required": True,
        "basic_auth_header_used": mode == "fetch",
        "query_string_credentials_used": False,
        "link_headers_followed": False,
        "customer_identity_retained": False,
        "address_retained": False,
        "customer_ip_or_user_agent_retained": False,
        "customer_note_or_metadata_retained": False,
        "product_identity_or_name_retained": False,
        "raw_source_ids_retained": False,
        "business_write_api_called": False,
    })
    return {
        "batch_id": batch_id,
        "source": source,
        "datasets": {
            "commerce.woocommerce_orders": orders,
            "commerce.woocommerce_refunds": refunds,
        },
        "rejected_rows": rejected,
    }


def register_connectors(registry: ConnectorRegistry) -> None:
    registry.register(ConnectorDefinition(
        connector_id="woocommerce.order_refund_activity",
        pack_id="connector.woocommerce",
        capability="connector.woocommerce_order_refund_activity",
        display_name="WooCommerce 修改订单与退款事件证据（只读）",
        dataset_types=("commerce.woocommerce_orders", "commerce.woocommerce_refunds"),
        handler=_handler,
        business_keys={
            "commerce.woocommerce_orders": ("woocommerce_order_key",),
            "commerce.woocommerce_refunds": ("woocommerce_refund_key",),
        },
        credential_env=(
            WOOCOMMERCE_SITE_ORIGIN_ENV,
            WOOCOMMERCE_CONSUMER_KEY_ENV,
            WOOCOMMERCE_CONSUMER_SECRET_ENV,
        ),
        network_access=True,
        sync_window=ConnectorSyncWindow(
            start_field="interval_start", end_field="interval_end",
            value_format="iso8601", max_incremental_days=31, max_backfill_days=366,
        ),
    ))
