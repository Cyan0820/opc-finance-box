from __future__ import annotations

import os
from pathlib import Path
import re
import stat
from typing import Any

from .activation_runbook import ActivationRunbookError
from .activation_workspace import (
    ActivationWorkspaceError,
    verify_activation_workspace,
)
from .box_runtime import BoxRuntime
from .cfo_control_overlay import (
    CfoControlOverlayError,
    build_cfo_control_overlay,
)
from .cfo_metric_catalog import CfoMetricCatalogError, build_cfo_metric_catalog
from .pilot_shadow_next_period import (
    PERIOD_WORKSPACE_DIRECTORY,
    PilotShadowNextPeriodError,
)
from .pilot_shadow_period_runbook import PilotShadowPeriodRunbookStore
from .pilot_shadow_period_tasks import (
    PilotShadowPeriodTaskError,
    project_period_operator_tasks,
)


ACTIVATION_WORKSPACE_ROOT_ENV = "OPC_ACTIVATION_WORKSPACE_ROOT"
MAX_PERIOD_WORKSPACES = 24
PERIOD_PATTERN = re.compile(r"^[0-9]{4}-(?:0[1-9]|1[0-2])$")


class PilotShadowPeriodIndexError(ValueError):
    """Raised when the server-mounted monthly workspace index is unsafe."""


def _missing_workspace(
    control_overlay: dict[str, Any], metric_catalog: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "artifact_type": "pilot_shadow_period_workspace_index",
        "summary": {
            "activation_status": "missing",
            "configured": False,
            "period_count": 0,
            "started_runbook_count": 0,
            "reported_event_count": 0,
            "first_period": None,
            "latest_period": None,
            "resumable_period": None,
            "resumable_step_id": None,
            "operator_task_count": 0,
            "reported_complete_task_count": 0,
            "reported_blocked_task_count": 0,
            "current_task": None,
            "authoritative_period_completion_inferred": False,
        },
        "periods": [],
        "business_control_overlay": control_overlay,
        "business_metric_catalog": metric_catalog,
        "control_boundary": _control_boundary(),
    }


def _control_boundary() -> dict[str, bool]:
    return {
        "activation_root_accepted_from_request": False,
        "server_mounted_root_used": True,
        "private_paths_returned": False,
        "actors_returned": False,
        "role_types_returned": True,
        "evidence_references_returned": False,
        "evidence_requirement_types_only": True,
        "safe_method_guidance_returned": True,
        "business_control_overlay_returned": True,
        "business_control_types_only": True,
        "business_metric_catalog_returned": True,
        "metric_definitions_only": True,
        "metric_values_returned": False,
        "formula_evaluated": False,
        "founder_review_question_types_only": True,
        "source_boundary_types_only": True,
        "work_product_types_only": True,
        "checklist_types_only": True,
        "stop_condition_types_only": True,
        "hashes_returned": False,
        "financial_values_returned": False,
        "commands_returned": False,
        "browser_actions_available": False,
        "authoritative_completion_inferred": False,
        "authoritative_verifier_required": True,
        "evidence_gates_unlocked": False,
        "financial_state_changed": False,
        "external_action_performed": False,
        "filesystem_entries_created": False,
    }


def _period_directories(root: Path) -> list[str]:
    parent = root / PERIOD_WORKSPACE_DIRECTORY
    if (
        parent.is_symlink()
        or not parent.is_dir()
        or parent.resolve() != parent
    ):
        raise PilotShadowPeriodIndexError(
            "pilot Shadow period workspace directory is invalid"
        )
    if os.name != "nt" and stat.S_IMODE(parent.stat().st_mode) != 0o700:
        raise PilotShadowPeriodIndexError(
            "pilot Shadow period workspace directory must use mode 0700"
        )
    periods: list[str] = []
    for item in parent.iterdir():
        if (
            not PERIOD_PATTERN.fullmatch(item.name)
            or item.is_symlink()
            or not item.is_dir()
            or item.resolve() != item
        ):
            raise PilotShadowPeriodIndexError(
                "pilot Shadow period workspace directory contains an invalid entry"
            )
        periods.append(item.name)
    periods.sort()
    if len(periods) > MAX_PERIOD_WORKSPACES:
        raise PilotShadowPeriodIndexError(
            "pilot Shadow period workspace index exceeds 24 periods"
        )
    return periods


