from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .box_runtime import BoxRuntime, BoxRuntimeError


MAX_REQUEST_BYTES = 50 * 1024 * 1024
MAX_BANK_TRANSACTIONS = 100_000
PIPELINE_ID = "stripe.daily_close"
PERIOD_PATTERN = re.compile(r"^20\d{2}-(?:0[1-9]|1[0-2])$")
CURRENCY_PATTERN = re.compile(r"^[A-Z]{3}$")
PRIVATE_TEXT_PATTERN = re.compile(r"^[^\x00-\x1f\x7f]{1,512}$")
SECRET_KEY_PATTERN = re.compile(
    r"(?:token|secret|password|authorization|api[_-]?key|credential)", re.I,
)
SECRET_VALUE_PATTERN = re.compile(
    r"(?:sk_(?:live|test)_|rk_(?:live|test)_|bearer\s+)", re.I,
)


class StripeShadowRequestError(ValueError):
    """Raised when a private Stripe Shadow request is unsafe or incomplete."""


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _period_bounds(period: str) -> tuple[int, int, date, date]:
    if not isinstance(period, str) or not PERIOD_PATTERN.fullmatch(period):
        raise StripeShadowRequestError("period must use YYYY-MM")
    year, month = (int(item) for item in period.split("-"))
    next_year = year + (1 if month == 12 else 0)
    next_month = 1 if month == 12 else month + 1
    start_date = date(year, month, 1)
    end_date = date(next_year, next_month, 1)
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    end = datetime(next_year, next_month, 1, tzinfo=timezone.utc)
    return int(start.timestamp()), int(end.timestamp()), start_date, end_date


