from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import stat
from typing import Any, Mapping

from .activation_orchestrator import build_activation_workspace
from .box_runtime import BoxRuntime
from .connector_access_registry import (
    build_connector_access_alerts,
    build_connector_access_registry,
)
from .connector_shadow_registry import build_connector_shadow_registry_workspace
from .connector_shadow_artifacts import (
    build_connector_shadow_baseline_plan,
    build_connector_shadow_baseline_workpaper,
    validate_connector_shadow_baseline_workpaper,
)
from .pack_services import PackServiceRegistry
from .pilot_readiness import build_pilot_readiness_workpaper
from .tax_applicability_artifacts import (
    build_tax_applicability_workpaper,
    validate_tax_applicability_workpaper,
)


MANIFEST_NAME = "activation-workspace-manifest.json"
ENV_NAME = "artifact-paths.env"
COMMANDS_NAME = "commands.json"
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_FILE_BYTES = 256 * 1024 * 1024
MAX_TOTAL_BYTES = 1024 * 1024 * 1024
MAX_FILES = 5_000

# Production Readiness consumes both single reviewed files and bounded private
# directories.  Keep the directory mounts explicit so an initialized workspace
# can expose later observation/series/promotion evidence without accidentally
# forwarding the runbook or another private directory to a verifier.
READINESS_DIRECTORY_ENV_NAMES = frozenset({
    "OPC_TAX_APPLICABILITY_REVIEW_DIR",
    "OPC_CONNECTOR_SHADOW_REVIEW_DIR",
    "OPC_PILOT_SHADOW_ENTITY_REPORT_DIR",
    "OPC_PILOT_SHADOW_SERIES_EVIDENCE_ROOT",
    "OPC_STABLE_PROMOTION_ROOT",
})

V2_DIRECTORIES = (
    "tax/workpapers",
    "tax/reviewed-source",
    "tax/reviews",
    "connector-shadow/workpapers",
    "connector-shadow/baselines",
    "connector-shadow/assessments",
    "connector-shadow/reviews",
    "pilot/readiness",
    "pilot/handoff",
    "pilot/registrations",
    "pilot/observations",
    "pilot/entity-reports",
    "pilot/series-periods",
    "pipeline-runs",
    "promotion",
)
LEGACY_DIRECTORIES = tuple(
    item for item in V2_DIRECTORIES if item != "connector-shadow/workpapers"
)
V3_DIRECTORIES = (
    "tax/workpapers",
    "tax/reviewed-source",
    "tax/reviews",
    "connector-shadow/workpapers",
    "connector-shadow/baselines",
    "connector-shadow/assessments",
    "connector-shadow/reviews",
    "pilot/readiness",
    "pilot/handoff",
    "pilot/registrations",
    "pilot/shadow-baselines",
    "pilot/shadow-reports",
    "pilot/entity-reports",
    "pilot/portfolio",
    "pilot/observations",
    "pilot/series-periods",
    "pipeline-runs",
    "promotion/evidence",
    "promotion/ledger",
)
DIRECTORIES = (*V3_DIRECTORIES, "runbook")
MANIFEST_SCHEMA_VERSION = 5
COMMAND_STAGE_SEQUENCE = (
    "tax_applicability",
    "connector_shadow_evidence",
    "pilot_readiness",
    "data_handoff",
    "shadow_run_registration",
    "shadow_close_reports",
    "shadow_observation",
    "consecutive_shadow_series",
    "stable_promotion",
)


class ActivationWorkspaceError(RuntimeError):
    """Raised when a first-customer private workspace is unsafe or invalid."""


def build_activation_workspace_contract() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "artifact_type": "first_customer_activation_workspace_contract",
        "init_command": (
            "opc-finance-box activation-init BOX.json ABSOLUTE_NEW_PRIVATE_ROOT "
            "--period YYYY-MM --facts-as-of YYYY-MM-DD --prepared-by PREPARER"
        ),
        "verify_command": (
            "opc-finance-box activation-workspace-verify BOX.json "
            "ABSOLUTE_PRIVATE_ROOT"
        ),
        "status_command": (
            "opc-finance-box activation-workspace-status BOX.json "
            "ABSOLUTE_PRIVATE_ROOT --as-of YYYY-MM-DD"
        ),
        "period_archive_command": (
            "opc-finance-box pilot-shadow-period-archive BOX.json "
            "REVIEWED_OBSERVATION.json REGISTRATION.json HANDOFF_REVIEWED.json "
            "PILOT_REVIEWED.json --entity-report ENTITY_REVIEWED_REPORT.json "
            "--evidence-root PRIVATE_SERIES_ROOT --runs-root PRIVATE_RUNS_ROOT"
        ),
        "next_period_init_command": (
            "opc-finance-box pilot-shadow-next-period-init BOX.json "
            "ABSOLUTE_PRIVATE_ROOT --prepared-by PERIOD_PREPARER "
            "--facts-as-of YYYY-MM-DD"
        ),
        "next_period_verify_command": (
            "opc-finance-box pilot-shadow-next-period-verify BOX.json "
            "ABSOLUTE_PRIVATE_ROOT YYYY-MM --as-of YYYY-MM-DD"
        ),
        "root_constraints": [
            "absolute_path", "must_not_exist", "real_parent_directory",
            "mode_0700", "non_overwriting", "transactional_initialization",
        ],
        "generated_private_assets": [
            "one_unanswered_tax_workpaper_per_entity",
            "one_unanswered_connector_shadow_baseline_workpaper_per_supported_profile_and_entity",
            "one_box_bound_pilot_readiness_workpaper",
            "empty_tax_and_connector_review_rotations",
            "empty_pilot_and_shadow_evidence_directories",
            "secret_free_artifact_path_environment_file",
            "box_bound_command_manifest",
            "full_downstream_shadow_and_promotion_command_chain",
            "verified_single_or_multi_entity_period_archive_command",
            "verified_incremental_next_period_workspace_commands",
            "empty_append_only_operator_runbook",
        ],
        "review_artifacts_created": False,
        "connector_baselines_created": False,
        "credentials_accepted": False,
        "financial_source_files_copied": False,
        "commands_executed": False,
        "external_actions_performed": False,
    }


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")


def _sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _safe_new_root(value: str | Path) -> Path:
    raw = Path(value).expanduser()
    if not raw.is_absolute():
        raise ActivationWorkspaceError("activation workspace root must be absolute")
    if any(ord(character) < 32 or ord(character) == 127 for character in str(raw)):
        raise ActivationWorkspaceError("activation workspace root contains control characters")
    parent = raw.parent
    if parent.is_symlink() or not parent.is_dir():
        raise ActivationWorkspaceError(
            "activation workspace parent must be an existing real directory"
        )
    resolved_parent = parent.resolve()
    if resolved_parent != parent:
        raise ActivationWorkspaceError(
            "activation workspace parent must not traverse symbolic links"
        )
    root = resolved_parent / raw.name
    if root == Path(root.anchor) or root == Path.home().resolve():
        raise ActivationWorkspaceError(
            "activation workspace root must not be a filesystem root or home directory"
        )
    if root.exists() or root.is_symlink():
        raise ActivationWorkspaceError(
            "activation workspace root must not already exist; refusing to overwrite"
        )
    return root


def _existing_root(value: str | Path) -> Path:
    root = Path(value).expanduser()
    if not root.is_absolute():
        raise ActivationWorkspaceError("activation workspace root must be absolute")
    if root.is_symlink() or not root.is_dir():
        raise ActivationWorkspaceError(
            "activation workspace root must be an existing real directory"
        )
    if root.resolve() != root:
        raise ActivationWorkspaceError(
            "activation workspace root must not traverse symbolic links"
        )
    if os.name != "nt" and stat.S_IMODE(root.stat().st_mode) != 0o700:
        raise ActivationWorkspaceError("activation workspace root must use mode 0700")
    return root


def _mkdir_private(path: Path) -> None:
    path.mkdir(mode=0o700)
    if os.name != "nt":
        os.chmod(path, 0o700)


def _write_exclusive(path: Path, body: bytes) -> None:
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise ActivationWorkspaceError(
            f"activation workspace file already exists: {path.name}"
        ) from exc
    try:
        if os.name != "nt":
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise


def _command(step_id: str, purpose: str, argv: list[str]) -> dict[str, Any]:
    return {
        "step_id": step_id,
        "purpose": purpose,
        "action": "run_cli",
        "argv": argv,
        "shell_preview": shlex.join(argv),
        "requires_operator_edit": any(
            "REPLACE_WITH_" in argument for argument in argv
        ),
        "command_executed": False,
    }


