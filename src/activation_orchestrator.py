from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from .box_runtime import BoxRuntime
from .pack_services import PackServiceRegistry


_STAGE_CONTRACTS = (
    {
        "stage_id": "pack_contracts",
        "phase": "foundation",
        "depends_on": [],
        "operator_role": "box_platform_operator",
        "independent_review_role": "pack_contract_reviewer",
        "commands": ["opc-finance-box pack-audit"],
    },
    {
        "stage_id": "tax_rule_sources",
        "phase": "foundation",
        "depends_on": ["pack_contracts"],
        "operator_role": "tax_pack_maintainer",
        "independent_review_role": "local_tax_source_reviewer",
        "commands": [
            "opc-finance-box doctor BOX.json --as-of YYYY-MM-DD",
        ],
    },
    {
        "stage_id": "tax_applicability",
        "phase": "real_company_activation",
        "depends_on": ["tax_rule_sources"],
        "operator_role": "entity_tax_preparer",
        "independent_review_role": "local_tax_advisor",
        "commands": [
            "opc-finance-box tax-applicability-init BOX.json --entity ENTITY_ID --facts-as-of YYYY-MM-DD --prepared-by TAX_PREPARER --output WORKPAPER.json",
            "opc-finance-box tax-applicability-review BOX.json WORKPAPER.json --decision approved-in-scope --actor LOCAL_TAX_REVIEWER --rationale RATIONALE --evidence-reference EVIDENCE_REFERENCE --output REVIEWED.json",
            "opc-finance-box tax-applicability-import BOX.json REVIEWED.json --review-dir PRIVATE_REVIEW_DIR --as-of YYYY-MM-DD",
            "opc-finance-box tax-applicability-registry-seal BOX.json --review-dir PRIVATE_REVIEW_DIR --actor REGISTRY_CONTROLLER --as-of YYYY-MM-DD --output REGISTRY_RECEIPT.json",
        ],
    },
    {
        "stage_id": "connector_configuration",
        "phase": "real_company_activation",
        "depends_on": ["pack_contracts"],
        "operator_role": "connector_operator",
        "independent_review_role": "connector_security_reviewer",
        "commands": [
            "opc-finance-box production-readiness BOX.json --as-of YYYY-MM-DD",
        ],
    },
    {
        "stage_id": "connector_shadow_evidence",
        "phase": "real_company_activation",
        "depends_on": ["connector_configuration"],
        "operator_role": "connector_baseline_preparer",
        "independent_review_role": "connector_shadow_reviewer",
        "commands": [
            "opc-finance-box connector-shadow-baseline-init BOX.json --pipeline PIPELINE_ID --entity ENTITY_ID --period YYYY-MM --prepared-by BASELINE_PREPARER --output BASELINE.json",
            "opc-finance-box connector-shadow-assess BOX.json BASELINE.json PIPELINE_RESULT.json --output ASSESSMENT.json",
            "opc-finance-box connector-shadow-review BOX.json ASSESSMENT.json --decision passed --actor SHADOW_REVIEWER --rationale RATIONALE --evidence-reference EVIDENCE_REFERENCE --output REVIEWED.json",
            "opc-finance-box connector-shadow-status BOX.json --review-dir PRIVATE_REVIEW_DIR --as-of YYYY-MM-DD",
        ],
    },
    {
        "stage_id": "pilot_readiness",
        "phase": "bounded_shadow",
        "depends_on": ["tax_applicability", "connector_shadow_evidence"],
        "operator_role": "pilot_finance_preparer",
        "independent_review_role": "pilot_control_reviewer",
        "commands": [
            "opc-finance-box pilot-readiness-init BOX.json --period YYYY-MM --prepared-by PILOT_PREPARER --output WORKPAPER.json",
            "opc-finance-box pilot-readiness-review BOX.json WORKPAPER.json --actor PILOT_REVIEWER --rationale RATIONALE --evidence-reference EVIDENCE_REFERENCE --output REVIEWED.json",
            "opc-finance-box pilot-readiness-verify BOX.json REVIEWED.json --as-of YYYY-MM-DD",
        ],
    },
    {
        "stage_id": "data_handoff",
        "phase": "bounded_shadow",
        "depends_on": ["pilot_readiness"],
        "operator_role": "data_handoff_preparer",
        "independent_review_role": "data_access_reviewer",
        "commands": [
            "opc-finance-box pilot-data-handoff-init BOX.json PILOT_REVIEWED.json --prepared-by HANDOFF_PREPARER --custodian-principal DATA_CUSTODIAN --as-of YYYY-MM-DD --output WORKPAPER.json",
            "opc-finance-box pilot-data-handoff-review BOX.json WORKPAPER.json PILOT_REVIEWED.json --actor HANDOFF_REVIEWER --rationale RATIONALE --evidence-reference EVIDENCE_REFERENCE --output REVIEWED.json",
            "opc-finance-box pilot-data-handoff-verify BOX.json REVIEWED.json PILOT_REVIEWED.json --as-of YYYY-MM-DD",
        ],
    },
    {
        "stage_id": "shadow_run_registration",
        "phase": "bounded_shadow",
        "depends_on": ["data_handoff"],
        "operator_role": "pipeline_operator",
        "independent_review_role": "month_close_gate_reviewers",
        "commands": [
            "opc-finance-box pilot-shadow-run-register BOX.json HANDOFF_REVIEWED.json PILOT_REVIEWED.json --entity-attempt ENTITY_ID=ATTEMPT_ID --actor REGISTRAR --rationale RATIONALE --evidence-reference EVIDENCE_REFERENCE --runs-root PRIVATE_RUNS_ROOT --output REGISTRATION.json",
            "opc-finance-box pilot-shadow-run-verify BOX.json REGISTRATION.json HANDOFF_REVIEWED.json PILOT_REVIEWED.json --runs-root PRIVATE_RUNS_ROOT --as-of YYYY-MM-DD",
        ],
    },
    {
        "stage_id": "shadow_observation",
        "phase": "observation",
        "depends_on": ["shadow_run_registration"],
        "operator_role": "observation_preparer",
        "independent_review_role": "observation_control_reviewer",
        "commands": [
            "opc-finance-box pilot-shadow-observation-assemble BOX.json REGISTRATION.json HANDOFF_REVIEWED.json PILOT_REVIEWED.json --entity-report ENTITY_REVIEWED_REPORT.json --runs-root PRIVATE_RUNS_ROOT --output OBSERVATION.json",
            "opc-finance-box pilot-shadow-observation-review BOX.json OBSERVATION.json --decision passed --actor OBSERVATION_REVIEWER --rationale RATIONALE --evidence-reference EVIDENCE_REFERENCE --output REVIEWED.json",
            "opc-finance-box pilot-shadow-observation-verify BOX.json REVIEWED.json REGISTRATION.json HANDOFF_REVIEWED.json PILOT_REVIEWED.json --entity-report ENTITY_REVIEWED_REPORT.json --runs-root PRIVATE_RUNS_ROOT --as-of YYYY-MM-DD",
        ],
    },
    {
        "stage_id": "consecutive_shadow_series",
        "phase": "observation",
        "depends_on": ["shadow_observation"],
        "operator_role": "continuity_preparer",
        "independent_review_role": "continuity_reviewer",
        "commands": [
            "opc-finance-box pilot-shadow-period-archive BOX.json REVIEWED_OBSERVATION.json REGISTRATION.json HANDOFF_REVIEWED.json PILOT_REVIEWED.json --entity-report ENTITY_REVIEWED_REPORT.json --evidence-root PRIVATE_SERIES_ROOT --runs-root PRIVATE_RUNS_ROOT",
            "opc-finance-box pilot-shadow-next-period-init BOX.json PRIVATE_ACTIVATION_ROOT --prepared-by PERIOD_PREPARER --facts-as-of YYYY-MM-DD",
            "opc-finance-box pilot-shadow-next-period-verify BOX.json PRIVATE_ACTIVATION_ROOT YYYY-MM --as-of YYYY-MM-DD",
            "opc-finance-box pilot-shadow-period-runbook-status BOX.json PRIVATE_ACTIVATION_ROOT YYYY-MM",
            "opc-finance-box pilot-shadow-period-runbook-verify BOX.json PRIVATE_ACTIVATION_ROOT YYYY-MM",
            "opc-finance-box pilot-shadow-series-assemble BOX.json PRIVATE_SERIES_ROOT --runs-root PRIVATE_RUNS_ROOT --as-of YYYY-MM-DD --output SERIES.json",
            "opc-finance-box pilot-shadow-series-review BOX.json SERIES.json --decision approved-for-promotion-evidence --actor CONTINUITY_REVIEWER --rationale RATIONALE --evidence-reference EVIDENCE_REFERENCE --output REVIEWED.json",
            "opc-finance-box pilot-shadow-series-verify BOX.json REVIEWED.json PRIVATE_SERIES_ROOT --runs-root PRIVATE_RUNS_ROOT --as-of YYYY-MM-DD",
        ],
    },
    {
        "stage_id": "stable_promotion",
        "phase": "release",
        "depends_on": ["consecutive_shadow_series"],
        "operator_role": "promotion_evidence_preparer",
        "independent_review_role": "release_reviewer",
        "commands": [
            "opc-finance-box promotion-assess BOX.json PROMOTION_EVIDENCE.json",
            "opc-finance-box promotion-record BOX.json PROMOTION_EVIDENCE.json --promotion-root PRIVATE_PROMOTION_LEDGER --actor EVIDENCE_PREPARER",
            "opc-finance-box promotion-review BOX.json ASSESSMENT_ID --promotion-root PRIVATE_PROMOTION_LEDGER --actor RELEASE_REVIEWER --decision approved --rationale RATIONALE --evidence-reference EVIDENCE_REFERENCE",
        ],
    },
)


