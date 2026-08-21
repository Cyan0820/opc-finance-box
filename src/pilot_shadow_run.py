from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Any, Iterable, Mapping

from .box_runtime import BoxRuntime
from .pilot_data_handoff import verify_pilot_data_handoff_review
from .pipeline_run_store import PipelineRunStore, PipelineRunStoreError


MAX_ARTIFACT_BYTES = 2 * 1024 * 1024
ACTOR_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@:-]{1,127}$")
ATTEMPT_ID_PATTERN = re.compile(r"^[0-9a-f]{24}$")
FINGERPRINT_PATTERN = re.compile(r"^[0-9a-f]{64}$")
REFERENCE_PATTERN = re.compile(
    r"^(?:evidence|document|workpaper|registry|advisor|authority)://"
    r"[A-Za-z0-9][A-Za-z0-9._/#:-]{1,199}$"
)


class PilotShadowRunError(ValueError):
    """Raised when a first-company Shadow Run cannot be safely registered."""


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _actor(value: Any, field: str) -> str:
    actor = str(value or "").strip()
    if not ACTOR_PATTERN.fullmatch(actor):
        raise PilotShadowRunError(
            f"{field} must be a 2-128 character stable actor identifier"
        )
    return actor


def _references(value: Iterable[str], field: str) -> list[str]:
    references = list(value)
    if not references or len(references) > 20 or len(references) != len(set(references)):
        raise PilotShadowRunError(f"{field} requires 1-20 unique opaque references")
    if any(
        not isinstance(item, str) or not REFERENCE_PATTERN.fullmatch(item)
        for item in references
    ):
        raise PilotShadowRunError(
            f"{field} must contain evidence:// style opaque references"
        )
    return references


