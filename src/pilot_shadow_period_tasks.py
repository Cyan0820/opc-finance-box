from __future__ import annotations

from typing import Any, Mapping, Sequence


class PilotShadowPeriodTaskError(ValueError):
    """Raised when a monthly command step has no safe operator contract."""


def _contract(
    task_type: str,
    phase: str,
    responsible_role: str,
    *,
    evidence: Sequence[str],
    independent_review_role: str | None = None,
    separate_from: Sequence[str] = (),
) -> dict[str, Any]:
    return {
        "task_type": task_type,
        "phase": phase,
        "responsible_role": responsible_role,
        "independent_review_role": independent_review_role,
        "must_be_separate_from_role_ids": list(separate_from),
        "required_evidence_type_ids": list(evidence),
    }


_EXACT_CONTRACTS: dict[str, dict[str, Any]] = {
    "pilot-readiness-complete": _contract(
        "pilot_readiness_workpaper", "pilot_readiness",
        "pilot_finance_preparer",
        evidence=("current_period_entity_mapping", "bounded_connector_plan"),
        independent_review_role="pilot_control_reviewer",
    ),
    "pilot-readiness-review": _contract(
        "pilot_readiness_review", "pilot_readiness",
        "pilot_control_reviewer",
        evidence=("completed_readiness_workpaper", "readiness_evidence_reference"),
        separate_from=("pilot_finance_preparer",),
    ),
    "pilot-readiness-verify": _contract(
        "pilot_readiness_verification", "pilot_readiness", "control_verifier",
        evidence=("current_tax_rotation", "reviewed_readiness_workpaper"),
    ),
    "data-handoff-init": _contract(
        "data_handoff_initialization", "data_handoff", "data_handoff_preparer",
        evidence=("reviewed_readiness_workpaper", "data_custodian_assignment"),
        independent_review_role="data_access_reviewer",
    ),
    "data-handoff-complete": _contract(
        "data_handoff_inventory", "data_handoff", "data_handoff_preparer",
        evidence=("source_inventory", "custody_record", "access_boundary"),
        independent_review_role="data_access_reviewer",
    ),
    "data-handoff-review": _contract(
        "data_handoff_review", "data_handoff", "data_access_reviewer",
        evidence=("completed_handoff_workpaper", "handoff_evidence_reference"),
        separate_from=("data_handoff_preparer", "data_custodian"),
    ),
    "data-handoff-verify": _contract(
        "data_handoff_verification", "data_handoff", "control_verifier",
        evidence=("reviewed_handoff", "reviewed_readiness_workpaper"),
    ),
    "pipeline-attempts-complete": _contract(
        "entity_pipeline_attempts", "shadow_run", "pipeline_operator",
        evidence=("one_approved_attempt_per_entity", "month_close_gate_reviews"),
        independent_review_role="month_close_gate_reviewers",
    ),
    "shadow-run-register": _contract(
        "shadow_run_registration", "shadow_run", "shadow_run_registrar",
        evidence=("approved_entity_attempts", "reviewed_handoff"),
        independent_review_role="month_close_gate_reviewers",
    ),
    "shadow-run-verify": _contract(
        "shadow_run_verification", "shadow_run", "control_verifier",
        evidence=("registered_entity_attempts", "append_only_run_ledger"),
    ),
    "shadow-portfolio-assemble": _contract(
        "portfolio_assembly", "shadow_close", "portfolio_preparer",
        evidence=("all_reviewed_entity_reports", "explicit_fx_workpaper"),
        independent_review_role="portfolio_reviewer",
    ),
    "shadow-portfolio-review": _contract(
        "portfolio_review", "shadow_close", "portfolio_reviewer",
        evidence=("assembled_portfolio", "portfolio_evidence_reference"),
        separate_from=("entity_shadow_reviewer", "portfolio_preparer"),
    ),
    "shadow-portfolio-verify": _contract(
        "portfolio_verification", "shadow_close", "control_verifier",
        evidence=("reviewed_portfolio", "all_reviewed_entity_reports"),
    ),
    "shadow-observation-assemble": _contract(
        "observation_assembly", "observation", "observation_preparer",
        evidence=("shadow_run_registration", "all_reviewed_shadow_reports"),
        independent_review_role="observation_control_reviewer",
    ),
    "shadow-observation-review": _contract(
        "observation_review", "observation", "observation_control_reviewer",
        evidence=("assembled_observation", "observation_evidence_reference"),
        separate_from=(
            "pipeline_operator", "entity_shadow_reviewer", "portfolio_reviewer",
            "observation_preparer",
        ),
    ),
    "shadow-observation-verify": _contract(
        "observation_verification", "observation", "control_verifier",
        evidence=("reviewed_observation", "current_source_evidence"),
    ),
    "shadow-period-archive": _contract(
        "period_archive", "continuity", "continuity_preparer",
        evidence=("verified_observation", "verified_period_source_bundle"),
        independent_review_role="continuity_reviewer",
    ),
    "next-period-workspace-verify": _contract(
        "monthly_workspace_verification", "continuity", "control_verifier",
        evidence=("previous_period_archive", "current_monthly_workspace"),
    ),
    "period-runbook-status": _contract(
        "runbook_status", "continuity", "control_verifier",
        evidence=("append_only_runbook",),
    ),
    "period-runbook-verify": _contract(
        "runbook_verification", "continuity", "control_verifier",
        evidence=("append_only_runbook", "monthly_step_definition"),
    ),
    "activation-workspace-status": _contract(
        "activation_status_refresh", "continuity", "control_verifier",
        evidence=("current_activation_evidence", "period_archive"),
    ),
}


