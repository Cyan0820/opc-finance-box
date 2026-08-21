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

from .box_api import build_box_context
from .box_runtime import BoxRuntime
from .default_connectors import build_box_connector_registry
from .tax_applicability_artifacts import (
    TaxApplicabilityArtifactError,
    verify_tax_applicability_registry_receipt,
)


MAX_ARTIFACT_BYTES = 2 * 1024 * 1024
PERIOD_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
ACTOR_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@:-]{1,127}$")
REFERENCE_PATTERN = re.compile(
    r"^(?:evidence|document|workpaper|registry|advisor|authority)://"
    r"[A-Za-z0-9][A-Za-z0-9._/#:-]{1,199}$"
)
PLACEHOLDER_PREFIX = "REPLACE_WITH_"
REVIEW_DUE_AFTER_DAYS = 60
EXPIRES_AFTER_DAYS = 90


class PilotReadinessError(ValueError):
    """Raised when a first-company pilot readiness artifact is unsafe or invalid."""


BASE_DOMAINS = (
    ("legal_entity_profile", "法律主体与核算档案", True, False),
    ("opening_trial_balance", "期初 Trial Balance", True, False),
    ("general_ledger", "本期 General Ledger", True, False),
    ("bank_activity", "银行与真实现金", True, False),
    ("revenue_evidence", "收入与结算证据", True, False),
    ("expense_evidence", "采购、费用与应付证据", True, False),
)


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def _as_of(value: Any = None) -> date:
    if value is None:
        return datetime.now(timezone.utc).date()
    text = str(value or "")
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise PilotReadinessError("pilot readiness as_of must use YYYY-MM-DD") from exc
    if parsed.isoformat() != text:
        raise PilotReadinessError("pilot readiness as_of must use canonical YYYY-MM-DD")
    return parsed


def _actor(value: Any, field: str) -> str:
    actor = str(value or "").strip()
    if actor.startswith(PLACEHOLDER_PREFIX) or not ACTOR_PATTERN.fullmatch(actor):
        raise PilotReadinessError(
            f"{field} must be a 2-128 character stable actor identifier"
        )
    return actor


def _references(value: Any, field: str, *, required: bool = True) -> list[str]:
    if not isinstance(value, list):
        raise PilotReadinessError(f"{field} must be a list")
    if required and not value:
        raise PilotReadinessError(f"{field} requires at least one opaque reference")
    if len(value) > 20 or len(value) != len(set(value)) or any(
        not isinstance(item, str) or not REFERENCE_PATTERN.fullmatch(item)
        for item in value
    ):
        raise PilotReadinessError(
            f"{field} must contain unique opaque evidence:// style references"
        )
    return list(value)


