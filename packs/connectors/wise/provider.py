from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
from datetime import datetime, timezone
from typing import Any

from src.bank_import import mask_embedded_account_numbers
from src.connector_http import fetch_wise_business_json, urllib_transport
from src.connector_sdk import (
    ConnectorContext,
    ConnectorDefinition,
    ConnectorError,
    ConnectorRegistry,
    ConnectorSyncWindow,
)


WISE_ACCESS_TOKEN_ENV = "OPC_WISE_ACCESS_TOKEN"
WISE_ENTITY_BINDINGS_ENV = "OPC_WISE_ENTITY_BINDINGS_JSON"
WISE_API_VERSION = "2026Q3"
HTTP_TRANSPORT = urllib_transport
HTTP_SLEEPER = time.sleep
PERSONAL_TOKEN_JURISDICTIONS = frozenset({"US", "CA", "AU", "NZ", "SG", "MY"})
ACCESS_CONTRACTS = frozenset({"personal_token_eligible", "wise_partner_approved"})
_INLINE_SECRET_FIELDS = {
    "token", "access_token", "api_key", "secret", "password", "authorization",
    "profile_id", "balance_id", "business_name", "access_contract", "balances",
}


def _fingerprint(value: Any) -> str:
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def _positive_integer(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise ConnectorError(f"Wise {field} must be a positive integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ConnectorError(f"Wise {field} must be a positive integer") from exc
    if result <= 0 or str(result) != str(value):
        raise ConnectorError(f"Wise {field} must be a canonical positive integer")
    return result


def _utc_timestamp(value: Any, field: str) -> tuple[str, datetime]:
    text = str(value or "").strip()
    if not re.fullmatch(r"20\d{2}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", text):
        raise ConnectorError(f"Wise {field} must use UTC YYYY-MM-DDTHH:MM:SSZ")
    try:
        parsed = datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise ConnectorError(f"Wise {field} must be a real UTC timestamp") from exc
    return text, parsed


def _currency(value: Any, field: str) -> str:
    result = str(value or "").upper()
    if not re.fullmatch(r"[A-Z]{3}", result):
        raise ConnectorError(f"Wise {field} must be a three-letter currency")
    return result


def _amount(value: Any, field: str, *, positive: bool = False) -> float:
    raw = value.get("value") if isinstance(value, dict) else value
    if isinstance(raw, bool):
        raise ValueError(f"{field} must be numeric")
    try:
        result = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be numeric") from exc
    if not math.isfinite(result) or (positive and result <= 0):
        qualifier = "positive " if positive else ""
        raise ValueError(f"{field} must be a finite {qualifier}amount")
    return round(result, 2)


def _money(value: Any, field: str, currency: str, *, positive: bool = False) -> float:
    if not isinstance(value, dict) or str(value.get("currency") or "").upper() != currency:
        raise ValueError(f"{field} currency must match the bound Wise balance")
    return _amount(value, field, positive=positive)


def _validate_binding(selected: Any, jurisdiction: str, currency: str) -> dict[str, Any]:
    if not isinstance(selected, dict) or set(selected) != {
        "profile_id", "business_name", "access_contract", "balances",
    }:
        raise ConnectorError(
            "Wise entity binding requires profile_id, business_name, access_contract and balances"
        )
    profile_id = _positive_integer(selected.get("profile_id"), "profile_id")
    business_name = str(selected.get("business_name") or "").strip()
    if not business_name or len(business_name) > 200:
        raise ConnectorError("Wise business_name binding is invalid")
    access_contract = str(selected.get("access_contract") or "")
    if access_contract not in ACCESS_CONTRACTS:
        raise ConnectorError("Wise access_contract is not supported")
    if access_contract == "personal_token_eligible" and jurisdiction not in PERSONAL_TOKEN_JURISDICTIONS:
        raise ConnectorError(
            "Wise personal-token balance statements are not eligible for this entity jurisdiction"
        )
    balances = selected.get("balances")
    if not isinstance(balances, dict) or currency not in balances:
        raise ConnectorError("Wise entity binding is missing the requested currency balance")
    balance = balances[currency]
    if not isinstance(balance, dict) or set(balance) != {"balance_id", "account_reference_masked"}:
        raise ConnectorError(
            "Wise currency balance binding requires balance_id and account_reference_masked"
        )
    balance_id = _positive_integer(balance.get("balance_id"), "balance_id")
    account_masked = str(balance.get("account_reference_masked") or "").strip()
    if (
        not account_masked or len(account_masked) > 80
        or re.search(r"(?<!\d)\d{9,}(?!\d)", account_masked)
    ):
        raise ConnectorError("Wise account_reference_masked must not expose a full account number")
    return {
        "profile_id": profile_id,
        "business_name": business_name,
        "access_contract": access_contract,
        "balance_id": balance_id,
        "account_reference_masked": account_masked,
    }


def _binding(entity_id: str, jurisdiction: str, currency: str) -> dict[str, Any]:
    raw = os.environ.get(WISE_ENTITY_BINDINGS_ENV, "")
    if not raw:
        raise ConnectorError("Wise entity binding configuration is missing")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConnectorError("Wise entity binding configuration is invalid JSON") from exc
    selected = payload.get(entity_id) if isinstance(payload, dict) else None
    return _validate_binding(selected, jurisdiction, currency)


def _fixture_binding(request: dict[str, Any], jurisdiction: str, currency: str) -> dict[str, Any]:
    selected = request.get("fixture_binding")
    if not isinstance(selected, dict) or set(selected) != {
        "profile_id", "business_name", "access_contract", "balance_id",
        "account_reference_masked",
    }:
        raise ConnectorError("Wise fixture mode requires an explicit strict fixture_binding")
    wrapped = {
        "profile_id": selected.get("profile_id"),
        "business_name": selected.get("business_name"),
        "access_contract": selected.get("access_contract"),
        "balances": {currency: {
            "balance_id": selected.get("balance_id"),
            "account_reference_masked": selected.get("account_reference_masked"),
        }},
    }
    return _validate_binding(wrapped, jurisdiction, currency)


def _profile(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ConnectorError("Wise profile response must be an object")
    profile_id = _positive_integer(payload.get("id"), "profile response id")
    profile_type = str(payload.get("type") or "").upper()
    business_name = str(payload.get("businessName") or "").strip()
    if profile_type != "BUSINESS" or not business_name:
        raise ConnectorError("Wise profile must be a named BUSINESS profile")
    return {"profile_id": profile_id, "business_name": business_name}


def _balance(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ConnectorError("Wise balance response must be an object")
    balance_id = _positive_integer(payload.get("id"), "balance response id")
    currency = _currency(payload.get("currency"), "balance response currency")
    balance_type = str(payload.get("type") or "").upper()
    if balance_type not in {"STANDARD", "SAVINGS"}:
        raise ConnectorError("Wise balance type must be STANDARD or SAVINGS")
    return {"balance_id": balance_id, "currency": currency, "balance_type": balance_type}


def _safe_text(value: Any) -> str:
    return mask_embedded_account_numbers(value)[:160]


def _details_text(details: Any, *, counterparty: bool) -> str:
    if not isinstance(details, dict):
        return ""
    if counterparty:
        candidates: list[Any] = [details.get("senderName"), details.get("recipientName")]
        for field in ("merchant", "recipient", "sender"):
            nested = details.get(field)
            if isinstance(nested, dict):
                candidates.append(nested.get("name"))
    else:
        candidates = [
            details.get("description"), details.get("paymentReference"),
            details.get("category"), details.get("type"),
        ]
    return next((_safe_text(item) for item in candidates if str(item or "").strip()), "")


def _statement(
    payload: Any, *, profile_id: int, balance_id: int, currency: str,
    interval_start: str, interval_end: str,
) -> tuple[list[dict[str, Any]], float, float]:
    if not isinstance(payload, dict):
        raise ConnectorError("Wise balance statement response must be an object")
    holder = payload.get("accountHolder")
    if not isinstance(holder, dict) or str(holder.get("type") or "").upper() != "BUSINESS":
        raise ConnectorError("Wise balance statement accountHolder must be BUSINESS")
    query = payload.get("query")
    if not isinstance(query, dict):
        raise ConnectorError("Wise balance statement is missing its query echo")
    if str(query.get("currency") or "").upper() != currency:
        raise ConnectorError("Wise balance statement query currency does not match the binding")
    query_account = query.get("accountId")
    if query_account is not None and _positive_integer(query_account, "statement accountId") != balance_id:
        raise ConnectorError("Wise balance statement query accountId does not match the binding")
    if query.get("intervalStart") != interval_start or query.get("intervalEnd") != interval_end:
        raise ConnectorError("Wise balance statement query window does not match the request")
    opening = _money(payload.get("startOfStatementBalance"), "startOfStatementBalance", currency)
    closing = _money(payload.get("endOfStatementBalance"), "endOfStatementBalance", currency)
    transactions = payload.get("transactions")
    if not isinstance(transactions, list):
        raise ConnectorError("Wise balance statement transactions must be a list")
    return transactions, opening, closing


def _map_transaction(
    raw: Any, *, entity_id: str, profile_id: int, balance_id: int, currency: str,
    account_masked: str, interval_start: datetime, interval_end: datetime,
    batch_id: str, row_number: int, profile_binding_hash: str, balance_binding_hash: str,
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("Wise transaction must be an object")
    transaction_type = str(raw.get("type") or "").upper()
    if transaction_type not in {"DEBIT", "CREDIT"}:
        raise ValueError("Wise transaction type must be DEBIT or CREDIT")
    date_text = str(raw.get("date") or "")
    try:
        transaction_time = datetime.fromisoformat(date_text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Wise transaction date must be ISO-8601") from exc
    if transaction_time.tzinfo is None:
        raise ValueError("Wise transaction date must include a timezone")
    transaction_time = transaction_time.astimezone(timezone.utc)
    if not interval_start <= transaction_time < interval_end:
        raise ValueError("Wise transaction date falls outside the requested half-open interval")
    reference = str(raw.get("referenceNumber") or "").strip()
    if not reference or len(reference) > 200:
        raise ValueError("Wise transaction requires a bounded referenceNumber")
    amount = _money(raw.get("amount"), "amount", currency, positive=True)
    balance = _money(raw.get("runningBalance"), "runningBalance", currency)
    fees = _money(raw.get("totalFees"), "totalFees", currency) if raw.get("totalFees") else 0.0
    reference_fingerprint = _fingerprint(reference)
    transaction_id = hashlib.sha256(
        f"wise|{entity_id}|{profile_id}|{balance_id}|{reference}".encode()
    ).hexdigest()[:24]
    direction_code = "inflow" if transaction_type == "CREDIT" else "outflow"
    return {
        "id": transaction_id[:16],
        "bank_transaction_id": transaction_id,
        "transaction_id": transaction_id,
        "entity_id": entity_id,
        "transaction_date": transaction_time.date().isoformat(),
        "account_masked": account_masked,
        "counterparty": _details_text(raw.get("details"), counterparty=True),
        "counterparty_account_masked": "",
        "summary": _details_text(raw.get("details"), counterparty=False),
        "direction": "收入" if direction_code == "inflow" else "支出",
        "direction_code": direction_code,
        "currency": currency,
        "amount": amount,
        "balance": balance,
        "source_file": "api:wise",
        "source_sheet": "balance_statement",
        "source_row": row_number,
        "status": "待人工确认",
        "evidence": {
            "source_file": "api:wise",
            "source_sheet": "balance_statement",
            "source_row": row_number,
            "batch_id": batch_id,
            "api_version": WISE_API_VERSION,
            "source_object_fingerprint": reference_fingerprint,
            "profile_binding_hash": profile_binding_hash,
            "balance_binding_hash": balance_binding_hash,
            "fee_amount": fees,
        },
    }


def _handler(request: dict[str, Any], context: ConnectorContext) -> dict[str, Any]:
    if any(str(key).lower() in _INLINE_SECRET_FIELDS for key in request):
        raise ConnectorError("Wise credentials and entity bindings must not be passed in connector requests")
    allowed_fields = {
        "mode", "default_entity_id", "currency", "interval_start", "interval_end",
        "fixture_binding", "profile", "balance", "statement",
    }
    unknown = sorted(set(request) - allowed_fields)
    if unknown:
        raise ConnectorError("Wise request contains unsupported fields: " + ", ".join(unknown))
    entity_id = str(request.get("default_entity_id") or "")
    if entity_id not in context.allowed_entity_ids:
        raise ConnectorError("Wise connector requires a valid default_entity_id")
    entity = context.runtime.entities.get(entity_id)
    currency = _currency(request.get("currency"), "currency")
    if currency != entity.functional_currency.upper():
        raise ConnectorError("Wise statement currency must equal the Box entity functional currency")
    interval_start, start_time = _utc_timestamp(request.get("interval_start"), "interval_start")
    interval_end, end_time = _utc_timestamp(request.get("interval_end"), "interval_end")
    if end_time <= start_time:
        raise ConnectorError("Wise interval_end must be later than interval_start")
    if (end_time - start_time).total_seconds() > 366 * 86400:
        raise ConnectorError("Wise connector interval cannot exceed 366 days")

    mode = request.get("mode", "fixture")
    if mode == "fixture":
        if not {"fixture_binding", "profile", "balance", "statement"} <= set(request):
            raise ConnectorError("Wise fixture mode requires binding, profile, balance and statement fixtures")
        binding = _fixture_binding(request, entity.jurisdiction, currency)
        profile_payload = request.get("profile")
        balance_payload = request.get("balance")
        statement_payload = request.get("statement")
        source_metrics = {
            "kind": "fixture", "network_access_performed": False,
            "retry_count": 0, "rate_limit_count": 0,
            "retry_delay_seconds_total": 0.0, "retry_after_honored": False,
        }
    elif mode == "fetch":
        forbidden_fixture_fields = sorted(
            {"fixture_binding", "profile", "balance", "statement"}.intersection(request)
        )
        if forbidden_fixture_fields:
            raise ConnectorError("Wise fetch mode must not include fixture payloads")
        binding = _binding(entity_id, entity.jurisdiction, currency)
        token = os.environ.get(WISE_ACCESS_TOKEN_ENV, "")
        parameters = {
            "currency": currency,
            "intervalStart": interval_start,
            "intervalEnd": interval_end,
            "type": "COMPACT",
            "statementLocale": "en",
        }
        try:
            profile_fetch = fetch_wise_business_json(
                "profile", access_token=token, profile_id=binding["profile_id"],
                transport=HTTP_TRANSPORT, sleeper=HTTP_SLEEPER,
            )
            balance_fetch = fetch_wise_business_json(
                "balance", access_token=token, profile_id=binding["profile_id"],
                balance_id=binding["balance_id"], transport=HTTP_TRANSPORT,
                sleeper=HTTP_SLEEPER,
            )
            statement_fetch = fetch_wise_business_json(
                "balance_statement", access_token=token, profile_id=binding["profile_id"],
                balance_id=binding["balance_id"], parameters=parameters,
                transport=HTTP_TRANSPORT, sleeper=HTTP_SLEEPER,
            )
        except Exception as exc:
            raise ConnectorError(str(exc)) from exc
        profile_payload = profile_fetch["payload"]
        balance_payload = balance_fetch["payload"]
        statement_payload = statement_fetch["payload"]
        fetches = (profile_fetch, balance_fetch, statement_fetch)
        source_metrics = {
            "kind": "api", "network_access_performed": True,
            "retry_count": sum(item["retry_count"] for item in fetches),
            "rate_limit_count": sum(item["rate_limit_count"] for item in fetches),
            "retry_delay_seconds_total": sum(
                item["retry_delay_seconds_total"] for item in fetches
            ),
            "retry_after_honored": any(item["retry_after_honored"] for item in fetches),
        }
    else:
        raise ConnectorError("Wise connector mode must be fixture or fetch")

    profile = _profile(profile_payload)
    balance = _balance(balance_payload)
    if profile["profile_id"] != binding["profile_id"]:
        raise ConnectorError("Wise profile response does not match the bound legal entity")
    if profile["business_name"] != binding["business_name"]:
        raise ConnectorError("Wise business profile name does not match the bound legal entity")
    if balance["balance_id"] != binding["balance_id"] or balance["currency"] != currency:
        raise ConnectorError("Wise balance response does not match the bound currency account")
    transactions, opening_balance, closing_balance = _statement(
        statement_payload, profile_id=binding["profile_id"], balance_id=binding["balance_id"],
        currency=currency, interval_start=interval_start, interval_end=interval_end,
    )
    canonical = json.dumps({
        "entity_id": entity_id, "currency": currency,
        "interval_start": interval_start, "interval_end": interval_end,
        "profile": profile_payload, "balance": balance_payload, "statement": statement_payload,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    batch_id = hashlib.sha256(canonical.encode()).hexdigest()[:24]
    profile_binding_hash = _fingerprint(binding["profile_id"])
    balance_binding_hash = _fingerprint({
        "profile_id": binding["profile_id"], "balance_id": binding["balance_id"],
        "currency": currency,
    })
    rows: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for row_number, raw in enumerate(transactions, 1):
        try:
            rows.append(_map_transaction(
                raw, entity_id=entity_id, profile_id=binding["profile_id"],
                balance_id=binding["balance_id"], currency=currency,
                account_masked=binding["account_reference_masked"],
                interval_start=start_time, interval_end=end_time, batch_id=batch_id,
                row_number=row_number, profile_binding_hash=profile_binding_hash,
                balance_binding_hash=balance_binding_hash,
            ))
        except (TypeError, ValueError, ConnectorError) as exc:
            rejected.append({
                "dataset_type": "finance.bank_transactions",
                "row": row_number,
                "source_sheet": "balance_statement",
                "reason": str(exc),
            })
    running_balance_validated = False
    if not rejected:
        expected_balance = opening_balance
        for row in rows:
            expected_balance = round(
                expected_balance
                + (row["amount"] if row["direction_code"] == "inflow" else -row["amount"]),
                2,
            )
            if abs(expected_balance - row["balance"]) > 0.005:
                raise ConnectorError(
                    "Wise statement running balance does not reconcile to transaction activity"
                )
        if abs(expected_balance - closing_balance) > 0.005:
            raise ConnectorError(
                "Wise statement closing balance does not reconcile to transaction activity"
            )
        running_balance_validated = True
    return {
        "batch_id": batch_id,
        "source": {
            **source_metrics,
            "name": "wise.balance_statement",
            "api_version": WISE_API_VERSION,
            "currency": currency,
            "interval_start": interval_start,
            "interval_end": interval_end,
            "account_reference_masked": binding["account_reference_masked"],
            "profile_binding_hash": profile_binding_hash,
            "balance_binding_hash": balance_binding_hash,
            "access_contract": binding["access_contract"],
            "opening_balance": opening_balance,
            "closing_balance": closing_balance,
            "statement_type": "COMPACT",
            "statement_locale": "en",
            "entity_binding_verified": True,
            "running_balance_validated": running_balance_validated,
        },
        "datasets": {"finance.bank_transactions": rows},
        "rejected_rows": rejected,
    }


def register_connectors(registry: ConnectorRegistry) -> None:
    registry.register(ConnectorDefinition(
        connector_id="wise.balance_statement",
        pack_id="connector.wise",
        capability="connector.wise_balance_statement",
        display_name="Wise Business 余额账户流水（主体绑定、只读）",
        dataset_types=("finance.bank_transactions",),
        handler=_handler,
        business_keys={"finance.bank_transactions": ("bank_transaction_id",)},
        credential_env=(WISE_ACCESS_TOKEN_ENV, WISE_ENTITY_BINDINGS_ENV),
        network_access=True,
        sync_window=ConnectorSyncWindow(
            start_field="interval_start",
            end_field="interval_end",
            value_format="iso8601",
            max_incremental_days=31,
            max_backfill_days=366,
        ),
    ))
