from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from src.connector_http import fetch_shopify_graphql_orders, urllib_transport
from src.connector_sdk import (
    ConnectorContext, ConnectorDefinition, ConnectorError, ConnectorRegistry,
    ConnectorSyncWindow,
)


SHOPIFY_API_VERSION = "2026-07"
SHOPIFY_TOKEN_ENV = "OPC_SHOPIFY_ADMIN_TOKEN"
HTTP_TRANSPORT = urllib_transport
HTTP_SLEEPER = time.sleep
_INLINE_SECRET_FIELDS = {"token", "api_key", "secret", "password", "authorization", "access_token"}
_SHOP_DOMAIN = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.myshopify\.com")

_ORDERS_QUERY_TEMPLATE = """
query FinanceOrders($first: Int!, $after: String, $query: String!) {
  orders(first: $first, after: $after, query: $query, sortKey: __SORT_KEY__) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id name createdAt processedAt updatedAt cancelledAt closedAt
      currencyCode presentmentCurrencyCode
      displayFinancialStatus displayFulfillmentStatus
      taxesIncluded test sourceName
      shippingAddress { countryCodeV2 }
      subtotalPriceSet { shopMoney { amount currencyCode } presentmentMoney { amount currencyCode } }
      totalDiscountsSet { shopMoney { amount currencyCode } presentmentMoney { amount currencyCode } }
      totalShippingPriceSet { shopMoney { amount currencyCode } presentmentMoney { amount currencyCode } }
      totalTaxSet { shopMoney { amount currencyCode } presentmentMoney { amount currencyCode } }
      totalPriceSet { shopMoney { amount currencyCode } presentmentMoney { amount currencyCode } }
      totalReceivedSet { shopMoney { amount currencyCode } presentmentMoney { amount currencyCode } }
      totalRefundedSet { shopMoney { amount currencyCode } presentmentMoney { amount currencyCode } }
      currentSubtotalPriceSet { shopMoney { amount currencyCode } presentmentMoney { amount currencyCode } }
      currentTotalDiscountsSet { shopMoney { amount currencyCode } presentmentMoney { amount currencyCode } }
      currentTotalTaxSet { shopMoney { amount currencyCode } presentmentMoney { amount currencyCode } }
      currentTotalPriceSet { shopMoney { amount currencyCode } presentmentMoney { amount currencyCode } }
      transactions {
        id createdAt processedAt gateway formattedGateway kind status test
        settlementCurrency settlementCurrencyRate
        parentTransaction { id }
        amountSet { shopMoney { amount currencyCode } presentmentMoney { amount currencyCode } }
      }
      refunds {
        id createdAt processedAt updatedAt
        totalRefundedSet { shopMoney { amount currencyCode } presentmentMoney { amount currencyCode } }
        refundLineItems(first: 100) {
          pageInfo { hasNextPage endCursor }
          nodes {
            id quantity
            subtotalSet { shopMoney { amount currencyCode } presentmentMoney { amount currencyCode } }
            totalTaxSet { shopMoney { amount currencyCode } presentmentMoney { amount currencyCode } }
          }
        }
        refundShippingLines(first: 100) {
          pageInfo { hasNextPage endCursor }
          nodes {
            subtotalAmountSet { shopMoney { amount currencyCode } presentmentMoney { amount currencyCode } }
            taxAmountSet { shopMoney { amount currencyCode } presentmentMoney { amount currencyCode } }
          }
        }
        orderAdjustments(first: 100) {
          pageInfo { hasNextPage endCursor }
          nodes {
            id reason
            amountSet { shopMoney { amount currencyCode } presentmentMoney { amount currencyCode } }
            taxAmountSet { shopMoney { amount currencyCode } presentmentMoney { amount currencyCode } }
          }
        }
        transactions(first: 100) {
          pageInfo { hasNextPage endCursor }
          nodes {
            id createdAt processedAt kind status
            amountSet { shopMoney { amount currencyCode } presentmentMoney { amount currencyCode } }
          }
        }
      }
    }
  }
}
""".strip()
ORDERS_QUERY = _ORDERS_QUERY_TEMPLATE.replace("__SORT_KEY__", "CREATED_AT")
UPDATED_ORDERS_QUERY = _ORDERS_QUERY_TEMPLATE.replace("__SORT_KEY__", "UPDATED_AT")


