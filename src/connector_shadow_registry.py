from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timedelta, timezone
import os
from pathlib import Path
import stat
from typing import Any

from .box_runtime import BoxRuntime
from .connector_onboarding import build_connector_onboarding
from .connector_shadow_artifacts import (
    CONNECTOR_SHADOW_PROFILES,
    ConnectorShadowArtifactError,
    verify_connector_shadow_artifact,
)


MAX_ARTIFACT_BYTES = 2 * 1024 * 1024
MAX_ARTIFACT_COUNT = 500
DEFAULT_MAXIMUM_AGE_DAYS = 30


class ConnectorShadowRegistryError(ValueError):
    """Raised when a Connector Shadow registry request is invalid."""


def _as_of(value: str | None) -> date:
    if value is None:
        return datetime.now(timezone.utc).date()
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ConnectorShadowRegistryError("as_of must use YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise ConnectorShadowRegistryError("as_of must use canonical YYYY-MM-DD")
    return parsed


def _required_network_pack_ids(runtime: BoxRuntime) -> list[str]:
    onboarding = build_connector_onboarding(runtime, environ={})
    return sorted({
        item["pack_id"]
        for item in onboarding["pipeline_connectors"]
        if item.get("network_access") is True and item.get("pack_id")
    })


def _supported_pack_ids() -> set[str]:
    return {
        pack_id
        for profile in CONNECTOR_SHADOW_PROFILES.values()
        for pack_id in profile["covered_pack_ids"]
        if pack_id.startswith("connector.")
    }


def _directory_state(root: Path) -> str | None:
    if not root.is_absolute():
        return "directory_not_absolute"
    if root.is_symlink() or not root.is_dir():
        return "directory_not_real"
    if os.name != "nt" and stat.S_IMODE(root.stat().st_mode) & 0o077:
        return "directory_permissions_not_private"
    return None


def _file_is_private(path: Path) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    metadata = path.stat()
    if not 0 < metadata.st_size <= MAX_ARTIFACT_BYTES:
        return False
    return os.name == "nt" or stat.S_IMODE(metadata.st_mode) & 0o077 == 0


def build_connector_shadow_registry_workspace(
    runtime: BoxRuntime,
    review_dir: str | Path | None,
    *,
    as_of: str | None = None,
    maximum_age_days: int = DEFAULT_MAXIMUM_AGE_DAYS,
) -> dict[str, Any]:
    """Inspect reviewed real-sample Connector Shadow artifacts without returning secrets.

    The registry is deliberately a rotating, private directory. Every regular JSON file
    must be a current schema-v2 review for the active Box. Old periods may coexist while
    they remain inside the configured freshness window; stale or ambiguous duplicates
    fail the registry closed.
    """
    evaluation_date = _as_of(as_of)
    if (
        not isinstance(maximum_age_days, int)
        or isinstance(maximum_age_days, bool)
        or not 1 <= maximum_age_days <= 365
    ):
        raise ConnectorShadowRegistryError(
            "maximum_age_days must be an integer between 1 and 365"
        )
    required_pack_ids = _required_network_pack_ids(runtime)
    supported_pack_ids = _supported_pack_ids()
    unsupported_pack_ids = sorted(set(required_pack_ids) - supported_pack_ids)
    entity_ids = {item["id"] for item in runtime.snapshot()["entities"]}

    configured = review_dir is not None
    directory_status = "not_required" if not required_pack_ids else "missing"
    registry_clean = not required_pack_ids
    unexpected_entry_count = 0
    records: list[dict[str, Any]] = []

    if configured:
        root = Path(review_dir)
        directory_error = _directory_state(root)
        if directory_error:
            directory_status = directory_error
            registry_clean = False
        else:
            entries = sorted(root.iterdir(), key=lambda item: item.name)
            if len(entries) > MAX_ARTIFACT_COUNT:
                directory_status = "too_many_entries"
                registry_clean = False
                unexpected_entry_count = len(entries) - MAX_ARTIFACT_COUNT
                entries = entries[:MAX_ARTIFACT_COUNT]
            else:
                directory_status = "inspected"
                registry_clean = True
            eligible: list[Path] = []
            for entry in entries:
                if entry.suffix.lower() != ".json" or not _file_is_private(entry):
                    unexpected_entry_count += 1
                    registry_clean = False
                    continue
                eligible.append(entry)

            verified_records: list[dict[str, Any]] = []
            for artifact_path in eligible:
                try:
                    verified = verify_connector_shadow_artifact(runtime, artifact_path)
                    reviewed_at = datetime.fromisoformat(
                        str(verified.get("reviewed_at") or "").replace("Z", "+00:00")
                    )
                    if reviewed_at.tzinfo is None:
                        raise ConnectorShadowArtifactError(
                            "Connector Shadow reviewed_at must include timezone"
                        )
                    reviewed_date = reviewed_at.astimezone(timezone.utc).date()
                    if verified.get("entity_id") not in entity_ids:
                        raise ConnectorShadowArtifactError(
                            "Connector Shadow entity is not selected by the active Box"
                        )
                    status = "current"
                    if (
                        verified.get("real_sample_evidence") is not True
                        or verified.get("sample_classification") != "real_anonymized"
                        or not verified.get("review_current")
                    ):
                        status = "not_real_or_unreviewed"
                    elif verified.get("passed") is not True or verified.get("decision") != "passed":
                        status = "not_passed"
                    elif reviewed_date > evaluation_date:
                        status = "future"
                    elif reviewed_date < evaluation_date - timedelta(days=maximum_age_days):
                        status = "stale"
                    verified_records.append({
                        "pipeline_id": verified["pipeline_id"],
                        "entity_id": verified["entity_id"],
                        "sample_period": verified["sample_period"],
                        "covered_pack_ids": sorted(
                            set(verified.get("covered_pack_ids") or [])
                            & set(required_pack_ids)
                        ),
                        "reviewed_on": reviewed_date.isoformat(),
                        "status": status,
                    })
                except (ConnectorShadowArtifactError, OSError, ValueError):
                    records.append({"status": "invalid"})

            scope_counts = Counter(
                (item["pipeline_id"], item["entity_id"], item["sample_period"])
                for item in verified_records
            )
            for item in verified_records:
                scope = (item["pipeline_id"], item["entity_id"], item["sample_period"])
                if scope_counts[scope] > 1:
                    item["status"] = "duplicate_scope"
                records.append(item)

    status_names = (
        "current", "stale", "future", "not_passed", "not_real_or_unreviewed",
        "duplicate_scope", "invalid",
    )
    counts = {
        status: sum(item["status"] == status for item in records)
        for status in status_names
    }
    current_records = [item for item in records if item["status"] == "current"]
    pack_coverage = []
    for pack_id in required_pack_ids:
        matching = [item for item in current_records if pack_id in item["covered_pack_ids"]]
        required_entity_ids = set(runtime.connector_entity_ids(pack_id))
        covered_entity_ids = {item["entity_id"] for item in matching}
        pack_coverage.append({
            "pack_id": pack_id,
            "status": (
                "unsupported_profile" if pack_id in unsupported_pack_ids
                else "current" if required_entity_ids and required_entity_ids <= covered_entity_ids
                else "missing_current_evidence"
            ),
            "current_artifact_count": len(matching),
            "required_entity_count": len(required_entity_ids),
            "covered_entity_count": len(covered_entity_ids),
            "sample_period_count": len({item["sample_period"] for item in matching}),
            "latest_sample_period": max(
                (item["sample_period"] for item in matching), default=None,
            ),
        })
    coverage_complete = all(item["status"] == "current" for item in pack_coverage)
    unsafe_or_noncurrent_count = sum(
        counts[status] for status in status_names if status != "current"
    )
    ready = (
        not required_pack_ids
        or (
            configured
            and directory_status == "inspected"
            and registry_clean
            and unexpected_entry_count == 0
            and unsafe_or_noncurrent_count == 0
            and coverage_complete
        )
    )
    activation_status = (
        "not_required" if not required_pack_ids
        else "current" if ready
        else directory_status if directory_status != "inspected"
        else "invalid" if unsafe_or_noncurrent_count or not registry_clean
        else "incomplete"
    )
    return {
        "schema_version": 1,
        "artifact_type": "connector_shadow_registry_workspace",
        "runtime_fingerprint": runtime.snapshot()["fingerprint"],
        "as_of": evaluation_date.isoformat(),
        "summary": {
            "activation_status": activation_status,
            "registry_configured": configured,
            "registry_clean": registry_clean,
            "required_network_pack_count": len(required_pack_ids),
            "covered_network_pack_count": sum(
                item["status"] == "current" for item in pack_coverage
            ),
            "artifact_count": len(records),
            "current_artifact_count": counts["current"],
            "unsafe_or_noncurrent_artifact_count": unsafe_or_noncurrent_count,
            "unexpected_entry_count": unexpected_entry_count,
            "maximum_age_days": maximum_age_days,
            "pack_coverage_complete": coverage_complete,
            "ready_for_connector_shadow_evidence": ready,
        },
        "counts": counts,
        "pack_coverage": pack_coverage,
        "current_artifacts": current_records,
        "control_boundary": {
            "review_directory_required_for_network_connectors": bool(required_pack_ids),
            "private_permissions_required": True,
            "symbolic_links_allowed": False,
            "paths_returned": False,
            "file_names_returned": False,
            "actors_returned": False,
            "evidence_references_returned": False,
            "source_counts_returned": False,
            "control_results_returned": False,
            "financial_values_returned": False,
            "credentials_returned": False,
            "stable_promotion_performed": False,
            "external_actions_performed": False,
        },
    }
