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

from .month_close_portfolio_evidence import (
    MonthClosePortfolioEvidenceError,
    month_close_portfolio_source_fingerprint,
    month_close_result_to_portfolio_source,
    verify_portfolio_source_record,
)

try:
    import fcntl
except ImportError:  # pragma: no cover - supported production targets are POSIX
    fcntl = None


ATTEMPT_ID_PATTERN = re.compile(r"^[0-9a-f]{24}$")
CLAIM_ID_PATTERN = re.compile(r"^[0-9a-f]{24}$")
SCHEDULE_KEY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$")
FAILURE_CODE_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,119}$")
PERIOD_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
MAX_LEDGER_BYTES = 64 * 1024 * 1024
MAX_EVENT_BYTES = 1024 * 1024
MAX_EVENTS = 100_000
BACKUP_MANIFEST_NAME = "pipeline_runs_backup.json"
REVIEW_DECISIONS = {"approved", "rejected", "needs_more_evidence"}
GATE_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,79}$")
SAFE_LINEAGE_FIELDS = {
    "run_id", "entity_id", "entity_ids", "period", "batch_id", "balance_batch_id", "payout_batch_id",
    "connector_batch_ids", "accepted_record_count", "service_executed", "service_id",
    "service_ids", "service_entity_ids", "bank_evidence_count", "processor_link_evidence_count",
    "contract_mapping_evidence_count",
    "inventory_mapping_evidence_count",
    "marketplace_id", "dataset_counts",
    "source_run_ledger_verified", "source_attempt_ids",
}


class PipelineRunStoreError(RuntimeError):
    """Raised when pipeline run history cannot be trusted or safely persisted."""