def _money(value: Any, field: str) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be a MoneyV2 object")
    try:
        amount = Decimal(str(value.get("amount")))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field}.amount must be a finite decimal string") from exc
    if not amount.is_finite():
        raise ValueError(f"{field}.amount must be a finite decimal string")
    currency = str(value.get("currencyCode") or "").upper()
    if not re.fullmatch(r"[A-Z]{3}", currency):
        raise ValueError(f"{field}.currencyCode must be a three-letter code")
    return {"amount": format(amount, "f"), "currency": currency}


def _money_bag(value: Any, field: str) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be a MoneyBag object")
    return {
        "shop_money": _money(value.get("shopMoney"), f"{field}.shopMoney"),
        "presentment_money": _money(value.get("presentmentMoney"), f"{field}.presentmentMoney"),
    }


def _source_id(value: Any) -> str | None:
    if isinstance(value, str):
        return value or None
    if isinstance(value, dict) and value.get("id"):
        return str(value["id"])
    return None


def _evidence(
    batch_id: str, shop_domain: str, dataset: str, object_id: str, page: int, row: int,
) -> dict[str, Any]:
    return {
        "source_file": "api:shopify",
        "source_sheet": dataset,
        "source_page": page,
        "source_row": row,
        "source_object_id": object_id,
        "batch_id": batch_id,
        "api_version": SHOPIFY_API_VERSION,
        "shop_domain": shop_domain,
    }


def _date_filter(value: Any, field: str) -> str:
    text = str(value or "")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ConnectorError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ConnectorError(f"{field} must include a timezone offset")
    if not re.fullmatch(r"[0-9T:+.Z-]+", text):
        raise ConnectorError(f"{field} contains unsupported characters")
    return text


def _search_query(request: dict[str, Any]) -> str:
    filters = ["status:any"]
    parsed: dict[str, datetime] = {}
    if "created_at_gte" in request:
        value = _date_filter(request["created_at_gte"], "created_at_gte")
        parsed["gte"] = datetime.fromisoformat(value.replace("Z", "+00:00"))
        filters.append(f"created_at:>={value}")
    if "created_at_lt" in request:
        value = _date_filter(request["created_at_lt"], "created_at_lt")
        parsed["lt"] = datetime.fromisoformat(value.replace("Z", "+00:00"))
        filters.append(f"created_at:<{value}")
    if parsed.get("gte") and parsed.get("lt") and parsed["gte"] >= parsed["lt"]:
        raise ConnectorError("created_at_gte must be earlier than created_at_lt")
    return " ".join(filters)


def _updated_search_query(interval_start: str, observed_at: str) -> str:
    return f"status:any updated_at:>={interval_start} updated_at:<{observed_at}"


def _utc_timestamp(value: Any, field: str) -> datetime:
    text = _date_filter(value, field)
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return parsed.astimezone(timezone.utc)


def _canonical_month_window(request: dict[str, Any]) -> tuple[str, str, str, datetime, datetime]:
    start = _utc_timestamp(request.get("interval_start"), "interval_start")
    end = _utc_timestamp(request.get("interval_end"), "interval_end")
    if start.second or start.microsecond or start.minute or start.hour:
        raise ConnectorError("interval_start must be midnight UTC on the first day of a month")
    if start.day != 1 or start.utcoffset() != timedelta(0):
        raise ConnectorError("interval_start must be midnight UTC on the first day of a month")
    if end.second or end.microsecond or end.minute or end.hour or end.day != 1:
        raise ConnectorError("interval_end must be midnight UTC on the first day of the next month")
    expected_end = (
        start.replace(year=start.year + 1, month=1)
        if start.month == 12 else start.replace(month=start.month + 1)
    )
    if end != expected_end:
        raise ConnectorError("interval_end must be the next calendar month boundary")
    return start.strftime("%Y-%m"), start.isoformat().replace("+00:00", "Z"), end.isoformat().replace("+00:00", "Z"), start, end