def _read_private(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    if source.is_symlink():
        raise PilotShadowRunError("pilot Shadow Run artifacts must not be symbolic links")
    if not source.is_file():
        raise PilotShadowRunError("pilot Shadow Run registration does not exist")
    metadata = source.stat()
    if os.name != "nt" and stat.S_IMODE(metadata.st_mode) & 0o077:
        raise PilotShadowRunError(
            "pilot Shadow Run registration must not be accessible by group or other users"
        )
    if metadata.st_size <= 0 or metadata.st_size > MAX_ARTIFACT_BYTES:
        raise PilotShadowRunError("pilot Shadow Run registration must be 1 byte to 2 MiB")
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PilotShadowRunError("pilot Shadow Run registration must be valid JSON") from exc
    if not isinstance(value, dict):
        raise PilotShadowRunError("pilot Shadow Run registration must be a JSON object")
    return value


def _write_private(path: str | Path, value: dict[str, Any]) -> Path:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise PilotShadowRunError(
            "pilot Shadow Run registration already exists; refusing to overwrite"
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
        raise PilotShadowRunError(f"{label} fields do not match the registration contract")


def _validated_entity_runs(
    runtime: BoxRuntime,
    store: PipelineRunStore,
    entity_attempts: Mapping[str, str],
    *,
    period: str,
    registrar: str,
) -> list[dict[str, Any]]:
    expected_entities = set(runtime.entities.ids())
    supplied_entities = set(entity_attempts)
    if supplied_entities != expected_entities:
        missing = sorted(expected_entities - supplied_entities)
        unexpected = sorted(supplied_entities - expected_entities)
        raise PilotShadowRunError(
            "entity attempt scope must exactly match the Box; "
            f"missing={missing}, unexpected={unexpected}"
        )
    attempt_ids = list(entity_attempts.values())
    if any(not ATTEMPT_ID_PATTERN.fullmatch(str(item or "")) for item in attempt_ids):
        raise PilotShadowRunError("every entity requires a valid 24-character attempt_id")
    if len(attempt_ids) != len(set(attempt_ids)):
        raise PilotShadowRunError("entity attempt_id values must be unique")

    fingerprint = runtime.snapshot()["fingerprint"]
    verified: list[dict[str, Any]] = []
    for entity_id in sorted(expected_entities):
        attempt_id = entity_attempts[entity_id]
        try:
            record = store.get(attempt_id, runtime_fingerprint=fingerprint)
        except PipelineRunStoreError as exc:
            raise PilotShadowRunError(str(exc)) from exc
        if record is None:
            raise PilotShadowRunError(
                f"Shadow Run attempt was not found for Box entity {entity_id}"
            )
        if record.get("entity_id") != entity_id:
            raise PilotShadowRunError("Shadow Run entity binding does not match the registration")
        if record.get("pipeline_id") != "finance.month_close_control":
            raise PilotShadowRunError("only finance.month_close_control attempts may be registered")
        if record.get("period") != period:
            raise PilotShadowRunError("Shadow Run period does not match the approved handoff period")
        if not all(
            record.get(field) is True
            for field in ("ready", "review_complete", "release_candidate")
        ):
            raise PilotShadowRunError(
                "every Shadow Run must be ready, fully reviewed and a release candidate"
            )
        if record.get("status") != "ready" or record.get("review_status") != "approved":
            raise PilotShadowRunError("Shadow Run control status is not fully approved")
        if record.get("external_actions_performed") is not False:
            raise PilotShadowRunError("Shadow Run performed an external action")
        if any(
            record.get(field) is not False
            for field in (
                "posting_performed", "full_request_persisted", "full_result_persisted",
                "secret_values_persisted",
            )
        ):
            raise PilotShadowRunError("Shadow Run persistence or posting boundary is unsafe")
        required_gates = record.get("required_review_gates")
        current_reviews = record.get("current_reviews")
        if not isinstance(required_gates, list) or not required_gates:
            raise PilotShadowRunError("month-close Shadow Run requires explicit review gates")
        if not isinstance(current_reviews, dict) or set(current_reviews) != set(required_gates):
            raise PilotShadowRunError("Shadow Run does not have one current decision for every gate")
        if any(
            not isinstance(current_reviews.get(gate), dict)
            or current_reviews[gate].get("decision") != "approved"
            for gate in required_gates
        ):
            raise PilotShadowRunError("every current Shadow Run gate decision must be approved")
        executor = str(record.get("actor") or "")
        reviewers = {
            str(current_reviews[gate].get("actor") or "") for gate in required_gates
        }
        if not executor or "" in reviewers:
            raise PilotShadowRunError("Shadow Run actor evidence is incomplete")
        if executor in reviewers:
            raise PilotShadowRunError("Shadow Run operator and reviewers must be separated")
        if registrar == executor or registrar in reviewers:
            raise PilotShadowRunError(
                "Shadow Run registrar must differ from the operator and current reviewers"
            )
        result_fingerprint = record.get("result_fingerprint")
        if not FINGERPRINT_PATTERN.fullmatch(str(result_fingerprint or "")):
            raise PilotShadowRunError("Shadow Run result fingerprint is invalid")
        verified.append({
            "entity_id": entity_id,
            "attempt_id": attempt_id,
            "pipeline_id": "finance.month_close_control",
            "period": period,
            "result_fingerprint": result_fingerprint,
        })
    return verified


def register_pilot_shadow_run(
    runtime: BoxRuntime,
    handoff_review: str | Path,
    pilot_readiness_review: str | Path,
    runs_root: str | Path,
    entity_attempts: Mapping[str, str],
    output: str | Path,
    *,
    actor: str,
    rationale: str,
    evidence_references: Iterable[str],
    as_of: str | None = None,
) -> dict[str, Any]:
    handoff = verify_pilot_data_handoff_review(
        runtime, handoff_review, pilot_readiness_review, as_of=as_of,
    )
    if not handoff["ready_for_bounded_shadow"]:
        raise PilotShadowRunError("approved handoff is not current for a bounded Shadow Run")
    registrar = _actor(actor, "pilot Shadow Run registrar")
    rationale_value = str(rationale or "").strip()
    if not 12 <= len(rationale_value) <= 1000:
        raise PilotShadowRunError("registration rationale must be 12-1000 characters")
    references = _references(evidence_references, "registration evidence")
    store = PipelineRunStore(runs_root)
    try:
        ledger = store.verify(runtime_fingerprint=handoff["runtime_fingerprint"])
    except PipelineRunStoreError as exc:
        raise PilotShadowRunError(str(exc)) from exc
    entity_runs = _validated_entity_runs(
        runtime, store, entity_attempts, period=handoff["period"], registrar=registrar,
    )
    registered_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    registration = {
        "schema_version": 1,
        "artifact_type": "pilot_shadow_run_registration",
        "runtime_fingerprint": handoff["runtime_fingerprint"],
        "pilot_readiness_review_id": handoff["pilot_readiness_review_id"],
        "pilot_data_handoff_review_id": handoff["review_id"],
        "period": handoff["period"],
        "registered_at": registered_at,
        "registered_by": registrar,
        "rationale": rationale_value,
        "evidence_references": references,
        "ledger_integrity": ledger["integrity"],
        "ledger_chain_head_at_registration": ledger["chain_head"],
        "entity_runs": entity_runs,
        "all_entities_covered": True,
        "all_runs_ready": True,
        "all_reviews_complete": True,
        "registrar_role_separation_verified": True,
        "raw_pipeline_request_or_result_persisted": False,
        "financial_values_persisted": False,
        "posting_authorized": False,
        "payment_authorized": False,
        "period_close_authorized": False,
        "external_filing_authorized": False,
        "external_actions_authorized": False,
    }
    registration["registration_id"] = _hash(registration)[:24]
    target = _write_private(output, registration)
    return {
        "schema_version": 1,
        "valid": True,
        "registration_id": registration["registration_id"],
        "runtime_fingerprint": registration["runtime_fingerprint"],
        "period": registration["period"],
        "entity_count": len(entity_runs),
        "ready_for_first_shadow_observation": True,
        "file_mode": "0600" if os.name != "nt" else "private_acl_review_required",
        "output_written": target.is_file(),
        "attempt_ids_returned": False,
        "result_fingerprints_returned": False,
        "actors_returned": False,
        "evidence_references_returned": False,
        "financial_values_returned": False,
        "posting_authorized": False,
        "payment_authorized": False,
        "period_close_authorized": False,
        "external_filing_authorized": False,
        "external_actions_performed": False,
    }


def verify_pilot_shadow_run_registration(
    runtime: BoxRuntime,
    registration_json: str | Path,
    handoff_review: str | Path,
    pilot_readiness_review: str | Path,
    runs_root: str | Path,
    *,
    as_of: str | None = None,
) -> dict[str, Any]:
    handoff = verify_pilot_data_handoff_review(
        runtime, handoff_review, pilot_readiness_review, as_of=as_of,
    )
    if not handoff["ready_for_bounded_shadow"]:
        raise PilotShadowRunError("approved handoff is not current for a bounded Shadow Run")
    value = _read_private(registration_json)
    expected_fields = {
        "schema_version", "artifact_type", "runtime_fingerprint",
        "pilot_readiness_review_id", "pilot_data_handoff_review_id", "period",
        "registered_at", "registered_by", "rationale", "evidence_references",
        "ledger_integrity", "ledger_chain_head_at_registration", "entity_runs",
        "all_entities_covered", "all_runs_ready", "all_reviews_complete",
        "registrar_role_separation_verified",
        "raw_pipeline_request_or_result_persisted", "financial_values_persisted",
        "posting_authorized", "payment_authorized", "period_close_authorized",
        "external_filing_authorized", "external_actions_authorized", "registration_id",
    }
    _strict_fields(value, expected_fields, "pilot Shadow Run registration")
    if value.get("schema_version") != 1 or value.get("artifact_type") != "pilot_shadow_run_registration":
        raise PilotShadowRunError("pilot Shadow Run registration type or version is invalid")
    bindings = {
        "runtime_fingerprint": handoff["runtime_fingerprint"],
        "pilot_readiness_review_id": handoff["pilot_readiness_review_id"],
        "pilot_data_handoff_review_id": handoff["review_id"],
        "period": handoff["period"],
    }
    if any(value.get(field) != expected for field, expected in bindings.items()):
        raise PilotShadowRunError("pilot Shadow Run registration binding is stale or invalid")
    try:
        registered_at = datetime.fromisoformat(
            str(value.get("registered_at") or "").replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise PilotShadowRunError("registered_at must be an ISO date-time") from exc
    if registered_at.tzinfo is None:
        raise PilotShadowRunError("registered_at must include a timezone")
    registrar = _actor(value.get("registered_by"), "pilot Shadow Run registrar")
    rationale = str(value.get("rationale") or "").strip()
    if not 12 <= len(rationale) <= 1000:
        raise PilotShadowRunError("registration rationale must be 12-1000 characters")
    _references(value.get("evidence_references") or [], "registration evidence")
    if value.get("ledger_integrity") != "sha256_hash_chain":
        raise PilotShadowRunError("registration ledger integrity contract is invalid")
    if not FINGERPRINT_PATTERN.fullmatch(str(value.get("ledger_chain_head_at_registration") or "")):
        if value.get("ledger_chain_head_at_registration") != "GENESIS":
            raise PilotShadowRunError("registration ledger chain head is invalid")
    true_controls = (
        "all_entities_covered", "all_runs_ready", "all_reviews_complete",
        "registrar_role_separation_verified",
    )
    false_controls = (
        "raw_pipeline_request_or_result_persisted", "financial_values_persisted",
        "posting_authorized", "payment_authorized", "period_close_authorized",
        "external_filing_authorized", "external_actions_authorized",
    )
    if any(value.get(field) is not True for field in true_controls) or any(
        value.get(field) is not False for field in false_controls
    ):
        raise PilotShadowRunError("pilot Shadow Run registration control flags are invalid")
    entity_runs = value.get("entity_runs")
    if not isinstance(entity_runs, list) or not entity_runs:
        raise PilotShadowRunError("pilot Shadow Run registration requires entity runs")
    expected_run_fields = {
        "entity_id", "attempt_id", "pipeline_id", "period", "result_fingerprint",
    }
    for item in entity_runs:
        _strict_fields(item, expected_run_fields, "registered entity run")
    entity_attempts = {str(item["entity_id"]): str(item["attempt_id"]) for item in entity_runs}
    if len(entity_attempts) != len(entity_runs):
        raise PilotShadowRunError("registered entity ids must be unique")
    store = PipelineRunStore(runs_root)
    try:
        ledger = store.verify(runtime_fingerprint=handoff["runtime_fingerprint"])
    except PipelineRunStoreError as exc:
        raise PilotShadowRunError(str(exc)) from exc
    verified_runs = _validated_entity_runs(
        runtime, store, entity_attempts, period=handoff["period"], registrar=registrar,
    )
    supplied_by_entity = {item["entity_id"]: item for item in entity_runs}
    if any(supplied_by_entity[item["entity_id"]] != item for item in verified_runs):
        raise PilotShadowRunError("registered Shadow Run evidence no longer matches the ledger")
    registration_id = value.pop("registration_id")
    try:
        if registration_id != _hash(value)[:24]:
            raise PilotShadowRunError("pilot Shadow Run registration_id is invalid")
    finally:
        value["registration_id"] = registration_id
    return {
        "schema_version": 1,
        "valid": True,
        "registration_id": registration_id,
        "runtime_fingerprint": handoff["runtime_fingerprint"],
        "period": handoff["period"],
        "pilot_readiness_lifecycle_status": handoff[
            "pilot_readiness_lifecycle_status"
        ],
        "entity_count": len(verified_runs),
        "ledger_currently_valid": ledger["valid"],
        "registration_chain_head_is_historical": (
            value["ledger_chain_head_at_registration"] != ledger["chain_head"]
        ),
        "ready_for_first_shadow_observation": True,
        "ready_for_statutory_release": False,
        "ready_for_external_filing": False,
        "attempt_ids_returned": False,
        "result_fingerprints_returned": False,
        "actors_returned": False,
        "review_rationales_returned": False,
        "evidence_references_returned": False,
        "financial_values_returned": False,
        "posting_authorized": False,
        "payment_authorized": False,
        "period_close_authorized": False,
        "external_filing_authorized": False,
        "external_actions_performed": False,
    }


def build_pilot_shadow_run_status(
    runtime: BoxRuntime,
    registration_json: str | Path | None = None,
    handoff_review: str | Path | None = None,
    pilot_readiness_review: str | Path | None = None,
    runs_root: str | Path | None = None,
    *,
    as_of: str | None = None,
) -> dict[str, Any]:
    fingerprint = runtime.snapshot()["fingerprint"]
    base = {
        "schema_version": 1,
        "runtime_fingerprint": fingerprint,
        "configured": registration_json is not None,
        "handoff_configured": handoff_review is not None,
        "pilot_readiness_configured": pilot_readiness_review is not None,
        "pipeline_ledger_configured": runs_root is not None,
        "valid": False,
        "ready_for_first_shadow_observation": False,
        "ready_for_statutory_release": False,
        "ready_for_external_filing": False,
        "paths_returned": False,
        "attempt_ids_returned": False,
        "result_fingerprints_returned": False,
        "actors_returned": False,
        "review_rationales_returned": False,
        "evidence_references_returned": False,
        "financial_values_returned": False,
        "posting_authorized": False,
        "payment_authorized": False,
        "period_close_authorized": False,
        "external_filing_authorized": False,
        "external_actions_performed": False,
    }
    if registration_json is None:
        return {**base, "status": "missing"}
    if handoff_review is None or pilot_readiness_review is None or runs_root is None:
        message = (
            "pilot Shadow Run registration requires handoff, readiness and pipeline ledger"
        )
        return {
            **base,
            "status": "invalid",
            "error_sha256": hashlib.sha256(message.encode("utf-8")).hexdigest(),
        }
    try:
        verified = verify_pilot_shadow_run_registration(
            runtime, registration_json, handoff_review, pilot_readiness_review,
            runs_root, as_of=as_of,
        )
    except (PilotShadowRunError, PipelineRunStoreError, OSError, ValueError) as exc:
        return {
            **base,
            "status": "invalid",
            "error_sha256": hashlib.sha256(str(exc).encode("utf-8")).hexdigest(),
        }
    status = (
        "review_due"
        if verified["pilot_readiness_lifecycle_status"] == "review_due"
        else "current"
    )
    return {
        **base,
        **verified,
        "configured": True,
        "handoff_configured": True,
        "pilot_readiness_configured": True,
        "pipeline_ledger_configured": True,
        "status": status,
    }


def build_pilot_shadow_run_workspace(
    runtime: BoxRuntime,
    registration_json: str | Path | None = None,
    handoff_review: str | Path | None = None,
    pilot_readiness_review: str | Path | None = None,
    runs_root: str | Path | None = None,
    *,
    as_of: str | None = None,
) -> dict[str, Any]:
    status = build_pilot_shadow_run_status(
        runtime, registration_json, handoff_review, pilot_readiness_review,
        runs_root, as_of=as_of,
    )
    entity_ids = sorted(runtime.entities.ids())
    active = status["status"] in {"current", "review_due"} and status[
        "ready_for_first_shadow_observation"
    ]
    return {
        "schema_version": 1,
        "runtime_fingerprint": status["runtime_fingerprint"],
        "summary": {
            "entity_count": len(entity_ids),
            "registered_entity_count": len(entity_ids) if active else 0,
            "activation_status": status["status"],
            "ready_for_first_shadow_observation": active,
            "period": status.get("period"),
        },
        "entities": [
            {"entity_id": entity_id, "reviewed_month_close_registered": active}
            for entity_id in entity_ids
        ],
        "activation": status,
        "control_boundary": {
            "raw_pipeline_request_or_result_returned": False,
            "attempt_ids_returned": False,
            "result_fingerprints_returned": False,
            "actors_returned": False,
            "review_rationales_returned": False,
            "evidence_references_returned": False,
            "financial_values_returned": False,
            "ready_for_statutory_release": False,
            "ready_for_external_filing": False,
            "posting_authorized": False,
            "payment_authorized": False,
            "period_close_authorized": False,
            "external_filing_authorized": False,
            "external_actions_performed": False,
        },
    }
