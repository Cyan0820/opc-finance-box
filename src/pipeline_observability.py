from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .box_runtime import BoxRuntime
from .pipeline_run_store import PipelineRunStore
from .pipeline_scheduler import PipelineScheduleError, inspect_pipeline_schedule


SCHEDULE_ALERT_STATUS = {
    "blocked_configuration": ("warning", "schedule_configuration"),
    "blocked_non_retryable": ("critical", "non_retryable_failure"),
    "retry_exhausted": ("critical", "retry_exhausted"),
    "missed_window": ("warning", "missed_window"),
}


def _timestamp(value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError as exc:
        raise PipelineScheduleError("observability source contains an invalid timestamp") from exc
    if parsed.tzinfo is None:
        raise PipelineScheduleError("observability source timestamp must include a timezone offset")
    return parsed.astimezone(timezone.utc)


def build_pipeline_observability(
    runtime: BoxRuntime,
    store: PipelineRunStore,
    *,
    schedule_path: str | Path | None = None,
    now: datetime | None = None,
    stale_review_hours: int = 24,
) -> dict[str, Any]:
    """Build secret-free, read-only operational metrics and actionable alerts."""
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise PipelineScheduleError("now must include a timezone offset")
    current = current.astimezone(timezone.utc)
    if not isinstance(stale_review_hours, int) or isinstance(stale_review_hours, bool) or not 1 <= stale_review_hours <= 720:
        raise PipelineScheduleError("stale_review_hours must be an integer from 1 to 720")

    runtime_fingerprint = runtime.snapshot()["fingerprint"]
    integrity = store.verify(runtime_fingerprint=runtime_fingerprint)
    records = store.list(runtime_fingerprint=runtime_fingerprint, limit=500)
    review_tasks = store.review_queue(runtime_fingerprint=runtime_fingerprint, limit=500)
    cutoff_24h = current - timedelta(hours=24)
    stale_cutoff = current - timedelta(hours=stale_review_hours)
    recent = [
        item for item in records
        if cutoff_24h <= _timestamp(item["recorded_at"]) <= current
    ]
    stale_reviews = [item for item in review_tasks if _timestamp(item["recorded_at"]) < stale_cutoff]

    schedule = None
    jobs: list[dict[str, Any]] = []
    if schedule_path is not None:
        schedule = inspect_pipeline_schedule(schedule_path, runtime, store, now=current)
        jobs = schedule["jobs"]
    status_counts = Counter(item["status"] for item in jobs)
    alerts: list[dict[str, Any]] = []
    for job in jobs:
        alert_policy = SCHEDULE_ALERT_STATUS.get(job["status"])
        if alert_policy is None:
            continue
        severity, kind = alert_policy
        alerts.append({
            "alert_id": f"schedule:{job['job_id']}:{kind}",
            "severity": severity,
            "kind": kind,
            "job_id": job["job_id"],
            "owner": job["alert_owner"],
            "status": job["status"],
            "message": f"scheduled Pipeline job {job['job_id']} requires human attention",
            "blockers": job["blockers"],
            "requires_human_action": True,
            "notification_sent": False,
        })
    if stale_reviews:
        alerts.append({
            "alert_id": "review:stale_queue",
            "severity": "warning",
            "kind": "stale_review_queue",
            "job_id": None,
            "owner": "REQUIRED_REVIEW_QUEUE_OWNER",
            "status": "stale",
            "message": f"{len(stale_reviews)} review gates have waited more than {stale_review_hours} hours",
            "blockers": [],
            "requires_human_action": True,
            "notification_sent": False,
        })

    recent_status = Counter("ready" if item.get("ready") else "blocked" for item in recent)
    severity_counts = Counter(item["severity"] for item in alerts)
    kind_counts = Counter(item["kind"] for item in alerts)
    return {
        "schema_version": 1,
        "evaluated_at": current.isoformat(),
        "runtime_fingerprint": runtime_fingerprint,
        "schedule_configured": schedule is not None,
        "schedule": {
            "timezone": schedule["timezone"] if schedule else None,
            "counts_by_status": dict(sorted(status_counts.items())),
            "job_count": len(jobs),
            "runnable_now": sum(item["runnable_now"] for item in jobs),
        },
        "runs_24h": {
            "total": len(recent),
            "ready": recent_status["ready"],
            "blocked": recent_status["blocked"],
            "retryable_blocked": sum(not item.get("ready") and item.get("retryable") for item in recent),
            "source_record_limit": 500,
            "counts_may_be_truncated": len(records) == 500,
        },
        "review_queue": {
            "pending_gate_count": len(review_tasks),
            "stale_gate_count": len(stale_reviews),
            "stale_after_hours": stale_review_hours,
            "source_task_limit": 500,
            "counts_may_be_truncated": len(review_tasks) == 500,
        },
        "ledger": {
            "integrity_valid": integrity["valid"],
            "event_count": integrity["event_count"],
            "attempt_count_for_box": integrity["attempt_count_for_box"],
            "schedule_claim_count_for_box": integrity["schedule_claim_count_for_box"],
        },
        "alerts": alerts,
        "alert_counts": {
            "total": len(alerts),
            "critical": severity_counts["critical"],
            "warning": severity_counts["warning"],
            "by_kind": dict(sorted(kind_counts.items())),
        },
        "raw_financial_data_included": False,
        "secret_values_included": False,
        "notification_sent": False,
        "external_actions_performed": False,
    }


def render_pipeline_prometheus(observability: dict[str, Any]) -> str:
    """Render a bounded, low-cardinality Prometheus exposition without business labels."""
    lines = [
        "# HELP opc_finance_pipeline_ledger_integrity Whether the Pipeline ledger hash chain is valid.",
        "# TYPE opc_finance_pipeline_ledger_integrity gauge",
        f"opc_finance_pipeline_ledger_integrity {1 if observability['ledger']['integrity_valid'] else 0}",
        "# HELP opc_finance_pipeline_schedule_jobs Scheduled jobs by operational status.",
        "# TYPE opc_finance_pipeline_schedule_jobs gauge",
    ]
    statuses = (
        "disabled", "due", "completed", "leased", "retry_due", "retry_wait",
        "retry_exhausted", "blocked_non_retryable", "blocked_configuration", "missed_window",
    )
    counts = observability["schedule"]["counts_by_status"]
    lines.extend(
        f'opc_finance_pipeline_schedule_jobs{{status="{status}"}} {int(counts.get(status, 0))}'
        for status in statuses
    )
    lines.extend([
        "# HELP opc_finance_pipeline_runs_24h Pipeline attempts recorded in the last 24 hours.",
        "# TYPE opc_finance_pipeline_runs_24h gauge",
        f'opc_finance_pipeline_runs_24h{{status="ready"}} {observability["runs_24h"]["ready"]}',
        f'opc_finance_pipeline_runs_24h{{status="blocked"}} {observability["runs_24h"]["blocked"]}',
        "# HELP opc_finance_pipeline_review_gates Current review gate queue by age state.",
        "# TYPE opc_finance_pipeline_review_gates gauge",
        f'opc_finance_pipeline_review_gates{{state="pending"}} {observability["review_queue"]["pending_gate_count"]}',
        f'opc_finance_pipeline_review_gates{{state="stale"}} {observability["review_queue"]["stale_gate_count"]}',
        "# HELP opc_finance_pipeline_alerts Current derived alerts by severity.",
        "# TYPE opc_finance_pipeline_alerts gauge",
        f'opc_finance_pipeline_alerts{{severity="critical"}} {observability["alert_counts"]["critical"]}',
        f'opc_finance_pipeline_alerts{{severity="warning"}} {observability["alert_counts"]["warning"]}',
    ])
    return "\n".join(lines) + "\n"