def _canonical(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise PipelineRunStoreError("pipeline run evidence must be JSON-serializable") from exc


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _clean_actor(value: Any) -> str:
    actor = str(value or "").strip()
    if not actor or len(actor) > 80 or any(ord(char) < 32 for char in actor):
        raise PipelineRunStoreError("actor must be 1-80 printable characters")
    return actor


def _aware_datetime(value: Any, field: str) -> datetime:
    text = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PipelineRunStoreError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise PipelineRunStoreError(f"{field} must include a timezone offset")
    return parsed.astimezone(timezone.utc)


def _schedule_key(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not SCHEDULE_KEY_PATTERN.fullmatch(text):
        raise PipelineRunStoreError(f"{field} is invalid")
    return text


def _schedule_claims(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    claims: list[dict[str, Any]] = []
    seen: set[str] = set()
    for event in events:
        if event.get("event_type") != "PIPELINE_SCHEDULE_CLAIMED":
            continue
        claim = event.get("claim")
        if not isinstance(claim, dict):
            raise PipelineRunStoreError("pipeline schedule claim event has no claim object")
        claim_id = claim.get("claim_id")
        if not CLAIM_ID_PATTERN.fullmatch(str(claim_id or "")) or claim_id in seen:
            raise PipelineRunStoreError("pipeline schedule claim event has an invalid claim_id")
        _schedule_key(claim.get("job_id"), "schedule claim job_id")
        _schedule_key(claim.get("occurrence_id"), "schedule claim occurrence_id")
        _aware_datetime(claim.get("scheduled_for"), "schedule claim scheduled_for")
        _aware_datetime(claim.get("claimed_at"), "schedule claim claimed_at")
        _aware_datetime(claim.get("lease_expires_at"), "schedule claim lease_expires_at")
        if not isinstance(claim.get("runtime_fingerprint"), str) or not claim["runtime_fingerprint"]:
            raise PipelineRunStoreError("pipeline schedule claim requires runtime_fingerprint")
        seen.add(claim_id)
        claims.append(dict(claim))
    return claims


def _service_summaries(result: dict[str, Any]) -> list[dict[str, Any]]:
    services = result.get("services") or {}
    if not isinstance(services, dict):
        return []
    output = []
    for stage, invocation in sorted(services.items()):
        if not isinstance(invocation, dict):
            continue
        service = invocation.get("service") or {}
        service_output = invocation.get("output") or {}
        output.append({
            "stage": str(stage),
            "service_id": service.get("service_id"),
            "action_class": service.get("action_class"),
            "entity_ids": list(service.get("entity_ids") or []),
            "ready": bool(service_output.get("ready")),
        })
    return output


def _connector_summaries(result: dict[str, Any]) -> list[dict[str, Any]]:
    batches = result.get("connector_batches") or {}
    if not isinstance(batches, dict):
        return []
    output = []
    for connector_id, value in sorted(batches.items()):
        if not isinstance(value, dict):
            continue
        quality = value.get("quality") or {}
        output.append({
            "connector_id": str(connector_id),
            "batch_id": value.get("batch_id"),
            "quality_ready": bool(quality.get("ready")),
            "record_count": quality.get("record_count"),
            "rejected_count": quality.get("rejected_count"),
        })
    return output


def _safe_lineage(result: dict[str, Any]) -> dict[str, Any]:
    lineage = result.get("lineage") or {}
    if not isinstance(lineage, dict):
        return {}
    return {key: lineage[key] for key in sorted(SAFE_LINEAGE_FIELDS & set(lineage))}


def _safe_metric_assembly(result: dict[str, Any]) -> dict[str, Any] | None:
    collection = result.get("cfo_metric_operand_assembly")
    if not isinstance(collection, dict):
        return None
    assemblies = collection.get("assemblies") or []
    if not isinstance(assemblies, list):
        raise PipelineRunStoreError("metric operand assembly collection is invalid")
    summaries = []
    for item in assemblies:
        if not isinstance(item, dict):
            raise PipelineRunStoreError("metric operand assembly item is invalid")
        preview = item.get("evaluation_preview") or {}
        summaries.append({
            "assembly_id": item.get("assembly_id"),
            "entity_id": item.get("entity_id"),
            "period": item.get("period"),
            "currency": item.get("currency"),
            "dimension_scope": item.get("dimension_scope"),
            "metric_type_ids": list(item.get("metric_type_ids") or []),
            "confirmed_control_type_ids": list(item.get("confirmed_control_type_ids") or []),
            "pending_control_type_ids": list(item.get("pending_control_type_ids") or []),
            "evaluation_status": item.get("evaluation_status"),
            "evaluation_input_fingerprint": preview.get("input_fingerprint"),
            "operand_values_persisted": False,
            "evaluation_values_persisted": False,
        })
    return {
        "source_type_id": collection.get("source_type_id"),
        "source_id": collection.get("source_id"),
        "source_result_fingerprint": collection.get("source_result_fingerprint"),
        "coverage_status": collection.get("coverage_status"),
        "coverage_blocker_type_ids": list(collection.get("coverage_blocker_type_ids") or []),
        "assembly_count": len(summaries),
        "assemblies": summaries,
        "raw_source_records_persisted": False,
        "operand_values_persisted": False,
        "evaluation_values_persisted": False,
    }


def build_pipeline_run_record(
    runtime_snapshot: dict[str, Any],
    request: dict[str, Any],
    result: dict[str, Any],
    *,
    actor: str,
    execution_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a secret-free control record; raw request and full result never leave this function."""
    if not isinstance(request, dict) or not isinstance(result, dict):
        raise PipelineRunStoreError("request and result must be JSON objects")
    if result.get("external_actions_performed") is not False:
        raise PipelineRunStoreError("only pipelines that explicitly performed no external actions may be recorded")
    request_pipeline_id = request.get("pipeline_id")
    result_pipeline = result.get("pipeline") or {}
    pipeline_id = result_pipeline.get("pipeline_id")
    if not isinstance(request_pipeline_id, str) or pipeline_id != request_pipeline_id:
        raise PipelineRunStoreError("request and result pipeline_id do not match")
    runtime = runtime_snapshot.get("runtime") if isinstance(runtime_snapshot.get("runtime"), dict) else runtime_snapshot
    runtime_fingerprint = runtime.get("fingerprint")
    if not isinstance(runtime_fingerprint, str) or not runtime_fingerprint:
        raise PipelineRunStoreError("runtime snapshot requires fingerprint")
    product = runtime_snapshot.get("product") or {}
    payload = request.get("payload") or {}
    entity_id = payload.get("entity_id") if isinstance(payload, dict) else None
    period_value = payload.get("period") if isinstance(payload, dict) else None
    period = (
        period_value
        if isinstance(period_value, str) and PERIOD_PATTERN.fullmatch(period_value)
        else None
    )
    if pipeline_id == "finance.month_close_control":
        result_lineage = result.get("lineage")
        if not isinstance(result_lineage, dict):
            raise PipelineRunStoreError("month-close result requires entity-period lineage")
        if (
            period is None
            or not isinstance(entity_id, str)
            or result_lineage.get("period") != period
            or result_lineage.get("entity_id") != entity_id
        ):
            raise PipelineRunStoreError(
                "month-close request and result entity-period lineage do not match"
            )
    if period is None:
        result_lineage = result.get("lineage") or {}
        lineage_period = result_lineage.get("period") if isinstance(result_lineage, dict) else None
        if isinstance(lineage_period, str) and PERIOD_PATTERN.fullmatch(lineage_period):
            period = lineage_period
    request_fingerprint = _hash({
        "runtime_fingerprint": runtime_fingerprint,
        "request": request,
    })
    run_id = result_pipeline.get("run_id")
    idempotency_key = str(run_id or request_fingerprint)
    ready = bool(result.get("ready"))
    required_review_gates = result_pipeline.get("required_review_gates") or []
    if not isinstance(required_review_gates, list) or any(
        not isinstance(gate, str) or not GATE_PATTERN.fullmatch(gate)
        for gate in required_review_gates
    ) or len(set(required_review_gates)) != len(required_review_gates):
        raise PipelineRunStoreError("pipeline result has invalid required_review_gates")
    portfolio_source_fingerprint = None
    if pipeline_id == "finance.month_close_control" and ready:
        try:
            portfolio_source_fingerprint = month_close_portfolio_source_fingerprint(
                month_close_result_to_portfolio_source(result)
            )
        except MonthClosePortfolioEvidenceError as exc:
            raise PipelineRunStoreError(
                f"ready month-close result cannot produce a safe portfolio fingerprint: {exc}"
            ) from exc
    record = {
        "pipeline_id": pipeline_id,
        "run_id": run_id if isinstance(run_id, str) else None,
        "idempotency_key": idempotency_key,
        "request_fingerprint": request_fingerprint,
        "result_fingerprint": _hash(result),
        "runtime_fingerprint": runtime_fingerprint,
        "box_name": runtime_snapshot.get("name") or product.get("name"),
        "entity_id": entity_id if isinstance(entity_id, str) else None,
        "period": period,
        "executed_at": result_pipeline.get("executed_at"),
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "actor": _clean_actor(actor),
        "status": "ready" if ready else "blocked",
        "ready": ready,
        "blocked_at": result.get("blocked_at"),
        "retryable": bool(result.get("retryable")),
        "failure_code": (
            result.get("failure_code")
            if isinstance(result.get("failure_code"), str)
            and FAILURE_CODE_PATTERN.fullmatch(result["failure_code"])
            else None
        ),
        "network_access_performed": bool(result.get("network_access_performed")),
        "external_actions_performed": False,
        "connector_batches": _connector_summaries(result),
        "service_stages": _service_summaries(result),
        "lineage": _safe_lineage(result),
        "cfo_metric_operand_assembly": _safe_metric_assembly(result),
        "required_review_gates": list(required_review_gates),
        "review_status": "pending_review" if required_review_gates else "not_required",
        "review_complete": not required_review_gates,
        "current_reviews": {},
        "review_history": [],
        "release_candidate": bool(ready and not required_review_gates),
        "release_candidate_is_external_authorization": False,
        "candidate_only": bool((result.get("founder_briefing") or {}).get("candidate_only")),
        "posting_performed": False,
        "portfolio_source_fingerprint": portfolio_source_fingerprint,
        "portfolio_source_artifact_persisted": False,
        "full_request_persisted": False,
        "full_result_persisted": False,
        "secret_values_persisted": False,
        "trigger_kind": "manual",
        "schedule_job_id": None,
        "schedule_occurrence_id": None,
        "schedule_claim_id": None,
        "scheduled_for": None,
    }
    if execution_context is not None:
        if not isinstance(execution_context, dict) or set(execution_context) != {
            "trigger_kind", "job_id", "occurrence_id", "claim_id", "scheduled_for",
        }:
            raise PipelineRunStoreError("schedule execution_context contains unsupported fields")
        if execution_context.get("trigger_kind") != "schedule":
            raise PipelineRunStoreError("execution_context trigger_kind must be schedule")
        claim_id = str(execution_context.get("claim_id") or "")
        if not CLAIM_ID_PATTERN.fullmatch(claim_id):
            raise PipelineRunStoreError("schedule execution_context claim_id is invalid")
        job_id = _schedule_key(execution_context.get("job_id"), "schedule job_id")
        occurrence_id = _schedule_key(
            execution_context.get("occurrence_id"), "schedule occurrence_id",
        )
        scheduled_for = _aware_datetime(
            execution_context.get("scheduled_for"), "schedule scheduled_for",
        ).isoformat()
        record.update({
            "trigger_kind": "schedule",
            "schedule_job_id": job_id,
            "schedule_occurrence_id": occurrence_id,
            "schedule_claim_id": claim_id,
            "scheduled_for": scheduled_for,
            "idempotency_key": _hash({
                "runtime_fingerprint": runtime_fingerprint,
                "schedule_job_id": job_id,
                "schedule_occurrence_id": occurrence_id,
            }),
        })
    return record


class PipelineRunStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.events_file = self.root / "pipeline_runs.jsonl"
        self.lock_file = self.root / ".pipeline_runs.lock"
        self._lock = threading.RLock()

    def _locked(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        handle = self.lock_file.open("a+b")
        os.chmod(self.lock_file, 0o600)
        if fcntl is None:
            handle.close()
            raise PipelineRunStoreError("cross-process ledger locking is unavailable")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        return handle

    @staticmethod
    def _verified_events_from_file(events_file: Path) -> list[dict[str, Any]]:
        if not events_file.exists():
            return []
        size = events_file.stat().st_size
        if size > MAX_LEDGER_BYTES:
            raise PipelineRunStoreError("pipeline run ledger exceeds the supported size")
        events: list[dict[str, Any]] = []
        previous_hash = "GENESIS"
        with events_file.open("rb") as handle:
            for line_number, raw in enumerate(handle, 1):
                if line_number > MAX_EVENTS:
                    raise PipelineRunStoreError("pipeline run ledger contains too many events")
                if len(raw) > MAX_EVENT_BYTES:
                    raise PipelineRunStoreError(f"pipeline run ledger event {line_number} is too large")
                try:
                    event = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise PipelineRunStoreError(
                        f"pipeline run ledger is corrupt at line {line_number}"
                    ) from exc
                if not isinstance(event, dict):
                    raise PipelineRunStoreError(f"pipeline run ledger event {line_number} is not an object")
                supplied_hash = event.get("event_hash")
                body = {key: value for key, value in event.items() if key != "event_hash"}
                if event.get("sequence") != line_number:
                    raise PipelineRunStoreError(f"pipeline run ledger sequence mismatch at line {line_number}")
                if event.get("previous_hash") != previous_hash:
                    raise PipelineRunStoreError(f"pipeline run ledger chain mismatch at line {line_number}")
                if supplied_hash != _hash(body):
                    raise PipelineRunStoreError(f"pipeline run ledger hash mismatch at line {line_number}")
                previous_hash = str(supplied_hash)
                events.append(event)
        return events

    def _events_unlocked(self) -> list[dict[str, Any]]:
        return self._verified_events_from_file(self.events_file)

    @staticmethod
    def _write_private_file(path: Path, body: bytes) -> None:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb", closefd=False) as handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            os.close(descriptor)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _project_records(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        records: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        for event in events:
            event_type = event.get("event_type")
            if event_type == "PIPELINE_RUN_RECORDED":
                raw_record = event.get("record")
                if not isinstance(raw_record, dict):
                    raise PipelineRunStoreError("pipeline run event has no record object")
                record = dict(raw_record)
                attempt_id = record.get("attempt_id")
                if not ATTEMPT_ID_PATTERN.fullmatch(str(attempt_id or "")):
                    raise PipelineRunStoreError("pipeline run event has an invalid attempt_id")
                if attempt_id in records:
                    raise PipelineRunStoreError("pipeline run ledger contains a duplicate attempt_id")
                records[attempt_id] = record
                order.append(attempt_id)
            elif event_type == "PIPELINE_RUN_REVIEWED":
                review = dict(event.get("review") or {})
                attempt_id = review.get("attempt_id")
                if attempt_id not in records:
                    raise PipelineRunStoreError("pipeline review references an unknown attempt")
                record = records[attempt_id]
                gate = review.get("gate")
                if gate not in set(record.get("required_review_gates") or []):
                    raise PipelineRunStoreError("pipeline review references an unknown gate")
                if review.get("decision") not in REVIEW_DECISIONS:
                    raise PipelineRunStoreError("pipeline review contains an invalid decision")
                history = [*record.get("review_history", []), review]
                current = {**record.get("current_reviews", {}), gate: review}
                required = set(record.get("required_review_gates") or [])
                approved = {
                    gate for gate, decision in current.items() if decision.get("decision") == "approved"
                }
                review_complete = required <= approved
                record.update({
                    "review_history": history,
                    "current_reviews": current,
                    "review_complete": review_complete,
                    "review_status": (
                        "approved" if review_complete
                        else "rejected" if any(
                            decision.get("decision") == "rejected" for decision in current.values()
                        )
                        else "needs_more_evidence" if any(
                            decision.get("decision") == "needs_more_evidence" for decision in current.values()
                        )
                        else "pending_review"
                    ),
                    "release_candidate": bool(record.get("ready") and review_complete),
                })
            elif event_type == "PIPELINE_SCHEDULE_CLAIMED":
                # Claims are control events, not financial run attempts. Their schema is
                # verified separately and they intentionally do not enter the run list.
                continue
            else:
                raise PipelineRunStoreError(f"unknown pipeline run event type: {event_type}")
        return [records[attempt_id] for attempt_id in order]

    def _append_event_unlocked(self, events: list[dict[str, Any]], event: dict[str, Any]) -> None:
        event.update({
            "schema_version": 1,
            "sequence": len(events) + 1,
            "previous_hash": events[-1]["event_hash"] if events else "GENESIS",
        })
        event["event_hash"] = _hash(event)
        encoded = (_canonical(event) + "\n").encode("utf-8")
        if len(encoded) > MAX_EVENT_BYTES:
            raise PipelineRunStoreError("pipeline run control event is too large")
        current_size = self.events_file.stat().st_size if self.events_file.exists() else 0
        if current_size + len(encoded) > MAX_LEDGER_BYTES:
            raise PipelineRunStoreError("pipeline run ledger has reached its size limit")
        with self.events_file.open("ab") as handle:
            os.chmod(self.events_file, 0o600)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())

    def record(
        self,
        runtime_snapshot: dict[str, Any],
        request: dict[str, Any],
        result: dict[str, Any],
        *,
        actor: str,
        execution_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        record = build_pipeline_run_record(
            runtime_snapshot, request, result, actor=actor,
            execution_context=execution_context,
        )
        with self._lock:
            lock_handle = self._locked()
            try:
                events = self._events_unlocked()
                prior_records = self._project_records(events)
                if record["trigger_kind"] == "schedule":
                    claim = next((
                        item for item in _schedule_claims(events)
                        if item["claim_id"] == record["schedule_claim_id"]
                    ), None)
                    if claim is None:
                        raise PipelineRunStoreError("scheduled run requires a recorded schedule claim")
                    if any(
                        claim.get(key) != record.get(record_key)
                        for key, record_key in (
                            ("runtime_fingerprint", "runtime_fingerprint"),
                            ("job_id", "schedule_job_id"),
                            ("occurrence_id", "schedule_occurrence_id"),
                        )
                    ) or _aware_datetime(
                        claim.get("scheduled_for"), "schedule claim scheduled_for",
                    ) != _aware_datetime(record.get("scheduled_for"), "scheduled_for"):
                        raise PipelineRunStoreError("scheduled run does not match its recorded claim")
                    if any(
                        prior.get("schedule_claim_id") == record["schedule_claim_id"]
                        for prior in prior_records
                    ):
                        raise PipelineRunStoreError("schedule claim has already been consumed")
                duplicate = next((
                    prior["attempt_id"] for prior in reversed(prior_records)
                    if prior.get("runtime_fingerprint") == record["runtime_fingerprint"]
                    and prior.get("idempotency_key") == record["idempotency_key"]
                ), None)
                attempt_id = uuid.uuid4().hex[:24]
                record.update({
                    "attempt_id": attempt_id,
                    "attempt_number_for_idempotency_key": 1 + sum(
                        prior.get("runtime_fingerprint") == record["runtime_fingerprint"]
                        and prior.get("idempotency_key") == record["idempotency_key"]
                        for prior in prior_records
                    ),
                    "duplicate_of_attempt_id": duplicate,
                })
                self._append_event_unlocked(events, {
                    "event_type": "PIPELINE_RUN_RECORDED", "record": record,
                })
                return dict(record)
            finally:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
                lock_handle.close()

    def reserve_schedule_occurrence(
        self,
        *,
        runtime_fingerprint: str,
        job_id: str,
        occurrence_id: str,
        scheduled_for: str,
        actor: str,
        max_attempts: int,
        retry_delay_minutes: int,
        lease_seconds: int = 900,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Atomically lease one scheduled occurrence before any Connector is dispatched."""
        if not isinstance(runtime_fingerprint, str) or not runtime_fingerprint:
            raise PipelineRunStoreError("runtime_fingerprint is required")
        job_id = _schedule_key(job_id, "schedule job_id")
        occurrence_id = _schedule_key(occurrence_id, "schedule occurrence_id")
        scheduled = _aware_datetime(scheduled_for, "schedule scheduled_for")
        if not isinstance(max_attempts, int) or isinstance(max_attempts, bool) or not 1 <= max_attempts <= 5:
            raise PipelineRunStoreError("max_attempts must be an integer from 1 to 5")
        if (
            not isinstance(retry_delay_minutes, int)
            or isinstance(retry_delay_minutes, bool)
            or not 1 <= retry_delay_minutes <= 1440
        ):
            raise PipelineRunStoreError("retry_delay_minutes must be an integer from 1 to 1440")
        if not isinstance(lease_seconds, int) or isinstance(lease_seconds, bool) or not 60 <= lease_seconds <= 3600:
            raise PipelineRunStoreError("lease_seconds must be an integer from 60 to 3600")
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            raise PipelineRunStoreError("schedule claim now must include a timezone offset")
        current = current.astimezone(timezone.utc)
        with self._lock:
            lock_handle = self._locked()
            try:
                events = self._events_unlocked()
                records = [
                    item for item in self._project_records(events)
                    if item.get("runtime_fingerprint") == runtime_fingerprint
                    and item.get("schedule_job_id") == job_id
                    and item.get("schedule_occurrence_id") == occurrence_id
                ]
                if records:
                    latest = records[-1]
                    if latest.get("ready"):
                        raise PipelineRunStoreError("schedule occurrence is already complete")
                    if not latest.get("retryable"):
                        raise PipelineRunStoreError("schedule occurrence failed with a non-retryable result")
                    if len(records) >= max_attempts:
                        raise PipelineRunStoreError("schedule occurrence exhausted its retry attempts")
                    recorded_at = _aware_datetime(latest.get("recorded_at"), "recorded_at")
                    if current < recorded_at + timedelta(minutes=retry_delay_minutes):
                        raise PipelineRunStoreError("schedule occurrence is waiting for its retry delay")
                consumed_claim_ids = {
                    item.get("schedule_claim_id") for item in self._project_records(events)
                    if item.get("schedule_claim_id")
                }
                active_claim = next((
                    claim for claim in reversed(_schedule_claims(events))
                    if claim.get("runtime_fingerprint") == runtime_fingerprint
                    and claim.get("job_id") == job_id
                    and claim.get("occurrence_id") == occurrence_id
                    and claim.get("claim_id") not in consumed_claim_ids
                    and _aware_datetime(
                        claim.get("lease_expires_at"), "schedule claim lease_expires_at",
                    ) > current
                ), None)
                if active_claim is not None:
                    raise PipelineRunStoreError("schedule occurrence already has an active execution lease")
                claim = {
                    "claim_id": uuid.uuid4().hex[:24],
                    "runtime_fingerprint": runtime_fingerprint,
                    "job_id": job_id,
                    "occurrence_id": occurrence_id,
                    "scheduled_for": scheduled.isoformat(),
                    "claimed_at": current.isoformat(),
                    "lease_expires_at": (current + timedelta(seconds=lease_seconds)).isoformat(),
                    "actor": _clean_actor(actor),
                    "external_action_performed": False,
                }
                self._append_event_unlocked(events, {
                    "event_type": "PIPELINE_SCHEDULE_CLAIMED", "claim": claim,
                })
                return dict(claim)
            finally:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
                lock_handle.close()

    def schedule_occurrence_status(
        self,
        *,
        runtime_fingerprint: str,
        job_id: str,
        occurrence_id: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Read retry and lease state for one occurrence without exposing source data."""
        job_id = _schedule_key(job_id, "schedule job_id")
        occurrence_id = _schedule_key(occurrence_id, "schedule occurrence_id")
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            raise PipelineRunStoreError("schedule status now must include a timezone offset")
        current = current.astimezone(timezone.utc)
        with self._lock:
            lock_handle = self._locked()
            try:
                events = self._events_unlocked()
                records = [
                    dict(item) for item in self._project_records(events)
                    if item.get("runtime_fingerprint") == runtime_fingerprint
                    and item.get("schedule_job_id") == job_id
                    and item.get("schedule_occurrence_id") == occurrence_id
                ]
                consumed = {item.get("schedule_claim_id") for item in records}
                claims = [
                    dict(claim) for claim in _schedule_claims(events)
                    if claim.get("runtime_fingerprint") == runtime_fingerprint
                    and claim.get("job_id") == job_id
                    and claim.get("occurrence_id") == occurrence_id
                ]
                active = next((
                    claim for claim in reversed(claims)
                    if claim.get("claim_id") not in consumed
                    and _aware_datetime(claim.get("lease_expires_at"), "lease_expires_at") > current
                ), None)
                latest = records[-1] if records else None
                return {
                    "job_id": job_id,
                    "occurrence_id": occurrence_id,
                    "attempt_count": len(records),
                    "latest_attempt_id": latest.get("attempt_id") if latest else None,
                    "latest_ready": bool(latest and latest.get("ready")),
                    "latest_retryable": bool(latest and latest.get("retryable")),
                    "latest_recorded_at": latest.get("recorded_at") if latest else None,
                    "active_claim": ({
                        "claim_id": active["claim_id"],
                        "claimed_at": active["claimed_at"],
                        "lease_expires_at": active["lease_expires_at"],
                        "actor": active["actor"],
                    } if active else None),
                    "claim_count": len(claims),
                    "external_action_performed": False,
                }
            finally:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
                lock_handle.close()

    def list(
        self,
        *,
        runtime_fingerprint: str,
        pipeline_id: str | None = None,
        entity_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 500:
            raise PipelineRunStoreError("limit must be an integer from 1 to 500")
        with self._lock:
            lock_handle = self._locked()
            try:
                records = [
                    record for record in self._project_records(self._events_unlocked())
                    if record.get("runtime_fingerprint") == runtime_fingerprint
                    and (pipeline_id is None or record.get("pipeline_id") == pipeline_id)
                    and (entity_id is None or record.get("entity_id") == entity_id)
                ]
            finally:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
                lock_handle.close()
        return [dict(record) for record in reversed(records[-limit:])]

    def get(self, attempt_id: str, *, runtime_fingerprint: str) -> dict[str, Any] | None:
        if not ATTEMPT_ID_PATTERN.fullmatch(str(attempt_id or "")):
            raise PipelineRunStoreError("attempt_id is invalid")
        with self._lock:
            lock_handle = self._locked()
            try:
                return next((
                    dict(record) for record in self._project_records(self._events_unlocked())
                    if record.get("runtime_fingerprint") == runtime_fingerprint
                    and record.get("attempt_id") == attempt_id
                ), None)
            finally:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
                lock_handle.close()

    def verify_month_close_portfolio_sources(
        self,
        request: dict[str, Any],
        *,
        runtime_fingerprint: str,
    ) -> dict[str, Any]:
        """Bind supplied aggregate summaries to reviewed records without persisting raw results."""
        payload = request.get("payload") if isinstance(request, dict) else None
        controls = payload.get("entity_close_controls") if isinstance(payload, dict) else None
        if request.get("pipeline_id") != "finance.multi_entity_month_close_portfolio":
            raise PipelineRunStoreError("request is not a multi-entity month-close portfolio")
        if not isinstance(controls, list) or not controls:
            raise PipelineRunStoreError("portfolio request requires entity_close_controls")
        attempt_ids = [str(item.get("source_attempt_id") or "") for item in controls]
        if any(not ATTEMPT_ID_PATTERN.fullmatch(item) for item in attempt_ids):
            raise PipelineRunStoreError(
                "every portfolio source requires a valid source_attempt_id"
            )
        if len(attempt_ids) != len(set(attempt_ids)):
            raise PipelineRunStoreError("portfolio source_attempt_id values must be unique")
        with self._lock:
            lock_handle = self._locked()
            try:
                events = self._events_unlocked()
                records = {
                    record.get("attempt_id"): record
                    for record in self._project_records(events)
                    if record.get("runtime_fingerprint") == runtime_fingerprint
                    and record.get("attempt_id") in set(attempt_ids)
                }
                chain_head = events[-1]["event_hash"] if events else "GENESIS"
            finally:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
                lock_handle.close()
        verified = []
        for candidate, attempt_id in zip(controls, attempt_ids):
            record = records.get(attempt_id)
            if record is None:
                raise PipelineRunStoreError(
                    f"portfolio source attempt was not found for this Box: {attempt_id}"
                )
            evidence_ref = f"pipeline-ledger://attempts/{attempt_id}"
            if evidence_ref not in (candidate.get("source_evidence") or []):
                raise PipelineRunStoreError(
                    f"portfolio source evidence must include {evidence_ref}"
                )
            try:
                verify_portfolio_source_record(record, candidate)
            except MonthClosePortfolioEvidenceError as exc:
                raise PipelineRunStoreError(
                    f"portfolio source attempt {attempt_id} failed verification: {exc}"
                ) from exc
            verified.append({
                "attempt_id": attempt_id,
                "entity_id": record.get("entity_id"),
                "run_id": record.get("run_id"),
                "result_fingerprint": record.get("result_fingerprint"),
                "portfolio_source_fingerprint": record.get("portfolio_source_fingerprint"),
                "review_complete": True,
            })
        return {
            "verified": True,
            "integrity": "sha256_hash_chain",
            "chain_head": chain_head,
            "source_count": len(verified),
            "sources": sorted(verified, key=lambda item: item["entity_id"]),
            "raw_pipeline_results_persisted": False,
            "external_action_performed": False,
        }

    def review_queue(
        self,
        *,
        runtime_fingerprint: str,
        pipeline_id: str | None = None,
        entity_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Project unresolved review gates without exposing raw Pipeline evidence."""
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 500:
            raise PipelineRunStoreError("limit must be an integer from 1 to 500")
        with self._lock:
            lock_handle = self._locked()
            try:
                records = [
                    record for record in reversed(self._project_records(self._events_unlocked()))
                    if record.get("runtime_fingerprint") == runtime_fingerprint
                    and (pipeline_id is None or record.get("pipeline_id") == pipeline_id)
                    and (entity_id is None or record.get("entity_id") == entity_id)
                ]
                tasks: list[dict[str, Any]] = []
                for record in records:
                    current = record.get("current_reviews") or {}
                    for gate in record.get("required_review_gates") or []:
                        latest = current.get(gate) or {}
                        if latest.get("decision") == "approved":
                            continue
                        tasks.append({
                            "attempt_id": record["attempt_id"],
                            "pipeline_id": record["pipeline_id"],
                            "entity_id": record.get("entity_id"),
                            "run_status": record.get("status"),
                            "blocked_at": record.get("blocked_at"),
                            "gate": gate,
                            "current_decision": latest.get("decision") or "pending",
                            "current_reviewer": latest.get("actor"),
                            "last_reviewed_at": latest.get("reviewed_at"),
                            "executed_by": record.get("actor"),
                            "recorded_at": record.get("recorded_at"),
                            "external_action_performed": False,
                        })
                        if len(tasks) == limit:
                            return tasks
                return tasks
            finally:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
                lock_handle.close()

    def verify(self, *, runtime_fingerprint: str) -> dict[str, Any]:
        """Verify the complete ledger chain and report only Box-scoped activity counts."""
        with self._lock:
            lock_handle = self._locked()
            try:
                events = self._events_unlocked()
                claims = _schedule_claims(events)
                records = [
                    record for record in self._project_records(events)
                    if record.get("runtime_fingerprint") == runtime_fingerprint
                ]
                return {
                    "valid": True,
                    "integrity": "sha256_hash_chain",
                    "integrity_limit": "tamper_evident_not_immutable",
                    "chain_head": events[-1]["event_hash"] if events else "GENESIS",
                    "event_count": len(events),
                    "attempt_count_for_box": len(records),
                    "review_event_count_for_box": sum(
                        len(record.get("review_history") or []) for record in records
                    ),
                    "schedule_claim_count_for_box": sum(
                        claim.get("runtime_fingerprint") == runtime_fingerprint
                        for claim in claims
                    ),
                    "verified_at": datetime.now(timezone.utc).isoformat(),
                    "external_action_performed": False,
                }
            finally:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
                lock_handle.close()

    def backup(self, destination: str | Path, *, actor: str) -> dict[str, Any]:
        """Create a new, non-overwriting physical-ledger backup with a verification manifest."""
        destination = Path(destination).expanduser().resolve()
        if destination.exists():
            raise PipelineRunStoreError("backup destination already exists; backups never overwrite")
        if not destination.parent.is_dir():
            raise PipelineRunStoreError("backup destination parent must already exist")
        with self._lock:
            lock_handle = self._locked()
            try:
                events = self._events_unlocked()
                ledger_bytes = self.events_file.read_bytes() if self.events_file.exists() else b""
                ledger_sha256 = hashlib.sha256(ledger_bytes).hexdigest()
                manifest = {
                    "schema_version": 1,
                    "backup_id": uuid.uuid4().hex,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "created_by": _clean_actor(actor),
                    "ledger_file": "pipeline_runs.jsonl",
                    "ledger_sha256": ledger_sha256,
                    "ledger_bytes": len(ledger_bytes),
                    "event_count": len(events),
                    "chain_head": events[-1]["event_hash"] if events else "GENESIS",
                    "contains_all_boxes_in_physical_ledger": True,
                    "contains_control_metadata": True,
                    "raw_pipeline_request_or_result_automatically_included": False,
                    "restore_requires_empty_target": True,
                    "tamper_evident_not_immutable": True,
                    "external_action_performed": False,
                }
                destination.mkdir(mode=0o700)
                os.chmod(destination, 0o700)
                try:
                    self._write_private_file(destination / "pipeline_runs.jsonl", ledger_bytes)
                    self._write_private_file(
                        destination / BACKUP_MANIFEST_NAME,
                        (_canonical(manifest) + "\n").encode("utf-8"),
                    )
                    self._fsync_directory(destination)
                except Exception:
                    # Leave a partial, never-overwritten directory for operator inspection.
                    raise
                return {"valid": True, **manifest, "backup_path": str(destination)}
            finally:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
                lock_handle.close()

    @classmethod
    def verify_backup(cls, backup_directory: str | Path) -> dict[str, Any]:
        backup_directory = Path(backup_directory).expanduser().resolve()
        manifest_path = backup_directory / BACKUP_MANIFEST_NAME
        ledger_path = backup_directory / "pipeline_runs.jsonl"
        try:
            if manifest_path.stat().st_size > 1024 * 1024:
                raise PipelineRunStoreError("pipeline run backup manifest exceeds 1 MiB")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except PipelineRunStoreError:
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PipelineRunStoreError("pipeline run backup manifest is missing or invalid") from exc
        if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
            raise PipelineRunStoreError("unsupported pipeline run backup manifest")
        if manifest.get("ledger_file") != "pipeline_runs.jsonl":
            raise PipelineRunStoreError("pipeline run backup has an invalid ledger_file")
        try:
            if ledger_path.stat().st_size > MAX_LEDGER_BYTES:
                raise PipelineRunStoreError("pipeline run backup ledger exceeds the supported size")
            ledger_bytes = ledger_path.read_bytes()
        except PipelineRunStoreError:
            raise
        except OSError as exc:
            raise PipelineRunStoreError("pipeline run backup ledger is missing") from exc
        supplied_sha = manifest.get("ledger_sha256")
        if supplied_sha != hashlib.sha256(ledger_bytes).hexdigest():
            raise PipelineRunStoreError("pipeline run backup ledger fingerprint mismatch")
        if manifest.get("ledger_bytes") != len(ledger_bytes):
            raise PipelineRunStoreError("pipeline run backup ledger size mismatch")
        events = cls._verified_events_from_file(ledger_path)
        cls._project_records(events)
        _schedule_claims(events)
        if manifest.get("event_count") != len(events):
            raise PipelineRunStoreError("pipeline run backup event count mismatch")
        chain_head = events[-1]["event_hash"] if events else "GENESIS"
        if manifest.get("chain_head") != chain_head:
            raise PipelineRunStoreError("pipeline run backup chain head mismatch")
        return {
            "valid": True,
            "backup_id": manifest.get("backup_id"),
            "created_at": manifest.get("created_at"),
            "created_by": manifest.get("created_by"),
            "ledger_sha256": supplied_sha,
            "ledger_bytes": len(ledger_bytes),
            "event_count": len(events),
            "chain_head": chain_head,
            "tamper_evident_not_immutable": True,
            "external_action_performed": False,
            "backup_path": str(backup_directory),
        }

    def restore_from_backup(
        self, backup_directory: str | Path, *, actor: str,
    ) -> dict[str, Any]:
        """Restore a verified ledger only when this target has never had an events file."""
        verified = self.verify_backup(backup_directory)
        backup_directory = Path(backup_directory).expanduser().resolve()
        actor = _clean_actor(actor)
        with self._lock:
            lock_handle = self._locked()
            try:
                if self.events_file.exists():
                    raise PipelineRunStoreError(
                        "restore target already has a ledger; restore never overwrites or merges"
                    )
                receipt_path = self.root / "pipeline_runs_restore_receipt.json"
                if receipt_path.exists():
                    raise PipelineRunStoreError("restore target already has a restore receipt")
                ledger_bytes = (backup_directory / "pipeline_runs.jsonl").read_bytes()
                if hashlib.sha256(ledger_bytes).hexdigest() != verified["ledger_sha256"]:
                    raise PipelineRunStoreError("pipeline run backup changed during restore")
                self._write_private_file(self.events_file, ledger_bytes)
                receipt = {
                    "schema_version": 1,
                    "backup_id": verified["backup_id"],
                    "restored_at": datetime.now(timezone.utc).isoformat(),
                    "restored_by": actor,
                    "ledger_sha256": verified["ledger_sha256"],
                    "event_count": verified["event_count"],
                    "chain_head": verified["chain_head"],
                    "target_was_empty": True,
                    "external_action_performed": False,
                }
                self._write_private_file(
                    receipt_path,
                    (_canonical(receipt) + "\n").encode("utf-8"),
                )
                self._fsync_directory(self.root)
                # Re-read the restored chain before reporting success.
                self._events_unlocked()
                return {"restored": True, **receipt, "target_path": str(self.root)}
            finally:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
                lock_handle.close()

    def review(
        self,
        attempt_id: str,
        *,
        runtime_fingerprint: str,
        gate: str,
        decision: str,
        actor: str,
        rationale: str,
        evidence_references: list[str] | None = None,
    ) -> dict[str, Any]:
        if not ATTEMPT_ID_PATTERN.fullmatch(str(attempt_id or "")):
            raise PipelineRunStoreError("attempt_id is invalid")
        if decision not in REVIEW_DECISIONS:
            raise PipelineRunStoreError("decision must be approved, rejected or needs_more_evidence")
        if not isinstance(gate, str) or not GATE_PATTERN.fullmatch(gate):
            raise PipelineRunStoreError("gate is invalid")
        rationale = str(rationale or "").strip()
        if not rationale or len(rationale) > 1000 or any(ord(char) < 32 and char not in "\n\t" for char in rationale):
            raise PipelineRunStoreError("rationale must be 1-1000 printable characters")
        evidence = evidence_references or []
        if not isinstance(evidence, list) or len(evidence) > 20 or any(
            not isinstance(item, str) or not item.strip() or len(item) > 200 for item in evidence
        ):
            raise PipelineRunStoreError("evidence_references must contain up to 20 bounded strings")
        with self._lock:
            lock_handle = self._locked()
            try:
                events = self._events_unlocked()
                record = next((
                    item for item in self._project_records(events)
                    if item.get("runtime_fingerprint") == runtime_fingerprint
                    and item.get("attempt_id") == attempt_id
                ), None)
                if record is None:
                    raise PipelineRunStoreError("pipeline run attempt was not found for this Box")
                if gate not in set(record.get("required_review_gates") or []):
                    raise PipelineRunStoreError("review gate is not required by this Pipeline attempt")
                review = {
                    "review_id": uuid.uuid4().hex[:24],
                    "attempt_id": attempt_id,
                    "gate": gate,
                    "decision": decision,
                    "actor": _clean_actor(actor),
                    "rationale": rationale,
                    "evidence_references": [item.strip() for item in evidence],
                    "reviewed_at": datetime.now(timezone.utc).isoformat(),
                    "financial_state_changed": False,
                    "external_action_performed": False,
                }
                self._append_event_unlocked(events, {
                    "event_type": "PIPELINE_RUN_REVIEWED", "review": review,
                })
                projected = self._project_records([*events, {
                    "event_type": "PIPELINE_RUN_REVIEWED", "review": review,
                }])
                return next(item for item in projected if item["attempt_id"] == attempt_id)
            finally:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
                lock_handle.close()
