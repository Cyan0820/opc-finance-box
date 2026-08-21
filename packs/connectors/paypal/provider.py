from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from src.connector_http import fetch_paypal_transaction_pages, urllib_transport
from src.connector_entity_credentials import (
    PAYPAL_LEGACY_CLIENT_ID_ENV,
    PAYPAL_LEGACY_CLIENT_SECRET_ENV,
    resolve_paypal_entity_credentials,
)
from src.connector_sdk import (
    ConnectorContext, ConnectorDefinition, ConnectorError, ConnectorRegistry,
    ConnectorSyncWindow,
)


PAYPAL_CLIENT_ID_ENV = PAYPAL_LEGACY_CLIENT_ID_ENV
PAYPAL_CLIENT_SECRET_ENV = PAYPAL_LEGACY_CLIENT_SECRET_ENV
PAYPAL_API_CONTRACT = "transaction-search-v1"
HTTP_TRANSPORT = urllib_transport
HTTP_SLEEPER = time.sleep
_INLINE_SECRET_FIELDS = {
    "token", "access_token", "api_key", "secret", "password", "authorization",
    "client_id", "client_secret",
}
_ACTIVITY_CLASS_BY_GROUP = {
    "T00": "payment_activity",
    "T01": "non_payment_fee_activity",
    "T04": "balance_withdrawal_or_transfer",
    "T11": "reversal_refund_or_hold_activity",
}
_REFUND_CODES = {"T1107", "T1115"}
_REVERSAL_CODES = {"T1106", "T1114"}
_FEE_REFUND_CODES = {"T1108", "T1109"}


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
    return text, parsed


def _window(request: dict[str, Any]) -> tuple[str, datetime, str, datetime]:
    try:
        start_text, start = _timestamp(request.get("interval_start"), "interval_start", required=True)
        end_text, end = _timestamp(request.get("interval_end"), "interval_end", required=True)
    except ValueError as exc:
        raise ConnectorError(str(exc)) from exc
    assert start_text is not None and start is not None and end_text is not None and end is not None
    if start >= end:
        raise ConnectorError("interval_start must be earlier than interval_end")
    if end - start > timedelta(days=31):
        raise ConnectorError("PayPal evidence window must not exceed 31 days")
    return start_text, start, end_text, end


def _api_inclusive_end(end: datetime) -> str:
    value = (end - timedelta(microseconds=1)).isoformat(timespec="microseconds")
    return value.replace("+00:00", "Z")


def _source_id(value: Any, field: str) -> str:
    text = str(value if value is not None else "").strip()
    if not text or len(text) > 255 or any(ord(character) < 33 or ord(character) == 127 for character in text):
        raise ValueError(f"{field} requires a bounded source identifier")
    return text


def _hash_reference(kind: str, value: Any, *, required: bool = False) -> str | None:
    if value in (None, "") and not required:
        return None
    raw = _source_id(value, kind)
    return hashlib.sha256(f"paypal|{kind}|{raw}".encode()).hexdigest()


def _money(value: Any, field: str, *, required: bool = False) -> tuple[str | None, str | None]:
    if value in (None, "") and not required:
        return None, None
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be a money object")
    currency = str(value.get("currency_code") or "").upper()
    if not re.fullmatch(r"[A-Z]{3}", currency):
        raise ValueError(f"{field}.currency_code must be a three-letter code")
    try:
        amount = Decimal(str(value.get("value")))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field}.value must be a finite decimal") from exc
    if not amount.is_finite():
        raise ValueError(f"{field}.value must be a finite decimal")
    return format(amount, "f"), currency


def _bounded_code(value: Any, field: str, pattern: str, *, required: bool = False) -> str | None:
    if value in (None, "") and not required:
        return None
    text = str(value or "").strip()
    if not re.fullmatch(pattern, text):
        raise ValueError(f"{field} is invalid")
    return text


def _evidence(batch_id: str, transaction_key: str, page: int, row: int) -> dict[str, Any]:
    return {
        "source_file": "api:paypal",
        "source_sheet": "transaction_search",
        "source_page": page,
        "source_row": row,
        "source_object_id": transaction_key,
        "batch_id": batch_id,
        "api_contract": PAYPAL_API_CONTRACT,
    }


