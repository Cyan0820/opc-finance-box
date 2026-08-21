from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Any, Iterable

from .box_runtime import BoxRuntime
from .multi_entity_shadow_close import validate_multi_entity_shadow_close_manifest
from .pilot_shadow_run import (
    _read_private as _read_registration_private,
    verify_pilot_shadow_run_registration,
)
from .shadow_close import validate_shadow_close_report


MAX_RECEIPT_BYTES = 4 * 1024 * 1024
MAX_SOURCE_BYTES = 50 * 1024 * 1024
HEX64_PATTERN = re.compile(r"^[0-9a-f]{64}$")
ID24_PATTERN = re.compile(r"^[0-9a-f]{24}$")
ENTITY_REVIEW_ID_PATTERN = re.compile(r"^SHADOW-REVIEW-[0-9a-f]{16}$")
PORTFOLIO_REVIEW_ID_PATTERN = re.compile(
    r"^PORTFOLIO-SHADOW-REVIEW-[0-9a-f]{16}$"
)
ACTOR_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@:-]{1,127}$")
PERIOD_PATTERN = re.compile(r"^[0-9]{4}-(?:0[1-9]|1[0-2])$")
REFERENCE_PATTERN = re.compile(
    r"^(?:evidence|document|workpaper|registry|advisor|authority|audit)://"
    r"[^\s]{2,500}$"
)
ENTITY_DECISIONS = {"验证通过", "接受差异"}
REVIEW_DECISIONS = {"passed", "accepted-differences", "needs-correction"}
EXCEPTION_CLASSIFICATIONS = {
    "timing", "foreign_exchange", "accepted_scope", "system_defect",
}


class PilotShadowObservationError(ValueError):
    """Raised when first Shadow observation evidence is incomplete or unsafe."""