def _connection_nodes(value: Any, field: str) -> tuple[list[dict[str, Any]], bool]:
    if value is None:
        return [], False
    if not isinstance(value, dict) or not isinstance(value.get("nodes"), list):
        raise ValueError(f"{field} must be a GraphQL connection")
    nodes = value["nodes"]
    if any(not isinstance(node, dict) for node in nodes):
        raise ValueError(f"{field}.nodes must contain objects")
    page_info = value.get("pageInfo")
    if not isinstance(page_info, dict) or not isinstance(page_info.get("hasNextPage"), bool):
        raise ValueError(f"{field}.pageInfo must expose hasNextPage")
    if page_info["hasNextPage"]:
        raise ValueError(f"{field} exceeds the supported nested page and is incomplete")
    return list(nodes), True


def _handler(request: dict[str, Any], context: ConnectorContext) -> dict[str, Any]:
    if any(str(key).lower() in _INLINE_SECRET_FIELDS for key in request):
        raise ConnectorError("Shopify credentials must not be passed in connector requests")
    entity_id = request.get("default_entity_id")
    if entity_id not in context.allowed_entity_ids:
        raise ConnectorError("Shopify connector requires a valid default_entity_id")
    shop_domain = str(request.get("shop_domain") or "").lower()
    mode = request.get("mode", "fixture")
    if mode == "fixture":
        objects = request.get("objects")
        if not isinstance(objects, list):
            raise ConnectorError("Shopify fixture mode requires an objects list")
        if not shop_domain:
            shop_domain = "fixture.myshopify.com"
        if not _SHOP_DOMAIN.fullmatch(shop_domain):
            raise ConnectorError("Shopify shop_domain must be one store under myshopify.com")
        indexed = [(1, index, raw) for index, raw in enumerate(objects, 1)]
        source = {
            "kind": "fixture", "name": "shopify.orders", "shop_domain": shop_domain,
            "network_access_performed": False, "api_version": SHOPIFY_API_VERSION,
            "page_count": 1, "retry_count": 0,
        }
    elif mode == "fetch":
        max_pages = request.get("max_pages", 50)
        if not isinstance(max_pages, int) or isinstance(max_pages, bool) or not 1 <= max_pages <= 100:
            raise ConnectorError("max_pages must be an integer from 1 to 100")
        starting_after = request.get("starting_after")
        if starting_after is not None and (
            not isinstance(starting_after, str) or not starting_after or len(starting_after) > 2048
        ):
            raise ConnectorError("starting_after must be a bounded non-empty cursor")
        try:
            fetched = fetch_shopify_graphql_orders(
                shop_domain,
                access_token=os.environ.get(SHOPIFY_TOKEN_ENV, ""),
                api_version=SHOPIFY_API_VERSION,
                query=ORDERS_QUERY,
                search_query=_search_query(request),
                start_cursor=starting_after,
                max_pages=max_pages,
                transport=HTTP_TRANSPORT,
                sleeper=HTTP_SLEEPER,
            )
        except Exception as exc:
            if isinstance(exc, ConnectorError):
                raise
            raise ConnectorError(str(exc)) from exc
        indexed = [
            (page_number, row_number, raw)
            for page_number, page in enumerate(fetched["pages"], 1)
            for row_number, raw in enumerate(page["data"]["orders"]["nodes"], 1)
        ]
        objects = [raw for _, _, raw in indexed]
        source = {
            "kind": "api", "name": "shopify.orders", "shop_domain": fetched["shop_domain"],
            "network_access_performed": True, "api_version": SHOPIFY_API_VERSION,
            "page_count": fetched["page_count"], "retry_count": fetched["retry_count"],
            "rate_limit_count": fetched["rate_limit_count"],
            "retry_delay_seconds_total": fetched["retry_delay_seconds_total"],
            "retry_after_honored": fetched["retry_after_honored"],
        }
    else:
        raise ConnectorError("Shopify connector mode must be fixture or fetch")

    canonical = json.dumps({
        "api_version": SHOPIFY_API_VERSION, "entity_id": entity_id,
        "shop_domain": shop_domain, "orders": objects,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    batch_id = hashlib.sha256(canonical.encode()).hexdigest()[:24]
    orders, transactions, refunds, rejected = [], [], [], []
    money_fields = (
        "subtotalPriceSet", "totalDiscountsSet", "totalShippingPriceSet", "totalTaxSet",
        "totalPriceSet", "totalReceivedSet", "totalRefundedSet", "currentSubtotalPriceSet",
        "currentTotalDiscountsSet", "currentTotalTaxSet", "currentTotalPriceSet",
    )
    for page, row_number, raw in indexed:
        if not isinstance(raw, dict):
            rejected.append({"dataset_type": "commerce.shopify_orders", "row": row_number, "reason": "record must be an object"})
            continue
        order_id = str(raw.get("id") or "")
        try:
            if not order_id:
                raise ValueError("Shopify order requires id")
            order = {
                "order_id": order_id,
                "entity_id": str(entity_id),
                "order_name": raw.get("name"),
                "created_at": raw.get("createdAt"),
                "processed_at": raw.get("processedAt"),
                "updated_at": raw.get("updatedAt"),
                "cancelled_at": raw.get("cancelledAt"),
                "closed_at": raw.get("closedAt"),
                "shop_currency": str(raw.get("currencyCode") or "").upper(),
                "presentment_currency": str(raw.get("presentmentCurrencyCode") or "").upper(),
                "financial_status": raw.get("displayFinancialStatus"),
                "fulfillment_status": raw.get("displayFulfillmentStatus"),
                "taxes_included": raw.get("taxesIncluded"),
                "test": raw.get("test"),
                "source_name": raw.get("sourceName"),
                "destination_country": (raw.get("shippingAddress") or {}).get("countryCodeV2") if isinstance(raw.get("shippingAddress"), dict) else None,
                "money": {
                    field: _money_bag(raw.get(field), field) for field in money_fields
                },
                "evidence": _evidence(batch_id, shop_domain, "orders", order_id, page, row_number),
            }
            if not re.fullmatch(r"[A-Z]{3}", order["shop_currency"]):
                raise ValueError("Shopify order currencyCode must be a three-letter code")
            order_transactions = []
            order_refunds = []
            for nested_index, transaction in enumerate(raw.get("transactions") or [], 1):
                if not isinstance(transaction, dict) or not transaction.get("id"):
                    raise ValueError("Shopify transaction requires object and id")
                transaction_id = str(transaction["id"])
                order_transactions.append({
                    "transaction_id": transaction_id,
                    "order_id": order_id,
                    "entity_id": str(entity_id),
                    "created_at": transaction.get("createdAt"),
                    "processed_at": transaction.get("processedAt"),
                    "gateway": transaction.get("gateway"),
                    "formatted_gateway": transaction.get("formattedGateway"),
                    "kind": transaction.get("kind"),
                    "status": transaction.get("status"),
                    "test": transaction.get("test"),
                    "settlement_currency": transaction.get("settlementCurrency"),
                    "settlement_currency_rate": transaction.get("settlementCurrencyRate"),
                    "parent_transaction_id": _source_id(transaction.get("parentTransaction")),
                    "amount_set": _money_bag(transaction.get("amountSet"), "transaction.amountSet"),
                    "evidence": _evidence(batch_id, shop_domain, "transactions", transaction_id, page, nested_index),
                })
            for nested_index, refund in enumerate(raw.get("refunds") or [], 1):
                if not isinstance(refund, dict) or not refund.get("id"):
                    raise ValueError("Shopify refund requires object and id")
                refund_id = str(refund["id"])
                line_items, line_items_complete = _connection_nodes(
                    refund.get("refundLineItems"), "refund.refundLineItems",
                )
                shipping_lines, shipping_lines_complete = _connection_nodes(
                    refund.get("refundShippingLines"), "refund.refundShippingLines",
                )
                adjustments, adjustments_complete = _connection_nodes(
                    refund.get("orderAdjustments"), "refund.orderAdjustments",
                )
                refund_transactions, refund_transactions_complete = _connection_nodes(
                    refund.get("transactions"), "refund.transactions",
                )
                order_refunds.append({
                    "refund_id": refund_id,
                    "order_id": order_id,
                    "entity_id": str(entity_id),
                    "created_at": refund.get("createdAt"),
                    "processed_at": refund.get("processedAt"),
                    "updated_at": refund.get("updatedAt"),
                    "total_refunded_set": _money_bag(refund.get("totalRefundedSet"), "refund.totalRefundedSet"),
                    "refund_line_items": [{
                        "component_id": str(item.get("id") or f"{refund_id}:line:{index}"),
                        "quantity": item.get("quantity"),
                        "subtotal_set": _money_bag(
                            item.get("subtotalSet"), "refund.refundLineItems.subtotalSet",
                        ),
                        "total_tax_set": _money_bag(
                            item.get("totalTaxSet"), "refund.refundLineItems.totalTaxSet",
                        ),
                    } for index, item in enumerate(line_items, 1)],
                    "refund_shipping_lines": [{
                        "component_id": f"{refund_id}:shipping:{index}",
                        "subtotal_amount_set": _money_bag(
                            item.get("subtotalAmountSet"),
                            "refund.refundShippingLines.subtotalAmountSet",
                        ),
                        "tax_amount_set": _money_bag(
                            item.get("taxAmountSet"),
                            "refund.refundShippingLines.taxAmountSet",
                        ),
                    } for index, item in enumerate(shipping_lines, 1)],
                    "order_adjustments": [{
                        "component_id": str(item.get("id") or f"{refund_id}:adjustment:{index}"),
                        "reason": item.get("reason"),
                        "amount_set": _money_bag(
                            item.get("amountSet"), "refund.orderAdjustments.amountSet",
                        ),
                        "tax_amount_set": _money_bag(
                            item.get("taxAmountSet"), "refund.orderAdjustments.taxAmountSet",
                        ),
                    } for index, item in enumerate(adjustments, 1)],
                    "refund_transactions": [{
                        "transaction_id": str(item.get("id") or ""),
                        "created_at": item.get("createdAt"),
                        "processed_at": item.get("processedAt"),
                        "kind": item.get("kind"),
                        "status": item.get("status"),
                        "amount_set": _money_bag(
                            item.get("amountSet"), "refund.transactions.amountSet",
                        ),
                    } for item in refund_transactions],
                    "component_contract_complete": all((
                        line_items_complete, shipping_lines_complete,
                        adjustments_complete, refund_transactions_complete,
                    )),
                    "evidence": _evidence(batch_id, shop_domain, "refunds", refund_id, page, nested_index),
                })
            orders.append(order)
            transactions.extend(order_transactions)
            refunds.extend(order_refunds)
        except (TypeError, ValueError) as exc:
            rejected.append({"dataset_type": "commerce.shopify_orders", "row": row_number, "reason": str(exc)})
    return {
        "batch_id": batch_id,
        "source": source,
        "datasets": {
            "commerce.shopify_orders": orders,
            "commerce.shopify_transactions": transactions,
            "commerce.shopify_refunds": refunds,
        },
        "rejected_rows": rejected,
    }


def _fetch_order_population(
    shop_domain: str, *, query: str, search_query: str, max_pages: int,
) -> dict[str, Any]:
    try:
        return fetch_shopify_graphql_orders(
            shop_domain,
            access_token=os.environ.get(SHOPIFY_TOKEN_ENV, ""),
            api_version=SHOPIFY_API_VERSION,
            query=query,
            search_query=search_query,
            max_pages=max_pages,
            transport=HTTP_TRANSPORT,
            sleeper=HTTP_SLEEPER,
        )
    except Exception as exc:
        if isinstance(exc, ConnectorError):
            raise
        raise ConnectorError(str(exc)) from exc


def _population_objects(
    objects: Any, *, population: str, timestamp_field: str,
    lower: datetime, upper: datetime,
) -> dict[str, dict[str, Any]]:
    if not isinstance(objects, list):
        raise ConnectorError(f"Shopify monthly fixture requires {population}_objects list")
    indexed: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(objects, 1):
        if not isinstance(raw, dict) or not raw.get("id"):
            raise ConnectorError(f"{population}_objects[{index}] requires an order object with id")
        occurred_at = _utc_timestamp(raw.get(timestamp_field), f"{population}_objects[{index}].{timestamp_field}")
        if not lower <= occurred_at < upper:
            raise ConnectorError(
                f"{population}_objects[{index}].{timestamp_field} is outside its declared half-open window"
            )
        order_id = str(raw["id"])
        if order_id in indexed:
            raise ConnectorError(f"{population}_objects contains duplicate order id {order_id}")
        indexed[order_id] = raw
    return indexed


def _monthly_handler(request: dict[str, Any], context: ConnectorContext) -> dict[str, Any]:
    """Capture one close-time snapshot covering created orders and all orders updated since month start."""
    if any(str(key).lower() in _INLINE_SECRET_FIELDS for key in request):
        raise ConnectorError("Shopify credentials must not be passed in connector requests")
    entity_id = request.get("default_entity_id")
    if entity_id not in context.allowed_entity_ids:
        raise ConnectorError("Shopify connector requires a valid default_entity_id")
    period, interval_start, interval_end, start_dt, end_dt = _canonical_month_window(request)
    mode = request.get("mode", "fixture")
    if mode == "fetch":
        observed_dt = datetime.now(timezone.utc)
        observed_at = observed_dt.isoformat().replace("+00:00", "Z")
    else:
        observed_dt = _utc_timestamp(request.get("source_observed_at"), "source_observed_at")
        observed_at = observed_dt.isoformat().replace("+00:00", "Z")
    if observed_dt < end_dt or observed_dt > end_dt + timedelta(hours=72):
        raise ConnectorError(
            "source_observed_at must be from month end through the 72-hour close-capture window"
        )
    shop_domain = str(request.get("shop_domain") or "").lower()
    if not shop_domain and mode == "fixture":
        shop_domain = "fixture.myshopify.com"
    if not _SHOP_DOMAIN.fullmatch(shop_domain):
        raise ConnectorError("Shopify shop_domain must be one store under myshopify.com")

    source_stats: dict[str, Any]
    if mode == "fixture":
        created_objects = request.get("created_objects")
        updated_objects = request.get("updated_objects")
        source_stats = {
            "kind": "fixture", "network_access_performed": False,
            "created_page_count": 1, "updated_page_count": 1,
            "retry_count": 0, "rate_limit_count": 0,
            "retry_delay_seconds_total": 0.0, "retry_after_honored": False,
        }
    elif mode == "fetch":
        max_pages = request.get("max_pages", 50)
        if not isinstance(max_pages, int) or isinstance(max_pages, bool) or not 1 <= max_pages <= 100:
            raise ConnectorError("max_pages must be an integer from 1 to 100")
        created_fetch = _fetch_order_population(
            shop_domain, query=ORDERS_QUERY,
            search_query=f"status:any created_at:>={interval_start} created_at:<{interval_end}",
            max_pages=max_pages,
        )
        updated_fetch = _fetch_order_population(
            shop_domain, query=UPDATED_ORDERS_QUERY,
            search_query=_updated_search_query(interval_start, observed_at),
            max_pages=max_pages,
        )
        created_objects = [
            raw for page in created_fetch["pages"]
            for raw in page["data"]["orders"]["nodes"]
        ]
        updated_objects = [
            raw for page in updated_fetch["pages"]
            for raw in page["data"]["orders"]["nodes"]
        ]
        source_stats = {
            "kind": "api", "network_access_performed": True,
            "created_page_count": created_fetch["page_count"],
            "updated_page_count": updated_fetch["page_count"],
            "retry_count": created_fetch["retry_count"] + updated_fetch["retry_count"],
            "rate_limit_count": created_fetch["rate_limit_count"] + updated_fetch["rate_limit_count"],
            "retry_delay_seconds_total": (
                created_fetch["retry_delay_seconds_total"]
                + updated_fetch["retry_delay_seconds_total"]
            ),
            "retry_after_honored": bool(
                created_fetch["retry_after_honored"] or updated_fetch["retry_after_honored"]
            ),
        }
    else:
        raise ConnectorError("Shopify monthly connector mode must be fixture or fetch")

    created = _population_objects(
        created_objects, population="created", timestamp_field="createdAt",
        lower=start_dt, upper=end_dt,
    )
    updated = _population_objects(
        updated_objects, population="updated", timestamp_field="updatedAt",
        lower=start_dt, upper=observed_dt,
    )
    for order_id in sorted(set(created) & set(updated)):
        if created[order_id] != updated[order_id]:
            raise ConnectorError(
                f"Shopify order {order_id} changed between created and updated population reads; rerun close capture"
            )
    union = {**created, **updated}
    normalized = _handler({
        "mode": "fixture", "default_entity_id": entity_id,
        "shop_domain": shop_domain, "objects": [union[key] for key in sorted(union)],
    }, context)
    populations_by_order = {
        order_id: [
            population for population, values in (("created", created), ("updated", updated))
            if order_id in values
        ]
        for order_id in sorted(union)
    }
    canonical = json.dumps({
        "api_version": SHOPIFY_API_VERSION, "entity_id": entity_id,
        "shop_domain": shop_domain, "period": period,
        "interval_start": interval_start, "interval_end": interval_end,
        "source_observed_at": observed_at,
        "created_objects": created_objects, "updated_objects": updated_objects,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    batch_id = hashlib.sha256(canonical.encode()).hexdigest()[:24]
    for rows in normalized["datasets"].values():
        for row in rows:
            row["source_populations"] = populations_by_order[str(row["order_id"])]
            row["evidence"]["batch_id"] = batch_id
            row["evidence"]["canonical_month_period"] = period
    normalized["batch_id"] = batch_id
    normalized["source"] = {
        **source_stats,
        "name": "shopify.monthly_order_evidence",
        "shop_domain": shop_domain,
        "api_version": SHOPIFY_API_VERSION,
        "canonical_month_period": period,
        "interval_semantics": "half_open_utc_calendar_month",
        "interval_start": interval_start,
        "interval_end": interval_end,
        "source_observed_at": observed_at,
        "close_capture_deadline_hours": 72,
        "created_population_count": len(created),
        "updated_since_month_start_population_count": len(updated),
        "deduplicated_order_count": len(union),
        "updated_population_upper_bound_is_source_observed_at": True,
        "refund_event_membership_uses_processed_at": True,
    }
    return normalized


def register_connectors(registry: ConnectorRegistry) -> None:
    registry.register(ConnectorDefinition(
        connector_id="shopify.orders",
        pack_id="connector.shopify",
        capability="connector.shopify_orders",
        display_name="Shopify Orders / Transactions / Refunds（只读）",
        dataset_types=(
            "commerce.shopify_orders", "commerce.shopify_transactions", "commerce.shopify_refunds",
        ),
        handler=_handler,
        business_keys={
            "commerce.shopify_orders": ("order_id",),
            "commerce.shopify_transactions": ("transaction_id",),
            "commerce.shopify_refunds": ("refund_id",),
        },
        credential_env=(SHOPIFY_TOKEN_ENV,),
        network_access=True,
        sync_window=ConnectorSyncWindow(
            start_field="created_at_gte", end_field="created_at_lt", value_format="iso8601",
        ),
    ))
    registry.register(ConnectorDefinition(
        connector_id="shopify.monthly_order_evidence",
        pack_id="connector.shopify",
        capability="connector.shopify_monthly_order_evidence",
        display_name="Shopify 月度订单与退款双窗口证据（只读）",
        dataset_types=(
            "commerce.shopify_orders", "commerce.shopify_transactions", "commerce.shopify_refunds",
        ),
        handler=_monthly_handler,
        business_keys={
            "commerce.shopify_orders": ("order_id",),
            "commerce.shopify_transactions": ("transaction_id",),
            "commerce.shopify_refunds": ("refund_id",),
        },
        credential_env=(SHOPIFY_TOKEN_ENV,),
        network_access=True,
        sync_window=ConnectorSyncWindow(
            start_field="interval_start", end_field="interval_end", value_format="iso8601",
        ),
    ))