def _read_private(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    if source.is_symlink():
        raise PilotReadinessError("pilot readiness artifacts must not be symbolic links")
    if not source.is_file():
        raise PilotReadinessError("pilot readiness artifact does not exist")
    metadata = source.stat()
    if os.name != "nt" and stat.S_IMODE(metadata.st_mode) & 0o077:
        raise PilotReadinessError(
            "pilot readiness artifact must not be accessible by group or other users"
        )
    if metadata.st_size <= 0 or metadata.st_size > MAX_ARTIFACT_BYTES:
        raise PilotReadinessError("pilot readiness artifact must be 1 byte to 2 MiB")
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PilotReadinessError("pilot readiness artifact must be valid JSON") from exc
    if not isinstance(value, dict):
        raise PilotReadinessError("pilot readiness artifact must be a JSON object")
    return value


def _write_private(path: str | Path, value: dict[str, Any]) -> Path:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise PilotReadinessError(
            "pilot readiness output already exists; refusing to overwrite"
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


def _domain_requirements(capabilities: set[str]) -> list[dict[str, Any]]:
    requirements = [
        {"domain": key, "display_name": label, "required": required,
         "not_applicable_allowed": na_allowed}
        for key, label, required, na_allowed in BASE_DOMAINS
    ]
    if any(item.startswith("game.") for item in capabilities):
        requirements.extend([
            {"domain": "channel_settlements", "display_name": "游戏渠道结算",
             "required": True, "not_applicable_allowed": False},
            {"domain": "operating_kpis", "display_name": "游戏经营 KPI",
             "required": False, "not_applicable_allowed": True},
        ])
    if any(item.startswith("commerce.") for item in capabilities):
        requirements.extend([
            {"domain": "orders", "display_name": "订单事实",
             "required": True, "not_applicable_allowed": False},
            {"domain": "payments_and_settlements", "display_name": "支付与结算",
             "required": True, "not_applicable_allowed": False},
            {"domain": "refunds_and_returns", "display_name": "退款与退货",
             "required": True, "not_applicable_allowed": True},
            {"domain": "fulfillment", "display_name": "履约与物流",
             "required": True, "not_applicable_allowed": True},
        ])
    if "commerce.inventory_cost" in capabilities:
        requirements.append({
            "domain": "inventory", "display_name": "库存数量与成本",
            "required": True, "not_applicable_allowed": True,
        })
    if "commerce.import_landed_cost" in capabilities:
        requirements.append({
            "domain": "import_landed_cost", "display_name": "进口费用与到岸成本",
            "required": True, "not_applicable_allowed": True,
        })
    if "entity.intercompany_elimination" in capabilities:
        requirements.append({
            "domain": "intercompany", "display_name": "内部交易与抵销范围",
            "required": True, "not_applicable_allowed": True,
        })
    return sorted(requirements, key=lambda item: item["domain"])


def build_pilot_readiness_plan(runtime: BoxRuntime) -> dict[str, Any]:
    context = build_box_context(runtime)
    capabilities = {
        item for values in context["capability_groups"].values() for item in values
    }
    connectors = build_box_connector_registry(runtime).catalog(runtime)
    explicit_bindings = runtime.snapshot().get("connector_binding_mode") == "explicit"
    network_connectors = [{
        "connector_id": item["connector_id"],
        "display_name": item.get("display_name") or item["connector_id"],
        "credential_env_names": list(item.get("credential_env") or []),
        "network_access": True,
        **({"entity_ids": list(item.get("entity_ids") or [])} if explicit_bindings else {}),
    } for item in connectors if item.get("network_access")]
    return {
        "schema_version": 1,
        "runtime_fingerprint": context["runtime"]["fingerprint"],
        "entity_ids": sorted(str(item["id"]) for item in context["entities"]),
        "period_format": "YYYY-MM",
        "review_policy": {
            "review_due_after_days": REVIEW_DUE_AFTER_DAYS,
            "expires_after_days": EXPIRES_AFTER_DAYS,
            "expiry_effect": "block_new_bounded_shadow_runs",
            "reverification_triggers": [
                "box_runtime_fingerprint_change",
                "entity_scope_change",
                "data_domain_requirement_change",
                "network_connector_selection_change",
                "source_mapping_or_control_change",
            ],
        },
        "data_domain_requirements": _domain_requirements(capabilities),
        "network_connector_requirements": sorted(
            network_connectors, key=lambda item: item["connector_id"]
        ),
        "release_levels": {
            "ready_for_bounded_shadow": (
                "complete independently reviewed mappings and a read-only one-period plan"
            ),
            "ready_for_statutory_release": False,
            "ready_for_external_filing": False,
        },
        "control_boundary": {
            "credential_values_requested": False,
            "raw_source_identifiers_requested": False,
            "raw_tax_identifiers_requested": False,
            "financial_values_requested": False,
            "connector_dispatched": False,
            "schedule_installed": False,
            "external_actions_authorized": False,
        },
    }


def _domain_starter(requirement: dict[str, Any], entity_id: str) -> dict[str, Any]:
    return {
        **deepcopy(requirement),
        "status": "pending",
        "acquisition_mode": "pending",
        "mapped_entity_id": entity_id,
        "period_coverage": [],
        "read_only_confirmed": False,
        "mapping_approved_by": "REPLACE_WITH_DATA_REVIEWER",
        "evidence_references": [],
    }


def build_pilot_readiness_workpaper(
    runtime: BoxRuntime, *, period: str, prepared_by: str,
) -> dict[str, Any]:
    if not PERIOD_PATTERN.fullmatch(str(period or "")):
        raise PilotReadinessError("pilot period must use YYYY-MM")
    prepared = _actor(prepared_by, "prepared_by")
    plan = build_pilot_readiness_plan(runtime)
    return {
        "schema_version": 1,
        "artifact_type": "pilot_readiness_workpaper",
        "runtime_fingerprint": plan["runtime_fingerprint"],
        "plan_fingerprint": _hash(plan),
        "period": period,
        "prepared_by": prepared,
        "operator_principal": "REPLACE_WITH_DATA_OPERATOR",
        "entities": [{
            "entity_id": entity_id,
            "data_domains": [
                _domain_starter(item, entity_id)
                for item in plan["data_domain_requirements"]
            ],
        } for entity_id in plan["entity_ids"]],
        "network_connectors": [{
            "connector_id": item["connector_id"],
            "status": "pending",
            "entity_ids": list(item.get("entity_ids") or plan["entity_ids"]),
            "credential_reference_configured": False,
            "provider_contract_passed": False,
            "bounded_read_window_confirmed": False,
            "checkpoint_owner": "REPLACE_WITH_CHECKPOINT_OWNER",
            "mapping_approved_by": "REPLACE_WITH_DATA_REVIEWER",
            "evidence_references": [],
        } for item in plan["network_connector_requirements"]],
        "shadow_close_plan": {
            "planned": False,
            "period": period,
            "baseline_owner": "REPLACE_WITH_BASELINE_OWNER",
            "evidence_references": [],
        },
        "contains_credentials": False,
        "contains_raw_source_identifiers": False,
        "contains_raw_tax_identifiers": False,
        "contains_financial_values": False,
        "external_actions_authorized": False,
        "template_only": True,
    }


def write_pilot_readiness_workpaper(
    runtime: BoxRuntime, output: str | Path, *, period: str, prepared_by: str,
) -> dict[str, Any]:
    workpaper = build_pilot_readiness_workpaper(
        runtime, period=period, prepared_by=prepared_by,
    )
    target = _write_private(output, workpaper)
    return {
        "artifact_type": workpaper["artifact_type"],
        "runtime_fingerprint": workpaper["runtime_fingerprint"],
        "period": workpaper["period"],
        "entity_count": len(workpaper["entities"]),
        "network_connector_count": len(workpaper["network_connectors"]),
        "file_mode": "0600" if os.name != "nt" else "private_acl_review_required",
        "output_written": target.is_file(),
        "template_only": True,
        "credentials_returned": False,
        "financial_values_returned": False,
        "external_actions_performed": False,
    }


def _strict_fields(value: dict[str, Any], expected: set[str], field: str) -> None:
    if set(value) != expected:
        raise PilotReadinessError(f"{field} fields do not match the strict contract")


def _validate_complete_workpaper(
    runtime: BoxRuntime, value: dict[str, Any], *, require_template_only: bool,
) -> tuple[dict[str, Any], str, str]:
    fields = {
        "schema_version", "artifact_type", "runtime_fingerprint", "plan_fingerprint",
        "period", "prepared_by", "operator_principal", "entities",
        "network_connectors", "shadow_close_plan", "contains_credentials",
        "contains_raw_source_identifiers", "contains_raw_tax_identifiers",
        "contains_financial_values", "external_actions_authorized", "template_only",
    }
    _strict_fields(value, fields, "pilot readiness workpaper")
    if value.get("schema_version") != 1 or value.get("artifact_type") != "pilot_readiness_workpaper":
        raise PilotReadinessError("pilot readiness workpaper type or schema is invalid")
    plan = build_pilot_readiness_plan(runtime)
    if value.get("runtime_fingerprint") != plan["runtime_fingerprint"]:
        raise PilotReadinessError("pilot readiness workpaper belongs to a different Box runtime")
    if value.get("plan_fingerprint") != _hash(plan):
        raise PilotReadinessError("pilot readiness plan no longer matches the current Box")
    period = str(value.get("period") or "")
    if not PERIOD_PATTERN.fullmatch(period):
        raise PilotReadinessError("pilot period must use YYYY-MM")
    prepared_by = _actor(value.get("prepared_by"), "prepared_by")
    operator = _actor(value.get("operator_principal"), "operator_principal")
    if operator == prepared_by:
        raise PilotReadinessError("pilot data operator must differ from the preparer")
    for field in (
        "contains_credentials", "contains_raw_source_identifiers",
        "contains_raw_tax_identifiers", "contains_financial_values",
        "external_actions_authorized",
    ):
        if value.get(field) is not False:
            raise PilotReadinessError(f"{field} must remain false")
    if value.get("template_only") is not require_template_only:
        raise PilotReadinessError("pilot readiness template_only state is invalid")

    entities = value.get("entities")
    if not isinstance(entities, list):
        raise PilotReadinessError("pilot readiness entities must be a list")
    if [item.get("entity_id") for item in entities if isinstance(item, dict)] != plan["entity_ids"]:
        raise PilotReadinessError("pilot readiness entities must exactly match current Box scope")
    requirement_by_domain = {
        item["domain"]: item for item in plan["data_domain_requirements"]
    }
    domain_fields = {
        "domain", "display_name", "required", "not_applicable_allowed", "status",
        "acquisition_mode", "mapped_entity_id", "period_coverage",
        "read_only_confirmed", "mapping_approved_by", "evidence_references",
    }
    for entity in entities:
        _strict_fields(entity, {"entity_id", "data_domains"}, "pilot entity")
        domains = entity.get("data_domains")
        if not isinstance(domains, list) or [item.get("domain") for item in domains] != list(requirement_by_domain):
            raise PilotReadinessError("pilot data domains must exactly match current Box requirements")
        for domain in domains:
            _strict_fields(domain, domain_fields, "pilot data domain")
            requirement = requirement_by_domain[domain["domain"]]
            for field in ("display_name", "required", "not_applicable_allowed"):
                if domain.get(field) != requirement[field]:
                    raise PilotReadinessError("pilot data-domain requirement was modified")
            status_value = domain.get("status")
            if status_value not in {"ready", "not_applicable"}:
                raise PilotReadinessError("every pilot data domain requires a reviewed disposition")
            if status_value == "not_applicable" and not requirement["not_applicable_allowed"]:
                raise PilotReadinessError(f"{domain['domain']} cannot be declared not applicable")
            expected_mode = "not_applicable" if status_value == "not_applicable" else None
            if expected_mode:
                if domain.get("acquisition_mode") != expected_mode:
                    raise PilotReadinessError("not-applicable data domains require matching acquisition_mode")
            elif domain.get("acquisition_mode") not in {"file_export", "connector"}:
                raise PilotReadinessError("ready data domains require file_export or connector acquisition")
            if domain.get("mapped_entity_id") != entity["entity_id"]:
                raise PilotReadinessError("pilot data domain has a cross-entity mapping")
            coverage = domain.get("period_coverage")
            if not isinstance(coverage, list) or coverage != [period]:
                raise PilotReadinessError("pilot data-domain period coverage must exactly match the pilot period")
            if domain.get("read_only_confirmed") is not True:
                raise PilotReadinessError("pilot data-domain acquisition must be confirmed read-only")
            approver = _actor(domain.get("mapping_approved_by"), "mapping_approved_by")
            if approver == operator:
                raise PilotReadinessError("data mapping approver must differ from the data operator")
            _references(domain.get("evidence_references"), "data domain evidence")

    connector_requirements = plan["network_connector_requirements"]
    connectors = value.get("network_connectors")
    if not isinstance(connectors, list) or [item.get("connector_id") for item in connectors] != [item["connector_id"] for item in connector_requirements]:
        raise PilotReadinessError("network connectors must exactly match the current Box")
    valid_entity_ids = set(plan["entity_ids"])
    connector_fields = {
        "connector_id", "status", "entity_ids", "credential_reference_configured",
        "provider_contract_passed", "bounded_read_window_confirmed",
        "checkpoint_owner", "mapping_approved_by", "evidence_references",
    }
    for connector, requirement in zip(connectors, connector_requirements, strict=True):
        _strict_fields(connector, connector_fields, "pilot network connector")
        if connector.get("status") not in {"ready", "approved_file_fallback"}:
            raise PilotReadinessError("every selected network connector needs readiness or approved fallback")
        entity_ids = connector.get("entity_ids")
        expected_entity_ids = list(requirement.get("entity_ids") or plan["entity_ids"])
        if (
            not isinstance(entity_ids, list)
            or entity_ids != expected_entity_ids
            or not entity_ids
            or len(entity_ids) != len(set(entity_ids))
            or not set(entity_ids) <= valid_entity_ids
        ):
            raise PilotReadinessError(
                "connector entity_ids must exactly match the resolved Connector binding"
            )
        if connector["status"] == "ready":
            if connector.get("credential_reference_configured") is not True:
                raise PilotReadinessError("ready network connector lacks a credential reference")
            if connector.get("provider_contract_passed") is not True:
                raise PilotReadinessError("ready network connector lacks provider contract evidence")
            if connector.get("bounded_read_window_confirmed") is not True:
                raise PilotReadinessError("ready network connector lacks a bounded read window")
        else:
            if connector.get("credential_reference_configured") is not False:
                raise PilotReadinessError("file fallback must not claim a connector credential reference")
        checkpoint_owner = _actor(connector.get("checkpoint_owner"), "checkpoint_owner")
        approver = _actor(connector.get("mapping_approved_by"), "connector mapping_approved_by")
        if approver in {operator, checkpoint_owner}:
            raise PilotReadinessError("connector reviewer must differ from operator and checkpoint owner")
        _references(connector.get("evidence_references"), "connector evidence")

    shadow = value.get("shadow_close_plan")
    _strict_fields(
        shadow, {"planned", "period", "baseline_owner", "evidence_references"},
        "shadow close plan",
    )
    if shadow.get("planned") is not True or shadow.get("period") != period:
        raise PilotReadinessError("one-period Shadow Close must be explicitly planned")
    baseline_owner = _actor(shadow.get("baseline_owner"), "baseline_owner")
    if baseline_owner == operator:
        raise PilotReadinessError("human baseline owner must differ from the data operator")
    _references(shadow.get("evidence_references"), "shadow close plan evidence")
    return plan, prepared_by, operator


def review_pilot_readiness_workpaper(
    runtime: BoxRuntime, workpaper_json: str | Path, output: str | Path, *,
    actor: str, rationale: str, evidence_references: Iterable[str],
) -> dict[str, Any]:
    value = _read_private(workpaper_json)
    plan, prepared_by, operator = _validate_complete_workpaper(
        runtime, value, require_template_only=True,
    )
    reviewer = _actor(actor, "pilot readiness reviewer")
    if reviewer in {prepared_by, operator}:
        raise PilotReadinessError(
            "pilot readiness reviewer must differ from preparer and data operator"
        )
    rationale_value = str(rationale or "").strip()
    if len(rationale_value) < 12 or len(rationale_value) > 1000:
        raise PilotReadinessError("pilot review rationale must be 12-1000 characters")
    references = _references(list(evidence_references), "pilot review evidence")
    reviewed = deepcopy(value)
    reviewed["artifact_type"] = "pilot_readiness_review"
    reviewed["template_only"] = False
    workpaper_fingerprint = _hash(value)
    reviewed_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    reviewed_on = datetime.fromisoformat(reviewed_at.replace("Z", "+00:00")).date()
    reviewed["review"] = {
        "decision": "approved_for_bounded_shadow",
        "actor": reviewer,
        "rationale": rationale_value,
        "evidence_references": references,
        "reviewed_at": reviewed_at,
        "review_due_at": (
            reviewed_on + timedelta(days=REVIEW_DUE_AFTER_DAYS)
        ).isoformat(),
        "expires_at": (
            reviewed_on + timedelta(days=EXPIRES_AFTER_DAYS)
        ).isoformat(),
        "workpaper_fingerprint": workpaper_fingerprint,
        "review_id": _hash({
            "runtime_fingerprint": plan["runtime_fingerprint"],
            "workpaper_fingerprint": workpaper_fingerprint,
            "actor": reviewer,
            "reviewed_at": reviewed_at,
        })[:24],
    }
    target = _write_private(output, reviewed)
    return {
        "artifact_type": reviewed["artifact_type"],
        "runtime_fingerprint": reviewed["runtime_fingerprint"],
        "period": reviewed["period"],
        "review_id": reviewed["review"]["review_id"],
        "entity_count": len(reviewed["entities"]),
        "network_connector_count": len(reviewed["network_connectors"]),
        "ready_for_bounded_shadow": True,
        "ready_for_statutory_release": False,
        "ready_for_external_filing": False,
        "file_mode": "0600" if os.name != "nt" else "private_acl_review_required",
        "output_written": target.is_file(),
        "actors_returned": False,
        "evidence_references_returned": False,
        "credentials_returned": False,
        "financial_values_returned": False,
        "external_actions_performed": False,
    }


def verify_pilot_readiness_review(
    runtime: BoxRuntime, review_json: str | Path, *,
    tax_review_dir: str | Path | None = None,
    tax_registry_receipt: str | Path | None = None,
    as_of: str | None = None,
) -> dict[str, Any]:
    value = _read_private(review_json)
    review = value.pop("review", None)
    try:
        if value.get("artifact_type") != "pilot_readiness_review":
            raise PilotReadinessError("pilot readiness review artifact type is invalid")
        workpaper = deepcopy(value)
        workpaper["artifact_type"] = "pilot_readiness_workpaper"
        workpaper["template_only"] = True
        plan, prepared_by, operator = _validate_complete_workpaper(
            runtime, workpaper, require_template_only=True,
        )
    finally:
        if isinstance(review, dict):
            value["review"] = review
    if not isinstance(review, dict):
        raise PilotReadinessError("pilot readiness review is missing")
    _strict_fields(review, {
        "decision", "actor", "rationale", "evidence_references", "reviewed_at",
        "review_due_at", "expires_at", "workpaper_fingerprint", "review_id",
    }, "pilot readiness review")
    reviewer = _actor(review.get("actor"), "pilot readiness reviewer")
    if reviewer in {prepared_by, operator}:
        raise PilotReadinessError(
            "pilot readiness reviewer must differ from preparer and data operator"
        )
    if review.get("decision") != "approved_for_bounded_shadow":
        raise PilotReadinessError("pilot readiness review is not approved for bounded shadow")
    rationale = str(review.get("rationale") or "").strip()
    if len(rationale) < 12 or len(rationale) > 1000:
        raise PilotReadinessError("pilot review rationale must be 12-1000 characters")
    _references(review.get("evidence_references"), "pilot review evidence")
    try:
        reviewed_at = datetime.fromisoformat(
            str(review.get("reviewed_at") or "").replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise PilotReadinessError("pilot reviewed_at must be an ISO date-time") from exc
    if reviewed_at.tzinfo is None:
        raise PilotReadinessError("pilot reviewed_at must include a timezone")
    reviewed_on = reviewed_at.astimezone(timezone.utc).date()
    expected_review_due = reviewed_on + timedelta(days=REVIEW_DUE_AFTER_DAYS)
    expected_expiry = reviewed_on + timedelta(days=EXPIRES_AFTER_DAYS)
    if review.get("review_due_at") != expected_review_due.isoformat():
        raise PilotReadinessError("pilot review_due_at does not match review policy")
    if review.get("expires_at") != expected_expiry.isoformat():
        raise PilotReadinessError("pilot expires_at does not match review policy")
    if review.get("workpaper_fingerprint") != _hash(workpaper):
        raise PilotReadinessError("pilot readiness review is not bound to the current workpaper")
    expected_review_id = _hash({
        "runtime_fingerprint": plan["runtime_fingerprint"],
        "workpaper_fingerprint": review["workpaper_fingerprint"],
        "actor": reviewer,
        "reviewed_at": review["reviewed_at"],
    })[:24]
    if review.get("review_id") != expected_review_id:
        raise PilotReadinessError("pilot readiness review_id is invalid")

    effective_as_of = _as_of(as_of)
    if effective_as_of < reviewed_on:
        raise PilotReadinessError("pilot readiness as_of cannot predate reviewed_at")
    lifecycle_status = (
        "expired" if effective_as_of > expected_expiry else
        "review_due" if effective_as_of >= expected_review_due else
        "current"
    )
    bounded_shadow_ready = lifecycle_status != "expired"

    if (tax_review_dir is None) != (tax_registry_receipt is None):
        raise PilotReadinessError(
            "tax review directory and registry receipt must be configured together"
        )
    activation: dict[str, Any] = {
        "configured": False, "valid": False,
        "ready_for_calendar_release": False,
    }
    if tax_review_dir is not None and tax_registry_receipt is not None:
        try:
            verified = verify_tax_applicability_registry_receipt(
                runtime, tax_review_dir, tax_registry_receipt, as_of=as_of,
            )
        except (TaxApplicabilityArtifactError, OSError, ValueError) as exc:
            activation = {
                "configured": True, "valid": False,
                "ready_for_calendar_release": False,
                "error_sha256": hashlib.sha256(str(exc).encode("utf-8")).hexdigest(),
            }
        else:
            activation = {
                "configured": True,
                "valid": bool(verified["valid"]),
                "ready_for_calendar_release": bool(
                    verified["ready_for_calendar_release"]
                ),
                "receipt_id": verified["receipt_id"],
            }
    return {
        "schema_version": 1,
        "valid": True,
        "runtime_fingerprint": plan["runtime_fingerprint"],
        "period": workpaper["period"],
        "review_id": review["review_id"],
        "entity_count": len(workpaper["entities"]),
        "data_domain_count": sum(
            len(item["data_domains"]) for item in workpaper["entities"]
        ),
        "network_connector_count": len(workpaper["network_connectors"]),
        "as_of": effective_as_of.isoformat(),
        "lifecycle_status": lifecycle_status,
        "review_due_at": expected_review_due.isoformat(),
        "expires_at": expected_expiry.isoformat(),
        "ready_for_bounded_shadow": bounded_shadow_ready,
        "tax_registry_activation": activation,
        "ready_for_tax_calendar_release": False,
        "ready_for_statutory_release": False,
        "ready_for_external_filing": False,
        "actors_returned": False,
        "evidence_references_returned": False,
        "paths_returned": False,
        "credentials_returned": False,
        "raw_source_identifiers_returned": False,
        "raw_tax_identifiers_returned": False,
        "financial_values_returned": False,
        "digital_signature_verified": False,
        "external_actions_authorized": False,
        "external_actions_performed": False,
    }


def build_pilot_readiness_status(
    runtime: BoxRuntime, review_json: str | Path | None = None, *,
    tax_review_dir: str | Path | None = None,
    tax_registry_receipt: str | Path | None = None,
    as_of: str | None = None,
) -> dict[str, Any]:
    effective_as_of = _as_of(as_of)
    if review_json is None:
        return {
            "schema_version": 1,
            "runtime_fingerprint": runtime.snapshot()["fingerprint"],
            "as_of": effective_as_of.isoformat(),
            "configured": False,
            "valid": False,
            "status": "missing",
            "ready_for_bounded_shadow": False,
            "ready_for_statutory_release": False,
            "ready_for_external_filing": False,
            "paths_returned": False,
            "actors_returned": False,
            "evidence_references_returned": False,
            "credentials_returned": False,
            "financial_values_returned": False,
            "external_actions_performed": False,
        }
    try:
        verified = verify_pilot_readiness_review(
            runtime, review_json,
            tax_review_dir=tax_review_dir,
            tax_registry_receipt=tax_registry_receipt,
            as_of=effective_as_of.isoformat(),
        )
    except (PilotReadinessError, OSError, ValueError) as exc:
        return {
            "schema_version": 1,
            "runtime_fingerprint": runtime.snapshot()["fingerprint"],
            "as_of": effective_as_of.isoformat(),
            "configured": True,
            "valid": False,
            "status": "invalid",
            "error_sha256": hashlib.sha256(str(exc).encode("utf-8")).hexdigest(),
            "ready_for_bounded_shadow": False,
            "ready_for_statutory_release": False,
            "ready_for_external_filing": False,
            "paths_returned": False,
            "actors_returned": False,
            "evidence_references_returned": False,
            "credentials_returned": False,
            "financial_values_returned": False,
            "external_actions_performed": False,
        }
    return {
        **verified,
        "configured": True,
        "status": verified["lifecycle_status"],
    }


def build_pilot_readiness_alerts(
    runtime: BoxRuntime, review_json: str | Path | None = None, *,
    as_of: str | None = None,
) -> dict[str, Any]:
    status = build_pilot_readiness_status(runtime, review_json, as_of=as_of)
    alerts: list[dict[str, Any]] = []
    if status["status"] == "missing":
        alerts.append({
            "alert_id": "pilot-readiness:review:missing",
            "severity": "warning", "category": "pilot_activation",
            "status": "missing",
        })
    elif status["status"] == "invalid":
        alerts.append({
            "alert_id": "pilot-readiness:review:invalid",
            "severity": "critical", "category": "pilot_activation",
            "status": "invalid", "error_sha256": status["error_sha256"],
        })
    elif status["status"] in {"review_due", "expired"}:
        alerts.append({
            "alert_id": f"pilot-readiness:review:{status['status']}",
            "severity": "critical" if status["status"] == "expired" else "warning",
            "category": "pilot_review_lifecycle",
            "status": status["status"],
            "review_due_at": status["review_due_at"],
            "expires_at": status["expires_at"],
        })
    return {
        "schema_version": 1,
        "runtime_fingerprint": status["runtime_fingerprint"],
        "as_of": status["as_of"],
        "status": status["status"],
        "alert_count": len(alerts),
        "critical_count": sum(item["severity"] == "critical" for item in alerts),
        "warning_count": sum(item["severity"] == "warning" for item in alerts),
        "alerts": alerts,
        "ready_for_bounded_shadow": status["ready_for_bounded_shadow"],
        "notification_candidates_only": True,
        "notifications_sent": False,
        "schedule_installed": False,
        "paths_returned": False,
        "actors_returned": False,
        "evidence_references_returned": False,
        "credentials_returned": False,
        "financial_values_returned": False,
        "external_actions_performed": False,
    }


def build_pilot_readiness_workspace(
    runtime: BoxRuntime, review_json: str | Path | None = None, *,
    tax_review_dir: str | Path | None = None,
    tax_registry_receipt: str | Path | None = None,
    as_of: str | None = None,
) -> dict[str, Any]:
    plan = build_pilot_readiness_plan(runtime)
    activation = build_pilot_readiness_status(
        runtime, review_json,
        tax_review_dir=tax_review_dir,
        tax_registry_receipt=tax_registry_receipt,
        as_of=as_of,
    )
    alerts = build_pilot_readiness_alerts(
        runtime, review_json, as_of=activation["as_of"],
    )
    return {
        "schema_version": 1,
        "runtime_fingerprint": plan["runtime_fingerprint"],
        "as_of": activation["as_of"],
        "summary": {
            "entity_count": len(plan["entity_ids"]),
            "data_domain_count_per_entity": len(plan["data_domain_requirements"]),
            "total_data_domain_count": (
                len(plan["entity_ids"]) * len(plan["data_domain_requirements"])
            ),
            "network_connector_count": len(plan["network_connector_requirements"]),
            "activation_status": activation["status"],
            "ready_for_bounded_shadow": activation["ready_for_bounded_shadow"],
            "alert_count": alerts["alert_count"],
        },
        "entities": [{
            "entity_id": entity_id,
            "required_domains": deepcopy(plan["data_domain_requirements"]),
        } for entity_id in plan["entity_ids"]],
        "network_connectors": deepcopy(plan["network_connector_requirements"]),
        "review_policy": deepcopy(plan["review_policy"]),
        "activation": activation,
        "alerts": alerts,
        "control_boundary": {
            **deepcopy(plan["control_boundary"]),
            "review_path_returned": False,
            "actors_returned": False,
            "evidence_references_returned": False,
            "activation_is_digital_signature": False,
            "ready_for_statutory_release": False,
            "ready_for_external_filing": False,
        },
    }
