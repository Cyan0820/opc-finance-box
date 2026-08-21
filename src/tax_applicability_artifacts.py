from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Any, Iterable

from .box_runtime import BoxRuntime
from .tax_pack_lifecycle import build_tax_applicability_questionnaire


MAX_ARTIFACT_BYTES = 2 * 1024 * 1024
DECISIONS = {
    "approved-in-scope", "confirmed-out-of-scope", "needs-correction",
}
ACTOR_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@:-]{1,127}$")
EVIDENCE_REFERENCE_PATTERN = re.compile(
    r"^(?:evidence|document|workpaper|registry|advisor|authority)://"
    r"[A-Za-z0-9][A-Za-z0-9._/#:-]{1,199}$"
)


class TaxApplicabilityArtifactError(ValueError):
    """Raised when a tax applicability workpaper or review is unsafe or invalid."""


def _hash(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _actor(value: Any, field: str) -> str:
    actor = str(value or "").strip()
    if not ACTOR_PATTERN.fullmatch(actor):
        raise TaxApplicabilityArtifactError(
            f"{field} must be a 2-128 character stable actor identifier"
        )
    return actor


def _canonical_date(value: Any, field: str) -> date:
    text = str(value or "")
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise TaxApplicabilityArtifactError(f"{field} must use YYYY-MM-DD") from exc
    if parsed.isoformat() != text:
        raise TaxApplicabilityArtifactError(f"{field} must use canonical YYYY-MM-DD")
    return parsed


def _facts_date(runtime: BoxRuntime, entity_id: str, value: Any) -> date:
    facts_as_of = _canonical_date(value, "facts_as_of")
    rules_verified_at = date.fromisoformat(
        runtime.tax_rules(entity_id)["rules"]["verified_at"]
    )
    if facts_as_of < rules_verified_at:
        raise TaxApplicabilityArtifactError(
            "facts_as_of cannot predate the selected Pack rules_verified_at"
        )
    if facts_as_of > datetime.now(timezone.utc).date():
        raise TaxApplicabilityArtifactError("facts_as_of cannot be in the future")
    return facts_as_of


def _references(
    value: Any, field: str, *, required: bool = False,
) -> list[str]:
    if not isinstance(value, list):
        raise TaxApplicabilityArtifactError(f"{field} must be a list")
    if required and not value:
        raise TaxApplicabilityArtifactError(f"{field} requires at least one reference")
    if len(value) > 20 or len(value) != len(set(value)) or any(
        not isinstance(item, str) or not EVIDENCE_REFERENCE_PATTERN.fullmatch(item)
        for item in value
    ):
        raise TaxApplicabilityArtifactError(
            f"{field} must contain unique opaque evidence:// style references"
        )
    return list(value)


def _read(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    if source.is_symlink():
        raise TaxApplicabilityArtifactError(
            "tax applicability artifacts must not be symbolic links"
        )
    if not source.is_file():
        raise TaxApplicabilityArtifactError(
            f"tax applicability artifact does not exist: {source}"
        )
    size = source.stat().st_size
    if os.name != "nt" and stat.S_IMODE(source.stat().st_mode) & 0o077:
        raise TaxApplicabilityArtifactError(
            "tax applicability artifact must not be accessible by group or other users"
        )
    if size <= 0 or size > MAX_ARTIFACT_BYTES:
        raise TaxApplicabilityArtifactError(
            "tax applicability artifact must be 1 byte to 2 MiB"
        )
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise TaxApplicabilityArtifactError(
            "tax applicability artifact must be valid JSON"
        ) from exc
    if not isinstance(value, dict):
        raise TaxApplicabilityArtifactError(
            "tax applicability artifact must be a JSON object"
        )
    return value


def _write_private(path: str | Path, value: dict[str, Any]) -> Path:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(
            destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600,
        )
    except FileExistsError as exc:
        raise TaxApplicabilityArtifactError(
            "tax applicability output already exists; refusing to overwrite"
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


def _entity_template(runtime: BoxRuntime, entity_id: str) -> dict[str, Any]:
    runtime.require_entity(entity_id)
    questionnaire = build_tax_applicability_questionnaire(runtime)
    entity = next(
        item for item in questionnaire["entities"] if item["entity_id"] == entity_id
    )
    return {
        "schema_version": 1,
        "artifact_type": "tax_applicability_entity_questionnaire_template",
        "runtime_fingerprint": questionnaire["runtime_fingerprint"],
        "entity": deepcopy(entity),
        "instructions": list(questionnaire["instructions"]),
        "template_only": True,
        "raw_tax_identifiers_requested": False,
        "tax_calculation_performed": False,
        "external_actions_performed": False,
    }


def _workpaper_contract(
    runtime: BoxRuntime, entity_id: str, facts_as_of: Any,
) -> dict[str, Any]:
    template = _entity_template(runtime, entity_id)
    facts_date = _facts_date(runtime, entity_id, facts_as_of)
    entity = deepcopy(template["entity"])
    for key in (
        "questionnaire_status", "unanswered_count", "tax_applicability_determined",
    ):
        entity.pop(key, None)
    return {
        "schema_version": 1,
        "artifact_type": "tax_applicability_workpaper",
        "runtime_fingerprint": template["runtime_fingerprint"],
        "questionnaire_fingerprint": _hash(template),
        "facts_as_of": facts_date.isoformat(),
        "prepared_by": None,
        "entity": entity,
        "instructions": list(template["instructions"]),
        "template_only": False,
        "raw_tax_identifiers_included": False,
        "tax_calculation_performed": False,
        "external_actions_performed": False,
    }


def build_tax_applicability_workpaper(
    runtime: BoxRuntime, entity_id: str, *, prepared_by: str, facts_as_of: str,
) -> dict[str, Any]:
    """Create one entity-scoped, unanswered private workpaper."""
    workpaper = _workpaper_contract(runtime, entity_id, facts_as_of)
    workpaper["prepared_by"] = _actor(prepared_by, "prepared_by")
    return workpaper


def write_tax_applicability_workpaper(
    runtime: BoxRuntime,
    entity_id: str,
    *,
    prepared_by: str,
    facts_as_of: str,
    output: str | Path,
) -> dict[str, Any]:
    workpaper = build_tax_applicability_workpaper(
        runtime, entity_id, prepared_by=prepared_by, facts_as_of=facts_as_of,
    )
    destination = _write_private(output, workpaper)
    return {
        "output": str(destination),
        "runtime_fingerprint": workpaper["runtime_fingerprint"],
        "questionnaire_fingerprint": workpaper["questionnaire_fingerprint"],
        "facts_as_of": workpaper["facts_as_of"],
        "entity_id": entity_id,
        "pack_id": workpaper["entity"]["pack_id"],
        "prepared_by": workpaper["prepared_by"],
        "question_count": workpaper["entity"]["question_count"],
        "unanswered_count": workpaper["entity"]["question_count"],
        "raw_tax_identifiers_returned": False,
        "tax_calculation_performed": False,
        "external_actions_performed": False,
    }


def validate_tax_applicability_workpaper(
    runtime: BoxRuntime, value: Any,
) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("artifact_type") != "tax_applicability_workpaper":
        raise TaxApplicabilityArtifactError("tax applicability workpaper is invalid")
    entity = value.get("entity")
    if not isinstance(entity, dict):
        raise TaxApplicabilityArtifactError("tax applicability workpaper entity is invalid")
    entity_id = str(entity.get("entity_id") or "")
    expected = _workpaper_contract(runtime, entity_id, value.get("facts_as_of"))
    if set(value) != set(expected):
        raise TaxApplicabilityArtifactError("tax applicability workpaper fields are invalid")
    _actor(value.get("prepared_by"), "prepared_by")
    for key in set(expected) - {"prepared_by", "entity"}:
        if value.get(key) != expected[key]:
            raise TaxApplicabilityArtifactError(
                f"tax applicability workpaper {key} does not match the current Box"
            )
    expected_entity = expected["entity"]
    if set(entity) != set(expected_entity):
        raise TaxApplicabilityArtifactError("tax applicability entity fields are invalid")
    for key in set(expected_entity) - {"questions"}:
        if entity.get(key) != expected_entity[key]:
            raise TaxApplicabilityArtifactError(
                f"tax applicability entity {key} does not match the selected Pack"
            )
    questions = entity.get("questions")
    expected_questions = expected_entity["questions"]
    if not isinstance(questions, list) or len(questions) != len(expected_questions):
        raise TaxApplicabilityArtifactError("tax applicability questions are incomplete")
    for index, (question, expected_question) in enumerate(zip(questions, expected_questions)):
        if not isinstance(question, dict) or set(question) != set(expected_question):
            raise TaxApplicabilityArtifactError(
                f"tax applicability question {index} fields are invalid"
            )
        for key in set(expected_question) - {"answer", "evidence_references"}:
            if question.get(key) != expected_question[key]:
                raise TaxApplicabilityArtifactError(
                    f"tax applicability question {index} contract was modified"
                )
        answer = question.get("answer")
        if answer is not None and answer not in expected_question["allowed_answers"]:
            raise TaxApplicabilityArtifactError(
                f"tax applicability question {index} answer is invalid"
            )
        _references(
            question.get("evidence_references"),
            f"tax applicability question {index} evidence_references",
        )
    if (
        value.get("raw_tax_identifiers_included") is not False
        or value.get("tax_calculation_performed") is not False
        or value.get("external_actions_performed") is not False
    ):
        raise TaxApplicabilityArtifactError(
            "tax applicability workpaper must not contain raw identifiers or actions"
        )
    return deepcopy(value)


def _answer_map(workpaper: dict[str, Any]) -> dict[str, str | None]:
    return {
        item["question_id"]: item.get("answer")
        for item in workpaper["entity"]["questions"]
    }


def _validate_decision(workpaper: dict[str, Any], decision: str) -> None:
    if decision not in DECISIONS:
        raise TaxApplicabilityArtifactError("tax applicability review decision is invalid")
    answers = _answer_map(workpaper)
    questions = workpaper["entity"]["questions"]
    complete = all(item.get("answer") is not None for item in questions)
    fully_evidenced = all(item.get("evidence_references") for item in questions)
    if decision == "approved-in-scope":
        expected = {
            "legal_form_and_pack_scope": "confirmed_in_scope",
            "tax_residency_and_permanent_establishment": "confirmed_in_scope",
            "direct_and_indirect_tax_registrations": {
                "confirmed_complete", "confirmed_not_applicable",
            },
            "fiscal_year_and_return_periods": "confirmed",
            "special_cross_border_and_group_regimes": "reviewed_no_additional_scope",
        }
        approved = all(
            answers.get(key) in value if isinstance(value, set)
            else answers.get(key) == value
            for key, value in expected.items()
        )
        if not complete or not fully_evidenced or not approved:
            raise TaxApplicabilityArtifactError(
                "approved-in-scope requires complete evidence-backed in-scope answers"
            )
    elif decision == "confirmed-out-of-scope":
        outside = any(
            answers.get(key) == "confirmed_out_of_scope"
            for key in (
                "legal_form_and_pack_scope",
                "tax_residency_and_permanent_establishment",
            )
        )
        if not complete or not fully_evidenced or not outside:
            raise TaxApplicabilityArtifactError(
                "confirmed-out-of-scope requires complete evidence and an out-of-scope answer"
            )


def review_tax_applicability_workpaper(
    runtime: BoxRuntime,
    workpaper_json: str | Path,
    output: str | Path,
    *,
    decision: str,
    actor: str,
    rationale: str,
    evidence_references: Iterable[str],
) -> dict[str, Any]:
    """Independently review one exact entity workpaper into a sealed private artifact."""
    workpaper = validate_tax_applicability_workpaper(runtime, _read(workpaper_json))
    _validate_decision(workpaper, decision)
    reviewer = _actor(actor, "actor")
    if reviewer == workpaper["prepared_by"]:
        raise TaxApplicabilityArtifactError(
            "tax applicability reviewer must differ from workpaper preparer"
        )
    rationale = str(rationale or "").strip()
    if not 1 <= len(rationale) <= 1000:
        raise TaxApplicabilityArtifactError("rationale must be 1-1000 characters")
    references = _references(
        list(evidence_references), "review evidence_references", required=True,
    )
    workpaper_fingerprint = _hash(workpaper)
    facts_as_of = date.fromisoformat(workpaper["facts_as_of"])
    applicability_policy = workpaper["entity"]["applicability_review_policy"]
    expires_at = facts_as_of + timedelta(days=applicability_policy["max_age_days"])
    review_due_at = expires_at - timedelta(
        days=applicability_policy["warning_days_before_expiry"]
    )
    review_core = {
        "workpaper_fingerprint": workpaper_fingerprint,
        "decision": decision,
        "actor": reviewer,
        "rationale": rationale,
        "evidence_references": references,
        "reviewed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    review = {**review_core, "review_id": _hash(review_core)[:24]}
    questions = workpaper["entity"]["questions"]
    reviewed = {
        **workpaper,
        "artifact_type": "tax_applicability_review",
        "workpaper_fingerprint": workpaper_fingerprint,
        "review_due_at": review_due_at.isoformat(),
        "expires_at": expires_at.isoformat(),
        "unanswered_count": sum(item.get("answer") is None for item in questions),
        "applicability_gate_passed": decision == "approved-in-scope",
        "review": review,
        "review_current": True,
    }
    validate_tax_applicability_review(runtime, reviewed)
    destination = _write_private(output, reviewed)
    return {
        "output": str(destination),
        "runtime_fingerprint": workpaper["runtime_fingerprint"],
        "entity_id": workpaper["entity"]["entity_id"],
        "pack_id": workpaper["entity"]["pack_id"],
        "workpaper_fingerprint": workpaper_fingerprint,
        "facts_as_of": workpaper["facts_as_of"],
        "review_due_at": review_due_at.isoformat(),
        "expires_at": expires_at.isoformat(),
        "review_id": review["review_id"],
        "decision": decision,
        "review_current": True,
        "applicability_gate_passed": decision == "approved-in-scope",
        "raw_tax_identifiers_returned": False,
        "tax_calculation_performed": False,
        "external_actions_performed": False,
    }


def validate_tax_applicability_review(
    runtime: BoxRuntime, value: Any,
) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("artifact_type") != "tax_applicability_review":
        raise TaxApplicabilityArtifactError("tax applicability review is invalid")
    extra_fields = {
        "workpaper_fingerprint", "review_due_at", "expires_at", "unanswered_count",
        "applicability_gate_passed", "review", "review_current",
    }
    workpaper = {
        key: deepcopy(item) for key, item in value.items() if key not in extra_fields
    }
    workpaper["artifact_type"] = "tax_applicability_workpaper"
    workpaper = validate_tax_applicability_workpaper(runtime, workpaper)
    expected_fields = set(workpaper) | extra_fields
    if set(value) != expected_fields:
        raise TaxApplicabilityArtifactError("tax applicability review fields are invalid")
    workpaper_fingerprint = _hash(workpaper)
    if value.get("workpaper_fingerprint") != workpaper_fingerprint:
        raise TaxApplicabilityArtifactError("tax applicability workpaper fingerprint mismatch")
    facts_as_of = date.fromisoformat(workpaper["facts_as_of"])
    applicability_policy = workpaper["entity"]["applicability_review_policy"]
    expected_expires_at = facts_as_of + timedelta(
        days=applicability_policy["max_age_days"]
    )
    expected_review_due_at = expected_expires_at - timedelta(
        days=applicability_policy["warning_days_before_expiry"]
    )
    if value.get("review_due_at") != expected_review_due_at.isoformat():
        raise TaxApplicabilityArtifactError(
            "tax applicability review_due_at does not match Pack policy"
        )
    if value.get("expires_at") != expected_expires_at.isoformat():
        raise TaxApplicabilityArtifactError(
            "tax applicability expires_at does not match Pack policy"
        )
    questions = workpaper["entity"]["questions"]
    unanswered_count = sum(item.get("answer") is None for item in questions)
    if value.get("unanswered_count") != unanswered_count:
        raise TaxApplicabilityArtifactError("tax applicability unanswered count is invalid")
    review = value.get("review")
    if value.get("review_current") is not True or not isinstance(review, dict):
        raise TaxApplicabilityArtifactError("tax applicability review is not current")
    expected_review_fields = {
        "workpaper_fingerprint", "decision", "actor", "rationale",
        "evidence_references", "reviewed_at", "review_id",
    }
    if set(review) != expected_review_fields:
        raise TaxApplicabilityArtifactError("tax applicability review signature fields are invalid")
    if review.get("workpaper_fingerprint") != workpaper_fingerprint:
        raise TaxApplicabilityArtifactError("tax applicability review is not bound to the workpaper")
    decision = str(review.get("decision") or "")
    _validate_decision(workpaper, decision)
    reviewer = _actor(review.get("actor"), "review actor")
    if reviewer == workpaper["prepared_by"]:
        raise TaxApplicabilityArtifactError(
            "tax applicability reviewer must differ from workpaper preparer"
        )
    rationale = str(review.get("rationale") or "").strip()
    if not 1 <= len(rationale) <= 1000:
        raise TaxApplicabilityArtifactError("tax applicability review rationale is invalid")
    _references(review.get("evidence_references"), "review evidence_references", required=True)
    try:
        reviewed_at = datetime.fromisoformat(
            str(review.get("reviewed_at") or "").replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise TaxApplicabilityArtifactError("reviewed_at must use ISO-8601") from exc
    if reviewed_at.tzinfo is None:
        raise TaxApplicabilityArtifactError("reviewed_at must include timezone")
    if reviewed_at.astimezone(timezone.utc).date() < facts_as_of:
        raise TaxApplicabilityArtifactError("reviewed_at cannot predate facts_as_of")
    review_core = {key: item for key, item in review.items() if key != "review_id"}
    if review.get("review_id") != _hash(review_core)[:24]:
        raise TaxApplicabilityArtifactError("tax applicability review fingerprint mismatch")
    expected_gate = decision == "approved-in-scope"
    if value.get("applicability_gate_passed") is not expected_gate:
        raise TaxApplicabilityArtifactError("tax applicability gate status is invalid")
    return deepcopy(value)


def verify_tax_applicability_review(
    runtime: BoxRuntime, review_json: str | Path, *, as_of: str | None = None,
) -> dict[str, Any]:
    """Verify a sealed review while returning no answers, rationale, or private evidence."""
    reviewed = validate_tax_applicability_review(runtime, _read(review_json))
    return _tax_applicability_review_summary(runtime, reviewed, as_of=as_of)


def _tax_applicability_review_summary(
    runtime: BoxRuntime, reviewed: dict[str, Any], *, as_of: str | None = None,
) -> dict[str, Any]:
    review = reviewed["review"]
    entity = reviewed["entity"]
    evaluation_date = (
        _canonical_date(as_of, "as_of")
        if as_of is not None else datetime.now(timezone.utc).date()
    )
    facts_as_of = date.fromisoformat(reviewed["facts_as_of"])
    if evaluation_date < facts_as_of:
        raise TaxApplicabilityArtifactError("as_of cannot predate facts_as_of")
    review_due_at = date.fromisoformat(reviewed["review_due_at"])
    expires_at = date.fromisoformat(reviewed["expires_at"])
    if evaluation_date > expires_at:
        lifecycle_status = "expired"
    elif evaluation_date >= review_due_at:
        lifecycle_status = "review_due"
    else:
        lifecycle_status = "current"
    decision_gate_passed = reviewed["applicability_gate_passed"]
    applicability_gate_passed = decision_gate_passed and lifecycle_status != "expired"
    return {
        "valid": True,
        "runtime_fingerprint": reviewed["runtime_fingerprint"],
        "entity_id": entity["entity_id"],
        "pack_id": entity["pack_id"],
        "pack_version": entity["pack_version"],
        "facts_as_of": reviewed["facts_as_of"],
        "as_of": evaluation_date.isoformat(),
        "review_due_at": reviewed["review_due_at"],
        "expires_at": reviewed["expires_at"],
        "days_until_expiry": (expires_at - evaluation_date).days,
        "lifecycle_status": lifecycle_status,
        "questionnaire_fingerprint": reviewed["questionnaire_fingerprint"],
        "workpaper_fingerprint": reviewed["workpaper_fingerprint"],
        "review_id": review["review_id"],
        "review_actor": review["actor"],
        "reviewed_at": review["reviewed_at"],
        "decision": review["decision"],
        "review_current": True,
        "unanswered_count": reviewed["unanswered_count"],
        "decision_gate_passed": decision_gate_passed,
        "applicability_gate_passed": applicability_gate_passed,
        "answers_returned": False,
        "review_rationale_returned": False,
        "evidence_references_returned": False,
        "raw_tax_identifiers_returned": False,
        "tax_calculation_performed": False,
        "external_actions_performed": False,
    }


def import_tax_applicability_review(
    runtime: BoxRuntime,
    review_json: str | Path,
    review_dir: str | Path,
    *,
    as_of: str | None = None,
) -> dict[str, Any]:
    """Validate and exclusively install one review under its Box entity name."""
    root = Path(review_dir)
    if not root.is_absolute():
        raise TaxApplicabilityArtifactError(
            "tax applicability review directory must be an absolute path"
        )
    if root.is_symlink() or not root.is_dir():
        raise TaxApplicabilityArtifactError(
            "tax applicability review directory must be a real directory"
        )
    before = inspect_tax_applicability_review_directory(runtime, root, as_of=as_of)
    if not before["registry_clean"]:
        raise TaxApplicabilityArtifactError(
            "tax applicability review directory contains unexpected entries"
        )
    reviewed = validate_tax_applicability_review(runtime, _read(review_json))
    summary = _tax_applicability_review_summary(runtime, reviewed, as_of=as_of)
    destination = root / f"{summary['entity_id']}.json"
    _write_private(destination, reviewed)
    after = inspect_tax_applicability_review_directory(runtime, root, as_of=as_of)
    return {
        "schema_version": 1,
        "imported": True,
        "entity_id": summary["entity_id"],
        "pack_id": summary["pack_id"],
        "pack_version": summary["pack_version"],
        "review_id": summary["review_id"],
        "decision": summary["decision"],
        "lifecycle_status": summary["lifecycle_status"],
        "applicability_gate_passed": summary["applicability_gate_passed"],
        "registry_counts": after["counts"],
        "registry_clean": after["registry_clean"],
        "ready_for_calendar_release": after["ready_for_calendar_release"],
        "overwrite_performed": False,
        "paths_returned": False,
        "answers_returned": False,
        "review_rationale_returned": False,
        "evidence_references_returned": False,
        "raw_tax_identifiers_returned": False,
        "tax_calculation_performed": False,
        "external_actions_performed": False,
    }


def verify_tax_applicability_review_portfolio(
    runtime: BoxRuntime, review_paths: Iterable[str | Path], *, as_of: str | None = None,
) -> dict[str, Any]:
    """Require exactly one unexpired approved applicability review per Box entity."""
    paths = list(review_paths)
    if not paths:
        raise TaxApplicabilityArtifactError(
            "tax applicability portfolio requires at least one review"
        )
    reviews: dict[str, dict[str, Any]] = {}
    for index, path in enumerate(paths):
        try:
            summary = verify_tax_applicability_review(runtime, path, as_of=as_of)
        except (TaxApplicabilityArtifactError, OSError, ValueError) as exc:
            raise TaxApplicabilityArtifactError(
                f"tax applicability portfolio review at input index {index} is invalid"
            ) from exc
        entity_id = summary["entity_id"]
        if entity_id in reviews:
            raise TaxApplicabilityArtifactError(
                f"tax applicability portfolio has duplicate review for {entity_id}"
            )
        reviews[entity_id] = summary
    required_entity_ids = {
        item["id"] for item in runtime.snapshot()["entities"]
    }
    provided_entity_ids = set(reviews)
    missing = sorted(required_entity_ids - provided_entity_ids)
    unexpected = sorted(provided_entity_ids - required_entity_ids)
    if missing or unexpected:
        raise TaxApplicabilityArtifactError(
            "tax applicability portfolio coverage is incomplete; "
            f"missing={missing}, unexpected={unexpected}"
        )
    unapproved = sorted(
        entity_id for entity_id, summary in reviews.items()
        if summary["decision"] != "approved-in-scope"
        or not summary["review_current"]
        or summary["unanswered_count"] != 0
        or not summary["applicability_gate_passed"]
    )
    if unapproved:
        raise TaxApplicabilityArtifactError(
            f"tax applicability portfolio has unapproved entities: {unapproved}"
        )
    lifecycle_counts = {
        status: sum(
            summary["lifecycle_status"] == status for summary in reviews.values()
        )
        for status in ("current", "review_due", "expired")
    }
    return {
        "valid": True,
        "complete": True,
        "runtime_fingerprint": runtime.snapshot()["fingerprint"],
        "entity_count": len(required_entity_ids),
        "lifecycle_counts": lifecycle_counts,
        "all_current": lifecycle_counts["current"] == len(required_entity_ids),
        "calendar_release_allowed": lifecycle_counts["expired"] == 0,
        "entity_reviews": [{
            "entity_id": entity_id,
            "pack_id": reviews[entity_id]["pack_id"],
            "pack_version": reviews[entity_id]["pack_version"],
            "review_id": reviews[entity_id]["review_id"],
            "review_actor": reviews[entity_id]["review_actor"],
            "reviewed_at": reviews[entity_id]["reviewed_at"],
            "facts_as_of": reviews[entity_id]["facts_as_of"],
            "review_due_at": reviews[entity_id]["review_due_at"],
            "expires_at": reviews[entity_id]["expires_at"],
            "lifecycle_status": reviews[entity_id]["lifecycle_status"],
            "decision": reviews[entity_id]["decision"],
            "applicability_gate_passed": True,
        } for entity_id in sorted(required_entity_ids)],
        "answers_returned": False,
        "review_rationales_returned": False,
        "evidence_references_returned": False,
        "raw_tax_identifiers_returned": False,
        "tax_calculation_performed": False,
        "external_actions_performed": False,
    }


def inspect_tax_applicability_review_directory(
    runtime: BoxRuntime, review_dir: str | Path, *, as_of: str | None = None,
) -> dict[str, Any]:
    """Inspect exact entity-named private reviews without returning private content or paths."""
    root = Path(review_dir)
    if not root.is_absolute():
        raise TaxApplicabilityArtifactError(
            "tax applicability review directory must be an absolute path"
        )
    if root.is_symlink() or not root.is_dir():
        raise TaxApplicabilityArtifactError(
            "tax applicability review directory must be a real directory"
        )
    evaluation_date = (
        _canonical_date(as_of, "as_of")
        if as_of is not None else datetime.now(timezone.utc).date()
    )
    entity_ids = sorted(item["id"] for item in runtime.snapshot()["entities"])
    expected_names = {f"{entity_id}.json" for entity_id in entity_ids}
    unexpected_entries = [
        item for item in root.iterdir() if item.name not in expected_names
    ]
    entities = []
    for entity_id in entity_ids:
        review_path = root / f"{entity_id}.json"
        if not review_path.exists() and not review_path.is_symlink():
            entities.append({
                "entity_id": entity_id,
                "status": "missing",
                "lifecycle_status": None,
                "decision": None,
                "applicability_gate_passed": False,
                "facts_as_of": None,
                "review_due_at": None,
                "expires_at": None,
                "review_id": None,
            })
            continue
        try:
            summary = verify_tax_applicability_review(
                runtime, review_path, as_of=evaluation_date.isoformat(),
            )
        except (TaxApplicabilityArtifactError, OSError, ValueError) as exc:
            entities.append({
                "entity_id": entity_id,
                "status": "invalid",
                "lifecycle_status": None,
                "decision": None,
                "applicability_gate_passed": False,
                "facts_as_of": None,
                "review_due_at": None,
                "expires_at": None,
                "review_id": None,
                "error_sha256": hashlib.sha256(str(exc).encode("utf-8")).hexdigest(),
            })
            continue
        status = (
            summary["lifecycle_status"]
            if summary["decision"] == "approved-in-scope" else "not_approved"
        )
        entities.append({
            "entity_id": entity_id,
            "pack_id": summary["pack_id"],
            "pack_version": summary["pack_version"],
            "status": status,
            "lifecycle_status": summary["lifecycle_status"],
            "decision": summary["decision"],
            "applicability_gate_passed": summary["applicability_gate_passed"],
            "facts_as_of": summary["facts_as_of"],
            "review_due_at": summary["review_due_at"],
            "expires_at": summary["expires_at"],
            "review_id": summary["review_id"],
        })
    status_names = ("current", "review_due", "expired", "missing", "invalid", "not_approved")
    counts = {
        status: sum(item["status"] == status for item in entities)
        for status in status_names
    }
    ready = (
        counts["current"] + counts["review_due"] == len(entity_ids)
        and not unexpected_entries
    )
    return {
        "schema_version": 1,
        "as_of": evaluation_date.isoformat(),
        "entity_count": len(entity_ids),
        "counts": counts,
        "unexpected_entry_count": len(unexpected_entries),
        "registry_clean": not unexpected_entries,
        "ready_for_calendar_release": ready,
        "entities": entities,
        "paths_returned": False,
        "answers_returned": False,
        "review_rationales_returned": False,
        "evidence_references_returned": False,
        "raw_tax_identifiers_returned": False,
        "tax_calculation_performed": False,
        "external_actions_performed": False,
    }


_REGISTRY_REVIEW_FIELDS = (
    "entity_id", "pack_id", "pack_version", "review_id", "decision",
    "facts_as_of", "review_due_at", "expires_at",
)


def _registry_review_set(status: dict[str, Any]) -> list[dict[str, Any]]:
    entities = status.get("entities")
    if not isinstance(entities, list) or len(entities) != status.get("entity_count"):
        raise TaxApplicabilityArtifactError(
            "tax applicability review registry coverage is invalid"
        )
    review_set = []
    for item in entities:
        if (
            not isinstance(item, dict)
            or item.get("status") in {"missing", "invalid"}
            or any(item.get(key) is None for key in _REGISTRY_REVIEW_FIELDS)
        ):
            raise TaxApplicabilityArtifactError(
                "tax applicability review registry contains missing or invalid reviews"
            )
        review_set.append({key: item[key] for key in _REGISTRY_REVIEW_FIELDS})
    return sorted(review_set, key=lambda item: item["entity_id"])


def _registry_content_fingerprint(
    runtime: BoxRuntime, review_set: list[dict[str, Any]],
) -> str:
    return _hash({
        "runtime_fingerprint": runtime.snapshot()["fingerprint"],
        "review_set": review_set,
    })


def _registry_control_actors(
    runtime: BoxRuntime, review_dir: str | Path, review_set: list[dict[str, Any]],
) -> set[str]:
    root = Path(review_dir)
    actors: set[str] = set()
    for item in review_set:
        reviewed = validate_tax_applicability_review(
            runtime, _read(root / f"{item['entity_id']}.json")
        )
        actors.add(reviewed["prepared_by"])
        actors.add(reviewed["review"]["actor"])
    return actors


def write_tax_applicability_registry_receipt(
    runtime: BoxRuntime,
    review_dir: str | Path,
    output: str | Path,
    *,
    actor: str,
    as_of: str | None = None,
) -> dict[str, Any]:
    """Seal one complete approved registry without copying private review content."""
    status = inspect_tax_applicability_review_directory(runtime, review_dir, as_of=as_of)
    if not status["ready_for_calendar_release"]:
        raise TaxApplicabilityArtifactError(
            "tax applicability review registry is not ready for receipt sealing"
        )
    review_set = _registry_review_set(status)
    sealed_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )
    controller = _actor(actor, "sealed_by")
    if controller in _registry_control_actors(runtime, review_dir, review_set):
        raise TaxApplicabilityArtifactError(
            "tax applicability registry controller must differ from every preparer and reviewer"
        )
    receipt_core = {
        "schema_version": 1,
        "artifact_type": "tax_applicability_registry_receipt",
        "runtime_fingerprint": runtime.snapshot()["fingerprint"],
        "sealed_as_of": status["as_of"],
        "sealed_by": controller,
        "sealed_at": sealed_at,
        "entity_count": status["entity_count"],
        "review_set": review_set,
        "registry_content_fingerprint": _registry_content_fingerprint(
            runtime, review_set,
        ),
        "private_review_contents_included": False,
        "controller_role_separation_verified": True,
        "external_actions_performed": False,
    }
    receipt = {**receipt_core, "receipt_id": _hash(receipt_core)[:24]}
    _write_private(output, receipt)
    return {
        "schema_version": 1,
        "receipt_created": True,
        "receipt_id": receipt["receipt_id"],
        "runtime_fingerprint": receipt["runtime_fingerprint"],
        "registry_content_fingerprint": receipt["registry_content_fingerprint"],
        "sealed_as_of": receipt["sealed_as_of"],
        "sealed_by": receipt["sealed_by"],
        "sealed_at": receipt["sealed_at"],
        "entity_count": receipt["entity_count"],
        "controller_role_separation_verified": True,
        "paths_returned": False,
        "answers_returned": False,
        "review_rationales_returned": False,
        "evidence_references_returned": False,
        "raw_tax_identifiers_returned": False,
        "digital_signature_performed": False,
        "filing_authorization_granted": False,
        "external_actions_performed": False,
    }


def verify_tax_applicability_registry_receipt(
    runtime: BoxRuntime,
    review_dir: str | Path,
    receipt_json: str | Path,
    *,
    as_of: str | None = None,
) -> dict[str, Any]:
    """Verify that a private receipt still binds the exact current registry content."""
    receipt = _read(receipt_json)
    expected_fields = {
        "schema_version", "artifact_type", "runtime_fingerprint", "sealed_as_of",
        "sealed_by", "sealed_at", "entity_count", "review_set",
        "registry_content_fingerprint", "private_review_contents_included",
        "controller_role_separation_verified", "external_actions_performed",
        "receipt_id",
    }
    if set(receipt) != expected_fields:
        raise TaxApplicabilityArtifactError(
            "tax applicability registry receipt fields are invalid"
        )
    if (
        receipt.get("schema_version") != 1
        or receipt.get("artifact_type") != "tax_applicability_registry_receipt"
        or receipt.get("runtime_fingerprint") != runtime.snapshot()["fingerprint"]
        or receipt.get("private_review_contents_included") is not False
        or receipt.get("controller_role_separation_verified") is not True
        or receipt.get("external_actions_performed") is not False
    ):
        raise TaxApplicabilityArtifactError(
            "tax applicability registry receipt does not match the current Box"
        )
    _canonical_date(receipt.get("sealed_as_of"), "sealed_as_of")
    _actor(receipt.get("sealed_by"), "sealed_by")
    try:
        sealed_at = datetime.fromisoformat(
            str(receipt.get("sealed_at") or "").replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise TaxApplicabilityArtifactError(
            "tax applicability registry sealed_at must use ISO-8601"
        ) from exc
    if sealed_at.tzinfo is None:
        raise TaxApplicabilityArtifactError(
            "tax applicability registry sealed_at must include timezone"
        )
    review_set = receipt.get("review_set")
    if (
        not isinstance(review_set, list)
        or not isinstance(receipt.get("entity_count"), int)
        or not 1 <= receipt["entity_count"] <= 50
        or len(review_set) != receipt.get("entity_count")
        or any(
            not isinstance(item, dict)
            or set(item) != set(_REGISTRY_REVIEW_FIELDS)
            or any(not isinstance(item[key], str) for key in _REGISTRY_REVIEW_FIELDS)
            or not re.fullmatch(r"[a-z][a-z0-9_-]*", item["entity_id"])
            or not re.fullmatch(r"jurisdiction\.[a-z0-9_]+", item["pack_id"])
            or not re.fullmatch(r"[0-9a-f]{24}", item["review_id"])
            or item["decision"] != "approved-in-scope"
            for item in review_set
        )
        or review_set != sorted(review_set, key=lambda item: item["entity_id"])
        or len({item["entity_id"] for item in review_set}) != len(review_set)
    ):
        raise TaxApplicabilityArtifactError(
            "tax applicability registry receipt review set is invalid"
        )
    for index, item in enumerate(review_set):
        for field in ("facts_as_of", "review_due_at", "expires_at"):
            _canonical_date(item[field], f"review_set[{index}].{field}")
    expected_content_fingerprint = _registry_content_fingerprint(runtime, review_set)
    if receipt.get("registry_content_fingerprint") != expected_content_fingerprint:
        raise TaxApplicabilityArtifactError(
            "tax applicability registry receipt content fingerprint is invalid"
        )
    receipt_core = {key: value for key, value in receipt.items() if key != "receipt_id"}
    if receipt.get("receipt_id") != _hash(receipt_core)[:24]:
        raise TaxApplicabilityArtifactError(
            "tax applicability registry receipt fingerprint is invalid"
        )
    status = inspect_tax_applicability_review_directory(runtime, review_dir, as_of=as_of)
    if not status["registry_clean"]:
        raise TaxApplicabilityArtifactError(
            "tax applicability review registry no longer has a clean directory"
        )
    current_review_set = _registry_review_set(status)
    current_fingerprint = _registry_content_fingerprint(runtime, current_review_set)
    if current_fingerprint != receipt["registry_content_fingerprint"]:
        raise TaxApplicabilityArtifactError(
            "tax applicability review registry no longer matches its receipt"
        )
    if receipt["sealed_by"] in _registry_control_actors(
        runtime, review_dir, current_review_set
    ):
        raise TaxApplicabilityArtifactError(
            "tax applicability registry controller role separation no longer holds"
        )
    final_status = inspect_tax_applicability_review_directory(
        runtime, review_dir, as_of=status["as_of"],
    )
    if not final_status["registry_clean"]:
        raise TaxApplicabilityArtifactError(
            "tax applicability review registry changed during receipt verification"
        )
    final_review_set = _registry_review_set(final_status)
    final_fingerprint = _registry_content_fingerprint(runtime, final_review_set)
    if final_fingerprint != current_fingerprint:
        raise TaxApplicabilityArtifactError(
            "tax applicability review registry changed during receipt verification"
        )
    status = final_status
    return {
        "schema_version": 1,
        "valid": True,
        "registry_unchanged": True,
        "receipt_id": receipt["receipt_id"],
        "runtime_fingerprint": receipt["runtime_fingerprint"],
        "registry_content_fingerprint": current_fingerprint,
        "sealed_as_of": receipt["sealed_as_of"],
        "sealed_by": receipt["sealed_by"],
        "sealed_at": receipt["sealed_at"],
        "as_of": status["as_of"],
        "entity_count": status["entity_count"],
        "counts": status["counts"],
        "controller_role_separation_verified": True,
        "ready_for_calendar_release": status["ready_for_calendar_release"],
        "paths_returned": False,
        "answers_returned": False,
        "review_rationales_returned": False,
        "evidence_references_returned": False,
        "raw_tax_identifiers_returned": False,
        "digital_signature_verified": False,
        "filing_authorization_granted": False,
        "external_actions_performed": False,
    }


def build_tax_applicability_registry_alerts(
    runtime: BoxRuntime,
    review_dir: str | Path,
    *,
    receipt_json: str | Path | None = None,
    as_of: str | None = None,
) -> dict[str, Any]:
    """Build deterministic rotation alert candidates without sending notifications."""
    status = inspect_tax_applicability_review_directory(runtime, review_dir, as_of=as_of)
    alerts: list[dict[str, Any]] = []
    severity_by_status = {
        "review_due": "warning",
        "expired": "critical",
        "missing": "critical",
        "invalid": "critical",
        "not_approved": "critical",
    }
    for item in status["entities"]:
        entity_status = item["status"]
        if entity_status not in severity_by_status:
            continue
        alerts.append({
            "alert_id": f"tax-applicability:{item['entity_id']}:{entity_status}",
            "severity": severity_by_status[entity_status],
            "category": "entity_review_lifecycle",
            "entity_id": item["entity_id"],
            "pack_id": item.get("pack_id"),
            "status": entity_status,
            "review_due_at": item.get("review_due_at"),
            "expires_at": item.get("expires_at"),
            **({"error_sha256": item["error_sha256"]}
               if "error_sha256" in item else {}),
        })
    if not status["registry_clean"]:
        alerts.append({
            "alert_id": "tax-applicability:registry:unexpected-entries",
            "severity": "critical",
            "category": "registry_integrity",
            "unexpected_entry_count": status["unexpected_entry_count"],
            "status": "dirty",
        })
    receipt_status: dict[str, Any]
    if receipt_json is None:
        receipt_status = {
            "configured": False, "valid": False, "registry_unchanged": False,
        }
        alerts.append({
            "alert_id": "tax-applicability:registry:receipt-missing",
            "severity": "critical",
            "category": "registry_activation",
            "status": "missing",
        })
    else:
        try:
            verified = verify_tax_applicability_registry_receipt(
                runtime, review_dir, receipt_json, as_of=status["as_of"],
            )
            receipt_status = {
                "configured": True,
                "valid": verified["valid"],
                "registry_unchanged": verified["registry_unchanged"],
                "receipt_id": verified["receipt_id"],
            }
        except (TaxApplicabilityArtifactError, OSError, ValueError) as exc:
            error_sha256 = hashlib.sha256(str(exc).encode("utf-8")).hexdigest()
            receipt_status = {
                "configured": True, "valid": False, "registry_unchanged": False,
                "error_sha256": error_sha256,
            }
            alerts.append({
                "alert_id": "tax-applicability:registry:receipt-invalid",
                "severity": "critical",
                "category": "registry_activation",
                "status": "invalid",
                "error_sha256": error_sha256,
            })
    alerts.sort(key=lambda item: item["alert_id"])
    return {
        "schema_version": 1,
        "runtime_fingerprint": runtime.snapshot()["fingerprint"],
        "as_of": status["as_of"],
        "entity_count": status["entity_count"],
        "registry_counts": status["counts"],
        "receipt": receipt_status,
        "alert_count": len(alerts),
        "critical_count": sum(item["severity"] == "critical" for item in alerts),
        "warning_count": sum(item["severity"] == "warning" for item in alerts),
        "alerts": alerts,
        "ready_for_calendar_release": bool(
            status["ready_for_calendar_release"]
            and receipt_status["valid"]
            and receipt_status["registry_unchanged"]
        ),
        "notification_candidates_only": True,
        "notifications_sent": False,
        "schedule_installed": False,
        "paths_returned": False,
        "answers_returned": False,
        "review_rationales_returned": False,
        "evidence_references_returned": False,
        "raw_tax_identifiers_returned": False,
        "filing_authorization_granted": False,
        "external_actions_performed": False,
    }