def _workspace_paths(root: Path, *, multi_entity: bool) -> dict[str, Path]:
    paths = {
        "OPC_ACTIVATION_WORKSPACE_ROOT": root,
        "OPC_TAX_APPLICABILITY_REVIEW_DIR": root / "tax" / "reviews",
        "OPC_TAX_APPLICABILITY_REGISTRY_RECEIPT": (
            root / "tax" / "registry-receipt.json"
        ),
        "OPC_CONNECTOR_SHADOW_REVIEW_DIR": (
            root / "connector-shadow" / "reviews"
        ),
        "OPC_PILOT_READINESS_REVIEW": (
            root / "pilot" / "readiness" / "reviewed.json"
        ),
        "OPC_PILOT_DATA_HANDOFF_REVIEW": (
            root / "pilot" / "handoff" / "reviewed.json"
        ),
        "OPC_PILOT_SHADOW_RUN_REGISTRATION": (
            root / "pilot" / "registrations" / "first-run.json"
        ),
        "OPC_PILOT_SHADOW_OBSERVATION_REVIEW": (
            root / "pilot" / "observations" / "first-reviewed.json"
        ),
        "OPC_PILOT_SHADOW_ENTITY_REPORT_DIR": (
            root / "pilot" / "entity-reports"
        ),
        "OPC_PILOT_SHADOW_SERIES_REVIEW": (
            root / "pilot" / "observations" / "series-reviewed.json"
        ),
        "OPC_PILOT_SHADOW_SERIES_EVIDENCE_ROOT": (
            root / "pilot" / "series-periods"
        ),
        "OPC_STABLE_PROMOTION_ROOT": root / "promotion" / "ledger",
        "OPC_FINANCE_PIPELINE_RUNS_ROOT": root / "pipeline-runs",
        "OPC_ACTIVATION_RUNBOOK_ROOT": root / "runbook",
    }
    if multi_entity:
        paths["OPC_PILOT_SHADOW_PORTFOLIO_REVIEW"] = (
            root / "pilot" / "observations" / "portfolio-reviewed.json"
        )
    return paths


def _connector_workpaper_specs(
    runtime: BoxRuntime, root: Path, *, shared_access_scopes: bool = True,
) -> list[dict[str, Any]]:
    plan = build_connector_shadow_baseline_plan(runtime)
    specs: list[dict[str, Any]] = []
    for profile in plan["profiles"]:
        for entity_id in profile["entity_ids"]:
            pipeline_id = profile["pipeline_id"]
            slug = pipeline_id.replace(".", "-")
            stem = f"{entity_id}--{slug}"
            if pipeline_id == "dtc.shopify_stripe_month_close":
                access_pack_ids = ["connector.shopify", "connector.stripe"]
            elif pipeline_id == "stripe.daily_close":
                access_pack_ids = ["connector.stripe"]
            elif pipeline_id == "finance.bank_statement_close":
                access_pack_ids = ["connector.wise"]
            elif pipeline_id == "finance.trial_balance_review":
                access_pack_ids = ["connector.xero"]
            elif pipeline_id == "paypal.transaction_close":
                access_pack_ids = ["connector.paypal"]
            elif pipeline_id == "woocommerce.order_refund_close":
                access_pack_ids = ["connector.woocommerce"]
            elif pipeline_id == "commerce.shipbob_fulfillment_close":
                access_pack_ids = ["connector.shipbob"]
            elif pipeline_id == "amazon_seller.marketplace_close":
                access_pack_ids = ["connector.amazon_seller"]
            else:
                access_pack_ids = []
            access_stems = {
                pack_id: (
                    f"{entity_id}--{pack_id.removeprefix('connector.')}"
                    if shared_access_scopes else
                    f"{stem}--{pack_id.removeprefix('connector.')}"
                )
                for pack_id in access_pack_ids
            }
            specs.append({
                "entity_id": entity_id,
                "pipeline_id": pipeline_id,
                "covered_pack_ids": list(profile["covered_pack_ids"]),
                "workpaper_relative": f"connector-shadow/workpapers/{stem}.json",
                "workpaper": root / "connector-shadow" / "workpapers" / f"{stem}.json",
                "request_relative": (
                    f"connector-shadow/workpapers/{stem}-live-request.json"
                ),
                "request": (
                    root / "connector-shadow" / "workpapers" / f"{stem}-live-request.json"
                ),
                "baseline": root / "connector-shadow" / "baselines" / f"{stem}.json",
                "observation": (
                    root / "connector-shadow" / "assessments" / f"{stem}-observation.json"
                ),
                "assessment": root / "connector-shadow" / "assessments" / f"{stem}.json",
                "review": root / "connector-shadow" / "reviews" / f"{stem}.json",
                "access_pack_ids": access_pack_ids,
                "access_requests": {
                    pack_id: root / "connector-shadow" / "workpapers" /
                    f"{access_stems[pack_id]}-access-request.json"
                    for pack_id in access_pack_ids
                },
                "access_receipts": {
                    pack_id: root / "connector-shadow" / "workpapers" /
                    f"{access_stems[pack_id]}-access-receipt.json"
                    for pack_id in access_pack_ids
                },
            })
    return specs


def _environment_body(paths: Mapping[str, Path]) -> bytes:
    lines = [
        "# Generated by opc-finance-box activation-init.",
        "# Contains private artifact paths only; never add credentials or secret values.",
    ]
    for name in sorted(paths):
        lines.append(f"{name}={shlex.quote(str(paths[name]))}")
    lines.extend([
        "",
        "# Connector credentials remain outside this file and must come from a secret manager.",
    ])
    return ("\n".join(lines) + "\n").encode("utf-8")