def build_activation_stage_contracts() -> dict[str, dict[str, Any]]:
    """Return the immutable, secret-free operator contract for every readiness stage."""
    return {
        item["stage_id"]: {
            **item,
            "depends_on": list(item["depends_on"]),
            "commands": list(item["commands"]),
            "command_templates_only": True,
            "credentials_accepted": False,
            "external_actions_performed": False,
        }
        for item in _STAGE_CONTRACTS
    }


def project_activation_stages(
    readiness_stages: list[dict[str, Any]],
    stage_contracts: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Project authoritative gate results into a dependency-aware operator queue."""
    contracts = dict(stage_contracts or build_activation_stage_contracts())
    gate_by_id = {
        str(item["stage_id"]): bool(item.get("gate_passed"))
        for item in readiness_stages
    }
    expected = set(contracts)
    actual = set(gate_by_id)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        raise ValueError(
            f"activation stage contract mismatch: missing={missing}, unknown={unknown}"
        )

    projected = []
    for stage in readiness_stages:
        stage_id = str(stage["stage_id"])
        contract = contracts[stage_id]
        dependencies = list(contract["depends_on"])
        unmet = [item for item in dependencies if not gate_by_id.get(item, False)]
        if stage.get("gate_passed") is True:
            work_status = "completed"
        elif not unmet:
            work_status = "ready_to_work"
        else:
            work_status = "blocked_by_dependency"
        projected.append({
            "stage_order": stage["stage_order"],
            "stage_id": stage_id,
            "display_name": stage["display_name"],
            "phase": contract["phase"],
            "work_status": work_status,
            "evidence_status": stage["status"],
            "gate_passed": stage.get("gate_passed") is True,
            "evidence_complete": stage.get("evidence_complete") is True,
            "depends_on": dependencies,
            "unmet_dependency_ids": unmet,
            "operator_role": contract["operator_role"],
            "independent_review_role": contract["independent_review_role"],
            "command_templates": list(contract["commands"]),
            "required_evidence": stage["required_evidence"],
            "external_actions_performed": False,
        })
    return projected


def build_activation_workspace(
    runtime: BoxRuntime,
    services: PackServiceRegistry,
    *,
    runs_root: str | Path,
    environ: Mapping[str, str] | None = None,
    as_of: str | None = None,
) -> dict[str, Any]:
    """Build the first-customer operator queue from the readiness source of truth."""
    from .activation_workspace import build_activation_workspace_contract
    from .production_readiness import build_production_readiness_workspace

    readiness = build_production_readiness_workspace(
        runtime,
        services,
        runs_root=runs_root,
        environ=environ,
        as_of=as_of,
    )
    stages = project_activation_stages(readiness["stages"])
    current_wave = [
        item for item in stages if item["work_status"] == "ready_to_work"
    ]
    return {
        "schema_version": 1,
        "artifact_type": "first_customer_activation_workspace",
        "runtime_fingerprint": readiness["runtime_fingerprint"],
        "as_of": readiness["as_of"],
        "summary": {
            "stage_count": len(stages),
            "completed_stage_count": sum(
                item["work_status"] == "completed" for item in stages
            ),
            "current_wave_stage_count": len(current_wave),
            "blocked_stage_count": sum(
                item["work_status"] == "blocked_by_dependency" for item in stages
            ),
            "current_wave_stage_ids": [item["stage_id"] for item in current_wave],
            "activation_workflow_complete": all(
                item["work_status"] == "completed" for item in stages
            ),
            "ready_for_bounded_shadow": readiness["summary"][
                "ready_for_bounded_shadow"
            ],
            "ready_for_stable_promotion": readiness["summary"][
                "ready_for_stable_promotion"
            ],
            "ready_for_external_filing": False,
        },
        "current_wave": [{
            "stage_id": item["stage_id"],
            "display_name": item["display_name"],
            "operator_role": item["operator_role"],
            "independent_review_role": item["independent_review_role"],
            "recommended_command": item["command_templates"][0],
            "evidence_status": item["evidence_status"],
        } for item in current_wave],
        "workspace_initialization": build_activation_workspace_contract(),
        "stages": stages,
        "control_boundary": {
            "readiness_source": "production_readiness_workspace",
            "configured_private_paths_returned": False,
            "credential_values_returned": False,
            "private_artifact_contents_returned": False,
            "actors_returned": False,
            "evidence_references_returned": False,
            "financial_values_returned": False,
            "commands_are_templates_only": True,
            "commands_executed": False,
            "stable_promotion_performed": False,
            "external_filing_authorized": False,
            "external_actions_performed": False,
        },
    }
