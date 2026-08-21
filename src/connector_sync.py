from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - supported production targets are POSIX
    fcntl = None

from .box_runtime import BoxRuntime
from .connector_sdk import (
    ConnectorDefinition,
    ConnectorError,
    ConnectorRegistry,
    ConnectorSyncWindow,
)


MAX_LEDGER_BYTES = 64 * 1024 * 1024
MAX_EVENT_BYTES = 256 * 1024
MAX_EVENTS = 100_000
STREAM_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}$")
ATTEMPT_PATTERN = re.compile(r"^[0-9a-f]{24}$")
ACTOR_PATTERN = re.compile(r"^[^\x00-\x1f\x7f]{1,80}$")
SECRET_KEY_PATTERN = re.compile(
    r"(?:secret|token|password|authorization|api[_-]?key|credential|restricted[_-]?key)", re.I,
)
SAFE_SOURCE_FIELDS = {
    "kind", "name", "network_access_performed", "api_version", "page_count",
    "retry_count", "rate_limit_count", "retry_delay_seconds_total", "retry_after_honored",
    "shop_domain",
    "currency", "interval_start", "interval_end", "account_reference_masked",
    "profile_binding_hash", "balance_binding_hash", "access_contract",
    "statement_type", "statement_locale", "entity_binding_verified",
    "beta_api", "update_capture_basis", "complete_update_capture",
}


class ConnectorSyncError(RuntimeError):
    """Raised when a controlled connector sync cannot be trusted or advanced."""