def _commands(
    runtime: BoxRuntime,
    config_path: Path,
    root: Path,
    connector_specs: list[dict[str, Any]],
    *,
    period: str,
    facts_as_of: str,
    deduplicate_access_scopes: bool = True,
) -> dict[str, Any]:
    steps: list[dict[str, Any]] = []
    for entity in runtime.entities.all():
        workpaper = root / "tax" / "workpapers" / f"{entity.entity_id}.json"
        reviewed = (
            root / "tax" / "reviewed-source" / f"{entity.entity_id}.json"
        )
        steps.append({
            "step_id": f"tax-workpaper-complete:{entity.entity_id}",
            "purpose": "complete evidence-backed applicability answers without raw tax identifiers",
            "action": "edit_private_json",
            "relative_file": workpaper.relative_to(root).as_posix(),
            "requires_operator_edit": True,
            "command_executed": False,
        })
        steps.append(_command(
            f"tax-review:{entity.entity_id}",
            "independent local tax review",
            [
                "opc-finance-box", "tax-applicability-review", str(config_path),
                str(workpaper), "--decision", "needs-correction",
                "--actor", "REPLACE_WITH_LOCAL_TAX_REVIEWER", "--rationale",
                "REPLACE_WITH_RATIONALE", "--evidence-reference",
                "advisor://REPLACE_WITH_REVIEW_REFERENCE", "--output", str(reviewed),
            ],
        ))
        steps.append(_command(
            f"tax-import:{entity.entity_id}",
            "controlled import into the exact entity review rotation",
            [
                "opc-finance-box", "tax-applicability-import", str(config_path),
                str(reviewed), "--review-dir", str(root / "tax" / "reviews"),
                "--as-of", facts_as_of,
            ],
        ))
    steps.append(_command(
        "tax-registry-seal",
        "seal the complete per-entity applicability rotation",
        [
            "opc-finance-box", "tax-applicability-registry-seal", str(config_path),
            "--review-dir", str(root / "tax" / "reviews"), "--actor",
            "REPLACE_WITH_REGISTRY_CONTROLLER", "--as-of", facts_as_of,
            "--output", str(root / "tax" / "registry-receipt.json"),
        ],
    ))
    emitted_access_scopes: set[tuple[str, str]] = set()
    for spec in connector_specs:
        scope = f"{spec['entity_id']}:{spec['pipeline_id']}"
        steps.extend([
            {
                "step_id": f"connector-baseline-complete:{scope}",
                "purpose": (
                    "complete an independent real-source count/control workpaper; "
                    "never copy Pipeline output into the baseline"
                ),
                "action": "edit_private_json",
                "relative_file": spec["workpaper_relative"],
                "requires_operator_edit": True,
                "command_executed": False,
            },
            _command(
                f"connector-baseline-finalize:{scope}",
                "seal the independently prepared anonymized baseline",
                [
                    "opc-finance-box", "connector-shadow-baseline-finalize",
                    str(config_path), str(spec["workpaper"]), "--output",
                    str(spec["baseline"]),
                ],
            ),
        ])
        for pack_id in spec["access_pack_ids"]:
            access_request = spec["access_requests"][pack_id]
            access_receipt = spec["access_receipts"][pack_id]
            access_key = (pack_id, spec["entity_id"])
            if deduplicate_access_scopes and access_key in emitted_access_scopes:
                continue
            emitted_access_scopes.add(access_key)
            access_scope = (
                f"{pack_id}:{spec['entity_id']}"
                if deduplicate_access_scopes else f"{pack_id}:{scope}"
            )
            steps.extend([
                _command(
                    f"connector-access-request-init:{access_scope}",
                    "initialize one private provider-account binding for the exact Pack and entity",
                    [
                        "opc-finance-box", "connector-access-request-init",
                        str(config_path), "--pack", pack_id, "--entity",
                        spec["entity_id"], "--output", str(access_request),
                    ],
                ),
                {
                    "step_id": f"connector-access-request-complete:{access_scope}",
                    "purpose": (
                        "bind the private provider account without placing credentials, "
                        "account identifiers or store domains in the command contract"
                    ),
                    "action": "edit_private_json",
                    "relative_file": access_request.relative_to(root).as_posix(),
                    "requires_operator_edit": True,
                    "command_executed": False,
                },
                _command(
                    f"connector-access-request-verify:{access_scope}",
                    "verify the private provider-account binding before any network call",
                    [
                        "opc-finance-box", "connector-access-request-verify",
                        str(config_path), str(access_request),
                    ],
                ),
                _command(
                    f"connector-access-probe:{access_scope}",
                    (
                        "after explicit operator authorization, run only the bounded read "
                        "probe and persist a secret-free private receipt"
                    ),
                    [
                        "opc-finance-box", "connector-access-probe",
                        str(config_path), str(access_request), "--allow-network",
                        "--output", str(access_receipt),
                    ],
                ),
                _command(
                    f"connector-access-receipt-verify:{access_scope}",
                    "rebind the current passed receipt to this Box, Pack, entity and request",
                    [
                        "opc-finance-box", "connector-access-receipt-verify",
                        str(config_path), str(access_request), str(access_receipt),
                    ],
                ),
            ])
        pipeline_evidence = "REPLACE_WITH_PRIVATE_PIPELINE_RESULT.json"
        if spec["pipeline_id"] == "dtc.shopify_stripe_month_close":
            steps.extend([
                _command(
                    f"connector-shadow-request-init:{scope}",
                    "generate exact entity/month bounds in a private incomplete live request",
                    [
                        "opc-finance-box", "shopify-monthly-shadow-request-init",
                        str(config_path), "--entity", spec["entity_id"],
                        "--period", period, "--output", str(spec["request"]),
                    ],
                ),
                {
                    "step_id": f"connector-shadow-request-complete:{scope}",
                    "purpose": (
                        "fill the private store domain, currency exponents and explicit "
                        "Shopify transaction to Stripe source evidence links"
                    ),
                    "action": "edit_private_json",
                    "relative_file": spec["request_relative"],
                    "requires_operator_edit": True,
                    "command_executed": False,
                },
                _command(
                    f"connector-shadow-request-verify:{scope}",
                    "verify exact windows, entity binding, evidence links and secret-free request",
                    [
                        "opc-finance-box", "shopify-monthly-shadow-request-verify",
                        str(config_path), str(spec["request"]),
                    ],
                ),
                _command(
                    f"connector-shadow-observe:{scope}",
                    (
                        "run the live monthly Shopify + Stripe read in memory and persist only "
                        "amount-, store- and raw-id-free controls"
                    ),
                    [
                        "opc-finance-box", "shopify-monthly-shadow-observe",
                        str(config_path), str(spec["request"]),
                        "--shopify-access-request",
                        str(spec["access_requests"]["connector.shopify"]),
                        "--shopify-access-receipt",
                        str(spec["access_receipts"]["connector.shopify"]),
                        "--stripe-access-request",
                        str(spec["access_requests"]["connector.stripe"]),
                        "--stripe-access-receipt",
                        str(spec["access_receipts"]["connector.stripe"]),
                        "--output", str(spec["observation"]),
                    ],
                ),
            ])
            pipeline_evidence = str(spec["observation"])
        elif spec["pipeline_id"] == "stripe.daily_close":
            steps.extend([
                _command(
                    f"connector-shadow-request-init:{scope}",
                    "generate exact entity/month Stripe bounds in a private incomplete request",
                    [
                        "opc-finance-box", "stripe-shadow-request-init",
                        str(config_path), "--entity", spec["entity_id"],
                        "--period", period, "--output", str(spec["request"]),
                    ],
                ),
                {
                    "step_id": f"connector-shadow-request-complete:{scope}",
                    "purpose": (
                        "fill private bank payout-arrival evidence; retain bank IDs, "
                        "references and amounts only in this mode-0600 request"
                    ),
                    "action": "edit_private_json",
                    "relative_file": spec["request_relative"],
                    "requires_operator_edit": True,
                    "command_executed": False,
                },
                _command(
                    f"connector-shadow-request-verify:{scope}",
                    (
                        "verify Stripe same-window bounds, entity binding, bank evidence, "
                        "permissions and secret-free request"
                    ),
                    [
                        "opc-finance-box", "stripe-shadow-request-verify",
                        str(config_path), str(spec["request"]),
                    ],
                ),
                _command(
                    f"connector-shadow-observe:{scope}",
                    (
                        "run the live Stripe read and bank candidate match in memory and "
                        "persist only amount-, bank-reference- and raw-id-free controls"
                    ),
                    [
                        "opc-finance-box", "stripe-shadow-observe",
                        str(config_path), str(spec["request"]),
                        "--access-request",
                        str(spec["access_requests"]["connector.stripe"]),
                        "--access-receipt",
                        str(spec["access_receipts"]["connector.stripe"]),
                        "--output", str(spec["observation"]),
                    ],
                ),
            ])
            pipeline_evidence = str(spec["observation"])
        elif spec["pipeline_id"] == "finance.bank_statement_close":
            steps.extend([
                _command(
                    f"connector-shadow-request-init:{scope}",
                    (
                        "generate a complete secret-free Wise request bound to the "
                        "entity functional currency and exact calendar month"
                    ),
                    [
                        "opc-finance-box", "wise-shadow-request-init",
                        str(config_path), "--entity", spec["entity_id"],
                        "--period", period, "--output", str(spec["request"]),
                    ],
                ),
                _command(
                    f"connector-shadow-request-verify:{scope}",
                    (
                        "verify Wise entity, functional currency, month window, file "
                        "permissions and secret-free request before network access"
                    ),
                    [
                        "opc-finance-box", "wise-shadow-request-verify",
                        str(config_path), str(spec["request"]),
                    ],
                ),
                _command(
                    f"connector-shadow-observe:{scope}",
                    (
                        "run the live Wise monthly read in memory and persist only "
                        "amount-, account- and counterparty-free controls"
                    ),
                    [
                        "opc-finance-box", "wise-shadow-observe",
                        str(config_path), str(spec["request"]),
                        "--access-request",
                        str(spec["access_requests"]["connector.wise"]),
                        "--access-receipt",
                        str(spec["access_receipts"]["connector.wise"]),
                        "--output", str(spec["observation"]),
                    ],
                ),
            ])
            pipeline_evidence = str(spec["observation"])
        elif spec["pipeline_id"] == "finance.trial_balance_review":
            steps.extend([
                _command(
                    f"connector-shadow-request-init:{scope}",
                    (
                        "generate a complete secret-free Xero Trial Balance request "
                        "bound to the entity and exact calendar month end"
                    ),
                    [
                        "opc-finance-box", "xero-shadow-request-init",
                        str(config_path), "--entity", spec["entity_id"],
                        "--period", period, "--output", str(spec["request"]),
                    ],
                ),
                _command(
                    f"connector-shadow-request-verify:{scope}",
                    (
                        "verify Xero entity, month-end snapshot, accrual basis, file "
                        "permissions and secret-free request before network access"
                    ),
                    [
                        "opc-finance-box", "xero-shadow-request-verify",
                        str(config_path), str(spec["request"]),
                    ],
                ),
                _command(
                    f"connector-shadow-observe:{scope}",
                    (
                        "run the live Xero Trial Balance read in memory and persist only "
                        "amount-, account- and tenant-free controls"
                    ),
                    [
                        "opc-finance-box", "xero-shadow-observe",
                        str(config_path), str(spec["request"]),
                        "--access-request",
                        str(spec["access_requests"]["connector.xero"]),
                        "--access-receipt",
                        str(spec["access_receipts"]["connector.xero"]),
                        "--output", str(spec["observation"]),
                    ],
                ),
            ])
            pipeline_evidence = str(spec["observation"])
        elif spec["pipeline_id"] == "paypal.transaction_close":
            steps.extend([
                _command(
                    f"connector-shadow-request-init:{scope}",
                    (
                        "generate a complete secret-free production PayPal request "
                        "bound to the entity and exact calendar month"
                    ),
                    [
                        "opc-finance-box", "paypal-shadow-request-init",
                        str(config_path), "--entity", spec["entity_id"],
                        "--period", period, "--output", str(spec["request"]),
                    ],
                ),
                _command(
                    f"connector-shadow-request-verify:{scope}",
                    (
                        "verify PayPal production environment, entity, exact month, "
                        "bounded pagination, file permissions and secret-free request"
                    ),
                    [
                        "opc-finance-box", "paypal-shadow-request-verify",
                        str(config_path), str(spec["request"]),
                    ],
                ),
                _command(
                    f"connector-shadow-observe:{scope}",
                    (
                        "run the live PayPal Transaction Search in memory and persist only "
                        "amount-, customer- and raw-id-free controls"
                    ),
                    [
                        "opc-finance-box", "paypal-shadow-observe",
                        str(config_path), str(spec["request"]),
                        "--access-request",
                        str(spec["access_requests"]["connector.paypal"]),
                        "--access-receipt",
                        str(spec["access_receipts"]["connector.paypal"]),
                        "--output", str(spec["observation"]),
                    ],
                ),
            ])
            pipeline_evidence = str(spec["observation"])
        elif spec["pipeline_id"] == "woocommerce.order_refund_close":
            steps.extend([
                _command(
                    f"connector-shadow-request-init:{scope}",
                    (
                        "generate a complete site- and secret-free WooCommerce request "
                        "bound to the entity and exact calendar month"
                    ),
                    [
                        "opc-finance-box", "woocommerce-shadow-request-init",
                        str(config_path), "--entity", spec["entity_id"],
                        "--period", period, "--output", str(spec["request"]),
                    ],
                ),
                _command(
                    f"connector-shadow-request-verify:{scope}",
                    (
                        "verify WooCommerce entity, exact month, bounded pagination, "
                        "file permissions and site-/secret-free request"
                    ),
                    [
                        "opc-finance-box", "woocommerce-shadow-request-verify",
                        str(config_path), str(spec["request"]),
                    ],
                ),
                _command(
                    f"connector-shadow-observe:{scope}",
                    (
                        "run the live WooCommerce REST API v3 read in memory and persist "
                        "only amount-, site-, customer-, product- and raw-id-free controls"
                    ),
                    [
                        "opc-finance-box", "woocommerce-shadow-observe",
                        str(config_path), str(spec["request"]),
                        "--access-request",
                        str(spec["access_requests"]["connector.woocommerce"]),
                        "--access-receipt",
                        str(spec["access_receipts"]["connector.woocommerce"]),
                        "--output", str(spec["observation"]),
                    ],
                ),
            ])
            pipeline_evidence = str(spec["observation"])
        elif spec["pipeline_id"] == "commerce.shipbob_fulfillment_close":
            steps.extend([
                _command(
                    f"connector-shadow-request-init:{scope}",
                    (
                        "generate a complete secret-free production ShipBob request "
                        "bound to the entity and exact calendar month"
                    ),
                    [
                        "opc-finance-box", "shipbob-shadow-request-init",
                        str(config_path), "--entity", spec["entity_id"],
                        "--period", period, "--output", str(spec["request"]),
                    ],
                ),
                _command(
                    f"connector-shadow-request-verify:{scope}",
                    (
                        "verify ShipBob production environment, entity, exact month, "
                        "bounded pagination, file permissions and secret-free request"
                    ),
                    [
                        "opc-finance-box", "shipbob-shadow-request-verify",
                        str(config_path), str(spec["request"]),
                    ],
                ),
                _command(
                    f"connector-shadow-observe:{scope}",
                    (
                        "run the live ShipBob order/return read in memory and persist only "
                        "amount-, merchant-, customer-, inventory- and raw-id-free controls"
                    ),
                    [
                        "opc-finance-box", "shipbob-shadow-observe",
                        str(config_path), str(spec["request"]),
                        "--access-request",
                        str(spec["access_requests"]["connector.shipbob"]),
                        "--access-receipt",
                        str(spec["access_receipts"]["connector.shipbob"]),
                        "--output", str(spec["observation"]),
                    ],
                ),
            ])
            pipeline_evidence = str(spec["observation"])
        elif spec["pipeline_id"] == "amazon_seller.marketplace_close":
            steps.extend([
                _command(
                    f"connector-shadow-request-init:{scope}",
                    (
                        "generate a complete secret-free production Amazon Orders/FBA "
                        "Inventory/Finances request bound to the entity, selected Marketplace "
                        "and one completed calendar month"
                    ),
                    [
                        "opc-finance-box", "amazon-seller-shadow-request-init",
                        str(config_path), "--entity", spec["entity_id"],
                        "--period", period, "--marketplace-id",
                        "REPLACE_WITH_MARKETPLACE_ID", "--output", str(spec["request"]),
                    ],
                ),
                _command(
                    f"connector-shadow-request-verify:{scope}",
                    (
                        "verify Amazon production, entity, Marketplace, closed month, "
                        "three-source pagination, file permissions and secret-free request"
                    ),
                    [
                        "opc-finance-box", "amazon-seller-shadow-request-verify",
                        str(config_path), str(spec["request"]),
                    ],
                ),
                _command(
                    f"connector-shadow-observe:{scope}",
                    (
                        "run the live Amazon three-source read in memory and persist only "
                        "amount-, seller-, region-, marketplace-, buyer-, product-, inventory- "
                        "and raw-id-free controls"
                    ),
                    [
                        "opc-finance-box", "amazon-seller-shadow-observe",
                        str(config_path), str(spec["request"]),
                        "--access-request",
                        str(spec["access_requests"]["connector.amazon_seller"]),
                        "--access-receipt",
                        str(spec["access_receipts"]["connector.amazon_seller"]),
                        "--output", str(spec["observation"]),
                    ],
                ),
            ])
            pipeline_evidence = str(spec["observation"])
        steps.extend([
            _command(
                f"connector-shadow-assess:{scope}",
                "compare one real read-only Pipeline result or safe observation with the independent baseline",
                [
                    "opc-finance-box", "connector-shadow-assess", str(config_path),
                    str(spec["baseline"]), pipeline_evidence, "--output",
                    str(spec["assessment"]),
                ],
            ),
            _command(
                f"connector-shadow-review:{scope}",
                "independently review the comparison into the active private rotation",
                [
                    "opc-finance-box", "connector-shadow-review", str(config_path),
                    str(spec["assessment"]), "--decision",
                    "needs-correction", "--actor",
                    "REPLACE_WITH_INDEPENDENT_CONNECTOR_REVIEWER", "--rationale",
                    "REPLACE_WITH_RATIONALE", "--evidence-reference",
                    "review://REPLACE_WITH_CONNECTOR_SHADOW_REFERENCE", "--output",
                    str(spec["review"]),
                ],
            ),
        ])
    pilot_review = root / "pilot" / "readiness" / "reviewed.json"
    handoff_workpaper = root / "pilot" / "handoff" / "workpaper.json"
    handoff_review = root / "pilot" / "handoff" / "reviewed.json"
    registration = root / "pilot" / "registrations" / "first-run.json"
    observation_receipt = root / "pilot" / "observations" / "first-receipt.json"
    observation_review = root / "pilot" / "observations" / "first-reviewed.json"
    series_receipt = root / "pilot" / "observations" / "series-receipt.json"
    series_review = root / "pilot" / "observations" / "series-reviewed.json"
    runs_root = root / "pipeline-runs"
    entity_reports = {
        entity.entity_id: root / "pilot" / "entity-reports" / f"{entity.entity_id}.json"
        for entity in runtime.entities.all()
    }
    steps.extend([
        {
            "step_id": "pilot-readiness-complete",
            "purpose": "complete entity domains, connector posture and one-period Shadow plan",
            "action": "edit_private_json",
            "relative_file": "pilot/readiness/workpaper.json",
            "requires_operator_edit": True,
            "command_executed": False,
        },
        _command(
            "pilot-readiness-review",
            "independent review of the first-company bounded Shadow plan",
            [
                "opc-finance-box", "pilot-readiness-review", str(config_path),
                str(root / "pilot" / "readiness" / "workpaper.json"),
                "--actor", "REPLACE_WITH_PILOT_REVIEWER", "--rationale",
                "REPLACE_WITH_RATIONALE", "--evidence-reference",
                "advisor://REPLACE_WITH_PILOT_REFERENCE", "--output",
                str(pilot_review),
            ],
        ),
        _command(
            "pilot-readiness-verify",
            "re-verify the approved pilot against current tax applicability",
            [
                "opc-finance-box", "pilot-readiness-verify", str(config_path),
                str(pilot_review), "--tax-review-dir",
                str(root / "tax" / "reviews"), "--tax-registry-receipt",
                str(root / "tax" / "registry-receipt.json"), "--as-of",
                facts_as_of,
            ],
        ),
        _command(
            "data-handoff-init",
            "create the Box-bound real-source intake workpaper after pilot approval",
            [
                "opc-finance-box", "pilot-data-handoff-init", str(config_path),
                str(pilot_review), "--prepared-by",
                "REPLACE_WITH_HANDOFF_PREPARER", "--custodian-principal",
                "REPLACE_WITH_DATA_CUSTODIAN", "--as-of", facts_as_of,
                "--output", str(handoff_workpaper),
            ],
        ),
        {
            "step_id": "data-handoff-complete",
            "purpose": "complete source inventory, custody and access evidence without copying data into the manifest",
            "action": "edit_private_json",
            "relative_file": handoff_workpaper.relative_to(root).as_posix(),
            "requires_operator_edit": True,
            "command_executed": False,
        },
        _command(
            "data-handoff-review",
            "independently approve the completed controlled-intake manifest",
            [
                "opc-finance-box", "pilot-data-handoff-review", str(config_path),
                str(handoff_workpaper), str(pilot_review), "--actor",
                "REPLACE_WITH_HANDOFF_REVIEWER", "--rationale",
                "REPLACE_WITH_RATIONALE", "--evidence-reference",
                "review://REPLACE_WITH_HANDOFF_REFERENCE", "--as-of",
                facts_as_of, "--output", str(handoff_review),
            ],
        ),
        _command(
            "data-handoff-verify",
            "re-verify controlled intake without exposing source paths or values",
            [
                "opc-finance-box", "pilot-data-handoff-verify", str(config_path),
                str(handoff_review), str(pilot_review), "--as-of", facts_as_of,
            ],
        ),
    ])
    registration_argv = [
        "opc-finance-box", "pilot-shadow-run-register", str(config_path),
        str(handoff_review), str(pilot_review),
    ]
    for entity_id in entity_reports:
        registration_argv.extend([
            "--entity-attempt", f"{entity_id}=REPLACE_WITH_REVIEWED_ATTEMPT_ID",
        ])
    registration_argv.extend([
        "--actor", "REPLACE_WITH_SHADOW_RUN_REGISTRAR", "--rationale",
        "REPLACE_WITH_RATIONALE", "--evidence-reference",
        "run-ledger://REPLACE_WITH_REGISTRATION_REFERENCE", "--as-of",
        facts_as_of, "--runs-root", str(runs_root), "--output",
        str(registration),
    ])
    steps.extend([
        {
            "step_id": "pipeline-attempts-complete",
            "purpose": "record and independently review one month-close Pipeline attempt per legal entity",
            "action": "complete_external_prerequisite",
            "required_entity_ids": list(entity_reports),
            "requires_operator_edit": True,
            "command_executed": False,
        },
        _command(
            "shadow-run-register",
            "bind exactly one fully reviewed attempt per entity to the approved handoff",
            registration_argv,
        ),
        _command(
            "shadow-run-verify",
            "re-verify the registration against the tamper-evident private run ledger",
            [
                "opc-finance-box", "pilot-shadow-run-verify", str(config_path),
                str(registration), str(handoff_review), str(pilot_review),
                "--runs-root", str(runs_root), "--as-of", facts_as_of,
            ],
        ),
    ])
    for entity_id, reviewed_report in entity_reports.items():
        baseline = root / "pilot" / "shadow-baselines" / f"{entity_id}.xlsx"
        report = root / "pilot" / "shadow-reports" / f"{entity_id}.json"
        steps.extend([
            _command(
                f"shadow-close-template:{entity_id}",
                "create a private human-close baseline workbook",
                [
                    "opc-finance-box", "shadow-close-template", str(config_path),
                    "--output", str(baseline),
                ],
            ),
            {
                "step_id": f"shadow-close-baseline-complete:{entity_id}",
                "purpose": "complete the independent human-close baseline for exactly this entity and period",
                "action": "edit_private_workbook",
                "relative_file": baseline.relative_to(root).as_posix(),
                "entity_id": entity_id,
                "period": period,
                "requires_operator_edit": True,
                "command_executed": False,
            },
            _command(
                f"shadow-close-compare:{entity_id}",
                "compare the human baseline with the scoped deterministic finance result",
                [
                    "opc-finance-box", "shadow-close-compare", str(config_path),
                    str(baseline),
                    f"REPLACE_WITH_PRIVATE_FINANCE_RESULT_{entity_id}.json",
                    "--output", str(report),
                ],
            ),
            _command(
                f"shadow-close-review:{entity_id}",
                "independently review the exact entity Shadow Close report",
                [
                    "opc-finance-box", "shadow-close-review", str(config_path),
                    str(report), "--decision", "needs-correction", "--actor",
                    "REPLACE_WITH_ENTITY_SHADOW_REVIEWER", "--rationale",
                    "REPLACE_WITH_RATIONALE", "--evidence-reference",
                    f"review://REPLACE_WITH_{entity_id}_SHADOW_REFERENCE", "--output",
                    str(reviewed_report),
                ],
            ),
            _command(
                f"shadow-close-verify:{entity_id}",
                "verify the reviewed entity report without returning financial values",
                [
                    "opc-finance-box", "shadow-close-verify", str(config_path),
                    str(reviewed_report),
                ],
            ),
        ])
    portfolio_review: Path | None = None
    if len(entity_reports) > 1:
        portfolio_receipt = root / "pilot" / "portfolio" / "receipt.json"
        portfolio_review = root / "pilot" / "observations" / "portfolio-reviewed.json"
        portfolio_argv = [
            "opc-finance-box", "shadow-close-portfolio-assemble", str(config_path),
        ]
        for reviewed_report in entity_reports.values():
            portfolio_argv.extend(["--entity-report", str(reviewed_report)])
        portfolio_argv.extend([
            "--portfolio-result", "REPLACE_WITH_PRIVATE_PORTFOLIO_RESULT.json",
            "--output", str(portfolio_receipt),
        ])
        steps.extend([
            _command(
                "shadow-portfolio-assemble",
                "bind every reviewed entity report to one ledger-verified management portfolio",
                portfolio_argv,
            ),
            _command(
                "shadow-portfolio-review",
                "independently review the amount-free multi-entity acceptance manifest",
                [
                    "opc-finance-box", "shadow-close-portfolio-review",
                    str(config_path), str(portfolio_receipt), "--decision",
                    "needs-correction", "--actor",
                    "REPLACE_WITH_PORTFOLIO_REVIEWER", "--rationale",
                    "REPLACE_WITH_RATIONALE", "--evidence-reference",
                    "review://REPLACE_WITH_PORTFOLIO_REFERENCE", "--output",
                    str(portfolio_review),
                ],
            ),
            _command(
                "shadow-portfolio-verify",
                "verify the reviewed portfolio manifest without returning amounts",
                [
                    "opc-finance-box", "shadow-close-portfolio-verify",
                    str(config_path), str(portfolio_review),
                ],
            ),
        ])
    observation_assemble_argv = [
        "opc-finance-box", "pilot-shadow-observation-assemble", str(config_path),
        str(registration), str(handoff_review), str(pilot_review),
    ]
    for reviewed_report in entity_reports.values():
        observation_assemble_argv.extend(["--entity-report", str(reviewed_report)])
    if portfolio_review is not None:
        observation_assemble_argv.extend(["--portfolio-review", str(portfolio_review)])
    observation_assemble_argv.extend([
        "--runs-root", str(runs_root), "--as-of", facts_as_of,
        "--output", str(observation_receipt),
    ])
    observation_verify_argv = [
        "opc-finance-box", "pilot-shadow-observation-verify", str(config_path),
        str(observation_review), str(registration), str(handoff_review),
        str(pilot_review),
    ]
    for reviewed_report in entity_reports.values():
        observation_verify_argv.extend(["--entity-report", str(reviewed_report)])
    if portfolio_review is not None:
        observation_verify_argv.extend(["--portfolio-review", str(portfolio_review)])
    observation_verify_argv.extend([
        "--runs-root", str(runs_root), "--as-of", facts_as_of,
    ])
    steps.extend([
        _command(
            "shadow-observation-assemble",
            "bind the registration, reviewed entity reports and optional portfolio into one observation",
            observation_assemble_argv,
        ),
        _command(
            "shadow-observation-review",
            "independently review the first real observation",
            [
                "opc-finance-box", "pilot-shadow-observation-review",
                str(config_path), str(observation_receipt), "--decision",
                "needs-correction", "--actor",
                "REPLACE_WITH_OBSERVATION_REVIEWER", "--rationale",
                "REPLACE_WITH_RATIONALE", "--evidence-reference",
                "review://REPLACE_WITH_OBSERVATION_REFERENCE", "--output",
                str(observation_review),
            ],
        ),
        _command(
            "shadow-observation-verify",
            "re-verify the reviewed observation against all current source evidence",
            observation_verify_argv,
        ),
        {
            "step_id": "consecutive-period-evidence-complete",
            "purpose": "prepare the exact private directory contract for at least two consecutive reviewed periods",
            "action": "assemble_private_period_evidence",
            "relative_directory": "pilot/series-periods",
            "minimum_period_count": 2,
            "requires_operator_edit": True,
            "command_executed": False,
        },
        _command(
            "shadow-series-assemble",
            "re-verify and bind two to twenty-four consecutive reviewed periods",
            [
                "opc-finance-box", "pilot-shadow-series-assemble", str(config_path),
                str(root / "pilot" / "series-periods"), "--runs-root",
                str(runs_root), "--as-of", facts_as_of, "--output",
                str(series_receipt),
            ],
        ),
        _command(
            "shadow-series-review",
            "independently review the exact consecutive-period receipt",
            [
                "opc-finance-box", "pilot-shadow-series-review", str(config_path),
                str(series_receipt), "--decision", "needs-correction", "--actor",
                "REPLACE_WITH_CONTINUITY_REVIEWER", "--rationale",
                "REPLACE_WITH_RATIONALE", "--evidence-reference",
                "review://REPLACE_WITH_SERIES_REFERENCE", "--output",
                str(series_review),
            ],
        ),
        _command(
            "shadow-series-verify",
            "re-verify the reviewed series against every private period source",
            [
                "opc-finance-box", "pilot-shadow-series-verify", str(config_path),
                str(series_review), str(root / "pilot" / "series-periods"),
                "--runs-root", str(runs_root), "--as-of", facts_as_of,
            ],
        ),
        _command(
            "promotion-template",
            "create one deliberately incomplete evidence template for the selected Pack",
            [
                "opc-finance-box", "promotion-template", str(config_path),
                "REPLACE_WITH_SELECTED_PACK_ID", "--output",
                str(root / "promotion" / "evidence" / "selected-pack.json"),
            ],
        ),
        {
            "step_id": "promotion-evidence-complete",
            "purpose": "complete current reports, reviewed series, gates, rehearsals, limitations and thresholds",
            "action": "edit_private_json",
            "relative_file": "promotion/evidence/selected-pack.json",
            "requires_operator_edit": True,
            "command_executed": False,
        },
        _command(
            "promotion-assess",
            "evaluate the complete evidence without persisting raw reports or values",
            [
                "opc-finance-box", "promotion-assess", str(config_path),
                str(root / "promotion" / "evidence" / "selected-pack.json"),
            ],
        ),
        _command(
            "promotion-record",
            "append one secret-safe candidate assessment to the private promotion ledger",
            [
                "opc-finance-box", "promotion-record", str(config_path),
                str(root / "promotion" / "evidence" / "selected-pack.json"),
                "--actor", "REPLACE_WITH_EVIDENCE_PREPARER", "--promotion-root",
                str(root / "promotion" / "ledger"),
            ],
        ),
        _command(
            "promotion-review",
            "independently review the exact recorded assessment with a fail-closed default decision",
            [
                "opc-finance-box", "promotion-review", str(config_path),
                "REPLACE_WITH_ASSESSMENT_ID", "--decision", "needs_more_evidence",
                "--actor", "REPLACE_WITH_RELEASE_REVIEWER", "--rationale",
                "REPLACE_WITH_RATIONALE", "--evidence-reference",
                "release://REPLACE_WITH_REVIEW_REFERENCE", "--promotion-root",
                str(root / "promotion" / "ledger"),
            ],
        ),
        _command(
            "promotion-status",
            "read the safe stable-candidate and independent-review state",
            [
                "opc-finance-box", "promotion-status", str(config_path),
                "--promotion-root", str(root / "promotion" / "ledger"),
            ],
        ),
        _command(
            "promotion-ledger-verify",
            "verify the append-only promotion ledger hash chain",
            [
                "opc-finance-box", "promotion-verify", "--promotion-root",
                str(root / "promotion" / "ledger"),
            ],
        ),
        _command(
            "workspace-status",
            "re-evaluate the initialized workspace without exporting private paths",
            [
                "opc-finance-box", "activation-workspace-status", str(config_path),
                str(root), "--as-of", facts_as_of,
            ],
        ),
    ])
    return {
        "schema_version": 2,
        "artifact_type": "first_customer_activation_commands",
        "runtime_fingerprint": runtime.snapshot()["fingerprint"],
        "period": period,
        "stage_sequence": list(COMMAND_STAGE_SEQUENCE),
        "steps": steps,
        "contains_credentials": False,
        "contains_financial_values": False,
        "commands_executed": False,
        "external_actions_performed": False,
    }


