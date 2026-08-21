from __future__ import annotations

from datetime import date
import hashlib
from pathlib import Path
from typing import Any

from .box_runtime import BoxRuntime
from .pack_services import PackServiceError, PackServiceRegistry
from .tax_applicability_artifacts import (
    TaxApplicabilityArtifactError,
    inspect_tax_applicability_review_directory,
    verify_tax_applicability_registry_receipt,
)
from .tax_pack_lifecycle import evaluate_tax_rule_lifecycle


_EDITABLE_ANCHOR_SCHEDULES = {
    "annual_fixed_after_date", "days_after_date", "months_after_date",
}
_IMPLICIT_ENTITY_ANCHORS = {"financial_year_end"}
_MAX_PREVIEW_ENTITIES = 50
_MAX_ANCHORS_PER_ENTITY = 20


def _as_of(value: str | None) -> str:
    candidate = str(value or date.today().isoformat()).strip()
    try:
        return date.fromisoformat(candidate).isoformat()
    except ValueError as exc:
        raise ValueError("as_of must use YYYY-MM-DD") from exc


def _period_year(value: int | str | None, as_of: str) -> int:
    if value is None or value == "":
        return date.fromisoformat(as_of).year
    try:
        year = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("period_year must be a four-digit year") from exc
    if not 1900 <= year <= 9999:
        raise ValueError("period_year must be a four-digit year")
    return year


def _registration_posture(registrations: list[str]) -> dict[str, Any]:
    normalized = [str(item).strip() for item in registrations if str(item).strip()]
    requires_review = [
        item for item in normalized
        if any(marker in item.lower() for marker in ("review_required", "pending", "unknown"))
    ]
    if not normalized:
        status = "not_configured"
    elif requires_review:
        status = "needs_confirmation"
    else:
        status = "configured_requires_evidence"
    return {
        "status": status,
        "configured_codes": normalized,
        "review_required_codes": requires_review,
        "configuration_is_evidence_confirmation": False,
    }


def _service_for(
    catalog: list[dict[str, Any]], pack_id: str, suffixes: tuple[str, ...],
) -> dict[str, Any] | None:
    return next((
        item for item in catalog
        if item["pack_id"] == pack_id
        and any(str(item["capability"]).endswith(suffix) for suffix in suffixes)
    ), None)


def _project_calendar(output: dict[str, Any]) -> dict[str, Any]:
    tasks = []
    for task in output.get("tasks") or []:
        tasks.append({
            "task_id": task.get("task_id"),
            "rule_id": task.get("rule_id"),
            "summary": task.get("summary"),
            "status": task.get("status"),
            "applicability": task.get("applicability"),
            "anchor": task.get("anchor"),
            "anchor_date": task.get("anchor_date"),
            "candidate_due_date": task.get("candidate_due_date"),
            "missing_configuration": list(task.get("missing_configuration") or []),
            "review_gate": task.get("review_gate"),
            "candidate_only": task.get("candidate_only") is True,
            "filing_completed": task.get("filing_completed") is True,
            "official_sources": list(task.get("official_sources") or []),
        })
    determinations = {
        key: bool(value)
        for key, value in output.items()
        if key.endswith("_determined")
        or key.endswith("_classification_performed")
    }
    return {
        "ready": output.get("ready") is True,
        "task_count": len(tasks),
        "tasks": tasks,
        "warnings": list(output.get("warnings") or []),
        "blockers": list(output.get("blockers") or []),
        "source_freshness": output.get("source_freshness"),
        "tax_calculation_performed": bool(
            output.get("tax_calculation_performed")
            or output.get("corporation_tax_calculated")
        ),
        "filing_performed": bool(output.get("filing_performed")),
        "payment_performed": bool(output.get("payment_performed")),
        "external_submission_enabled": bool(output.get("external_submission_enabled")),
        "determinations": determinations,
    }


