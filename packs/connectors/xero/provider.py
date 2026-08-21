from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
from typing import Any

from src.connector_http import fetch_xero_accounting_json, urllib_transport
from src.connector_sdk import ConnectorContext, ConnectorDefinition, ConnectorError, ConnectorRegistry


XERO_ACCESS_TOKEN_ENV = "OPC_XERO_ACCESS_TOKEN"
XERO_ENTITY_BINDINGS_ENV = "OPC_XERO_ENTITY_BINDINGS_JSON"
HTTP_TRANSPORT = urllib_transport
HTTP_SLEEPER = time.sleep
_INLINE_SECRET_FIELDS = {
    "token", "access_token", "api_key", "secret", "password", "authorization",
    "tenant_id", "organisation_id", "organization_id", "xero_tenant_id",
}
_UUID = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:16]


def _as_at(value: Any) -> str:
    text = str(value or "")
    if not re.fullmatch(r"20\d{2}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])", text):
        raise ConnectorError("Xero Trial Balance as_at must use YYYY-MM-DD")
    try:
        time.strptime(text, "%Y-%m-%d")
    except ValueError as exc:
        raise ConnectorError("Xero Trial Balance as_at must be a real calendar date") from exc
    return text


def _number(value: Any, field: str) -> float:
    if value in (None, ""):
        return 0.0
    if isinstance(value, bool):
        raise ValueError(f"{field} must be numeric")
    try:
        result = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be numeric") from exc
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"{field} must be a finite non-negative amount")
    return round(result, 2)


def _value(cell: Any) -> Any:
    if not isinstance(cell, dict):
        return None
    return cell.get("Value")


def _account_id(cell: Any) -> str:
    if not isinstance(cell, dict):
        return ""
    for attribute in cell.get("Attributes") or []:
        if isinstance(attribute, dict) and str(attribute.get("Id") or "").lower() == "account":
            return str(attribute.get("Value") or "")
    return ""


def _organisation(payload: Any) -> dict[str, Any]:
    organisations = payload.get("Organisations") if isinstance(payload, dict) else None
    if not isinstance(organisations, list) or len(organisations) != 1 or not isinstance(organisations[0], dict):
        raise ConnectorError("Xero Organisation response must contain exactly one organisation")
    organisation = organisations[0]
    organisation_id = str(organisation.get("OrganisationID") or "")
    currency = str(organisation.get("BaseCurrency") or "").upper()
    if not _UUID.fullmatch(organisation_id) or not re.fullmatch(r"[A-Z]{3}", currency):
        raise ConnectorError("Xero Organisation response is missing a valid id or base currency")
    return {"organisation_id": organisation_id, "base_currency": currency}


def _binding(entity_id: str) -> dict[str, str]:
    raw = os.environ.get(XERO_ENTITY_BINDINGS_ENV, "")
    if not raw:
        raise ConnectorError("Xero entity binding configuration is missing")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConnectorError("Xero entity binding configuration is invalid JSON") from exc
    selected = payload.get(entity_id) if isinstance(payload, dict) else None
    if not isinstance(selected, dict):
        raise ConnectorError("Xero entity binding is missing for the requested legal entity")
    if set(selected) != {"tenant_id", "organisation_id"}:
        raise ConnectorError("Xero entity binding requires only tenant_id and organisation_id")
    tenant_id = str(selected.get("tenant_id") or "")
    organisation_id = str(selected.get("organisation_id") or "")
    if not _UUID.fullmatch(tenant_id) or not _UUID.fullmatch(organisation_id):
        raise ConnectorError("Xero entity binding contains an invalid identifier")
    return {"tenant_id": tenant_id, "organisation_id": organisation_id}


