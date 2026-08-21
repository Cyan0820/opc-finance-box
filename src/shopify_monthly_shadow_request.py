from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from datetime import datetime
from pathlib import Path
from typing import Any

from .box_runtime import BoxRuntime, BoxRuntimeError


MAX_REQUEST_BYTES = 10 * 1024 * 1024
PIPELINE_ID = "dtc.shopify_stripe_month_close"
PERIOD_PATTERN = re.compile(r"^20\d{2}-(?:0[1-9]|1[0-2])$")
SHOP_DOMAIN_PATTERN = re.compile(
    r"^(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.myshopify\.com$"
)
CURRENCY_PATTERN = re.compile(r"^[A-Z]{3}$")
SHOPIFY_TRANSACTION_PATTERN = re.compile(
    r"^gid://shopify/OrderTransaction/[A-Za-z0-9_-]{1,160}$"
)
STRIPE_SOURCE_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_:-]{1,190}$")
SECRET_KEY_PATTERN = re.compile(
    r"(?:token|secret|password|authorization|api[_-]?key|credential)", re.I,
)
SECRET_VALUE_PATTERN = re.compile(
    r"(?:shpat_|sk_(?:live|test)_|rk_(?:live|test)_|bearer\s+)", re.I,
)


class ShopifyMonthlyShadowRequestError(ValueError):
    """Raised when a monthly Shopify Shadow request is unsafe or incomplete."""


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _period_bounds(period: str) -> tuple[str, str, int, int]:
    if not isinstance(period, str) or not PERIOD_PATTERN.fullmatch(period):
        raise ShopifyMonthlyShadowRequestError("period must use YYYY-MM")
    year, month = (int(item) for item in period.split("-"))
    next_year = year + (1 if month == 12 else 0)
    next_month = 1 if month == 12 else month + 1
    start = f"{period}-01T00:00:00Z"
    end = f"{next_year:04d}-{next_month:02d}-01T00:00:00Z"
    start_unix = int(datetime.fromisoformat(start.replace("Z", "+00:00")).timestamp())
    end_unix = int(datetime.fromisoformat(end.replace("Z", "+00:00")).timestamp())
    return start, end, start_unix, end_unix


