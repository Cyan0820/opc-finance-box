from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.parse
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from src.bank_import import mask_embedded_account_numbers
from src.connector_http import HttpRequest, urllib_transport
from src.connector_sdk import (
    ConnectorContext,
    ConnectorDefinition,
    ConnectorError,
    ConnectorRegistry,
    ConnectorSyncWindow,
)


AIRWALLEX_API_VERSION = "2026-07-17"
CLIENT_ID_ENV = "OPC_AIRWALLEX_CLIENT_ID"
API_KEY_ENV = "OPC_AIRWALLEX_API_KEY"
ENTITY_BINDINGS_ENV = "OPC_AIRWALLEX_ENTITY_BINDINGS_JSON"
HTTP_TRANSPORT = urllib_transport
HTTP_SLEEPER = time.sleep
BASE_URLS = {
    "production": "https://api.airwallex.com",
    "sandbox": "https://api-demo.airwallex.com",
}
INLINE_SECRET_FIELDS = {
    "client_id", "api_key", "token", "access_token", "authorization", "secret",
    "password", "legal_entity_id", "account_id",
}
ALLOWED_SYNC_STATUSES = {"NOT_SYNCED", "READY_TO_SYNC", "SYNCED", "SYNC_FAILED"}
ALLOWED_CARD_STATUSES = {"AUTHORIZED", "CLEARED", "DECLINED", "REVERSED"}
ALLOWED_FIELD_TYPES = {
    "SUBSIDIARY", "MERCHANT", "GENERAL_LEDGER_ACCOUNT", "TAX_CODE", "CLASS",
    "DEPARTMENT", "LOCATION", "PROJECT", "OTHER",
}
EXPENSE_WEBHOOK_EVENTS = {
    "spend.expense.draft",
    "spend.expense.awaiting_approval",
    "spend.expense.updated",
    "spend.expense.rejected",
    "spend.expense.approved",
    "spend.expense.archived",
    "spend.expense.deleted",
}
HASH64_PATTERN = re.compile(r"^[0-9a-f]{64}$")
RECEIPT_PATTERN = re.compile(r"^[0-9a-f]{24}$")


def _hash(value: Any, length: int = 24) -> str:
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:length]