def initialize_activation_workspace(
    runtime: BoxRuntime,
    config_path: str | Path,
    root: str | Path,
    *,
    period: str,
    facts_as_of: str,
    prepared_by: str,
) -> dict[str, Any]:
    """Create a new private first-customer workspace without producing evidence."""
    destination = _safe_new_root(root)
    config = Path(config_path).expanduser().resolve()
    if not config.is_file():
        raise ActivationWorkspaceError("Box config must be an existing regular file")
    snapshot = runtime.snapshot()
    entity_ids = sorted(item["id"] for item in snapshot["entities"])
    if not entity_ids:
        raise ActivationWorkspaceError("activation workspace requires at least one entity")
    connector = build_connector_shadow_registry_workspace(
        runtime, None, as_of=facts_as_of,
    )
    network_pack_ids = [item["pack_id"] for item in connector["pack_coverage"]]
    tax_workpapers = {
        entity_id: build_tax_applicability_workpaper(
            runtime,
            entity_id,
            prepared_by=prepared_by,
            facts_as_of=facts_as_of,
        )
        for entity_id in entity_ids
    }
    pilot_workpaper = build_pilot_readiness_workpaper(
        runtime, period=period, prepared_by=prepared_by,
    )
    paths = _workspace_paths(destination, multi_entity=len(entity_ids) > 1)
    connector_specs = _connector_workpaper_specs(runtime, destination)
    environment_body = _environment_body(paths)
    commands_body = _json_bytes(_commands(
        runtime, config, destination, connector_specs,
        period=period, facts_as_of=facts_as_of,
    ))
    created = False
    try:
        _mkdir_private(destination)
        created = True
        for relative in DIRECTORIES:
            current = destination
            for part in Path(relative).parts:
                current = current / part
                if not current.exists():
                    _mkdir_private(current)
        initial_files: list[dict[str, Any]] = []
        for entity_id, workpaper in tax_workpapers.items():
            relative = f"tax/workpapers/{entity_id}.json"
            body = _json_bytes(workpaper)
            _write_exclusive(destination / relative, body)
            initial_files.append({
                "relative_path": relative,
                "kind": "mutable_tax_workpaper",
                "mutable": True,
                "initial_sha256": _sha256(body),
            })
        for spec in connector_specs:
            build_connector_shadow_baseline_workpaper(
                runtime,
                pipeline_id=spec["pipeline_id"],
                entity_id=spec["entity_id"],
                sample_period=period,
                prepared_by=prepared_by,
                output=spec["workpaper"],
            )
            body = spec["workpaper"].read_bytes()
            initial_files.append({
                "relative_path": spec["workpaper_relative"],
                "kind": "mutable_connector_shadow_baseline_workpaper",
                "mutable": True,
                "initial_sha256": _sha256(body),
            })
        pilot_relative = "pilot/readiness/workpaper.json"
        pilot_body = _json_bytes(pilot_workpaper)
        _write_exclusive(destination / pilot_relative, pilot_body)
        initial_files.append({
            "relative_path": pilot_relative,
            "kind": "mutable_pilot_readiness_workpaper",
            "mutable": True,
            "initial_sha256": _sha256(pilot_body),
        })
        for name, body, kind in (
            (ENV_NAME, environment_body, "immutable_secret_free_path_contract"),
            (COMMANDS_NAME, commands_body, "immutable_command_contract"),
        ):
            _write_exclusive(destination / name, body)
            initial_files.append({
                "relative_path": name,
                "kind": kind,
                "mutable": False,
                "initial_sha256": _sha256(body),
            })
        manifest = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "artifact_type": "first_customer_activation_workspace",
            "runtime_fingerprint": snapshot["fingerprint"],
            "period": period,
            "facts_as_of": facts_as_of,
            "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "prepared_by": prepared_by,
            "entity_ids": entity_ids,
            "network_connector_pack_ids": network_pack_ids,
            "directory_contract": list(DIRECTORIES),
            "initial_files": initial_files,
            "connector_baseline_workpaper_count": len(connector_specs),
            "review_artifacts_created": False,
            "connector_baselines_created": False,
            "credentials_included": False,
            "financial_source_files_copied": False,
            "commands_executed": False,
            "external_actions_performed": False,
        }
        manifest_body = _json_bytes(manifest)
        _write_exclusive(destination / MANIFEST_NAME, manifest_body)
        if os.name != "nt":
            os.chmod(destination, 0o700)
    except Exception:
        if created and destination.is_dir() and not destination.is_symlink():
            shutil.rmtree(destination)
        raise
    return {
        "initialized": True,
        "output_root": str(destination),
        "runtime_fingerprint": snapshot["fingerprint"],
        "workspace_manifest_sha256": _sha256(manifest_body),
        "entity_count": len(entity_ids),
        "network_connector_pack_count": len(network_pack_ids),
        "directory_count": len(DIRECTORIES),
        "initial_file_count": len(initial_files) + 1,
        "tax_workpaper_count": len(tax_workpapers),
        "connector_baseline_workpaper_count": len(connector_specs),
        "pilot_readiness_workpaper_created": True,
        "review_artifact_count": 0,
        "connector_baseline_count": 0,
        "credentials_included": False,
        "financial_source_files_copied": False,
        "commands_executed": False,
        "external_actions_performed": False,
    }


