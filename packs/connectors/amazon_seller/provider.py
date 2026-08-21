from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from src.connector_http import (
    fetch_amazon_seller_marketplace_evidence_pages,
    fetch_amazon_seller_transaction_pages,
    urllib_transport,
)
from src.connector_entity_credentials import (
    AMAZON_SELLER_LEGACY_CLIENT_ID_ENV,
    AMAZON_SELLER_LEGACY_CLIENT_SECRET_ENV,
    AMAZON_SELLER_LEGACY_MARKETPLACE_IDS_ENV,
    AMAZON_SELLER_LEGACY_REFRESH_TOKEN_ENV,
    AMAZON_SELLER_LEGACY_REGION_ENV,
    AMAZON_SELLER_LEGACY_SELLER_ID_ENV,
    ConnectorEntityCredentialError,
    resolve_amazon_seller_entity_credentials,
)
from src.connector_sdk import (
    ConnectorContext, ConnectorDefinition, ConnectorError, ConnectorRegistry,
    ConnectorSyncWindow,
)


AMAZON_CLIENT_ID_ENV = AMAZON_SELLER_LEGACY_CLIENT_ID_ENV
AMAZON_CLIENT_SECRET_ENV = AMAZON_SELLER_LEGACY_CLIENT_SECRET_ENV
AMAZON_REFRESH_TOKEN_ENV = AMAZON_SELLER_LEGACY_REFRESH_TOKEN_ENV
AMAZON_REGION_ENV = AMAZON_SELLER_LEGACY_REGION_ENV
AMAZON_SELLER_ID_ENV = AMAZON_SELLER_LEGACY_SELLER_ID_ENV
AMAZON_MARKETPLACE_IDS_ENV = AMAZON_SELLER_LEGACY_MARKETPLACE_IDS_ENV
AMAZON_API_CONTRACT = "finances-v2024-06-19"
AMAZON_MARKETPLACE_EVIDENCE_CONTRACT = "orders-v2026-01-01+fba-inventory-v1+finances-v2024-06-19"
HTTP_TRANSPORT = urllib_transport
HTTP_SLEEPER = time.sleep
_INLINE_SECRET_FIELDS = {
    "token", "access_token", "refresh_token", "api_key", "secret", "password",
    "authorization", "client_id", "client_secret", "seller_id", "endpoint", "url",
}
_RELATED_IDENTIFIER_NAMES = {
    "ORDER_ID", "SHIPMENT_ID", "FINANCIAL_EVENT_GROUP_ID", "REFUND_ID", "INVOICE_ID",
    "DISBURSEMENT_ID", "TRANSFER_ID", "DEFERRED_TRANSACTION_ID",
    "RELEASE_TRANSACTION_ID", "SETTLEMENT_ID",
}
_TRANSACTION_STATUSES = {"RELEASED", "DEFERRED", "DEFERRED_RELEASED"}
_FULFILLMENT_STATUSES = {
    "PENDING_AVAILABILITY", "PENDING", "UNSHIPPED", "PARTIALLY_SHIPPED",
    "SHIPPED", "CANCELLED", "UNFULFILLABLE",
}
_FULFILLED_BY = {"AMAZON", "MERCHANT"}


def _timestamp(value: Any, field: str, *, required: bool = False) -> tuple[str | None, datetime | None]:
    if value in (None, "") and not required:
        return None, None
    text = str(value or "")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or not re.fullmatch(r"[0-9T:+.Z-]+", text):
        raise ValueError(f"{field} must include a timezone and supported characters")
    normalized = parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    return normalized, parsed.astimezone(timezone.utc)


def _window(request: dict[str, Any]) -> tuple[str, datetime, str, datetime]:
    try:
        start_text, start = _timestamp(request.get("interval_start"), "interval_start", required=True)
        end_text, end = _timestamp(request.get("interval_end"), "interval_end", required=True)
    except ValueError as exc:
        raise ConnectorError(str(exc)) from exc
    assert start_text and start and end_text and end
    if start >= end:
        raise ConnectorError("interval_start must be earlier than interval_end")
    if end - start > timedelta(days=31):
        raise ConnectorError("Amazon Seller evidence window must not exceed 31 days")
    return start_text, start, end_text, end


def _canonical_month_period(start: datetime, end: datetime) -> str | None:
    month_start = start.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start != month_start:
        return None
    next_month = (
        month_start.replace(year=month_start.year + 1, month=1)
        if month_start.month == 12
        else month_start.replace(month=month_start.month + 1)
    )
    return month_start.strftime("%Y-%m") if end == next_month else None


def _bounded_id(value: Any, field: str, *, required: bool = False) -> str | None:
    if value in (None, "") and not required:
        return None
    text = str(value or "").strip()
    if not text or len(text) > 512 or any(ord(character) < 33 or ord(character) == 127 for character in text):
        raise ValueError(f"{field} requires a bounded source identifier")
    return text


def _hash_reference(binding: str, kind: str, value: Any, *, required: bool = False) -> str | None:
    raw = _bounded_id(value, kind, required=required)
    if raw is None:
        return None
    return hashlib.sha256(f"amazon-seller|{binding}|{kind}|{raw}".encode()).hexdigest()


