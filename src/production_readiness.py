from __future__ import annotations

from datetime import date, timedelta
import os
from pathlib import Path
from typing import Any, Mapping

from .box_api import build_box_context
from .box_config import load_pack_catalog
from .box_runtime import BoxRuntime
from .default_connectors import build_box_connector_registry


PRIVATE_ENV_PATHS = {
    "tax_review_dir": "OPC_TAX_APPLICABILITY_REVIEW_DIR",
    "tax_registry_receipt": "OPC_TAX_APPLICABILITY_REGISTRY_RECEIPT",
    "connector_shadow_review_dir": "OPC_CONNECTOR_SHADOW_REVIEW_DIR",
    "pilot_readiness_review": "OPC_PILOT_READINESS_REVIEW",
    "pilot_data_handoff_review": "OPC_PILOT_DATA_HANDOFF_REVIEW",
    "pilot_shadow_run_registration": "OPC_PILOT_SHADOW_RUN_REGISTRATION",
    "pilot_shadow_observation_review": "OPC_PILOT_SHADOW_OBSERVATION_REVIEW",
    "pilot_shadow_entity_report_dir": "OPC_PILOT_SHADOW_ENTITY_REPORT_DIR",
    "pilot_shadow_portfolio_review": "OPC_PILOT_SHADOW_PORTFOLIO_REVIEW",
    "pilot_shadow_series_review": "OPC_PILOT_SHADOW_SERIES_REVIEW",
    "pilot_shadow_series_evidence_root": "OPC_PILOT_SHADOW_SERIES_EVIDENCE_ROOT",
    "stable_promotion_root": "OPC_STABLE_PROMOTION_ROOT",
}


def _private_path(environment: Mapping[str, str], key: str) -> Path | None:
    value = str(environment.get(PRIVATE_ENV_PATHS[key]) or "").strip()
    return Path(value) if value else None


def _stable_promotion_stage(
    runtime: BoxRuntime,
    root: Path | None,
    selected_packs: list[dict[str, Any]],
) -> dict[str, Any]:
    """Project current Pack-level approvals without exposing or mutating the ledger."""
    targets = [item for item in selected_packs if item.get("status") != "stable"]
    base_facts = {
        "ledger_configured": root is not None,
        "ledger_integrity_valid": False,
        "assessment_performed": False,
        "target_pack_count": len(targets),
        "approved_target_pack_count": 0,
        "missing_target_pack_count": len(targets),
        "stable_candidate_approved": False,
        "pack_manifest_changed": False,
    }
    if not targets:
        return {
            "stage_id": "stable_promotion",
            "status": "selected_packs_already_stable",
            "gate_passed": True,
            "evidence_complete": True,
            "facts": {
                **base_facts,
                "ledger_integrity_valid": root is None,
                "missing_target_pack_count": 0,
                "stable_candidate_approved": True,
            },
        }
    if root is None:
        return {
            "stage_id": "stable_promotion",
            "status": "promotion_ledger_not_attached",
            "gate_passed": False,
            "evidence_complete": False,
            "facts": base_facts,
        }
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        return {
            "stage_id": "stable_promotion",
            "status": "promotion_ledger_missing_or_unsafe",
            "gate_passed": False,
            "evidence_complete": False,
            "facts": base_facts,
        }
    from .release_promotion import ReleasePromotionError, ReleasePromotionStore

    try:
        snapshot = ReleasePromotionStore(root).readiness_snapshot(
            runtime_fingerprint=runtime.snapshot()["fingerprint"],
        )
    except (ReleasePromotionError, OSError, ValueError):
        return {
            "stage_id": "stable_promotion",
            "status": "promotion_ledger_invalid",
            "gate_passed": False,
            "evidence_complete": False,
            "facts": base_facts,
        }
    current_by_pack: dict[str, dict[str, Any]] = {}
    for item in snapshot["candidates"]:
        current_by_pack.setdefault(item["pack_id"], item)
    approved = 0
    matching_assessments = 0
    for target in targets:
        candidate = current_by_pack.get(target["pack_id"])
        if candidate is None or candidate.get("pack_version") != target["version"]:
            continue
        matching_assessments += 1
        if candidate.get("release_status") == "stable_candidate_approved":
            approved += 1
    complete = approved == len(targets)
    if complete:
        status = "all_selected_pack_candidates_approved"
    elif matching_assessments:
        status = "partial_or_pending_pack_promotion"
    elif snapshot["counts"]["assessments"]:
        status = "no_current_box_pack_assessment"
    else:
        status = "promotion_assessment_missing"
    return {
        "stage_id": "stable_promotion",
        "status": status,
        "gate_passed": complete,
        "evidence_complete": complete,
        "facts": {
            **base_facts,
            "ledger_integrity_valid": True,
            "assessment_performed": matching_assessments > 0,
            "target_pack_count": len(targets),
            "approved_target_pack_count": approved,
            "missing_target_pack_count": len(targets) - approved,
            "stable_candidate_approved": complete,
            "ledger_event_count": snapshot["event_count"],
        },
    }