_ENTITY_CONTRACTS: dict[str, dict[str, Any]] = {
    "shadow-close-template": _contract(
        "entity_baseline_template", "shadow_close", "human_close_baseline_preparer",
        evidence=("entity_scope", "current_period"),
        independent_review_role="entity_shadow_reviewer",
    ),
    "shadow-close-baseline-complete": _contract(
        "entity_human_close_baseline", "shadow_close",
        "human_close_baseline_preparer",
        evidence=("human_close_package", "entity_period_binding"),
        independent_review_role="entity_shadow_reviewer",
    ),
    "shadow-close-compare": _contract(
        "entity_shadow_comparison", "shadow_close", "shadow_close_operator",
        evidence=("human_close_baseline", "deterministic_entity_result"),
        independent_review_role="entity_shadow_reviewer",
    ),
    "shadow-close-review": _contract(
        "entity_shadow_review", "shadow_close", "entity_shadow_reviewer",
        evidence=("entity_comparison", "difference_resolution_evidence"),
        separate_from=("human_close_baseline_preparer", "shadow_close_operator"),
    ),
    "shadow-close-verify": _contract(
        "entity_shadow_verification", "shadow_close", "control_verifier",
        evidence=("reviewed_entity_report", "entity_period_binding"),
    ),
}


_EXPECTED_ACTIONS = {
    "pilot-readiness-complete": "edit_private_json",
    "pilot-readiness-review": "run_cli",
    "pilot-readiness-verify": "run_cli",
    "data-handoff-init": "run_cli",
    "data-handoff-complete": "edit_private_json",
    "data-handoff-review": "run_cli",
    "data-handoff-verify": "run_cli",
    "pipeline-attempts-complete": "complete_external_prerequisite",
    "shadow-run-register": "run_cli",
    "shadow-run-verify": "run_cli",
    "shadow-portfolio-assemble": "run_cli",
    "shadow-portfolio-review": "run_cli",
    "shadow-portfolio-verify": "run_cli",
    "shadow-observation-assemble": "run_cli",
    "shadow-observation-review": "run_cli",
    "shadow-observation-verify": "run_cli",
    "shadow-period-archive": "run_cli",
    "next-period-workspace-verify": "run_cli",
    "period-runbook-status": "run_cli",
    "period-runbook-verify": "run_cli",
    "activation-workspace-status": "run_cli",
    "shadow-close-template": "run_cli",
    "shadow-close-baseline-complete": "edit_private_workbook",
    "shadow-close-compare": "run_cli",
    "shadow-close-review": "run_cli",
    "shadow-close-verify": "run_cli",
}


def _playbook(
    work_product: str,
    *,
    checks: Sequence[str],
    stop_conditions: Sequence[str],
) -> dict[str, Any]:
    return {
        "work_product_type_id": work_product,
        "operator_checklist_type_ids": list(checks),
        "stop_condition_type_ids": list(stop_conditions),
    }