def _code(value: Any, field: str, *, required: bool = False) -> str | None:
    if value in (None, "") and not required:
        return None
    text = str(value or "").strip()
    if not text or len(text) > 128 or any(ord(character) < 32 or ord(character) == 127 for character in text):
        raise ValueError(f"{field} is invalid")
    normalized = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_").upper()
    if not normalized:
        raise ValueError(f"{field} is invalid")
    return normalized[:128]


def _money(value: Any, field: str, *, required: bool = False) -> tuple[str | None, str | None]:
    if value in (None, "") and not required:
        return None, None
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be a currency object")
    currency = str(value.get("currencyCode") or "").upper()
    if not re.fullmatch(r"[A-Z]{3}", currency):
        raise ValueError(f"{field}.currencyCode must be a three-letter code")
    try:
        amount = Decimal(str(value.get("currencyAmount")))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field}.currencyAmount must be a finite decimal") from exc
    if not amount.is_finite():
        raise ValueError(f"{field}.currencyAmount must be a finite decimal")
    return format(amount, "f"), currency


def _quantity(value: Any, field: str, *, required: bool = False) -> int | None:
    if value in (None, "") and not required:
        return None
    if (
        not isinstance(value, int) or isinstance(value, bool)
        or value < 0 or value > 1_000_000_000
    ):
        raise ValueError(f"{field} must be a bounded non-negative integer")
    return value


def _object(value: Any, field: str, *, required: bool = False) -> dict[str, Any]:
    if value is None and not required:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    return value


def _seller_binding(
    entity_id: str, region: str, seller_id: str, marketplace_ids: list[str],
) -> str:
    return hashlib.sha256(
        f"amazon-seller|{entity_id}|{region}|{seller_id}|{','.join(sorted(marketplace_ids))}".encode()
    ).hexdigest()


def _flatten_breakdowns(value: Any, *, scope: str) -> list[dict[str, Any]]:
    if value in (None, ""):
        return []
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ValueError(f"{scope} breakdowns must be an object list")
    result: list[dict[str, Any]] = []

    def visit(items: list[dict[str, Any]], parents: tuple[str, ...], depth: int) -> None:
        if depth > 8:
            raise ValueError("Amazon Seller breakdown nesting exceeds eight levels")
        for item in items:
            component = _code(item.get("breakdownType"), "breakdownType", required=True)
            assert component is not None
            amount, currency = _money(item.get("breakdownAmount"), "breakdownAmount", required=True)
            assert amount is not None and currency is not None
            path = (*parents, component)
            result.append({
                "scope": scope,
                "path": "/".join(path),
                "depth": depth,
                "amount": amount,
                "currency": currency,
            })
            nested = item.get("breakdowns")
            if nested not in (None, []):
                if not isinstance(nested, list) or any(not isinstance(child, dict) for child in nested):
                    raise ValueError("nested breakdowns must be an object list")
                visit(nested, path, depth + 1)

    visit(value, (), 0)
    return result


def _normalize_transaction(
    raw: dict[str, Any], *, entity_id: str, batch_id: str, binding: str,
    expected_seller_id: str, marketplace_id: str, page: int, row: int,
    start: datetime, end: datetime,
) -> dict[str, Any]:
    metadata = raw.get("sellingPartnerMetadata")
    if not isinstance(metadata, dict):
        raise ValueError("sellingPartnerMetadata must be an object")
    observed_seller = _bounded_id(metadata.get("sellingPartnerId"), "sellingPartnerId", required=True)
    if observed_seller != expected_seller_id:
        raise ValueError("transaction seller does not match the configured seller binding")
    marketplace_details = raw.get("marketplaceDetails")
    if marketplace_details is not None and not isinstance(marketplace_details, dict):
        raise ValueError("marketplaceDetails must be an object")
    observed_marketplace = str(
        metadata.get("marketplaceId")
        or (marketplace_details or {}).get("marketplaceId")
        or ""
    )
    if observed_marketplace != marketplace_id:
        raise ValueError("transaction marketplace does not match the requested marketplace binding")
    transaction_key = _hash_reference(binding, "transaction", raw.get("transactionId"), required=True)
    assert transaction_key is not None
    posted_at, posted = _timestamp(raw.get("postedDate"), "postedDate", required=True)
    if posted is None or not start <= posted < end:
        raise ValueError("postedDate must fall inside the requested half-open interval")
    status = _code(raw.get("transactionStatus"), "transactionStatus", required=True)
    if status not in _TRANSACTION_STATUSES:
        raise ValueError("transactionStatus is outside the supported Finances API statuses")
    transaction_type = _code(raw.get("transactionType"), "transactionType", required=True)
    account_type = _code(metadata.get("accountType"), "accountType")
    total, currency = _money(raw.get("totalAmount"), "totalAmount", required=True)
    assert total is not None and currency is not None

    related = raw.get("relatedIdentifiers") or []
    if not isinstance(related, list) or any(not isinstance(item, dict) for item in related):
        raise ValueError("relatedIdentifiers must be an object list")
    related_keys = []
    for item in related:
        name = str(item.get("relatedIdentifierName") or "").upper()
        if name not in _RELATED_IDENTIFIER_NAMES:
            raise ValueError("relatedIdentifierName is outside the supported Finances API values")
        value_hash = _hash_reference(
            binding, name.lower(), item.get("relatedIdentifierValue"), required=True,
        )
        related_keys.append({"type": name, "key": value_hash})

    items = raw.get("items") or []
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise ValueError("items must be an object list")
    item_totals: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    components = _flatten_breakdowns(raw.get("breakdowns"), scope="transaction")
    for item in items:
        item_amount, item_currency = _money(item.get("totalAmount"), "item.totalAmount")
        if item_amount is not None and item_currency is not None:
            item_totals[item_currency] += Decimal(item_amount)
        components.extend(_flatten_breakdowns(item.get("breakdowns"), scope="item"))

    return {
        "amazon_transaction_key": transaction_key,
        "entity_id": entity_id,
        "marketplace_id": marketplace_id,
        "account_type": account_type,
        "transaction_type": transaction_type,
        "transaction_status": status,
        "posted_at": posted_at,
        "amount": total,
        "currency": currency,
        "related_keys": sorted(related_keys, key=lambda item: (item["type"], str(item["key"]))),
        "item_count": len(items),
        "item_totals": [
            {"currency": code, "amount": format(amount, "f")}
            for code, amount in sorted(item_totals.items())
        ],
        "financial_components": sorted(
            components,
            key=lambda item: (
                item["scope"], item["path"], item["currency"], item["amount"], item["depth"],
            ),
        ),
        "evidence": {
            "source_file": "api:amazon-seller",
            "source_sheet": "finances_transactions",
            "source_page": page,
            "source_row": row,
            "source_object_id": transaction_key,
            "batch_id": batch_id,
            "api_contract": AMAZON_API_CONTRACT,
        },
    }