def _anchor_contracts(rules: dict[str, Any]) -> list[dict[str, Any]]:
    """Derive date-only preview inputs from the selected, versioned rule bundle."""
    contracts: dict[str, dict[str, Any]] = {}
    for rule in rules.get("rules") or []:
        schedule = rule.get("schedule") or {}
        kind = schedule.get("kind")
        anchor = str(schedule.get("anchor") or "").strip()
        if kind not in _EDITABLE_ANCHOR_SCHEDULES or not anchor:
            continue
        implicit = anchor in _IMPLICIT_ENTITY_ANCHORS
        item = contracts.setdefault(anchor, {
            "anchor": anchor,
            "input_type": "date",
            "editable": not implicit,
            "implicit_from_entity": implicit,
            "candidate_only": True,
            "evidence_reference_required_for_release": True,
            "rule_ids": [],
            "schedule_kinds": [],
        })
        item["rule_ids"].append(rule.get("id"))
        if kind not in item["schedule_kinds"]:
            item["schedule_kinds"].append(kind)
    return [contracts[key] for key in sorted(contracts)]


def _normalize_preview_anchors(
    anchors: dict[str, Any] | None,
    contracts_by_entity: dict[str, list[dict[str, Any]]],
) -> dict[str, dict[str, str]]:
    if anchors is None:
        return {}
    if not isinstance(anchors, dict):
        raise ValueError("anchors must be an object keyed by entity_id")
    if len(anchors) > _MAX_PREVIEW_ENTITIES:
        raise ValueError("anchors contains too many legal entities")

    normalized: dict[str, dict[str, str]] = {}
    for raw_entity_id, raw_values in anchors.items():
        entity_id = str(raw_entity_id).strip()
        if entity_id not in contracts_by_entity:
            raise ValueError(f"anchors contains unknown entity_id: {entity_id}")
        if not isinstance(raw_values, dict):
            raise ValueError(f"anchors.{entity_id} must be an object")
        if len(raw_values) > _MAX_ANCHORS_PER_ENTITY:
            raise ValueError(f"anchors.{entity_id} contains too many values")
        editable = {
            item["anchor"] for item in contracts_by_entity[entity_id]
            if item["editable"]
        }
        entity_values: dict[str, str] = {}
        for raw_name, raw_value in raw_values.items():
            name = str(raw_name).strip()
            if name not in editable:
                raise ValueError(
                    f"anchors.{entity_id}.{name} is not an editable rule anchor"
                )
            if not isinstance(raw_value, str):
                raise ValueError(
                    f"anchors.{entity_id}.{name} must be one YYYY-MM-DD date"
                )
            try:
                value = date.fromisoformat(raw_value.strip()).isoformat()
            except ValueError as exc:
                raise ValueError(
                    f"anchors.{entity_id}.{name} must use YYYY-MM-DD"
                ) from exc
            entity_values[name] = value
        if entity_values:
            normalized[entity_id] = entity_values
    return normalized