def build_pilot_shadow_period_workspace_index(
    runtime: BoxRuntime,
    activation_root: str | Path | None,
) -> dict[str, Any]:
    """Build a path-free, non-authoritative view of monthly operator progress."""
    try:
        runtime_snapshot = runtime.snapshot()
        control_overlay = build_cfo_control_overlay(
            (pack["id"] for pack in runtime_snapshot["packs"]),
        )
        metric_catalog = build_cfo_metric_catalog(
            (pack["id"] for pack in runtime_snapshot["packs"]),
        )
    except (
        CfoControlOverlayError,
        CfoMetricCatalogError,
        KeyError,
        TypeError,
    ) as error:
        raise PilotShadowPeriodIndexError(
            "pilot Shadow business method catalog failed validation"
        ) from error
    if activation_root is None:
        return _missing_workspace(control_overlay, metric_catalog)
    try:
        root = Path(activation_root)
        verify_activation_workspace(runtime, root)
        periods = _period_directories(root)
        projected: list[dict[str, Any]] = []
        for period in periods:
            status = PilotShadowPeriodRunbookStore(
                root, period,
            ).read_only_status(runtime)
            tasks = project_period_operator_tasks(
                status["steps"], entity_ids=runtime.entities.ids(),
            )
            projected.append({
                "period": period,
                "workspace_valid": status["period_workspace_valid"],
                "runbook_valid": True,
                "runbook_started": status["event_count"] > 0,
                "event_count": status["event_count"],
                "step_count": status["step_count"],
                "reported_complete_count": status[
                    "reported_complete_count"
                ],
                "reported_blocked_count": status["reported_blocked_count"],
                "next_reported_progress_step_id": status[
                    "next_reported_progress_step_id"
                ],
                "all_steps_reported_complete": (
                    status["step_count"] > 0
                    and status["reported_complete_count"]
                    == status["step_count"]
                ),
                "operator_tasks": tasks,
                "open_task_count": sum(
                    item["reported_outcome"] != "reported_complete"
                    for item in tasks
                ),
                "actionable_task_count": sum(
                    item["actionable_now"] for item in tasks
                ),
                "authoritative_period_completion": False,
            })
    except (
        ActivationRunbookError,
        ActivationWorkspaceError,
        PilotShadowNextPeriodError,
        PilotShadowPeriodTaskError,
        CfoControlOverlayError,
        CfoMetricCatalogError,
        OSError,
        ValueError,
    ) as error:
        if isinstance(error, PilotShadowPeriodIndexError):
            raise
        raise PilotShadowPeriodIndexError(
            "pilot Shadow period workspace index failed validation"
        ) from error
    resumable = next((
        item for item in reversed(projected)
        if not item["all_steps_reported_complete"]
    ), None)
    current_task = next((
        {"period": resumable["period"], **task}
        for task in (resumable or {}).get("operator_tasks", [])
        if task["actionable_now"]
    ), None)
    all_tasks = [
        task for period in projected for task in period["operator_tasks"]
    ]
    return {
        "schema_version": 1,
        "artifact_type": "pilot_shadow_period_workspace_index",
        "summary": {
            "activation_status": "current",
            "configured": True,
            "period_count": len(projected),
            "started_runbook_count": sum(
                item["runbook_started"] for item in projected
            ),
            "reported_event_count": sum(
                item["event_count"] for item in projected
            ),
            "first_period": projected[0]["period"] if projected else None,
            "latest_period": projected[-1]["period"] if projected else None,
            "resumable_period": resumable["period"] if resumable else None,
            "resumable_step_id": (
                resumable["next_reported_progress_step_id"]
                if resumable else None
            ),
            "operator_task_count": len(all_tasks),
            "reported_complete_task_count": sum(
                item["reported_outcome"] == "reported_complete"
                for item in all_tasks
            ),
            "reported_blocked_task_count": sum(
                item["reported_outcome"] == "blocked"
                for item in all_tasks
            ),
            "current_task": current_task,
            "authoritative_period_completion_inferred": False,
        },
        "periods": projected,
        "business_control_overlay": control_overlay,
        "business_metric_catalog": metric_catalog,
        "control_boundary": _control_boundary(),
    }