def _read_json(path: Path, *, label: str, maximum_bytes: int) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ActivationWorkspaceError(f"{label} must be a regular non-symlink file")
    metadata = path.stat()
    if os.name != "nt" and stat.S_IMODE(metadata.st_mode) != 0o600:
        raise ActivationWorkspaceError(f"{label} must use mode 0600")
    if not 0 < metadata.st_size <= maximum_bytes:
        raise ActivationWorkspaceError(f"{label} exceeds its size boundary")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ActivationWorkspaceError(f"{label} must contain valid JSON") from exc
    if not isinstance(value, dict):
        raise ActivationWorkspaceError(f"{label} must be a JSON object")
    return value


def _inspect_private_tree(root: Path) -> tuple[int, int]:
    file_count = 0
    directory_count = 0
    total_bytes = 0
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        metadata = path.lstat()
        relative = path.relative_to(root).as_posix()
        if stat.S_ISLNK(metadata.st_mode):
            raise ActivationWorkspaceError(
                f"activation workspace must not contain symbolic links: {relative}"
            )
        if stat.S_ISDIR(metadata.st_mode):
            directory_count += 1
            if os.name != "nt" and stat.S_IMODE(metadata.st_mode) != 0o700:
                raise ActivationWorkspaceError(
                    f"activation workspace directory must use mode 0700: {relative}"
                )
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise ActivationWorkspaceError(
                f"activation workspace contains a non-regular file: {relative}"
            )
        file_count += 1
        total_bytes += metadata.st_size
        if file_count > MAX_FILES or metadata.st_size > MAX_FILE_BYTES:
            raise ActivationWorkspaceError("activation workspace exceeds file limits")
        if total_bytes > MAX_TOTAL_BYTES:
            raise ActivationWorkspaceError("activation workspace exceeds 1 GiB")
        if os.name != "nt" and stat.S_IMODE(metadata.st_mode) != 0o600:
            raise ActivationWorkspaceError(
                f"activation workspace file must use mode 0600: {relative}"
            )
    return directory_count, file_count


