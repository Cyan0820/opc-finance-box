from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .box_runtime import BoxRuntime, BoxRuntimeError


MAX_REQUEST_BYTES = 256 * 1024
PIPELINE_ID = "finance.bank_statement_close"
CONNECTOR_ID = "wise.balance_statement"
PERIOD_PATTERN = re.compile(r"^20\d{2}-(?:0[1-9]|1[0-2])$")
SECRET_KEY_PATTERN = re.compile(
    r"(?:token|secret|password|authorization|api[_-]?key|credential|profile[_-]?id|balance[_-]?id)",
    re.I,
)
SECRET_VALUE_PATTERN = re.compile(r"(?:bearer\s+|eyJ[A-Za-z0-9_-]{12,})", re.I)


class WiseShadowRequestError(ValueError):
    """Raised when a Wise monthly Shadow request is unsafe or out of scope."""


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _period_bounds(period: str) -> tuple[str, str]:
    if not isinstance(period, str) or not PERIOD_PATTERN.fullmatch(period):
        raise WiseShadowRequestError("period must use YYYY-MM")
    year, month = (int(item) for item in period.split("-"))
    next_year = year + (1 if month == 12 else 0)
    next_month = 1 if month == 12 else month + 1
    return (
        f"{period}-01T00:00:00Z",
        f"{next_year:04d}-{next_month:02d}-01T00:00:00Z",
    )


def _ensure_entity_scope(runtime: BoxRuntime, entity_id: str) -> str:
    try:
        runtime.reload()
        runtime.require_capability("finance.bank_reconciliation")
        runtime.require_capability("connector.wise_balance_statement")
        runtime.require_connector_entity("connector.wise", entity_id)
        runtime.require_entity(entity_id)
        entity = runtime.entities.get(entity_id)
    except BoxRuntimeError as exc:
        raise WiseShadowRequestError(str(exc)) from exc
    return entity.functional_currency.upper()


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
        raise WiseShadowRequestError(
            "Wise Shadow request output already exists"
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


def build_wise_shadow_request(
    runtime: BoxRuntime,
    *,
    entity_id: str,
    period: str,
    output: str | Path,
) -> dict[str, Any]:
    """Create a complete secret-free monthly Wise request with no manual JSON editing."""
    currency = _ensure_entity_scope(runtime, entity_id)
    interval_start, interval_end = _period_bounds(period)
    request = {
        "pipeline_id": PIPELINE_ID,
        "payload": {
            "entity_id": entity_id,
            "period": period,
            "connector_id": CONNECTOR_ID,
            "connector_request": {
                "mode": "fetch",
                "default_entity_id": entity_id,
                "currency": currency,
                "interval_start": interval_start,
                "interval_end": interval_end,
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
        "exact_month_bounds_generated": True,
        "functional_currency_bound": True,
        "credential_configuration_checked": False,
        "credentials_included": False,
        "financial_amounts_included": False,
        "account_references_included": False,
        "external_actions_performed": False,
    }


def read_private_wise_shadow_request(path: str | Path) -> dict[str, Any]:
    source = Path(path).expanduser().resolve()
    if source.is_symlink() or not source.is_file():
        raise WiseShadowRequestError(
            "Wise Shadow request must be a regular non-symlink file"
        )
    metadata = source.stat()
    if os.name != "nt" and stat.S_IMODE(metadata.st_mode) != 0o600:
        raise WiseShadowRequestError("Wise Shadow request must use mode 0600")
    if not 0 < metadata.st_size <= MAX_REQUEST_BYTES:
        raise WiseShadowRequestError(
            "Wise Shadow request must be between 1 byte and 256 KiB"
        )
    try:
        request = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WiseShadowRequestError(
            "Wise Shadow request must contain valid UTF-8 JSON"
        ) from exc
    if not isinstance(request, dict):
        raise WiseShadowRequestError("Wise Shadow request must be a JSON object")
    return request


def validate_wise_shadow_request(
    runtime: BoxRuntime,
    request: dict[str, Any],
) -> dict[str, Any]:
    """Validate one complete Wise request without touching credentials or the network."""
    if not isinstance(request, dict) or set(request) != {"pipeline_id", "payload"}:
        raise WiseShadowRequestError(
            "Wise Shadow request fields must be pipeline_id and payload"
        )
    if request.get("pipeline_id") != PIPELINE_ID:
        raise WiseShadowRequestError(
            f"Wise Shadow request requires pipeline_id {PIPELINE_ID}"
        )
    payload = request.get("payload")
    if not isinstance(payload, dict) or set(payload) != {
        "entity_id", "period", "connector_id", "connector_request",
    }:
        raise WiseShadowRequestError("Wise Shadow request payload fields are invalid")
    if _contains_secret(request):
        raise WiseShadowRequestError(
            "credentials and Wise account bindings are prohibited in the request"
        )
    entity_id = payload.get("entity_id")
    if not isinstance(entity_id, str) or not entity_id:
        raise WiseShadowRequestError("payload.entity_id is required")
    currency = _ensure_entity_scope(runtime, entity_id)
    period = payload.get("period")
    interval_start, interval_end = _period_bounds(period)
    if payload.get("connector_id") != CONNECTOR_ID:
        raise WiseShadowRequestError(
            f"Wise Shadow request requires connector_id {CONNECTOR_ID}"
        )
    connector_request = payload.get("connector_request")
    if not isinstance(connector_request, dict) or set(connector_request) != {
        "mode", "default_entity_id", "currency", "interval_start", "interval_end",
    }:
        raise WiseShadowRequestError(
            "Wise Shadow connector_request fields are invalid"
        )
    if connector_request != {
        "mode": "fetch",
        "default_entity_id": entity_id,
        "currency": currency,
        "interval_start": interval_start,
        "interval_end": interval_end,
    }:
        raise WiseShadowRequestError(
            "Wise Shadow request must use fetch mode, functional currency and exact month bounds"
        )
    try:
        start = datetime.strptime(interval_start, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
        end = datetime.strptime(interval_end, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError as exc:
        raise WiseShadowRequestError(
            "Wise Shadow request must use real UTC month bounds"
        ) from exc
    if end <= start:
        raise WiseShadowRequestError(
            "Wise Shadow request must use an increasing UTC month window"
        )
    return {
        "valid": True,
        "pipeline_id": PIPELINE_ID,
        "connector_id": CONNECTOR_ID,
        "entity_id": entity_id,
        "period": period,
        "currency": currency,
        "request_fingerprint": _fingerprint(request),
        "request_contract_complete": True,
        "exact_month_bounds": True,
        "functional_currency_bound": True,
        "credentials_included": False,
        "account_references_returned": False,
        "financial_amounts_returned": False,
        "network_access_performed": False,
        "external_actions_performed": False,
    }


def verify_private_wise_shadow_request(
    runtime: BoxRuntime,
    path: str | Path,
) -> dict[str, Any]:
    return validate_wise_shadow_request(
        runtime,
        read_private_wise_shadow_request(path),
    )
