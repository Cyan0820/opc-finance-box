from __future__ import annotations

from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
import math
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

from .box_runtime import BoxRuntime
from .connector_access_probe import (
    CREDENTIAL_ENV_NAMES,
    ConnectorAccessProbeError,
    verify_private_connector_access_probe_receipt,
    verify_private_connector_access_probe_receipt_contract,
    verify_private_connector_access_request,
)
from .connector_entity_credentials import access_credentials_configured


class ConnectorAccessRegistryError(ValueError):
    """Raised when the expected Connector access scope contract is invalid."""


_CREDENTIAL_ENV_NAMES = CREDENTIAL_ENV_NAMES


def _verification_clock(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ConnectorAccessRegistryError("as_of must use YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise ConnectorAccessRegistryError("as_of must use canonical YYYY-MM-DD")
    return datetime.combine(parsed, time.max, timezone.utc).isoformat()


def _exists(path: Path) -> bool:
    try:
        path.lstat()
    except OSError:
        return False
    return True


def build_connector_access_registry(
    runtime: BoxRuntime,
    access_scopes: Sequence[Mapping[str, Any]],
    *,
    as_of: str | None = None,
    maximum_age_days: int = 30,
    warning_days_before_expiry: int = 7,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Project private access request/receipt pairs into a path- and secret-free state.

    One expected scope represents one Connector Pack bound to one legal entity.  A
    current receipt can therefore gate every compatible Shadow pipeline for that
    entity without repeating the provider probe.  This registry performs no network
    access and deliberately does not treat a receipt as stable-promotion evidence.
    """
    if (
        not isinstance(maximum_age_days, int)
        or isinstance(maximum_age_days, bool)
        or not 1 <= maximum_age_days <= 365
    ):
        raise ConnectorAccessRegistryError(
            "maximum_age_days must be an integer between 1 and 365"
        )
    if (
        not isinstance(warning_days_before_expiry, int)
        or isinstance(warning_days_before_expiry, bool)
        or not 0 <= warning_days_before_expiry <= maximum_age_days
    ):
        raise ConnectorAccessRegistryError(
            "warning_days_before_expiry must be an integer from 0 through maximum_age_days"
        )
    clock = _verification_clock(as_of) or datetime.now(timezone.utc).isoformat()
    clock_at = datetime.fromisoformat(clock).astimezone(timezone.utc)
    environment = os.environ if environ is None else environ
    normalized: dict[tuple[str, str], tuple[Path, Path]] = {}
    for raw in access_scopes:
        pack_id = raw.get("pack_id")
        entity_id = raw.get("entity_id")
        request = raw.get("request")
        receipt = raw.get("receipt")
        if (
            pack_id not in _CREDENTIAL_ENV_NAMES
            or not isinstance(entity_id, str)
            or not entity_id
            or not isinstance(request, Path)
            or not isinstance(receipt, Path)
            or not request.is_absolute()
            or not receipt.is_absolute()
        ):
            raise ConnectorAccessRegistryError(
                "Connector access registry scope contract is invalid"
            )
        key = (str(pack_id), entity_id)
        paths = (request, receipt)
        previous = normalized.get(key)
        if previous is not None and previous != paths:
            raise ConnectorAccessRegistryError(
                "Connector access registry scope has conflicting private paths"
            )
        normalized[key] = paths

    entries: list[dict[str, Any]] = []
    for (pack_id, entity_id), (request, receipt) in sorted(normalized.items()):
        request_present = _exists(request)
        receipt_present = _exists(receipt)
        binding_mode: str | None = None
        days_until_expiry: int | None = None
        credential_configured = (
            access_credentials_configured(pack_id, entity_id, environment)
            if pack_id in {
                "connector.paypal", "connector.woocommerce", "connector.shipbob",
                "connector.amazon_seller",
            }
            else all(
                str(environment.get(env_name) or "").strip()
                for env_name in _CREDENTIAL_ENV_NAMES[pack_id]
            )
        )
        if not request_present and not receipt_present:
            status = "not_initialized"
        elif not request_present:
            status = "blocked_orphan_receipt"
        else:
            try:
                request_verification = verify_private_connector_access_request(
                    runtime, request,
                )
                if (
                    request_verification["pack_id"] != pack_id
                    or request_verification["entity_id"] != entity_id
                ):
                    raise ConnectorAccessProbeError(
                        "Connector access request scope does not match the registry"
                    )
                binding_mode = request_verification["binding_mode"]
            except (ConnectorAccessProbeError, OSError, ValueError):
                status = "blocked_invalid_request"
            else:
                if not receipt_present:
                    status = (
                        "ready_for_authorized_probe"
                        if credential_configured
                        else "awaiting_current_credential"
                    )
                elif not credential_configured:
                    status = "blocked_missing_current_credential"
                else:
                    try:
                        verified = verify_private_connector_access_probe_receipt(
                            runtime,
                            request,
                            receipt,
                            as_of=clock,
                            maximum_age_days=maximum_age_days,
                            environ=environment,
                        )
                        if (
                            verified["pack_id"] != pack_id
                            or verified["entity_id"] != entity_id
                            or verified["binding_mode"] != binding_mode
                        ):
                            raise ConnectorAccessProbeError(
                                "Connector access receipt scope does not match the registry"
                            )
                    except (ConnectorAccessProbeError, OSError, ValueError):
                        try:
                            verify_private_connector_access_probe_receipt_contract(
                                runtime, request, receipt,
                            )
                        except (ConnectorAccessProbeError, OSError, ValueError):
                            status = "blocked_invalid_receipt"
                        else:
                            status = "renewal_required"
                    else:
                        observed_at = datetime.fromisoformat(
                            str(verified["observed_at"]).replace("Z", "+00:00")
                        ).astimezone(timezone.utc)
                        expires_at = observed_at + timedelta(days=maximum_age_days)
                        seconds_until_expiry = max(
                            0.0, (expires_at - clock_at).total_seconds(),
                        )
                        days_until_expiry = math.ceil(
                            seconds_until_expiry / 86400,
                        )
                        status = (
                            "renewal_due"
                            if expires_at - clock_at <= timedelta(
                                days=warning_days_before_expiry,
                            )
                            else "current"
                        )
        next_action_ids = {
            "not_initialized": "initialize_private_access_request",
            "awaiting_current_credential": "configure_current_credential_reference",
            "ready_for_authorized_probe": "run_authorized_access_probe",
            "current": "initialize_bounded_shadow_request",
            "blocked_orphan_receipt": "quarantine_orphan_receipt_then_initialize",
            "blocked_invalid_request": "repair_private_access_request",
            "blocked_missing_current_credential": "configure_current_credential_reference",
            "renewal_due": "renew_current_access_receipt",
            "renewal_required": "renew_current_access_receipt",
            "blocked_invalid_receipt": "quarantine_invalid_receipt_then_probe",
        }
        local_step_prefixes = {
            "not_initialized": "connector-access-request-init",
            "blocked_orphan_receipt": "connector-access-request-init",
            "blocked_invalid_request": "connector-access-request-complete",
            "ready_for_authorized_probe": "connector-access-probe",
        }
        step_prefix = local_step_prefixes.get(status)
        entries.append({
            "pack_id": pack_id,
            "entity_id": entity_id,
            "status": status,
            "binding_mode": binding_mode,
            "request_present": request_present,
            "receipt_present": receipt_present,
            "credential_configured": credential_configured,
            "days_until_expiry": days_until_expiry,
            "ready_for_bounded_shadow_dispatch": status in {
                "current", "renewal_due",
            },
            "next_action_id": next_action_ids[status],
            "next_local_step_id": (
                f"{step_prefix}:{pack_id}:{entity_id}" if step_prefix else None
            ),
            "next_cli_command": (
                "connector-access-receipt-renew"
                if status in {"renewal_due", "renewal_required"} else None
            ),
        })

    status_names = (
        "not_initialized",
        "awaiting_current_credential",
        "ready_for_authorized_probe",
        "current",
        "blocked_orphan_receipt",
        "blocked_invalid_request",
        "blocked_missing_current_credential",
        "renewal_due",
        "renewal_required",
        "blocked_invalid_receipt",
    )
    raw_counts = Counter(item["status"] for item in entries)
    counts = {name: raw_counts[name] for name in status_names}
    current_count = counts["current"] + counts["renewal_due"]
    expected_count = len(entries)
    return {
        "schema_version": 3,
        "artifact_type": "connector_access_registry",
        "runtime_fingerprint": runtime.snapshot()["fingerprint"],
        "as_of": as_of or datetime.now(timezone.utc).date().isoformat(),
        "summary": {
            "expected_scope_count": expected_count,
            "current_scope_count": current_count,
            "ready_for_authorized_probe_count": counts[
                "ready_for_authorized_probe"
            ],
            "renewal_due_count": counts["renewal_due"],
            "renewal_required_count": counts["renewal_required"],
            "blocked_or_incomplete_scope_count": expected_count - current_count,
            "all_expected_access_current": current_count == expected_count,
            "ready_for_bounded_shadow_dispatch": current_count == expected_count,
            "maximum_age_days": maximum_age_days,
            "warning_days_before_expiry": warning_days_before_expiry,
            "local_command_manifest": "commands.json",
        },
        "counts": counts,
        "scopes": entries,
        "control_boundary": {
            "scope_is_pack_plus_entity": True,
            "one_current_scope_may_gate_multiple_compatible_pipelines": True,
            "private_paths_returned": False,
            "provider_account_identifiers_returned": False,
            "provider_account_fingerprints_returned": False,
            "credential_values_returned": False,
            "credential_fingerprints_returned": False,
            "network_access_performed": False,
            "shadow_request_dispatched": False,
            "financial_values_returned": False,
            "receipt_is_stable_promotion_evidence": False,
            "superseded_receipt_retained_during_renewal": True,
            "credential_rotation_requires_new_authorized_probe": True,
            "renewal_due_remains_dispatchable_until_expiry": True,
            "external_actions_performed": False,
        },
    }


def build_connector_access_alerts(
    registry: Mapping[str, Any],
) -> dict[str, Any]:
    """Convert one verified access registry into stable, secret-free alert candidates.

    The result is deliberately transport-agnostic.  A deployment may poll and route
    these candidates, but this function neither installs a schedule nor sends a
    notification.
    """
    if (
        registry.get("schema_version") != 3
        or registry.get("artifact_type") != "connector_access_registry"
        or not isinstance(registry.get("scopes"), list)
        or not isinstance(registry.get("summary"), Mapping)
    ):
        raise ConnectorAccessRegistryError(
            "Connector access alerts require a verified registry schema v3"
        )
    severity_by_status = {
        "not_initialized": "warning",
        "awaiting_current_credential": "warning",
        "ready_for_authorized_probe": "warning",
        "renewal_due": "warning",
        "blocked_orphan_receipt": "critical",
        "blocked_invalid_request": "critical",
        "blocked_missing_current_credential": "critical",
        "renewal_required": "critical",
        "blocked_invalid_receipt": "critical",
    }
    category_by_status = {
        "not_initialized": "access_initialization",
        "awaiting_current_credential": "access_initialization",
        "ready_for_authorized_probe": "access_initialization",
        "renewal_due": "credential_lifecycle",
        "renewal_required": "credential_lifecycle",
        "blocked_missing_current_credential": "credential_lifecycle",
        "blocked_orphan_receipt": "access_integrity",
        "blocked_invalid_request": "access_integrity",
        "blocked_invalid_receipt": "access_integrity",
    }
    alerts: list[dict[str, Any]] = []
    for scope in registry["scopes"]:
        if not isinstance(scope, Mapping):
            raise ConnectorAccessRegistryError(
                "Connector access registry scope is invalid"
            )
        status = scope.get("status")
        if status == "current":
            continue
        pack_id = scope.get("pack_id")
        entity_id = scope.get("entity_id")
        if (
            status not in severity_by_status
            or pack_id not in _CREDENTIAL_ENV_NAMES
            or not isinstance(entity_id, str)
            or not entity_id
        ):
            raise ConnectorAccessRegistryError(
                "Connector access registry alert scope is invalid"
            )
        alert = {
            "alert_id": f"connector-access:{pack_id}:{entity_id}:{status}",
            "severity": severity_by_status[status],
            "category": category_by_status[status],
            "pack_id": pack_id,
            "entity_id": entity_id,
            "status": status,
            "next_action_id": scope.get("next_action_id"),
            "next_local_step_id": scope.get("next_local_step_id"),
            "next_cli_command": scope.get("next_cli_command"),
        }
        days_until_expiry = scope.get("days_until_expiry")
        if isinstance(days_until_expiry, int) and not isinstance(
            days_until_expiry, bool,
        ):
            alert["days_until_expiry"] = days_until_expiry
        alerts.append(alert)
    alerts.sort(key=lambda item: item["alert_id"])
    summary = registry["summary"]
    return {
        "schema_version": 1,
        "artifact_type": "connector_access_alert_candidates",
        "runtime_fingerprint": registry.get("runtime_fingerprint"),
        "as_of": registry.get("as_of"),
        "expected_scope_count": summary.get("expected_scope_count", 0),
        "alert_count": len(alerts),
        "critical_count": sum(
            item["severity"] == "critical" for item in alerts
        ),
        "warning_count": sum(
            item["severity"] == "warning" for item in alerts
        ),
        "alerts": alerts,
        "ready_for_bounded_shadow_dispatch": bool(
            summary.get("ready_for_bounded_shadow_dispatch")
        ),
        "notification_candidates_only": True,
        "notifications_sent": False,
        "schedule_installed": False,
        "paths_returned": False,
        "provider_account_identifiers_returned": False,
        "credential_values_returned": False,
        "credential_fingerprints_returned": False,
        "financial_values_returned": False,
        "network_access_performed": False,
        "external_actions_performed": False,
    }
