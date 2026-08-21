from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import stat
import threading
from typing import Any, Mapping

from .activation_workspace import (
    COMMANDS_NAME,
    MANIFEST_NAME,
    verify_activation_workspace,
)
from .box_runtime import BoxRuntime

try:
    import fcntl
except ImportError:  # pragma: no cover - supported production targets are POSIX
    fcntl = None


LEDGER_NAME = "activation-runbook.jsonl"
LOCK_NAME = ".activation-runbook.lock"
MAX_LEDGER_BYTES = 32 * 1024 * 1024
MAX_EVENT_BYTES = 64 * 1024
MAX_EVENTS = 100_000
OUTCOMES = {
    "reported_complete",
    "reported_failed",
    "blocked",
    "deferred",
}


class ActivationRunbookError(RuntimeError):
    """Raised when private activation progress cannot be trusted or recorded."""


def _canonical(value: Any) -> str:
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ActivationRunbookError(
            "activation runbook event must be JSON-serializable"
        ) from exc


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _text(value: Any, *, label: str, minimum: int, maximum: int) -> str:
    output = str(value or "").strip()
    if (
        not minimum <= len(output) <= maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in output)
    ):
        raise ActivationRunbookError(
            f"{label} must be {minimum}-{maximum} printable characters"
        )
    return output


def _references(values: list[str] | tuple[str, ...]) -> list[str]:
    if len(values) > 20:
        raise ActivationRunbookError(
            "activation runbook accepts at most 20 evidence references per event"
        )
    output: list[str] = []
    for value in values:
        reference = _text(
            value, label="evidence reference", minimum=1, maximum=500,
        )
        if reference in output:
            raise ActivationRunbookError(
                "activation runbook evidence references must be unique"
            )
        output.append(reference)
    return output