def _ensure_entity_scope(runtime: BoxRuntime, entity_id: str) -> None:
    try:
        runtime.reload()
        runtime.require_capability("connector.stripe_balance_transactions")
        runtime.require_capability("connector.stripe_payouts")
        runtime.require_connector_entity("connector.stripe", entity_id)
    except BoxRuntimeError as exc:
        raise StripeShadowRequestError(str(exc)) from exc


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
        raise StripeShadowRequestError(
            "Stripe Shadow request output already exists"
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


def build_stripe_shadow_request_template(
    runtime: BoxRuntime,
    *,
    entity_id: str,
    period: str,
    output: str | Path,
) -> dict[str, Any]:
    """Create a private incomplete Stripe read plus bank-evidence request."""
    _ensure_entity_scope(runtime, entity_id)
    created_gte, created_lt, _, _ = _period_bounds(period)
    request = {
        "pipeline_id": PIPELINE_ID,
        "payload": {
            "entity_id": entity_id,
            "arrival_date_tolerance_days": 3,
            "balance_request": {
                "mode": "fetch",
                "default_entity_id": entity_id,
                "created_gte": created_gte,
                "created_lt": created_lt,
                "max_pages": 50,
            },
            "payout_request": {
                "mode": "fetch",
                "default_entity_id": entity_id,
                "created_gte": created_gte,
                "created_lt": created_lt,
                "max_pages": 50,
            },
            "bank_transactions": [],
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
        "operator_edits_required": ["bank_transactions"],
        "exact_month_bounds_generated": True,
        "credentials_included": False,
        "financial_amounts_included": False,
        "external_actions_performed": False,
    }


def read_private_stripe_shadow_request(path: str | Path) -> dict[str, Any]:
    source = Path(path).expanduser().resolve()
    if source.is_symlink() or not source.is_file():
        raise StripeShadowRequestError(
            "Stripe Shadow request must be a regular non-symlink file"
        )
    metadata = source.stat()
    if os.name != "nt" and stat.S_IMODE(metadata.st_mode) != 0o600:
        raise StripeShadowRequestError("Stripe Shadow request must use mode 0600")
    if not 0 < metadata.st_size <= MAX_REQUEST_BYTES:
        raise StripeShadowRequestError(
            "Stripe Shadow request must be between 1 byte and 50 MiB"
        )
    try:
        request = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StripeShadowRequestError(
            "Stripe Shadow request must contain valid UTF-8 JSON"
        ) from exc
    if not isinstance(request, dict):
        raise StripeShadowRequestError("Stripe Shadow request must be a JSON object")
    return request


def _validate_fetch_request(
    value: Any,
    *,
    name: str,
    entity_id: str,
    expected_gte: int | None = None,
    expected_lt: int | None = None,
) -> tuple[int, int]:
    expected_fields = {
        "mode", "default_entity_id", "created_gte", "created_lt", "max_pages",
    }
    if not isinstance(value, dict) or set(value) != expected_fields:
        raise StripeShadowRequestError(f"{name} fields are invalid")
    created_gte = value.get("created_gte")
    created_lt = value.get("created_lt")
    if (
        value.get("mode") != "fetch"
        or value.get("default_entity_id") != entity_id
        or not isinstance(created_gte, int)
        or isinstance(created_gte, bool)
        or not isinstance(created_lt, int)
        or isinstance(created_lt, bool)
        or created_gte < 0
        or created_lt <= created_gte
    ):
        raise StripeShadowRequestError(
            f"{name} requires fetch mode, exact entity binding and valid Unix bounds"
        )
    if (
        expected_gte is not None
        and (created_gte != expected_gte or created_lt != expected_lt)
    ):
        raise StripeShadowRequestError(
            "Stripe Balance Transaction and Payout requests must use identical bounds"
        )
    max_pages = value.get("max_pages")
    if (
        not isinstance(max_pages, int)
        or isinstance(max_pages, bool)
        or not 1 <= max_pages <= 100
    ):
        raise StripeShadowRequestError(f"{name}.max_pages must be an integer from 1 to 100")
    return created_gte, created_lt


def validate_stripe_shadow_request(
    runtime: BoxRuntime,
    request: dict[str, Any],
) -> dict[str, Any]:
    """Validate a private request without returning bank IDs, references or amounts."""
    if not isinstance(request, dict) or set(request) != {"pipeline_id", "payload"}:
        raise StripeShadowRequestError(
            "Stripe Shadow request fields must be pipeline_id and payload"
        )
    if request.get("pipeline_id") != PIPELINE_ID:
        raise StripeShadowRequestError(
            f"Stripe Shadow request requires pipeline_id {PIPELINE_ID}"
        )
    payload = request.get("payload")
    expected_payload_fields = {
        "entity_id", "arrival_date_tolerance_days", "balance_request",
        "payout_request", "bank_transactions",
    }
    if not isinstance(payload, dict) or set(payload) != expected_payload_fields:
        raise StripeShadowRequestError("Stripe Shadow request payload fields are invalid")
    entity_id = payload.get("entity_id")
    if not isinstance(entity_id, str) or not entity_id:
        raise StripeShadowRequestError("payload.entity_id is required")
    _ensure_entity_scope(runtime, entity_id)
    if _contains_secret(request):
        raise StripeShadowRequestError(
            "credentials and credential-like values are prohibited in the request"
        )
    tolerance = payload.get("arrival_date_tolerance_days")
    if (
        not isinstance(tolerance, int)
        or isinstance(tolerance, bool)
        or not 0 <= tolerance <= 7
    ):
        raise StripeShadowRequestError(
            "arrival_date_tolerance_days must be an integer from 0 to 7"
        )
    created_gte, created_lt = _validate_fetch_request(
        payload.get("balance_request"),
        name="balance_request",
        entity_id=entity_id,
    )
    _validate_fetch_request(
        payload.get("payout_request"),
        name="payout_request",
        entity_id=entity_id,
        expected_gte=created_gte,
        expected_lt=created_lt,
    )
    try:
        start = datetime.fromtimestamp(created_gte, timezone.utc)
        end = datetime.fromtimestamp(created_lt, timezone.utc)
    except (OverflowError, OSError, ValueError) as exc:
        raise StripeShadowRequestError(
            "Stripe requests must use one exact UTC calendar month"
        ) from exc
    period = start.strftime("%Y-%m")
    expected_gte, expected_lt, start_date, end_date = _period_bounds(period)
    if (
        created_gte != expected_gte
        or created_lt != expected_lt
        or start.day != 1
        or start.time().isoformat() != "00:00:00"
        or end.day != 1
        or end.time().isoformat() != "00:00:00"
    ):
        raise StripeShadowRequestError(
            "Stripe requests must use one exact UTC calendar month"
        )

    rows = payload.get("bank_transactions")
    if not isinstance(rows, list) or not rows or len(rows) > MAX_BANK_TRANSACTIONS:
        raise StripeShadowRequestError(
            "bank_transactions must contain 1 to 100000 private evidence rows"
        )
    seen_ids: set[str] = set()
    currencies: set[str] = set()
    latest_date = end_date + timedelta(days=tolerance)
    for index, row in enumerate(rows, 1):
        expected_fields = {
            "bank_transaction_id", "entity_id", "amount_minor", "currency",
            "direction", "transaction_date", "reference", "evidence",
        }
        if not isinstance(row, dict) or set(row) != expected_fields:
            raise StripeShadowRequestError(f"bank_transactions[{index}] fields are invalid")
        bank_id = row.get("bank_transaction_id")
        if (
            not isinstance(bank_id, str)
            or not PRIVATE_TEXT_PATTERN.fullmatch(bank_id)
            or bank_id in seen_ids
        ):
            raise StripeShadowRequestError(
                "bank_transaction_id values must be valid and unique"
            )
        seen_ids.add(bank_id)
        if row.get("entity_id") != entity_id:
            raise StripeShadowRequestError(
                f"bank_transactions[{index}] is outside the requested legal entity"
            )
        amount = row.get("amount_minor")
        if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
            raise StripeShadowRequestError(
                f"bank_transactions[{index}].amount_minor must be a positive integer"
            )
        currency = row.get("currency")
        if not isinstance(currency, str) or not CURRENCY_PATTERN.fullmatch(currency):
            raise StripeShadowRequestError(
                f"bank_transactions[{index}].currency must be an uppercase code"
            )
        currencies.add(currency)
        if row.get("direction") != "inflow":
            raise StripeShadowRequestError(
                f"bank_transactions[{index}].direction must be inflow"
            )
        try:
            transaction_date = date.fromisoformat(str(row.get("transaction_date") or ""))
        except ValueError as exc:
            raise StripeShadowRequestError(
                f"bank_transactions[{index}].transaction_date must use YYYY-MM-DD"
            ) from exc
        if not start_date <= transaction_date <= latest_date:
            raise StripeShadowRequestError(
                f"bank_transactions[{index}].transaction_date is outside the month and arrival tolerance"
            )
        reference = row.get("reference")
        if not isinstance(reference, str) or not PRIVATE_TEXT_PATTERN.fullmatch(reference):
            raise StripeShadowRequestError(
                f"bank_transactions[{index}].reference is invalid"
            )
        evidence = row.get("evidence")
        if not isinstance(evidence, dict) or set(evidence) != {"source_file", "batch_id"}:
            raise StripeShadowRequestError(
                f"bank_transactions[{index}].evidence fields are invalid"
            )
        for field in ("source_file", "batch_id"):
            value = evidence.get(field)
            if not isinstance(value, str) or not PRIVATE_TEXT_PATTERN.fullmatch(value):
                raise StripeShadowRequestError(
                    f"bank_transactions[{index}].evidence.{field} is invalid"
                )

    return {
        "valid": True,
        "pipeline_id": PIPELINE_ID,
        "entity_id": entity_id,
        "period": period,
        "request_fingerprint": _fingerprint(request),
        "bank_transaction_count": len(rows),
        "currency_count": len(currencies),
        "arrival_date_tolerance_days": tolerance,
        "exact_month_bounds": True,
        "same_window_balance_and_payout_bounds": True,
        "credentials_included": False,
        "bank_references_returned": False,
        "raw_source_ids_returned": False,
        "financial_amounts_returned": False,
        "network_access_performed": False,
        "external_actions_performed": False,
    }


def verify_private_stripe_shadow_request(
    runtime: BoxRuntime,
    path: str | Path,
) -> dict[str, Any]:
    return validate_stripe_shadow_request(
        runtime,
        read_private_stripe_shadow_request(path),
    )