def _normalize_transaction(
    raw: dict[str, Any], *, entity_id: str, batch_id: str, page: int, row: int,
    start: datetime, end: datetime,
) -> dict[str, Any]:
    info = raw.get("transaction_info")
    if not isinstance(info, dict):
        raise ValueError("transaction_info must be an object")
    transaction_key = _hash_reference("transaction", info.get("transaction_id"), required=True)
    assert transaction_key is not None
    initiated_at, initiated = _timestamp(
        info.get("transaction_initiation_date"), "transaction_initiation_date", required=True,
    )
    if initiated is None or not start <= initiated < end:
        raise ValueError("transaction_initiation_date must fall inside the requested half-open interval")
    updated_at, _ = _timestamp(info.get("transaction_updated_date"), "transaction_updated_date")
    event_code = _bounded_code(
        info.get("transaction_event_code"), "transaction_event_code", r"T[0-9]{4}", required=True,
    )
    assert event_code is not None
    status = _bounded_code(info.get("transaction_status"), "transaction_status", r"[A-Za-z0-9_-]{1,32}")
    amount, amount_currency = _money(info.get("transaction_amount"), "transaction_amount", required=True)
    fee, fee_currency = _money(info.get("fee_amount"), "fee_amount")
    assert amount is not None and amount_currency is not None
    if fee is None:
        fee, fee_currency = "0", amount_currency
    net = None
    if fee_currency == amount_currency:
        net = format(Decimal(amount) + Decimal(fee), "f")
    event_group = event_code[:3]
    return {
        "paypal_transaction_key": transaction_key,
        "entity_id": entity_id,
        "event_code": event_code,
        "event_group": event_group,
        "activity_class": _ACTIVITY_CLASS_BY_GROUP.get(event_group, "other_balance_activity"),
        "transaction_status": status,
        "initiated_at": initiated_at,
        "updated_at": updated_at,
        "amount": amount,
        "amount_currency": amount_currency,
        "fee": fee,
        "fee_currency": fee_currency,
        "net_when_same_currency": net,
        "reference_transaction_key": _hash_reference("transaction", info.get("paypal_reference_id")),
        "reference_type": _bounded_code(
            info.get("paypal_reference_id_type"), "paypal_reference_id_type", r"[A-Za-z0-9_-]{1,32}",
        ),
        "refund_candidate": event_code in _REFUND_CODES,
        "reversal_candidate": event_code in _REVERSAL_CODES,
        "fee_refund_or_reversal": event_code in _FEE_REFUND_CODES,
        "evidence": _evidence(batch_id, transaction_key, page, row),
    }


