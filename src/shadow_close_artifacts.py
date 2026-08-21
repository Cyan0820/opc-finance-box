from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from typing import Any, Iterable

from .box_runtime import BoxRuntime
from .shadow_close import (
    compare_shadow_close,
    parse_shadow_close_workbook,
    review_shadow_close,
    validate_shadow_close_report,
)
from .workbook_templates import build_shadow_close_template


MAX_ARTIFACT_BYTES = 50 * 1024 * 1024
CLI_DECISIONS = {
    "passed": "验证通过",
    "accepted-differences": "接受差异",
    "needs-correction": "退回补数",
}


class ShadowCloseArtifactError(ValueError):
    """Raised when a local Shadow Close artifact cannot be safely produced."""


def _read_json(path: str | Path) -> Any:
    source = Path(path)
    if not source.is_file():
        raise ShadowCloseArtifactError(f"Shadow Close input does not exist: {source}")
    size = source.stat().st_size
    if size <= 0 or size > MAX_ARTIFACT_BYTES:
        raise ShadowCloseArtifactError("Shadow Close JSON input must be 1 byte to 50 MiB")
    return json.loads(source.read_text(encoding="utf-8"))


def _write_private_json(path: str | Path, payload: dict[str, Any]) -> Path:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
    except FileExistsError as exc:
        raise ShadowCloseArtifactError(
            "Shadow Close output already exists; refusing to overwrite"
        ) from exc
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    return destination