_PLAYBOOKS: dict[str, dict[str, Any]] = {
    "pilot_readiness_workpaper": _playbook(
        "completed_readiness_workpaper",
        checks=(
            "confirm_current_box_and_period",
            "confirm_legal_entity_scope",
            "confirm_bounded_read_only_connector_scope",
        ),
        stop_conditions=(
            "stop_on_box_or_period_mismatch",
            "stop_on_entity_scope_mismatch",
            "stop_if_credentials_or_private_paths_would_be_exposed",
        ),
    ),
    "pilot_readiness_review": _playbook(
        "reviewed_readiness_workpaper",
        checks=(
            "compare_workpaper_to_source_evidence",
            "confirm_required_roles_are_separate",
            "record_only_opaque_evidence_references",
        ),
        stop_conditions=(
            "stop_on_role_overlap",
            "stop_on_missing_or_stale_source_evidence",
            "stop_on_box_or_period_mismatch",
        ),
    ),
    "pilot_readiness_verification": _playbook(
        "readiness_verification_result",
        checks=(
            "confirm_current_box_and_period",
            "confirm_current_tax_and_connector_evidence",
            "refresh_from_authoritative_verifiers",
        ),
        stop_conditions=(
            "stop_on_box_or_period_mismatch",
            "stop_on_missing_or_stale_source_evidence",
        ),
    ),
    "data_handoff_initialization": _playbook(
        "data_handoff_workspace",
        checks=(
            "confirm_current_box_and_period",
            "confirm_data_custody_and_access",
            "confirm_required_roles_are_separate",
        ),
        stop_conditions=(
            "stop_on_role_overlap",
            "stop_on_entity_scope_mismatch",
            "stop_if_credentials_or_private_paths_would_be_exposed",
        ),
    ),
    "data_handoff_inventory": _playbook(
        "completed_handoff_workpaper",
        checks=(
            "confirm_source_inventory_completeness",
            "confirm_data_custody_and_access",
            "preserve_source_immutability",
        ),
        stop_conditions=(
            "stop_on_incomplete_entity_coverage",
            "stop_on_missing_or_stale_source_evidence",
            "stop_if_credentials_or_private_paths_would_be_exposed",
        ),
    ),
    "data_handoff_review": _playbook(
        "reviewed_handoff",
        checks=(
            "compare_workpaper_to_source_evidence",
            "confirm_required_roles_are_separate",
            "record_only_opaque_evidence_references",
        ),
        stop_conditions=(
            "stop_on_role_overlap",
            "stop_on_incomplete_entity_coverage",
            "stop_on_missing_or_stale_source_evidence",
        ),
    ),
    "data_handoff_verification": _playbook(
        "data_handoff_verification_result",
        checks=(
            "confirm_current_box_and_period",
            "confirm_source_inventory_completeness",
            "refresh_from_authoritative_verifiers",
        ),
        stop_conditions=(
            "stop_on_box_or_period_mismatch",
            "stop_on_incomplete_entity_coverage",
        ),
    ),
    "entity_pipeline_attempts": _playbook(
        "one_approved_attempt_per_entity",
        checks=(
            "confirm_one_approved_run_per_entity",
            "confirm_gate_reviews_before_registration",
            "confirm_ledger_binding_and_integrity",
        ),
        stop_conditions=(
            "stop_on_incomplete_entity_coverage",
            "stop_on_role_overlap",
            "stop_on_ledger_or_hash_chain_mismatch",
        ),
    ),
    "shadow_run_registration": _playbook(
        "shadow_run_registration",
        checks=(
            "confirm_one_approved_run_per_entity",
            "confirm_gate_reviews_before_registration",
            "confirm_current_box_and_period",
        ),
        stop_conditions=(
            "stop_on_incomplete_entity_coverage",
            "stop_on_box_or_period_mismatch",
            "stop_on_ledger_or_hash_chain_mismatch",
        ),
    ),
    "shadow_run_verification": _playbook(
        "shadow_run_verification_result",
        checks=(
            "confirm_ledger_binding_and_integrity",
            "confirm_one_approved_run_per_entity",
            "refresh_from_authoritative_verifiers",
        ),
        stop_conditions=(
            "stop_on_ledger_or_hash_chain_mismatch",
            "stop_on_incomplete_entity_coverage",
        ),
    ),
    "entity_baseline_template": _playbook(
        "human_close_baseline_template",
        checks=(
            "confirm_legal_entity_scope",
            "confirm_current_box_and_period",
            "keep_human_baseline_independent",
        ),
        stop_conditions=(
            "stop_on_entity_scope_mismatch",
            "stop_on_box_or_period_mismatch",
        ),
    ),
    "entity_human_close_baseline": _playbook(
        "human_close_baseline",
        checks=(
            "keep_human_baseline_independent",
            "confirm_source_inventory_completeness",
            "preserve_source_immutability",
        ),
        stop_conditions=(
            "stop_on_missing_or_stale_source_evidence",
            "stop_on_entity_scope_mismatch",
            "stop_before_posting_payment_or_filing",
        ),
    ),
    "entity_shadow_comparison": _playbook(
        "entity_comparison",
        checks=(
            "compare_at_same_scope_and_tolerance",
            "confirm_current_box_and_period",
            "preserve_source_immutability",
        ),
        stop_conditions=(
            "stop_on_box_or_period_mismatch",
            "stop_on_entity_scope_mismatch",
            "stop_on_missing_or_stale_source_evidence",
        ),
    ),
    "entity_shadow_review": _playbook(
        "reviewed_entity_report",
        checks=(
            "resolve_each_difference_with_evidence",
            "confirm_required_roles_are_separate",
            "confirm_report_matches_entity_and_period",
        ),
        stop_conditions=(
            "stop_on_unresolved_difference",
            "stop_on_role_overlap",
            "stop_on_entity_scope_mismatch",
        ),
    ),
    "entity_shadow_verification": _playbook(
        "entity_report_verification_result",
        checks=(
            "confirm_report_matches_entity_and_period",
            "confirm_ledger_binding_and_integrity",
            "refresh_from_authoritative_verifiers",
        ),
        stop_conditions=(
            "stop_on_entity_scope_mismatch",
            "stop_on_ledger_or_hash_chain_mismatch",
            "stop_on_unresolved_difference",
        ),
    ),
    "portfolio_assembly": _playbook(
        "assembled_portfolio",
        checks=(
            "confirm_all_entity_reports_present",
            "confirm_explicit_fx_source",
            "confirm_report_matches_entity_and_period",
        ),
        stop_conditions=(
            "stop_on_incomplete_entity_coverage",
            "stop_on_missing_or_stale_source_evidence",
            "stop_on_unresolved_difference",
        ),
    ),
    "portfolio_review": _playbook(
        "reviewed_portfolio",
        checks=(
            "confirm_all_entity_reports_present",
            "confirm_portfolio_reviewer_is_independent",
            "record_only_opaque_evidence_references",
        ),
        stop_conditions=(
            "stop_on_role_overlap",
            "stop_on_incomplete_entity_coverage",
            "stop_on_unresolved_difference",
        ),
    ),
    "portfolio_verification": _playbook(
        "portfolio_verification_result",
        checks=(
            "confirm_all_entity_reports_present",
            "confirm_explicit_fx_source",
            "refresh_from_authoritative_verifiers",
        ),
        stop_conditions=(
            "stop_on_incomplete_entity_coverage",
            "stop_on_ledger_or_hash_chain_mismatch",
        ),
    ),
    "observation_assembly": _playbook(
        "assembled_observation",
        checks=(
            "confirm_observation_covers_registered_runs",
            "confirm_all_entity_reports_present",
            "confirm_current_box_and_period",
        ),
        stop_conditions=(
            "stop_on_incomplete_entity_coverage",
            "stop_on_ledger_or_hash_chain_mismatch",
            "stop_on_box_or_period_mismatch",
        ),
    ),
    "observation_review": _playbook(
        "reviewed_observation",
        checks=(
            "confirm_observation_covers_registered_runs",
            "confirm_observation_reviewer_is_independent",
            "record_only_opaque_evidence_references",
        ),
        stop_conditions=(
            "stop_on_role_overlap",
            "stop_on_unresolved_difference",
            "stop_on_ledger_or_hash_chain_mismatch",
        ),
    ),
    "observation_verification": _playbook(
        "observation_verification_result",
        checks=(
            "confirm_observation_covers_registered_runs",
            "confirm_current_source_evidence",
            "refresh_from_authoritative_verifiers",
        ),
        stop_conditions=(
            "stop_on_missing_or_stale_source_evidence",
            "stop_on_ledger_or_hash_chain_mismatch",
        ),
    ),
    "period_archive": _playbook(
        "period_archive",
        checks=(
            "confirm_current_source_evidence",
            "confirm_ledger_binding_and_integrity",
            "confirm_natural_month_continuity",
        ),
        stop_conditions=(
            "stop_on_missing_or_stale_source_evidence",
            "stop_on_ledger_or_hash_chain_mismatch",
            "stop_on_nonconsecutive_period",
        ),
    ),
    "monthly_workspace_verification": _playbook(
        "monthly_workspace_verification_result",
        checks=(
            "confirm_previous_period_archive",
            "confirm_natural_month_continuity",
            "refresh_from_authoritative_verifiers",
        ),
        stop_conditions=(
            "stop_on_nonconsecutive_period",
            "stop_on_ledger_or_hash_chain_mismatch",
            "stop_on_box_or_period_mismatch",
        ),
    ),
    "runbook_status": _playbook(
        "reported_progress_snapshot",
        checks=(
            "confirm_runbook_chain_integrity",
            "confirm_current_box_and_period",
            "keep_operator_report_non_authoritative",
        ),
        stop_conditions=(
            "stop_on_ledger_or_hash_chain_mismatch",
            "stop_on_box_or_period_mismatch",
        ),
    ),
    "runbook_verification": _playbook(
        "verified_runbook_chain",
        checks=(
            "confirm_runbook_chain_integrity",
            "confirm_monthly_step_definition",
            "keep_operator_report_non_authoritative",
        ),
        stop_conditions=(
            "stop_on_ledger_or_hash_chain_mismatch",
            "stop_on_box_or_period_mismatch",
        ),
    ),
    "activation_status_refresh": _playbook(
        "activation_status_snapshot",
        checks=(
            "refresh_from_authoritative_verifiers",
            "confirm_previous_period_archive",
            "keep_operator_report_non_authoritative",
        ),
        stop_conditions=(
            "stop_on_ledger_or_hash_chain_mismatch",
            "stop_before_posting_payment_or_filing",
        ),
    ),
}