def _handler(request: dict[str, Any], context: ConnectorContext) -> dict[str, Any]:
    if any(str(key).lower() in _INLINE_SECRET_FIELDS for key in request):
        raise ConnectorError("PayPal credentials must not be passed in connector requests")
    entity_id = request.get("default_entity_id")
    if entity_id not in context.allowed_entity_ids:
        raise ConnectorError("PayPal connector requires a valid default_entity_id")
    start_text, start, end_text, end = _window(request)
    mode = request.get("mode", "fixture")
    if mode == "fixture":
        pages = request.get("transaction_pages")
        if not isinstance(pages, list) or any(not isinstance(item, dict) for item in pages):
            raise ConnectorError("PayPal fixture mode requires transaction_pages objects")
        canonical = json.dumps({
            "entity_id": entity_id, "interval_start": start_text,
            "interval_end": end_text, "transaction_pages": pages,
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        batch_id = hashlib.sha256(f"paypal|fixture|{canonical}".encode()).hexdigest()[:24]
        source = {
            "kind": "fixture",
            "name": "paypal.transaction_activity",
            "api_contract": PAYPAL_API_CONTRACT,
            "environment": "fixture",
            "interval_start": start_text,
            "interval_end": end_text,
            "page_count": len(pages),
            "network_access_performed": False,
            "oauth_token_exchange_performed": False,
            "retry_count": 0,
        }
    elif mode == "fetch":
        max_pages = request.get("max_pages", 20)
        page_size = request.get("page_size", 500)
        if not isinstance(max_pages, int) or isinstance(max_pages, bool):
            raise ConnectorError("max_pages must be an integer")
        if not isinstance(page_size, int) or isinstance(page_size, bool):
            raise ConnectorError("page_size must be an integer")
        try:
            credentials = resolve_paypal_entity_credentials(
                str(entity_id),
                os.environ,
                legacy_environment=str(request.get("environment", "production")),
                require_entity_binding=len(context.allowed_entity_ids) > 1,
            )
            fetched = fetch_paypal_transaction_pages(
                client_id=credentials["client_id"],
                client_secret=credentials["client_secret"],
                environment=credentials["environment"],
                interval_start=start_text,
                interval_end=_api_inclusive_end(end),
                page_size=page_size,
                max_pages=max_pages,
                transport=HTTP_TRANSPORT,
                sleeper=HTTP_SLEEPER,
            )
        except Exception as exc:
            if isinstance(exc, ConnectorError):
                raise
            raise ConnectorError(str(exc)) from exc
        pages = fetched["pages"]
        batch_id = fetched["batch_id"]
        source = {
            "kind": "api",
            "name": "paypal.transaction_activity",
            "api_contract": PAYPAL_API_CONTRACT,
            "environment": fetched["environment"],
            "interval_start": start_text,
            "interval_end": end_text,
            "api_end_inclusive": _api_inclusive_end(end),
            "page_count": fetched["page_count"],
            "total_items": fetched["total_items"],
            "network_access_performed": True,
            "oauth_token_exchange_performed": True,
            "retry_count": fetched["retry_count"],
            "rate_limit_count": fetched["rate_limit_count"],
            "retry_delay_seconds_total": fetched["retry_delay_seconds_total"],
            "retry_after_honored": fetched["retry_after_honored"],
        }
    else:
        raise ConnectorError("PayPal connector mode must be fixture or fetch")

    records: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for page, payload in enumerate(pages, 1):
        details = payload.get("transaction_details")
        if not isinstance(details, list) or any(not isinstance(item, dict) for item in details):
            rejected.append({
                "dataset_type": "payments.paypal_balance_activity", "page": page,
                "row": 0, "reason": "transaction_details must be an object list",
            })
            continue
        for row, raw in enumerate(details, 1):
            try:
                records.append(_normalize_transaction(
                    raw, entity_id=str(entity_id), batch_id=batch_id, page=page, row=row,
                    start=start, end=end,
                ))
            except (TypeError, ValueError) as exc:
                rejected.append({
                    "dataset_type": "payments.paypal_balance_activity", "page": page,
                    "row": row, "reason": str(exc),
                })
    source.update({
        "query_fields": "transaction_info",
        "balance_affecting_records_only": True,
        "payer_identity_retained": False,
        "shipping_address_retained": False,
        "cart_or_item_detail_retained": False,
        "free_text_retained": False,
        "raw_source_ids_retained": False,
        "oauth_token_persisted": False,
        "business_write_api_called": False,
    })
    return {
        "batch_id": batch_id,
        "source": source,
        "datasets": {"payments.paypal_balance_activity": records},
        "rejected_rows": rejected,
    }


def register_connectors(registry: ConnectorRegistry) -> None:
    registry.register(ConnectorDefinition(
        connector_id="paypal.transaction_activity",
        pack_id="connector.paypal",
        capability="connector.paypal_transaction_activity",
        display_name="PayPal 余额影响交易、费用与退款证据（只读）",
        dataset_types=("payments.paypal_balance_activity",),
        handler=_handler,
        business_keys={"payments.paypal_balance_activity": ("paypal_transaction_key",)},
        credential_env=(PAYPAL_CLIENT_ID_ENV, PAYPAL_CLIENT_SECRET_ENV),
        network_access=True,
        sync_window=ConnectorSyncWindow(
            start_field="interval_start",
            end_field="interval_end",
            value_format="iso8601",
            max_incremental_days=31,
            max_backfill_days=366,
        ),
    ))