def write_shadow_close_template(
    runtime: BoxRuntime,
    output: str | Path,
) -> dict[str, Any]:
    """Publish a Box-scoped blank workbook without replacing an existing file."""
    destination = Path(output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise ShadowCloseArtifactError(
            "Shadow Close template output already exists; refusing to overwrite"
        )
    snapshot = runtime.snapshot()
    entity_ids = [item["id"] for item in snapshot["entities"]]
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{destination.name}.",
            suffix=".xlsx",
            dir=destination.parent,
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
        build_shadow_close_template(temporary_path, entity_ids=entity_ids)
        os.chmod(temporary_path, 0o600)
        try:
            os.link(temporary_path, destination)
        except FileExistsError as exc:
            raise ShadowCloseArtifactError(
                "Shadow Close template output already exists; refusing to overwrite"
            ) from exc
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return {
        "output": str(destination),
        "runtime_fingerprint": snapshot["fingerprint"],
        "entity_ids": entity_ids,
        "template_only": True,
        "contains_financial_values": False,
        "external_actions_performed": False,
    }


def compare_shadow_close_artifacts(
    runtime: BoxRuntime,
    baseline_workbook: str | Path,
    finance_json: str | Path,
    output: str | Path,
) -> dict[str, Any]:
    """Compare one scoped human baseline with one scoped deterministic finance result."""
    baseline = parse_shadow_close_workbook(baseline_workbook)
    runtime.require_entity(str(baseline.get("entity_id") or ""))
    finance = _read_json(finance_json)
    if not isinstance(finance, dict):
        raise ShadowCloseArtifactError("finance result must be a JSON object")
    if finance.get("entity_id") != baseline["entity_id"]:
        raise ShadowCloseArtifactError(
            "finance result entity_id does not match the Shadow Close baseline"
        )
    if finance.get("period") != baseline["period"]:
        raise ShadowCloseArtifactError(
            "finance result period does not match the Shadow Close baseline"
        )
    if not isinstance(finance.get("financial_statements"), dict):
        raise ShadowCloseArtifactError(
            "finance result must include financial_statements"
        )
    snapshot = runtime.snapshot()
    report = compare_shadow_close(
        baseline,
        finance,
        runtime_fingerprint=snapshot["fingerprint"],
    )
    validate_shadow_close_report(report)
    destination = _write_private_json(output, report)
    return {
        "output": str(destination),
        "runtime_fingerprint": snapshot["fingerprint"],
        "baseline_id": report["baseline_id"],
        "entity_id": report["entity_id"],
        "period": report["period"],
        "report_fingerprint": report["report_fingerprint"],
        "comparison_count": report["comparison_count"],
        "matched_count": report["matched_count"],
        "exception_count": report["exception_count"],
        "review_current": False,
        "raw_financial_values_written_to_output": True,
        "raw_financial_values_returned": False,
        "ledger_changed": False,
        "external_actions_performed": False,
    }


def review_shadow_close_artifact(
    runtime: BoxRuntime,
    report_json: str | Path,
    output: str | Path,
    *,
    decision: str,
    actor: str,
    rationale: str,
    evidence_references: Iterable[str] = (),
    resolutions_json: str | Path | None = None,
) -> dict[str, Any]:
    """Attach an exact-fingerprint human review and publish a new private report."""
    report = _read_json(report_json)
    if not isinstance(report, dict):
        raise ShadowCloseArtifactError("Shadow Close report must be a JSON object")
    validate_shadow_close_report(report)
    runtime.require_entity(str(report.get("entity_id") or ""))
    runtime_fingerprint = runtime.snapshot()["fingerprint"]
    if report.get("runtime_fingerprint") != runtime_fingerprint:
        raise ShadowCloseArtifactError(
            "Shadow Close report belongs to a different Box runtime fingerprint"
        )
    if decision not in CLI_DECISIONS:
        raise ShadowCloseArtifactError(
            "decision must be passed, accepted-differences or needs-correction"
        )
    resolutions: Any = []
    if resolutions_json is not None:
        resolutions = _read_json(resolutions_json)
        if isinstance(resolutions, dict):
            resolutions = resolutions.get("exception_resolutions")
        if not isinstance(resolutions, list):
            raise ShadowCloseArtifactError(
                "resolutions JSON must be a list or contain exception_resolutions"
            )
    review = review_shadow_close(
        report,
        CLI_DECISIONS[decision],
        actor,
        rationale,
        evidence_references,
        resolutions,
    )
    reviewed_report = dict(report)
    reviewed_report["review"] = review
    reviewed_report["review_current"] = True
    validate_shadow_close_report(reviewed_report)
    destination = _write_private_json(output, reviewed_report)
    return {
        "output": str(destination),
        "runtime_fingerprint": runtime_fingerprint,
        "baseline_id": report["baseline_id"],
        "entity_id": report["entity_id"],
        "period": report["period"],
        "report_fingerprint": report["report_fingerprint"],
        "review_id": review["id"],
        "decision": review["decision"],
        "review_actor": review["actor"],
        "review_current": True,
        "raw_financial_values_written_to_output": True,
        "raw_financial_values_returned": False,
        "ledger_changed": False,
        "external_actions_performed": False,
    }


def verify_shadow_close_artifact(
    runtime: BoxRuntime,
    report_json: str | Path,
) -> dict[str, Any]:
    """Verify a private report for CI without returning any comparison values."""
    report = _read_json(report_json)
    if not isinstance(report, dict):
        raise ShadowCloseArtifactError("Shadow Close report must be a JSON object")
    integrity = validate_shadow_close_report(report)
    runtime.require_entity(str(report.get("entity_id") or ""))
    runtime_fingerprint = runtime.snapshot()["fingerprint"]
    if report.get("runtime_fingerprint") != runtime_fingerprint:
        raise ShadowCloseArtifactError(
            "Shadow Close report belongs to a different Box runtime fingerprint"
        )
    review = report.get("review") if report.get("review_current") else None
    return {
        "valid": True,
        "runtime_fingerprint": runtime_fingerprint,
        "baseline_id": report["baseline_id"],
        "entity_id": report["entity_id"],
        "period": report["period"],
        "report_fingerprint": integrity["report_fingerprint"],
        "comparison_count": integrity["comparison_count"],
        "matched_count": integrity["matched_count"],
        "exception_count": integrity["exception_count"],
        "review_current": bool(review),
        "review_id": review.get("id") if review else None,
        "decision": review.get("decision") if review else None,
        "raw_financial_values_returned": False,
        "ledger_changed": False,
        "external_actions_performed": False,
    }
