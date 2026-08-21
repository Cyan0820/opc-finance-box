from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .box_runtime import BoxRuntime, BoxRuntimeError


MAX_REQUEST_BYTES = 256 * 1024
PIPELINE_ID = "amazon_seller.marketplace_close"
CONNECTOR_ID = "amazon_seller.marketplace_evidence"
PERIOD_PATTERN = re.compile(r"^20\d{2}-(?:0[1-9]|1[0-2])$")
MARKETPLACE_PATTERN = re.compile(r"^[A-Z0-9]{6,32}$")
SECRET_KEY_PATTERN = re.compile(
    r"(?:token|secret|password|authorization|api[_-]?key|credential|client[_-]?id|seller[_-]?id|region|endpoint|url)",
    re.I,
)
SECRET_VALUE_PATTERN = re.compile(r"(?:bearer\s+|https?://|eyJ[A-Za-z0-9_-]{12,})", re.I)


class AmazonSellerShadowRequestError(ValueError):
    """Raised when an Amazon Seller marketplace Shadow request is unsafe or incomplete."""


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _period_bounds(period: str) -> tuple[str, str, datetime, datetime]:
    if not isinstance(period, str) or not PERIOD_PATTERN.fullmatch(period):
        raise AmazonSellerShadowRequestError("period must use YYYY-MM")
    year, month = (int(item) for item in period.split("-"))
    next_year = year + (1 if month == 12 else 0)
    next_month = 1 if month == 12 else month + 1
    start_text = f"{period}-01T00:00:00Z"
    end_text = f"{next_year:04d}-{next_month:02d}-01T00:00:00Z"
    start = datetime.strptime(start_text, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )
    end = datetime.strptime(end_text, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )
    return start_text, end_text, start, end


def _ensure_closed_month(end: datetime) -> None:
    if end > datetime.now(timezone.utc) - timedelta(minutes=2):
        raise AmazonSellerShadowRequestError(
            "Amazon Seller Shadow requires a completed calendar month ending at least two minutes ago"
        )


def _ensure_entity_scope(runtime: BoxRuntime, entity_id: str) -> None:
    try:
        runtime.reload()
        runtime.require_capability("connector.amazon_seller_marketplace_evidence")
        runtime.require_connector_entity("connector.amazon_seller", entity_id)
        runtime.require_entity(entity_id)
    except (BoxRuntimeError, ValueError) as exc:
        raise AmazonSellerShadowRequestError(str(exc)) from exc


def _marketplace_id(value: Any) -> str:
    text = str(value or "").strip().upper()
    if not MARKETPLACE_PATTERN.fullmatch(text):
        raise AmazonSellerShadowRequestError(
            "marketplace_id must use 6-32 uppercase letters or digits"
        )
    return text