def build_production_readiness_plan(runtime: BoxRuntime) -> dict[str, Any]:
    """Compile a deterministic evidence plan without inspecting credentials or artifacts."""
    from .pack_audit import audit_pack_catalog

    context = build_box_context(runtime)
    catalog = load_pack_catalog(runtime.packs_root)
    audit = audit_pack_catalog(catalog)
    audit_by_id = {item["pack_id"]: item for item in audit["packs"]}
    selected_packs = []
    for pack in context["packs"]:
        pack_audit = audit_by_id[pack["id"]]
        selected_packs.append({
            "pack_id": pack["id"],
            "kind": pack["kind"],
            "version": pack["version"],
            "status": pack["status"],
            "contract_valid": pack_audit["contract_valid"],
            "complete_implementation": pack_audit["complete_implementation"],
            "stable_release_ready": pack_audit["stable_release_ready"],
        })

    tax_entities = []
    for entity in context["entities"]:
        bundle = runtime.tax_rules(entity["id"])
        rules = bundle["rules"]
        verified_at = date.fromisoformat(rules["verified_at"])
        review_policy = rules["review_policy"]
        expires_at = verified_at + timedelta(days=review_policy["max_age_days"])
        review_due_at = expires_at - timedelta(
            days=review_policy["warning_days_before_expiry"],
        )
        tax_entities.append({
            "entity_id": entity["id"],
            "jurisdiction": entity["jurisdiction"],
            "tax_pack": bundle["pack_id"],
            "pack_version": bundle["pack_version"],
            "pack_status": bundle["pack_status"],
            "tax_readiness": entity["tax_readiness"],
            "rules_verified_at": verified_at.isoformat(),
            "review_due_at": review_due_at.isoformat(),
            "expires_at": expires_at.isoformat(),
            "applicability_review_required": True,
            "registration_evidence_required": True,
            "external_filing_ready": False,
        })

    connectors = build_box_connector_registry(runtime).catalog(runtime)
    explicit_bindings = runtime.snapshot().get("connector_binding_mode") == "explicit"
    connector_requirements = [{
        "connector_id": item["connector_id"],
        "display_name": item.get("display_name") or item["connector_id"],
        "network_access": bool(item.get("network_access")),
        "credential_env_names": list(item.get("credential_env") or []),
        "entity_source_mapping_required": True,
        "bounded_shadow_required": True,
        "financial_reconciliation_required": True,
        "schedule_release_separate": True,
        **({"entity_ids": list(item.get("entity_ids") or [])} if explicit_bindings else {}),
    } for item in connectors]

    selected_contract_valid = all(
        item["contract_valid"] and item["complete_implementation"]
        for item in selected_packs
    )
    stages = [
        ("pack_contracts", "能力包契约", "能力包审计与当前工作台版本锁"),
        ("tax_rule_sources", "税务规则时效", "官方来源复核策略"),
        ("tax_applicability", "主体税务适用性", "逐主体独立签认与目录封印"),
        ("connector_configuration", "数据连接器配置", "无密钥配置状态与主体映射"),
        (
            "connector_shadow_evidence", "连接器并行证据",
            "真实匿名来源、当前独立复核与所选网络 Connector Pack 覆盖",
        ),
        ("pilot_readiness", "首家试运行准入", "逐主体资料域与独立复核"),
        ("data_handoff", "真实资料交接", "私有清单与受控接收"),
        ("shadow_run_registration", "首次月结试跑登记", "运行台账与全部复核门"),
        ("shadow_observation", "首次观察复核", "逐主体报告与组合签认"),
        ("consecutive_shadow_series", "连续月份证据", "至少两个自然月与独立连续性复核"),
        ("stable_promotion", "稳定版晋级", "独立晋级评估与追加式签认账本"),
    ]
    from .activation_orchestrator import build_activation_stage_contracts
    from .activation_workspace import build_activation_workspace_contract

    activation_contracts = build_activation_stage_contracts()
    return {
        "schema_version": 1,
        "artifact_type": "production_readiness_plan",
        "runtime_fingerprint": context["runtime"]["fingerprint"],
        "entity_ids": sorted(item["id"] for item in context["entities"]),
        "selected_packs": selected_packs,
        "pack_contracts": {
            "selected_pack_count": len(selected_packs),
            "selected_contract_valid": selected_contract_valid,
            "installed_pack_count": audit["pack_count"],
            "installed_capability_count": audit["capability_count"],
            "stable_release_ready": False,
        },
        "tax_entities": tax_entities,
        "connector_requirements": connector_requirements,
        "stages": [{
            "stage_order": index,
            "stage_id": stage_id,
            "display_name": display_name,
            "required_evidence": evidence,
            "operator_contract": activation_contracts[stage_id],
            "runtime_evaluation_required": stage_id != "pack_contracts",
            "initial_status": (
                "contract_ready_for_preview"
                if stage_id == "pack_contracts" and selected_contract_valid
                else "not_evaluated"
            ),
        } for index, (stage_id, display_name, evidence) in enumerate(stages, start=1)],
        "first_customer_workspace": build_activation_workspace_contract(),
        "private_artifact_environment_names": sorted(PRIVATE_ENV_PATHS.values()),
        "control_boundary": {
            "credential_values_inspected": False,
            "private_artifacts_inspected": False,
            "paths_returned": False,
            "financial_values_returned": False,
            "pack_maturity_inferred": False,
            "stable_promotion_performed": False,
            "external_filing_authorized": False,
            "external_actions_performed": False,
        },
    }