def _validate_private_relative_reference(
    workspace: Path, value: Any, *, label: str,
) -> None:
    if not isinstance(value, str) or not value or Path(value).is_absolute():
        raise ActivationWorkspaceError(f"{label} must be a private relative path")
    candidate = (workspace / value).resolve()
    try:
        candidate.relative_to(workspace)
    except ValueError as exc:
        raise ActivationWorkspaceError(f"{label} escapes the private workspace") from exc


def _validate_commands_contract(
    runtime: BoxRuntime,
    workspace: Path,
    workspace_manifest: Mapping[str, Any],
    connector_specs: list[dict[str, Any]],
) -> int:
    commands = _read_json(
        workspace / COMMANDS_NAME,
        label="activation command contract",
        maximum_bytes=MAX_MANIFEST_BYTES,
    )
    schema_version = commands.get("schema_version")
    manifest_schema_version = workspace_manifest.get("schema_version")
    expected_schema = 2 if manifest_schema_version in {3, 4, 5} else 1
    if schema_version != expected_schema:
        raise ActivationWorkspaceError(
            "activation command schema does not match the workspace generation"
        )
    fields = {
        "schema_version", "artifact_type", "runtime_fingerprint", "period",
        "steps", "contains_credentials", "contains_financial_values",
        "commands_executed", "external_actions_performed",
    }
    if schema_version == 2:
        fields.add("stage_sequence")
    if set(commands) != fields:
        raise ActivationWorkspaceError("activation command contract fields are invalid")
    snapshot = runtime.snapshot()
    if (
        commands.get("artifact_type") != "first_customer_activation_commands"
        or commands.get("runtime_fingerprint") != snapshot["fingerprint"]
        or commands.get("period") != workspace_manifest.get("period")
        or commands.get("contains_credentials") is not False
        or commands.get("contains_financial_values") is not False
        or commands.get("commands_executed") is not False
        or commands.get("external_actions_performed") is not False
    ):
        raise ActivationWorkspaceError(
            "activation command contract violates its Box or safety boundary"
        )
    if schema_version == 2 and commands.get("stage_sequence") != list(
        COMMAND_STAGE_SEQUENCE
    ):
        raise ActivationWorkspaceError("activation command stage sequence changed")
    steps = commands.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ActivationWorkspaceError("activation command steps are invalid")
    seen: set[str] = set()
    common = {"step_id", "purpose", "action", "command_executed"}
    for step in steps:
        if not isinstance(step, dict):
            raise ActivationWorkspaceError("activation command step must be an object")
        step_id = step.get("step_id")
        if (
            not isinstance(step_id, str) or not step_id
            or step_id in seen
            or not isinstance(step.get("purpose"), str)
            or not step["purpose"].strip()
            or step.get("command_executed") is not False
        ):
            raise ActivationWorkspaceError("activation command step identity is invalid")
        seen.add(step_id)
        action = step.get("action")
        expected_step_fields: set[str]
        if action == "run_cli":
            expected_step_fields = common | {"argv", "shell_preview"}
            if schema_version == 2:
                expected_step_fields.add("requires_operator_edit")
            argv = step.get("argv")
            preview = step.get("shell_preview")
            if (
                not isinstance(argv, list) or len(argv) < 2
                or any(not isinstance(argument, str) or not argument for argument in argv)
                or argv[0] != "opc-finance-box"
                or not isinstance(preview, str)
                or preview != shlex.join(argv)
                or shlex.split(preview) != argv
            ):
                raise ActivationWorkspaceError("activation CLI command is invalid")
            if schema_version == 2 and step.get("requires_operator_edit") is not any(
                "REPLACE_WITH_" in argument for argument in argv
            ):
                raise ActivationWorkspaceError(
                    "activation CLI operator-edit marker is invalid"
                )
        elif action == "edit_private_json":
            expected_step_fields = common | {"relative_file"}
            if schema_version == 2:
                expected_step_fields.add("requires_operator_edit")
                if step.get("requires_operator_edit") is not True:
                    raise ActivationWorkspaceError(
                        "private JSON step must require an operator edit"
                    )
            _validate_private_relative_reference(
                workspace, step.get("relative_file"), label="private JSON step",
            )
        elif schema_version == 2 and action == "edit_private_workbook":
            expected_step_fields = common | {
                "relative_file", "entity_id", "period", "requires_operator_edit",
            }
            _validate_private_relative_reference(
                workspace, step.get("relative_file"), label="private workbook step",
            )
            if (
                step.get("requires_operator_edit") is not True
                or step.get("entity_id") not in workspace_manifest.get("entity_ids", [])
                or step.get("period") != workspace_manifest.get("period")
            ):
                raise ActivationWorkspaceError("private workbook step binding is invalid")
        elif schema_version == 2 and action == "complete_external_prerequisite":
            expected_step_fields = common | {
                "required_entity_ids", "requires_operator_edit",
            }
            if (
                step.get("requires_operator_edit") is not True
                or step.get("required_entity_ids") != workspace_manifest.get("entity_ids")
            ):
                raise ActivationWorkspaceError(
                    "external prerequisite entity scope is invalid"
                )
        elif schema_version == 2 and action == "assemble_private_period_evidence":
            expected_step_fields = common | {
                "relative_directory", "minimum_period_count",
                "requires_operator_edit",
            }
            _validate_private_relative_reference(
                workspace,
                step.get("relative_directory"),
                label="private period evidence step",
            )
            if (
                step.get("requires_operator_edit") is not True
                or step.get("minimum_period_count") != 2
            ):
                raise ActivationWorkspaceError(
                    "private period evidence requirement is invalid"
                )
        else:
            raise ActivationWorkspaceError("activation command action is invalid")
        if set(step) != expected_step_fields:
            raise ActivationWorkspaceError("activation command step fields are invalid")
    if schema_version == 2:
        expected = _commands(
            runtime,
            runtime.config_path.expanduser().resolve(),
            workspace,
            connector_specs,
            period=str(workspace_manifest["period"]),
            facts_as_of=str(workspace_manifest["facts_as_of"]),
            deduplicate_access_scopes=manifest_schema_version == 5,
        )
        if commands != expected:
            raise ActivationWorkspaceError(
                "activation command contract changed from the current Box template"
            )
    return schema_version