def _ensure_entity_scope(runtime: BoxRuntime, entity_id: str) -> None:
    try:
        runtime.reload()
        runtime.require_capability("integration.shopify_stripe_monthly_close")
        runtime.require_connector_entity("connector.shopify", entity_id)
        runtime.require_connector_entity("connector.stripe", entity_id)
    except BoxRuntimeError as exc:
        raise ShopifyMonthlyShadowRequestError(str(exc)) from exc


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
        raise ShopifyMonthlyShadowRequestError(
            "Shopify monthly Shadow request output already exists"
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


def build_shopify_monthly_shadow_request_template(
    runtime: BoxRuntime,
    *,
    entity_id: str,
    period: str,
    output: str | Path,
) -> dict[str, Any]:
    """Create a private, incomplete request with exact entity/month boundaries."""
    _ensure_entity_scope(runtime, entity_id)
    start, end, start_unix, end_unix = _period_bounds(period)
    request = {
        "pipeline_id": PIPELINE_ID,
        "payload": {
            "entity_id": entity_id,
            "include_test_orders": False,
            "currency_minor_units": {
                "REPLACE_CURRENCY": "REPLACE_EXPONENT_0_TO_4",
            },
            "shopify_monthly_request": {
                "mode": "fetch",
                "default_entity_id": entity_id,
                "shop_domain": "REPLACE_WITH_STORE.myshopify.com",
                "interval_start": start,
                "interval_end": end,
                "max_pages": 50,
            },
            "stripe_balance_request": {
                "mode": "fetch",
                "default_entity_id": entity_id,
                "created_gte": start_unix,
                "created_lt": end_unix,
                "max_pages": 50,
            },
            "processor_links": [],
        },
    }
    destination = _write_private_json(output, request)
    return {
        "written": True,
        "output": str(destination),
        "pipeline_id": PIPELINE_ID,
        "entity_id": entity_id,
        "period": period,
        "template_fingerprint": _fingerprint(request),
        "template_only": True,
        "ready_for_network_dispatch": False,
        "operator_edits_required": [
            "shop_domain",
            "currency_minor_units",
            "processor_links",
        ],
        "exact_month_bounds_generated": True,
        "credentials_included": False,
        "financial_amounts_included": False,
        "external_actions_performed": False,
    }


def read_private_shopify_monthly_shadow_request(path: str | Path) -> dict[str, Any]:
    source = Path(path).expanduser().resolve()
    if source.is_symlink() or not source.is_file():
        raise ShopifyMonthlyShadowRequestError(
            "Shopify monthly Shadow request must be a regular non-symlink file"
        )
    metadata = source.stat()
    if os.name != "nt" and stat.S_IMODE(metadata.st_mode) != 0o600:
        raise ShopifyMonthlyShadowRequestError(
            "Shopify monthly Shadow request must use mode 0600"
        )
    if not 0 < metadata.st_size <= MAX_REQUEST_BYTES:
        raise ShopifyMonthlyShadowRequestError(
            "Shopify monthly Shadow request must be between 1 byte and 10 MiB"
        )
    try:
        request = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ShopifyMonthlyShadowRequestError(
            "Shopify monthly Shadow request must contain valid UTF-8 JSON"
        ) from exc
    if not isinstance(request, dict):
        raise ShopifyMonthlyShadowRequestError(
            "Shopify monthly Shadow request must be a JSON object"
        )
    return request


def validate_shopify_monthly_shadow_request(
    runtime: BoxRuntime,
    request: dict[str, Any],
) -> dict[str, Any]:
    """Validate a completed live request without returning store or source identifiers."""
    if not isinstance(request, dict) or set(request) != {"pipeline_id", "payload"}:
        raise ShopifyMonthlyShadowRequestError(
            "Shopify monthly Shadow request fields must be pipeline_id and payload"
        )
    if request.get("pipeline_id") != PIPELINE_ID:
        raise ShopifyMonthlyShadowRequestError(
            f"Shopify monthly Shadow request requires pipeline_id {PIPELINE_ID}"
        )
    payload = request.get("payload")
    expected_payload_fields = {
        "entity_id", "include_test_orders", "currency_minor_units",
        "shopify_monthly_request", "stripe_balance_request", "processor_links",
    }
    if not isinstance(payload, dict) or set(payload) != expected_payload_fields:
        raise ShopifyMonthlyShadowRequestError(
            "Shopify monthly Shadow request payload fields are invalid"
        )
    entity_id = payload.get("entity_id")
    if not isinstance(entity_id, str) or not entity_id:
        raise ShopifyMonthlyShadowRequestError("payload.entity_id is required")
    _ensure_entity_scope(runtime, entity_id)
    if payload.get("include_test_orders") is not False:
        raise ShopifyMonthlyShadowRequestError(
            "real Shopify monthly Shadow requests must exclude test orders"
        )
    if _contains_secret(request):
        raise ShopifyMonthlyShadowRequestError(
            "credentials and credential-like values are prohibited in the request"
        )

    shopify = payload.get("shopify_monthly_request")
    expected_shopify_fields = {
        "mode", "default_entity_id", "shop_domain", "interval_start",
        "interval_end", "max_pages",
    }
    if not isinstance(shopify, dict) or set(shopify) != expected_shopify_fields:
        raise ShopifyMonthlyShadowRequestError(
            "shopify_monthly_request fields are invalid"
        )
    domain = shopify.get("shop_domain")
    if (
        not isinstance(domain, str)
        or domain != domain.lower()
        or not SHOP_DOMAIN_PATTERN.fullmatch(domain)
        or "replace" in domain
    ):
        raise ShopifyMonthlyShadowRequestError(
            "shop_domain must be a completed lowercase *.myshopify.com domain"
        )
    start = shopify.get("interval_start")
    end = shopify.get("interval_end")
    if (
        shopify.get("mode") != "fetch"
        or shopify.get("default_entity_id") != entity_id
        or not isinstance(start, str)
        or not isinstance(end, str)
    ):
        raise ShopifyMonthlyShadowRequestError(
            "Shopify fetch mode, entity binding and UTC bounds are required"
        )
    period = start[:7]
    expected_start, expected_end, start_unix, end_unix = _period_bounds(period)
    if start != expected_start or end != expected_end:
        raise ShopifyMonthlyShadowRequestError(
            "Shopify interval must be one exact UTC calendar month"
        )
    if (
        not isinstance(shopify.get("max_pages"), int)
        or isinstance(shopify.get("max_pages"), bool)
        or not 1 <= shopify["max_pages"] <= 100
    ):
        raise ShopifyMonthlyShadowRequestError(
            "shopify_monthly_request.max_pages must be an integer from 1 to 100"
        )

    stripe = payload.get("stripe_balance_request")
    expected_stripe_fields = {
        "mode", "default_entity_id", "created_gte", "created_lt", "max_pages",
    }
    if not isinstance(stripe, dict) or set(stripe) != expected_stripe_fields:
        raise ShopifyMonthlyShadowRequestError(
            "stripe_balance_request fields are invalid"
        )
    if (
        stripe.get("mode") != "fetch"
        or stripe.get("default_entity_id") != entity_id
        or stripe.get("created_gte") != start_unix
        or stripe.get("created_lt") != end_unix
    ):
        raise ShopifyMonthlyShadowRequestError(
            "Stripe bounds must exactly match the Shopify UTC calendar month"
        )
    if (
        not isinstance(stripe.get("max_pages"), int)
        or isinstance(stripe.get("max_pages"), bool)
        or not 1 <= stripe["max_pages"] <= 100
    ):
        raise ShopifyMonthlyShadowRequestError(
            "stripe_balance_request.max_pages must be an integer from 1 to 100"
        )

    exponents = payload.get("currency_minor_units")
    if not isinstance(exponents, dict) or not exponents:
        raise ShopifyMonthlyShadowRequestError(
            "currency_minor_units must be a non-empty object"
        )
    for currency, exponent in exponents.items():
        if not isinstance(currency, str) or not CURRENCY_PATTERN.fullmatch(currency):
            raise ShopifyMonthlyShadowRequestError(
                "currency_minor_units keys must be uppercase three-letter codes"
            )
        if (
            not isinstance(exponent, int)
            or isinstance(exponent, bool)
            or not 0 <= exponent <= 4
        ):
            raise ShopifyMonthlyShadowRequestError(
                f"currency_minor_units.{currency} must be an integer from 0 to 4"
            )

    links = payload.get("processor_links")
    if not isinstance(links, list) or not links or len(links) > 100_000:
        raise ShopifyMonthlyShadowRequestError(
            "processor_links must contain 1 to 100000 explicit evidence links"
        )
    shopify_ids: set[str] = set()
    stripe_ids: set[str] = set()
    for index, link in enumerate(links, 1):
        if not isinstance(link, dict) or set(link) != {
            "entity_id", "shopify_transaction_id", "stripe_source_object_id", "evidence",
        }:
            raise ShopifyMonthlyShadowRequestError(
                f"processor_links[{index}] fields are invalid"
            )
        shopify_id = link.get("shopify_transaction_id")
        stripe_id = link.get("stripe_source_object_id")
        evidence = link.get("evidence")
        if link.get("entity_id") != entity_id:
            raise ShopifyMonthlyShadowRequestError(
                f"processor_links[{index}] is outside the requested legal entity"
            )
        if not isinstance(shopify_id, str) or not SHOPIFY_TRANSACTION_PATTERN.fullmatch(shopify_id):
            raise ShopifyMonthlyShadowRequestError(
                f"processor_links[{index}].shopify_transaction_id is invalid"
            )
        if not isinstance(stripe_id, str) or not STRIPE_SOURCE_PATTERN.fullmatch(stripe_id):
            raise ShopifyMonthlyShadowRequestError(
                f"processor_links[{index}].stripe_source_object_id is invalid"
            )
        if shopify_id in shopify_ids or stripe_id in stripe_ids:
            raise ShopifyMonthlyShadowRequestError(
                "processor_links must be one-to-one without repeated source identifiers"
            )
        shopify_ids.add(shopify_id)
        stripe_ids.add(stripe_id)
        if not isinstance(evidence, dict) or set(evidence) != {"source_file", "batch_id"}:
            raise ShopifyMonthlyShadowRequestError(
                f"processor_links[{index}].evidence fields are invalid"
            )
        for field in ("source_file", "batch_id"):
            value = evidence.get(field)
            if not isinstance(value, str) or not value.strip() or len(value) > 512:
                raise ShopifyMonthlyShadowRequestError(
                    f"processor_links[{index}].evidence.{field} is invalid"
                )

    return {
        "valid": True,
        "pipeline_id": PIPELINE_ID,
        "entity_id": entity_id,
        "period": period,
        "request_fingerprint": _fingerprint(request),
        "currency_count": len(exponents),
        "processor_link_count": len(links),
        "exact_month_bounds": True,
        "same_window_stripe_bounds": True,
        "test_orders_excluded": True,
        "credentials_included": False,
        "store_domain_returned": False,
        "raw_source_ids_returned": False,
        "financial_amounts_returned": False,
        "network_access_performed": False,
        "external_actions_performed": False,
    }


def verify_private_shopify_monthly_shadow_request(
    runtime: BoxRuntime,
    path: str | Path,
) -> dict[str, Any]:
    return validate_shopify_monthly_shadow_request(
        runtime,
        read_private_shopify_monthly_shadow_request(path),
    )
