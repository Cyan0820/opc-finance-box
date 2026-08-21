from __future__ import annotations

import json
import hashlib
import re
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .box_compiler import build_pipeline_runtime_catalog, preflight_pipeline_request
from .box_pipeline import dispatch_box_pipeline_request
from .box_runtime import BoxRuntime
from .pipeline_run_store import PipelineRunStore, PipelineRunStoreError


class PipelineScheduleError(ValueError):
    """Raised when a schedule cannot be trusted or safely executed."""


JOB_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$")
MAX_PLAN_BYTES = 1024 * 1024
MAX_REQUEST_BYTES = 28 * 1024 * 1024
ROOT_FIELDS = {"schema_version", "timezone", "jobs"}
LEGACY_JOB_FIELDS = {
    "job_id", "enabled", "pipeline_id", "entity_id", "request_file", "cadence",
    "execution_window_minutes", "max_attempts", "retry_delay_minutes", "lease_seconds",
    "operator", "alert_owner", "approved_by", "approved_at",
    "approval_fingerprint",
}
JOB_FIELDS = LEGACY_JOB_FIELDS | {"request_fingerprint"}
APPROVAL_BASIS_FIELDS = {
    "job_id", "pipeline_id", "entity_id", "request_file", "request_fingerprint", "cadence",
    "execution_window_minutes", "max_attempts", "retry_delay_minutes", "lease_seconds",
    "operator", "alert_owner",
}


