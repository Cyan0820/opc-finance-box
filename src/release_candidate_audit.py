from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from .box_builder import (
    build_box_candidate_bundle,
    build_box_starter_catalog,
    preview_box_candidate,
)
from .box_config import load_pack_catalog
from .box_eval import run_box_eval_suite
from .deployment_assets import verify_deployment_assets
from .distribution_verify import verify_wheel
from .handoff_verify import _verify_bundle_body
from .pack_audit import audit_pack_catalog
from .resource_paths import find_resource_root
from .source_kit import verify_source_kit_bundle


class ReleaseCandidateAuditError(ValueError):
    """Raised when the installed product cannot produce a trustworthy RC audit."""


def _canonical_fingerprint(value: Any) -> str:
    body = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def _profile_entry(
    entries: list[dict[str, Any]], profile_id: str, *, preferred_country: str = "US",
) -> dict[str, Any]:
    matches = [item for item in entries if item["profile_id"] == profile_id]
    if not matches:
        raise ReleaseCandidateAuditError(f"Starter profile has no installed country: {profile_id}")
    return next(
        (item for item in matches if item["country_code"] == preferred_country),
        matches[0],
    )


def _audit_starter_handoffs(
    entries: list[dict[str, Any]], packs_root: Path,
) -> tuple[list[dict[str, Any]], str]:
    results: list[dict[str, Any]] = []
    for entry in entries:
        body, _, manifest = build_box_candidate_bundle(entry["starter_spec"], packs_root)
        verified, _ = _verify_bundle_body(body, packs_root)
        if (
            verified.get("valid") is not True
            or verified.get("archive_bytes_match_current_builder") is not True
            or verified.get("runtime_fingerprint") != manifest.get("runtime_fingerprint")
            or verified.get("secret_values_included") is not False
            or verified.get("external_actions_performed") is not False
        ):
            raise ReleaseCandidateAuditError(
                f"Starter handoff failed deterministic verification: {entry['id']}"
            )
        results.append({
            "starter_id": entry["id"],
            "profile_id": entry["profile_id"],
            "country_code": entry["country_code"],
            "jurisdiction_id": entry["jurisdiction_id"],
            "runtime_fingerprint": verified["runtime_fingerprint"],
            "bundle_sha256": verified["bundle_sha256"],
            "member_count": verified["member_count"],
            "manifest_file_count": verified["manifest_file_count"],
            "passed": True,
        })
    return results, _canonical_fingerprint(results)


def _audit_integration_variants(
    starter_catalog: dict[str, Any], packs_root: Path,
) -> tuple[list[dict[str, Any]], str]:
    entries = starter_catalog["entries"]
    profile_ids = sorted({item["profile_id"] for item in entries})
    results: list[dict[str, Any]] = []
    for profile_id in profile_ids:
        entry = _profile_entry(entries, profile_id)
        baseline = preview_box_candidate(entry["starter_spec"], packs_root)
        baseline_pack_ids = {
            *baseline["config"].get("connectors", []),
            *baseline["config"].get("features", []),
        }
        for integration_id in entry["allowed_integrations"]:
            spec = deepcopy(entry["starter_spec"])
            spec["name"] = f"RC {profile_id} {integration_id}"
            spec["integrations"] = [integration_id]
            preview = preview_box_candidate(spec, packs_root)
            enabled_pack_ids = sorted({
                *preview["config"].get("connectors", []),
                *preview["config"].get("features", []),
            } - baseline_pack_ids)
            if not enabled_pack_ids:
                raise ReleaseCandidateAuditError(
                    f"Integration did not enable a Connector or Feature: {profile_id}.{integration_id}"
                )
            results.append({
                "profile_id": profile_id,
                "country_code": entry["country_code"],
                "integration_id": integration_id,
                "enabled_pack_ids": enabled_pack_ids,
                "runtime_fingerprint": preview["candidate"]["runtime_fingerprint"],
                "pipeline_count": len(preview["candidate"]["pipelines"]),
                "passed": True,
            })
    return results, _canonical_fingerprint(results)