def _canonical(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise PilotShadowObservationError(
            "pilot Shadow observation evidence must be JSON-serializable"
        ) from exc


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _text(value: Any, field: str, *, minimum: int = 2, maximum: int = 1000) -> str:
    text = str(value or "").strip()
    if not minimum <= len(text) <= maximum or any(
        ord(char) < 32 and char not in "\n\t" for char in text
    ):
        raise PilotShadowObservationError(
            f"{field} must be {minimum}-{maximum} printable characters"
        )
    return text


def _actor(value: Any, field: str) -> str:
    actor = str(value or "").strip()
    if not ACTOR_PATTERN.fullmatch(actor):
        raise PilotShadowObservationError(
            f"{field} must be a 2-128 character stable actor identifier"
        )
    return actor


def _references(value: Iterable[str], field: str) -> list[str]:
    if isinstance(value, (str, bytes)):
        raise PilotShadowObservationError(f"{field} must be a list")
    references = list(value)
    if not references or len(references) > 20 or len(references) != len(set(references)):
        raise PilotShadowObservationError(f"{field} requires 1-20 unique references")
    if any(
        not isinstance(item, str) or not REFERENCE_PATTERN.fullmatch(item)
        for item in references
    ):
        raise PilotShadowObservationError(
            f"{field} must contain bounded opaque evidence references"
        )
    return references


def _read_private_json(
    path: str | Path, *, label: str, maximum_bytes: int,
) -> dict[str, Any]:
    source = Path(path)
    if source.is_symlink():
        raise PilotShadowObservationError(f"{label} must not be a symbolic link")
    if not source.is_file():
        raise PilotShadowObservationError(f"{label} does not exist")
    metadata = source.stat()
    if os.name != "nt" and stat.S_IMODE(metadata.st_mode) & 0o077:
        raise PilotShadowObservationError(
            f"{label} must not be accessible by group or other users"
        )
    if metadata.st_size <= 0 or metadata.st_size > maximum_bytes:
        raise PilotShadowObservationError(
            f"{label} must be 1 byte to {maximum_bytes} bytes"
        )
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PilotShadowObservationError(f"{label} must be valid JSON") from exc
    if not isinstance(value, dict):
        raise PilotShadowObservationError(f"{label} must be a JSON object")
    return value


def _write_private(path: str | Path, value: dict[str, Any]) -> Path:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise PilotShadowObservationError(
            "pilot Shadow observation output already exists; refusing to overwrite"
        ) from exc
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    return destination


def _strict_fields(value: Any, expected: set[str], label: str) -> None:
    if not isinstance(value, dict) or set(value) != expected:
        raise PilotShadowObservationError(
            f"{label} fields do not match the observation contract"
        )


def _system_defect_count(report: dict[str, Any]) -> int:
    review = report.get("review")
    decision = review.get("decision") if isinstance(review, dict) else None
    resolutions = review.get("exception_resolutions") if isinstance(review, dict) else None
    exception_rows = {
        (str(item.get("domain") or ""), str(item.get("key") or ""))
        for item in report.get("comparisons") or []
        if isinstance(item, dict) and item.get("status") != "一致"
    }
    if decision == "验证通过":
        if resolutions not in (None, []):
            raise PilotShadowObservationError(
                "clean entity Shadow review cannot attach exception resolutions"
            )
        return 0
    if decision != "接受差异" or not isinstance(resolutions, list):
        raise PilotShadowObservationError(
            "accepted entity Shadow differences require structured resolutions"
        )
    resolved: set[tuple[str, str]] = set()
    system_defects = 0
    for item in resolutions:
        _strict_fields(
            item,
            {"domain", "key", "classification", "rationale", "evidence_references"},
            "entity Shadow exception resolution",
        )
        identity = (str(item.get("domain") or ""), str(item.get("key") or ""))
        if identity not in exception_rows or identity in resolved:
            raise PilotShadowObservationError(
                "entity Shadow exception resolutions must cover unique current exceptions"
            )
        classification = str(item.get("classification") or "")
        if classification not in EXCEPTION_CLASSIFICATIONS:
            raise PilotShadowObservationError(
                "entity Shadow exception resolution classification is invalid"
            )
        _text(
            item.get("rationale"), "entity Shadow exception rationale",
            minimum=12, maximum=1000,
        )
        _references(
            item.get("evidence_references") or [],
            "entity Shadow exception evidence",
        )
        resolved.add(identity)
        system_defects += classification == "system_defect"
    if resolved != exception_rows:
        raise PilotShadowObservationError(
            "accepted entity Shadow differences require one resolution per exception"
        )
    return system_defects


def _receipt_fingerprint(receipt: dict[str, Any]) -> str:
    return _hash({
        key: receipt.get(key)
        for key in (
            "schema_version", "artifact_type", "runtime_fingerprint",
            "pilot_shadow_run_registration_id",
            "pilot_shadow_run_registration_content_sha256", "registration_actor",
            "period", "entity_ids",
            "entity_observations", "portfolio_observation",
            "observation_result_candidate", "status",
            "source_artifacts_input_only", "raw_financial_values_persisted",
            "statutory_books_modified", "posting_performed", "payment_performed",
            "period_close_performed", "external_filing_performed",
            "external_actions_performed", "guardrail",
        )
    })


def _review_fingerprint(review: dict[str, Any]) -> str:
    return _hash({
        key: review.get(key)
        for key in (
            "review_id", "receipt_fingerprint", "decision", "actor", "rationale",
            "evidence_references", "reviewed_at", "scope_note",
        )
    })


def _verified_registration(
    runtime: BoxRuntime,
    registration_json: str | Path,
    handoff_review: str | Path,
    pilot_readiness_review: str | Path,
    runs_root: str | Path,
    *,
    as_of: str | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        safe = verify_pilot_shadow_run_registration(
            runtime, registration_json, handoff_review, pilot_readiness_review,
            runs_root, as_of=as_of,
        )
        private = _read_registration_private(registration_json)
    except (ValueError, OSError) as exc:
        raise PilotShadowObservationError(str(exc)) from exc
    if private.get("registration_id") != safe["registration_id"]:
        raise PilotShadowObservationError(
            "pilot Shadow Run registration changed during observation assembly"
        )
    return safe, private


def _collect_entity_observations(
    runtime: BoxRuntime,
    entity_report_paths: Iterable[str | Path],
    registration: dict[str, Any],
    *,
    period: str,
) -> list[dict[str, Any]]:
    paths = list(entity_report_paths)
    expected_entities = sorted(runtime.entities.ids())
    if len(paths) != len(expected_entities):
        raise PilotShadowObservationError(
            "entity Shadow reports must cover every configured entity exactly once"
        )
    attempts = {
        str(item.get("entity_id") or ""): str(item.get("attempt_id") or "")
        for item in registration.get("entity_runs") or []
        if isinstance(item, dict)
    }
    if set(attempts) != set(expected_entities):
        raise PilotShadowObservationError("registration entity attempt scope is incomplete")
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path in paths:
        report = _read_private_json(
            path, label="entity Shadow Close report", maximum_bytes=MAX_SOURCE_BYTES,
        )
        try:
            integrity = validate_shadow_close_report(report)
        except ValueError as exc:
            raise PilotShadowObservationError(str(exc)) from exc
        entity_id = str(report.get("entity_id") or "")
        if entity_id not in expected_entities or entity_id in seen:
            raise PilotShadowObservationError(
                "entity Shadow reports must cover configured entities exactly once"
            )
        if report.get("runtime_fingerprint") != runtime.snapshot()["fingerprint"]:
            raise PilotShadowObservationError(
                "entity Shadow report belongs to a different Box runtime"
            )
        if report.get("period") != period:
            raise PilotShadowObservationError(
                "entity Shadow report period does not match the registered pilot period"
            )
        review = report.get("review") if report.get("review_current") is True else None
        if not isinstance(review, dict) or review.get("decision") not in ENTITY_DECISIONS:
            raise PilotShadowObservationError(
                "every entity Shadow report requires a current accepted review"
            )
        system_defect_count = _system_defect_count(report)
        output.append({
            "entity_id": entity_id,
            "source_attempt_id": attempts[entity_id],
            "baseline_id": str(report.get("baseline_id") or ""),
            "report_fingerprint": integrity["report_fingerprint"],
            "report_content_sha256": _hash(report),
            "review_id": str(review.get("id") or ""),
            "decision": review["decision"],
            "review_actor": _actor(review.get("actor"), "entity Shadow reviewer"),
            "comparison_count": integrity["comparison_count"],
            "matched_count": integrity["matched_count"],
            "exception_count": integrity["exception_count"],
            "system_defect_count": system_defect_count,
        })
        seen.add(entity_id)
    if seen != set(expected_entities):
        raise PilotShadowObservationError(
            "entity Shadow reports do not cover every configured entity"
        )
    return sorted(output, key=lambda item: item["entity_id"])


def _collect_portfolio_observation(
    runtime: BoxRuntime,
    portfolio_review_path: str | Path | None,
    entity_observations: list[dict[str, Any]],
    *,
    period: str,
    registration_actor: str,
) -> dict[str, Any] | None:
    multi_entity = len(entity_observations) > 1
    if multi_entity and portfolio_review_path is None:
        raise PilotShadowObservationError(
            "multi-entity pilot observation requires a reviewed portfolio Shadow manifest"
        )
    if not multi_entity and portfolio_review_path is not None:
        raise PilotShadowObservationError(
            "single-entity pilot observation must not attach a portfolio Shadow manifest"
        )
    if portfolio_review_path is None:
        return None
    manifest = _read_private_json(
        portfolio_review_path,
        label="portfolio Shadow Close review",
        maximum_bytes=MAX_SOURCE_BYTES,
    )
    try:
        integrity = validate_multi_entity_shadow_close_manifest(
            runtime, manifest, require_review=True,
        )
    except ValueError as exc:
        raise PilotShadowObservationError(str(exc)) from exc
    if manifest.get("period") != period:
        raise PilotShadowObservationError(
            "portfolio Shadow period does not match the registered pilot period"
        )
    expected_attempts = sorted(
        item["source_attempt_id"] for item in entity_observations
    )
    if manifest.get("portfolio", {}).get("source_attempt_ids") != expected_attempts:
        raise PilotShadowObservationError(
            "portfolio Shadow source attempts do not match the pilot registration"
        )
    expected_reports = {
        item["entity_id"]: (item["report_fingerprint"], item["review_id"])
        for item in entity_observations
    }
    manifest_reports = {
        str(item.get("entity_id") or ""): (
            item.get("report_fingerprint"), item.get("review_id")
        )
        for item in manifest.get("entity_reports") or []
        if isinstance(item, dict)
    }
    if manifest_reports != expected_reports:
        raise PilotShadowObservationError(
            "portfolio Shadow entity reports do not match the supplied observations"
        )
    review = manifest["review"]
    reviewer = _actor(review.get("actor"), "portfolio Shadow reviewer")
    if reviewer == registration_actor:
        raise PilotShadowObservationError(
            "portfolio Shadow reviewer must differ from the pilot registration actor"
        )
    return {
        "manifest_fingerprint": integrity["manifest_fingerprint"],
        "manifest_content_sha256": _hash(manifest),
        "review_id": str(review.get("id") or ""),
        "decision": review.get("decision"),
        "review_actor": reviewer,
        "source_attempt_ids": expected_attempts,
    }


def _candidate_result(entity_observations: list[dict[str, Any]]) -> str:
    if any(item["system_defect_count"] for item in entity_observations):
        return "needs_correction"
    if any(item["exception_count"] for item in entity_observations):
        return "accepted_differences"
    return "passed"


def validate_pilot_shadow_observation_receipt(
    runtime: BoxRuntime,
    receipt: dict[str, Any],
    *,
    require_review: bool = False,
) -> dict[str, Any]:
    expected_fields = {
        "schema_version", "artifact_type", "runtime_fingerprint",
        "pilot_shadow_run_registration_id",
        "pilot_shadow_run_registration_content_sha256", "registration_actor",
        "period", "entity_ids",
        "entity_observations", "portfolio_observation",
        "observation_result_candidate", "status",
        "source_artifacts_input_only", "raw_financial_values_persisted",
        "statutory_books_modified", "posting_performed", "payment_performed",
        "period_close_performed", "external_filing_performed",
        "external_actions_performed", "guardrail", "receipt_fingerprint",
        "review", "review_current",
    }
    _strict_fields(receipt, expected_fields, "pilot Shadow observation receipt")
    snapshot = runtime.snapshot()
    if receipt.get("schema_version") != 1 or receipt.get("artifact_type") != (
        "pilot_shadow_observation_receipt"
    ):
        raise PilotShadowObservationError("pilot Shadow observation contract is invalid")
    if receipt.get("runtime_fingerprint") != snapshot["fingerprint"]:
        raise PilotShadowObservationError(
            "pilot Shadow observation belongs to a different Box runtime"
        )
    if not ID24_PATTERN.fullmatch(
        str(receipt.get("pilot_shadow_run_registration_id") or "")
    ):
        raise PilotShadowObservationError("pilot Shadow registration id is invalid")
    if not HEX64_PATTERN.fullmatch(
        str(receipt.get("pilot_shadow_run_registration_content_sha256") or "")
    ):
        raise PilotShadowObservationError("pilot Shadow registration content hash is invalid")
    if not PERIOD_PATTERN.fullmatch(str(receipt.get("period") or "")):
        raise PilotShadowObservationError("pilot Shadow observation period is invalid")
    registration_actor = _actor(
        receipt.get("registration_actor"), "pilot registration actor",
    )
    expected_entities = sorted(runtime.entities.ids())
    if receipt.get("entity_ids") != expected_entities:
        raise PilotShadowObservationError(
            "pilot Shadow observation must cover every Box entity exactly once"
        )
    observations = receipt.get("entity_observations")
    if not isinstance(observations, list) or len(observations) != len(expected_entities):
        raise PilotShadowObservationError("entity observation summaries are incomplete")
    observation_fields = {
        "entity_id", "source_attempt_id", "baseline_id", "report_fingerprint",
        "report_content_sha256", "review_id", "decision", "review_actor",
        "comparison_count", "matched_count", "exception_count", "system_defect_count",
    }
    seen_entities: list[str] = []
    seen_attempts: list[str] = []
    entity_reviewers: list[str] = []
    for item in observations:
        _strict_fields(item, observation_fields, "entity observation")
        entity_id = str(item.get("entity_id") or "")
        attempt_id = str(item.get("source_attempt_id") or "")
        if entity_id not in expected_entities or not ID24_PATTERN.fullmatch(attempt_id):
            raise PilotShadowObservationError("entity observation identity is invalid")
        if not 2 <= len(str(item.get("baseline_id") or "")) <= 256:
            raise PilotShadowObservationError("entity observation baseline id is invalid")
        for field in ("report_fingerprint", "report_content_sha256"):
            if not HEX64_PATTERN.fullmatch(str(item.get(field) or "")):
                raise PilotShadowObservationError(
                    f"entity observation {field} is invalid"
                )
        if not ENTITY_REVIEW_ID_PATTERN.fullmatch(str(item.get("review_id") or "")):
            raise PilotShadowObservationError("entity Shadow review id is invalid")
        if item.get("decision") not in ENTITY_DECISIONS:
            raise PilotShadowObservationError("entity Shadow decision is not accepted")
        counts = [
            item.get(field) for field in (
                "comparison_count", "matched_count", "exception_count",
                "system_defect_count",
            )
        ]
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in counts):
            raise PilotShadowObservationError("entity observation counts are invalid")
        if counts[0] != counts[1] + counts[2] or counts[3] > counts[2]:
            raise PilotShadowObservationError("entity observation counts are inconsistent")
        if item["decision"] == "验证通过" and counts[2]:
            raise PilotShadowObservationError("clean entity decision cannot contain exceptions")
        if item["decision"] == "接受差异" and not counts[2]:
            raise PilotShadowObservationError("accepted differences require exceptions")
        seen_entities.append(entity_id)
        seen_attempts.append(attempt_id)
        entity_reviewers.append(
            _actor(item.get("review_actor"), "entity Shadow reviewer")
        )
    if seen_entities != expected_entities or len(set(seen_attempts)) != len(seen_attempts):
        raise PilotShadowObservationError("entity observation scope or attempts are duplicated")
    candidate = _candidate_result(observations)
    if receipt.get("observation_result_candidate") != candidate:
        raise PilotShadowObservationError("observation result candidate is inconsistent")
    portfolio = receipt.get("portfolio_observation")
    portfolio_reviewer = None
    if len(expected_entities) == 1:
        if portfolio is not None:
            raise PilotShadowObservationError("single-entity observation cannot contain a portfolio")
    else:
        portfolio_fields = {
            "manifest_fingerprint", "manifest_content_sha256", "review_id",
            "decision", "review_actor", "source_attempt_ids",
        }
        _strict_fields(portfolio, portfolio_fields, "portfolio observation")
        for field in ("manifest_fingerprint", "manifest_content_sha256"):
            if not HEX64_PATTERN.fullmatch(str(portfolio.get(field) or "")):
                raise PilotShadowObservationError(f"portfolio observation {field} is invalid")
        if not PORTFOLIO_REVIEW_ID_PATTERN.fullmatch(
            str(portfolio.get("review_id") or "")
        ):
            raise PilotShadowObservationError("portfolio observation review id is invalid")
        if portfolio.get("decision") not in REVIEW_DECISIONS:
            raise PilotShadowObservationError("portfolio observation decision is invalid")
        if portfolio.get("source_attempt_ids") != sorted(seen_attempts):
            raise PilotShadowObservationError("portfolio attempts do not match entity observations")
        portfolio_reviewer = _actor(
            portfolio.get("review_actor"), "portfolio Shadow reviewer",
        )
        if portfolio_reviewer in set(entity_reviewers):
            raise PilotShadowObservationError(
                "portfolio reviewer must differ from all entity reviewers"
            )
        if portfolio_reviewer == registration_actor:
            raise PilotShadowObservationError(
                "portfolio reviewer must differ from the pilot registration actor"
            )
        if candidate == "passed" and portfolio.get("decision") != "passed":
            raise PilotShadowObservationError("clean observation requires a passed portfolio")
        if candidate == "accepted_differences" and portfolio.get("decision") != (
            "accepted-differences"
        ):
            raise PilotShadowObservationError(
                "accepted entity differences require an accepted-differences portfolio"
            )
    if receipt.get("status") != "ready_for_independent_review":
        raise PilotShadowObservationError("pilot Shadow observation status is invalid")
    if receipt.get("source_artifacts_input_only") is not True:
        raise PilotShadowObservationError("source Shadow artifacts must remain input-only")
    for field in (
        "raw_financial_values_persisted", "statutory_books_modified", "posting_performed",
        "payment_performed", "period_close_performed", "external_filing_performed",
        "external_actions_performed",
    ):
        if receipt.get(field) is not False:
            raise PilotShadowObservationError(f"observation {field} must be false")
    _text(receipt.get("guardrail"), "observation guardrail", minimum=40, maximum=1000)
    expected_fingerprint = _receipt_fingerprint(receipt)
    if receipt.get("receipt_fingerprint") != expected_fingerprint:
        raise PilotShadowObservationError("pilot Shadow observation fingerprint mismatch")
    review = receipt.get("review")
    review_current = receipt.get("review_current") is True
    if require_review and not review_current:
        raise PilotShadowObservationError("pilot Shadow observation is not independently reviewed")
    if review_current:
        review_fields = {
            "review_id", "receipt_fingerprint", "decision", "actor", "rationale",
            "evidence_references", "reviewed_at", "scope_note", "review_fingerprint",
        }
        _strict_fields(review, review_fields, "pilot Shadow observation review")
        if review.get("receipt_fingerprint") != expected_fingerprint:
            raise PilotShadowObservationError("observation review is not bound to this receipt")
        decision = review.get("decision")
        if decision not in REVIEW_DECISIONS:
            raise PilotShadowObservationError("observation review decision is invalid")
        actor = _actor(review.get("actor"), "observation reviewer")
        separated = {registration_actor, *entity_reviewers}
        if portfolio_reviewer:
            separated.add(portfolio_reviewer)
        if actor in separated:
            raise PilotShadowObservationError(
                "observation reviewer must differ from registration, entity and portfolio reviewers"
            )
        _text(review.get("rationale"), "observation review rationale", minimum=12)
        _references(review.get("evidence_references") or [], "observation review evidence")
        _text(
            review.get("scope_note"), "observation review scope note",
            minimum=40, maximum=1000,
        )
        reviewed_at = str(review.get("reviewed_at") or "")
        try:
            parsed = datetime.fromisoformat(reviewed_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise PilotShadowObservationError("observation reviewed_at is invalid") from exc
        if parsed.tzinfo is None:
            raise PilotShadowObservationError("observation reviewed_at requires a timezone")
        expected_id = _hash({
            "receipt_fingerprint": expected_fingerprint,
            "actor": actor,
            "reviewed_at": reviewed_at,
        })[:24]
        if review.get("review_id") != expected_id:
            raise PilotShadowObservationError("observation review id is invalid")
        if review.get("review_fingerprint") != _review_fingerprint(review):
            raise PilotShadowObservationError("observation review fingerprint is invalid")
        if decision == "passed" and candidate != "passed":
            raise PilotShadowObservationError("only a clean observation can be reviewed as passed")
        if decision == "accepted-differences" and candidate != "accepted_differences":
            raise PilotShadowObservationError(
                "accepted-differences review requires accepted observation differences"
            )
    elif review is not None:
        raise PilotShadowObservationError("non-current observation review cannot be attached")
    return {
        "valid": True,
        "receipt_fingerprint": expected_fingerprint,
        "entity_count": len(expected_entities),
        "comparison_count": sum(item["comparison_count"] for item in observations),
        "matched_count": sum(item["matched_count"] for item in observations),
        "exception_count": sum(item["exception_count"] for item in observations),
        "system_defect_count": sum(item["system_defect_count"] for item in observations),
        "entity_reviewers": entity_reviewers,
        "portfolio_reviewer": portfolio_reviewer,
        "registration_actor": registration_actor,
        "candidate": candidate,
        "review_current": review_current,
    }


def assemble_pilot_shadow_observation(
    runtime: BoxRuntime,
    registration_json: str | Path,
    handoff_review: str | Path,
    pilot_readiness_review: str | Path,
    runs_root: str | Path,
    entity_report_paths: Iterable[str | Path],
    output: str | Path,
    *,
    portfolio_review_path: str | Path | None = None,
    as_of: str | None = None,
) -> dict[str, Any]:
    safe_registration, registration = _verified_registration(
        runtime, registration_json, handoff_review, pilot_readiness_review,
        runs_root, as_of=as_of,
    )
    registration_actor = _actor(
        registration.get("registered_by"), "pilot registration actor",
    )
    observations = _collect_entity_observations(
        runtime, entity_report_paths, registration, period=safe_registration["period"],
    )
    portfolio = _collect_portfolio_observation(
        runtime, portfolio_review_path, observations,
        period=safe_registration["period"], registration_actor=registration_actor,
    )
    receipt = {
        "schema_version": 1,
        "artifact_type": "pilot_shadow_observation_receipt",
        "runtime_fingerprint": safe_registration["runtime_fingerprint"],
        "pilot_shadow_run_registration_id": safe_registration["registration_id"],
        "pilot_shadow_run_registration_content_sha256": _hash(registration),
        "registration_actor": registration_actor,
        "period": safe_registration["period"],
        "entity_ids": sorted(runtime.entities.ids()),
        "entity_observations": observations,
        "portfolio_observation": portfolio,
        "observation_result_candidate": _candidate_result(observations),
        "status": "ready_for_independent_review",
        "source_artifacts_input_only": True,
        "raw_financial_values_persisted": False,
        "statutory_books_modified": False,
        "posting_performed": False,
        "payment_performed": False,
        "period_close_performed": False,
        "external_filing_performed": False,
        "external_actions_performed": False,
        "guardrail": (
            "This private no-values receipt binds the current pilot registration to reviewed "
            "entity Shadow reports and, when required, one reviewed portfolio. It does not "
            "authorize posting, payment, statutory close, filing or Pack promotion."
        ),
        "review": None,
        "review_current": False,
    }
    receipt["receipt_fingerprint"] = _receipt_fingerprint(receipt)
    integrity = validate_pilot_shadow_observation_receipt(runtime, receipt)
    destination = _write_private(output, receipt)
    return {
        "schema_version": 1,
        "valid": True,
        "runtime_fingerprint": receipt["runtime_fingerprint"],
        "period": receipt["period"],
        "entity_count": integrity["entity_count"],
        "comparison_count": integrity["comparison_count"],
        "exception_count": integrity["exception_count"],
        "system_defect_count": integrity["system_defect_count"],
        "observation_result_candidate": integrity["candidate"],
        "review_current": False,
        "output_written": destination.is_file(),
        "raw_financial_values_written_to_output": False,
        "source_attempt_ids_returned": False,
        "source_report_fingerprints_returned": False,
        "actors_returned": False,
        "external_actions_performed": False,
    }


def review_pilot_shadow_observation(
    runtime: BoxRuntime,
    receipt_json: str | Path,
    output: str | Path,
    *,
    decision: str,
    actor: str,
    rationale: str,
    evidence_references: Iterable[str],
) -> dict[str, Any]:
    receipt = _read_private_json(
        receipt_json, label="pilot Shadow observation receipt",
        maximum_bytes=MAX_RECEIPT_BYTES,
    )
    integrity = validate_pilot_shadow_observation_receipt(runtime, receipt)
    if receipt.get("review_current") is True:
        raise PilotShadowObservationError(
            "pilot Shadow observation is already reviewed; assemble a new receipt"
        )
    if decision not in REVIEW_DECISIONS:
        raise PilotShadowObservationError(
            "decision must be passed, accepted-differences or needs-correction"
        )
    reviewer = _actor(actor, "observation reviewer")
    separated = {integrity["registration_actor"], *integrity["entity_reviewers"]}
    if integrity["portfolio_reviewer"]:
        separated.add(integrity["portfolio_reviewer"])
    if reviewer in separated:
        raise PilotShadowObservationError(
            "observation reviewer must differ from registration, entity and portfolio reviewers"
        )
    rationale_value = _text(
        rationale, "observation review rationale", minimum=12, maximum=1000,
    )
    evidence = _references(evidence_references, "observation review evidence")
    if decision == "passed" and integrity["candidate"] != "passed":
        raise PilotShadowObservationError("only a clean observation can be reviewed as passed")
    if decision == "accepted-differences" and integrity["candidate"] != (
        "accepted_differences"
    ):
        raise PilotShadowObservationError(
            "accepted-differences review requires accepted observation differences"
        )
    reviewed_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    review_id = _hash({
        "receipt_fingerprint": integrity["receipt_fingerprint"],
        "actor": reviewer,
        "reviewed_at": reviewed_at,
    })[:24]
    review = {
        "review_id": review_id,
        "receipt_fingerprint": integrity["receipt_fingerprint"],
        "decision": decision,
        "actor": reviewer,
        "rationale": rationale_value,
        "evidence_references": evidence,
        "reviewed_at": reviewed_at,
        "scope_note": (
            "This review applies only to the exact registration, entity reports and portfolio "
            "fingerprints in this receipt. It grants no external financial authority."
        ),
    }
    review["review_fingerprint"] = _review_fingerprint(review)
    reviewed = dict(receipt)
    reviewed["review"] = review
    reviewed["review_current"] = True
    validate_pilot_shadow_observation_receipt(runtime, reviewed, require_review=True)
    destination = _write_private(output, reviewed)
    return {
        "schema_version": 1,
        "valid": True,
        "runtime_fingerprint": reviewed["runtime_fingerprint"],
        "period": reviewed["period"],
        "entity_count": integrity["entity_count"],
        "comparison_count": integrity["comparison_count"],
        "exception_count": integrity["exception_count"],
        "system_defect_count": integrity["system_defect_count"],
        "decision": decision,
        "review_id": review_id,
        "review_current": True,
        "ready_for_next_shadow_period": decision in {"passed", "accepted-differences"},
        "output_written": destination.is_file(),
        "raw_financial_values_written_to_output": False,
        "actors_returned": False,
        "external_actions_performed": False,
    }


def verify_pilot_shadow_observation(
    runtime: BoxRuntime,
    reviewed_receipt_json: str | Path,
    registration_json: str | Path,
    handoff_review: str | Path,
    pilot_readiness_review: str | Path,
    runs_root: str | Path,
    entity_report_paths: Iterable[str | Path],
    *,
    portfolio_review_path: str | Path | None = None,
    as_of: str | None = None,
) -> dict[str, Any]:
    safe_registration, registration = _verified_registration(
        runtime, registration_json, handoff_review, pilot_readiness_review,
        runs_root, as_of=as_of,
    )
    receipt = _read_private_json(
        reviewed_receipt_json, label="reviewed pilot Shadow observation receipt",
        maximum_bytes=MAX_RECEIPT_BYTES,
    )
    integrity = validate_pilot_shadow_observation_receipt(
        runtime, receipt, require_review=True,
    )
    if receipt["pilot_shadow_run_registration_id"] != safe_registration["registration_id"]:
        raise PilotShadowObservationError(
            "observation receipt is bound to a different pilot registration"
        )
    if receipt["pilot_shadow_run_registration_content_sha256"] != _hash(registration):
        raise PilotShadowObservationError(
            "pilot registration artifact changed after observation assembly"
        )
    if receipt["period"] != safe_registration["period"]:
        raise PilotShadowObservationError(
            "observation period no longer matches the current pilot registration"
        )
    observations = _collect_entity_observations(
        runtime, entity_report_paths, registration, period=safe_registration["period"],
    )
    registration_actor = _actor(
        registration.get("registered_by"), "pilot registration actor",
    )
    portfolio = _collect_portfolio_observation(
        runtime, portfolio_review_path, observations,
        period=safe_registration["period"], registration_actor=registration_actor,
    )
    if observations != receipt["entity_observations"] or portfolio != receipt[
        "portfolio_observation"
    ]:
        raise PilotShadowObservationError(
            "source Shadow evidence no longer matches the reviewed observation receipt"
        )
    decision = receipt["review"]["decision"]
    ready = decision in {"passed", "accepted-differences"}
    return {
        "schema_version": 1,
        "valid": True,
        "runtime_fingerprint": receipt["runtime_fingerprint"],
        "period": receipt["period"],
        "entity_count": integrity["entity_count"],
        "comparison_count": integrity["comparison_count"],
        "matched_count": integrity["matched_count"],
        "exception_count": integrity["exception_count"],
        "system_defect_count": integrity["system_defect_count"],
        "decision": decision,
        "review_id": receipt["review"]["review_id"],
        "review_current": True,
        "ready_for_next_shadow_period": ready,
        "ready_for_stable_promotion": False,
        "ready_for_statutory_release": False,
        "ready_for_external_filing": False,
        "source_attempt_ids_returned": False,
        "source_report_fingerprints_returned": False,
        "source_artifact_hashes_returned": False,
        "actors_returned": False,
        "review_rationales_returned": False,
        "evidence_references_returned": False,
        "raw_financial_values_returned": False,
        "posting_authorized": False,
        "payment_authorized": False,
        "period_close_authorized": False,
        "external_filing_authorized": False,
        "external_actions_performed": False,
    }


def build_pilot_shadow_observation_status(
    runtime: BoxRuntime,
    reviewed_receipt_json: str | Path | None = None,
    registration_json: str | Path | None = None,
    handoff_review: str | Path | None = None,
    pilot_readiness_review: str | Path | None = None,
    runs_root: str | Path | None = None,
    entity_report_paths: Iterable[str | Path] = (),
    *,
    portfolio_review_path: str | Path | None = None,
    as_of: str | None = None,
) -> dict[str, Any]:
    """Build a secret-safe current-state projection for Doctor and Workbench."""
    reports = list(entity_report_paths)
    configured = reviewed_receipt_json is not None
    base = {
        "schema_version": 1,
        "configured": configured,
        "registration_configured": registration_json is not None,
        "handoff_configured": handoff_review is not None,
        "pilot_readiness_configured": pilot_readiness_review is not None,
        "pipeline_ledger_configured": runs_root is not None,
        "entity_reports_configured": bool(reports),
        "portfolio_review_configured": portfolio_review_path is not None,
        "valid": False,
        "status": "missing" if not configured else "invalid",
        "ready_for_next_shadow_period": False,
        "ready_for_stable_promotion": False,
        "ready_for_statutory_release": False,
        "ready_for_external_filing": False,
        "paths_returned": False,
        "source_attempt_ids_returned": False,
        "source_report_fingerprints_returned": False,
        "source_artifact_hashes_returned": False,
        "actors_returned": False,
        "review_rationales_returned": False,
        "evidence_references_returned": False,
        "raw_financial_values_returned": False,
        "posting_authorized": False,
        "payment_authorized": False,
        "period_close_authorized": False,
        "external_filing_authorized": False,
        "external_actions_performed": False,
    }
    if not configured:
        return base
    if not all((registration_json, handoff_review, pilot_readiness_review, runs_root)):
        base["error_sha256"] = _hash(
            "reviewed pilot Shadow observation requires registration, handoff, "
            "readiness and pipeline ledger companions"
        )
        return base
    try:
        verified = verify_pilot_shadow_observation(
            runtime, reviewed_receipt_json, registration_json, handoff_review,
            pilot_readiness_review, runs_root, reports,
            portfolio_review_path=portfolio_review_path, as_of=as_of,
        )
    except (ValueError, OSError) as exc:
        base["error_sha256"] = _hash(str(exc))
        return base
    return {
        **base,
        "valid": True,
        "status": "current",
        "runtime_fingerprint": verified["runtime_fingerprint"],
        "period": verified["period"],
        "entity_count": verified["entity_count"],
        "comparison_count": verified["comparison_count"],
        "matched_count": verified["matched_count"],
        "exception_count": verified["exception_count"],
        "system_defect_count": verified["system_defect_count"],
        "decision": verified["decision"],
        "review_id": verified["review_id"],
        "ready_for_next_shadow_period": verified["ready_for_next_shadow_period"],
    }


def _entity_report_paths_from_directory(
    runtime: BoxRuntime, directory: str | Path | None,
) -> list[Path]:
    if directory is None:
        return []
    root = Path(directory)
    if root.is_symlink() or not root.is_dir():
        raise PilotShadowObservationError(
            "pilot Shadow entity report directory must be a non-symbolic directory"
        )
    expected_names = {f"{entity_id}.json" for entity_id in runtime.entities.ids()}
    actual_names = {item.name for item in root.iterdir()}
    if actual_names != expected_names:
        raise PilotShadowObservationError(
            "pilot Shadow entity report directory must contain only exact entity_id.json files"
        )
    paths = [root / f"{entity_id}.json" for entity_id in sorted(runtime.entities.ids())]
    if any(path.is_symlink() or not path.is_file() for path in paths):
        raise PilotShadowObservationError(
            "pilot Shadow entity reports must be non-symbolic regular files"
        )
    return paths


def build_pilot_shadow_observation_workspace(
    runtime: BoxRuntime,
    reviewed_receipt_json: str | Path | None = None,
    registration_json: str | Path | None = None,
    handoff_review: str | Path | None = None,
    pilot_readiness_review: str | Path | None = None,
    runs_root: str | Path | None = None,
    entity_report_directory: str | Path | None = None,
    *,
    portfolio_review_path: str | Path | None = None,
    as_of: str | None = None,
) -> dict[str, Any]:
    """Return a browser-safe projection; private artifacts stay server-mounted inputs."""
    try:
        report_paths = _entity_report_paths_from_directory(
            runtime, entity_report_directory,
        )
        status = build_pilot_shadow_observation_status(
            runtime, reviewed_receipt_json, registration_json, handoff_review,
            pilot_readiness_review, runs_root, report_paths,
            portfolio_review_path=portfolio_review_path, as_of=as_of,
        )
    except (ValueError, OSError) as exc:
        status = build_pilot_shadow_observation_status(
            runtime, reviewed_receipt_json, registration_json, handoff_review,
            pilot_readiness_review, runs_root, [],
            portfolio_review_path=portfolio_review_path, as_of=as_of,
        )
        if reviewed_receipt_json is not None:
            status.update({
                "status": "invalid", "valid": False,
                "ready_for_next_shadow_period": False,
                "error_sha256": _hash(str(exc)),
            })
    entity_count = len(runtime.entities.ids())
    current = status["status"] == "current"
    return {
        "schema_version": 1,
        "summary": {
            "activation_status": status["status"],
            "entity_count": entity_count,
            "reviewed_entity_count": entity_count if current else 0,
            "period": status.get("period"),
            "comparison_count": status.get("comparison_count", 0),
            "matched_count": status.get("matched_count", 0),
            "exception_count": status.get("exception_count", 0),
            "system_defect_count": status.get("system_defect_count", 0),
            "decision": status.get("decision"),
            "portfolio_review_required": entity_count > 1,
            "portfolio_review_configured": status["portfolio_review_configured"],
            "ready_for_next_shadow_period": status[
                "ready_for_next_shadow_period"
            ],
        },
        "entities": [{
            "entity_id": entity_id,
            "reviewed_shadow_report_bound": current,
        } for entity_id in sorted(runtime.entities.ids())],
        "control_boundary": {
            "private_mounts_server_configured_only": True,
            "paths_returned": False,
            "source_attempt_ids_returned": False,
            "source_report_fingerprints_returned": False,
            "source_artifact_hashes_returned": False,
            "actors_returned": False,
            "review_rationales_returned": False,
            "evidence_references_returned": False,
            "raw_financial_values_returned": False,
            "ready_for_stable_promotion": False,
            "ready_for_statutory_release": False,
            "ready_for_external_filing": False,
            "posting_authorized": False,
            "payment_authorized": False,
            "period_close_authorized": False,
            "external_filing_authorized": False,
            "external_actions_performed": False,
        },
        **({"error_sha256": status["error_sha256"]}
           if "error_sha256" in status else {}),
    }