def _timestamp(value: Any, field: str) -> tuple[str, datetime]:
    text = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ConnectorError(f"Airwallex {field} must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ConnectorError(f"Airwallex {field} must include timezone")
    parsed = parsed.astimezone(timezone.utc)
    return parsed.isoformat().replace("+00:00", "Z"), parsed


def _identifier(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{1,199}", text):
        raise ConnectorError(f"Airwallex {field} is invalid")
    return text


def _validate_binding(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {
        "legal_entity_id", "account_id", "environment",
    }:
        raise ConnectorError(
            "Airwallex binding requires legal_entity_id, account_id and environment"
        )
    environment = str(value.get("environment") or "")
    if environment not in BASE_URLS:
        raise ConnectorError("Airwallex environment must be production or sandbox")
    return {
        "legal_entity_id": _identifier(value.get("legal_entity_id"), "legal_entity_id"),
        "account_id": _identifier(value.get("account_id"), "account_id"),
        "environment": environment,
    }


def _binding(entity_id: str) -> dict[str, str]:
    raw = os.environ.get(ENTITY_BINDINGS_ENV, "")
    if not raw:
        raise ConnectorError("Airwallex entity binding configuration is missing")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConnectorError("Airwallex entity binding configuration is invalid JSON") from exc
    selected = payload.get(entity_id) if isinstance(payload, dict) else None
    return _validate_binding(selected)


def _minor_units(value: Any, currency: str, units: dict[str, Any], field: str) -> int:
    exponent = units.get(currency)
    if not isinstance(exponent, int) or isinstance(exponent, bool) or not 0 <= exponent <= 4:
        raise ConnectorError(f"Airwallex currency_minor_units lacks {currency}")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field} must be a finite decimal string") from exc
    if not amount.is_finite():
        raise ValueError(f"{field} must be a finite decimal string")
    scaled = amount * (Decimal(10) ** exponent)
    if scaled != scaled.to_integral_value():
        raise ValueError(f"{field} cannot be represented without rounding")
    return int(scaled)


def _currency(value: Any, field: str) -> str:
    text = str(value or "").upper()
    if not re.fullmatch(r"[A-Z]{3}", text):
        raise ValueError(f"{field} must be a three-letter currency")
    return text


def _validate_refetch_contexts(
    value: Any,
    expense_ids: list[str],
    runtime_fingerprint: str,
) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) != len(expense_ids):
        raise ConnectorError(
            "Airwallex refetch requires one signed webhook context per expense id"
        )
    expected_fields = {
        "receipt_id", "event_name", "event_created_at", "expense_id_sha256",
        "body_sha256", "runtime_fingerprint",
    }
    contexts = []
    receipts = set()
    for index, (raw, expense_id) in enumerate(zip(value, expense_ids), 1):
        if not isinstance(raw, dict) or set(raw) != expected_fields:
            raise ConnectorError(
                f"Airwallex webhook context {index} fields are invalid"
            )
        receipt_id = str(raw.get("receipt_id") or "")
        event_name = str(raw.get("event_name") or "")
        expense_hash = str(raw.get("expense_id_sha256") or "")
        body_hash = str(raw.get("body_sha256") or "")
        context_fingerprint = str(raw.get("runtime_fingerprint") or "")
        event_created_at, _ = _timestamp(
            raw.get("event_created_at"), "webhook event_created_at",
        )
        if not RECEIPT_PATTERN.fullmatch(receipt_id) or receipt_id in receipts:
            raise ConnectorError("Airwallex webhook receipt id is invalid or repeated")
        if event_name not in EXPENSE_WEBHOOK_EVENTS:
            raise ConnectorError("Airwallex webhook context event name is unsupported")
        if (
            not HASH64_PATTERN.fullmatch(expense_hash)
            or expense_hash != hashlib.sha256(expense_id.encode()).hexdigest()
        ):
            raise ConnectorError("Airwallex webhook context expense binding is invalid")
        if not HASH64_PATTERN.fullmatch(body_hash):
            raise ConnectorError("Airwallex webhook context body hash is invalid")
        if (
            not HASH64_PATTERN.fullmatch(context_fingerprint)
            or context_fingerprint != runtime_fingerprint
        ):
            raise ConnectorError("Airwallex webhook context Box fingerprint is invalid")
        receipts.add(receipt_id)
        contexts.append({
            "receipt_id": receipt_id,
            "event_name": event_name,
            "event_created_at": event_created_at,
            "expense_id_sha256": expense_hash,
            "body_sha256": body_hash,
            "runtime_fingerprint": context_fingerprint,
        })
    return contexts


def _json_response(response: Any, field: str) -> dict[str, Any]:
    if response.status != 200 and not (field == "authentication" and response.status == 201):
        raise ConnectorError(f"Airwallex {field} returned HTTP {response.status}")
    try:
        payload = json.loads(response.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConnectorError(f"Airwallex {field} response is invalid JSON") from exc
    if not isinstance(payload, dict):
        raise ConnectorError(f"Airwallex {field} response must be an object")
    return payload


def _send(request: HttpRequest) -> tuple[Any, int]:
    for attempt in range(1, 4):
        response = HTTP_TRANSPORT(request)
        if response.status not in {429, 500, 502, 503, 504} or attempt == 3:
            return response, attempt - 1
        retry_after = next(
            (value for key, value in response.headers.items() if key.lower() == "retry-after"),
            None,
        )
        try:
            delay = min(max(float(retry_after), 0.0), 30.0) if retry_after is not None else float(2 ** (attempt - 1))
        except (TypeError, ValueError):
            delay = float(2 ** (attempt - 1))
        HTTP_SLEEPER(delay)
    raise ConnectorError("Airwallex request retry loop failed")  # pragma: no cover


def _authenticate(base_url: str, binding: dict[str, str]) -> tuple[str, int]:
    client_id = os.environ.get(CLIENT_ID_ENV, "")
    api_key = os.environ.get(API_KEY_ENV, "")
    if not client_id or not api_key:
        raise ConnectorError("Airwallex credentials are missing")
    response, retries = _send(HttpRequest(
        url=f"{base_url}/api/v1/authentication/login",
        method="POST",
        headers={
            "x-client-id": client_id,
            "x-api-key": api_key,
            "x-login-as": binding["account_id"],
            "x-api-version": AIRWALLEX_API_VERSION,
            "Accept": "application/json",
        },
        timeout_seconds=20,
        max_response_bytes=1024 * 1024,
    ))
    payload = _json_response(response, "authentication")
    token = str(payload.get("token") or "")
    if not token or len(token) > 16_384:
        raise ConnectorError("Airwallex authentication token is invalid")
    return token, retries


def _fetch_pages(
    base_url: str, token: str, binding: dict[str, str], start: str, end: str,
    *, max_pages: int,
) -> tuple[list[dict[str, Any]], int]:
    pages = []
    retries = 0
    cursor = None
    seen = set()
    for _ in range(max_pages):
        query = {
            "from_created_at": start,
            "to_created_at": end,
            "legal_entity_id": binding["legal_entity_id"],
            "status": "APPROVED",
        }
        if cursor:
            query["page"] = cursor
        url = f"{base_url}/api/v1/spend/expenses?{urllib.parse.urlencode(query)}"
        response, page_retries = _send(HttpRequest(
            url=url,
            headers={
                "Authorization": f"Bearer {token}",
                "x-api-version": AIRWALLEX_API_VERSION,
                "Accept": "application/json",
            },
            timeout_seconds=20,
            max_response_bytes=10 * 1024 * 1024,
        ))
        retries += page_retries
        payload = _json_response(response, "expenses")
        if not isinstance(payload.get("items"), list):
            raise ConnectorError("Airwallex expenses response requires items")
        pages.append(payload)
        cursor = payload.get("page_after")
        if not cursor:
            return pages, retries
        if not isinstance(cursor, str) or len(cursor) > 4096 or cursor in seen:
            raise ConnectorError("Airwallex expenses pagination cursor is invalid or repeated")
        seen.add(cursor)
    raise ConnectorError("Airwallex expenses pagination exceeded max_pages")


def _record(
    raw: dict[str, Any], entity_id: str, binding: dict[str, str], units: dict[str, Any],
    batch_id: str, page: int, row: int, start_dt: datetime | None, end_dt: datetime | None,
) -> dict[str, Any]:
    raw_id = _identifier(raw.get("id"), "expense id")
    if raw.get("legal_entity_id") != binding["legal_entity_id"]:
        raise ValueError("expense legal_entity_id does not match the Box binding")
    if raw.get("account_id") != binding["account_id"]:
        raise ValueError("expense account_id does not match the Box binding")
    if raw.get("status") != "APPROVED":
        raise ValueError("expense status must be APPROVED")
    created_at, created_dt = _timestamp(raw.get("created_at"), "expense created_at")
    if start_dt is not None and end_dt is not None and not start_dt <= created_dt < end_dt:
        raise ValueError("expense created_at is outside the requested half-open window")
    updated_at, _ = _timestamp(raw.get("updated_at"), "expense updated_at")
    settled_at = None
    if raw.get("settled_at") is not None:
        settled_at, _ = _timestamp(raw.get("settled_at"), "expense settled_at")
    billing_currency = _currency(raw.get("billing_currency"), "billing_currency")
    transaction = raw.get("card_transaction")
    if not isinstance(transaction, dict):
        raise ValueError("card_transaction must be an object")
    transaction_currency = _currency(transaction.get("currency"), "card transaction currency")
    card_status = str(transaction.get("status") or "")
    if card_status not in ALLOWED_CARD_STATUSES:
        raise ValueError("card transaction status is unsupported")
    sync_status = str(raw.get("sync_status") or "")
    if sync_status not in ALLOWED_SYNC_STATUSES:
        raise ValueError("expense sync status is unsupported")
    attachments = raw.get("attachments") or []
    line_items = raw.get("line_items") or []
    accounting_fields = raw.get("accounting_field_selections") or []
    if not isinstance(attachments, list):
        raise ValueError("expense attachments must be an array")
    if not isinstance(line_items, list):
        raise ValueError("expense line_items must be an array")
    if not isinstance(accounting_fields, list):
        raise ValueError("expense accounting_field_selections must be an array")
    field_types = sorted({
        str(item.get("type") or "")
        for item in accounting_fields
        if isinstance(item, dict) and item.get("type") in ALLOWED_FIELD_TYPES
    })
    merchant = mask_embedded_account_numbers(str(raw.get("merchant") or ""))[:160]
    return {
        "expense_evidence_id": _hash({"provider": "airwallex", "id": raw_id}),
        "entity_id": entity_id,
        "billing_amount_minor": _minor_units(
            raw.get("billing_amount"), billing_currency, units, "billing_amount",
        ),
        "billing_currency": billing_currency,
        "transaction_amount_minor": _minor_units(
            transaction.get("amount"), transaction_currency, units, "card_transaction.amount",
        ),
        "transaction_currency": transaction_currency,
        "approval_status": "APPROVED",
        "card_transaction_status": card_status,
        "source_sync_status": sync_status,
        "created_at": created_at,
        "updated_at": updated_at,
        "settled_at": settled_at,
        "merchant": merchant,
        "business_purpose_present": bool(str(raw.get("description") or "").strip()),
        "receipt_count": len(attachments),
        "line_item_count": len(line_items),
        "accounting_field_types": field_types,
        "ready_for_accounting_review": card_status == "CLEARED",
        "evidence": {
            "source_file": "api:airwallex",
            "source_sheet": "approved_expenses",
            "source_page": page,
            "source_row": row,
            "source_object_id_sha256": hashlib.sha256(raw_id.encode()).hexdigest(),
            "source_record_version_sha256": _hash({
                "id": raw_id, "updated_at": updated_at, "record": raw,
            }, length=64),
            "batch_id": batch_id,
            "api_version": AIRWALLEX_API_VERSION,
            "legal_entity_binding_sha256": hashlib.sha256(
                binding["legal_entity_id"].encode()
            ).hexdigest(),
            "account_binding_sha256": hashlib.sha256(binding["account_id"].encode()).hexdigest(),
        },
    }


def _state_change_record(
    raw: dict[str, Any], entity_id: str, binding: dict[str, str],
    batch_id: str, page: int, row: int,
) -> dict[str, Any]:
    raw_id = _identifier(raw.get("id"), "expense id")
    if raw.get("legal_entity_id") != binding["legal_entity_id"]:
        raise ValueError("expense legal_entity_id does not match the Box binding")
    if raw.get("account_id") != binding["account_id"]:
        raise ValueError("expense account_id does not match the Box binding")
    status = str(raw.get("status") or "").upper()
    if not re.fullmatch(r"[A-Z][A-Z_]{1,39}", status) or status == "APPROVED":
        raise ValueError("expense state change requires a non-APPROVED status")
    updated_at, _ = _timestamp(raw.get("updated_at"), "expense updated_at")
    absence_confirmed = raw.get("_provider_absence_confirmed") is True
    trigger_event_name = str(raw.get("_trigger_event_name") or "")
    if absence_confirmed and (
        status != "DELETED" or trigger_event_name != "spend.expense.deleted"
    ):
        raise ValueError("provider absence requires a signed deleted expense event")
    return {
        "expense_evidence_id": _hash({"provider": "airwallex", "id": raw_id}),
        "entity_id": entity_id,
        "current_status": status,
        "updated_at": updated_at,
        "invalidates_approved_evidence": True,
        "candidate_only": True,
        "provider_absence_confirmed": absence_confirmed,
        "deletion_signal": (
            "signed_webhook_and_get_404" if absence_confirmed
            else "current_object_non_approved_status"
        ),
        "trigger_event_name": trigger_event_name,
        "evidence": {
            "source_file": "api:airwallex",
            "source_sheet": "expense_state_refetch",
            "source_page": page,
            "source_row": row,
            "source_object_id_sha256": hashlib.sha256(raw_id.encode()).hexdigest(),
            "source_record_version_sha256": _hash({
                "id": raw_id, "updated_at": updated_at, "record": raw,
            }, length=64),
            "batch_id": batch_id,
            "api_version": AIRWALLEX_API_VERSION,
            "legal_entity_binding_sha256": hashlib.sha256(
                binding["legal_entity_id"].encode()
            ).hexdigest(),
            "account_binding_sha256": hashlib.sha256(binding["account_id"].encode()).hexdigest(),
        },
    }


def _fetch_expenses_by_id(
    base_url: str, token: str, binding: dict[str, str], expense_ids: list[str],
    webhook_contexts: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    objects = []
    retries = 0
    for expense_id, webhook_context in zip(expense_ids, webhook_contexts):
        quoted = urllib.parse.quote(expense_id, safe="")
        response, item_retries = _send(HttpRequest(
            url=f"{base_url}/api/v1/spend/expenses/{quoted}",
            headers={
                "Authorization": f"Bearer {token}",
                "x-api-version": AIRWALLEX_API_VERSION,
                "Accept": "application/json",
            },
            timeout_seconds=20,
            max_response_bytes=10 * 1024 * 1024,
        ))
        retries += item_retries
        if response.status == 404:
            if webhook_context["event_name"] != "spend.expense.deleted":
                raise ConnectorError(
                    "Airwallex expense refetch returned 404 without a signed deleted event"
                )
            objects.append({
                "id": expense_id,
                "legal_entity_id": binding["legal_entity_id"],
                "account_id": binding["account_id"],
                "status": "DELETED",
                "updated_at": webhook_context["event_created_at"],
                "_provider_absence_confirmed": True,
                "_trigger_event_name": webhook_context["event_name"],
                "_webhook_receipt_id": webhook_context["receipt_id"],
            })
            continue
        payload = _json_response(response, "expense refetch")
        if payload.get("id") != expense_id:
            raise ConnectorError("Airwallex expense refetch returned a different expense id")
        if payload.get("legal_entity_id") != binding["legal_entity_id"]:
            raise ConnectorError("Airwallex expense refetch escaped the legal entity binding")
        if payload.get("account_id") != binding["account_id"]:
            raise ConnectorError("Airwallex expense refetch escaped the account binding")
        payload = dict(payload)
        payload["_provider_absence_confirmed"] = False
        payload["_trigger_event_name"] = webhook_context["event_name"]
        payload["_webhook_receipt_id"] = webhook_context["receipt_id"]
        objects.append(payload)
    return objects, retries


def _handler(request: dict[str, Any], context: ConnectorContext) -> dict[str, Any]:
    if any(str(key).lower() in INLINE_SECRET_FIELDS for key in request):
        raise ConnectorError("Airwallex credentials and binding ids must not be passed inline")
    entity_id = request.get("default_entity_id")
    if entity_id not in context.allowed_entity_ids:
        raise ConnectorError("Airwallex connector requires a valid default_entity_id")
    units = request.get("currency_minor_units")
    if not isinstance(units, dict) or not units:
        raise ConnectorError("Airwallex currency_minor_units must be a non-empty object")
    mode = request.get("mode", "fixture")
    start = end = None
    start_dt = end_dt = None
    if mode in {"fixture", "fetch"}:
        start, start_dt = _timestamp(request.get("from_created_at"), "from_created_at")
        end, end_dt = _timestamp(request.get("to_created_at"), "to_created_at")
        if start_dt >= end_dt:
            raise ConnectorError("Airwallex from_created_at must be earlier than to_created_at")
    if mode == "fixture":
        binding = _validate_binding(request.get("fixture_binding"))
        objects = request.get("objects")
        if not isinstance(objects, list):
            raise ConnectorError("Airwallex fixture mode requires objects")
        indexed = [(1, index, item) for index, item in enumerate(objects, 1)]
        source = {
            "kind": "fixture", "name": "airwallex.approved_expenses",
            "network_access_performed": False, "api_version": AIRWALLEX_API_VERSION,
            "page_count": 1, "beta_api": True,
            "update_capture_basis": "created_at_window",
            "complete_update_capture": False,
        }
    elif mode == "fetch":
        binding = _binding(str(entity_id))
        max_pages = request.get("max_pages", 50)
        if not isinstance(max_pages, int) or isinstance(max_pages, bool) or not 1 <= max_pages <= 100:
            raise ConnectorError("Airwallex max_pages must be an integer from 1 to 100")
        base_url = BASE_URLS[binding["environment"]]
        token, auth_retries = _authenticate(base_url, binding)
        pages, page_retries = _fetch_pages(
            base_url, token, binding, start, end, max_pages=max_pages,
        )
        indexed = [
            (page_number, row_number, item)
            for page_number, page in enumerate(pages, 1)
            for row_number, item in enumerate(page["items"], 1)
        ]
        objects = [item for _, _, item in indexed]
        source = {
            "kind": "api", "name": "airwallex.approved_expenses",
            "network_access_performed": True, "api_version": AIRWALLEX_API_VERSION,
            "page_count": len(pages), "retry_count": auth_retries + page_retries,
            "beta_api": True, "update_capture_basis": "created_at_window",
            "complete_update_capture": False,
        }
    elif mode == "refetch":
        binding = _binding(str(entity_id))
        raw_ids = request.get("expense_ids")
        if (
            not isinstance(raw_ids, list) or not raw_ids or len(raw_ids) > 100
            or any(not isinstance(item, str) for item in raw_ids)
        ):
            raise ConnectorError("Airwallex refetch requires 1-100 expense_ids")
        expense_ids = [_identifier(item, "expense id") for item in raw_ids]
        if len(set(expense_ids)) != len(expense_ids):
            raise ConnectorError("Airwallex refetch expense_ids must be unique")
        webhook_contexts = _validate_refetch_contexts(
            request.get("webhook_contexts"),
            expense_ids,
            context.runtime.snapshot()["fingerprint"],
        )
        base_url = BASE_URLS[binding["environment"]]
        token, auth_retries = _authenticate(base_url, binding)
        objects, item_retries = _fetch_expenses_by_id(
            base_url, token, binding, expense_ids, webhook_contexts,
        )
        indexed = [(1, index, item) for index, item in enumerate(objects, 1)]
        source = {
            "kind": "api", "name": "airwallex.expense_refetch",
            "network_access_performed": True, "api_version": AIRWALLEX_API_VERSION,
            "page_count": len(objects), "retry_count": auth_retries + item_retries,
            "beta_api": True, "update_capture_basis": "signed_webhook_then_read_only_refetch",
            "webhook_context_validated": True,
            "webhook_context_count": len(webhook_contexts),
            "webhook_context_fingerprint": _hash(webhook_contexts, length=64),
            "provider_absence_count": sum(
                item.get("_provider_absence_confirmed") is True for item in objects
            ),
            "complete_update_capture": False,
        }
    else:
        raise ConnectorError("Airwallex connector mode must be fixture, fetch or refetch")
    batch_id = _hash({
        "api_version": AIRWALLEX_API_VERSION, "entity_id": entity_id,
        "mode": mode, "start": start, "end": end, "objects": objects,
    })
    rows, state_changes, rejected = [], [], []
    for page, row_number, raw in indexed:
        if not isinstance(raw, dict):
            rejected.append({
                "dataset_type": "finance.expense_evidence", "row": row_number,
                "reason": "record must be an object",
            })
            continue
        try:
            if mode == "refetch" and raw.get("status") != "APPROVED":
                state_changes.append(_state_change_record(
                    raw, str(entity_id), binding, batch_id, page, row_number,
                ))
            else:
                rows.append(_record(
                    raw, str(entity_id), binding, units, batch_id, page, row_number,
                    start_dt, end_dt,
                ))
        except (ConnectorError, TypeError, ValueError) as exc:
            rejected.append({
                "dataset_type": "finance.expense_evidence", "row": row_number,
                "reason": str(exc),
            })
    return {
        "batch_id": batch_id,
        "source": source,
        "datasets": {
            "finance.expense_evidence": rows,
            "finance.expense_evidence_state_changes": state_changes,
        },
        "rejected_rows": rejected,
    }


def register_connectors(registry: ConnectorRegistry) -> None:
    registry.register(ConnectorDefinition(
        connector_id="airwallex.approved_expenses",
        pack_id="connector.airwallex",
        capability="connector.airwallex_approved_expenses",
        display_name="Airwallex 已批准企业卡费用（主体绑定、只读）",
        dataset_types=(
            "finance.expense_evidence", "finance.expense_evidence_state_changes",
        ),
        handler=_handler,
        business_keys={
            "finance.expense_evidence": ("expense_evidence_id",),
            "finance.expense_evidence_state_changes": ("expense_evidence_id",),
        },
        credential_env=(
            CLIENT_ID_ENV, API_KEY_ENV, ENTITY_BINDINGS_ENV,
            "OPC_AIRWALLEX_WEBHOOK_SECRET",
        ),
        network_access=True,
        sync_window=ConnectorSyncWindow(
            start_field="from_created_at", end_field="to_created_at",
            value_format="iso8601", max_incremental_days=31, max_backfill_days=366,
            incremental_overlap_seconds=7 * 24 * 60 * 60,
        ),
    ))