def build_production_readiness_workspace(
    runtime: BoxRuntime,
    services: Any,
    *,
    runs_root: str | Path,
    environ: Mapping[str, str] | None = None,
    as_of: str | None = None,
) -> dict[str, Any]:
    """Aggregate existing fail-closed readiness gates into one browser-safe matrix."""
    from .connector_onboarding import build_connector_onboarding
    from .connector_shadow_registry import build_connector_shadow_registry_workspace
    from .pilot_data_handoff import build_pilot_data_handoff_workspace
    from .pilot_readiness import build_pilot_readiness_workspace
    from .pilot_shadow_observation import build_pilot_shadow_observation_workspace
    from .pilot_shadow_run import build_pilot_shadow_run_workspace
    from .pilot_shadow_series import build_pilot_shadow_series_workspace
    from .tax_workspace import build_tax_workspace

    environment = os.environ if environ is None else environ
    plan = build_production_readiness_plan(runtime)
    paths = {
        key: _private_path(environment, key) for key in PRIVATE_ENV_PATHS
    }
    tax = build_tax_workspace(
        runtime,
        services,
        as_of=as_of,
        applicability_review_dir=paths["tax_review_dir"],
        applicability_registry_receipt=paths["tax_registry_receipt"],
    )
    effective_as_of = tax["as_of"]
    connector = build_connector_onboarding(runtime, environ=environment)
    connector_shadow = build_connector_shadow_registry_workspace(
        runtime,
        paths["connector_shadow_review_dir"],
        as_of=effective_as_of,
    )
    pilot = build_pilot_readiness_workspace(
        runtime,
        paths["pilot_readiness_review"],
        tax_review_dir=paths["tax_review_dir"],
        tax_registry_receipt=paths["tax_registry_receipt"],
        as_of=effective_as_of,
    )
    handoff = build_pilot_data_handoff_workspace(
        runtime,
        paths["pilot_data_handoff_review"],
        paths["pilot_readiness_review"],
        as_of=effective_as_of,
    )
    shadow_run = build_pilot_shadow_run_workspace(
        runtime,
        paths["pilot_shadow_run_registration"],
        paths["pilot_data_handoff_review"],
        paths["pilot_readiness_review"],
        runs_root,
        as_of=effective_as_of,
    )
    observation = build_pilot_shadow_observation_workspace(
        runtime,
        paths["pilot_shadow_observation_review"],
        paths["pilot_shadow_run_registration"],
        paths["pilot_data_handoff_review"],
        paths["pilot_readiness_review"],
        runs_root,
        paths["pilot_shadow_entity_report_dir"],
        portfolio_review_path=paths["pilot_shadow_portfolio_review"],
        as_of=effective_as_of,
    )
    series = build_pilot_shadow_series_workspace(
        runtime,
        paths["pilot_shadow_series_review"],
        paths["pilot_shadow_series_evidence_root"],
        runs_root,
        as_of=effective_as_of,
    )
    promotion = _stable_promotion_stage(
        runtime,
        paths["stable_promotion_root"],
        plan["selected_packs"],
    )

    tax_summary = tax["summary"]
    entity_count = tax_summary["entity_count"]
    tax_expired = tax_summary["rule_expired_count"]
    tax_due = tax_summary["rule_review_due_count"]
    tax_rule_status = (
        "expired" if tax_expired else "review_due" if tax_due else "current"
    )
    applicability_ready = (
        entity_count > 0
        and tax_summary["calendar_release_ready_entity_count"] == entity_count
    )
    connector_summary = connector["summary"]
    connector_gate = connector_summary["blocked_connector_count"] == 0
    network_connector_count = connector_summary["network_connector_count"]
    connector_status = (
        "blocked_missing_credential_reference"
        if not connector_gate
        else "credentials_ready_shadow_evidence_required"
        if network_connector_count
        else "ready_for_fixture_or_shadow"
    )

    raw_stages = [
        {
            "stage_id": "pack_contracts",
            "status": (
                "contract_ready_for_preview"
                if plan["pack_contracts"]["selected_contract_valid"]
                else "blocked_pack_contract"
            ),
            "gate_passed": plan["pack_contracts"]["selected_contract_valid"],
            "evidence_complete": plan["pack_contracts"]["selected_contract_valid"],
            "facts": {
                "selected_pack_count": plan["pack_contracts"]["selected_pack_count"],
                "installed_capability_count": plan["pack_contracts"]["installed_capability_count"],
            },
        },
        {
            "stage_id": "tax_rule_sources",
            "status": tax_rule_status,
            "gate_passed": tax_expired == 0,
            "evidence_complete": tax_expired == 0 and tax_due == 0,
            "facts": {
                "entity_count": entity_count,
                "review_due_count": tax_due,
                "expired_count": tax_expired,
            },
        },
        {
            "stage_id": "tax_applicability",
            "status": "current" if applicability_ready else "not_attached_or_not_activated",
            "gate_passed": applicability_ready,
            "evidence_complete": applicability_ready,
            "facts": {
                "entity_count": entity_count,
                "review_attached_count": tax_summary["applicability_review_attached_count"],
                "release_ready_count": tax_summary["calendar_release_ready_entity_count"],
            },
        },
        {
            "stage_id": "connector_configuration",
            "status": connector_status,
            "gate_passed": connector_gate,
            "evidence_complete": network_connector_count == 0,
            "facts": {
                "pipeline_connector_count": connector_summary["pipeline_connector_count"],
                "network_connector_count": network_connector_count,
                "blocked_connector_count": connector_summary["blocked_connector_count"],
                "shadow_run_performed": False,
            },
        },
        {
            "stage_id": "connector_shadow_evidence",
            "status": connector_shadow["summary"]["activation_status"],
            "gate_passed": connector_shadow["summary"][
                "ready_for_connector_shadow_evidence"
            ],
            "evidence_complete": connector_shadow["summary"][
                "pack_coverage_complete"
            ],
            "facts": {
                "required_network_pack_count": connector_shadow["summary"][
                    "required_network_pack_count"
                ],
                "covered_network_pack_count": connector_shadow["summary"][
                    "covered_network_pack_count"
                ],
                "current_artifact_count": connector_shadow["summary"][
                    "current_artifact_count"
                ],
                "unsafe_or_noncurrent_artifact_count": connector_shadow["summary"][
                    "unsafe_or_noncurrent_artifact_count"
                ],
            },
        },
        {
            "stage_id": "pilot_readiness",
            "status": pilot["summary"]["activation_status"],
            "gate_passed": pilot["summary"]["ready_for_bounded_shadow"],
            "evidence_complete": pilot["summary"]["ready_for_bounded_shadow"],
            "facts": {
                "entity_count": pilot["summary"]["entity_count"],
                "data_domain_count": pilot["summary"]["total_data_domain_count"],
            },
        },
        {
            "stage_id": "data_handoff",
            "status": handoff["summary"]["activation_status"],
            "gate_passed": handoff["summary"]["ready_for_bounded_shadow"],
            "evidence_complete": handoff["summary"]["ready_for_controlled_data_intake"],
            "facts": {
                "entity_count": handoff["summary"]["entity_count"],
                "data_domain_count": handoff["summary"]["total_data_domain_count"],
            },
        },
        {
            "stage_id": "shadow_run_registration",
            "status": shadow_run["summary"]["activation_status"],
            "gate_passed": shadow_run["summary"]["ready_for_first_shadow_observation"],
            "evidence_complete": shadow_run["summary"]["ready_for_first_shadow_observation"],
            "facts": {
                "entity_count": shadow_run["summary"]["entity_count"],
                "registered_entity_count": shadow_run["summary"]["registered_entity_count"],
            },
        },
        {
            "stage_id": "shadow_observation",
            "status": observation["summary"]["activation_status"],
            "gate_passed": observation["summary"]["ready_for_next_shadow_period"],
            "evidence_complete": observation["summary"]["ready_for_next_shadow_period"],
            "facts": {
                "entity_count": observation["summary"]["entity_count"],
                "reviewed_entity_count": observation["summary"]["reviewed_entity_count"],
                "system_defect_count": observation["summary"]["system_defect_count"],
            },
        },
        {
            "stage_id": "consecutive_shadow_series",
            "status": series["summary"]["activation_status"],
            "gate_passed": series["summary"]["eligible_to_prepare_stable_promotion_evidence"],
            "evidence_complete": series["summary"]["consecutive_periods_verified"],
            "facts": {
                "period_count": series["summary"]["period_count"],
                "consecutive_periods_verified": series["summary"]["consecutive_periods_verified"],
                "system_defect_count": series["summary"]["system_defect_count"],
            },
        },
        promotion,
    ]
    plan_stages = {item["stage_id"]: item for item in plan["stages"]}
    stages = [{
        "stage_order": plan_stages[item["stage_id"]]["stage_order"],
        "display_name": plan_stages[item["stage_id"]]["display_name"],
        "required_evidence": plan_stages[item["stage_id"]]["required_evidence"],
        **item,
    } for item in raw_stages]
    blockers = [{
        "stage_id": item["stage_id"],
        "code": item["status"],
        "next_gate": item["required_evidence"],
    } for item in stages if not item["gate_passed"]]

    ready_for_internal_demo = (
        stages[0]["gate_passed"] and tax_expired == 0
    )
    return {
        "schema_version": 1,
        "artifact_type": "production_readiness_workspace",
        "runtime_fingerprint": plan["runtime_fingerprint"],
        "as_of": effective_as_of,
        "summary": {
            "stage_count": len(stages),
            "passed_stage_count": sum(item["gate_passed"] for item in stages),
            "blocking_stage_count": len(blockers),
            "entity_count": entity_count,
            "selected_pack_count": plan["pack_contracts"]["selected_pack_count"],
            "network_connector_count": network_connector_count,
            "connector_shadow_pack_coverage_complete": connector_shadow["summary"][
                "pack_coverage_complete"
            ],
            "connector_shadow_current_artifact_count": connector_shadow["summary"][
                "current_artifact_count"
            ],
            "ready_for_internal_demo": ready_for_internal_demo,
            "ready_for_bounded_shadow": shadow_run["summary"]["ready_for_first_shadow_observation"],
            "ready_for_next_shadow_period": observation["summary"]["ready_for_next_shadow_period"],
            "eligible_to_prepare_stable_promotion_evidence": series["summary"]["eligible_to_prepare_stable_promotion_evidence"],
            "stable_candidate_target_pack_count": promotion["facts"]["target_pack_count"],
            "approved_stable_candidate_pack_count": promotion["facts"]["approved_target_pack_count"],
            "ready_for_stable_promotion": promotion["gate_passed"],
            "ready_for_external_filing": False,
        },
        "stages": stages,
        "blockers": blockers,
        "selected_packs": plan["selected_packs"],
        "tax_entities": plan["tax_entities"],
        "control_boundary": {
            "private_mounts_server_or_environment_configured_only": True,
            "paths_returned": False,
            "credential_values_returned": False,
            "private_artifact_contents_returned": False,
            "actors_returned": False,
            "evidence_references_returned": False,
            "financial_values_returned": False,
            "readiness_is_release_approval": False,
            "promotion_ledger_inspected_read_only": promotion["facts"][
                "ledger_integrity_valid"
            ],
            "stable_promotion_performed": False,
            "posting_authorized": False,
            "payment_authorized": False,
            "period_close_authorized": False,
            "external_filing_authorized": False,
            "external_actions_performed": False,
        },
    }