def _load_activation_workspace(
    runtime: BoxRuntime, root: str | Path,
) -> tuple[Path, dict[str, Any], dict[str, Path], dict[str, Any]]:
    workspace = _existing_root(root)
    directory_count, file_count = _inspect_private_tree(workspace)
    manifest_path = workspace / MANIFEST_NAME
    manifest = _read_json(
        manifest_path, label="activation workspace manifest",
        maximum_bytes=MAX_MANIFEST_BYTES,
    )
    expected_fields = {
        "schema_version", "artifact_type", "runtime_fingerprint", "period",
        "facts_as_of", "created_at", "prepared_by", "entity_ids",
        "network_connector_pack_ids", "directory_contract", "initial_files",
        "review_artifacts_created", "connector_baselines_created",
        "credentials_included", "financial_source_files_copied",
        "commands_executed", "external_actions_performed",
    }
    schema_version = manifest.get("schema_version")
    if schema_version in {2, 3, 4, MANIFEST_SCHEMA_VERSION}:
        expected_fields.add("connector_baseline_workpaper_count")
    if set(manifest) != expected_fields:
        raise ActivationWorkspaceError("activation workspace manifest fields are invalid")
    directory_contract = {
        1: LEGACY_DIRECTORIES,
        2: V2_DIRECTORIES,
        3: V3_DIRECTORIES,
        4: DIRECTORIES,
        MANIFEST_SCHEMA_VERSION: DIRECTORIES,
    }.get(schema_version, DIRECTORIES)
    snapshot = runtime.snapshot()
    entity_ids = sorted(item["id"] for item in snapshot["entities"])
    if (
        schema_version not in {1, 2, 3, 4, MANIFEST_SCHEMA_VERSION}
        or manifest.get("artifact_type") != "first_customer_activation_workspace"
        or manifest.get("runtime_fingerprint") != snapshot["fingerprint"]
        or manifest.get("entity_ids") != entity_ids
        or manifest.get("directory_contract") != list(directory_contract)
    ):
        raise ActivationWorkspaceError(
            "activation workspace does not match the current Box contract"
        )
    for field in (
        "review_artifacts_created", "connector_baselines_created",
        "credentials_included", "financial_source_files_copied",
        "commands_executed", "external_actions_performed",
    ):
        if manifest.get(field) is not False:
            raise ActivationWorkspaceError(
                f"activation workspace manifest {field} must remain false at initialization"
            )
    for relative in directory_contract:
        directory = workspace / relative
        if directory.is_symlink() or not directory.is_dir():
            raise ActivationWorkspaceError(
                f"activation workspace directory is missing: {relative}"
            )
    initial_files = manifest.get("initial_files")
    if not isinstance(initial_files, list) or not initial_files:
        raise ActivationWorkspaceError("activation workspace initial_files are invalid")
    immutable_count = 0
    for item in initial_files:
        if not isinstance(item, dict) or set(item) != {
            "relative_path", "kind", "mutable", "initial_sha256",
        }:
            raise ActivationWorkspaceError("activation workspace file contract is invalid")
        relative = str(item.get("relative_path") or "")
        candidate = (workspace / relative).resolve()
        try:
            candidate.relative_to(workspace)
        except ValueError as exc:
            raise ActivationWorkspaceError(
                "activation workspace file escapes the private root"
            ) from exc
        if candidate.is_symlink() or not candidate.is_file():
            raise ActivationWorkspaceError(
                f"activation workspace initial file is missing: {relative}"
            )
        if item.get("mutable") is False:
            immutable_count += 1
            if _sha256(candidate.read_bytes()) != item.get("initial_sha256"):
                raise ActivationWorkspaceError(
                    f"activation workspace immutable file changed: {relative}"
                )
    for entity_id in entity_ids:
        workpaper = _read_json(
            workspace / "tax" / "workpapers" / f"{entity_id}.json",
            label="tax applicability workpaper", maximum_bytes=MAX_MANIFEST_BYTES,
        )
        try:
            validate_tax_applicability_workpaper(runtime, workpaper)
        except ValueError as exc:
            raise ActivationWorkspaceError(
                "activation workspace tax workpaper is invalid"
            ) from exc
    connector_specs = (
        _connector_workpaper_specs(
            runtime,
            workspace,
            shared_access_scopes=schema_version == MANIFEST_SCHEMA_VERSION,
        )
        if schema_version in {2, 3, 4, MANIFEST_SCHEMA_VERSION} else []
    )
    if schema_version in {2, 3, 4, MANIFEST_SCHEMA_VERSION}:
        if manifest.get("connector_baseline_workpaper_count") != len(connector_specs):
            raise ActivationWorkspaceError(
                "activation workspace Connector baseline workpaper count changed"
            )
        expected_relatives = {item["workpaper_relative"] for item in connector_specs}
        contracted_relatives = {
            item.get("relative_path") for item in initial_files
            if isinstance(item, dict)
            and item.get("kind") == "mutable_connector_shadow_baseline_workpaper"
        }
        if contracted_relatives != expected_relatives:
            raise ActivationWorkspaceError(
                "activation workspace Connector workpaper scope changed"
            )
        for spec in connector_specs:
            workpaper = _read_json(
                spec["workpaper"], label="Connector Shadow baseline workpaper",
                maximum_bytes=MAX_MANIFEST_BYTES,
            )
            try:
                validate_connector_shadow_baseline_workpaper(runtime, workpaper)
            except ValueError as exc:
                raise ActivationWorkspaceError(
                    "activation workspace Connector baseline workpaper is invalid"
                ) from exc
            if (
                workpaper.get("entity_id") != spec["entity_id"]
                or workpaper.get("pipeline_id") != spec["pipeline_id"]
                or workpaper.get("sample_period") != manifest.get("period")
                or workpaper.get("covered_pack_ids") != spec["covered_pack_ids"]
            ):
                raise ActivationWorkspaceError(
                    "activation workspace Connector workpaper binding changed"
                )
    pilot = _read_json(
        workspace / "pilot" / "readiness" / "workpaper.json",
        label="pilot readiness workpaper", maximum_bytes=MAX_MANIFEST_BYTES,
    )
    if (
        pilot.get("runtime_fingerprint") != snapshot["fingerprint"]
        or pilot.get("period") != manifest.get("period")
        or pilot.get("contains_credentials") is not False
        or pilot.get("contains_raw_source_identifiers") is not False
        or pilot.get("contains_raw_tax_identifiers") is not False
        or pilot.get("contains_financial_values") is not False
        or pilot.get("external_actions_authorized") is not False
    ):
        raise ActivationWorkspaceError(
            "activation workspace pilot workpaper violates the private input boundary"
        )
    command_schema_version = _validate_commands_contract(
        runtime, workspace, manifest, connector_specs,
    )
    expected_network = [
        item["pack_id"] for item in build_connector_shadow_registry_workspace(
            runtime, None, as_of=manifest["facts_as_of"],
        )["pack_coverage"]
    ]
    if manifest.get("network_connector_pack_ids") != expected_network:
        raise ActivationWorkspaceError(
            "activation workspace network Connector scope changed"
        )
    paths = _workspace_paths(workspace, multi_entity=len(entity_ids) > 1)
    summary = {
        "valid": True,
        "runtime_fingerprint": snapshot["fingerprint"],
        "workspace_manifest_sha256": _sha256(manifest_path.read_bytes()),
        "entity_count": len(entity_ids),
        "network_connector_pack_count": len(expected_network),
        "directory_count": directory_count,
        "file_count": file_count,
        "initial_file_count": len(initial_files) + 1,
        "immutable_file_count": immutable_count,
        "command_contract_schema_version": command_schema_version,
        "tax_workpaper_count": len(entity_ids),
        "connector_baseline_workpaper_count": len(connector_specs),
        "pilot_workpaper_private_boundary_valid": True,
        "review_content_validation_deferred_to_stage_verifiers": True,
        "credentials_returned": False,
        "financial_values_returned": False,
        "paths_returned": False,
        "commands_executed": False,
        "external_actions_performed": False,
    }
    return workspace, manifest, paths, summary