def _audit_multi_entity_variants(
    starter_catalog: dict[str, Any], packs_root: Path,
) -> tuple[list[dict[str, Any]], str]:
    entries = starter_catalog["entries"]
    profile_ids = sorted({item["profile_id"] for item in entries})
    results: list[dict[str, Any]] = []
    for profile_id in profile_ids:
        matches = [item for item in entries if item["profile_id"] == profile_id]
        if len(matches) < 2:
            raise ReleaseCandidateAuditError(
                f"Multi-entity RC audit requires two countries for profile: {profile_id}"
            )
        first, second = matches[:2]
        spec = deepcopy(first["starter_spec"])
        spec["name"] = f"RC {profile_id} multi-entity"
        spec["entities"] = [
            deepcopy(first["starter_spec"]["entities"][0]),
            deepcopy(second["starter_spec"]["entities"][0]),
        ]
        spec["features"] = ["feature.multi_entity"]
        preview = preview_box_candidate(spec, packs_root)
        config = preview["config"]
        if (
            len(config.get("entities") or []) != 2
            or "feature.multi_entity" not in set(config.get("features") or [])
        ):
            raise ReleaseCandidateAuditError(
                f"Multi-entity Starter did not preserve two statutory entities: {profile_id}"
            )
        results.append({
            "profile_id": profile_id,
            "country_codes": [first["country_code"], second["country_code"]],
            "entity_count": 2,
            "runtime_fingerprint": preview["candidate"]["runtime_fingerprint"],
            "pipeline_count": len(preview["candidate"]["pipelines"]),
            "passed": True,
        })
    return results, _canonical_fingerprint(results)