def _contains_secret(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            SECRET_KEY_PATTERN.search(str(key)) is not None
            or _contains_secret(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_secret(item) for item in value)
    return isinstance(value, str) and SECRET_VALUE_PATTERN.search(value) is not None


def _write_private_json(output: str | Path, value: dict[str, Any]) -> Path:
    destination = Path(output).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    body = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    try:
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise AmazonSellerShadowRequestError(
            "Amazon Seller Shadow request output already exists"
        ) from exc
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(body)
    except Exception:
        try:
            destination.unlink()
        except OSError:
            pass
        raise
    if os.name != "nt":
        os.chmod(destination, 0o600)
    return destination


def build_amazon_seller_shadow_request(
    runtime: BoxRuntime,
    *,
    entity_id: str,
    period: str,
    marketplace_id: str,
    output: str | Path,
) -> dict[str, Any]:
    """Create one complete production Amazon Orders/FBA Inventory/Finances request."""
    _ensure_entity_scope(runtime, entity_id)
    marketplace = _marketplace_id(marketplace_id)
    interval_start, interval_end, _start, end = _period_bounds(period)
    _ensure_closed_month(end)
    request = {
        "pipeline_id": PIPELINE_ID,
        "payload": {
            "entity_id": entity_id,
            "period": period,
            "amazon_seller_marketplace_request": {
                "mode": "fetch",
                "default_entity_id": entity_id,
                "environment": "production",
                "marketplace_id": marketplace,
                "interval_start": interval_start,
                "interval_end": interval_end,
                "orders_time_basis": "created",
                "max_order_pages": 20,
                "max_inventory_pages": 20,
                "max_transaction_pages": 20,
            },
        },
    }
    destination = _write_private_json(output, request)
    return {
        "written": True,
        "output": str(destination),
        "pipeline_id": PIPELINE_ID,
        "connector_id": CONNECTOR_ID,
        "entity_id": entity_id,
        "period": period,
        "request_fingerprint": _fingerprint(request),
        "template_only": False,
        "request_contract_complete": True,
        "ready_for_network_dispatch": True,
        "operator_edits_required": [],
        "production_environment_bound": True,
        "exact_closed_month_bounds_generated": True,
        "marketplace_scope_in_private_request": True,
        "bounded_three_source_pagination": True,
        "credential_configuration_checked": False,
        "credentials_included": False,
        "seller_or_region_values_included": False,
        "marketplace_value_returned": False,
        "financial_amounts_included": False,
        "buyer_product_or_inventory_values_included": False,
        "external_actions_performed": False,
    }


def read_private_amazon_seller_shadow_request(path: str | Path) -> dict[str, Any]:
    source = Path(path).expanduser().resolve()
    if source.is_symlink() or not source.is_file():
        raise AmazonSellerShadowRequestError(
            "Amazon Seller Shadow request must be a regular non-symlink file"
        )
    metadata = source.stat()
    if os.name != "nt" and stat.S_IMODE(metadata.st_mode) != 0o600:
        raise AmazonSellerShadowRequestError(
            "Amazon Seller Shadow request must use mode 0600"
        )
    if not 0 < metadata.st_size <= MAX_REQUEST_BYTES:
        raise AmazonSellerShadowRequestError(
            "Amazon Seller Shadow request must be between 1 byte and 256 KiB"
        )
    try:
        request = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AmazonSellerShadowRequestError(
            "Amazon Seller Shadow request must contain valid UTF-8 JSON"
        ) from exc
    if not isinstance(request, dict):
        raise AmazonSellerShadowRequestError(
            "Amazon Seller Shadow request must be a JSON object"
        )
    return request


def validate_amazon_seller_shadow_request(
    runtime: BoxRuntime,
    request: dict[str, Any],
) -> dict[str, Any]:
    """Validate one complete three-source request without reading credentials or the network."""
    if not isinstance(request, dict) or set(request) != {"pipeline_id", "payload"}:
        raise AmazonSellerShadowRequestError(
            "Amazon Seller Shadow request fields must be pipeline_id and payload"
        )
    if request.get("pipeline_id") != PIPELINE_ID:
        raise AmazonSellerShadowRequestError(
            f"Amazon Seller Shadow request requires pipeline_id {PIPELINE_ID}"
        )
    payload = request.get("payload")
    if not isinstance(payload, dict) or set(payload) != {
        "entity_id", "period", "amazon_seller_marketplace_request",
    }:
        raise AmazonSellerShadowRequestError(
            "Amazon Seller Shadow request payload fields are invalid"
        )
    if _contains_secret(request):
        raise AmazonSellerShadowRequestError(
            "credentials, Seller ID, region and endpoint values are prohibited in the request"
        )
    entity_id = payload.get("entity_id")
    if not isinstance(entity_id, str) or not entity_id:
        raise AmazonSellerShadowRequestError("payload.entity_id is required")
    _ensure_entity_scope(runtime, entity_id)
    period = payload.get("period")
    interval_start, interval_end, _start, end = _period_bounds(period)
    _ensure_closed_month(end)
    connector_request = payload.get("amazon_seller_marketplace_request")
    if not isinstance(connector_request, dict):
        raise AmazonSellerShadowRequestError(
            "Amazon Seller Shadow connector request fields are invalid"
        )
    marketplace = _marketplace_id(connector_request.get("marketplace_id"))
    expected = {
        "mode": "fetch",
        "default_entity_id": entity_id,
        "environment": "production",
        "marketplace_id": marketplace,
        "interval_start": interval_start,
        "interval_end": interval_end,
        "orders_time_basis": "created",
        "max_order_pages": 20,
        "max_inventory_pages": 20,
        "max_transaction_pages": 20,
    }
    if set(connector_request) != set(expected):
        raise AmazonSellerShadowRequestError(
            "Amazon Seller Shadow connector request fields are invalid"
        )
    if connector_request != expected:
        raise AmazonSellerShadowRequestError(
            "Amazon Seller Shadow request must use production fetch, one exact closed month, "
            "created-order scope and bounded three-source pagination"
        )
    return {
        "valid": True,
        "pipeline_id": PIPELINE_ID,
        "connector_id": CONNECTOR_ID,
        "entity_id": entity_id,
        "period": period,
        "request_fingerprint": _fingerprint(request),
        "request_contract_complete": True,
        "production_environment_bound": True,
        "exact_closed_month_bounds": True,
        "marketplace_scope_declared": True,
        "bounded_three_source_pagination": True,
        "credentials_included": False,
        "seller_or_region_values_returned": False,
        "marketplace_value_returned": False,
        "buyer_product_or_inventory_values_returned": False,
        "financial_amounts_returned": False,
        "network_access_performed": False,
        "external_actions_performed": False,
    }


def verify_private_amazon_seller_shadow_request(
    runtime: BoxRuntime,
    path: str | Path,
) -> dict[str, Any]:
    return validate_amazon_seller_shadow_request(
        runtime,
        read_private_amazon_seller_shadow_request(path),
    )