def _canonical(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ConnectorSyncError("connector sync metadata must be JSON-serializable") from exc


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _actor(value: Any) -> str:
    text = str(value or "").strip()
    if not ACTOR_PATTERN.fullmatch(text):
        raise ConnectorSyncError("actor must be 1-80 printable characters")
    return text


def _timestamp(value: Any, field: str) -> datetime:
    text = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ConnectorSyncError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ConnectorSyncError(f"{field} must include a timezone offset")
    return parsed.astimezone(timezone.utc)


def _render_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _stream(value: Any) -> str:
    text = str(value or "").strip()
    if not STREAM_PATTERN.fullmatch(text):
        raise ConnectorSyncError("stream_id must be 1-120 safe characters")
    return text


def _assert_secret_free(value: Any, path: str = "request") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise ConnectorSyncError(f"{path} keys must be strings")
            if SECRET_KEY_PATTERN.search(key):
                raise ConnectorSyncError(f"{path} contains a forbidden credential-like field")
            _assert_secret_free(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _assert_secret_free(child, f"{path}[{index}]")
    elif not isinstance(value, (str, int, float, bool, type(None))):
        raise ConnectorSyncError(f"{path} contains a non-JSON value")


def _safe_failure(value: Any) -> str:
    text = " ".join(str(value or "connector execution failed").split())[:240]
    lowered = text.lower()
    if (
        SECRET_KEY_PATTERN.search(text)
        or "bearer " in lowered
        or "sk_" in lowered
        or "rk_" in lowered
        or "shpat_" in lowered
        or "http://" in lowered
        or "https://" in lowered
    ):
        return "connector execution failed; sensitive error detail was suppressed"
    return text or "connector execution failed"


def _window_value(contract: ConnectorSyncWindow, value: datetime) -> str | int:
    if contract.value_format == "unix_seconds":
        return int(value.timestamp())
    return _render_timestamp(value)


def _plan_core(plan: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in plan.items() if key != "plan_id"}


def _legacy_capture_policy(window: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_window_strategy": "legacy_contiguous_window",
        "configured_overlap_seconds": 0,
        "applied_overlap_seconds": 0,
        "logical_start": window["start"],
        "request_start": window["start"],
        "end": window["end"],
        "complete_update_capture_claimed": False,
    }


def validate_sync_plan(plan: Any) -> dict[str, Any]:
    if not isinstance(plan, dict) or plan.get("schema_version") not in {1, 2}:
        raise ConnectorSyncError("connector sync plan requires schema_version 1 or 2")
    required = {
        "schema_version", "plan_id", "runtime_fingerprint", "connector_id", "entity_id",
        "stream_id", "stream_key", "sync_mode", "window", "expected_checkpoint_event_hash",
        "request", "request_sha256", "checkpoint_promotion_allowed",
        "secret_values_included", "external_actions_performed",
    }
    if plan["schema_version"] == 2:
        required.add("capture_policy")
    if set(plan) != required:
        raise ConnectorSyncError("connector sync plan fields do not match the strict contract")
    if plan.get("sync_mode") not in {"incremental", "backfill"}:
        raise ConnectorSyncError("connector sync mode must be incremental or backfill")
    if plan.get("checkpoint_promotion_allowed") != (plan["sync_mode"] == "incremental"):
        raise ConnectorSyncError("connector sync checkpoint promotion flag is inconsistent")
    if plan.get("secret_values_included") is not False or plan.get("external_actions_performed") is not False:
        raise ConnectorSyncError("connector sync plan must not contain secrets or external actions")
    for field in ("runtime_fingerprint", "connector_id", "entity_id", "stream_key"):
        if not isinstance(plan.get(field), str) or not plan[field]:
            raise ConnectorSyncError(f"connector sync plan requires {field}")
    _stream(plan.get("stream_id"))
    window = plan.get("window")
    if not isinstance(window, dict) or set(window) != {"start", "end", "duration_seconds"}:
        raise ConnectorSyncError("connector sync plan has an invalid window")
    start = _timestamp(window["start"], "window.start")
    end = _timestamp(window["end"], "window.end")
    if end <= start or window["duration_seconds"] != int((end - start).total_seconds()):
        raise ConnectorSyncError("connector sync plan window is inconsistent")
    if plan["schema_version"] == 2:
        capture = plan.get("capture_policy")
        if not isinstance(capture, dict) or set(capture) != {
            "source_window_strategy", "configured_overlap_seconds",
            "applied_overlap_seconds", "logical_start", "request_start", "end",
            "complete_update_capture_claimed",
        }:
            raise ConnectorSyncError("connector sync capture policy is invalid")
        configured = capture.get("configured_overlap_seconds")
        applied = capture.get("applied_overlap_seconds")
        if (
            not isinstance(configured, int) or isinstance(configured, bool) or configured < 0
            or not isinstance(applied, int) or isinstance(applied, bool)
            or not 0 <= applied <= configured
        ):
            raise ConnectorSyncError("connector sync overlap values are invalid")
        if capture.get("source_window_strategy") not in {
            "initial_window", "contiguous_window", "bounded_overlap_refetch",
        }:
            raise ConnectorSyncError("connector sync source window strategy is invalid")
        logical_start = _timestamp(capture.get("logical_start"), "capture_policy.logical_start")
        request_start = _timestamp(capture.get("request_start"), "capture_policy.request_start")
        capture_end = _timestamp(capture.get("end"), "capture_policy.end")
        if (
            logical_start != start or capture_end != end
            or request_start != logical_start - timedelta(seconds=applied)
            or (capture["source_window_strategy"] == "bounded_overlap_refetch") != (applied > 0)
            or capture.get("complete_update_capture_claimed") is not False
        ):
            raise ConnectorSyncError("connector sync capture policy is inconsistent")
    if not isinstance(plan.get("request"), dict):
        raise ConnectorSyncError("connector sync plan request must be an object")
    _assert_secret_free(plan["request"])
    if len(_canonical(plan["request"]).encode("utf-8")) > 256 * 1024:
        raise ConnectorSyncError("connector sync request exceeds 256 KiB")
    if plan.get("request_sha256") != _hash(plan["request"]):
        raise ConnectorSyncError("connector sync request fingerprint mismatch")
    if plan.get("plan_id") != _hash(_plan_core(plan))[:24]:
        raise ConnectorSyncError("connector sync plan fingerprint mismatch")
    checkpoint_hash = plan.get("expected_checkpoint_event_hash")
    if checkpoint_hash != "GENESIS" and not re.fullmatch(r"[0-9a-f]{64}", str(checkpoint_hash or "")):
        raise ConnectorSyncError("connector sync expected checkpoint hash is invalid")
    return dict(plan)


def build_sync_plan(
    runtime: BoxRuntime,
    connector: ConnectorDefinition,
    store: "ConnectorSyncStore",
    *,
    entity_id: str,
    stream_id: str,
    sync_mode: str,
    window_end: str,
    window_start: str | None = None,
    request_base: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    if connector.sync_window is None:
        raise ConnectorSyncError(f"Connector {connector.connector_id} has no incremental sync contract")
    runtime.reload()
    snapshot = runtime.snapshot()
    selected_packs = {pack["id"] for pack in snapshot["packs"]}
    if connector.pack_id not in selected_packs or connector.capability not in snapshot["capabilities"]:
        raise ConnectorSyncError("connector is not enabled by this Box")
    if entity_id not in runtime.entities.ids():
        raise ConnectorSyncError("connector sync requires a valid legal entity")
    stream_id = _stream(stream_id)
    if sync_mode not in {"incremental", "backfill"}:
        raise ConnectorSyncError("sync_mode must be incremental or backfill")
    stream_key = f"{connector.connector_id}:{entity_id}:{stream_id}"
    checkpoint = store.checkpoint(stream_key)
    if checkpoint and checkpoint["runtime_fingerprint"] != snapshot["fingerprint"]:
        raise ConnectorSyncError(
            "connector checkpoint belongs to a different Box fingerprint; review migration before reuse"
        )
    end = _timestamp(window_end, "window_end")
    clock = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if end > clock + timedelta(minutes=5):
        raise ConnectorSyncError("window_end must not be more than five minutes in the future")
    if sync_mode == "incremental" and checkpoint:
        start = _timestamp(checkpoint["window_end"], "checkpoint.window_end")
        if window_start is not None and _timestamp(window_start, "window_start") != start:
            raise ConnectorSyncError("window_start must equal the current committed checkpoint")
    else:
        if window_start is None:
            raise ConnectorSyncError("window_start is required before the first sync and for every backfill")
        start = _timestamp(window_start, "window_start")
    if end <= start:
        raise ConnectorSyncError("window_end must be later than window_start")
    duration = end - start
    maximum_days = (
        connector.sync_window.max_incremental_days
        if sync_mode == "incremental" else connector.sync_window.max_backfill_days
    )
    if duration > timedelta(days=maximum_days):
        raise ConnectorSyncError(f"connector sync window exceeds the {maximum_days}-day limit")
    request = dict(request_base or {})
    _assert_secret_free(request)
    reserved = {
        "mode", "default_entity_id", "starting_after",
        connector.sync_window.start_field, connector.sync_window.end_field,
    }
    conflict = sorted(reserved.intersection(request))
    if conflict:
        raise ConnectorSyncError("request_base contains sync-controlled fields: " + ", ".join(conflict))
    configured_overlap = connector.sync_window.incremental_overlap_seconds
    applied_overlap = (
        configured_overlap
        if sync_mode == "incremental" and checkpoint is not None and configured_overlap > 0
        else 0
    )
    request_start = start - timedelta(seconds=applied_overlap)
    request.update({
        "mode": "fetch",
        "default_entity_id": entity_id,
        connector.sync_window.start_field: _window_value(connector.sync_window, request_start),
        connector.sync_window.end_field: _window_value(connector.sync_window, end),
    })
    plan: dict[str, Any] = {
        "schema_version": 2,
        "runtime_fingerprint": snapshot["fingerprint"],
        "connector_id": connector.connector_id,
        "entity_id": entity_id,
        "stream_id": stream_id,
        "stream_key": stream_key,
        "sync_mode": sync_mode,
        "window": {
            "start": _render_timestamp(start),
            "end": _render_timestamp(end),
            "duration_seconds": int(duration.total_seconds()),
        },
        "capture_policy": {
            "source_window_strategy": (
                "bounded_overlap_refetch" if applied_overlap
                else "initial_window" if checkpoint is None
                else "contiguous_window"
            ),
            "configured_overlap_seconds": configured_overlap,
            "applied_overlap_seconds": applied_overlap,
            "logical_start": _render_timestamp(start),
            "request_start": _render_timestamp(request_start),
            "end": _render_timestamp(end),
            "complete_update_capture_claimed": False,
        },
        "expected_checkpoint_event_hash": checkpoint["event_hash"] if checkpoint else "GENESIS",
        "request": request,
        "request_sha256": _hash(request),
        "checkpoint_promotion_allowed": sync_mode == "incremental",
        "secret_values_included": False,
        "external_actions_performed": False,
    }
    plan["plan_id"] = _hash(plan)[:24]
    return validate_sync_plan(plan)


class ConnectorSyncStore:
    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()
        self.events_file = self.root / "connector_sync_events.jsonl"
        self.lock_file = self.root / ".connector_sync.lock"
        self._lock = threading.RLock()

    def _locked(self):
        if fcntl is None:
            raise ConnectorSyncError("connector sync store requires POSIX file locking")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        handle = self.lock_file.open("a+b")
        os.chmod(self.lock_file, 0o600)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        return handle

    def _events_unlocked(self) -> list[dict[str, Any]]:
        if not self.events_file.exists():
            return []
        if self.events_file.stat().st_size > MAX_LEDGER_BYTES:
            raise ConnectorSyncError("connector sync ledger exceeds 64 MiB")
        events: list[dict[str, Any]] = []
        previous = "GENESIS"
        with self.events_file.open("rb") as handle:
            for sequence, raw in enumerate(handle, 1):
                if len(raw) > MAX_EVENT_BYTES:
                    raise ConnectorSyncError("connector sync event exceeds 256 KiB")
                if sequence > MAX_EVENTS:
                    raise ConnectorSyncError("connector sync ledger exceeds 100000 events")
                try:
                    event = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ConnectorSyncError("connector sync ledger contains invalid JSON") from exc
                if not isinstance(event, dict) or event.get("schema_version") != 1:
                    raise ConnectorSyncError("connector sync ledger contains an unsupported event")
                if event.get("sequence") != sequence or event.get("previous_event_hash") != previous:
                    raise ConnectorSyncError("connector sync ledger sequence or chain is invalid")
                supplied = event.get("event_hash")
                unsigned = {key: value for key, value in event.items() if key != "event_hash"}
                if supplied != _hash(unsigned):
                    raise ConnectorSyncError("connector sync ledger event fingerprint mismatch")
                previous = supplied
                events.append(event)
        return events

    def _append_unlocked(self, event_type: str, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        events = self._events_unlocked()
        event = {
            "schema_version": 1,
            "sequence": len(events) + 1,
            "event_type": event_type,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "actor": _actor(actor),
            "payload": payload,
            "previous_event_hash": events[-1]["event_hash"] if events else "GENESIS",
        }
        event["event_hash"] = _hash(event)
        encoded = (_canonical(event) + "\n").encode("utf-8")
        if len(encoded) > MAX_EVENT_BYTES:
            raise ConnectorSyncError("connector sync event exceeds 256 KiB")
        with self.events_file.open("ab") as handle:
            os.chmod(self.events_file, 0o600)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        return event

    @staticmethod
    def _project(events: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], set[str]]:
        attempts: dict[str, dict[str, Any]] = {}
        checkpoints: dict[str, dict[str, Any]] = {}
        resolved: set[str] = set()
        for event in events:
            payload = event["payload"]
            if event["event_type"] == "CONNECTOR_SYNC_ATTEMPT_RECORDED":
                attempt = payload.get("attempt")
                if not isinstance(attempt, dict) or not ATTEMPT_PATTERN.fullmatch(str(attempt.get("attempt_id") or "")):
                    raise ConnectorSyncError("connector sync attempt event is invalid")
                if attempt["attempt_id"] in attempts:
                    raise ConnectorSyncError("connector sync attempt id is duplicated")
                attempts[attempt["attempt_id"]] = dict(
                    attempt, event_hash=event["event_hash"], recorded_at=event["recorded_at"],
                )
            elif event["event_type"] == "CONNECTOR_CHECKPOINT_COMMITTED":
                checkpoint = payload.get("checkpoint")
                attempt_id = payload.get("attempt_id")
                if not isinstance(checkpoint, dict) or attempt_id not in attempts:
                    raise ConnectorSyncError("connector checkpoint event is invalid")
                stream_key = checkpoint.get("stream_key")
                if not isinstance(stream_key, str) or not stream_key:
                    raise ConnectorSyncError("connector checkpoint stream key is invalid")
                checkpoints[stream_key] = dict(checkpoint, event_hash=event["event_hash"])
            elif event["event_type"] == "CONNECTOR_QUARANTINE_RESOLVED":
                attempt_id = payload.get("attempt_id")
                if attempt_id not in attempts or attempt_id in resolved:
                    raise ConnectorSyncError("connector quarantine resolution is invalid")
                resolved.add(attempt_id)
            else:
                raise ConnectorSyncError("connector sync ledger contains an unknown event type")
        return attempts, checkpoints, resolved

    def checkpoint(self, stream_key: str) -> dict[str, Any] | None:
        with self._lock:
            handle = self._locked()
            try:
                _, checkpoints, _ = self._project(self._events_unlocked())
                checkpoint = checkpoints.get(stream_key)
                return dict(checkpoint) if checkpoint else None
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                handle.close()

    def _record(
        self,
        plan: dict[str, Any],
        *,
        actor: str,
        status: str,
        result: dict[str, Any] | None = None,
        failure_summary: str | None = None,
    ) -> dict[str, Any]:
        plan = validate_sync_plan(plan)
        with self._lock:
            handle = self._locked()
            try:
                events = self._events_unlocked()
                _, checkpoints, _ = self._project(events)
                current = checkpoints.get(plan["stream_key"])
                current_hash = current["event_hash"] if current else "GENESIS"
                if current_hash != plan["expected_checkpoint_event_hash"]:
                    raise ConnectorSyncError("connector sync plan is stale because its checkpoint changed")
                attempt_id = uuid.uuid4().hex[:24]
                quality_summary = None
                source_summary = None
                batch_id = None
                sync_window_complete = False
                if result is not None:
                    connector = result.get("connector") or {}
                    batch = result.get("batch") or {}
                    quality = batch.get("quality") or {}
                    source = batch.get("source") or {}
                    if connector.get("connector_id") != plan["connector_id"]:
                        raise ConnectorSyncError("connector result does not match the sync plan")
                    source_summary = {key: source[key] for key in SAFE_SOURCE_FIELDS if key in source}
                    quality_summary = {
                        "batch_quality_ready": bool(quality.get("ready")),
                        "record_count": int(quality.get("record_count") or 0),
                        "dataset_counts": dict(quality.get("dataset_counts") or {}),
                        "rejected_count": int(quality.get("rejected_count") or 0),
                        "duplicate_count": len(quality.get("duplicate_business_keys") or []),
                    }
                    sync_window_complete = bool(
                        source.get("network_access_performed")
                        and quality_summary["rejected_count"] == 0
                        and quality_summary["duplicate_count"] == 0
                    )
                    batch_id = str(batch.get("batch_id") or "") or None
                attempt = {
                    "attempt_id": attempt_id,
                    "plan_id": plan["plan_id"],
                    "runtime_fingerprint": plan["runtime_fingerprint"],
                    "connector_id": plan["connector_id"],
                    "entity_id": plan["entity_id"],
                    "stream_id": plan["stream_id"],
                    "stream_key": plan["stream_key"],
                    "sync_mode": plan["sync_mode"],
                    "window": plan["window"],
                    "capture_policy": plan.get("capture_policy") or _legacy_capture_policy(
                        plan["window"]
                    ),
                    "expected_checkpoint_event_hash": plan["expected_checkpoint_event_hash"],
                    "request_sha256": plan["request_sha256"],
                    "status": status,
                    "batch_id": batch_id,
                    "source_summary": source_summary,
                    "quality_summary": quality_summary,
                    "sync_window_complete": sync_window_complete,
                    "checkpoint_candidate": bool(
                        status == "succeeded" and sync_window_complete
                        and plan["checkpoint_promotion_allowed"]
                    ),
                    "quarantined": status != "succeeded" or not sync_window_complete,
                    "failure_summary": _safe_failure(failure_summary) if failure_summary else None,
                    "raw_request_stored": False,
                    "raw_response_stored": False,
                    "secret_values_included": False,
                }
                event = self._append_unlocked(
                    "CONNECTOR_SYNC_ATTEMPT_RECORDED", {"attempt": attempt}, actor,
                )
                return dict(attempt, event_hash=event["event_hash"])
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                handle.close()

    def record_success(self, plan: dict[str, Any], result: dict[str, Any], *, actor: str) -> dict[str, Any]:
        return self._record(plan, actor=actor, status="succeeded", result=result)

    def record_failure(self, plan: dict[str, Any], error: Any, *, actor: str) -> dict[str, Any]:
        return self._record(plan, actor=actor, status="failed", failure_summary=str(error))

    def commit_checkpoint(
        self,
        attempt_id: str,
        *,
        runtime_fingerprint: str,
        actor: str,
        rationale: str,
        evidence_references: list[str],
    ) -> dict[str, Any]:
        if not ATTEMPT_PATTERN.fullmatch(str(attempt_id or "")):
            raise ConnectorSyncError("attempt_id is invalid")
        rationale = str(rationale or "").strip()
        if not rationale or len(rationale) > 500:
            raise ConnectorSyncError("rationale must be 1-500 characters")
        evidence = [str(item).strip() for item in evidence_references]
        if not evidence or any(not item or len(item) > 240 for item in evidence):
            raise ConnectorSyncError("at least one bounded evidence reference is required")
        with self._lock:
            handle = self._locked()
            try:
                events = self._events_unlocked()
                attempts, checkpoints, _ = self._project(events)
                attempt = attempts.get(attempt_id)
                if attempt is None:
                    raise ConnectorSyncError("connector sync attempt was not found")
                if attempt["runtime_fingerprint"] != runtime_fingerprint:
                    raise ConnectorSyncError("connector sync attempt belongs to a different Box fingerprint")
                if not attempt["checkpoint_candidate"] or attempt["sync_mode"] != "incremental":
                    raise ConnectorSyncError("connector sync attempt is not eligible for checkpoint promotion")
                if any(
                    event["event_type"] == "CONNECTOR_CHECKPOINT_COMMITTED"
                    and event["payload"].get("attempt_id") == attempt_id
                    for event in events
                ):
                    raise ConnectorSyncError("connector sync attempt checkpoint is already committed")
                current = checkpoints.get(attempt["stream_key"])
                current_hash = current["event_hash"] if current else "GENESIS"
                if current_hash != attempt["expected_checkpoint_event_hash"]:
                    raise ConnectorSyncError("connector checkpoint advanced after this attempt; stale commit refused")
                checkpoint = {
                    "stream_key": attempt["stream_key"],
                    "connector_id": attempt["connector_id"],
                    "entity_id": attempt["entity_id"],
                    "stream_id": attempt["stream_id"],
                    "runtime_fingerprint": attempt["runtime_fingerprint"],
                    "window_end": attempt["window"]["end"],
                    "capture_policy": attempt.get("capture_policy") or _legacy_capture_policy(
                        attempt["window"]
                    ),
                    "attempt_id": attempt_id,
                    "batch_id": attempt["batch_id"],
                    "rationale": rationale,
                    "evidence_references": evidence,
                }
                event = self._append_unlocked(
                    "CONNECTOR_CHECKPOINT_COMMITTED",
                    {"attempt_id": attempt_id, "checkpoint": checkpoint},
                    actor,
                )
                return dict(checkpoint, event_hash=event["event_hash"], committed=True)
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                handle.close()

    def resolve_quarantine(
        self,
        attempt_id: str,
        *,
        runtime_fingerprint: str,
        actor: str,
        resolution: str,
        rationale: str,
        replacement_attempt_id: str | None = None,
    ) -> dict[str, Any]:
        if resolution not in {"dismissed", "replaced"}:
            raise ConnectorSyncError("quarantine resolution must be dismissed or replaced")
        rationale = str(rationale or "").strip()
        if not rationale or len(rationale) > 500:
            raise ConnectorSyncError("rationale must be 1-500 characters")
        with self._lock:
            handle = self._locked()
            try:
                events = self._events_unlocked()
                attempts, _, resolved = self._project(events)
                attempt = attempts.get(attempt_id)
                if attempt is None or not attempt["quarantined"]:
                    raise ConnectorSyncError("attempt is not an unresolved quarantine candidate")
                if attempt["runtime_fingerprint"] != runtime_fingerprint:
                    raise ConnectorSyncError("connector sync attempt belongs to a different Box fingerprint")
                if attempt_id in resolved:
                    raise ConnectorSyncError("connector sync quarantine is already resolved")
                if resolution == "replaced":
                    replacement = attempts.get(str(replacement_attempt_id or ""))
                    if (
                        replacement is None
                        or replacement["stream_key"] != attempt["stream_key"]
                        or not replacement["sync_window_complete"]
                    ):
                        raise ConnectorSyncError("replacement attempt must be a complete run for the same stream")
                elif replacement_attempt_id is not None:
                    raise ConnectorSyncError("dismissed quarantine must not name a replacement attempt")
                payload = {
                    "attempt_id": attempt_id,
                    "resolution": resolution,
                    "replacement_attempt_id": replacement_attempt_id,
                    "rationale": rationale,
                }
                event = self._append_unlocked("CONNECTOR_QUARANTINE_RESOLVED", payload, actor)
                return dict(payload, resolved=True, event_hash=event["event_hash"])
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                handle.close()

    def status(self, *, runtime_fingerprint: str, limit: int = 100) -> dict[str, Any]:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 500:
            raise ConnectorSyncError("connector sync status limit must be an integer from 1 to 500")
        with self._lock:
            handle = self._locked()
            try:
                events = self._events_unlocked()
                attempts, checkpoints, resolved = self._project(events)
                selected = [
                    attempt for attempt in attempts.values()
                    if attempt["runtime_fingerprint"] == runtime_fingerprint
                ]
                quarantine = [
                    attempt for attempt in selected
                    if attempt["quarantined"] and attempt["attempt_id"] not in resolved
                ]
                candidates = [
                    attempt for attempt in selected
                    if attempt["checkpoint_candidate"]
                    and attempt["expected_checkpoint_event_hash"] == (
                        checkpoints.get(attempt["stream_key"], {}).get("event_hash") or "GENESIS"
                    )
                    and not any(
                        checkpoint.get("attempt_id") == attempt["attempt_id"]
                        for checkpoint in checkpoints.values()
                    )
                ]
                selected_checkpoints = [
                    item for item in checkpoints.values()
                    if item["runtime_fingerprint"] == runtime_fingerprint
                ]
                return {
                    "schema_version": 1,
                    "checkpoints": sorted(
                        selected_checkpoints,
                        key=lambda item: item["stream_key"],
                    )[:limit],
                    "checkpoint_candidates": sorted(
                        candidates, key=lambda item: item["recorded_at"], reverse=True,
                    )[:limit],
                    "quarantine": sorted(
                        quarantine, key=lambda item: item["recorded_at"], reverse=True,
                    )[:limit],
                    "counts": {
                        "attempts": len(selected),
                        "checkpoints": sum(
                            item["runtime_fingerprint"] == runtime_fingerprint
                            for item in checkpoints.values()
                        ),
                        "checkpoint_candidates": len(candidates),
                        "quarantine": len(quarantine),
                    },
                    "list_limit": limit,
                    "counts_may_be_truncated": any(
                        count > limit for count in (
                            len(selected_checkpoints), len(candidates), len(quarantine),
                        )
                    ),
                    "raw_requests_included": False,
                    "raw_responses_included": False,
                    "secret_values_included": False,
                    "external_actions_performed": False,
                }
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                handle.close()

    def verify(self) -> dict[str, Any]:
        with self._lock:
            handle = self._locked()
            try:
                events = self._events_unlocked()
                attempts, checkpoints, resolved = self._project(events)
                return {
                    "valid": True,
                    "integrity": "sha256_hash_chain",
                    "integrity_limit": "tamper_evident_not_immutable",
                    "event_count": len(events),
                    "attempt_count": len(attempts),
                    "checkpoint_count": len(checkpoints),
                    "resolved_quarantine_count": len(resolved),
                    "chain_head": events[-1]["event_hash"] if events else "GENESIS",
                    "raw_requests_stored": False,
                    "raw_responses_stored": False,
                    "external_actions_performed": False,
                }
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                handle.close()


def execute_sync_plan(
    runtime: BoxRuntime,
    registry: ConnectorRegistry,
    store: ConnectorSyncStore,
    plan: dict[str, Any],
    *,
    actor: str,
) -> dict[str, Any]:
    plan = validate_sync_plan(plan)
    runtime.reload()
    if runtime.snapshot()["fingerprint"] != plan["runtime_fingerprint"]:
        raise ConnectorSyncError("connector sync plan belongs to a different Box fingerprint")
    connector = registry.definition(plan["connector_id"])
    if connector.sync_window is None:
        raise ConnectorSyncError("connector no longer exposes an incremental sync contract")
    try:
        result = registry.dispatch(runtime, plan["connector_id"], plan["request"])
    except Exception as exc:
        record = store.record_failure(plan, exc, actor=actor)
        if isinstance(exc, (ConnectorError, ConnectorSyncError)):
            raise ConnectorSyncError(
                f"connector sync failed and was quarantined as attempt {record['attempt_id']}: "
                f"{_safe_failure(exc)}"
            ) from exc
        raise ConnectorSyncError(
            f"connector sync failed and was quarantined as attempt {record['attempt_id']}"
        ) from exc
    record = store.record_success(plan, result, actor=actor)
    return {
        "connector_result": result,
        "sync_attempt": record,
        "checkpoint_advanced": False,
        "requires_checkpoint_commit": bool(record["checkpoint_candidate"]),
        "external_actions_performed": False,
    }