def audit_release_candidate(
    packs_root: str | Path,
    *,
    project_root: str | Path | None = None,
    wheel: str | Path | None = None,
    source_kit: str | Path | None = None,
) -> dict[str, Any]:
    """Audit the installed three-profile product matrix without using private data.

    This proves a source/distribution release candidate is reproducible and complete
    across installed Starters. It deliberately cannot prove production maturity,
    tax filing readiness or real-customer evidence.
    """
    packs = Path(packs_root).resolve()
    root = (
        Path(project_root).resolve()
        if project_root is not None
        else find_resource_root().resolve()
    )
    catalog = load_pack_catalog(packs)
    pack_audit = audit_pack_catalog(catalog)
    starter_catalog = build_box_starter_catalog(catalog)
    if (
        pack_audit.get("contract_valid") is not True
        or pack_audit.get("complete_implementation") is not True
        or (pack_audit.get("coverage_counts") or {}).get("declared_only") != 0
    ):
        raise ReleaseCandidateAuditError("Pack catalog is not implementation-complete")
    if (
        starter_catalog.get("complete") is not True
        or starter_catalog.get("unavailable_combinations") != []
    ):
        raise ReleaseCandidateAuditError("Installed Starter matrix is incomplete")

    handoffs, handoff_fingerprint = _audit_starter_handoffs(
        starter_catalog["entries"], packs,
    )
    integrations, integration_fingerprint = _audit_integration_variants(
        starter_catalog, packs,
    )
    multi_entity, multi_entity_fingerprint = _audit_multi_entity_variants(
        starter_catalog, packs,
    )
    evaluation = run_box_eval_suite(
        root / "evals" / "core_packs.json", packs, project_root=root,
    )
    if evaluation.get("passed") is not True:
        raise ReleaseCandidateAuditError("Finance boundary Eval suite failed")
    deployment = verify_deployment_assets(root / "deployment")
    if deployment.get("valid") is not True:
        raise ReleaseCandidateAuditError("Deployment asset controls failed")

    artifact_checks: dict[str, Any] = {
        "wheel": {"provided": wheel is not None, "verified": False},
        "source_kit": {"provided": source_kit is not None, "verified": False},
    }
    if wheel is not None:
        verified_wheel = verify_wheel(wheel)
        artifact_checks["wheel"] = {
            "provided": True,
            "verified": verified_wheel["valid"],
            "project_name": verified_wheel["project_name"],
            "version": verified_wheel["version"],
            "member_count": verified_wheel["member_count"],
            "required_member_count": verified_wheel["required_member_count"],
        }
    if source_kit is not None:
        verified_source = verify_source_kit_bundle(source_kit, project_root=root)
        artifact_checks["source_kit"] = {
            "provided": True,
            "verified": verified_source["valid"],
            "member_count": verified_source["member_count"],
            "manifest_file_count": verified_source["manifest_file_count"],
            "content_fingerprint": verified_source["content_fingerprint"],
            "reproducible_from_installed_source": verified_source[
                "reproducible_from_installed_source"
            ],
        }

    source_tree_passed = all((
        pack_audit["contract_valid"],
        pack_audit["complete_implementation"],
        starter_catalog["complete"],
        len(handoffs) == starter_catalog["ready_combination_count"],
        bool(integrations),
        len(multi_entity) == starter_catalog["profile_count"],
        evaluation["passed"],
        deployment["valid"],
    ))
    artifacts_provided = all(item["provided"] for item in artifact_checks.values())
    artifacts_verified = artifacts_provided and all(
        item["verified"] for item in artifact_checks.values()
    )
    matrix_fingerprint = _canonical_fingerprint({
        "handoffs": handoff_fingerprint,
        "integrations": integration_fingerprint,
        "multi_entity": multi_entity_fingerprint,
        "eval_counts": evaluation["counts"],
        "pack_count": pack_audit["pack_count"],
        "capability_count": pack_audit["capability_count"],
    })
    return {
        "schema_version": 1,
        "artifact_type": "opc_finance_box_release_candidate_audit",
        "passed": source_tree_passed and (not artifacts_provided or artifacts_verified),
        "source_tree_release_candidate": source_tree_passed,
        "release_artifacts_provided": artifacts_provided,
        "release_artifacts_verified": artifacts_verified,
        "matrix_fingerprint": matrix_fingerprint,
        "pack_contracts": {
            "pack_count": pack_audit["pack_count"],
            "capability_count": pack_audit["capability_count"],
            "executable_count": pack_audit["coverage_counts"]["executable"],
            "declared_only_count": pack_audit["coverage_counts"]["declared_only"],
            "complete_implementation": pack_audit["complete_implementation"],
        },
        "starter_matrix": {
            "profile_count": starter_catalog["profile_count"],
            "jurisdiction_count": starter_catalog["jurisdiction_count"],
            "eligible_combination_count": starter_catalog["eligible_combination_count"],
            "verified_handoff_count": len(handoffs),
            "unavailable_combination_count": len(
                starter_catalog["unavailable_combinations"]
            ),
            "fingerprint": handoff_fingerprint,
            "entries": handoffs,
        },
        "integration_matrix": {
            "verified_variant_count": len(integrations),
            "fingerprint": integration_fingerprint,
            "entries": integrations,
        },
        "multi_entity_matrix": {
            "verified_variant_count": len(multi_entity),
            "fingerprint": multi_entity_fingerprint,
            "entries": multi_entity,
        },
        "finance_boundary_eval": evaluation["counts"],
        "deployment_assets": {
            "verified": deployment["valid"],
            "asset_count": deployment["asset_count"],
        },
        "artifact_checks": artifact_checks,
        "maturity_boundary": {
            "technical_release_candidate_only": True,
            "stable_release_ready": False,
            "real_customer_shadow_evidence_verified": False,
            "tax_filing_ready": False,
            "posting_payment_or_filing_authorized": False,
        },
        "control_boundary": {
            "private_financial_data_used": False,
            "credential_values_inspected": False,
            "persistent_workspace_written": False,
            "connector_network_dispatch_performed": False,
            "server_started": False,
            "paths_returned": False,
            "external_actions_performed": False,
        },
    }