def _normalize_order(
    raw: dict[str, Any], *, entity_id: str, batch_id: str, binding: str,
    marketplace_id: str, page: int, row: int, start: datetime, end: datetime,
    time_basis: str,
) -> dict[str, Any]:
    sales_channel = _object(raw.get("salesChannel"), "salesChannel", required=True)
    if sales_channel.get("marketplaceId") != marketplace_id:
        raise ValueError("order marketplace does not match the requested marketplace binding")
    order_key = _hash_reference(binding, "order_id", raw.get("orderId"), required=True)
    assert order_key is not None
    created_at, created = _timestamp(raw.get("createdTime"), "createdTime", required=True)
    updated_at, updated = _timestamp(raw.get("lastUpdatedTime"), "lastUpdatedTime", required=True)
    observed = created if time_basis == "created" else updated
    if observed is None or not start <= observed < end:
        raise ValueError(f"order {time_basis} time must fall inside the requested half-open interval")
    fulfillment = _object(raw.get("fulfillment"), "fulfillment", required=True)
    status = _code(fulfillment.get("fulfillmentStatus"), "fulfillmentStatus", required=True)
    fulfilled_by = _code(fulfillment.get("fulfilledBy"), "fulfilledBy", required=True)
    if status not in _FULFILLMENT_STATUSES:
        raise ValueError("fulfillmentStatus is outside the supported Orders API values")
    if fulfilled_by not in _FULFILLED_BY:
        raise ValueError("fulfilledBy is outside the supported Orders API values")
    raw_items = raw.get("orderItems")
    if (
        not isinstance(raw_items, list) or len(raw_items) > 1000
        or any(not isinstance(item, dict) for item in raw_items)
    ):
        raise ValueError("orderItems must be a bounded object list")
    items = []
    quantity_total = 0
    for index, item in enumerate(raw_items, 1):
        item_key = _hash_reference(
            binding, "order_item_id", item.get("orderItemId"), required=True,
        )
        quantity = _quantity(item.get("quantityOrdered"), "quantityOrdered", required=True)
        product = _object(item.get("product"), "product", required=True)
        sku_key = _hash_reference(binding, "seller_sku", product.get("sellerSku"))
        asin_key = _hash_reference(binding, "asin", product.get("asin"))
        if sku_key is None and asin_key is None:
            raise ValueError("order item requires sellerSku or asin for minimized product binding")
        condition = _object(product.get("condition"), "condition")
        assert item_key is not None and quantity is not None
        quantity_total += quantity
        items.append({
            "amazon_order_item_key": item_key,
            "amazon_sku_key": sku_key,
            "amazon_asin_key": asin_key,
            "quantity_ordered": quantity,
            "condition_type": _code(condition.get("conditionType"), "conditionType"),
            "source_item_position": index,
        })
    return {
        "amazon_order_key": order_key,
        "entity_id": entity_id,
        "marketplace_id": marketplace_id,
        "created_at": created_at,
        "last_updated_at": updated_at,
        "orders_time_basis": time_basis,
        "fulfillment_status": status,
        "fulfilled_by": fulfilled_by,
        "item_count": len(items),
        "quantity_ordered_total": quantity_total,
        "items": items,
        "evidence": {
            "source_file": "api:amazon-seller",
            "source_sheet": "orders_2026_search",
            "source_page": page,
            "source_row": row,
            "source_object_id": order_key,
            "batch_id": batch_id,
            "api_contract": "orders-v2026-01-01-searchOrders",
        },
    }


