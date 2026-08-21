from __future__ import annotations

import os
from pathlib import Path
import stat
from typing import Any

from .activation_runbook import (
    ActivationRunbookError,
    ActivationRunbookStore,
    _file_sha256,
    _read_private_json,
    _step_index,
)
from .box_runtime import BoxRuntime
from .pilot_shadow_next_period import (
    COMMANDS_NAME,
    MANIFEST_NAME,
    PERIOD_WORKSPACE_DIRECTORY,
    verify_next_pilot_shadow_period,
)
from .pilot_shadow_series import _period_number


LEDGER_NAME = "period-runbook.jsonl"
LOCK_NAME = ".period-runbook.lock"


class PilotShadowPeriodRunbookStore(ActivationRunbookStore):
    """Append-only, non-authoritative progress ledger for one Shadow month."""

    ledger_name = LEDGER_NAME
    lock_name = LOCK_NAME
    event_type = "PILOT_SHADOW_PERIOD_STEP_PROGRESS_REPORTED"
    status_artifact_type = "pilot_shadow_period_runbook_status"

    def __init__(self, activation_root: str | Path, period: str):
        _period_number(period)
        self.activation_root = Path(activation_root)
        self.period = period
        workspace = (
            self.activation_root / PERIOD_WORKSPACE_DIRECTORY / self.period
        )
        super().__init__(workspace)

    def _context(
        self, runtime: BoxRuntime,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, dict[str, Any]], str]:
        verification = dict(verify_next_pilot_shadow_period(
            runtime, self.activation_root, self.period,
        ))
        verification["workspace_manifest_sha256"] = _file_sha256(
            self.workspace / MANIFEST_NAME
        )
        manifest = _read_private_json(
            self.workspace / MANIFEST_NAME,
            label="pilot Shadow period workspace manifest",
        )
        if (
            manifest.get("schema_version") != 1
            or manifest.get("artifact_type")
            != "pilot_shadow_next_period_workspace"
            or manifest.get("period") != self.period
            or manifest.get("runtime_fingerprint")
            != runtime.snapshot()["fingerprint"]
        ):
            raise ActivationRunbookError(
                "pilot Shadow period runbook does not match the monthly workspace"
            )
        if self.root.is_symlink() or not self.root.is_dir():
            raise ActivationRunbookError(
                "pilot Shadow period runbook directory is missing or symbolic"
            )
        if os.name != "nt" and stat.S_IMODE(self.root.stat().st_mode) != 0o700:
            raise ActivationRunbookError(
                "pilot Shadow period runbook directory must use mode 0700"
            )
        commands_path = self.workspace / COMMANDS_NAME
        commands = _read_private_json(
            commands_path,
            label="pilot Shadow period command contract",
        )
        return (
            verification,
            commands,
            _step_index(commands),
            _file_sha256(commands_path),
        )

    def record(self, runtime: BoxRuntime, **kwargs: Any) -> dict[str, Any]:
        result = super().record(runtime, **kwargs)
        return {
            **result,
            "period": self.period,
            "period_workspace_valid": True,
        }

    def status(self, runtime: BoxRuntime) -> dict[str, Any]:
        result = super().status(runtime)
        return {
            **result,
            "period": self.period,
            "period_workspace_valid": True,
            "authoritative_period_completion_inferred": False,
        }

    def read_only_status(self, runtime: BoxRuntime) -> dict[str, Any]:
        """Project monthly progress without creating or writing any file."""
        result = super().status_read_only(runtime)
        return {
            **result,
            "period": self.period,
            "period_workspace_valid": True,
            "authoritative_period_completion_inferred": False,
        }

    def verify(self, runtime: BoxRuntime) -> dict[str, Any]:
        result = super().verify(runtime)
        return {
            **result,
            "period": self.period,
            "period_workspace_valid": True,
            "authoritative_period_completion_inferred": False,
        }
