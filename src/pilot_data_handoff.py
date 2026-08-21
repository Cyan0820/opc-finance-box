from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Any, Iterable

from .box_runtime import BoxRuntime
from .pilot_readiness import (
    build_pilot_readiness_plan,
    verify_pilot_readiness_review,
)


MAX_ARTIFACT_BYTES = 2 * 1024 * 1024
ACTOR_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@:-]{1,127}$")
REFERENCE_PATTERN = re.compile(
    r"^(?:evidence|document|workpaper|registry|advisor|authority)://"
    r"[A-Za-z0-9][A-Za-z0-9._/#:-]{1,199}$"
)
FINGERPRINT_PATTERN = re.compile(r"^[0-9a-f]{64}$")
PLACEHOLDER_PREFIX = "REPLACE_WITH_"


class PilotDataHandoffError(ValueError):
    """Raised when a real-company data handoff manifest is unsafe or invalid."""


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def _actor(value: Any, field: str) -> str:
    actor = str(value or "").strip()
    if actor.startswith(PLACEHOLDER_PREFIX) or not ACTOR_PATTERN.fullmatch(actor):
        raise PilotDataHandoffError(
            f"{field} must be a 2-128 character stable actor identifier"
        )
    return actor


def _references(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise PilotDataHandoffError(f"{field} requires opaque evidence references")
    if len(value) > 20 or len(value) != len(set(value)) or any(
        not isinstance(item, str) or not REFERENCE_PATTERN.fullmatch(item)
        for item in value
    ):
        raise PilotDataHandoffError(
            f"{field} must contain unique opaque evidence:// style references"
        )
    return list(value)


def _strict_fields(value: Any, expected: set[str], label: str) -> None:
    if not isinstance(value, dict) or set(value) != expected:
        raise PilotDataHandoffError(f"{label} fields do not match the handoff contract")


def _read_private(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    if source.is_symlink():
        raise PilotDataHandoffError("pilot data handoff artifacts must not be symbolic links")
    if not source.is_file():
        raise PilotDataHandoffError("pilot data handoff artifact does not exist")
    metadata = source.stat()
    if os.name != "nt" and stat.S_IMODE(metadata.st_mode) & 0o077:
        raise PilotDataHandoffError(
            "pilot data handoff artifact must not be accessible by group or other users"
        )
    if metadata.st_size <= 0 or metadata.st_size > MAX_ARTIFACT_BYTES:
        raise PilotDataHandoffError("pilot data handoff artifact must be 1 byte to 2 MiB")
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PilotDataHandoffError("pilot data handoff artifact must be valid JSON") from exc
    if not isinstance(value, dict):
        raise PilotDataHandoffError("pilot data handoff artifact must be a JSON object")
    return value


def _write_private(path: str | Path, value: dict[str, Any]) -> Path:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise PilotDataHandoffError(
            "pilot data handoff output already exists; refusing to overwrite"
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


def build_pilot_data_handoff_plan(runtime: BoxRuntime) -> dict[str, Any]:
    readiness = build_pilot_readiness_plan(runtime)
    return {
        "schema_version": 1,
        "runtime_fingerprint": readiness["runtime_fingerprint"],
        "entity_ids": list(readiness["entity_ids"]),
        "period_format": readiness["period_format"],
        "data_domain_requirements": deepcopy(readiness["data_domain_requirements"]),
        "transfer_modes": ["local_only", "encrypted_archive", "controlled_drive"],
        "privacy_controls": ["anonymized", "access_restricted", "not_required"],
        "required_bindings": [
            "current_pilot_readiness_review_id",
            "box_runtime_fingerprint",
            "exact_legal_entity_scope",
            "exact_data_domain_scope",
            "single_shadow_close_period",
        ],
        "control_boundary": {
            "raw_files_copied_by_manifest": False,
            "source_file_names_requested": False,
            "source_paths_requested": False,
            "credentials_requested": False,
            "raw_source_identifiers_requested": False,
            "raw_tax_identifiers_requested": False,
            "financial_values_requested": False,
            "source_manifest_hashes_are_content_review": False,
            "data_import_performed": False,
            "connector_dispatched": False,
            "external_actions_authorized": False,
        },
    }


def _domain_starter(requirement: dict[str, Any], entity_id: str) -> dict[str, Any]:
    return {
        **deepcopy(requirement),
        "mapped_entity_id": entity_id,
        "status": "pending",
        "transfer_mode": "pending",
        "source_file_count": 0,
        "source_manifest_sha256": None,
        "period_coverage": [],
        "contains_personal_data": "unknown",
        "privacy_control": "pending",
        "source_owner": "REPLACE_WITH_SOURCE_OWNER",
        "access_approved_by": "REPLACE_WITH_ACCESS_APPROVER",
        "evidence_references": [],
    }


def build_pilot_data_handoff_workpaper(
    runtime: BoxRuntime, pilot_readiness_review: str | Path, *,
    prepared_by: str, custodian_principal: str, as_of: str | None = None,
) -> dict[str, Any]:
    readiness = verify_pilot_readiness_review(
        runtime, pilot_readiness_review, as_of=as_of,
    )
    if not readiness["ready_for_bounded_shadow"]:
        raise PilotDataHandoffError(
            "current pilot readiness review does not allow a bounded Shadow Close"
        )
    prepared = _actor(prepared_by, "prepared_by")
    custodian = _actor(custodian_principal, "custodian_principal")
    if prepared == custodian:
        raise PilotDataHandoffError("handoff preparer must differ from the data custodian")
    plan = build_pilot_data_handoff_plan(runtime)
    return {
        "schema_version": 1,
        "artifact_type": "pilot_data_handoff_workpaper",
        "runtime_fingerprint": plan["runtime_fingerprint"],
        "plan_fingerprint": _hash(plan),
        "pilot_readiness_review_id": readiness["review_id"],
        "period": readiness["period"],
        "prepared_by": prepared,
        "custodian_principal": custodian,
        "entities": [{
            "entity_id": entity_id,
            "data_domains": [
                _domain_starter(item, entity_id)
                for item in plan["data_domain_requirements"]
            ],
        } for entity_id in plan["entity_ids"]],
        "template_only": True,
        "contains_credentials": False,
        "contains_source_file_names_or_paths": False,
        "contains_raw_source_identifiers": False,
        "contains_raw_tax_identifiers": False,
        "contains_financial_values": False,
        "raw_files_copied": False,
        "data_import_performed": False,
        "external_actions_authorized": False,
    }


def write_pilot_data_handoff_workpaper(
    runtime: BoxRuntime, pilot_readiness_review: str | Path, output: str | Path,
    *, prepared_by: str, custodian_principal: str, as_of: str | None = None,
) -> dict[str, Any]:
    workpaper = build_pilot_data_handoff_workpaper(
        runtime, pilot_readiness_review,
        prepared_by=prepared_by, custodian_principal=custodian_principal,
        as_of=as_of,
    )
    target = _write_private(output, workpaper)
    return {
        "artifact_type": workpaper["artifact_type"],
        "runtime_fingerprint": workpaper["runtime_fingerprint"],
        "period": workpaper["period"],
        "pilot_readiness_review_id": workpaper["pilot_readiness_review_id"],
        "entity_count": len(workpaper["entities"]),
        "data_domain_count": sum(
            len(item["data_domains"]) for item in workpaper["entities"]
        ),
        "template_only": True,
        "file_mode": "0600" if os.name != "nt" else "private_acl_review_required",
        "output_written": target.is_file(),
        "source_file_names_or_paths_returned": False,
        "raw_files_copied": False,
        "credentials_returned": False,
        "financial_values_returned": False,
        "external_actions_performed": False,
    }


def _validate_complete_workpaper(
    runtime: BoxRuntime, value: dict[str, Any], *, require_template_only: bool,
) -> tuple[dict[str, Any], str, str]:
    plan = build_pilot_data_handoff_plan(runtime)
    expected_top = {
        "schema_version", "artifact_type", "runtime_fingerprint", "plan_fingerprint",
        "pilot_readiness_review_id", "period", "prepared_by", "custodian_principal",
        "entities", "template_only", "contains_credentials",
        "contains_source_file_names_or_paths", "contains_raw_source_identifiers",
        "contains_raw_tax_identifiers", "contains_financial_values", "raw_files_copied",
        "data_import_performed", "external_actions_authorized",
    }
    _strict_fields(value, expected_top, "pilot data handoff workpaper")
    expected_type = (
        "pilot_data_handoff_workpaper" if require_template_only
        else "pilot_data_handoff_review"
    )
    if value.get("schema_version") != 1 or value.get("artifact_type") != expected_type:
        raise PilotDataHandoffError("pilot data handoff artifact type is invalid")
    if value.get("runtime_fingerprint") != plan["runtime_fingerprint"]:
        raise PilotDataHandoffError("pilot data handoff is not bound to the current Box")
    if value.get("plan_fingerprint") != _hash(plan):
        raise PilotDataHandoffError("pilot data handoff plan fingerprint changed")
    review_id = str(value.get("pilot_readiness_review_id") or "")
    if not re.fullmatch(r"[0-9a-f]{24}", review_id):
        raise PilotDataHandoffError("pilot readiness review binding is invalid")
    period = str(value.get("period") or "")
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", period):
        raise PilotDataHandoffError("pilot data handoff period must use YYYY-MM")
    prepared = _actor(value.get("prepared_by"), "prepared_by")
    custodian = _actor(value.get("custodian_principal"), "custodian_principal")
    if prepared == custodian:
        raise PilotDataHandoffError("handoff preparer must differ from the data custodian")
    for field in (
        "contains_credentials", "contains_source_file_names_or_paths",
        "contains_raw_source_identifiers", "contains_raw_tax_identifiers",
        "contains_financial_values", "raw_files_copied", "data_import_performed",
        "external_actions_authorized",
    ):
        if value.get(field) is not False:
            raise PilotDataHandoffError(f"{field} must remain false")
    if value.get("template_only") is not require_template_only:
        raise PilotDataHandoffError("pilot data handoff template_only state is invalid")

    entities = value.get("entities")
    if not isinstance(entities, list) or [
        item.get("entity_id") for item in entities if isinstance(item, dict)
    ] != plan["entity_ids"]:
        raise PilotDataHandoffError("handoff entities must exactly match the current Box")
    requirements = {item["domain"]: item for item in plan["data_domain_requirements"]}
    domain_fields = {
        "domain", "display_name", "required", "not_applicable_allowed",
        "mapped_entity_id", "status", "transfer_mode", "source_file_count",
        "source_manifest_sha256", "period_coverage", "contains_personal_data",
        "privacy_control", "source_owner", "access_approved_by",
        "evidence_references",
    }
    for entity in entities:
        _strict_fields(entity, {"entity_id", "data_domains"}, "handoff entity")
        domains = entity.get("data_domains")
        if not isinstance(domains, list) or [
            item.get("domain") for item in domains if isinstance(item, dict)
        ] != list(requirements):
            raise PilotDataHandoffError(
                "handoff data domains must exactly match current Box requirements"
            )
        for domain in domains:
            _strict_fields(domain, domain_fields, "handoff data domain")
            requirement = requirements[domain["domain"]]
            for field in ("display_name", "required", "not_applicable_allowed"):
                if domain.get(field) != requirement[field]:
                    raise PilotDataHandoffError("handoff domain requirement was modified")
            if domain.get("mapped_entity_id") != entity["entity_id"]:
                raise PilotDataHandoffError("handoff data domain has a cross-entity mapping")
            status_value = domain.get("status")
            if status_value not in {"delivered", "not_applicable"}:
                raise PilotDataHandoffError("every handoff domain requires a final disposition")
            if status_value == "not_applicable":
                if not requirement["not_applicable_allowed"]:
                    raise PilotDataHandoffError(
                        f"{domain['domain']} cannot be declared not applicable"
                    )
                if any((
                    domain.get("transfer_mode") != "not_applicable",
                    domain.get("source_file_count") != 0,
                    domain.get("source_manifest_sha256") is not None,
                    domain.get("privacy_control") != "not_applicable",
                )):
                    raise PilotDataHandoffError(
                        "not-applicable handoff domains cannot claim transferred source files"
                    )
            else:
                if domain.get("transfer_mode") not in plan["transfer_modes"]:
                    raise PilotDataHandoffError("delivered domains require an approved transfer mode")
                count = domain.get("source_file_count")
                if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= 10000:
                    raise PilotDataHandoffError("source_file_count must be 1-10000")
                if not isinstance(domain.get("source_manifest_sha256"), str) or not FINGERPRINT_PATTERN.fullmatch(domain["source_manifest_sha256"]):
                    raise PilotDataHandoffError("delivered domains require a SHA-256 source manifest fingerprint")
                if domain.get("contains_personal_data") not in {"yes", "no"}:
                    raise PilotDataHandoffError("personal-data classification must be yes or no")
                if domain.get("privacy_control") not in plan["privacy_controls"]:
                    raise PilotDataHandoffError("delivered domains require a privacy control")
                if domain["contains_personal_data"] == "yes" and domain["privacy_control"] == "not_required":
                    raise PilotDataHandoffError("personal data requires anonymization or restricted access")
            if domain.get("period_coverage") != [period]:
                raise PilotDataHandoffError("handoff period coverage must exactly match the pilot period")
            source_owner = _actor(domain.get("source_owner"), "source_owner")
            approver = _actor(domain.get("access_approved_by"), "access_approved_by")
            if source_owner == custodian or approver in {custodian, source_owner}:
                raise PilotDataHandoffError(
                    "source owner, access approver and data custodian must be separated"
                )
            _references(domain.get("evidence_references"), "handoff domain evidence")
    return plan, prepared, custodian


def review_pilot_data_handoff_workpaper(
    runtime: BoxRuntime, workpaper_json: str | Path,
    pilot_readiness_review: str | Path, output: str | Path, *, actor: str,
    rationale: str, evidence_references: Iterable[str], as_of: str | None = None,
) -> dict[str, Any]:
    readiness = verify_pilot_readiness_review(
        runtime, pilot_readiness_review, as_of=as_of,
    )
    if not readiness["ready_for_bounded_shadow"]:
        raise PilotDataHandoffError("pilot readiness review is expired")
    value = _read_private(workpaper_json)
    plan, prepared, custodian = _validate_complete_workpaper(
        runtime, value, require_template_only=True,
    )
    if value["pilot_readiness_review_id"] != readiness["review_id"]:
        raise PilotDataHandoffError("handoff is bound to a different pilot readiness review")
    reviewer = _actor(actor, "pilot data handoff reviewer")
    if reviewer in {prepared, custodian}:
        raise PilotDataHandoffError(
            "handoff reviewer must differ from preparer and data custodian"
        )
    rationale_value = str(rationale or "").strip()
    if not 12 <= len(rationale_value) <= 1000:
        raise PilotDataHandoffError("handoff review rationale must be 12-1000 characters")
    references = _references(list(evidence_references), "handoff review evidence")
    reviewed = deepcopy(value)
    reviewed["artifact_type"] = "pilot_data_handoff_review"
    reviewed["template_only"] = False
    workpaper_fingerprint = _hash(value)
    reviewed_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    reviewed["review"] = {
        "decision": "approved_for_controlled_data_intake",
        "actor": reviewer,
        "rationale": rationale_value,
        "evidence_references": references,
        "reviewed_at": reviewed_at,
        "workpaper_fingerprint": workpaper_fingerprint,
        "review_id": _hash({
            "runtime_fingerprint": plan["runtime_fingerprint"],
            "pilot_readiness_review_id": readiness["review_id"],
            "workpaper_fingerprint": workpaper_fingerprint,
            "actor": reviewer,
            "reviewed_at": reviewed_at,
        })[:24],
    }
    target = _write_private(output, reviewed)
    return {
        "artifact_type": reviewed["artifact_type"],
        "runtime_fingerprint": reviewed["runtime_fingerprint"],
        "pilot_readiness_review_id": readiness["review_id"],
        "period": reviewed["period"],
        "review_id": reviewed["review"]["review_id"],
        "entity_count": len(reviewed["entities"]),
        "data_domain_count": sum(len(item["data_domains"]) for item in reviewed["entities"]),
        "ready_for_controlled_data_intake": True,
        "ready_for_bounded_shadow": True,
        "file_mode": "0600" if os.name != "nt" else "private_acl_review_required",
        "output_written": target.is_file(),
        "source_file_names_or_paths_returned": False,
        "actors_returned": False,
        "evidence_references_returned": False,
        "credentials_returned": False,
        "financial_values_returned": False,
        "data_import_performed": False,
        "external_actions_performed": False,
    }


def verify_pilot_data_handoff_review(
    runtime: BoxRuntime, review_json: str | Path,
    pilot_readiness_review: str | Path, *, as_of: str | None = None,
) -> dict[str, Any]:
    readiness = verify_pilot_readiness_review(
        runtime, pilot_readiness_review, as_of=as_of,
    )
    value = _read_private(review_json)
    review = value.pop("review", None)
    try:
        if value.get("artifact_type") != "pilot_data_handoff_review":
            raise PilotDataHandoffError("pilot data handoff review artifact type is invalid")
        workpaper = deepcopy(value)
        workpaper["artifact_type"] = "pilot_data_handoff_workpaper"
        workpaper["template_only"] = True
        plan, prepared, custodian = _validate_complete_workpaper(
            runtime, workpaper, require_template_only=True,
        )
    finally:
        if isinstance(review, dict):
            value["review"] = review
    if workpaper["pilot_readiness_review_id"] != readiness["review_id"]:
        raise PilotDataHandoffError("handoff review is not bound to this pilot readiness review")
    expected_review_fields = {
        "decision", "actor", "rationale", "evidence_references", "reviewed_at",
        "workpaper_fingerprint", "review_id",
    }
    _strict_fields(review, expected_review_fields, "pilot data handoff review")
    reviewer = _actor(review.get("actor"), "pilot data handoff reviewer")
    if reviewer in {prepared, custodian}:
        raise PilotDataHandoffError(
            "handoff reviewer must differ from preparer and data custodian"
        )
    if review.get("decision") != "approved_for_controlled_data_intake":
        raise PilotDataHandoffError("pilot data handoff is not approved")
    rationale = str(review.get("rationale") or "").strip()
    if not 12 <= len(rationale) <= 1000:
        raise PilotDataHandoffError("handoff review rationale must be 12-1000 characters")
    _references(review.get("evidence_references"), "handoff review evidence")
    try:
        reviewed_at = datetime.fromisoformat(
            str(review.get("reviewed_at") or "").replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise PilotDataHandoffError("handoff reviewed_at must be an ISO date-time") from exc
    if reviewed_at.tzinfo is None:
        raise PilotDataHandoffError("handoff reviewed_at must include a timezone")
    if review.get("workpaper_fingerprint") != _hash(workpaper):
        raise PilotDataHandoffError("handoff review is not bound to the current workpaper")
    expected_review_id = _hash({
        "runtime_fingerprint": plan["runtime_fingerprint"],
        "pilot_readiness_review_id": readiness["review_id"],
        "workpaper_fingerprint": review["workpaper_fingerprint"],
        "actor": reviewer,
        "reviewed_at": review["reviewed_at"],
    })[:24]
    if review.get("review_id") != expected_review_id:
        raise PilotDataHandoffError("pilot data handoff review_id is invalid")
    ready = bool(readiness["ready_for_bounded_shadow"])
    return {
        "schema_version": 1,
        "valid": True,
        "runtime_fingerprint": plan["runtime_fingerprint"],
        "period": workpaper["period"],
        "pilot_readiness_review_id": readiness["review_id"],
        "pilot_readiness_lifecycle_status": readiness["lifecycle_status"],
        "pilot_readiness_expires_at": readiness["expires_at"],
        "review_id": review["review_id"],
        "entity_count": len(workpaper["entities"]),
        "data_domain_count": sum(len(item["data_domains"]) for item in workpaper["entities"]),
        "delivered_domain_count": sum(
            domain["status"] == "delivered"
            for entity in workpaper["entities"] for domain in entity["data_domains"]
        ),
        "not_applicable_domain_count": sum(
            domain["status"] == "not_applicable"
            for entity in workpaper["entities"] for domain in entity["data_domains"]
        ),
        "ready_for_controlled_data_intake": ready,
        "ready_for_bounded_shadow": ready,
        "ready_for_statutory_release": False,
        "ready_for_external_filing": False,
        "source_file_names_or_paths_returned": False,
        "source_manifest_hash_values_returned": False,
        "actors_returned": False,
        "evidence_references_returned": False,
        "credentials_returned": False,
        "raw_source_identifiers_returned": False,
        "raw_tax_identifiers_returned": False,
        "financial_values_returned": False,
        "data_import_performed": False,
        "external_actions_authorized": False,
        "external_actions_performed": False,
    }


def build_pilot_data_handoff_status(
    runtime: BoxRuntime, review_json: str | Path | None = None,
    pilot_readiness_review: str | Path | None = None, *,
    as_of: str | None = None,
) -> dict[str, Any]:
    fingerprint = runtime.snapshot()["fingerprint"]
    if review_json is None:
        return {
            "schema_version": 1,
            "runtime_fingerprint": fingerprint,
            "configured": False,
            "pilot_readiness_configured": pilot_readiness_review is not None,
            "valid": False,
            "status": "missing",
            "ready_for_controlled_data_intake": False,
            "ready_for_bounded_shadow": False,
            "paths_returned": False,
            "source_manifest_hash_values_returned": False,
            "actors_returned": False,
            "evidence_references_returned": False,
            "credentials_returned": False,
            "financial_values_returned": False,
            "external_actions_performed": False,
        }
    if pilot_readiness_review is None:
        message = "pilot readiness review is required with a data handoff review"
        return {
            "schema_version": 1,
            "runtime_fingerprint": fingerprint,
            "configured": True,
            "pilot_readiness_configured": False,
            "valid": False,
            "status": "invalid",
            "error_sha256": hashlib.sha256(message.encode("utf-8")).hexdigest(),
            "ready_for_controlled_data_intake": False,
            "ready_for_bounded_shadow": False,
            "paths_returned": False,
            "source_manifest_hash_values_returned": False,
            "actors_returned": False,
            "evidence_references_returned": False,
            "credentials_returned": False,
            "financial_values_returned": False,
            "external_actions_performed": False,
        }
    try:
        verified = verify_pilot_data_handoff_review(
            runtime, review_json, pilot_readiness_review, as_of=as_of,
        )
    except (PilotDataHandoffError, OSError, ValueError) as exc:
        return {
            "schema_version": 1,
            "runtime_fingerprint": fingerprint,
            "configured": True,
            "pilot_readiness_configured": True,
            "valid": False,
            "status": "invalid",
            "error_sha256": hashlib.sha256(str(exc).encode("utf-8")).hexdigest(),
            "ready_for_controlled_data_intake": False,
            "ready_for_bounded_shadow": False,
            "paths_returned": False,
            "source_manifest_hash_values_returned": False,
            "actors_returned": False,
            "evidence_references_returned": False,
            "credentials_returned": False,
            "financial_values_returned": False,
            "external_actions_performed": False,
        }
    lifecycle = verified["pilot_readiness_lifecycle_status"]
    status = (
        "pilot_readiness_expired"
        if not verified["ready_for_controlled_data_intake"]
        else "review_due" if lifecycle == "review_due" else "current"
    )
    return {
        **verified,
        "configured": True,
        "pilot_readiness_configured": True,
        "status": status,
        "paths_returned": False,
    }


def build_pilot_data_handoff_workspace(
    runtime: BoxRuntime, review_json: str | Path | None = None,
    pilot_readiness_review: str | Path | None = None, *,
    as_of: str | None = None,
) -> dict[str, Any]:
    plan = build_pilot_data_handoff_plan(runtime)
    activation = build_pilot_data_handoff_status(
        runtime, review_json, pilot_readiness_review, as_of=as_of,
    )
    return {
        "schema_version": 1,
        "runtime_fingerprint": plan["runtime_fingerprint"],
        "summary": {
            "entity_count": len(plan["entity_ids"]),
            "data_domain_count_per_entity": len(plan["data_domain_requirements"]),
            "total_data_domain_count": (
                len(plan["entity_ids"]) * len(plan["data_domain_requirements"])
            ),
            "activation_status": activation["status"],
            "ready_for_controlled_data_intake": activation[
                "ready_for_controlled_data_intake"
            ],
            "ready_for_bounded_shadow": activation["ready_for_bounded_shadow"],
        },
        "entities": [{
            "entity_id": entity_id,
            "required_domains": deepcopy(plan["data_domain_requirements"]),
        } for entity_id in plan["entity_ids"]],
        "transfer_modes": list(plan["transfer_modes"]),
        "privacy_controls": list(plan["privacy_controls"]),
        "activation": activation,
        "control_boundary": {
            **deepcopy(plan["control_boundary"]),
            "ready_for_statutory_release": False,
            "ready_for_external_filing": False,
            "source_manifest_hash_values_returned": False,
            "actors_returned": False,
            "evidence_references_returned": False,
        },
    }