def verify_activation_workspace(
    runtime: BoxRuntime, root: str | Path,
) -> dict[str, Any]:
    """Verify private layout and Box binding without returning configured paths."""
    _, _, _, summary = _load_activation_workspace(runtime, root)
    return summary


def build_initialized_activation_status(
    runtime: BoxRuntime,
    services: PackServiceRegistry,
    root: str | Path,
    *,
    as_of: str | None = None,
) -> dict[str, Any]:
    """Evaluate real artifacts placed in an initialized workspace, path-free."""
    workspace, manifest, paths, verification = _load_activation_workspace(runtime, root)
    environment: dict[str, str] = {}
    for name, path in paths.items():
        if name == "OPC_FINANCE_PIPELINE_RUNS_ROOT":
            continue
        if path.is_file() or (
            name in READINESS_DIRECTORY_ENV_NAMES and path.is_dir()
        ):
            environment[name] = str(path)
    activation = build_activation_workspace(
        runtime,
        services,
        runs_root=workspace / "pipeline-runs",
        environ=environment,
        as_of=as_of,
    )
    connector_specs = _connector_workpaper_specs(
        runtime,
        workspace,
        shared_access_scopes=manifest["schema_version"] == MANIFEST_SCHEMA_VERSION,
    )
    access_scopes = [
        {
            "pack_id": pack_id,
            "entity_id": spec["entity_id"],
            "request": spec["access_requests"][pack_id],
            "receipt": spec["access_receipts"][pack_id],
        }
        for spec in connector_specs
        for pack_id in spec["access_pack_ids"]
    ]
    if manifest["schema_version"] == MANIFEST_SCHEMA_VERSION:
        connector_access = build_connector_access_registry(
            runtime,
            access_scopes,
            as_of=as_of,
        )
    else:
        connector_access = {
            "schema_version": 1,
            "artifact_type": "legacy_pipeline_scoped_connector_access",
            "as_of": as_of or datetime.now(timezone.utc).date().isoformat(),
            "summary": {
                "migration_required_for_shared_access_registry": True,
                "ready_for_bounded_shadow_dispatch": False,
            },
            "control_boundary": {
                "private_paths_returned": False,
                "provider_account_identifiers_returned": False,
                "credential_values_returned": False,
                "network_access_performed": False,
                "external_actions_performed": False,
            },
        }
    connector_access_alerts = (
        build_connector_access_alerts(connector_access)
        if connector_access.get("schema_version") == 3
        else {
            "schema_version": 1,
            "artifact_type": "legacy_connector_access_alert_candidates",
            "as_of": connector_access["as_of"],
            "alert_count": 1,
            "critical_count": 0,
            "warning_count": 1,
            "alerts": [{
                "alert_id": "connector-access:registry:migration-required",
                "severity": "warning",
                "category": "registry_migration",
                "status": "migration_required",
                "next_action_id": "initialize_schema_v5_activation_workspace",
            }],
            "migration_required_for_shared_access_registry": True,
            "notification_candidates_only": True,
            "notifications_sent": False,
            "schedule_installed": False,
            "paths_returned": False,
            "credential_values_returned": False,
            "network_access_performed": False,
            "external_actions_performed": False,
        }
    )
    return {
        "schema_version": 1,
        "artifact_type": "initialized_first_customer_activation_status",
        "as_of": activation["as_of"],
        "workspace": verification,
        "activation": activation,
        "connector_access": connector_access,
        "connector_access_alerts": connector_access_alerts,
        "control_boundary": {
            "private_paths_returned": False,
            "credential_values_returned": False,
            "private_artifact_contents_returned": False,
            "provider_account_identifiers_returned": False,
            "connector_access_status_is_current_operational_state_only": True,
            "commands_executed": False,
            "external_actions_performed": False,
        },
    }