def _reports(payload: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    reports = payload.get("Reports") if isinstance(payload, dict) else None
    if not isinstance(reports, list) or len(reports) != 1 or not isinstance(reports[0], dict):
        raise ConnectorError("Xero Trial Balance response must contain exactly one report")
    report = reports[0]
    rows = report.get("Rows")
    if not isinstance(rows, list):
        raise ConnectorError("Xero Trial Balance report is missing Rows")
    return report, rows


def _account_rows(rows: list[dict[str, Any]]) -> list[tuple[str, int, dict[str, Any]]]:
    output: list[tuple[str, int, dict[str, Any]]] = []
    for section_index, section in enumerate(rows, 1):
        if not isinstance(section, dict):
            continue
        candidates = section.get("Rows") if section.get("RowType") == "Section" else [section]
        if not isinstance(candidates, list):
            continue
        section_title = str(section.get("Title") or f"section-{section_index}")
        for row_index, row in enumerate(candidates, 1):
            if isinstance(row, dict) and row.get("RowType") == "Row":
                cells = row.get("Cells")
                if isinstance(cells, list) and cells and _account_id(cells[0]):
                    output.append((section_title, row_index, row))
    return output


def _map_row(
    row: dict[str, Any], *, entity_id: str, period: str, as_at: str, currency: str,
    batch_id: str, section: str, row_number: int, tenant_binding_hash: str,
    organisation_binding_hash: str,
) -> dict[str, Any]:
    cells = row.get("Cells")
    if not isinstance(cells, list) or len(cells) < 5:
        raise ValueError("Xero account row requires Account, Debit, Credit, YTD Debit and YTD Credit cells")
    account_id = _account_id(cells[0])
    label = str(_value(cells[0]) or "").strip()
    match = re.fullmatch(r"(.+?)\s*\(([^()]+)\)\s*", label)
    if not account_id or not match:
        raise ValueError("Xero account row requires an account id and a final '(account code)' suffix")
    account_name = match.group(1).strip()
    account_code = match.group(2).strip()
    if not account_name or not account_code or len(account_code) > 100:
        raise ValueError("Xero account name or code is invalid")
    closing_debit = _number(_value(cells[1]), "Debit")
    closing_credit = _number(_value(cells[2]), "Credit")
    if closing_debit and closing_credit:
        raise ValueError("Xero closing debit and credit cannot both be non-zero")
    line_id = hashlib.sha256(
        f"xero|{entity_id}|{period}|{currency}|{account_id}|{account_code}".encode()
    ).hexdigest()[:16]
    return {
        "line_id": line_id,
        "entity_id": entity_id,
        "period": period,
        "currency": currency,
        "account_code": account_code,
        "account_name": account_name,
        "opening_debit": 0.0,
        "opening_credit": 0.0,
        "period_debit": 0.0,
        "period_credit": 0.0,
        "closing_debit": closing_debit,
        "closing_credit": closing_credit,
        "xero_ytd_debit": _number(_value(cells[3]), "YTD Debit"),
        "xero_ytd_credit": _number(_value(cells[4]), "YTD Credit"),
        "evidence": {
            "source_file": "api:xero",
            "source_sheet": section,
            "source_row": row_number,
            "source_object_id_sha256": hashlib.sha256(account_id.encode()).hexdigest(),
            "batch_id": batch_id,
            "as_at": as_at,
            "tenant_binding_hash": tenant_binding_hash,
            "organisation_binding_hash": organisation_binding_hash,
        },
    }


def _handler(request: dict[str, Any], context: ConnectorContext) -> dict[str, Any]:
    if any(str(key).lower() in _INLINE_SECRET_FIELDS for key in request):
        raise ConnectorError("Xero credentials and entity bindings must not be passed in connector requests")
    entity_id = str(request.get("default_entity_id") or "")
    if entity_id not in context.allowed_entity_ids:
        raise ConnectorError("Xero connector requires a valid default_entity_id")
    as_at = _as_at(request.get("as_at"))
    period = as_at[:7]
    if request.get("default_period") not in (None, "", period):
        raise ConnectorError("Xero as_at does not match default_period")
    expected_currency = context.runtime.entities.get(entity_id).functional_currency.upper()
    payments_only = request.get("payments_only", False)
    if not isinstance(payments_only, bool):
        raise ConnectorError("payments_only must be boolean")
    mode = request.get("mode", "fixture")
    if mode == "fixture":
        organisation_payload = request.get("organisation")
        report_payload = request.get("report")
        binding = request.get("fixture_binding")
        if not isinstance(binding, dict) or set(binding) != {"tenant_id", "organisation_id"}:
            raise ConnectorError("Xero fixture mode requires an explicit fixture_binding")
        tenant_id = str(binding.get("tenant_id") or "")
        bound_organisation_id = str(binding.get("organisation_id") or "")
        if not _UUID.fullmatch(tenant_id) or not _UUID.fullmatch(bound_organisation_id):
            raise ConnectorError("Xero fixture binding contains an invalid identifier")
        source_metrics = {
            "kind": "fixture", "network_access_performed": False,
            "retry_count": 0, "rate_limit_count": 0,
            "retry_delay_seconds_total": 0.0, "retry_after_honored": False,
        }
    elif mode == "fetch":
        binding = _binding(entity_id)
        tenant_id = binding["tenant_id"]
        bound_organisation_id = binding["organisation_id"]
        token = os.environ.get(XERO_ACCESS_TOKEN_ENV, "")
        try:
            organisation_fetch = fetch_xero_accounting_json(
                "organisation", access_token=token, tenant_id=tenant_id,
                transport=HTTP_TRANSPORT, sleeper=HTTP_SLEEPER,
            )
            report_fetch = fetch_xero_accounting_json(
                "trial_balance", access_token=token, tenant_id=tenant_id,
                parameters={"date": as_at, "paymentsOnly": str(payments_only).lower()},
                transport=HTTP_TRANSPORT, sleeper=HTTP_SLEEPER,
            )
        except Exception as exc:
            raise ConnectorError(str(exc)) from exc
        organisation_payload = organisation_fetch["payload"]
        report_payload = report_fetch["payload"]
        source_metrics = {
            "kind": "api", "network_access_performed": True,
            "retry_count": organisation_fetch["retry_count"] + report_fetch["retry_count"],
            "rate_limit_count": organisation_fetch["rate_limit_count"] + report_fetch["rate_limit_count"],
            "retry_delay_seconds_total": (
                organisation_fetch["retry_delay_seconds_total"]
                + report_fetch["retry_delay_seconds_total"]
            ),
            "retry_after_honored": (
                organisation_fetch["retry_after_honored"]
                or report_fetch["retry_after_honored"]
            ),
        }
    else:
        raise ConnectorError("Xero connector mode must be fixture or fetch")

    organisation = _organisation(organisation_payload)
    if organisation["organisation_id"] != bound_organisation_id:
        raise ConnectorError("Xero Organisation response does not match the bound legal entity")
    if organisation["base_currency"] != expected_currency:
        raise ConnectorError("Xero Organisation base currency does not match the Box legal entity")
    report, report_rows = _reports(report_payload)
    canonical = json.dumps({
        "entity_id": entity_id,
        "as_at": as_at,
        "payments_only": payments_only,
        "organisation": organisation_payload,
        "report": report_payload,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    batch_id = hashlib.sha256(canonical.encode()).hexdigest()[:24]
    tenant_binding_hash = _fingerprint(tenant_id)
    organisation_binding_hash = _fingerprint(bound_organisation_id)
    rows: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for section, row_number, raw in _account_rows(report_rows):
        try:
            mapped = _map_row(
                raw, entity_id=entity_id, period=period, as_at=as_at,
                currency=expected_currency,
                batch_id=batch_id, section=section, row_number=row_number,
                tenant_binding_hash=tenant_binding_hash,
                organisation_binding_hash=organisation_binding_hash,
            )
            rows.append(mapped)
        except (TypeError, ValueError) as exc:
            rejected.append({
                "dataset_type": "finance.trial_balance_lines",
                "row": row_number,
                "source_sheet": section,
                "reason": str(exc),
            })
    return {
        "batch_id": batch_id,
        "source": {
            **source_metrics,
            "name": "xero.trial_balance",
            "as_at": as_at,
            "payments_only": payments_only,
            "report_id": report.get("ReportID"),
            "report_title": report.get("ReportTitle"),
            "tenant_binding_hash": tenant_binding_hash,
            "organisation_binding_hash": organisation_binding_hash,
            "base_currency": expected_currency,
            "point_in_time_snapshot": True,
            "opening_and_period_movements_provided": False,
            "ytd_columns_preserved_separately": True,
        },
        "datasets": {"finance.trial_balance_lines": rows},
        "rejected_rows": rejected,
    }


def register_connectors(registry: ConnectorRegistry) -> None:
    registry.register(ConnectorDefinition(
        connector_id="xero.trial_balance",
        pack_id="connector.xero",
        capability="connector.xero_trial_balance",
        display_name="Xero Trial Balance（主体绑定、只读快照）",
        dataset_types=("finance.trial_balance_lines",),
        handler=_handler,
        business_keys={"finance.trial_balance_lines": ("line_id",)},
        credential_env=(XERO_ACCESS_TOKEN_ENV, XERO_ENTITY_BINDINGS_ENV),
        network_access=True,
        sync_window=None,
    ))