def _json_fingerprint(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def pipeline_request_fingerprint(request: dict[str, Any]) -> str:
    """Fingerprint the semantic JSON request that a schedule approval authorizes."""
    if not isinstance(request, dict):
        raise PipelineScheduleError("Pipeline request must contain a JSON object")
    return _json_fingerprint(request)


def fingerprint_pipeline_request_file(path: str | Path) -> dict[str, Any]:
    request_path = Path(path).expanduser().resolve()
    try:
        if not request_path.is_file() or request_path.stat().st_size > MAX_REQUEST_BYTES:
            raise PipelineScheduleError(
                "Pipeline request file must be a regular JSON file no larger than 28 MiB"
            )
        request = json.loads(request_path.read_text(encoding="utf-8"))
    except PipelineScheduleError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PipelineScheduleError("Pipeline request file must contain valid UTF-8 JSON") from exc
    return {
        "algorithm": "sha256_canonical_json_v1",
        "request_fingerprint": pipeline_request_fingerprint(request),
        "raw_request_returned": False,
        "external_actions_performed": False,
    }


def _bounded_text(value: Any, field: str, *, required: bool = True) -> str | None:
    if value is None and not required:
        return None
    text = str(value or "").strip()
    if not text or len(text) > 80 or any(ord(character) < 32 for character in text):
        raise PipelineScheduleError(f"{field} must be 1-80 printable characters")
    return text


def _integer(value: Any, field: str, minimum: int, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise PipelineScheduleError(f"{field} must be an integer from {minimum} to {maximum}")
    return value


def _timestamp(value: Any, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError as exc:
        raise PipelineScheduleError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise PipelineScheduleError(f"{field} must include a timezone offset")
    return parsed.astimezone(timezone.utc)


def _local_time(value: Any, field: str) -> time:
    text = str(value or "")
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", text):
        raise PipelineScheduleError(f"{field} must use 24-hour HH:MM")
    hour, minute = (int(part) for part in text.split(":"))
    return time(hour, minute)


def _validate_cadence(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PipelineScheduleError(f"{field} must be an object")
    kind = value.get("kind")
    expected = {
        "daily": {"kind", "local_time"},
        "weekly": {"kind", "local_time", "weekdays"},
        "monthly": {"kind", "local_time", "day"},
    }.get(kind)
    if expected is None:
        raise PipelineScheduleError(f"{field}.kind must be daily, weekly or monthly")
    if set(value) != expected:
        raise PipelineScheduleError(f"{field} contains unsupported or missing fields")
    _local_time(value.get("local_time"), f"{field}.local_time")
    if kind == "weekly":
        weekdays = value.get("weekdays")
        if (
            not isinstance(weekdays, list) or not weekdays or len(weekdays) > 7
            or any(not isinstance(item, int) or isinstance(item, bool) or not 0 <= item <= 6 for item in weekdays)
            or len(set(weekdays)) != len(weekdays)
        ):
            raise PipelineScheduleError(f"{field}.weekdays requires unique integers 0-6")
    if kind == "monthly":
        _integer(value.get("day"), f"{field}.day", 1, 28)
    return dict(value)


def schedule_job_approval_fingerprint(job: dict[str, Any]) -> str:
    """Bind approval to every operational job field while allowing enabled to toggle."""
    if not isinstance(job, dict) or not APPROVAL_BASIS_FIELDS <= set(job):
        raise PipelineScheduleError("job is missing fields required for approval fingerprint")
    basis = {key: job[key] for key in sorted(APPROVAL_BASIS_FIELDS)}
    return _json_fingerprint(basis)


def load_pipeline_schedule(path: str | Path) -> dict[str, Any]:
    """Load a strict schedule. This never reads a Pipeline request or dispatches a source."""
    schedule_path = Path(path).expanduser().resolve()
    try:
        if not schedule_path.is_file() or schedule_path.stat().st_size > MAX_PLAN_BYTES:
            raise PipelineScheduleError("schedule file must be a regular JSON file no larger than 1 MiB")
        payload = json.loads(schedule_path.read_text(encoding="utf-8"))
    except PipelineScheduleError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PipelineScheduleError("schedule file must contain valid UTF-8 JSON") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") not in {1, 2}:
        raise PipelineScheduleError("schedule requires schema_version 1 or 2")
    if set(payload) != ROOT_FIELDS:
        raise PipelineScheduleError("schedule contains unsupported or missing root fields")
    schema_version = payload["schema_version"]
    timezone_name = _bounded_text(payload.get("timezone"), "timezone")
    try:
        ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise PipelineScheduleError("schedule timezone is not available in the IANA timezone database") from exc
    jobs = payload.get("jobs")
    if not isinstance(jobs, list) or not jobs or len(jobs) > 100:
        raise PipelineScheduleError("schedule requires 1-100 jobs")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(jobs):
        prefix = f"jobs[{index}]"
        expected_job_fields = JOB_FIELDS if schema_version == 2 else LEGACY_JOB_FIELDS
        if not isinstance(raw, dict) or set(raw) != expected_job_fields:
            raise PipelineScheduleError(f"{prefix} contains unsupported or missing fields")
        job_id = str(raw.get("job_id") or "")
        if not JOB_ID_PATTERN.fullmatch(job_id) or job_id in seen:
            raise PipelineScheduleError(f"{prefix}.job_id is invalid or duplicated")
        if not isinstance(raw.get("enabled"), bool):
            raise PipelineScheduleError(f"{prefix}.enabled must be boolean")
        pipeline_id = _bounded_text(raw.get("pipeline_id"), f"{prefix}.pipeline_id")
        entity_id = _bounded_text(raw.get("entity_id"), f"{prefix}.entity_id")
        request_file = _bounded_text(raw.get("request_file"), f"{prefix}.request_file")
        request_path = Path(request_file)
        if request_path.is_absolute() or ".." in request_path.parts or request_path.suffix.lower() != ".json":
            raise PipelineScheduleError(f"{prefix}.request_file must be a relative in-directory JSON path")
        request_fingerprint = raw.get("request_fingerprint")
        if request_fingerprint is not None and (
            not isinstance(request_fingerprint, str)
            or not re.fullmatch(r"[0-9a-f]{64}", request_fingerprint)
        ):
            raise PipelineScheduleError(f"{prefix}.request_fingerprint must be 64 lowercase hex characters")
        operator = _bounded_text(raw.get("operator"), f"{prefix}.operator")
        alert_owner = _bounded_text(raw.get("alert_owner"), f"{prefix}.alert_owner")
        approved_by = _bounded_text(raw.get("approved_by"), f"{prefix}.approved_by", required=False)
        approved_at_raw = raw.get("approved_at")
        approved_at = None if approved_at_raw is None else _timestamp(
            approved_at_raw, f"{prefix}.approved_at",
        ).isoformat()
        supplied_fingerprint = raw.get("approval_fingerprint")
        if supplied_fingerprint is not None and (
            not isinstance(supplied_fingerprint, str)
            or not re.fullmatch(r"[0-9a-f]{64}", supplied_fingerprint)
        ):
            raise PipelineScheduleError(f"{prefix}.approval_fingerprint must be 64 lowercase hex characters")
        approval_presence = (approved_by is not None, approved_at is not None, supplied_fingerprint is not None)
        if len(set(approval_presence)) != 1:
            raise PipelineScheduleError(
                f"{prefix} approval requires approved_by, approved_at and approval_fingerprint together"
            )
        if schema_version == 1 and (raw["enabled"] or any(approval_presence)):
            raise PipelineScheduleError(
                f"{prefix} uses legacy schema_version 1; migrate to version 2 and bind request_fingerprint"
            )
        if request_fingerprint is None and (raw["enabled"] or any(approval_presence)):
            raise PipelineScheduleError(
                f"{prefix}.request_fingerprint is required before approval or enablement"
            )
        normalized_job = {
            "job_id": job_id,
            "enabled": raw["enabled"],
            "pipeline_id": pipeline_id,
            "entity_id": entity_id,
            "request_file": request_file,
            "request_fingerprint": request_fingerprint,
            "cadence": _validate_cadence(raw.get("cadence"), f"{prefix}.cadence"),
            "execution_window_minutes": _integer(
                raw.get("execution_window_minutes"), f"{prefix}.execution_window_minutes", 1, 1440,
            ),
            "max_attempts": _integer(raw.get("max_attempts"), f"{prefix}.max_attempts", 1, 5),
            "retry_delay_minutes": _integer(
                raw.get("retry_delay_minutes"), f"{prefix}.retry_delay_minutes", 1, 1440,
            ),
            "lease_seconds": _integer(raw.get("lease_seconds"), f"{prefix}.lease_seconds", 60, 3600),
            "operator": operator,
            "alert_owner": alert_owner,
            "approved_by": approved_by,
            "approved_at": approved_at,
            "approval_fingerprint": supplied_fingerprint,
        }
        expected_fingerprint = (
            schedule_job_approval_fingerprint(normalized_job)
            if request_fingerprint is not None else None
        )
        normalized_job["expected_approval_fingerprint"] = expected_fingerprint
        if raw["enabled"] and approved_by is None:
            raise PipelineScheduleError(f"{prefix} cannot be enabled without explicit approval")
        if supplied_fingerprint is not None and supplied_fingerprint != expected_fingerprint:
            raise PipelineScheduleError(f"{prefix} approval_fingerprint does not match the current job")
        normalized.append(normalized_job)
        seen.add(job_id)
    trusted_payload = {
        "schema_version": schema_version,
        "timezone": timezone_name,
        "jobs": [{
            key: job[key] for key in sorted(
                JOB_FIELDS if schema_version == 2 else LEGACY_JOB_FIELDS
            )
        } for job in normalized],
    }
    return {
        "schema_version": schema_version,
        "timezone": timezone_name,
        "jobs": normalized,
        "plan_fingerprint": _json_fingerprint(trusted_payload),
        "schedule_path": str(schedule_path),
        "request_root": str(schedule_path.parent),
        "secrets_included": False,
        "external_actions_performed": False,
    }


def _candidate(local_date: date, local_at: time, zone: ZoneInfo) -> datetime | None:
    value = datetime.combine(local_date, local_at, tzinfo=zone).replace(fold=0)
    # A spring-forward wall clock time does not exist. Round-tripping makes that
    # visible; skip it rather than silently moving the financial job.
    round_trip = value.astimezone(timezone.utc).astimezone(zone)
    if (round_trip.date(), round_trip.time().replace(tzinfo=None)) != (local_date, local_at):
        return None
    return value


def _latest_occurrence(job: dict[str, Any], now: datetime, zone: ZoneInfo) -> datetime:
    local_now = now.astimezone(zone)
    cadence = job["cadence"]
    local_at = _local_time(cadence["local_time"], "cadence.local_time")
    kind = cadence["kind"]
    search_days = 40 if kind == "monthly" else 8
    for offset in range(search_days):
        day = local_now.date() - timedelta(days=offset)
        if kind == "weekly" and day.weekday() not in set(cadence["weekdays"]):
            continue
        if kind == "monthly" and day.day != cadence["day"]:
            continue
        candidate = _candidate(day, local_at, zone)
        if candidate is not None and candidate <= local_now:
            return candidate.astimezone(timezone.utc)
    # Monthly days are limited to 1-28, so this indicates corrupt internal logic.
    raise PipelineScheduleError(f"could not derive an occurrence for job {job['job_id']}")


def _load_request(plan: dict[str, Any], job: dict[str, Any]) -> dict[str, Any]:
    root = Path(plan["request_root"]).resolve()
    path = (root / job["request_file"]).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise PipelineScheduleError(f"job {job['job_id']} request_file escapes the schedule directory") from exc
    try:
        if not path.is_file() or path.stat().st_size > MAX_REQUEST_BYTES:
            raise PipelineScheduleError(
                f"job {job['job_id']} request_file must be a regular JSON file no larger than 28 MiB"
            )
        request = json.loads(path.read_text(encoding="utf-8"))
    except PipelineScheduleError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PipelineScheduleError(f"job {job['job_id']} request_file is invalid") from exc
    if not isinstance(request, dict):
        raise PipelineScheduleError(f"job {job['job_id']} request_file must contain an object")
    return request


def inspect_pipeline_schedule(
    plan_path: str | Path,
    runtime: BoxRuntime,
    store: PipelineRunStore,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return deterministic due/retry state. No Connector or Service is dispatched."""
    plan = load_pipeline_schedule(plan_path)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise PipelineScheduleError("now must include a timezone offset")
    current = current.astimezone(timezone.utc)
    zone = ZoneInfo(plan["timezone"])
    catalog = build_pipeline_runtime_catalog(runtime)
    enabled_pipelines = {item["pipeline_id"] for item in catalog["pipelines"] if item["implementation_status"] == "executable"}
    entity_ids = {entity.entity_id for entity in runtime.entities.all()}
    jobs: list[dict[str, Any]] = []
    for job in plan["jobs"]:
        occurrence = _latest_occurrence(job, current, zone)
        occurrence_id = f"{job['job_id']}:{occurrence.strftime('%Y%m%dT%H%M%SZ')}"
        age_minutes = int((current - occurrence).total_seconds() // 60)
        status = "disabled"
        blockers: list[str] = []
        preflight: dict[str, Any] | None = None
        observed_request_fingerprint: str | None = None
        history = store.schedule_occurrence_status(
            runtime_fingerprint=catalog["runtime_fingerprint"],
            job_id=job["job_id"], occurrence_id=occurrence_id, now=current,
        )
        if job["enabled"]:
            if _timestamp(job["approved_at"], "approved_at") > current:
                blockers.append("schedule approval timestamp is in the future")
            if job["pipeline_id"] not in enabled_pipelines:
                blockers.append("pipeline_id is not executable in the current Box")
            if job["entity_id"] not in entity_ids:
                blockers.append("entity_id is not configured in the current Box")
            try:
                request = _load_request(plan, job)
                observed_request_fingerprint = pipeline_request_fingerprint(request)
                if observed_request_fingerprint != job["request_fingerprint"]:
                    blockers.append("request content does not match the approved request_fingerprint")
                if request.get("pipeline_id") != job["pipeline_id"]:
                    blockers.append("request pipeline_id does not match the schedule job")
                payload = request.get("payload") if isinstance(request.get("payload"), dict) else {}
                if payload.get("entity_id") != job["entity_id"]:
                    blockers.append("request payload.entity_id does not match the schedule job")
                preflight = preflight_pipeline_request(runtime, request)
                blockers.extend(preflight["blockers"])
            except PipelineScheduleError as exc:
                blockers.append(str(exc))
            if blockers:
                status = "blocked_configuration"
            elif history["latest_ready"]:
                status = "completed"
            elif history["active_claim"]:
                status = "leased"
            elif history["attempt_count"] >= job["max_attempts"]:
                status = "retry_exhausted"
            elif history["attempt_count"] and not history["latest_retryable"]:
                status = "blocked_non_retryable"
            elif history["attempt_count"]:
                next_retry = _timestamp(history["latest_recorded_at"], "latest_recorded_at") + timedelta(
                    minutes=job["retry_delay_minutes"],
                )
                status = "retry_due" if current >= next_retry else "retry_wait"
            elif age_minutes <= job["execution_window_minutes"]:
                status = "due"
            else:
                status = "missed_window"
        jobs.append({
            **job,
            "scheduled_for": occurrence.isoformat(),
            "scheduled_for_local": occurrence.astimezone(zone).isoformat(),
            "occurrence_id": occurrence_id,
            "age_minutes": age_minutes,
            "status": status,
            "runnable_now": status in {"due", "retry_due"},
            "blockers": list(dict.fromkeys(blockers)),
            "preflight": preflight,
            "observed_request_fingerprint": observed_request_fingerprint,
            "history": history,
            "source_access_performed": False,
            "dispatch_performed": False,
        })
    return {
        "schema_version": plan["schema_version"],
        "runtime_fingerprint": catalog["runtime_fingerprint"],
        "plan_fingerprint": plan["plan_fingerprint"],
        "timezone": plan["timezone"],
        "evaluated_at": current.isoformat(),
        "dst_policy": "skip_nonexistent_wall_time_use_first_ambiguous_fold",
        "jobs": jobs,
        "counts": {
            "total": len(jobs),
            "runnable_now": sum(item["runnable_now"] for item in jobs),
            "blocked": sum(item["status"].startswith("blocked") for item in jobs),
            "missed_window": sum(item["status"] == "missed_window" for item in jobs),
        },
        "dispatch_performed": False,
        "external_actions_performed": False,
    }


def run_due_pipeline_schedule(
    plan_path: str | Path,
    runtime: BoxRuntime,
    store: PipelineRunStore,
    *,
    actor: str,
    now: datetime | None = None,
    job_id: str | None = None,
) -> dict[str, Any]:
    """Run due jobs only after an atomic lease; every result is recorded as candidate-only."""
    current = now or datetime.now(timezone.utc)
    inspection = inspect_pipeline_schedule(plan_path, runtime, store, now=current)
    plan = load_pipeline_schedule(plan_path)
    if plan["plan_fingerprint"] != inspection["plan_fingerprint"]:
        raise PipelineScheduleError("schedule changed after inspection; retry with the current approved plan")
    selected = [item for item in inspection["jobs"] if job_id is None or item["job_id"] == job_id]
    if job_id is not None and not selected:
        raise PipelineScheduleError("requested schedule job_id was not found")
    jobs_by_id = {item["job_id"]: item for item in plan["jobs"]}
    outcomes: list[dict[str, Any]] = []
    for item in selected:
        if not item["runnable_now"]:
            outcomes.append({
                "job_id": item["job_id"], "occurrence_id": item["occurrence_id"],
                "status": item["status"], "dispatched": False,
            })
            continue
        job = jobs_by_id[item["job_id"]]
        if actor != job["operator"]:
            outcomes.append({
                "job_id": item["job_id"], "occurrence_id": item["occurrence_id"],
                "status": "operator_mismatch", "dispatched": False,
            })
            continue
        request = _load_request(plan, job)
        if pipeline_request_fingerprint(request) != item["observed_request_fingerprint"]:
            outcomes.append({
                "job_id": item["job_id"], "occurrence_id": item["occurrence_id"],
                "status": "request_changed", "dispatched": False,
            })
            continue
        try:
            claim = store.reserve_schedule_occurrence(
                runtime_fingerprint=inspection["runtime_fingerprint"],
                job_id=item["job_id"], occurrence_id=item["occurrence_id"],
                scheduled_for=item["scheduled_for"], actor=actor,
                max_attempts=job["max_attempts"],
                retry_delay_minutes=job["retry_delay_minutes"],
                lease_seconds=job["lease_seconds"], now=current,
            )
        except PipelineRunStoreError as exc:
            outcomes.append({
                "job_id": item["job_id"], "occurrence_id": item["occurrence_id"],
                "status": "lease_not_acquired", "reason": str(exc), "dispatched": False,
            })
            continue
        failure_type: str | None = None
        try:
            result = dispatch_box_pipeline_request(runtime, request)
        except Exception as exc:  # The claim must still become an auditable terminal attempt.
            failure_type = type(exc).__name__
            result = {
                "pipeline": {
                    "pipeline_id": job["pipeline_id"], "run_id": None,
                    "executed_at": current.isoformat(), "required_review_gates": [],
                },
                "ready": False,
                "blocked_at": "dispatch_exception",
                "retryable": False,
                "failure_code": f"dispatch_exception:{failure_type}",
                "network_access_performed": False,
                "external_actions_performed": False,
            }
        record = store.record(
            runtime.snapshot(), request, result, actor=actor,
            execution_context={
                "trigger_kind": "schedule", "job_id": job["job_id"],
                "occurrence_id": item["occurrence_id"], "claim_id": claim["claim_id"],
                "scheduled_for": item["scheduled_for"],
            },
        )
        outcomes.append({
            "job_id": item["job_id"], "occurrence_id": item["occurrence_id"],
            "status": "ready" if record["ready"] else "blocked",
            "dispatched": True, "attempt_id": record["attempt_id"],
            "retryable": record["retryable"], "failure_type": failure_type,
            "review_status": record["review_status"],
            "external_actions_performed": False,
        })
    return {
        "schema_version": 1,
        "evaluated_at": inspection["evaluated_at"],
        "runtime_fingerprint": inspection["runtime_fingerprint"],
        "outcomes": outcomes,
        "counts": {
            "selected": len(selected),
            "dispatched": sum(item["dispatched"] for item in outcomes),
            "ready": sum(item["status"] == "ready" for item in outcomes),
            "blocked": sum(item["status"] == "blocked" for item in outcomes),
        },
        "posting_performed": False,
        "external_actions_performed": False,
    }