def _read_private_json(path: Path, *, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ActivationRunbookError(f"{label} must be a regular non-symlink file")
    metadata = path.stat()
    if os.name != "nt" and stat.S_IMODE(metadata.st_mode) != 0o600:
        raise ActivationRunbookError(f"{label} must use mode 0600")
    if not 0 < metadata.st_size <= 2 * 1024 * 1024:
        raise ActivationRunbookError(f"{label} exceeds its size boundary")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ActivationRunbookError(f"{label} must contain valid JSON") from exc
    if not isinstance(value, dict):
        raise ActivationRunbookError(f"{label} must be a JSON object")
    return value


def _step_index(commands: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    steps = commands.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ActivationRunbookError("activation command contract has no steps")
    indexed: dict[str, dict[str, Any]] = {}
    for step in steps:
        if not isinstance(step, dict):
            raise ActivationRunbookError("activation command step must be an object")
        step_id = step.get("step_id")
        if not isinstance(step_id, str) or not step_id or step_id in indexed:
            raise ActivationRunbookError("activation command step identity is invalid")
        indexed[step_id] = step
    return indexed


class ActivationRunbookStore:
    """Append-only operator continuity ledger that never unlocks evidence gates."""

    ledger_name = LEDGER_NAME
    lock_name = LOCK_NAME
    event_type = "ACTIVATION_STEP_PROGRESS_REPORTED"
    status_artifact_type = "activation_runbook_status"
    max_ledger_bytes = MAX_LEDGER_BYTES
    max_event_bytes = MAX_EVENT_BYTES
    max_events = MAX_EVENTS

    def __init__(self, workspace: str | Path):
        self.workspace = Path(workspace)
        self.root = self.workspace / "runbook"
        self.events_file = self.root / self.ledger_name
        self.lock_file = self.root / self.lock_name
        self._lock = threading.RLock()

    def _context(
        self, runtime: BoxRuntime,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, dict[str, Any]], str]:
        verification = verify_activation_workspace(runtime, self.workspace)
        manifest = _read_private_json(
            self.workspace / MANIFEST_NAME,
            label="activation workspace manifest",
        )
        if manifest.get("schema_version") not in {4, 5}:
            raise ActivationRunbookError(
                "activation runbook requires a workspace initialized with schema v4 or v5"
            )
        if self.root.is_symlink() or not self.root.is_dir():
            raise ActivationRunbookError(
                "activation runbook directory is missing or symbolic"
            )
        if os.name != "nt" and stat.S_IMODE(self.root.stat().st_mode) != 0o700:
            raise ActivationRunbookError("activation runbook directory must use mode 0700")
        commands_path = self.workspace / COMMANDS_NAME
        commands = _read_private_json(
            commands_path, label="activation command contract",
        )
        return verification, commands, _step_index(commands), _file_sha256(commands_path)

    def _locked(self):
        if fcntl is None:
            raise ActivationRunbookError("cross-process runbook locking is unavailable")
        handle = self.lock_file.open("a+b")
        if os.name != "nt":
            os.chmod(self.lock_file, 0o600)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        return handle

    @classmethod
    def _validate_event_shape(
        cls, event: dict[str, Any], *, line_number: int,
    ) -> None:
        expected = {
            "schema_version", "sequence", "previous_hash", "event_hash",
            "event_type", "recorded_at", "runtime_fingerprint",
            "command_contract_sha256", "step_id", "step_fingerprint",
            "outcome", "observed_exit_code", "actor", "rationale",
            "evidence_references", "authoritative_completion",
            "financial_state_changed", "external_action_performed",
            "credential_values_persisted", "financial_values_persisted",
        }
        if set(event) != expected:
            raise ActivationRunbookError(
                f"activation runbook event fields are invalid at line {line_number}"
            )
        if (
            event.get("schema_version") != 1
            or event.get("event_type") != cls.event_type
            or event.get("outcome") not in OUTCOMES
            or event.get("authoritative_completion") is not False
            or event.get("financial_state_changed") is not False
            or event.get("external_action_performed") is not False
            or event.get("credential_values_persisted") is not False
            or event.get("financial_values_persisted") is not False
        ):
            raise ActivationRunbookError(
                f"activation runbook safety boundary is invalid at line {line_number}"
            )
        for field in (
            "runtime_fingerprint", "command_contract_sha256", "step_fingerprint",
        ):
            value = event.get(field)
            if (
                not isinstance(value, str) or len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
            ):
                raise ActivationRunbookError(
                    f"activation runbook {field} is invalid at line {line_number}"
                )
        _text(event.get("step_id"), label="step_id", minimum=1, maximum=240)
        _text(event.get("actor"), label="actor", minimum=1, maximum=80)
        _text(event.get("rationale"), label="rationale", minimum=8, maximum=1000)
        try:
            recorded_at = datetime.fromisoformat(
                str(event.get("recorded_at") or "").replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise ActivationRunbookError(
                f"activation runbook timestamp is invalid at line {line_number}"
            ) from exc
        if recorded_at.tzinfo is None:
            raise ActivationRunbookError(
                f"activation runbook timestamp has no timezone at line {line_number}"
            )
        references = event.get("evidence_references")
        if not isinstance(references, list):
            raise ActivationRunbookError(
                f"activation runbook evidence references are invalid at line {line_number}"
            )
        _references(tuple(references))
        exit_code = event.get("observed_exit_code")
        if exit_code is not None and (
            not isinstance(exit_code, int) or isinstance(exit_code, bool)
            or not 0 <= exit_code <= 255
        ):
            raise ActivationRunbookError(
                f"activation runbook exit code is invalid at line {line_number}"
            )

    def _events_unlocked(self) -> list[dict[str, Any]]:
        if not self.events_file.exists():
            return []
        if self.events_file.is_symlink() or not self.events_file.is_file():
            raise ActivationRunbookError(
                "activation runbook ledger must be a regular non-symlink file"
            )
        metadata = self.events_file.stat()
        if os.name != "nt" and stat.S_IMODE(metadata.st_mode) != 0o600:
            raise ActivationRunbookError("activation runbook ledger must use mode 0600")
        if metadata.st_size > self.max_ledger_bytes:
            raise ActivationRunbookError("activation runbook ledger exceeds 32 MiB")
        events: list[dict[str, Any]] = []
        previous_hash = "GENESIS"
        with self.events_file.open("rb") as stream:
            for line_number, raw in enumerate(stream, 1):
                if line_number > self.max_events:
                    raise ActivationRunbookError(
                        "activation runbook contains too many events"
                    )
                if len(raw) > self.max_event_bytes:
                    raise ActivationRunbookError(
                        f"activation runbook event {line_number} is too large"
                    )
                try:
                    event = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ActivationRunbookError(
                        f"activation runbook is corrupt at line {line_number}"
                    ) from exc
                if not isinstance(event, dict):
                    raise ActivationRunbookError(
                        f"activation runbook event {line_number} is not an object"
                    )
                self._validate_event_shape(event, line_number=line_number)
                supplied_hash = event.get("event_hash")
                body = {key: value for key, value in event.items() if key != "event_hash"}
                if event.get("sequence") != line_number:
                    raise ActivationRunbookError(
                        f"activation runbook sequence mismatch at line {line_number}"
                    )
                if event.get("previous_hash") != previous_hash:
                    raise ActivationRunbookError(
                        f"activation runbook chain mismatch at line {line_number}"
                    )
                if supplied_hash != _hash(body):
                    raise ActivationRunbookError(
                        f"activation runbook hash mismatch at line {line_number}"
                    )
                previous_hash = str(supplied_hash)
                events.append(event)
        return events

    def _bound_events(
        self,
        events: list[dict[str, Any]],
        *,
        runtime_fingerprint: str,
        command_sha256: str,
        steps: Mapping[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        for index, event in enumerate(events, 1):
            step = steps.get(str(event.get("step_id") or ""))
            if (
                event.get("runtime_fingerprint") != runtime_fingerprint
                or event.get("command_contract_sha256") != command_sha256
                or step is None
                or event.get("step_fingerprint") != _hash(step)
            ):
                raise ActivationRunbookError(
                    f"activation runbook event {index} no longer matches this Box command contract"
                )
            exit_code = event.get("observed_exit_code")
            outcome = event.get("outcome")
            if step.get("action") == "run_cli":
                if outcome == "reported_complete" and exit_code != 0:
                    raise ActivationRunbookError(
                        f"activation runbook success event {index} requires exit code 0"
                    )
                if outcome == "reported_failed" and (
                    not isinstance(exit_code, int) or not 1 <= exit_code <= 255
                ):
                    raise ActivationRunbookError(
                        f"activation runbook failure event {index} requires exit code 1-255"
                    )
                if outcome in {"blocked", "deferred"} and exit_code is not None:
                    raise ActivationRunbookError(
                        f"activation runbook non-attempt event {index} cannot claim an exit code"
                    )
            elif exit_code is not None:
                raise ActivationRunbookError(
                    f"activation runbook manual event {index} cannot claim a CLI exit code"
                )
        return events

    def _append_unlocked(
        self, events: list[dict[str, Any]], event: dict[str, Any],
    ) -> None:
        event.update({
            "schema_version": 1,
            "sequence": len(events) + 1,
            "previous_hash": events[-1]["event_hash"] if events else "GENESIS",
        })
        event["event_hash"] = _hash(event)
        encoded = (_canonical(event) + "\n").encode("utf-8")
        if len(encoded) > self.max_event_bytes:
            raise ActivationRunbookError("activation runbook event is too large")
        current_size = self.events_file.stat().st_size if self.events_file.exists() else 0
        if current_size + len(encoded) > self.max_ledger_bytes:
            raise ActivationRunbookError("activation runbook ledger has reached its limit")
        with self.events_file.open("ab") as stream:
            if os.name != "nt":
                os.chmod(self.events_file, 0o600)
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())

    def record(
        self,
        runtime: BoxRuntime,
        *,
        step_id: str,
        outcome: str,
        actor: str,
        rationale: str,
        evidence_references: list[str] | tuple[str, ...] = (),
        observed_exit_code: int | None = None,
    ) -> dict[str, Any]:
        _, _, steps, command_sha256 = self._context(runtime)
        if step_id not in steps:
            raise ActivationRunbookError(
                "activation runbook step_id is not in the current command contract"
            )
        if outcome not in OUTCOMES:
            raise ActivationRunbookError(
                "outcome must be reported_complete, reported_failed, blocked or deferred"
            )
        actor = _text(actor, label="actor", minimum=1, maximum=80)
        rationale = _text(
            rationale, label="rationale", minimum=8, maximum=1000,
        )
        references = _references(tuple(evidence_references))
        step = steps[step_id]
        if step.get("action") == "run_cli":
            if outcome == "reported_complete" and observed_exit_code != 0:
                raise ActivationRunbookError(
                    "reported_complete CLI progress requires observed exit code 0"
                )
            if outcome == "reported_failed" and (
                not isinstance(observed_exit_code, int)
                or isinstance(observed_exit_code, bool)
                or not 1 <= observed_exit_code <= 255
            ):
                raise ActivationRunbookError(
                    "reported_failed CLI progress requires observed exit code 1-255"
                )
            if outcome in {"blocked", "deferred"} and observed_exit_code is not None:
                raise ActivationRunbookError(
                    "blocked or deferred progress cannot claim a CLI exit code"
                )
        elif observed_exit_code is not None:
            raise ActivationRunbookError(
                "manual activation steps cannot claim a CLI exit code"
            )
        fingerprint = runtime.snapshot()["fingerprint"]
        event = {
            "event_type": self.event_type,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "runtime_fingerprint": fingerprint,
            "command_contract_sha256": command_sha256,
            "step_id": step_id,
            "step_fingerprint": _hash(step),
            "outcome": outcome,
            "observed_exit_code": observed_exit_code,
            "actor": actor,
            "rationale": rationale,
            "evidence_references": references,
            "authoritative_completion": False,
            "financial_state_changed": False,
            "external_action_performed": False,
            "credential_values_persisted": False,
            "financial_values_persisted": False,
        }
        with self._lock:
            lock_handle = self._locked()
            try:
                events = self._bound_events(
                    self._events_unlocked(),
                    runtime_fingerprint=fingerprint,
                    command_sha256=command_sha256,
                    steps=steps,
                )
                self._append_unlocked(events, event)
            finally:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
                lock_handle.close()
        return {
            "recorded": True,
            "sequence": event["sequence"],
            "chain_head": event["event_hash"],
            "step_id": step_id,
            "reported_outcome": outcome,
            "authoritative_completion": False,
            "financial_state_changed": False,
            "external_action_performed": False,
            "actor_returned": False,
            "evidence_references_returned": False,
            "private_paths_returned": False,
        }

    def _project_status(
        self,
        *,
        verification: Mapping[str, Any],
        commands: Mapping[str, Any],
        command_sha256: str,
        fingerprint: str,
        events: list[dict[str, Any]],
    ) -> dict[str, Any]:
        latest: dict[str, dict[str, Any]] = {}
        event_counts: dict[str, int] = {}
        for event in events:
            step_id = event["step_id"]
            latest[step_id] = event
            event_counts[step_id] = event_counts.get(step_id, 0) + 1
        projected = []
        reported_complete_count = 0
        for step in commands["steps"]:
            current = latest.get(step["step_id"])
            outcome = current.get("outcome") if current else "not_reported"
            if outcome == "reported_complete":
                reported_complete_count += 1
            projected.append({
                "step_id": step["step_id"],
                "action": step["action"],
                "reported_outcome": outcome,
                "event_count": event_counts.get(step["step_id"], 0),
                "latest_sequence": current.get("sequence") if current else None,
                "authoritative_completion": False,
            })
        next_step = next((
            item["step_id"] for item in projected
            if item["reported_outcome"] != "reported_complete"
        ), None)
        return {
            "schema_version": 1,
            "artifact_type": self.status_artifact_type,
            "runtime_fingerprint": fingerprint,
            "command_contract_sha256": command_sha256,
            "command_schema_version": commands["schema_version"],
            "event_count": len(events),
            "step_count": len(projected),
            "reported_complete_count": reported_complete_count,
            "reported_blocked_count": sum(
                item["reported_outcome"] == "blocked" for item in projected
            ),
            "next_reported_progress_step_id": next_step,
            "steps": projected,
            "chain_head": events[-1]["event_hash"] if events else "GENESIS",
            "workspace_manifest_sha256": verification["workspace_manifest_sha256"],
            "workspace_valid": True,
            "authoritative_completion_inferred": False,
            "evidence_gates_unlocked": False,
            "financial_state_changed": False,
            "external_action_performed": False,
            "actors_returned": False,
            "evidence_references_returned": False,
            "private_paths_returned": False,
        }

    def status(self, runtime: BoxRuntime) -> dict[str, Any]:
        verification, commands, steps, command_sha256 = self._context(runtime)
        fingerprint = runtime.snapshot()["fingerprint"]
        with self._lock:
            lock_handle = self._locked()
            try:
                events = self._bound_events(
                    self._events_unlocked(),
                    runtime_fingerprint=fingerprint,
                    command_sha256=command_sha256,
                    steps=steps,
                )
            finally:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
                lock_handle.close()
        return self._project_status(
            verification=verification,
            commands=commands,
            command_sha256=command_sha256,
            fingerprint=fingerprint,
            events=events,
        )

    def status_read_only(self, runtime: BoxRuntime) -> dict[str, Any]:
        """Project progress through a shared lock without creating any file."""
        if fcntl is None:
            raise ActivationRunbookError(
                "cross-process runbook locking is unavailable"
            )
        verification, commands, steps, command_sha256 = self._context(runtime)
        fingerprint = runtime.snapshot()["fingerprint"]
        ledger_exists = self.events_file.exists() or self.events_file.is_symlink()
        lock_exists = self.lock_file.exists() or self.lock_file.is_symlink()
        if ledger_exists and not lock_exists:
            raise ActivationRunbookError(
                "activation runbook ledger exists without its lock"
            )
        if not lock_exists:
            events: list[dict[str, Any]] = []
        else:
            with self._lock:
                try:
                    lock_handle = self.lock_file.open("rb")
                except OSError as exc:
                    raise ActivationRunbookError(
                        "activation runbook lock cannot be opened read-only"
                    ) from exc
                try:
                    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_SH)
                    events = self._bound_events(
                        self._events_unlocked(),
                        runtime_fingerprint=fingerprint,
                        command_sha256=command_sha256,
                        steps=steps,
                    )
                finally:
                    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
                    lock_handle.close()
        return self._project_status(
            verification=verification,
            commands=commands,
            command_sha256=command_sha256,
            fingerprint=fingerprint,
            events=events,
        )

    def verify(self, runtime: BoxRuntime) -> dict[str, Any]:
        status = self.status(runtime)
        return {
            "valid": True,
            "runtime_fingerprint": status["runtime_fingerprint"],
            "command_contract_sha256": status["command_contract_sha256"],
            "event_count": status["event_count"],
            "step_count": status["step_count"],
            "chain_head": status["chain_head"],
            "authoritative_completion_inferred": False,
            "financial_state_changed": False,
            "external_action_performed": False,
            "actors_returned": False,
            "evidence_references_returned": False,
            "private_paths_returned": False,
        }