def build_tax_workspace(
    runtime: BoxRuntime,
    services: PackServiceRegistry,
    *,
    period_year: int | str | None = None,
    as_of: str | None = None,
    anchors: dict[str, Any] | None = None,
    applicability_review_dir: str | Path | None = None,
    applicability_registry_receipt: str | Path | None = None,
) -> dict[str, Any]:
    """Build a statutory, source-backed tax workspace without accepting identifiers or evidence."""
    effective_as_of = _as_of(as_of)
    target_year = _period_year(period_year, effective_as_of)
    runtime.reload()
    if applicability_registry_receipt is not None and applicability_review_dir is None:
        raise ValueError(
            "tax applicability registry receipt requires a configured review directory"
        )
    applicability_registry = (
        inspect_tax_applicability_review_directory(
            runtime, applicability_review_dir, as_of=effective_as_of,
        )
        if applicability_review_dir is not None else None
    )
    applicability_activation = None
    if (
        applicability_review_dir is not None
        and applicability_registry_receipt is not None
    ):
        try:
            applicability_activation = verify_tax_applicability_registry_receipt(
                runtime,
                applicability_review_dir,
                applicability_registry_receipt,
                as_of=effective_as_of,
            )
        except (TaxApplicabilityArtifactError, OSError, ValueError) as exc:
            applicability_activation = {
                "valid": False,
                "registry_unchanged": False,
                "ready_for_calendar_release": False,
                "error_sha256": hashlib.sha256(
                    str(exc).encode("utf-8")
                ).hexdigest(),
            }
    applicability_by_entity = {
        item["entity_id"]: item
        for item in (applicability_registry or {}).get("entities", [])
    }
    rule_lifecycle = evaluate_tax_rule_lifecycle(runtime, as_of=effective_as_of)
    lifecycle_by_entity = {
        item["entity_id"]: item for item in rule_lifecycle["entities"]
    }
    entity_bundles = {
        entity.entity_id: runtime.tax_rules(entity.entity_id)
        for entity in runtime.entities.all()
    }
    contracts_by_entity = {
        entity_id: _anchor_contracts(bundle["rules"])
        for entity_id, bundle in entity_bundles.items()
    }
    preview_anchors = _normalize_preview_anchors(anchors, contracts_by_entity)
    service_catalog = services.catalog(runtime)
    entities = []
    for entity in runtime.entities.all():
        bundle = entity_bundles[entity.entity_id]
        pack_id = bundle["pack_id"]
        registration_service = _service_for(
            service_catalog, pack_id, ("registration_profile",),
        )
        evidence_service = _service_for(
            service_catalog, pack_id, ("evidence_checklist",),
        )
        calendar_service = _service_for(
            service_catalog, pack_id, ("filing_calendar", "review_calendar_skeleton"),
        )
        calendar = None
        calendar_error = None
        if calendar_service:
            try:
                calendar_payload: dict[str, Any] = {
                    "period_year": target_year, "as_of": effective_as_of,
                }
                entity_anchors = preview_anchors.get(entity.entity_id, {})
                if entity_anchors:
                    calendar_payload["anchors"] = entity_anchors
                result = services.dispatch(
                    runtime,
                    calendar_service["service_id"],
                    calendar_payload,
                    entity_id=entity.entity_id,
                )
                calendar = _project_calendar(result["output"])
            except (PackServiceError, ValueError) as exc:
                calendar_error = str(exc)

        rules = bundle["rules"]
        applicability_policy = rules["applicability_review_policy"]
        applicability_review = {
            "status": "not_attached",
            "decision": None,
            "applicability_gate_passed": False,
            "calendar_release_allowed": False,
            "facts_as_of": None,
            "review_due_at": None,
            "expires_at": None,
            "review_id": None,
            "answers_returned": False,
            "review_rationale_returned": False,
            "evidence_references_returned": False,
        }
        registry_review = applicability_by_entity.get(entity.entity_id)
        if registry_review is not None:
            applicability_review.update({
                key: value for key, value in registry_review.items()
                if key not in {"entity_id", "pack_id", "pack_version"}
            })
            if applicability_review["status"] == "missing":
                applicability_review["status"] = "not_attached"
            applicability_review["calendar_release_allowed"] = (
                applicability_review["applicability_gate_passed"]
                and bool(applicability_registry["registry_clean"])
                and bool(applicability_activation)
                and bool(applicability_activation["registry_unchanged"])
                and bool(applicability_activation["ready_for_calendar_release"])
            )
        rule_status = lifecycle_by_entity[entity.entity_id]
        calendar_release_ready = (
            rule_status["calendar_release_allowed"]
            and applicability_review["calendar_release_allowed"]
        )
        entities.append({
            "entity": entity.to_dict(),
            "tax_pack": {
                "pack_id": pack_id,
                "version": bundle["pack_version"],
                "status": bundle["pack_status"],
                "tax_readiness": (bundle.get("jurisdiction") or {}).get("tax_readiness"),
                "rules_effective_at": (bundle.get("jurisdiction") or {}).get(
                    "rules_effective_at"
                ),
                "rules_verified_at": rules.get("verified_at"),
                "authority_scope": (bundle.get("jurisdiction") or {}).get("authority_scope"),
                "scope_note": rules.get("scope_note"),
            },
            "rule_lifecycle": rule_status,
            "applicability_review_requirement": {
                "policy": applicability_policy,
                "review": applicability_review,
                "workpaper_command": (
                    "opc-finance-box tax-applicability-init <box-config.json> "
                    f"--entity {entity.entity_id} --prepared-by <actor> "
                    f"--facts-as-of {effective_as_of} "
                    f"--output <{entity.entity_id}-tax-applicability-workpaper.json>"
                ),
                "review_command": (
                    "opc-finance-box tax-applicability-review <box-config.json> "
                    f"<{entity.entity_id}-tax-applicability-workpaper.json> "
                    "--decision approved-in-scope --actor <independent-local-tax-reviewer> "
                    "--rationale <review-rationale> "
                    "--evidence-reference <evidence://reference> "
                    f"--output <{entity.entity_id}.json>"
                ),
                "verify_command": (
                    "opc-finance-box tax-applicability-verify <box-config.json> "
                    f"<{entity.entity_id}.json> --as-of {effective_as_of}"
                ),
                "review_directory_configured": applicability_registry is not None,
                "private_artifact_contents_returned": False,
            },
            "calendar_release_ready": calendar_release_ready,
            "registration_posture": _registration_posture(
                list(entity.tax_registrations),
            ),
            "anchor_contracts": contracts_by_entity[entity.entity_id],
            "provided_anchors": dict(preview_anchors.get(entity.entity_id, {})),
            "services": {
                "registration_profile": registration_service,
                "evidence_checklist": evidence_service,
                "calendar": calendar_service,
                "dispatch_endpoint": "/api/box/services/dispatch",
                "request_templates": {
                    "registration_profile": ({
                        "service_id": registration_service["service_id"],
                        "entity_id": entity.entity_id,
                        "payload": {"as_of": effective_as_of},
                    } if registration_service else None),
                    "evidence_checklist": ({
                        "service_id": evidence_service["service_id"],
                        "entity_id": entity.entity_id,
                        "payload": {"as_of": effective_as_of, "provided_evidence": []},
                    } if evidence_service else None),
                    "calendar": ({
                        "service_id": calendar_service["service_id"],
                        "entity_id": entity.entity_id,
                        "payload": {
                            "as_of": effective_as_of,
                            "period_year": target_year,
                            **({"anchors": preview_anchors[entity.entity_id]}
                               if entity.entity_id in preview_anchors else {}),
                        },
                    } if calendar_service else None),
                },
            },
            "rules": [{
                "rule_id": rule.get("id"),
                "summary": rule.get("summary"),
                "effective_from": rule.get("effective_from"),
                "automation_level": rule.get("automation_level"),
                "human_review_required": rule.get("human_review_required") is True,
                "review_gate": rule.get("review_gate"),
                "source_ids": list(rule.get("source_ids") or []),
            } for rule in rules.get("rules") or []],
            "official_sources": list(rules.get("sources") or []),
            "calendar": calendar,
            "calendar_error": calendar_error,
        })

    calendar_tasks = sum(
        int((item.get("calendar") or {}).get("task_count") or 0) for item in entities
    )
    calendar_blocked = sum(
        1 for item in entities
        if item.get("calendar_error") or not (item.get("calendar") or {}).get("ready")
    )
    return {
        "schema_version": 4,
        "runtime_fingerprint": runtime.snapshot()["fingerprint"],
        "period_year": target_year,
        "as_of": effective_as_of,
        "summary": {
            "entity_count": len(entities),
            "jurisdiction_count": len({item["entity"]["jurisdiction"] for item in entities}),
            "calendar_task_count": calendar_tasks,
            "calendar_blocked_entity_count": calendar_blocked,
            "rule_review_due_count": rule_lifecycle["counts"]["review_due"],
            "rule_expired_count": rule_lifecycle["counts"]["expired"],
            "applicability_review_attached_count": sum(
                item["applicability_review_requirement"]["review"]["status"]
                not in {"not_attached", "invalid"}
                for item in entities
            ),
            "applicability_review_expired_count": sum(
                item["applicability_review_requirement"]["review"]["status"] == "expired"
                for item in entities
            ),
            "calendar_release_ready_entity_count": sum(
                item["calendar_release_ready"] for item in entities
            ),
            "registration_configuration_gap_count": sum(
                1 for item in entities
                if item["registration_posture"]["status"] != "configured_requires_evidence"
            ),
            # A configured category code is routing metadata, never evidence that a
            # registration exists. Every entity therefore retains an evidence gate.
            "registration_evidence_required_count": len(entities),
            "filing_assist_entity_count": sum(
                1 for item in entities
                if item["tax_pack"]["tax_readiness"] == "filing_assist"
            ),
            "preview_anchor_count": sum(
                len(values) for values in preview_anchors.values()
            ),
        },
        "entities": entities,
        "applicability_review_registry": ({
            "counts": applicability_registry["counts"],
            "unexpected_entry_count": applicability_registry[
                "unexpected_entry_count"
            ],
            "registry_clean": applicability_registry["registry_clean"],
            "content_ready_for_calendar_release": applicability_registry[
                "ready_for_calendar_release"
            ],
            "activation_receipt_configured": (
                applicability_registry_receipt is not None
            ),
            "activation_receipt_valid": bool(
                applicability_activation
                and applicability_activation["valid"]
                and applicability_activation["registry_unchanged"]
            ),
            "ready_for_calendar_release": bool(
                applicability_activation
                and applicability_activation["ready_for_calendar_release"]
            ),
            "activation": ({
                "status": (
                    "valid" if applicability_activation.get("valid") else "invalid"
                ),
                "receipt_id": applicability_activation.get("receipt_id"),
                "registry_content_fingerprint": applicability_activation.get(
                    "registry_content_fingerprint"
                ),
                "sealed_as_of": applicability_activation.get("sealed_as_of"),
                "sealed_at": applicability_activation.get("sealed_at"),
                "registry_unchanged": applicability_activation.get(
                    "registry_unchanged", False
                ),
                "controller_role_separation_verified": applicability_activation.get(
                    "controller_role_separation_verified", False
                ),
                **({"error_sha256": applicability_activation["error_sha256"]}
                   if "error_sha256" in applicability_activation else {}),
                "digital_signature_verified": False,
                "filing_authorization_granted": False,
            } if applicability_activation is not None else None),
            "paths_returned": False,
        } if applicability_registry is not None else {
            "configured": False,
            "activation_receipt_configured": False,
            "activation_receipt_valid": False,
            "ready_for_calendar_release": False,
            "paths_returned": False,
        }),
        "anchor_preview": {
            "provided_count": sum(len(values) for values in preview_anchors.values()),
            "persistent_write_performed": False,
            "box_configuration_changed": False,
            "external_service_dispatch_performed": False,
            "values_are_evidence_confirmation": False,
        },
        "control_boundary": {
            "statutory_books_kept_separate": True,
            "raw_tax_identifiers_requested": False,
            "evidence_values_accepted": False,
            "tax_calculation_performed": False,
            "filing_performed": False,
            "payment_performed": False,
            "external_submission_enabled": False,
            "calendar_candidates_are_release_approval": False,
            "registration_codes_are_evidence_confirmation": False,
            "anchor_values_persisted": False,
            "anchor_values_are_evidence_confirmation": False,
            "preview_changes_box_config": False,
            "applicability_review_path_accepted_from_request": False,
            "applicability_registry_receipt_path_accepted_from_request": False,
            "registry_receipt_is_digital_signature": False,
            "registry_receipt_grants_filing_authorization": False,
            "private_applicability_answers_returned": False,
            "private_review_rationales_returned": False,
            "private_evidence_references_returned": False,
            "registry_controller_identifier_returned": False,
        },
    }