def _resolve_contract(
    step_id: str, action: str, entity_ids: frozenset[str],
) -> tuple[dict[str, Any], str | None]:
    contract = _EXACT_CONTRACTS.get(step_id)
    entity_id: str | None = None
    contract_key = step_id
    if contract is None:
        contract_key, separator, entity_id = step_id.partition(":")
        contract = _ENTITY_CONTRACTS.get(contract_key) if separator else None
        if contract is None or entity_id not in entity_ids:
            raise PilotShadowPeriodTaskError(
                "monthly operator task is not covered by the safe task contract"
            )
    if _EXPECTED_ACTIONS.get(contract_key) != action:
        raise PilotShadowPeriodTaskError(
            "monthly operator task action does not match the safe task contract"
        )
    return contract, entity_id


def project_period_operator_tasks(
    steps: Sequence[Mapping[str, Any]], *, entity_ids: Sequence[str],
) -> list[dict[str, Any]]:
    """Project private monthly steps into a command-free operator task queue."""
    known_entities = frozenset(entity_ids)
    projected: list[dict[str, Any]] = []
    next_open_seen = False
    for step in steps:
        step_id = step.get("step_id")
        action = step.get("action")
        outcome = step.get("reported_outcome")
        if (
            not isinstance(step_id, str)
            or not isinstance(action, str)
            or outcome not in {
                "not_reported", "reported_complete", "reported_failed",
                "blocked", "deferred",
            }
        ):
            raise PilotShadowPeriodTaskError(
                "monthly operator task status is invalid"
            )
        contract, entity_id = _resolve_contract(
            step_id, action, known_entities,
        )
        playbook = _PLAYBOOKS.get(contract["task_type"])
        if playbook is None:
            raise PilotShadowPeriodTaskError(
                "monthly operator task has no safe method playbook"
            )
        actionable_now = not next_open_seen and outcome != "reported_complete"
        if outcome != "reported_complete":
            next_open_seen = True
        if outcome == "reported_complete":
            work_status = "reported_complete"
        elif outcome == "blocked":
            work_status = "reported_blocked"
        elif outcome == "reported_failed":
            work_status = "reported_failed"
        elif outcome == "deferred":
            work_status = "reported_deferred"
        elif actionable_now:
            work_status = "ready_to_work"
        else:
            work_status = "waiting_on_prior_task"
        projected.append({
            "step_id": step_id,
            **contract,
            "guidance_version": 1,
            **playbook,
            "entity_id": entity_id,
            "completion_channel": {
                "run_cli": "cli",
                "edit_private_json": "private_workspace",
                "edit_private_workbook": "private_workspace",
                "complete_external_prerequisite": "controlled_prerequisite",
            }[action],
            "reported_outcome": outcome,
            "work_status": work_status,
            "actionable_now": actionable_now,
            "event_count": int(step.get("event_count") or 0),
            "authoritative_completion": False,
            "completion_is_operator_report_only": True,
            "authoritative_verifier_required": True,
            "browser_action_available": False,
            "command_returned": False,
            "private_path_returned": False,
            "evidence_reference_returned": False,
        })
    return projected
