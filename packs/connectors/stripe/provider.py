from __future__ import annotations

import hashlib
import json
import os
import re
import time
from typing import Any

from src.connector_http import fetch_stripe_list_json, urllib_transport
from src.connector_sdk import (
    ConnectorContext, ConnectorDefinition, ConnectorError, ConnectorRegistry,
    ConnectorSyncWindow,
)


STRIPE_API_VERSION = "2026-06-24.dahlia"
STRIPE_KEY_ENV = "OPC_STRIPE_RESTRICTED_KEY"
BALANCE_ENDPOINT = "https://api.stripe.com/v1/balance_transactions"
PAYOUT_ENDPOINT = "https://api.stripe.com/v1/payouts"
HTTP_TRANSPORT = urllib_transport
HTTP_SLEEPER = time.sleep
_INLINE_SECRET_FIELDS = {"token", "api_key", "secret", "password", "authorization", "restricted_key"}


def _object_id(value: Any) -> str | None:
    if isinstance(value, str):
        return value or None
    if isinstance(value, dict):
        object_id = value.get("id")
        return str(object_id) if object_id else None
    return None


def _integer(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{field} must be an integer in the currency's smallest unit")
    return value


def _currency(value: Any) -> str:
    currency = str(value or "").upper()
    if not re.fullmatch(r"[A-Z]{3}", currency):
        raise ValueError("currency must be a three-letter code")
    return currency


def _evidence(batch_id: str, resource: str, object_id: str, page: int, row: int) -> dict[str, Any]:
    return {
        "source_file": "api:stripe",
        "source_sheet": resource,
        "source_row": row,
        "source_page": page,
        "source_object_id": object_id,
        "batch_id": batch_id,
        "api_version": STRIPE_API_VERSION,
    }


def _balance_record(
    raw: dict[str, Any], entity_id: str, batch_id: str, page: int, row: int,
) -> dict[str, Any]:
    transaction_id = str(raw.get("id") or "")
    if raw.get("object") != "balance_transaction" or not transaction_id:
        raise ValueError("expected a Stripe balance_transaction with id")
    fee_details = []
    for detail in raw.get("fee_details") or []:
        if isinstance(detail, dict):
            fee_details.append({
                "amount_minor": _integer(detail.get("amount"), "fee_details.amount"),
                "currency": _currency(detail.get("currency")),
                "type": detail.get("type"),
                "description": detail.get("description"),
            })
    return {
        "balance_transaction_id": transaction_id,
        "entity_id": entity_id,
        "amount_minor": _integer(raw.get("amount"), "amount"),
        "fee_minor": _integer(raw.get("fee"), "fee"),
        "net_minor": _integer(raw.get("net"), "net"),
        "currency": _currency(raw.get("currency")),
        "available_on": raw.get("available_on"),
        "created": raw.get("created"),
        "description": raw.get("description"),
        "exchange_rate": raw.get("exchange_rate"),
        "fee_details": fee_details,
        "reporting_category": raw.get("reporting_category"),
        "source_object_id": _object_id(raw.get("source")),
        "status": raw.get("status"),
        "transaction_type": raw.get("type"),
        "evidence": _evidence(batch_id, "balance_transactions", transaction_id, page, row),
    }


def _payout_record(
    raw: dict[str, Any], entity_id: str, batch_id: str, page: int, row: int,
) -> dict[str, Any]:
    payout_id = str(raw.get("id") or "")
    if raw.get("object") != "payout" or not payout_id:
        raise ValueError("expected a Stripe payout with id")
    return {
        "payout_id": payout_id,
        "entity_id": entity_id,
        "amount_minor": _integer(raw.get("amount"), "amount"),
        "currency": _currency(raw.get("currency")),
        "arrival_date": raw.get("arrival_date"),
        "automatic": raw.get("automatic"),
        "balance_transaction_id": _object_id(raw.get("balance_transaction")),
        "created": raw.get("created"),
        "description": raw.get("description"),
        "failure_balance_transaction_id": _object_id(raw.get("failure_balance_transaction")),
        "failure_code": raw.get("failure_code"),
        "failure_message": raw.get("failure_message"),
        "livemode": raw.get("livemode"),
        "method": raw.get("method"),
        "reconciliation_status": raw.get("reconciliation_status"),
        "source_type": raw.get("source_type"),
        "statement_descriptor": raw.get("statement_descriptor"),
        "status": raw.get("status"),
        "payout_type": raw.get("type"),
        "evidence": _evidence(batch_id, "payouts", payout_id, page, row),
    }


def _request_parameters(request: dict[str, Any], resource: str) -> dict[str, str | int]:
    parameters: dict[str, str | int] = {"limit": 100}
    for request_key, stripe_key in (("created_gte", "created[gte]"), ("created_lt", "created[lt]")):
        if request_key in request:
            value = request[request_key]
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ConnectorError(f"{request_key} must be a non-negative Unix timestamp")
            parameters[stripe_key] = value
    if "created_gte" in request and "created_lt" in request and request["created_gte"] >= request["created_lt"]:
        raise ConnectorError("created_gte must be earlier than created_lt")
    if resource == "payouts" and "status" in request:
        status = request["status"]
        if status not in {"pending", "paid", "failed", "canceled"}:
            raise ConnectorError("payout status filter is invalid")
        parameters["status"] = status
    return parameters


def _resource_handler(resource: str, request: dict[str, Any], context: ConnectorContext) -> dict[str, Any]:
    if any(str(key).lower() in _INLINE_SECRET_FIELDS for key in request):
        raise ConnectorError("Stripe credentials must not be passed in connector requests")
    entity_id = request.get("default_entity_id")
    if entity_id not in context.allowed_entity_ids:
        raise ConnectorError("Stripe connector requires a valid default_entity_id")
    mode = request.get("mode", "fixture")
    stripe_account = request.get("stripe_account")
    if stripe_account is not None and (
        not isinstance(stripe_account, str)
        or not re.fullmatch(r"acct_[A-Za-z0-9]{8,128}", stripe_account)
    ):
        raise ConnectorError("Stripe connected account binding is invalid")
    endpoint = BALANCE_ENDPOINT if resource == "balance_transactions" else PAYOUT_ENDPOINT
    parameters = _request_parameters(request, resource)
    if mode == "fixture":
        if stripe_account is not None:
            raise ConnectorError("Stripe connected account binding is supported in fetch mode only")
        objects = request.get("objects")
        if not isinstance(objects, list):
            raise ConnectorError("Stripe fixture mode requires an objects list")
        indexed = [(1, index, raw) for index, raw in enumerate(objects, 1)]
        source = {
            "kind": "fixture",
            "name": f"stripe.{resource}",
            "network_access_performed": False,
            "api_version": STRIPE_API_VERSION,
            "page_count": 1,
            "retry_count": 0,
        }
    elif mode == "fetch":
        restricted_key = os.environ.get(STRIPE_KEY_ENV, "")
        max_pages = request.get("max_pages", 50)
        if not isinstance(max_pages, int) or isinstance(max_pages, bool) or not 1 <= max_pages <= 100:
            raise ConnectorError("max_pages must be an integer from 1 to 100")
        starting_after = request.get("starting_after")
        if starting_after is not None and (
            not isinstance(starting_after, str) or not starting_after or len(starting_after) > 2048
        ):
            raise ConnectorError("starting_after must be a bounded non-empty cursor")
        try:
            fetched = fetch_stripe_list_json(
                endpoint,
                restricted_key=restricted_key,
                api_version=STRIPE_API_VERSION,
                stripe_account=stripe_account,
                parameters=parameters,
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
            for row_number, raw in enumerate(page["data"], 1)
        ]
        objects = [raw for _, _, raw in indexed]
        source = {
            "kind": "api",
            "name": f"stripe.{resource}",
            "network_access_performed": True,
            "api_version": STRIPE_API_VERSION,
            "page_count": fetched["page_count"],
            "retry_count": fetched["retry_count"],
            "rate_limit_count": fetched["rate_limit_count"],
            "retry_delay_seconds_total": fetched["retry_delay_seconds_total"],
            "retry_after_honored": fetched["retry_after_honored"],
        }
    else:
        raise ConnectorError("Stripe connector mode must be fixture or fetch")

    canonical = json.dumps(
        {
            "api_version": STRIPE_API_VERSION, "entity_id": entity_id,
            "resource": resource, "request_parameters": parameters, "objects": objects,
        },
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    batch_id = hashlib.sha256(canonical.encode()).hexdigest()[:24]
    dataset_type = f"payments.stripe_{resource}"
    rows = []
    rejected = []
    mapper = _balance_record if resource == "balance_transactions" else _payout_record
    for page, row_number, raw in indexed:
        if not isinstance(raw, dict):
            rejected.append({"dataset_type": dataset_type, "row": row_number, "reason": "record must be an object"})
            continue
        try:
            mapped = mapper(raw, str(entity_id), batch_id, page, row_number)
            created_gte = parameters.get("created[gte]")
            created_lt = parameters.get("created[lt]")
            if created_gte is not None and created_lt is not None:
                created = mapped.get("created")
                if not isinstance(created, int) or isinstance(created, bool):
                    raise ValueError("created must be a Unix timestamp when a sync window is declared")
                if not int(created_gte) <= created < int(created_lt):
                    raise ValueError("created is outside the declared half-open sync window")
            rows.append(mapped)
        except (TypeError, ValueError) as exc:
            rejected.append({"dataset_type": dataset_type, "row": row_number, "reason": str(exc)})
    source["created_window"] = {
        "gte": parameters.get("created[gte]"),
        "lt": parameters.get("created[lt]"),
        "semantics": "half_open_unix_seconds",
        "complete_bounds_declared": (
            "created[gte]" in parameters and "created[lt]" in parameters
        ),
    }
    return {
        "batch_id": batch_id,
        "source": source,
        "datasets": {dataset_type: rows},
        "rejected_rows": rejected,
    }


def _balance_handler(request: dict[str, Any], context: ConnectorContext) -> dict[str, Any]:
    return _resource_handler("balance_transactions", request, context)


def _payout_handler(request: dict[str, Any], context: ConnectorContext) -> dict[str, Any]:
    return _resource_handler("payouts", request, context)


def register_connectors(registry: ConnectorRegistry) -> None:
    registry.register(ConnectorDefinition(
        connector_id="stripe.balance_transactions",
        pack_id="connector.stripe",
        capability="connector.stripe_balance_transactions",
        display_name="Stripe Balance Transactions（只读）",
        dataset_types=("payments.stripe_balance_transactions",),
        handler=_balance_handler,
        business_keys={"payments.stripe_balance_transactions": ("balance_transaction_id",)},
        credential_env=(STRIPE_KEY_ENV,),
        network_access=True,
        sync_window=ConnectorSyncWindow(
            start_field="created_gte", end_field="created_lt", value_format="unix_seconds",
        ),
    ))
    registry.register(ConnectorDefinition(
        connector_id="stripe.payouts",
        pack_id="connector.stripe",
        capability="connector.stripe_payouts",
        display_name="Stripe Payouts（只读）",
        dataset_types=("payments.stripe_payouts",),
        handler=_payout_handler,
        business_keys={"payments.stripe_payouts": ("payout_id",)},
        credential_env=(STRIPE_KEY_ENV,),
        network_access=True,
        sync_window=ConnectorSyncWindow(
            start_field="created_gte", end_field="created_lt", value_format="unix_seconds",
        ),
    ))