def _normalize_inventory(
    raw: dict[str, Any], *, entity_id: str, batch_id: str, binding: str,
    marketplace_id: str, page: int, row: int,
) -> dict[str, Any]:
    sku_key = _hash_reference(binding, "seller_sku", raw.get("sellerSku"), required=True)
    asin_key = _hash_reference(binding, "asin", raw.get("asin"))
    fnsku_key = _hash_reference(binding, "fnsku", raw.get("fnSku"))
    updated_at, _ = _timestamp(raw.get("lastUpdatedTime"), "lastUpdatedTime", required=True)
    total = _quantity(raw.get("totalQuantity"), "totalQuantity", required=True)
    details = _object(raw.get("inventoryDetails"), "inventoryDetails", required=True)
    reserved = _object(details.get("reservedQuantity"), "reservedQuantity")
    researching = _object(details.get("researchingQuantity"), "researchingQuantity")
    unfulfillable = _object(details.get("unfulfillableQuantity"), "unfulfillableQuantity")
    assert sku_key is not None and total is not None
    quantity_sources = {
        "fulfillable_quantity": (details, "fulfillableQuantity"),
        "inbound_working_quantity": (details, "inboundWorkingQuantity"),
        "inbound_shipped_quantity": (details, "inboundShippedQuantity"),
        "inbound_receiving_quantity": (details, "inboundReceivingQuantity"),
        "reserved_quantity": (reserved, "totalReservedQuantity"),
        "pending_customer_order_quantity": (reserved, "pendingCustomerOrderQuantity"),
        "researching_quantity": (researching, "totalResearchingQuantity"),
        "unfulfillable_quantity": (unfulfillable, "totalUnfulfillableQuantity"),
    }
    quantity_fields_present = [
        output_field
        for output_field, (source, source_field) in quantity_sources.items()
        if source.get(source_field) not in (None, "")
    ]
    optional_quantities = {
        output_field: (
            _quantity(source.get(source_field), source_field)
            if source.get(source_field) not in (None, "")
            else 0
        )
        for output_field, (source, source_field) in quantity_sources.items()
    }
    quantities = {
        "total_quantity": total,
        **optional_quantities,
    }
    return {
        "amazon_sku_key": sku_key,
        "amazon_asin_key": asin_key,
        "amazon_fnsku_key": fnsku_key,
        "entity_id": entity_id,
        "marketplace_id": marketplace_id,
        "condition": _code(raw.get("condition"), "condition"),
        "observed_updated_at": updated_at,
        "quantity_fields_present": sorted(quantity_fields_present),
        **quantities,
        "evidence": {
            "source_file": "api:amazon-seller",
            "source_sheet": "fba_inventory_current_snapshot",
            "source_page": page,
            "source_row": row,
            "source_object_id": sku_key,
            "batch_id": batch_id,
            "api_contract": "fba-inventory-v1-getInventorySummaries",
        },
    }


def _marketplace_ids_from_environment() -> list[str]:
    raw = os.environ.get(AMAZON_MARKETPLACE_IDS_ENV, "")
    if not raw or len(raw) > 8192:
        raise ConnectorError(f"{AMAZON_MARKETPLACE_IDS_ENV} must be a bounded JSON string list")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConnectorError(f"{AMAZON_MARKETPLACE_IDS_ENV} must be a JSON string list") from exc
    if (
        not isinstance(value, list) or not value or len(value) > 100
        or any(not isinstance(item, str) or not re.fullmatch(r"[A-Z0-9]{6,32}", item) for item in value)
        or len(value) != len(set(value))
    ):
        raise ConnectorError(f"{AMAZON_MARKETPLACE_IDS_ENV} must contain unique marketplace IDs")
    return sorted(value)


def _handler(request: dict[str, Any], context: ConnectorContext) -> dict[str, Any]:
    if any(str(key).lower() in _INLINE_SECRET_FIELDS for key in request):
        raise ConnectorError("Amazon Seller credentials and seller ID must not be passed in connector requests")
    entity_id = request.get("default_entity_id")
    if entity_id not in context.allowed_entity_ids:
        raise ConnectorError("Amazon Seller connector requires a valid default_entity_id")
    start_text, start, end_text, end = _window(request)
    marketplace_id = str(request.get("marketplace_id") or "")
    if not re.fullmatch(r"[A-Z0-9]{6,32}", marketplace_id):
        raise ConnectorError("Amazon Seller connector requires a valid marketplace_id")
    transaction_status = request.get("transaction_status")
    if transaction_status is not None:
        transaction_status = str(transaction_status).upper()
        if transaction_status not in _TRANSACTION_STATUSES:
            raise ConnectorError("transaction_status is outside the supported Finances API statuses")
    mode = request.get("mode", "fixture")
    if mode == "fixture":
        pages = request.get("transaction_pages")
        try:
            expected_seller_id = _bounded_id(
                request.get("fixture_seller_id"), "fixture_seller_id", required=True,
            )
        except ValueError as exc:
            raise ConnectorError(str(exc)) from exc
        if (
            not isinstance(pages, list) or len(pages) > 20
            or any(not isinstance(page, dict) for page in pages)
        ):
            raise ConnectorError("Amazon Seller fixture mode requires transaction_pages and fixture_seller_id")
        allowed_marketplaces = request.get("fixture_marketplace_ids") or [marketplace_id]
        if (
            not isinstance(allowed_marketplaces, list) or len(allowed_marketplaces) > 100
            or any(
                not isinstance(item, str) or not re.fullmatch(r"[A-Z0-9]{6,32}", item)
                for item in allowed_marketplaces
            )
            or len(allowed_marketplaces) != len(set(allowed_marketplaces))
            or marketplace_id not in allowed_marketplaces
        ):
            raise ConnectorError("fixture marketplace is outside the fixture seller binding")
        region = str(request.get("fixture_region") or "NA").upper()
        if region not in {"NA", "EU", "FE"}:
            raise ConnectorError("fixture_region must be NA, EU or FE")
        canonical = json.dumps({
            "entity_id": entity_id, "marketplace_id": marketplace_id,
            "interval_start": start_text, "interval_end": end_text, "pages": pages,
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        batch_id = hashlib.sha256(f"amazon-seller|fixture|{canonical}".encode()).hexdigest()[:24]
        environment = "fixture"
        network_access = False
        fetch_metadata: dict[str, Any] = {
            "page_count": len(pages), "network_access_performed": False,
            "lwa_token_exchange_performed": False, "retry_count": 0,
        }
    elif mode == "fetch":
        requested_environment = str(
            request.get("environment") or "production"
        ).lower()
        try:
            credentials = resolve_amazon_seller_entity_credentials(
                str(entity_id),
                os.environ,
                legacy_environment=requested_environment,
                require_entity_binding=len(context.allowed_entity_ids) > 1,
            )
        except ConnectorEntityCredentialError as exc:
            raise ConnectorError(str(exc)) from exc
        if not credentials["configured"]:
            raise ConnectorError("Amazon Seller credential binding is missing or invalid")
        allowed_marketplaces = credentials["marketplace_ids"]
        if marketplace_id not in allowed_marketplaces:
            raise ConnectorError("requested marketplace is outside the configured seller binding")
        expected_seller_id = credentials["seller_id"]
        region = credentials["region"]
        environment = credentials["environment"]
        if end > datetime.now(timezone.utc) - timedelta(minutes=2):
            raise ConnectorError("Amazon Seller fetch interval_end must be at least two minutes before the request")
        max_pages = request.get("max_pages", 20)
        if not isinstance(max_pages, int) or isinstance(max_pages, bool):
            raise ConnectorError("max_pages must be an integer")
        try:
            fetched = fetch_amazon_seller_transaction_pages(
                client_id=credentials["client_id"],
                client_secret=credentials["client_secret"],
                refresh_token=credentials["refresh_token"],
                region=region, environment=environment, marketplace_id=marketplace_id,
                posted_after=start_text, posted_before=end_text,
                transaction_status=transaction_status, max_pages=max_pages,
                transport=HTTP_TRANSPORT, sleeper=HTTP_SLEEPER,
            )
        except Exception as exc:
            if isinstance(exc, ConnectorError):
                raise
            raise ConnectorError(str(exc)) from exc
        pages = fetched["pages"]
        batch_id = fetched["batch_id"]
        network_access = True
        fetch_metadata = {
            key: fetched[key] for key in (
                "page_count", "total_items", "network_access_performed",
                "lwa_token_exchange_performed", "retry_count", "rate_limit_count",
                "retry_delay_seconds_total", "retry_after_honored", "response_links_followed",
            )
        }
        fetch_metadata["entity_credential_binding_used"] = credentials[
            "entity_binding_used"
        ]
    else:
        raise ConnectorError("Amazon Seller connector mode must be fixture or fetch")

    seller_binding = _seller_binding(
        str(entity_id), region, expected_seller_id, list(allowed_marketplaces),
    )
    records: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for page_number, page in enumerate(pages, 1):
        payload = page.get("payload")
        rows = payload.get("transactions") if isinstance(payload, dict) else None
        if (
            not isinstance(rows, list) or len(rows) > 500
            or any(not isinstance(item, dict) for item in rows)
        ):
            rejected.append({
                "dataset_type": "commerce.amazon_seller_transactions", "page": page_number,
                "row": 0, "reason": "payload.transactions must be an object list",
            })
            continue
        for row_number, raw in enumerate(rows, 1):
            try:
                records.append(_normalize_transaction(
                    raw, entity_id=str(entity_id), batch_id=batch_id, binding=seller_binding,
                    expected_seller_id=expected_seller_id, marketplace_id=marketplace_id,
                    page=page_number, row=row_number, start=start, end=end,
                ))
            except (TypeError, ValueError, InvalidOperation) as exc:
                rejected.append({
                    "dataset_type": "commerce.amazon_seller_transactions", "page": page_number,
                    "row": row_number, "reason": str(exc),
                })
    source = {
        "kind": "api" if network_access else "fixture",
        "name": "amazon_seller.transaction_activity",
        "api_contract": AMAZON_API_CONTRACT,
        "region": region,
        "environment": environment,
        "marketplace_id": marketplace_id,
        "seller_binding_sha256": seller_binding,
        "interval_start": start_text,
        "interval_end": end_text,
        "transaction_status_filter": transaction_status,
        **fetch_metadata,
        "finance_and_accounting_role_required": True,
        "lwa_token_persisted": False,
        "aws_sigv4_used": False,
        "fixed_regional_endpoint_used": True,
        "response_urls_followed": False,
        "customer_or_address_retained": False,
        "product_identity_or_description_retained": False,
        "store_name_or_free_text_retained": False,
        "raw_seller_or_business_ids_retained": False,
        "business_write_api_called": False,
    }
    return {
        "batch_id": batch_id,
        "source": source,
        "datasets": {"commerce.amazon_seller_transactions": records},
        "rejected_rows": rejected,
    }


def _marketplace_evidence_handler(
    request: dict[str, Any], context: ConnectorContext,
) -> dict[str, Any]:
    if any(str(key).lower() in _INLINE_SECRET_FIELDS for key in request):
        raise ConnectorError("Amazon Seller credentials and seller ID must not be passed in connector requests")
    entity_id = request.get("default_entity_id")
    if entity_id not in context.allowed_entity_ids:
        raise ConnectorError("Amazon Seller marketplace evidence requires a valid default_entity_id")
    start_text, start, end_text, end = _window(request)
    canonical_month_period = _canonical_month_period(start, end)
    marketplace_id = str(request.get("marketplace_id") or "")
    if not re.fullmatch(r"[A-Z0-9]{6,32}", marketplace_id):
        raise ConnectorError("Amazon Seller marketplace evidence requires a valid marketplace_id")
    orders_time_basis = str(request.get("orders_time_basis") or "created").lower()
    if orders_time_basis not in {"created", "updated"}:
        raise ConnectorError("orders_time_basis must be created or updated")
    transaction_status = request.get("transaction_status")
    if transaction_status is not None:
        transaction_status = str(transaction_status).upper()
        if transaction_status not in _TRANSACTION_STATUSES:
            raise ConnectorError("transaction_status is outside the supported Finances API statuses")
    mode = request.get("mode", "fixture")
    if mode == "fixture":
        order_pages = request.get("order_pages")
        inventory_pages = request.get("inventory_pages")
        transaction_pages = request.get("transaction_pages")
        if any(
            not isinstance(pages, list) or not pages or len(pages) > 20
            or any(not isinstance(page, dict) for page in pages)
            for pages in (order_pages, inventory_pages, transaction_pages)
        ):
            raise ConnectorError(
                "Amazon Seller marketplace fixture requires bounded order, inventory and transaction pages"
            )
        try:
            expected_seller_id = _bounded_id(
                request.get("fixture_seller_id"), "fixture_seller_id", required=True,
            )
            inventory_observed_at, _ = _timestamp(
                request.get("fixture_inventory_observed_at"),
                "fixture_inventory_observed_at", required=True,
            )
        except ValueError as exc:
            raise ConnectorError(str(exc)) from exc
        allowed_marketplaces = request.get("fixture_marketplace_ids") or [marketplace_id]
        if (
            not isinstance(allowed_marketplaces, list) or len(allowed_marketplaces) > 100
            or any(
                not isinstance(item, str) or not re.fullmatch(r"[A-Z0-9]{6,32}", item)
                for item in allowed_marketplaces
            )
            or len(allowed_marketplaces) != len(set(allowed_marketplaces))
            or marketplace_id not in allowed_marketplaces
        ):
            raise ConnectorError("fixture marketplace is outside the fixture seller binding")
        region = str(request.get("fixture_region") or "NA").upper()
        if region not in {"NA", "EU", "FE"}:
            raise ConnectorError("fixture_region must be NA, EU or FE")
        canonical = json.dumps({
            "entity_id": entity_id, "marketplace_id": marketplace_id,
            "interval_start": start_text, "interval_end": end_text,
            "orders_time_basis": orders_time_basis,
            "order_pages": order_pages, "inventory_pages": inventory_pages,
            "transaction_pages": transaction_pages,
            "inventory_observed_at": inventory_observed_at,
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        batch_id = hashlib.sha256(
            f"amazon-seller|marketplace-fixture|{canonical}".encode()
        ).hexdigest()[:24]
        environment = "fixture"
        network_access = False
        fetch_metadata: dict[str, Any] = {
            "order_page_count": len(order_pages),
            "inventory_page_count": len(inventory_pages),
            "transaction_page_count": len(transaction_pages),
            "network_access_performed": False,
            "lwa_token_exchange_performed": False,
            "lwa_token_exchange_count": 0,
            "retry_count": 0,
        }
    elif mode == "fetch":
        requested_environment = str(
            request.get("environment") or "production"
        ).lower()
        try:
            credentials = resolve_amazon_seller_entity_credentials(
                str(entity_id),
                os.environ,
                legacy_environment=requested_environment,
                require_entity_binding=len(context.allowed_entity_ids) > 1,
            )
        except ConnectorEntityCredentialError as exc:
            raise ConnectorError(str(exc)) from exc
        if not credentials["configured"]:
            raise ConnectorError("Amazon Seller credential binding is missing or invalid")
        allowed_marketplaces = credentials["marketplace_ids"]
        if marketplace_id not in allowed_marketplaces:
            raise ConnectorError("requested marketplace is outside the configured seller binding")
        expected_seller_id = credentials["seller_id"]
        region = credentials["region"]
        environment = credentials["environment"]
        if end > datetime.now(timezone.utc) - timedelta(minutes=2):
            raise ConnectorError("Amazon Seller fetch interval_end must be at least two minutes before the request")
        page_limits = {
            "max_order_pages": request.get("max_order_pages", 20),
            "max_inventory_pages": request.get("max_inventory_pages", 20),
            "max_transaction_pages": request.get("max_transaction_pages", 20),
        }
        if any(
            not isinstance(value, int) or isinstance(value, bool)
            for value in page_limits.values()
        ):
            raise ConnectorError("Amazon Seller page limits must be integers")
        try:
            fetched = fetch_amazon_seller_marketplace_evidence_pages(
                client_id=credentials["client_id"],
                client_secret=credentials["client_secret"],
                refresh_token=credentials["refresh_token"],
                region=region, environment=environment, marketplace_id=marketplace_id,
                interval_start=start_text, interval_end=end_text,
                orders_time_basis=orders_time_basis,
                transaction_status=transaction_status,
                **page_limits, transport=HTTP_TRANSPORT, sleeper=HTTP_SLEEPER,
            )
        except Exception as exc:
            if isinstance(exc, ConnectorError):
                raise
            raise ConnectorError(str(exc)) from exc
        order_pages = fetched["order_pages"]
        inventory_pages = fetched["inventory_pages"]
        transaction_pages = fetched["transaction_pages"]
        inventory_observed_at = datetime.now(timezone.utc).isoformat(
            timespec="microseconds"
        ).replace("+00:00", "Z")
        batch_id = fetched["batch_id"]
        network_access = True
        fetch_metadata = {
            key: fetched[key] for key in (
                "order_page_count", "inventory_page_count", "transaction_page_count",
                "order_count", "inventory_count", "transaction_count", "retry_count",
                "rate_limit_count", "retry_delay_seconds_total", "retry_after_honored",
                "network_access_performed", "lwa_token_exchange_performed",
                "lwa_token_exchange_count", "response_links_followed",
            )
        }
        fetch_metadata["entity_credential_binding_used"] = credentials[
            "entity_binding_used"
        ]
    else:
        raise ConnectorError("Amazon Seller marketplace evidence mode must be fixture or fetch")

    assert expected_seller_id is not None and inventory_observed_at is not None
    seller_binding = _seller_binding(
        str(entity_id), region, expected_seller_id, list(allowed_marketplaces),
    )
    orders: list[dict[str, Any]] = []
    inventory: list[dict[str, Any]] = []
    transactions: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []

    for page_number, page in enumerate(order_pages, 1):
        rows = page.get("orders")
        if (
            not isinstance(rows, list) or len(rows) > 100
            or any(not isinstance(item, dict) for item in rows)
        ):
            rejected.append({
                "dataset_type": "commerce.amazon_seller_orders", "page": page_number,
                "row": 0, "reason": "orders must be a bounded object list",
            })
            continue
        for row_number, raw in enumerate(rows, 1):
            try:
                orders.append(_normalize_order(
                    raw, entity_id=str(entity_id), batch_id=batch_id, binding=seller_binding,
                    marketplace_id=marketplace_id, page=page_number, row=row_number,
                    start=start, end=end, time_basis=orders_time_basis,
                ))
            except (TypeError, ValueError, InvalidOperation) as exc:
                rejected.append({
                    "dataset_type": "commerce.amazon_seller_orders", "page": page_number,
                    "row": row_number, "reason": str(exc),
                })

    for page_number, page in enumerate(inventory_pages, 1):
        body = page.get("payload")
        granularity = body.get("granularity") if isinstance(body, dict) else None
        rows = body.get("inventorySummaries") if isinstance(body, dict) else None
        if (
            not isinstance(granularity, dict)
            or granularity.get("granularityType") != "Marketplace"
            or granularity.get("granularityId") != marketplace_id
            or not isinstance(rows, list) or len(rows) > 1000
            or any(not isinstance(item, dict) for item in rows)
        ):
            rejected.append({
                "dataset_type": "commerce.amazon_seller_inventory", "page": page_number,
                "row": 0, "reason": "inventory payload violates the Marketplace granularity contract",
            })
            continue
        for row_number, raw in enumerate(rows, 1):
            try:
                inventory.append(_normalize_inventory(
                    raw, entity_id=str(entity_id), batch_id=batch_id, binding=seller_binding,
                    marketplace_id=marketplace_id, page=page_number, row=row_number,
                ))
            except (TypeError, ValueError, InvalidOperation) as exc:
                rejected.append({
                    "dataset_type": "commerce.amazon_seller_inventory", "page": page_number,
                    "row": row_number, "reason": str(exc),
                })

    for page_number, page in enumerate(transaction_pages, 1):
        payload = page.get("payload")
        rows = payload.get("transactions") if isinstance(payload, dict) else None
        if (
            not isinstance(rows, list) or len(rows) > 500
            or any(not isinstance(item, dict) for item in rows)
        ):
            rejected.append({
                "dataset_type": "commerce.amazon_seller_transactions", "page": page_number,
                "row": 0, "reason": "payload.transactions must be a bounded object list",
            })
            continue
        for row_number, raw in enumerate(rows, 1):
            try:
                transactions.append(_normalize_transaction(
                    raw, entity_id=str(entity_id), batch_id=batch_id, binding=seller_binding,
                    expected_seller_id=expected_seller_id, marketplace_id=marketplace_id,
                    page=page_number, row=row_number, start=start, end=end,
                ))
            except (TypeError, ValueError, InvalidOperation) as exc:
                rejected.append({
                    "dataset_type": "commerce.amazon_seller_transactions", "page": page_number,
                    "row": row_number, "reason": str(exc),
                })

    source = {
        "kind": "api" if network_access else "fixture",
        "name": "amazon_seller.marketplace_evidence",
        "api_contract": AMAZON_MARKETPLACE_EVIDENCE_CONTRACT,
        "region": region,
        "environment": environment,
        "marketplace_id": marketplace_id,
        "seller_binding_sha256": seller_binding,
        "interval_start": start_text,
        "interval_end": end_text,
        "canonical_month_period": canonical_month_period,
        "canonical_month_scope": canonical_month_period is not None,
        "interval_semantics": "half_open_utc",
        "orders_time_basis": orders_time_basis,
        "orders_included_data": ["FULFILLMENT"],
        "inventory_observed_at": inventory_observed_at,
        "inventory_observation_type": "current_at_fetch_not_historical_period_end",
        "transaction_status_filter": transaction_status,
        **fetch_metadata,
        "orders_role_required": "Finance and Accounting or Inventory and Order Tracking",
        "inventory_role_required": "Amazon Fulfillment or Product Listing",
        "lwa_token_persisted": False,
        "aws_sigv4_used": False,
        "fixed_regional_endpoint_used": True,
        "response_urls_followed": False,
        "buyer_recipient_or_address_retained": False,
        "product_title_or_raw_identity_retained": False,
        "proceeds_expense_tax_payment_or_tracking_requested": False,
        "raw_seller_or_business_ids_retained": False,
        "business_write_api_called": False,
        "inventory_adjustment_performed": False,
    }
    return {
        "batch_id": batch_id,
        "source": source,
        "datasets": {
            "commerce.amazon_seller_orders": orders,
            "commerce.amazon_seller_inventory": inventory,
            "commerce.amazon_seller_transactions": transactions,
        },
        "rejected_rows": rejected,
    }


def register_connectors(registry: ConnectorRegistry) -> None:
    registry.register(ConnectorDefinition(
        connector_id="amazon_seller.transaction_activity",
        pack_id="connector.amazon_seller",
        capability="connector.amazon_seller_transaction_activity",
        display_name="Amazon Seller Finances 交易与费用证据（只读）",
        dataset_types=("commerce.amazon_seller_transactions",),
        handler=_handler,
        business_keys={"commerce.amazon_seller_transactions": ("amazon_transaction_key",)},
        credential_env=(
            AMAZON_CLIENT_ID_ENV, AMAZON_CLIENT_SECRET_ENV, AMAZON_REFRESH_TOKEN_ENV,
            AMAZON_REGION_ENV, AMAZON_SELLER_ID_ENV, AMAZON_MARKETPLACE_IDS_ENV,
        ),
        network_access=True,
        sync_window=ConnectorSyncWindow(
            start_field="interval_start", end_field="interval_end",
            value_format="iso8601", max_incremental_days=31, max_backfill_days=180,
        ),
    ))
    registry.register(ConnectorDefinition(
        connector_id="amazon_seller.marketplace_evidence",
        pack_id="connector.amazon_seller",
        capability="connector.amazon_seller_marketplace_evidence",
        display_name="Amazon Seller Orders、FBA Inventory 与 Finances 三源证据（只读）",
        dataset_types=(
            "commerce.amazon_seller_orders",
            "commerce.amazon_seller_inventory",
            "commerce.amazon_seller_transactions",
        ),
        handler=_marketplace_evidence_handler,
        business_keys={
            "commerce.amazon_seller_orders": ("amazon_order_key",),
            "commerce.amazon_seller_inventory": ("amazon_sku_key",),
            "commerce.amazon_seller_transactions": ("amazon_transaction_key",),
        },
        credential_env=(
            AMAZON_CLIENT_ID_ENV, AMAZON_CLIENT_SECRET_ENV, AMAZON_REFRESH_TOKEN_ENV,
            AMAZON_REGION_ENV, AMAZON_SELLER_ID_ENV, AMAZON_MARKETPLACE_IDS_ENV,
        ),
        network_access=True,
        sync_window=ConnectorSyncWindow(
            start_field="interval_start", end_field="interval_end",
            value_format="iso8601", max_incremental_days=31, max_backfill_days=180,
        ),
    ))
