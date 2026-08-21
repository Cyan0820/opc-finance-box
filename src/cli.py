from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from .box_api import build_box_context
from .box_compiler import compile_box_file, write_compiled_box
from .box_upgrade import BoxUpgradeError, compare_compiled_box
from .box_eval import BoxEvalError, run_box_eval_suite
from .box_doctor import diagnose_box
from .box_config import BoxConfigError, load_pack_catalog, resolve_box_file
from .box_runtime import BoxRuntime, BoxRuntimeError
from .box_scaffold import BoxScaffoldError, create_box_config, list_box_options
from .box_builder import build_box_starter_catalog, write_box_candidate_bundle
from .starter_workspace import (
    StarterWorkspaceError,
    initialize_box_starter_workspace,
    initialize_multi_entity_starter_workspace,
)
from .trial_workspace import (
    TrialWorkspaceError,
    build_trial_onboarding_plan,
    initialize_trial_workspace,
    run_trial_workbench,
    verify_trial_workspace,
)
from .handoff_verify import BoxHandoffVerifyError, verify_box_candidate_bundle
from .handoff_receipt import verify_browser_handoff_receipt
from .handoff_unpack import (
    BoxHandoffUnpackError,
    unpack_box_candidate_bundle,
    verify_unpacked_box_candidate,
)
from .source_kit import (
    SourceKitError,
    verify_source_kit_bundle,
    write_source_kit_bundle,
)
from .source_kit_unpack import (
    SourceKitUnpackError,
    unpack_source_kit_bundle,
    verify_unpacked_source_kit,
)
from .release_candidate_audit import audit_release_candidate
from .production_readiness import build_production_readiness_workspace
from .activation_orchestrator import build_activation_workspace
from .activation_workspace import (
    ActivationWorkspaceError,
    build_initialized_activation_status,
    initialize_activation_workspace,
    verify_activation_workspace,
)
from .pilot_shadow_next_period import (
    PilotShadowNextPeriodError,
    initialize_next_pilot_shadow_period,
    verify_next_pilot_shadow_period,
)
from .pilot_shadow_period_runbook import PilotShadowPeriodRunbookStore
from .activation_runbook import ActivationRunbookError, ActivationRunbookStore
from .connector_shadow_registry import build_connector_shadow_registry_workspace
from .connector_onboarding import build_connector_onboarding
from .connector_access_probe import (
    ConnectorAccessProbeError,
    initialize_connector_access_request,
    read_private_connector_access_request,
    renew_connector_access_probe_receipt,
    run_connector_access_probe,
    verify_private_connector_access_probe_receipt,
    verify_private_connector_access_request,
    write_connector_access_probe_receipt,
)
from .connector_access_registry import (
    ConnectorAccessRegistryError,
)
from .box_service_api import BoxServiceRequestError, build_box_bootstrap, dispatch_box_service_request
from .box_pipeline import (
    BoxPipelineError,
    dispatch_box_pipeline_request,
    run_commerce_import_analysis_pipeline,
)
from .connector_sdk import ConnectorError
from .default_connectors import build_box_connector_registry, build_default_connector_registry
from .default_services import build_default_service_registry
from .cfo_metric_evaluator import CfoMetricEvaluationError
from .pack_services import PackServiceError
from .pack_audit import audit_pack_catalog
from .tax_calendar import TaxCalendarError, build_tax_calendar
from .tax_pack_lifecycle import TaxPackLifecycleError, evaluate_tax_rule_lifecycle
from .tax_applicability_artifacts import (
    build_tax_applicability_registry_alerts,
    import_tax_applicability_review,
    inspect_tax_applicability_review_directory,
    TaxApplicabilityArtifactError,
    review_tax_applicability_workpaper,
    verify_tax_applicability_review,
    verify_tax_applicability_review_portfolio,
    verify_tax_applicability_registry_receipt,
    write_tax_applicability_registry_receipt,
    write_tax_applicability_workpaper,
)
from .pilot_readiness import (
    PilotReadinessError,
    build_pilot_readiness_alerts,
    review_pilot_readiness_workpaper,
    verify_pilot_readiness_review,
    write_pilot_readiness_workpaper,
)
from .pilot_data_handoff import (
    PilotDataHandoffError,
    review_pilot_data_handoff_workpaper,
    verify_pilot_data_handoff_review,
    write_pilot_data_handoff_workpaper,
)
from .pilot_shadow_run import (
    PilotShadowRunError,
    register_pilot_shadow_run,
    verify_pilot_shadow_run_registration,
)
from .pilot_shadow_observation import (
    PilotShadowObservationError,
    assemble_pilot_shadow_observation,
    review_pilot_shadow_observation,
    verify_pilot_shadow_observation,
)
from .pilot_shadow_series import (
    PilotShadowSeriesError,
    archive_pilot_shadow_period,
    assemble_pilot_shadow_series,
    review_pilot_shadow_series,
    verify_pilot_shadow_series,
)
from .resource_paths import find_resource_root
from .jurisdiction_scaffold import scaffold_jurisdiction_pack
from .connector_scaffold import ConnectorScaffoldError, scaffold_connector_pack
from .pipeline_run_store import PipelineRunStore, PipelineRunStoreError
from .pipeline_scheduler import (
    PipelineScheduleError, fingerprint_pipeline_request_file,
    inspect_pipeline_schedule, run_due_pipeline_schedule,
)
from .pipeline_observability import build_pipeline_observability, render_pipeline_prometheus
from .api_auth import ApiAuthError, hash_token, load_api_auth_policy
from .distribution_verify import DistributionVerifyError, verify_wheel
from .deployment_smoke import DeploymentSmokeError, run_deployment_smoke
from .deployment_assets import DeploymentAssetError, verify_deployment_assets
from .runtime_storage import (
    RuntimeStorageError,
    backup_runtime_data,
    initialize_runtime_data,
    inspect_runtime_data,
    migrate_runtime_data,
    restore_runtime_backup,
    runtime_upgrade_preflight,
    verify_runtime_backup,
)
from .connector_sync import (
    ConnectorSyncError,
    ConnectorSyncStore,
    build_sync_plan,
    execute_sync_plan,
)
from .airwallex_webhooks import AirwallexWebhookError, AirwallexWebhookStore
from .release_promotion import (
    ReleasePromotionError,
    ReleasePromotionStore,
    build_stable_promotion_evidence_template,
    build_stable_promotion_assessment,
)
from .shadow_close_artifacts import (
    ShadowCloseArtifactError,
    compare_shadow_close_artifacts,
    review_shadow_close_artifact,
    verify_shadow_close_artifact,
    write_shadow_close_template,
)
from .connector_shadow_artifacts import (
    ConnectorShadowArtifactError,
    assess_connector_shadow_artifacts,
    build_connector_shadow_baseline_workpaper,
    finalize_connector_shadow_baseline_workpaper,
    review_connector_shadow_artifact,
    verify_connector_shadow_artifact,
    write_airwallex_shadow_observation,
    write_amazon_seller_shadow_observation,
    write_shopify_stripe_monthly_shadow_observation,
    write_paypal_shadow_observation,
    write_shipbob_shadow_observation,
    write_woocommerce_shadow_observation,
    write_stripe_shadow_observation,
    write_wise_shadow_observation,
    write_xero_shadow_observation,
)
from .shopify_monthly_shadow_request import (
    build_shopify_monthly_shadow_request_template,
    read_private_shopify_monthly_shadow_request,
    validate_shopify_monthly_shadow_request,
    verify_private_shopify_monthly_shadow_request,
)
from .stripe_shadow_request import (
    build_stripe_shadow_request_template,
    read_private_stripe_shadow_request,
    validate_stripe_shadow_request,
    verify_private_stripe_shadow_request,
)
from .wise_shadow_request import (
    build_wise_shadow_request,
    read_private_wise_shadow_request,
    validate_wise_shadow_request,
    verify_private_wise_shadow_request,
)
from .xero_shadow_request import (
    build_xero_shadow_request,
    read_private_xero_shadow_request,
    validate_xero_shadow_request,
    verify_private_xero_shadow_request,
)
from .paypal_shadow_request import (
    build_paypal_shadow_request,
    read_private_paypal_shadow_request,
    validate_paypal_shadow_request,
    verify_private_paypal_shadow_request,
)
from .woocommerce_shadow_request import (
    build_woocommerce_shadow_request,
    read_private_woocommerce_shadow_request,
    validate_woocommerce_shadow_request,
    verify_private_woocommerce_shadow_request,
)
from .shipbob_shadow_request import (
    build_shipbob_shadow_request,
    read_private_shipbob_shadow_request,
    validate_shipbob_shadow_request,
    verify_private_shipbob_shadow_request,
)
from .amazon_seller_shadow_request import (
    build_amazon_seller_shadow_request,
    read_private_amazon_seller_shadow_request,
    validate_amazon_seller_shadow_request,
    verify_private_amazon_seller_shadow_request,
)
from .multi_entity_shadow_close import (
    MultiEntityShadowCloseError,
    assemble_multi_entity_shadow_close_artifact,
    review_multi_entity_shadow_close_artifact,
    verify_multi_entity_shadow_close_artifact,
)


ROOT = find_resource_root()


def _json_file(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def _runtime(config: Path, packs: Path) -> BoxRuntime:
    return BoxRuntime(config, packs)


def _verified_connector_access_binding(
    runtime: BoxRuntime,
    *,
    access_request: Path,
    access_receipt: Path,
    expected_pack_id: str,
    expected_entity_id: str,
    expected_provider_account: str | None = None,
) -> dict[str, Any]:
    verified = verify_private_connector_access_probe_receipt(
        runtime, access_request, access_receipt,
    )
    if (
        verified.get("pack_id") != expected_pack_id
        or verified.get("entity_id") != expected_entity_id
    ):
        raise ConnectorAccessProbeError(
            "Connector access receipt does not match the Shadow request Pack and entity"
        )
    private_request = read_private_connector_access_request(access_request)
    binding = private_request["account_binding"]
    if expected_provider_account is not None and binding.get("shop_domain") != (
        expected_provider_account
    ):
        raise ConnectorAccessProbeError(
            "Connector access receipt does not match the Shadow request provider account"
        )
    return binding


def _entity_attempts(values: list[str]) -> dict[str, str]:
    mappings: dict[str, str] = {}
    for value in values:
        entity_id, separator, attempt_id = value.partition("=")
        entity_id = entity_id.strip()
        attempt_id = attempt_id.strip()
        if not separator or not entity_id or not attempt_id:
            raise PilotShadowRunError(
                "--entity-attempt must use the exact entity_id=attempt_id form"
            )
        if entity_id in mappings:
            raise PilotShadowRunError(
                f"--entity-attempt repeats entity_id: {entity_id}"
            )
        mappings[entity_id] = attempt_id
    return mappings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="opc-finance-box", description="Build and run OPC Finance Boxes")
    parser.add_argument("--packs", type=Path, default=ROOT / "packs", help="Installed Pack catalog")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser(
        "options", help="List installed industries, channels, integrations and jurisdictions"
    )
    subparsers.add_parser(
        "box-starters",
        help="List contract-checked game, DTC and marketplace starters by installed tax Pack",
    )
    starter_init = subparsers.add_parser(
        "starter-init",
        help="Initialize a verified Box workspace from an installed profile and tax country",
    )
    starter_init.add_argument("root", type=Path)
    starter_init.add_argument("--profile", required=True)
    starter_init.add_argument("--country", required=True)
    starter_init.add_argument("--integration", action="append", default=[])
    starter_init.add_argument("--name")
    starter_init.add_argument("--entity-id")
    starter_init.add_argument("--entity-name")
    starter_init.add_argument("--data-mode", choices=("demo", "live"), default="demo")
    starter_init.add_argument("--actor", required=True)
    starter_compose = subparsers.add_parser(
        "starter-compose",
        help="Compose two or more installed same-profile Starters into one verified Box",
    )
    starter_compose.add_argument("root", type=Path)
    starter_compose.add_argument("--profile", required=True)
    starter_compose.add_argument(
        "--entity", action="append", required=True,
        help="Repeat COUNTRY or COUNTRY=entity_id for every legal entity",
    )
    starter_compose.add_argument(
        "--entity-name", action="append", default=[],
        help="Optional entity_id=visible legal entity name override",
    )
    starter_compose.add_argument("--integration", action="append", default=[])
    starter_compose.add_argument(
        "--entity-integration", action="append", default=[],
        help="Repeat entity_id=integration to bind an integration to one legal entity",
    )
    starter_compose.add_argument("--reporting-currency")
    starter_compose.add_argument("--name")
    starter_compose.add_argument("--data-mode", choices=("demo", "live"), default="demo")
    starter_compose.add_argument("--actor", required=True)
    trial_init = subparsers.add_parser(
        "trial-init",
        help="Create a verified local demo workspace from one industry, tax country and integration",
    )
    trial_init.add_argument("root", type=Path)
    trial_init.add_argument("--profile", required=True)
    trial_init.add_argument("--country", required=True)
    trial_init.add_argument("--integration", action="append", default=[])
    trial_init.add_argument("--name")
    trial_init.add_argument("--entity-id")
    trial_init.add_argument("--entity-name")
    trial_init.add_argument("--actor", required=True)
    trial_verify = subparsers.add_parser(
        "trial-verify",
        help="Verify one local trial Box and its separate versioned runtime data layout",
    )
    trial_verify.add_argument("root", type=Path)
    trial_onboarding = subparsers.add_parser(
        "trial-onboarding",
        help="Build a safe founder-facing journey from a verified local trial",
    )
    trial_onboarding.add_argument("root", type=Path)
    trial_run = subparsers.add_parser(
        "trial-run",
        help="Verify and start one local trial workbench without modifying its Box workspace",
    )
    trial_run.add_argument("root", type=Path)
    trial_run.add_argument("--host", default="127.0.0.1")
    trial_run.add_argument("--port", type=int, default=8765)
    trial_run.add_argument("--auth-file", type=Path)
    handoff_bundle = subparsers.add_parser(
        "handoff-bundle",
        help="Write a deterministic, secret-free, first-customer-ready Box handoff ZIP",
    )
    handoff_bundle.add_argument("spec", type=Path)
    handoff_bundle.add_argument("--output", type=Path, required=True)
    handoff_verify = subparsers.add_parser(
        "handoff-verify",
        help="Verify a private Box handoff ZIP against its manifest and installed Packs",
    )
    handoff_verify.add_argument("bundle", type=Path)
    handoff_receipt_verify = subparsers.add_parser(
        "handoff-receipt-verify",
        help="Verify a browser receipt against its private Handoff ZIP and installed Packs",
    )
    handoff_receipt_verify.add_argument("bundle", type=Path)
    handoff_receipt_verify.add_argument("receipt", type=Path)
    handoff_unpack = subparsers.add_parser(
        "handoff-unpack",
        help="Verify and materialize a handoff into a new private workspace",
    )
    handoff_unpack.add_argument("bundle", type=Path)
    handoff_unpack.add_argument("root", type=Path)
    handoff_unpack.add_argument("--actor", required=True)
    handoff_unpack_verify = subparsers.add_parser(
        "handoff-unpack-verify",
        help="Verify a materialized private fork and its non-signing receipt",
    )
    handoff_unpack_verify.add_argument("root", type=Path)
    source_kit_bundle = subparsers.add_parser(
        "source-kit-bundle",
        help="Write a deterministic allowlisted fork-ready source archive",
    )
    source_kit_bundle.add_argument("--output", type=Path, required=True)
    source_kit_verify = subparsers.add_parser(
        "source-kit-verify",
        help="Verify a Source Kit and reproduce it from installed source assets",
    )
    source_kit_verify.add_argument("bundle", type=Path)
    source_kit_unpack = subparsers.add_parser(
        "source-kit-unpack",
        help="Verify and materialize a Source Kit into a new private fork workspace",
    )
    source_kit_unpack.add_argument("bundle", type=Path)
    source_kit_unpack.add_argument("root", type=Path)
    source_kit_unpack.add_argument("--actor", required=True)
    source_kit_unpack_verify = subparsers.add_parser(
        "source-kit-unpack-verify",
        help="Verify a pristine Source Kit workspace and its non-signing receipt",
    )
    source_kit_unpack_verify.add_argument("root", type=Path)
    production_readiness = subparsers.add_parser(
        "production-readiness",
        help="Aggregate tax, Connector, pilot and Shadow gates without exposing private evidence",
    )
    production_readiness.add_argument("config", type=Path)
    production_readiness.add_argument("--as-of", help="Readiness lifecycle clock in YYYY-MM-DD")
    production_readiness.add_argument(
        "--runs-root", type=Path,
        default=Path.cwd() / ".opc-finance-data" / "pipeline-runs",
    )
    connector_preflight = subparsers.add_parser(
        "connector-preflight",
        help="Build a secret-free Pack-level Connector diagnostic and next-action plan",
    )
    connector_preflight.add_argument("config", type=Path)
    connector_access_init = subparsers.add_parser(
        "connector-access-request-init",
        help="Initialize a private provider-account binding request for a read-only access probe",
    )
    connector_access_init.add_argument("config", type=Path)
    connector_access_init.add_argument(
        "--pack", required=True,
        choices=(
            "connector.shopify", "connector.stripe",
            "connector.wise", "connector.xero",
            "connector.paypal", "connector.woocommerce",
            "connector.shipbob", "connector.amazon_seller",
        ),
    )
    connector_access_init.add_argument("--entity", required=True)
    connector_access_init.add_argument("--output", type=Path, required=True)
    connector_access_verify = subparsers.add_parser(
        "connector-access-request-verify",
        help="Verify a mode-0600 provider-account binding request without network access",
    )
    connector_access_verify.add_argument("config", type=Path)
    connector_access_verify.add_argument("request", type=Path)
    connector_access_probe = subparsers.add_parser(
        "connector-access-probe",
        help=(
            "Run an explicitly authorized, bounded Shopify, Stripe, Wise, Xero, "
            "PayPal, WooCommerce, ShipBob or Amazon Seller read-only permission probe"
        ),
    )
    connector_access_probe.add_argument("config", type=Path)
    connector_access_probe.add_argument("request", type=Path)
    connector_access_probe.add_argument(
        "--allow-network", action="store_true",
        help="Explicitly authorize the bounded provider API calls for this invocation",
    )
    connector_access_probe.add_argument(
        "--output", type=Path,
        help="Persist the authorized probe as a new private mode-0600 receipt",
    )
    connector_access_receipt_verify = subparsers.add_parser(
        "connector-access-receipt-verify",
        help="Verify a current private access receipt against its request and Box runtime",
    )
    connector_access_receipt_verify.add_argument("config", type=Path)
    connector_access_receipt_verify.add_argument("request", type=Path)
    connector_access_receipt_verify.add_argument("receipt", type=Path)
    connector_access_receipt_verify.add_argument(
        "--as-of", help="Verification clock as an ISO-8601 timestamp",
    )
    connector_access_receipt_verify.add_argument(
        "--maximum-age-days", type=int, default=30,
    )
    connector_access_receipt_renew = subparsers.add_parser(
        "connector-access-receipt-renew",
        help=(
            "Run a newly authorized access probe, retain the superseded receipt, "
            "and atomically install the new current receipt"
        ),
    )
    connector_access_receipt_renew.add_argument("config", type=Path)
    connector_access_receipt_renew.add_argument("request", type=Path)
    connector_access_receipt_renew.add_argument("receipt", type=Path)
    connector_access_receipt_renew.add_argument(
        "--allow-network", action="store_true",
        help="Explicitly authorize the bounded provider API calls for this renewal",
    )
    activation_status = subparsers.add_parser(
        "activation-status",
        help="Show the dependency-aware first-customer work wave without exposing private evidence",
    )
    activation_status.add_argument("config", type=Path)
    activation_status.add_argument("--as-of", help="Readiness lifecycle clock in YYYY-MM-DD")
    activation_status.add_argument(
        "--runs-root", type=Path,
        default=Path.cwd() / ".opc-finance-data" / "pipeline-runs",
    )
    activation_init = subparsers.add_parser(
        "activation-init",
        help="Initialize a new private first-customer workspace without creating reviews",
    )
    activation_init.add_argument("config", type=Path)
    activation_init.add_argument("root", type=Path)
    activation_init.add_argument("--period", required=True, help="First Shadow period in YYYY-MM")
    activation_init.add_argument("--facts-as-of", required=True, help="Tax facts date in YYYY-MM-DD")
    activation_init.add_argument("--prepared-by", required=True)
    activation_verify = subparsers.add_parser(
        "activation-workspace-verify",
        help="Verify private layout, permissions and active Box binding",
    )
    activation_verify.add_argument("config", type=Path)
    activation_verify.add_argument("root", type=Path)
    activation_workspace_status = subparsers.add_parser(
        "activation-workspace-status",
        help="Evaluate artifacts placed in an initialized private workspace",
    )
    activation_workspace_status.add_argument("config", type=Path)
    activation_workspace_status.add_argument("root", type=Path)
    activation_workspace_status.add_argument("--as-of", help="Readiness lifecycle clock in YYYY-MM-DD")
    connector_access_alerts = subparsers.add_parser(
        "connector-access-alerts",
        help=(
            "Build stable Pack-and-entity access alert candidates from a private "
            "Activation workspace without sending notifications"
        ),
    )
    connector_access_alerts.add_argument("config", type=Path)
    connector_access_alerts.add_argument("activation_root", type=Path)
    connector_access_alerts.add_argument(
        "--as-of", help="Connector access lifecycle clock in YYYY-MM-DD",
    )
    next_period_init = subparsers.add_parser(
        "pilot-shadow-next-period-init",
        help="Initialize the exact next private Shadow period from a verified archive",
    )
    next_period_init.add_argument("config", type=Path)
    next_period_init.add_argument("activation_root", type=Path)
    next_period_init.add_argument("--prepared-by", required=True)
    next_period_init.add_argument(
        "--facts-as-of", required=True,
        help="Next-period readiness and evidence date in YYYY-MM-DD",
    )
    next_period_verify = subparsers.add_parser(
        "pilot-shadow-next-period-verify",
        help="Verify one generated monthly workspace and its archived predecessor",
    )
    next_period_verify.add_argument("config", type=Path)
    next_period_verify.add_argument("activation_root", type=Path)
    next_period_verify.add_argument("period", help="Generated Shadow period in YYYY-MM")
    next_period_verify.add_argument(
        "--as-of", help="Reverification lifecycle clock in YYYY-MM-DD",
    )
    period_runbook_record = subparsers.add_parser(
        "pilot-shadow-period-runbook-record",
        help="Append one non-authoritative progress event for a generated Shadow month",
    )
    period_runbook_record.add_argument("config", type=Path)
    period_runbook_record.add_argument("activation_root", type=Path)
    period_runbook_record.add_argument("period", help="Generated Shadow period in YYYY-MM")
    period_runbook_record.add_argument("step_id")
    period_runbook_record.add_argument(
        "--outcome",
        choices=["reported-complete", "reported-failed", "blocked", "deferred"],
        required=True,
    )
    period_runbook_record.add_argument("--actor", required=True)
    period_runbook_record.add_argument("--rationale", required=True)
    period_runbook_record.add_argument(
        "--evidence-reference", action="append", default=[],
    )
    period_runbook_record.add_argument("--observed-exit-code", type=int)
    period_runbook_status = subparsers.add_parser(
        "pilot-shadow-period-runbook-status",
        help="Show resumable monthly progress without treating it as finance evidence",
    )
    period_runbook_status.add_argument("config", type=Path)
    period_runbook_status.add_argument("activation_root", type=Path)
    period_runbook_status.add_argument("period", help="Generated Shadow period in YYYY-MM")
    period_runbook_verify = subparsers.add_parser(
        "pilot-shadow-period-runbook-verify",
        help="Verify one monthly append-only progress hash chain",
    )
    period_runbook_verify.add_argument("config", type=Path)
    period_runbook_verify.add_argument("activation_root", type=Path)
    period_runbook_verify.add_argument("period", help="Generated Shadow period in YYYY-MM")
    activation_runbook_record = subparsers.add_parser(
        "activation-runbook-record",
        help="Append one non-authoritative operator progress event for a generated activation step",
    )
    activation_runbook_record.add_argument("config", type=Path)
    activation_runbook_record.add_argument("root", type=Path)
    activation_runbook_record.add_argument("step_id")
    activation_runbook_record.add_argument(
        "--outcome",
        choices=["reported-complete", "reported-failed", "blocked", "deferred"],
        required=True,
    )
    activation_runbook_record.add_argument("--actor", required=True)
    activation_runbook_record.add_argument("--rationale", required=True)
    activation_runbook_record.add_argument(
        "--evidence-reference", action="append", default=[],
    )
    activation_runbook_record.add_argument("--observed-exit-code", type=int)
    activation_runbook_status = subparsers.add_parser(
        "activation-runbook-status",
        help="Show resumable reported progress without treating it as evidence completion",
    )
    activation_runbook_status.add_argument("config", type=Path)
    activation_runbook_status.add_argument("root", type=Path)
    activation_runbook_verify = subparsers.add_parser(
        "activation-runbook-verify",
        help="Verify the private append-only activation progress hash chain",
    )
    activation_runbook_verify.add_argument("config", type=Path)
    activation_runbook_verify.add_argument("root", type=Path)
    connector_shadow_status = subparsers.add_parser(
        "connector-shadow-status",
        help="Inspect a private rotating directory of reviewed real Connector Shadow artifacts",
    )
    connector_shadow_status.add_argument("config", type=Path)
    connector_shadow_status.add_argument("--review-dir", type=Path, required=True)
    connector_shadow_status.add_argument("--as-of", help="Evidence lifecycle clock in YYYY-MM-DD")
    connector_shadow_status.add_argument("--maximum-age-days", type=int, default=30)
    subparsers.add_parser("pack-audit", help="Audit Pack capabilities, providers and review gates")
    release_candidate_audit = subparsers.add_parser(
        "release-candidate-audit",
        help="Verify the full game, DTC and marketplace Starter/Tax Pack RC matrix",
    )
    release_candidate_audit.add_argument(
        "--project-root", type=Path,
        help="Installed product resource root; defaults to the active source/share root",
    )
    release_candidate_audit.add_argument(
        "--wheel", type=Path, help="Optional wheel to include in the RC artifact audit",
    )
    release_candidate_audit.add_argument(
        "--source-kit", type=Path,
        help="Optional Source Kit to include in the RC artifact audit",
    )
    distribution_verify = subparsers.add_parser(
        "distribution-verify", help="Verify wheel metadata, entry points and required product assets"
    )
    distribution_verify.add_argument("wheel", type=Path)

    deployment_smoke = subparsers.add_parser(
        "deployment-smoke",
        help="Start the real workbench with isolated data, probe safe readiness endpoints and stop it",
    )
    deployment_smoke.add_argument("config", type=Path)
    deployment_smoke.add_argument("--timeout-seconds", type=int, default=15)

    deployment_assets = subparsers.add_parser(
        "deployment-assets-verify",
        help="Verify bundled container and systemd starters preserve required safety controls",
    )
    deployment_assets.add_argument("root", type=Path, nargs="?", default=ROOT / "deployment")

    runtime_inspect = subparsers.add_parser(
        "runtime-data-inspect", help="Inspect the versioned runtime data layout without changing it"
    )
    runtime_inspect.add_argument("root", type=Path)

    runtime_init = subparsers.add_parser(
        "runtime-data-init", help="Initialize or explicitly adopt a runtime data directory"
    )
    runtime_init.add_argument("root", type=Path)
    runtime_init.add_argument("--actor", required=True)
    runtime_init.add_argument("--adopt-existing", action="store_true")

    runtime_preflight = subparsers.add_parser(
        "runtime-data-upgrade-preflight",
        help="Determine whether runtime data can be used or needs controlled adoption/migration",
    )
    runtime_preflight.add_argument("root", type=Path)

    runtime_backup = subparsers.add_parser(
        "runtime-data-backup", help="Create a verified non-overwriting offline runtime data backup"
    )
    runtime_backup.add_argument("root", type=Path)
    runtime_backup.add_argument("destination", type=Path)
    runtime_backup.add_argument("--actor", required=True)
    runtime_backup.add_argument("--service-stopped-confirmed", action="store_true")

    runtime_backup_verify = subparsers.add_parser(
        "runtime-data-backup-verify", help="Verify a complete runtime data backup"
    )
    runtime_backup_verify.add_argument("backup", type=Path)

    runtime_restore = subparsers.add_parser(
        "runtime-data-restore", help="Restore a verified runtime backup into a new target directory"
    )
    runtime_restore.add_argument("backup", type=Path)
    runtime_restore.add_argument("target", type=Path)
    runtime_restore.add_argument("--actor", required=True)

    runtime_migrate = subparsers.add_parser(
        "runtime-data-migrate", help="Explicitly migrate a stopped v1 runtime after verified backup"
    )
    runtime_migrate.add_argument("root", type=Path)
    runtime_migrate.add_argument("backup", type=Path)
    runtime_migrate.add_argument("--actor", required=True)
    runtime_migrate.add_argument("--service-stopped-confirmed", action="store_true")

    connector_sync_plan = subparsers.add_parser(
        "connector-sync-plan", help="Build a checkpoint-bound incremental or backfill connector plan"
    )
    connector_sync_plan.add_argument("config", type=Path)
    connector_sync_plan.add_argument("connector_id")
    connector_sync_plan.add_argument("--entity", required=True)
    connector_sync_plan.add_argument("--stream", required=True)
    connector_sync_plan.add_argument("--mode", choices=["incremental", "backfill"], required=True)
    connector_sync_plan.add_argument("--window-start")
    connector_sync_plan.add_argument("--window-end", required=True)
    connector_sync_plan.add_argument("--request-base", type=Path)
    connector_sync_plan.add_argument(
        "--sync-root", type=Path,
        default=Path.cwd() / ".opc-finance-data" / "connector-sync",
    )

    connector_sync_run = subparsers.add_parser(
        "connector-sync-run", help="Execute and record one strict connector sync plan"
    )
    connector_sync_run.add_argument("config", type=Path)
    connector_sync_run.add_argument("plan", type=Path)
    connector_sync_run.add_argument("--actor", required=True)
    connector_sync_run.add_argument(
        "--sync-root", type=Path,
        default=Path.cwd() / ".opc-finance-data" / "connector-sync",
    )

    connector_sync_commit = subparsers.add_parser(
        "connector-sync-commit", help="Commit a completed incremental window as the stream checkpoint"
    )
    connector_sync_commit.add_argument("config", type=Path)
    connector_sync_commit.add_argument("attempt_id")
    connector_sync_commit.add_argument("--actor", required=True)
    connector_sync_commit.add_argument("--rationale", required=True)
    connector_sync_commit.add_argument("--evidence-reference", action="append", required=True)
    connector_sync_commit.add_argument(
        "--sync-root", type=Path,
        default=Path.cwd() / ".opc-finance-data" / "connector-sync",
    )

    connector_sync_status = subparsers.add_parser(
        "connector-sync-status", help="List checkpoint candidates and isolated connector failures"
    )
    connector_sync_status.add_argument("config", type=Path)
    connector_sync_status.add_argument("--limit", type=int, default=100)
    connector_sync_status.add_argument(
        "--sync-root", type=Path,
        default=Path.cwd() / ".opc-finance-data" / "connector-sync",
    )

    connector_sync_resolve = subparsers.add_parser(
        "connector-sync-quarantine-resolve", help="Resolve one isolated connector attempt with rationale"
    )
    connector_sync_resolve.add_argument("config", type=Path)
    connector_sync_resolve.add_argument("attempt_id")
    connector_sync_resolve.add_argument("--actor", required=True)
    connector_sync_resolve.add_argument("--resolution", choices=["dismissed", "replaced"], required=True)
    connector_sync_resolve.add_argument("--rationale", required=True)
    connector_sync_resolve.add_argument("--replacement-attempt-id")
    connector_sync_resolve.add_argument(
        "--sync-root", type=Path,
        default=Path.cwd() / ".opc-finance-data" / "connector-sync",
    )

    connector_sync_verify = subparsers.add_parser(
        "connector-sync-verify", help="Verify the connector sync control ledger hash chain"
    )
    connector_sync_verify.add_argument(
        "--sync-root", type=Path,
        default=Path.cwd() / ".opc-finance-data" / "connector-sync",
    )

    airwallex_webhook_status = subparsers.add_parser(
        "airwallex-webhook-status",
        help="List the redacted durable Airwallex Spend webhook queue for one Box",
    )
    airwallex_webhook_status.add_argument("config", type=Path)
    airwallex_webhook_status.add_argument("--limit", type=int, default=100)
    airwallex_webhook_status.add_argument(
        "--webhook-root", type=Path,
        default=Path.cwd() / ".opc-finance-data" / "connector-sync" / "airwallex_webhooks",
    )

    airwallex_webhook_process = subparsers.add_parser(
        "airwallex-webhook-process",
        help="Claim queued Airwallex events and perform read-only expense refetch + review",
    )
    airwallex_webhook_process.add_argument("config", type=Path)
    airwallex_webhook_process.add_argument("--request-base", type=Path, required=True)
    airwallex_webhook_process.add_argument("--actor", required=True)
    airwallex_webhook_process.add_argument("--limit", type=int, default=25)
    airwallex_webhook_process.add_argument(
        "--shadow-output", type=Path,
        help=(
            "Write one private, amount-free schema v2 assessor observation; "
            "requires --limit 1 and refuses overwrite"
        ),
    )
    airwallex_webhook_process.add_argument(
        "--webhook-root", type=Path,
        default=Path.cwd() / ".opc-finance-data" / "connector-sync" / "airwallex_webhooks",
    )

    airwallex_webhook_resolve = subparsers.add_parser(
        "airwallex-webhook-quarantine-resolve",
        help="Retry or dismiss one quarantined Airwallex event with review evidence",
    )
    airwallex_webhook_resolve.add_argument("config", type=Path)
    airwallex_webhook_resolve.add_argument("receipt_id")
    airwallex_webhook_resolve.add_argument("--resolution", choices=["retry", "dismissed"], required=True)
    airwallex_webhook_resolve.add_argument("--actor", required=True)
    airwallex_webhook_resolve.add_argument("--rationale", required=True)
    airwallex_webhook_resolve.add_argument("--evidence-reference", action="append", required=True)
    airwallex_webhook_resolve.add_argument(
        "--webhook-root", type=Path,
        default=Path.cwd() / ".opc-finance-data" / "connector-sync" / "airwallex_webhooks",
    )

    airwallex_webhook_verify = subparsers.add_parser(
        "airwallex-webhook-verify", help="Verify the Airwallex webhook ledger hash chain",
    )
    airwallex_webhook_verify.add_argument(
        "--webhook-root", type=Path,
        default=Path.cwd() / ".opc-finance-data" / "connector-sync" / "airwallex_webhooks",
    )

    promotion_assess = subparsers.add_parser(
        "promotion-assess",
        help="Evaluate strict Shadow Close and operational evidence for one Pack stable candidate",
    )
    promotion_assess.add_argument("config", type=Path)
    promotion_assess.add_argument("evidence", type=Path)

    promotion_template = subparsers.add_parser(
        "promotion-template",
        help="Write a Box/Pack-bound, deliberately incomplete stable evidence starter",
    )
    promotion_template.add_argument("config", type=Path)
    promotion_template.add_argument("pack_id")
    promotion_template.add_argument("--output", type=Path, required=True)

    promotion_record = subparsers.add_parser(
        "promotion-record",
        help="Evaluate and append a secret-safe stable-candidate assessment",
    )
    promotion_record.add_argument("config", type=Path)
    promotion_record.add_argument("evidence", type=Path)
    promotion_record.add_argument("--actor", required=True)
    promotion_record.add_argument(
        "--promotion-root", type=Path,
        default=Path.cwd() / ".opc-finance-data" / "release_promotion",
    )

    promotion_review = subparsers.add_parser(
        "promotion-review",
        help="Independently approve, reject or request evidence for one exact stable assessment",
    )
    promotion_review.add_argument("config", type=Path)
    promotion_review.add_argument("assessment_id")
    promotion_review.add_argument(
        "--decision", required=True,
        choices=["approved", "rejected", "needs_more_evidence"],
    )
    promotion_review.add_argument("--actor", required=True)
    promotion_review.add_argument("--rationale", required=True)
    promotion_review.add_argument("--evidence-reference", action="append", required=True)
    promotion_review.add_argument(
        "--promotion-root", type=Path,
        default=Path.cwd() / ".opc-finance-data" / "release_promotion",
    )

    promotion_status = subparsers.add_parser(
        "promotion-status", help="List stable-candidate assessments and independent review state"
    )
    promotion_status.add_argument("config", type=Path)
    promotion_status.add_argument("--limit", type=int, default=100)
    promotion_status.add_argument(
        "--promotion-root", type=Path,
        default=Path.cwd() / ".opc-finance-data" / "release_promotion",
    )

    promotion_verify = subparsers.add_parser(
        "promotion-verify", help="Verify the stable-promotion evidence ledger hash chain"
    )
    promotion_verify.add_argument(
        "--promotion-root", type=Path,
        default=Path.cwd() / ".opc-finance-data" / "release_promotion",
    )

    shadow_template = subparsers.add_parser(
        "shadow-close-template",
        help="Write a Box-scoped, blank human-close baseline workbook",
    )
    shadow_template.add_argument("config", type=Path)
    shadow_template.add_argument("--output", type=Path, required=True)

    shadow_compare = subparsers.add_parser(
        "shadow-close-compare",
        help="Compare one human baseline workbook with one scoped finance result JSON",
    )
    shadow_compare.add_argument("config", type=Path)
    shadow_compare.add_argument("baseline", type=Path)
    shadow_compare.add_argument("finance", type=Path)
    shadow_compare.add_argument("--output", type=Path, required=True)

    shadow_review = subparsers.add_parser(
        "shadow-close-review",
        help="Independently sign one exact Shadow Close report into a new private artifact",
    )
    shadow_review.add_argument("config", type=Path)
    shadow_review.add_argument("report", type=Path)
    shadow_review.add_argument(
        "--decision",
        choices=["passed", "accepted-differences", "needs-correction"],
        required=True,
    )
    shadow_review.add_argument("--actor", required=True)
    shadow_review.add_argument("--rationale", required=True)
    shadow_review.add_argument("--evidence-reference", action="append", default=[])
    shadow_review.add_argument("--resolutions", type=Path)
    shadow_review.add_argument("--output", type=Path, required=True)

    shadow_verify = subparsers.add_parser(
        "shadow-close-verify",
        help="Verify a private Shadow Close report without printing its financial values",
    )
    shadow_verify.add_argument("config", type=Path)
    shadow_verify.add_argument("report", type=Path)

    connector_shadow_assess = subparsers.add_parser(
        "connector-shadow-assess",
        help="Bind an independent source-count baseline to one supported Pipeline result or safe observation",
    )
    connector_shadow_assess.add_argument("config", type=Path)
    connector_shadow_assess.add_argument("baseline", type=Path)
    connector_shadow_assess.add_argument("pipeline_result", type=Path)
    connector_shadow_assess.add_argument("--output", type=Path, required=True)

    connector_shadow_baseline_init = subparsers.add_parser(
        "connector-shadow-baseline-init",
        help="Create a private, incomplete real-source Connector Shadow baseline workpaper",
    )
    connector_shadow_baseline_init.add_argument("config", type=Path)
    connector_shadow_baseline_init.add_argument(
        "--pipeline",
        choices=[
            "stripe.daily_close",
            "dtc.shopify_stripe_daily_close",
            "dtc.shopify_stripe_month_close",
            "finance.expense_evidence_review",
            "finance.bank_statement_close",
            "finance.trial_balance_review",
        ],
        required=True,
    )
    connector_shadow_baseline_init.add_argument("--entity", required=True)
    connector_shadow_baseline_init.add_argument("--period", required=True)
    connector_shadow_baseline_init.add_argument("--prepared-by", required=True)
    connector_shadow_baseline_init.add_argument("--output", type=Path, required=True)

    connector_shadow_baseline_finalize = subparsers.add_parser(
        "connector-shadow-baseline-finalize",
        help="Validate and seal a completed real_anonymized Connector Shadow baseline",
    )
    connector_shadow_baseline_finalize.add_argument("config", type=Path)
    connector_shadow_baseline_finalize.add_argument("workpaper", type=Path)
    connector_shadow_baseline_finalize.add_argument("--output", type=Path, required=True)

    shopify_monthly_shadow_request_init = subparsers.add_parser(
        "shopify-monthly-shadow-request-init",
        help=(
            "Create a private incomplete live monthly request with exact Shopify and "
            "Stripe calendar-month bounds"
        ),
    )
    shopify_monthly_shadow_request_init.add_argument("config", type=Path)
    shopify_monthly_shadow_request_init.add_argument("--entity", required=True)
    shopify_monthly_shadow_request_init.add_argument("--period", required=True)
    shopify_monthly_shadow_request_init.add_argument("--output", type=Path, required=True)

    shopify_monthly_shadow_request_verify = subparsers.add_parser(
        "shopify-monthly-shadow-request-verify",
        help=(
            "Verify one private live monthly request without returning its store or source IDs"
        ),
    )
    shopify_monthly_shadow_request_verify.add_argument("config", type=Path)
    shopify_monthly_shadow_request_verify.add_argument("request", type=Path)

    stripe_shadow_request_init = subparsers.add_parser(
        "stripe-shadow-request-init",
        help=(
            "Create a private incomplete Stripe Balance/Payout request with exact "
            "calendar-month bounds and a bank-evidence placeholder"
        ),
    )
    stripe_shadow_request_init.add_argument("config", type=Path)
    stripe_shadow_request_init.add_argument("--entity", required=True)
    stripe_shadow_request_init.add_argument("--period", required=True)
    stripe_shadow_request_init.add_argument("--output", type=Path, required=True)

    stripe_shadow_request_verify = subparsers.add_parser(
        "stripe-shadow-request-verify",
        help=(
            "Verify a private Stripe live request without returning bank IDs, "
            "references or amounts"
        ),
    )
    stripe_shadow_request_verify.add_argument("config", type=Path)
    stripe_shadow_request_verify.add_argument("request", type=Path)

    stripe_shadow_observe = subparsers.add_parser(
        "stripe-shadow-observe",
        help=(
            "Run one live Stripe Balance/Payout close and persist only amount-, "
            "bank-reference- and raw-id-free Shadow controls"
        ),
    )
    stripe_shadow_observe.add_argument("config", type=Path)
    stripe_shadow_observe.add_argument("request", type=Path)
    stripe_shadow_observe.add_argument("--output", type=Path, required=True)
    stripe_shadow_observe.add_argument("--access-request", type=Path, required=True)
    stripe_shadow_observe.add_argument("--access-receipt", type=Path, required=True)

    xero_shadow_observe = subparsers.add_parser(
        "xero-shadow-observe",
        help="Run one live Xero Trial Balance Pipeline and persist only amount-free Shadow controls",
    )
    xero_shadow_observe.add_argument("config", type=Path)
    xero_shadow_observe.add_argument("request", type=Path)
    xero_shadow_observe.add_argument("--output", type=Path, required=True)
    xero_shadow_observe.add_argument("--access-request", type=Path, required=True)
    xero_shadow_observe.add_argument("--access-receipt", type=Path, required=True)

    xero_shadow_request_init = subparsers.add_parser(
        "xero-shadow-request-init",
        help=(
            "Create a complete private Xero Trial Balance request bound to one entity "
            "and calendar month end"
        ),
    )
    xero_shadow_request_init.add_argument("config", type=Path)
    xero_shadow_request_init.add_argument("--entity", required=True)
    xero_shadow_request_init.add_argument("--period", required=True)
    xero_shadow_request_init.add_argument("--output", type=Path, required=True)

    xero_shadow_request_verify = subparsers.add_parser(
        "xero-shadow-request-verify",
        help=(
            "Verify a private Xero Trial Balance request without reading OAuth data or the network"
        ),
    )
    xero_shadow_request_verify.add_argument("config", type=Path)
    xero_shadow_request_verify.add_argument("request", type=Path)

    wise_shadow_observe = subparsers.add_parser(
        "wise-shadow-observe",
        help="Run one live Wise monthly statement Pipeline and persist only amount-free Shadow controls",
    )
    wise_shadow_observe.add_argument("config", type=Path)
    wise_shadow_observe.add_argument("request", type=Path)
    wise_shadow_observe.add_argument("--output", type=Path, required=True)
    wise_shadow_observe.add_argument("--access-request", type=Path, required=True)
    wise_shadow_observe.add_argument("--access-receipt", type=Path, required=True)

    wise_shadow_request_init = subparsers.add_parser(
        "wise-shadow-request-init",
        help=(
            "Create a complete private Wise request bound to one entity, functional "
            "currency and exact calendar month"
        ),
    )
    wise_shadow_request_init.add_argument("config", type=Path)
    wise_shadow_request_init.add_argument("--entity", required=True)
    wise_shadow_request_init.add_argument("--period", required=True)
    wise_shadow_request_init.add_argument("--output", type=Path, required=True)

    wise_shadow_request_verify = subparsers.add_parser(
        "wise-shadow-request-verify",
        help=(
            "Verify a private Wise monthly request without reading credentials or the network"
        ),
    )
    wise_shadow_request_verify.add_argument("config", type=Path)
    wise_shadow_request_verify.add_argument("request", type=Path)

    paypal_shadow_observe = subparsers.add_parser(
        "paypal-shadow-observe",
        help=(
            "Run one live PayPal Transaction Search month and persist only amount-, "
            "PII- and raw-id-free Shadow controls"
        ),
    )
    paypal_shadow_observe.add_argument("config", type=Path)
    paypal_shadow_observe.add_argument("request", type=Path)
    paypal_shadow_observe.add_argument("--output", type=Path, required=True)
    paypal_shadow_observe.add_argument("--access-request", type=Path, required=True)
    paypal_shadow_observe.add_argument("--access-receipt", type=Path, required=True)

    paypal_shadow_request_init = subparsers.add_parser(
        "paypal-shadow-request-init",
        help=(
            "Create a complete private production PayPal request bound to one entity "
            "and exact calendar month"
        ),
    )
    paypal_shadow_request_init.add_argument("config", type=Path)
    paypal_shadow_request_init.add_argument("--entity", required=True)
    paypal_shadow_request_init.add_argument("--period", required=True)
    paypal_shadow_request_init.add_argument("--output", type=Path, required=True)

    paypal_shadow_request_verify = subparsers.add_parser(
        "paypal-shadow-request-verify",
        help=(
            "Verify a private PayPal request without reading OAuth credentials or the network"
        ),
    )
    paypal_shadow_request_verify.add_argument("config", type=Path)
    paypal_shadow_request_verify.add_argument("request", type=Path)

    woocommerce_shadow_observe = subparsers.add_parser(
        "woocommerce-shadow-observe",
        help=(
            "Run one live WooCommerce order/refund month and persist only amount-, "
            "site-, customer-, product- and raw-id-free Shadow controls"
        ),
    )
    woocommerce_shadow_observe.add_argument("config", type=Path)
    woocommerce_shadow_observe.add_argument("request", type=Path)
    woocommerce_shadow_observe.add_argument("--output", type=Path, required=True)
    woocommerce_shadow_observe.add_argument("--access-request", type=Path, required=True)
    woocommerce_shadow_observe.add_argument("--access-receipt", type=Path, required=True)

    woocommerce_shadow_request_init = subparsers.add_parser(
        "woocommerce-shadow-request-init",
        help=(
            "Create a complete private production WooCommerce request bound to one "
            "entity and exact calendar month"
        ),
    )
    woocommerce_shadow_request_init.add_argument("config", type=Path)
    woocommerce_shadow_request_init.add_argument("--entity", required=True)
    woocommerce_shadow_request_init.add_argument("--period", required=True)
    woocommerce_shadow_request_init.add_argument("--output", type=Path, required=True)

    woocommerce_shadow_request_verify = subparsers.add_parser(
        "woocommerce-shadow-request-verify",
        help=(
            "Verify a private WooCommerce request without reading site credentials or the network"
        ),
    )
    woocommerce_shadow_request_verify.add_argument("config", type=Path)
    woocommerce_shadow_request_verify.add_argument("request", type=Path)

    shipbob_shadow_observe = subparsers.add_parser(
        "shipbob-shadow-observe",
        help=(
            "Run one live ShipBob fulfillment month and persist only amount-, merchant-, "
            "customer-, inventory- and raw-id-free Shadow controls"
        ),
    )
    shipbob_shadow_observe.add_argument("config", type=Path)
    shipbob_shadow_observe.add_argument("request", type=Path)
    shipbob_shadow_observe.add_argument("--output", type=Path, required=True)
    shipbob_shadow_observe.add_argument("--access-request", type=Path, required=True)
    shipbob_shadow_observe.add_argument("--access-receipt", type=Path, required=True)

    shipbob_shadow_request_init = subparsers.add_parser(
        "shipbob-shadow-request-init",
        help=(
            "Create a complete private production ShipBob request bound to one entity "
            "and exact calendar month"
        ),
    )
    shipbob_shadow_request_init.add_argument("config", type=Path)
    shipbob_shadow_request_init.add_argument("--entity", required=True)
    shipbob_shadow_request_init.add_argument("--period", required=True)
    shipbob_shadow_request_init.add_argument("--output", type=Path, required=True)

    shipbob_shadow_request_verify = subparsers.add_parser(
        "shipbob-shadow-request-verify",
        help=(
            "Verify a private ShipBob request without reading access tokens or the network"
        ),
    )
    shipbob_shadow_request_verify.add_argument("config", type=Path)
    shipbob_shadow_request_verify.add_argument("request", type=Path)

    amazon_seller_shadow_observe = subparsers.add_parser(
        "amazon-seller-shadow-observe",
        help=(
            "Run one live Amazon Orders/FBA Inventory/Finances month and persist only "
            "amount-, seller-, marketplace-, buyer-, product- and raw-id-free controls"
        ),
    )
    amazon_seller_shadow_observe.add_argument("config", type=Path)
    amazon_seller_shadow_observe.add_argument("request", type=Path)
    amazon_seller_shadow_observe.add_argument("--output", type=Path, required=True)
    amazon_seller_shadow_observe.add_argument(
        "--access-request", type=Path, required=True,
    )
    amazon_seller_shadow_observe.add_argument(
        "--access-receipt", type=Path, required=True,
    )

    amazon_seller_shadow_request_init = subparsers.add_parser(
        "amazon-seller-shadow-request-init",
        help=(
            "Create a complete private production Amazon three-source request bound to "
            "one entity, Marketplace and closed calendar month"
        ),
    )
    amazon_seller_shadow_request_init.add_argument("config", type=Path)
    amazon_seller_shadow_request_init.add_argument("--entity", required=True)
    amazon_seller_shadow_request_init.add_argument("--period", required=True)
    amazon_seller_shadow_request_init.add_argument("--marketplace-id", required=True)
    amazon_seller_shadow_request_init.add_argument("--output", type=Path, required=True)

    amazon_seller_shadow_request_verify = subparsers.add_parser(
        "amazon-seller-shadow-request-verify",
        help=(
            "Verify a private Amazon three-source request without reading Seller/LWA values or the network"
        ),
    )
    amazon_seller_shadow_request_verify.add_argument("config", type=Path)
    amazon_seller_shadow_request_verify.add_argument("request", type=Path)

    shopify_monthly_shadow_observe = subparsers.add_parser(
        "shopify-monthly-shadow-observe",
        help=(
            "Run one live Shopify + Stripe month close and persist only amount-, store- "
            "and raw-id-free Shadow controls"
        ),
    )
    shopify_monthly_shadow_observe.add_argument("config", type=Path)
    shopify_monthly_shadow_observe.add_argument("request", type=Path)
    shopify_monthly_shadow_observe.add_argument("--output", type=Path, required=True)
    shopify_monthly_shadow_observe.add_argument(
        "--shopify-access-request", type=Path, required=True,
    )
    shopify_monthly_shadow_observe.add_argument(
        "--shopify-access-receipt", type=Path, required=True,
    )
    shopify_monthly_shadow_observe.add_argument(
        "--stripe-access-request", type=Path, required=True,
    )
    shopify_monthly_shadow_observe.add_argument(
        "--stripe-access-receipt", type=Path, required=True,
    )

    connector_shadow_review = subparsers.add_parser(
        "connector-shadow-review",
        help="Independently review one exact Connector Shadow assessment",
    )
    connector_shadow_review.add_argument("config", type=Path)
    connector_shadow_review.add_argument("assessment", type=Path)
    connector_shadow_review.add_argument(
        "--decision", choices=sorted({"passed", "accepted-differences", "needs-correction"}),
        required=True,
    )
    connector_shadow_review.add_argument("--actor", required=True)
    connector_shadow_review.add_argument("--rationale", required=True)
    connector_shadow_review.add_argument("--evidence-reference", action="append", required=True)
    connector_shadow_review.add_argument("--output", type=Path, required=True)

    connector_shadow_verify = subparsers.add_parser(
        "connector-shadow-verify",
        help="Verify a reviewed no-financial-values Connector Shadow assessment",
    )
    connector_shadow_verify.add_argument("config", type=Path)
    connector_shadow_verify.add_argument("assessment", type=Path)

    shadow_portfolio_assemble = subparsers.add_parser(
        "shadow-close-portfolio-assemble",
        help="Bind every reviewed entity Shadow Close to one ledger-verified portfolio",
    )
    shadow_portfolio_assemble.add_argument("config", type=Path)
    shadow_portfolio_assemble.add_argument(
        "--entity-report", type=Path, action="append", required=True,
    )
    shadow_portfolio_assemble.add_argument("--portfolio-result", type=Path, required=True)
    shadow_portfolio_assemble.add_argument("--output", type=Path, required=True)

    shadow_portfolio_review = subparsers.add_parser(
        "shadow-close-portfolio-review",
        help="Independently review one exact no-values portfolio acceptance manifest",
    )
    shadow_portfolio_review.add_argument("config", type=Path)
    shadow_portfolio_review.add_argument("manifest", type=Path)
    shadow_portfolio_review.add_argument(
        "--decision",
        choices=["passed", "accepted-differences", "needs-correction"],
        required=True,
    )
    shadow_portfolio_review.add_argument("--actor", required=True)
    shadow_portfolio_review.add_argument("--rationale", required=True)
    shadow_portfolio_review.add_argument(
        "--evidence-reference", action="append", required=True,
    )
    shadow_portfolio_review.add_argument("--output", type=Path, required=True)

    shadow_portfolio_verify = subparsers.add_parser(
        "shadow-close-portfolio-verify",
        help="Verify a reviewed portfolio acceptance manifest without printing finance values",
    )
    shadow_portfolio_verify.add_argument("config", type=Path)
    shadow_portfolio_verify.add_argument("manifest", type=Path)

    auth_hash = subparsers.add_parser(
        "auth-token-hash", help="Hash a high-entropy bearer token from an environment variable"
    )
    auth_hash.add_argument("--token-env", default="OPC_FINANCE_TOKEN_TO_HASH")

    auth_validate = subparsers.add_parser(
        "auth-policy-validate", help="Validate a role-based API auth policy without exposing hashes"
    )
    auth_validate.add_argument("policy", type=Path)

    eval_command = subparsers.add_parser("eval", help="Run a declarative Box/Pack finance boundary eval suite")
    eval_command.add_argument("suite", type=Path)
    eval_command.add_argument("--project-root", type=Path)

    create = subparsers.add_parser("create", help="Create strict Box config from a simplified spec")
    create.add_argument("spec", type=Path)
    create.add_argument("--output", type=Path, required=True)

    jurisdiction = subparsers.add_parser(
        "jurisdiction-init", help="Scaffold a source-backed design-only jurisdiction Pack"
    )
    jurisdiction.add_argument("--output-root", type=Path, required=True)
    jurisdiction.add_argument("--slug", required=True)
    jurisdiction.add_argument("--country-code", required=True)
    jurisdiction.add_argument("--display-name", required=True)
    jurisdiction.add_argument("--source-authority", required=True)
    jurisdiction.add_argument("--source-title", required=True)
    jurisdiction.add_argument("--source-url", required=True)
    jurisdiction.add_argument("--verified-at", required=True)
    jurisdiction.add_argument("--rules-effective-at", required=True)

    connector_init = subparsers.add_parser("connector-init", help="Scaffold a local editable API Connector Pack")
    connector_init.add_argument("--output-root", type=Path, required=True)
    connector_init.add_argument("--slug", required=True)
    connector_init.add_argument("--display-name", required=True)
    connector_init.add_argument("--secret-env", required=True)
    connector_init.add_argument("--base-url", required=True)

    validate = subparsers.add_parser("validate", help="Validate and resolve a Box config")
    validate.add_argument("config", type=Path)

    compile_command = subparsers.add_parser("compile", help="Compile deployment lock and setup checklist")
    compile_command.add_argument("config", type=Path)
    compile_command.add_argument("--output", type=Path, required=True)

    upgrade = subparsers.add_parser("upgrade-check", help="Compare a previous compiled lock to current Box sources")
    upgrade.add_argument("config", type=Path)
    upgrade.add_argument("baseline", type=Path)

    context = subparsers.add_parser("context", help="Print runtime Box context")
    context.add_argument("config", type=Path)
    context.add_argument("--scope", choices=["management", "statutory"], default="management")
    context.add_argument("--entity")
    context.add_argument("--entities", nargs="+")

    services = subparsers.add_parser("services", help="List services enabled for a Box")
    services.add_argument("config", type=Path)

    connectors = subparsers.add_parser("connectors", help="List connectors enabled for a Box")
    connectors.add_argument("config", type=Path)

    import_command = subparsers.add_parser("import", help="Run one connector request JSON")
    import_command.add_argument("config", type=Path)
    import_command.add_argument("connector_id")
    import_command.add_argument("request", type=Path)

    pipeline = subparsers.add_parser("commerce-pipeline", help="Import, quality-gate and analyze Commerce data")
    pipeline.add_argument("config", type=Path)
    pipeline.add_argument("connector_id")
    pipeline.add_argument("request", type=Path)

    box_pipeline = subparsers.add_parser("pipeline", help="Run one validated Connector-to-Service pipeline")
    box_pipeline.add_argument("config", type=Path)
    box_pipeline.add_argument("request", type=Path)
    box_pipeline.add_argument("--record", action="store_true", help="Append a secret-free control record")
    box_pipeline.add_argument(
        "--verify-source-runs", action="store_true",
        help="Verify portfolio source summaries against reviewed Pipeline run records",
    )
    box_pipeline.add_argument("--runs-root", type=Path, default=Path.cwd() / ".opc-finance-data" / "pipeline-runs")
    box_pipeline.add_argument("--actor", default="CLI operator")

    pipeline_runs = subparsers.add_parser("pipeline-runs", help="List recorded Pipeline attempts for one Box")
    pipeline_runs.add_argument("config", type=Path)
    pipeline_runs.add_argument("--runs-root", type=Path, default=Path.cwd() / ".opc-finance-data" / "pipeline-runs")
    pipeline_runs.add_argument("--pipeline-id")
    pipeline_runs.add_argument("--entity")
    pipeline_runs.add_argument("--limit", type=int, default=50)

    pipeline_run_show = subparsers.add_parser("pipeline-run-show", help="Read one recorded Pipeline attempt")
    pipeline_run_show.add_argument("config", type=Path)
    pipeline_run_show.add_argument("attempt_id")
    pipeline_run_show.add_argument("--runs-root", type=Path, default=Path.cwd() / ".opc-finance-data" / "pipeline-runs")

    pipeline_run_review = subparsers.add_parser(
        "pipeline-run-review", help="Append a human review decision to one Pipeline attempt"
    )
    pipeline_run_review.add_argument("config", type=Path)
    pipeline_run_review.add_argument("attempt_id")
    pipeline_run_review.add_argument("--gate", required=True)
    pipeline_run_review.add_argument(
        "--decision", required=True,
        choices=["approved", "rejected", "needs_more_evidence"],
    )
    pipeline_run_review.add_argument("--actor", required=True)
    pipeline_run_review.add_argument("--rationale", required=True)
    pipeline_run_review.add_argument("--evidence-reference", action="append", default=[])
    pipeline_run_review.add_argument(
        "--runs-root", type=Path,
        default=Path.cwd() / ".opc-finance-data" / "pipeline-runs",
    )

    pipeline_review_queue = subparsers.add_parser(
        "pipeline-review-queue", help="List unresolved human review gates for one Box"
    )
    pipeline_review_queue.add_argument("config", type=Path)
    pipeline_review_queue.add_argument(
        "--runs-root", type=Path,
        default=Path.cwd() / ".opc-finance-data" / "pipeline-runs",
    )
    pipeline_review_queue.add_argument("--pipeline-id")
    pipeline_review_queue.add_argument("--entity")
    pipeline_review_queue.add_argument("--limit", type=int, default=100)

    pipeline_runs_verify = subparsers.add_parser(
        "pipeline-runs-verify", help="Verify the Pipeline ledger hash chain"
    )
    pipeline_runs_verify.add_argument("config", type=Path)
    pipeline_runs_verify.add_argument(
        "--runs-root", type=Path,
        default=Path.cwd() / ".opc-finance-data" / "pipeline-runs",
    )

    pipeline_runs_backup = subparsers.add_parser(
        "pipeline-runs-backup", help="Create a non-overwriting verified backup of a Pipeline ledger"
    )
    pipeline_runs_backup.add_argument("destination", type=Path)
    pipeline_runs_backup.add_argument("--actor", required=True)
    pipeline_runs_backup.add_argument(
        "--runs-root", type=Path,
        default=Path.cwd() / ".opc-finance-data" / "pipeline-runs",
    )

    pipeline_backup_verify = subparsers.add_parser(
        "pipeline-backup-verify", help="Verify a Pipeline ledger backup and hash chain"
    )
    pipeline_backup_verify.add_argument("backup", type=Path)

    pipeline_runs_restore = subparsers.add_parser(
        "pipeline-runs-restore", help="Restore a verified backup into an empty Pipeline ledger target"
    )
    pipeline_runs_restore.add_argument("backup", type=Path)
    pipeline_runs_restore.add_argument("--actor", required=True)
    pipeline_runs_restore.add_argument(
        "--runs-root", type=Path,
        default=Path.cwd() / ".opc-finance-data" / "pipeline-runs",
    )

    schedule_inspect = subparsers.add_parser(
        "pipeline-schedule-inspect",
        help="Inspect due/retry state without dispatching a scheduled Pipeline",
    )
    schedule_inspect.add_argument("config", type=Path)
    schedule_inspect.add_argument("schedule", type=Path)
    schedule_inspect.add_argument(
        "--runs-root", type=Path,
        default=Path.cwd() / ".opc-finance-data" / "pipeline-runs",
    )
    schedule_inspect.add_argument("--now", help="ISO-8601 test/operations clock with timezone")

    request_fingerprint = subparsers.add_parser(
        "pipeline-request-fingerprint",
        help="Fingerprint one Pipeline request for schedule approval without returning its content",
    )
    request_fingerprint.add_argument("request", type=Path)

    schedule_run = subparsers.add_parser(
        "pipeline-schedule-run",
        help="Lease, dispatch and record Pipeline jobs due in the current schedule window",
    )
    schedule_run.add_argument("config", type=Path)
    schedule_run.add_argument("schedule", type=Path)
    schedule_run.add_argument("--actor", required=True)
    schedule_run.add_argument("--job-id")
    schedule_run.add_argument(
        "--runs-root", type=Path,
        default=Path.cwd() / ".opc-finance-data" / "pipeline-runs",
    )
    schedule_run.add_argument("--now", help="ISO-8601 test/operations clock with timezone")

    observability = subparsers.add_parser(
        "pipeline-observability",
        help="Export secret-free Pipeline metrics and derived alert state without sending notifications",
    )
    observability.add_argument("config", type=Path)
    observability.add_argument("--schedule", type=Path)
    observability.add_argument(
        "--runs-root", type=Path,
        default=Path.cwd() / ".opc-finance-data" / "pipeline-runs",
    )
    observability.add_argument("--stale-review-hours", type=int, default=24)
    observability.add_argument("--now", help="ISO-8601 test/operations clock with timezone")
    observability.add_argument("--prometheus", action="store_true")

    doctor = subparsers.add_parser("doctor", help="Diagnose environment, Pack and control readiness")
    doctor.add_argument("config", type=Path)
    doctor.add_argument("--as-of", help="Tax-rule lifecycle clock in YYYY-MM-DD")
    doctor.add_argument(
        "--tax-applicability-review", type=Path, action="append", default=[],
        help="One sealed entity applicability review; repeat once per legal entity",
    )
    doctor.add_argument("--tax-applicability-review-dir", type=Path)
    doctor.add_argument("--tax-applicability-registry-receipt", type=Path)
    doctor.add_argument("--pilot-readiness-review", type=Path)
    doctor.add_argument("--pilot-data-handoff-review", type=Path)
    doctor.add_argument("--pilot-shadow-run-registration", type=Path)
    doctor.add_argument("--pilot-shadow-observation-review", type=Path)
    doctor.add_argument(
        "--pilot-shadow-entity-report", type=Path, action="append", default=[],
    )
    doctor.add_argument("--pilot-shadow-portfolio-review", type=Path)
    doctor.add_argument("--pilot-shadow-series-review", type=Path)
    doctor.add_argument("--pilot-shadow-series-evidence-root", type=Path)
    doctor.add_argument("--pipeline-runs-root", type=Path)

    tax_rule_status = subparsers.add_parser(
        "tax-rule-status",
        help="Evaluate jurisdiction Pack source-review freshness without calculating tax",
    )
    tax_rule_status.add_argument("config", type=Path)
    tax_rule_status.add_argument("--as-of", help="Lifecycle clock in YYYY-MM-DD")

    tax_applicability_init = subparsers.add_parser(
        "tax-applicability-init",
        help="Create one private entity-scoped tax applicability workpaper",
    )
    tax_applicability_init.add_argument("config", type=Path)
    tax_applicability_init.add_argument("--entity", required=True)
    tax_applicability_init.add_argument("--prepared-by", required=True)
    tax_applicability_init.add_argument(
        "--facts-as-of", required=True,
        help="Date through which the reviewed entity facts are current (YYYY-MM-DD)",
    )
    tax_applicability_init.add_argument("--output", type=Path, required=True)

    tax_applicability_review = subparsers.add_parser(
        "tax-applicability-review",
        help="Independently seal one answered entity tax applicability workpaper",
    )
    tax_applicability_review.add_argument("config", type=Path)
    tax_applicability_review.add_argument("workpaper", type=Path)
    tax_applicability_review.add_argument(
        "--decision", choices=[
            "approved-in-scope", "confirmed-out-of-scope", "needs-correction",
        ], required=True,
    )
    tax_applicability_review.add_argument("--actor", required=True)
    tax_applicability_review.add_argument("--rationale", required=True)
    tax_applicability_review.add_argument(
        "--evidence-reference", action="append", required=True,
    )
    tax_applicability_review.add_argument("--output", type=Path, required=True)

    tax_applicability_verify = subparsers.add_parser(
        "tax-applicability-verify",
        help="Verify a sealed entity applicability review without returning answers",
    )
    tax_applicability_verify.add_argument("config", type=Path)
    tax_applicability_verify.add_argument("review", type=Path)
    tax_applicability_verify.add_argument(
        "--as-of", help="Applicability-review lifecycle clock in YYYY-MM-DD",
    )

    tax_applicability_portfolio_verify = subparsers.add_parser(
        "tax-applicability-portfolio-verify",
        help="Require one current approved tax applicability review per legal entity",
    )
    tax_applicability_portfolio_verify.add_argument("config", type=Path)
    tax_applicability_portfolio_verify.add_argument(
        "review", type=Path, nargs="+",
    )
    tax_applicability_portfolio_verify.add_argument(
        "--as-of", help="Applicability-review lifecycle clock in YYYY-MM-DD",
    )

    tax_applicability_status = subparsers.add_parser(
        "tax-applicability-status",
        help="Inspect exact entity-named private reviews and produce a safe rotation status",
    )
    tax_applicability_status.add_argument("config", type=Path)
    tax_applicability_status.add_argument("--review-dir", type=Path, required=True)
    tax_applicability_status.add_argument(
        "--as-of", help="Applicability-review lifecycle clock in YYYY-MM-DD",
    )

    tax_applicability_import = subparsers.add_parser(
        "tax-applicability-import",
        help="Validate and exclusively install one review under its exact entity name",
    )
    tax_applicability_import.add_argument("config", type=Path)
    tax_applicability_import.add_argument("review", type=Path)
    tax_applicability_import.add_argument("--review-dir", type=Path, required=True)
    tax_applicability_import.add_argument(
        "--as-of", help="Applicability-review lifecycle clock in YYYY-MM-DD",
    )

    tax_registry_seal = subparsers.add_parser(
        "tax-applicability-registry-seal",
        help="Create a private Box-bound receipt for one complete review registry",
    )
    tax_registry_seal.add_argument("config", type=Path)
    tax_registry_seal.add_argument("--review-dir", type=Path, required=True)
    tax_registry_seal.add_argument("--actor", required=True)
    tax_registry_seal.add_argument("--output", type=Path, required=True)
    tax_registry_seal.add_argument(
        "--as-of", help="Applicability-review lifecycle clock in YYYY-MM-DD",
    )

    tax_registry_verify = subparsers.add_parser(
        "tax-applicability-registry-verify",
        help="Verify that a review registry still matches its private receipt",
    )
    tax_registry_verify.add_argument("config", type=Path)
    tax_registry_verify.add_argument("receipt", type=Path)
    tax_registry_verify.add_argument("--review-dir", type=Path, required=True)
    tax_registry_verify.add_argument(
        "--as-of", help="Applicability-review lifecycle clock in YYYY-MM-DD",
    )

    tax_registry_alerts = subparsers.add_parser(
        "tax-applicability-alerts",
        help="Build safe review-rotation alert candidates without sending notifications",
    )
    tax_registry_alerts.add_argument("config", type=Path)
    tax_registry_alerts.add_argument("--review-dir", type=Path, required=True)
    tax_registry_alerts.add_argument("--receipt", type=Path)
    tax_registry_alerts.add_argument(
        "--as-of", help="Applicability-review lifecycle clock in YYYY-MM-DD",
    )

    pilot_init = subparsers.add_parser(
        "pilot-readiness-init",
        help="Create a private Box-bound first-company onboarding workpaper",
    )
    pilot_init.add_argument("config", type=Path)
    pilot_init.add_argument("--period", required=True, help="First Shadow Close period in YYYY-MM")
    pilot_init.add_argument("--prepared-by", required=True)
    pilot_init.add_argument("--output", type=Path, required=True)

    pilot_review = subparsers.add_parser(
        "pilot-readiness-review",
        help="Independently approve a complete one-period bounded Shadow Close plan",
    )
    pilot_review.add_argument("config", type=Path)
    pilot_review.add_argument("workpaper", type=Path)
    pilot_review.add_argument("--actor", required=True)
    pilot_review.add_argument("--rationale", required=True)
    pilot_review.add_argument("--evidence-reference", action="append", required=True)
    pilot_review.add_argument("--output", type=Path, required=True)

    pilot_verify = subparsers.add_parser(
        "pilot-readiness-verify",
        help="Verify bounded Shadow readiness without returning actors, evidence or source identifiers",
    )
    pilot_verify.add_argument("config", type=Path)
    pilot_verify.add_argument("review", type=Path)
    pilot_verify.add_argument("--tax-review-dir", type=Path)
    pilot_verify.add_argument("--tax-registry-receipt", type=Path)
    pilot_verify.add_argument("--as-of", help="Tax applicability lifecycle clock in YYYY-MM-DD")

    pilot_alerts = subparsers.add_parser(
        "pilot-readiness-alerts",
        help="Build safe Pilot review lifecycle alerts without sending notifications",
    )
    pilot_alerts.add_argument("config", type=Path)
    pilot_alerts.add_argument("--review", type=Path)
    pilot_alerts.add_argument("--as-of", help="Pilot review lifecycle clock in YYYY-MM-DD")

    handoff_init = subparsers.add_parser(
        "pilot-data-handoff-init",
        help="Create a private manifest for controlled first-company source-data intake",
    )
    handoff_init.add_argument("config", type=Path)
    handoff_init.add_argument("pilot_readiness_review", type=Path)
    handoff_init.add_argument("--prepared-by", required=True)
    handoff_init.add_argument("--custodian-principal", required=True)
    handoff_init.add_argument("--as-of", help="Pilot review lifecycle clock in YYYY-MM-DD")
    handoff_init.add_argument("--output", type=Path, required=True)

    handoff_review = subparsers.add_parser(
        "pilot-data-handoff-review",
        help="Independently approve a completed source manifest for controlled intake",
    )
    handoff_review.add_argument("config", type=Path)
    handoff_review.add_argument("workpaper", type=Path)
    handoff_review.add_argument("pilot_readiness_review", type=Path)
    handoff_review.add_argument("--actor", required=True)
    handoff_review.add_argument("--rationale", required=True)
    handoff_review.add_argument("--evidence-reference", action="append", required=True)
    handoff_review.add_argument("--as-of", help="Pilot review lifecycle clock in YYYY-MM-DD")
    handoff_review.add_argument("--output", type=Path, required=True)

    handoff_verify = subparsers.add_parser(
        "pilot-data-handoff-verify",
        help="Verify controlled intake readiness without returning source paths or values",
    )
    handoff_verify.add_argument("config", type=Path)
    handoff_verify.add_argument("review", type=Path)
    handoff_verify.add_argument("pilot_readiness_review", type=Path)
    handoff_verify.add_argument("--as-of", help="Pilot review lifecycle clock in YYYY-MM-DD")

    shadow_register = subparsers.add_parser(
        "pilot-shadow-run-register",
        help="Bind one fully reviewed month-close Shadow Run per entity to the pilot handoff",
    )
    shadow_register.add_argument("config", type=Path)
    shadow_register.add_argument("handoff_review", type=Path)
    shadow_register.add_argument("pilot_readiness_review", type=Path)
    shadow_register.add_argument(
        "--entity-attempt", action="append", required=True,
        help="Exact entity_id=attempt_id binding; repeat once per Box legal entity",
    )
    shadow_register.add_argument("--actor", required=True)
    shadow_register.add_argument("--rationale", required=True)
    shadow_register.add_argument("--evidence-reference", action="append", required=True)
    shadow_register.add_argument("--as-of", help="Pilot review lifecycle clock in YYYY-MM-DD")
    shadow_register.add_argument("--output", type=Path, required=True)
    shadow_register.add_argument(
        "--runs-root", type=Path,
        default=Path.cwd() / ".opc-finance-data" / "pipeline-runs",
    )

    shadow_verify = subparsers.add_parser(
        "pilot-shadow-run-verify",
        help="Re-verify first Shadow Run registration against current reviews and ledger",
    )
    shadow_verify.add_argument("config", type=Path)
    shadow_verify.add_argument("registration", type=Path)
    shadow_verify.add_argument("handoff_review", type=Path)
    shadow_verify.add_argument("pilot_readiness_review", type=Path)
    shadow_verify.add_argument("--as-of", help="Pilot review lifecycle clock in YYYY-MM-DD")
    shadow_verify.add_argument(
        "--runs-root", type=Path,
        default=Path.cwd() / ".opc-finance-data" / "pipeline-runs",
    )

    observation_assemble = subparsers.add_parser(
        "pilot-shadow-observation-assemble",
        help="Bind the current pilot registration to reviewed entity and portfolio Shadow evidence",
    )
    observation_assemble.add_argument("config", type=Path)
    observation_assemble.add_argument("registration", type=Path)
    observation_assemble.add_argument("handoff_review", type=Path)
    observation_assemble.add_argument("pilot_readiness_review", type=Path)
    observation_assemble.add_argument(
        "--entity-report", type=Path, action="append", required=True,
        help="One current reviewed entity Shadow Close report; repeat for every legal entity",
    )
    observation_assemble.add_argument("--portfolio-review", type=Path)
    observation_assemble.add_argument("--as-of", help="Pilot review lifecycle clock in YYYY-MM-DD")
    observation_assemble.add_argument("--output", type=Path, required=True)
    observation_assemble.add_argument(
        "--runs-root", type=Path,
        default=Path.cwd() / ".opc-finance-data" / "pipeline-runs",
    )

    observation_review = subparsers.add_parser(
        "pilot-shadow-observation-review",
        help="Independently review one exact no-values first-pilot observation receipt",
    )
    observation_review.add_argument("config", type=Path)
    observation_review.add_argument("receipt", type=Path)
    observation_review.add_argument(
        "--decision",
        choices=["passed", "accepted-differences", "needs-correction"],
        required=True,
    )
    observation_review.add_argument("--actor", required=True)
    observation_review.add_argument("--rationale", required=True)
    observation_review.add_argument(
        "--evidence-reference", action="append", required=True,
    )
    observation_review.add_argument("--output", type=Path, required=True)

    observation_verify = subparsers.add_parser(
        "pilot-shadow-observation-verify",
        help="Re-verify a reviewed first-pilot observation against all current source evidence",
    )
    observation_verify.add_argument("config", type=Path)
    observation_verify.add_argument("reviewed_receipt", type=Path)
    observation_verify.add_argument("registration", type=Path)
    observation_verify.add_argument("handoff_review", type=Path)
    observation_verify.add_argument("pilot_readiness_review", type=Path)
    observation_verify.add_argument(
        "--entity-report", type=Path, action="append", required=True,
    )
    observation_verify.add_argument("--portfolio-review", type=Path)
    observation_verify.add_argument("--as-of", help="Pilot review lifecycle clock in YYYY-MM-DD")
    observation_verify.add_argument(
        "--runs-root", type=Path,
        default=Path.cwd() / ".opc-finance-data" / "pipeline-runs",
    )

    period_archive = subparsers.add_parser(
        "pilot-shadow-period-archive",
        help="Verify and archive one exact Pilot Shadow period for a future series",
    )
    period_archive.add_argument("config", type=Path)
    period_archive.add_argument("reviewed_receipt", type=Path)
    period_archive.add_argument("registration", type=Path)
    period_archive.add_argument("handoff_review", type=Path)
    period_archive.add_argument("pilot_readiness_review", type=Path)
    period_archive.add_argument(
        "--entity-report", type=Path, action="append", required=True,
    )
    period_archive.add_argument("--portfolio-review", type=Path)
    period_archive.add_argument("--evidence-root", type=Path, required=True)
    period_archive.add_argument("--as-of", help="Pilot review lifecycle clock in YYYY-MM-DD")
    period_archive.add_argument(
        "--runs-root", type=Path,
        default=Path.cwd() / ".opc-finance-data" / "pipeline-runs",
    )

    series_assemble = subparsers.add_parser(
        "pilot-shadow-series-assemble",
        help="Re-verify and bind 2-24 consecutive reviewed Pilot Shadow periods",
    )
    series_assemble.add_argument("config", type=Path)
    series_assemble.add_argument("evidence_root", type=Path)
    series_assemble.add_argument("--as-of", help="Pilot review lifecycle clock in YYYY-MM-DD")
    series_assemble.add_argument("--output", type=Path, required=True)
    series_assemble.add_argument(
        "--runs-root", type=Path,
        default=Path.cwd() / ".opc-finance-data" / "pipeline-runs",
    )

    series_review = subparsers.add_parser(
        "pilot-shadow-series-review",
        help="Independently review one exact consecutive Pilot Shadow series receipt",
    )
    series_review.add_argument("config", type=Path)
    series_review.add_argument("receipt", type=Path)
    series_review.add_argument(
        "--decision",
        choices=["approved-for-promotion-evidence", "needs-correction"],
        required=True,
    )
    series_review.add_argument("--actor", required=True)
    series_review.add_argument("--rationale", required=True)
    series_review.add_argument(
        "--evidence-reference", action="append", required=True,
    )
    series_review.add_argument("--output", type=Path, required=True)

    series_verify = subparsers.add_parser(
        "pilot-shadow-series-verify",
        help="Re-verify a reviewed consecutive series against every current period source",
    )
    series_verify.add_argument("config", type=Path)
    series_verify.add_argument("reviewed_receipt", type=Path)
    series_verify.add_argument("evidence_root", type=Path)
    series_verify.add_argument("--as-of", help="Pilot review lifecycle clock in YYYY-MM-DD")
    series_verify.add_argument(
        "--runs-root", type=Path,
        default=Path.cwd() / ".opc-finance-data" / "pipeline-runs",
    )

    dispatch = subparsers.add_parser("dispatch", help="Dispatch one validated service request JSON")
    dispatch.add_argument("config", type=Path)
    dispatch.add_argument("request", type=Path)

    cfo_metrics = subparsers.add_parser(
        "cfo-metrics-evaluate",
        help="Deterministically evaluate Pack-selected CFO metrics for one legal entity",
    )
    cfo_metrics.add_argument("config", type=Path)
    cfo_metrics.add_argument("request", type=Path)
    cfo_metrics.add_argument("--entity", required=True)

    tax = subparsers.add_parser("tax-calendar", help="Build evidence-backed tax deadline candidates")
    tax.add_argument("config", type=Path)
    tax.add_argument("--entity", required=True)
    tax.add_argument("--period-year", type=int)
    tax.add_argument("--as-of")
    tax.add_argument("--financial-year-end")
    tax.add_argument("--gst-period-end", action="append", default=[])
    return parser


def _run(args: argparse.Namespace) -> Any:
    if args.command == "options":
        return list_box_options(load_pack_catalog(args.packs))
    if args.command == "box-starters":
        return build_box_starter_catalog(load_pack_catalog(args.packs))
    if args.command == "starter-init":
        return initialize_box_starter_workspace(
            profile=args.profile,
            country=args.country,
            packs_root=args.packs,
            destination_root=args.root,
            actor=args.actor,
            integrations=args.integration,
            name=args.name,
            entity_id=args.entity_id,
            entity_name=args.entity_name,
            data_mode=args.data_mode,
        )
    if args.command == "starter-compose":
        return initialize_multi_entity_starter_workspace(
            profile=args.profile,
            entities=args.entity,
            packs_root=args.packs,
            destination_root=args.root,
            actor=args.actor,
            integrations=args.integration,
            entity_integrations=args.entity_integration,
            entity_names=args.entity_name,
            reporting_currency=args.reporting_currency,
            name=args.name,
            data_mode=args.data_mode,
        )
    if args.command == "trial-init":
        return initialize_trial_workspace(
            profile=args.profile,
            country=args.country,
            packs_root=args.packs,
            destination_root=args.root,
            actor=args.actor,
            integrations=args.integration,
            name=args.name,
            entity_id=args.entity_id,
            entity_name=args.entity_name,
        )
    if args.command == "trial-verify":
        return verify_trial_workspace(args.root, args.packs)
    if args.command == "trial-onboarding":
        return build_trial_onboarding_plan(args.root, args.packs)
    if args.command == "trial-run":
        return run_trial_workbench(
            args.root,
            args.packs,
            host=args.host,
            port=args.port,
            auth_file=args.auth_file,
        )
    if args.command == "handoff-bundle":
        return write_box_candidate_bundle(
            _json_file(args.spec), args.packs, args.output,
        )
    if args.command == "handoff-verify":
        return verify_box_candidate_bundle(args.bundle, args.packs)
    if args.command == "handoff-receipt-verify":
        return verify_browser_handoff_receipt(args.bundle, args.receipt, args.packs)
    if args.command == "handoff-unpack":
        return unpack_box_candidate_bundle(
            args.bundle, args.packs, args.root, actor=args.actor,
        )
    if args.command == "handoff-unpack-verify":
        return verify_unpacked_box_candidate(args.root, args.packs)
    if args.command == "source-kit-bundle":
        return write_source_kit_bundle(args.output)
    if args.command == "source-kit-verify":
        return verify_source_kit_bundle(args.bundle)
    if args.command == "source-kit-unpack":
        return unpack_source_kit_bundle(
            args.bundle, args.root, actor=args.actor,
        )
    if args.command == "source-kit-unpack-verify":
        return verify_unpacked_source_kit(args.root)
    if args.command == "pack-audit":
        return audit_pack_catalog(load_pack_catalog(args.packs))
    if args.command == "release-candidate-audit":
        return audit_release_candidate(
            args.packs,
            project_root=args.project_root,
            wheel=args.wheel,
            source_kit=args.source_kit,
        )
    if args.command == "distribution-verify":
        return verify_wheel(args.wheel)
    if args.command == "deployment-assets-verify":
        return verify_deployment_assets(args.root)
    if args.command == "runtime-data-inspect":
        return inspect_runtime_data(args.root)
    if args.command == "runtime-data-init":
        return initialize_runtime_data(
            args.root, actor=args.actor, adopt_existing=args.adopt_existing,
        )
    if args.command == "runtime-data-upgrade-preflight":
        return runtime_upgrade_preflight(args.root)
    if args.command == "runtime-data-backup":
        return backup_runtime_data(
            args.root,
            args.destination,
            actor=args.actor,
            service_stopped_confirmed=args.service_stopped_confirmed,
        )
    if args.command == "runtime-data-backup-verify":
        return verify_runtime_backup(args.backup)
    if args.command == "runtime-data-restore":
        return restore_runtime_backup(args.backup, args.target, actor=args.actor)
    if args.command == "runtime-data-migrate":
        return migrate_runtime_data(
            args.root,
            args.backup,
            actor=args.actor,
            service_stopped_confirmed=args.service_stopped_confirmed,
        )
    if args.command == "connector-sync-verify":
        return ConnectorSyncStore(args.sync_root).verify()
    if args.command == "airwallex-webhook-verify":
        return AirwallexWebhookStore(args.webhook_root).verify()
    if args.command == "promotion-verify":
        return ReleasePromotionStore(args.promotion_root).verify()
    if args.command == "pipeline-request-fingerprint":
        return fingerprint_pipeline_request_file(args.request)
    if args.command == "auth-token-hash":
        token = os.environ.get(args.token_env)
        if token is None:
            raise ApiAuthError(f"environment variable is not set: {args.token_env}")
        return {
            "token_sha256": hash_token(token),
            "raw_token_returned": False,
            "source_env": args.token_env,
        }
    if args.command == "auth-policy-validate":
        policy = load_api_auth_policy(legacy_token="", policy_path=args.policy)
        return {"valid": True, "policy": policy.public_dict()}
    if args.command == "pipeline-runs-backup":
        return {"backup": PipelineRunStore(args.runs_root).backup(
            args.destination, actor=args.actor,
        )}
    if args.command == "pipeline-backup-verify":
        return {"backup": PipelineRunStore.verify_backup(args.backup)}
    if args.command == "pipeline-runs-restore":
        return {"restore": PipelineRunStore(args.runs_root).restore_from_backup(
            args.backup, actor=args.actor,
        )}
    if args.command == "eval":
        return run_box_eval_suite(args.suite, args.packs, project_root=args.project_root)
    if args.command == "create":
        config = create_box_config(_json_file(args.spec), load_pack_catalog(args.packs))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return {"output": str(args.output), "config": config}
    if args.command == "jurisdiction-init":
        return scaffold_jurisdiction_pack(
            args.output_root,
            slug=args.slug,
            country_code=args.country_code,
            display_name=args.display_name,
            source_authority=args.source_authority,
            source_title=args.source_title,
            source_url=args.source_url,
            verified_at=args.verified_at,
            rules_effective_at=args.rules_effective_at,
        )
    if args.command == "connector-init":
        return scaffold_connector_pack(
            args.output_root, slug=args.slug, display_name=args.display_name,
            secret_env=args.secret_env, base_url=args.base_url,
        )
    if args.command == "validate":
        return resolve_box_file(args.config, args.packs)
    if args.command == "compile":
        compiled = compile_box_file(args.config, args.packs)
        paths = write_compiled_box(compiled, args.output)
        return {"output_files": [str(path) for path in paths], "deployment": compiled["deployment"]}
    if args.command == "upgrade-check":
        return compare_compiled_box(
            _json_file(args.baseline),
            compile_box_file(args.config, args.packs),
        )
    runtime = _runtime(args.config, args.packs)
    registry = build_default_service_registry()
    if args.command == "connector-preflight":
        return build_connector_onboarding(runtime)
    if args.command == "connector-access-request-init":
        return initialize_connector_access_request(
            runtime,
            pack_id=args.pack,
            entity_id=args.entity,
            output=args.output,
        )
    if args.command == "connector-access-request-verify":
        return verify_private_connector_access_request(runtime, args.request)
    if args.command == "connector-access-probe":
        if args.allow_network and args.output is None:
            raise ConnectorAccessProbeError(
                "Authorized Connector access probes require --output so the attempt is auditable"
            )
        if args.output is not None:
            return write_connector_access_probe_receipt(
                runtime,
                args.request,
                args.output,
                allow_network=args.allow_network,
            )
        return run_connector_access_probe(
            runtime, args.request, allow_network=args.allow_network,
        )
    if args.command == "connector-access-receipt-verify":
        return verify_private_connector_access_probe_receipt(
            runtime,
            args.request,
            args.receipt,
            as_of=args.as_of,
            maximum_age_days=args.maximum_age_days,
        )
    if args.command == "connector-access-receipt-renew":
        return renew_connector_access_probe_receipt(
            runtime,
            args.request,
            args.receipt,
            allow_network=args.allow_network,
        )
    if args.command == "production-readiness":
        return build_production_readiness_workspace(
            runtime, registry, runs_root=args.runs_root, as_of=args.as_of,
        )
    if args.command == "activation-status":
        return build_activation_workspace(
            runtime, registry, runs_root=args.runs_root, as_of=args.as_of,
        )
    if args.command == "activation-init":
        return initialize_activation_workspace(
            runtime,
            args.config,
            args.root,
            period=args.period,
            facts_as_of=args.facts_as_of,
            prepared_by=args.prepared_by,
        )
    if args.command == "activation-workspace-verify":
        return verify_activation_workspace(runtime, args.root)
    if args.command == "activation-workspace-status":
        return build_initialized_activation_status(
            runtime, registry, args.root, as_of=args.as_of,
        )
    if args.command == "connector-access-alerts":
        status = build_initialized_activation_status(
            runtime, registry, args.activation_root, as_of=args.as_of,
        )
        return status["connector_access_alerts"]
    if args.command == "pilot-shadow-next-period-init":
        return initialize_next_pilot_shadow_period(
            runtime,
            args.config,
            args.activation_root,
            prepared_by=args.prepared_by,
            facts_as_of=args.facts_as_of,
        )
    if args.command == "pilot-shadow-next-period-verify":
        return verify_next_pilot_shadow_period(
            runtime,
            args.activation_root,
            args.period,
            as_of=args.as_of,
        )
    if args.command == "pilot-shadow-period-runbook-record":
        return PilotShadowPeriodRunbookStore(
            args.activation_root, args.period,
        ).record(
            runtime,
            step_id=args.step_id,
            outcome=args.outcome.replace("-", "_"),
            actor=args.actor,
            rationale=args.rationale,
            evidence_references=args.evidence_reference,
            observed_exit_code=args.observed_exit_code,
        )
    if args.command == "pilot-shadow-period-runbook-status":
        return PilotShadowPeriodRunbookStore(
            args.activation_root, args.period,
        ).status(runtime)
    if args.command == "pilot-shadow-period-runbook-verify":
        return PilotShadowPeriodRunbookStore(
            args.activation_root, args.period,
        ).verify(runtime)
    if args.command == "activation-runbook-record":
        return ActivationRunbookStore(args.root).record(
            runtime,
            step_id=args.step_id,
            outcome=args.outcome.replace("-", "_"),
            actor=args.actor,
            rationale=args.rationale,
            evidence_references=args.evidence_reference,
            observed_exit_code=args.observed_exit_code,
        )
    if args.command == "activation-runbook-status":
        return ActivationRunbookStore(args.root).status(runtime)
    if args.command == "activation-runbook-verify":
        return ActivationRunbookStore(args.root).verify(runtime)
    if args.command == "connector-shadow-status":
        return build_connector_shadow_registry_workspace(
            runtime,
            args.review_dir,
            as_of=args.as_of,
            maximum_age_days=args.maximum_age_days,
        )
    if args.command == "tax-applicability-init":
        return write_tax_applicability_workpaper(
            runtime, args.entity, prepared_by=args.prepared_by,
            facts_as_of=args.facts_as_of, output=args.output,
        )
    if args.command == "tax-applicability-review":
        return review_tax_applicability_workpaper(
            runtime,
            args.workpaper,
            args.output,
            decision=args.decision,
            actor=args.actor,
            rationale=args.rationale,
            evidence_references=args.evidence_reference,
        )
    if args.command == "tax-applicability-verify":
        return verify_tax_applicability_review(runtime, args.review, as_of=args.as_of)
    if args.command == "tax-applicability-portfolio-verify":
        return verify_tax_applicability_review_portfolio(
            runtime, args.review, as_of=args.as_of,
        )
    if args.command == "tax-applicability-status":
        return inspect_tax_applicability_review_directory(
            runtime, args.review_dir, as_of=args.as_of,
        )
    if args.command == "tax-applicability-import":
        return import_tax_applicability_review(
            runtime, args.review, args.review_dir, as_of=args.as_of,
        )
    if args.command == "tax-applicability-registry-seal":
        return write_tax_applicability_registry_receipt(
            runtime, args.review_dir, args.output,
            actor=args.actor, as_of=args.as_of,
        )
    if args.command == "tax-applicability-registry-verify":
        return verify_tax_applicability_registry_receipt(
            runtime, args.review_dir, args.receipt, as_of=args.as_of,
        )
    if args.command == "tax-applicability-alerts":
        return build_tax_applicability_registry_alerts(
            runtime, args.review_dir,
            receipt_json=args.receipt, as_of=args.as_of,
        )
    if args.command == "pilot-readiness-init":
        return write_pilot_readiness_workpaper(
            runtime, args.output, period=args.period, prepared_by=args.prepared_by,
        )
    if args.command == "pilot-readiness-review":
        return review_pilot_readiness_workpaper(
            runtime, args.workpaper, args.output,
            actor=args.actor, rationale=args.rationale,
            evidence_references=args.evidence_reference,
        )
    if args.command == "pilot-readiness-verify":
        return verify_pilot_readiness_review(
            runtime, args.review,
            tax_review_dir=args.tax_review_dir,
            tax_registry_receipt=args.tax_registry_receipt,
            as_of=args.as_of,
        )
    if args.command == "pilot-readiness-alerts":
        return build_pilot_readiness_alerts(
            runtime, args.review, as_of=args.as_of,
        )
    if args.command == "pilot-data-handoff-init":
        return write_pilot_data_handoff_workpaper(
            runtime, args.pilot_readiness_review, args.output,
            prepared_by=args.prepared_by,
            custodian_principal=args.custodian_principal,
            as_of=args.as_of,
        )
    if args.command == "pilot-data-handoff-review":
        return review_pilot_data_handoff_workpaper(
            runtime, args.workpaper, args.pilot_readiness_review, args.output,
            actor=args.actor, rationale=args.rationale,
            evidence_references=args.evidence_reference, as_of=args.as_of,
        )
    if args.command == "pilot-data-handoff-verify":
        return verify_pilot_data_handoff_review(
            runtime, args.review, args.pilot_readiness_review, as_of=args.as_of,
        )
    if args.command == "pilot-shadow-run-register":
        return register_pilot_shadow_run(
            runtime, args.handoff_review, args.pilot_readiness_review,
            args.runs_root, _entity_attempts(args.entity_attempt), args.output,
            actor=args.actor, rationale=args.rationale,
            evidence_references=args.evidence_reference, as_of=args.as_of,
        )
    if args.command == "pilot-shadow-run-verify":
        return verify_pilot_shadow_run_registration(
            runtime, args.registration, args.handoff_review,
            args.pilot_readiness_review, args.runs_root, as_of=args.as_of,
        )
    if args.command == "pilot-shadow-observation-assemble":
        return assemble_pilot_shadow_observation(
            runtime, args.registration, args.handoff_review,
            args.pilot_readiness_review, args.runs_root, args.entity_report,
            args.output, portfolio_review_path=args.portfolio_review,
            as_of=args.as_of,
        )
    if args.command == "pilot-shadow-observation-review":
        return review_pilot_shadow_observation(
            runtime, args.receipt, args.output, decision=args.decision,
            actor=args.actor, rationale=args.rationale,
            evidence_references=args.evidence_reference,
        )
    if args.command == "pilot-shadow-observation-verify":
        return verify_pilot_shadow_observation(
            runtime, args.reviewed_receipt, args.registration,
            args.handoff_review, args.pilot_readiness_review, args.runs_root,
            args.entity_report, portfolio_review_path=args.portfolio_review,
            as_of=args.as_of,
        )
    if args.command == "pilot-shadow-period-archive":
        return archive_pilot_shadow_period(
            runtime,
            args.reviewed_receipt,
            args.registration,
            args.handoff_review,
            args.pilot_readiness_review,
            args.runs_root,
            args.entity_report,
            args.evidence_root,
            portfolio_review_path=args.portfolio_review,
            as_of=args.as_of,
        )
    if args.command == "pilot-shadow-series-assemble":
        return assemble_pilot_shadow_series(
            runtime, args.evidence_root, args.runs_root, args.output,
            as_of=args.as_of,
        )
    if args.command == "pilot-shadow-series-review":
        return review_pilot_shadow_series(
            runtime, args.receipt, args.output, decision=args.decision,
            actor=args.actor, rationale=args.rationale,
            evidence_references=args.evidence_reference,
        )
    if args.command == "pilot-shadow-series-verify":
        return verify_pilot_shadow_series(
            runtime, args.reviewed_receipt, args.evidence_root, args.runs_root,
            as_of=args.as_of,
        )
    if args.command == "shadow-close-template":
        return write_shadow_close_template(runtime, args.output)
    if args.command == "shadow-close-compare":
        return compare_shadow_close_artifacts(
            runtime, args.baseline, args.finance, args.output,
        )
    if args.command == "shadow-close-review":
        return review_shadow_close_artifact(
            runtime,
            args.report,
            args.output,
            decision=args.decision,
            actor=args.actor,
            rationale=args.rationale,
            evidence_references=args.evidence_reference,
            resolutions_json=args.resolutions,
        )
    if args.command == "shadow-close-verify":
        return verify_shadow_close_artifact(runtime, args.report)
    if args.command == "connector-shadow-assess":
        return assess_connector_shadow_artifacts(
            runtime, args.baseline, args.pipeline_result, args.output,
        )
    if args.command == "connector-shadow-baseline-init":
        return build_connector_shadow_baseline_workpaper(
            runtime,
            pipeline_id=args.pipeline,
            entity_id=args.entity,
            sample_period=args.period,
            prepared_by=args.prepared_by,
            output=args.output,
        )
    if args.command == "connector-shadow-baseline-finalize":
        return finalize_connector_shadow_baseline_workpaper(
            runtime, args.workpaper, args.output,
        )
    if args.command == "shopify-monthly-shadow-request-init":
        return build_shopify_monthly_shadow_request_template(
            runtime,
            entity_id=args.entity,
            period=args.period,
            output=args.output,
        )
    if args.command == "shopify-monthly-shadow-request-verify":
        return verify_private_shopify_monthly_shadow_request(
            runtime, args.request,
        )
    if args.command == "stripe-shadow-request-init":
        return build_stripe_shadow_request_template(
            runtime,
            entity_id=args.entity,
            period=args.period,
            output=args.output,
        )
    if args.command == "stripe-shadow-request-verify":
        return verify_private_stripe_shadow_request(runtime, args.request)
    if args.command == "stripe-shadow-observe":
        request = read_private_stripe_shadow_request(args.request)
        validated = validate_stripe_shadow_request(runtime, request)
        binding = _verified_connector_access_binding(
            runtime,
            access_request=args.access_request,
            access_receipt=args.access_receipt,
            expected_pack_id="connector.stripe",
            expected_entity_id=validated["entity_id"],
        )
        if binding["mode"] == "connected_account":
            request["payload"]["balance_request"]["stripe_account"] = binding["account_id"]
            request["payload"]["payout_request"]["stripe_account"] = binding["account_id"]
        result = dispatch_box_pipeline_request(runtime, request)
        return write_stripe_shadow_observation(runtime, result, args.output)
    if args.command == "xero-shadow-observe":
        request = read_private_xero_shadow_request(args.request)
        validated = validate_xero_shadow_request(runtime, request)
        _verified_connector_access_binding(
            runtime,
            access_request=args.access_request,
            access_receipt=args.access_receipt,
            expected_pack_id="connector.xero",
            expected_entity_id=validated["entity_id"],
        )
        result = dispatch_box_pipeline_request(runtime, request)
        return write_xero_shadow_observation(runtime, result, args.output)
    if args.command == "xero-shadow-request-init":
        return build_xero_shadow_request(
            runtime,
            entity_id=args.entity,
            period=args.period,
            output=args.output,
        )
    if args.command == "xero-shadow-request-verify":
        return verify_private_xero_shadow_request(runtime, args.request)
    if args.command == "wise-shadow-observe":
        request = read_private_wise_shadow_request(args.request)
        validated = validate_wise_shadow_request(runtime, request)
        _verified_connector_access_binding(
            runtime,
            access_request=args.access_request,
            access_receipt=args.access_receipt,
            expected_pack_id="connector.wise",
            expected_entity_id=validated["entity_id"],
        )
        result = dispatch_box_pipeline_request(runtime, request)
        return write_wise_shadow_observation(runtime, result, args.output)
    if args.command == "wise-shadow-request-init":
        return build_wise_shadow_request(
            runtime,
            entity_id=args.entity,
            period=args.period,
            output=args.output,
        )
    if args.command == "wise-shadow-request-verify":
        return verify_private_wise_shadow_request(runtime, args.request)
    if args.command == "paypal-shadow-observe":
        request = read_private_paypal_shadow_request(args.request)
        validated = validate_paypal_shadow_request(runtime, request)
        _verified_connector_access_binding(
            runtime,
            access_request=args.access_request,
            access_receipt=args.access_receipt,
            expected_pack_id="connector.paypal",
            expected_entity_id=validated["entity_id"],
        )
        result = dispatch_box_pipeline_request(runtime, request)
        return write_paypal_shadow_observation(runtime, result, args.output)
    if args.command == "paypal-shadow-request-init":
        return build_paypal_shadow_request(
            runtime,
            entity_id=args.entity,
            period=args.period,
            output=args.output,
        )
    if args.command == "paypal-shadow-request-verify":
        return verify_private_paypal_shadow_request(runtime, args.request)
    if args.command == "woocommerce-shadow-observe":
        request = read_private_woocommerce_shadow_request(args.request)
        validated = validate_woocommerce_shadow_request(runtime, request)
        _verified_connector_access_binding(
            runtime,
            access_request=args.access_request,
            access_receipt=args.access_receipt,
            expected_pack_id="connector.woocommerce",
            expected_entity_id=validated["entity_id"],
        )
        result = dispatch_box_pipeline_request(runtime, request)
        return write_woocommerce_shadow_observation(runtime, result, args.output)
    if args.command == "woocommerce-shadow-request-init":
        return build_woocommerce_shadow_request(
            runtime,
            entity_id=args.entity,
            period=args.period,
            output=args.output,
        )
    if args.command == "woocommerce-shadow-request-verify":
        return verify_private_woocommerce_shadow_request(runtime, args.request)
    if args.command == "shipbob-shadow-observe":
        request = read_private_shipbob_shadow_request(args.request)
        validated = validate_shipbob_shadow_request(runtime, request)
        _verified_connector_access_binding(
            runtime,
            access_request=args.access_request,
            access_receipt=args.access_receipt,
            expected_pack_id="connector.shipbob",
            expected_entity_id=validated["entity_id"],
        )
        result = dispatch_box_pipeline_request(runtime, request)
        return write_shipbob_shadow_observation(runtime, result, args.output)
    if args.command == "shipbob-shadow-request-init":
        return build_shipbob_shadow_request(
            runtime,
            entity_id=args.entity,
            period=args.period,
            output=args.output,
        )
    if args.command == "shipbob-shadow-request-verify":
        return verify_private_shipbob_shadow_request(runtime, args.request)
    if args.command == "amazon-seller-shadow-observe":
        request = read_private_amazon_seller_shadow_request(args.request)
        validated = validate_amazon_seller_shadow_request(runtime, request)
        _verified_connector_access_binding(
            runtime,
            access_request=args.access_request,
            access_receipt=args.access_receipt,
            expected_pack_id="connector.amazon_seller",
            expected_entity_id=validated["entity_id"],
        )
        result = dispatch_box_pipeline_request(runtime, request)
        return write_amazon_seller_shadow_observation(runtime, result, args.output)
    if args.command == "amazon-seller-shadow-request-init":
        return build_amazon_seller_shadow_request(
            runtime,
            entity_id=args.entity,
            period=args.period,
            marketplace_id=args.marketplace_id,
            output=args.output,
        )
    if args.command == "amazon-seller-shadow-request-verify":
        return verify_private_amazon_seller_shadow_request(runtime, args.request)
    if args.command == "shopify-monthly-shadow-observe":
        request = read_private_shopify_monthly_shadow_request(args.request)
        validated = validate_shopify_monthly_shadow_request(runtime, request)
        shop_domain = request["payload"]["shopify_monthly_request"]["shop_domain"]
        _verified_connector_access_binding(
            runtime,
            access_request=args.shopify_access_request,
            access_receipt=args.shopify_access_receipt,
            expected_pack_id="connector.shopify",
            expected_entity_id=validated["entity_id"],
            expected_provider_account=shop_domain,
        )
        stripe_binding = _verified_connector_access_binding(
            runtime,
            access_request=args.stripe_access_request,
            access_receipt=args.stripe_access_receipt,
            expected_pack_id="connector.stripe",
            expected_entity_id=validated["entity_id"],
        )
        if stripe_binding["mode"] == "connected_account":
            request["payload"]["stripe_balance_request"]["stripe_account"] = (
                stripe_binding["account_id"]
            )
        result = dispatch_box_pipeline_request(runtime, request)
        return write_shopify_stripe_monthly_shadow_observation(
            runtime, result, args.output,
        )
    if args.command == "connector-shadow-review":
        return review_connector_shadow_artifact(
            runtime, args.assessment, args.output, decision=args.decision,
            actor=args.actor, rationale=args.rationale,
            evidence_references=args.evidence_reference,
        )
    if args.command == "connector-shadow-verify":
        return verify_connector_shadow_artifact(runtime, args.assessment)
    if args.command == "shadow-close-portfolio-assemble":
        return assemble_multi_entity_shadow_close_artifact(
            runtime,
            args.entity_report,
            args.portfolio_result,
            args.output,
        )
    if args.command == "shadow-close-portfolio-review":
        return review_multi_entity_shadow_close_artifact(
            runtime,
            args.manifest,
            args.output,
            decision=args.decision,
            actor=args.actor,
            rationale=args.rationale,
            evidence_references=args.evidence_reference,
        )
    if args.command == "shadow-close-portfolio-verify":
        return verify_multi_entity_shadow_close_artifact(runtime, args.manifest)
    if args.command in {
        "promotion-template", "promotion-assess", "promotion-record", "promotion-review",
        "promotion-status",
    }:
        if args.command == "promotion-template":
            if args.output.exists():
                raise ReleasePromotionError(
                    "promotion evidence template output already exists; refusing to overwrite"
                )
            template = build_stable_promotion_evidence_template(runtime, args.pack_id)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(
                json.dumps(template, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            return {
                "output": str(args.output),
                "pack_id": template["pack_id"],
                "pack_version": template["pack_version"],
                "runtime_fingerprint": template["runtime_fingerprint"],
                "template_only": True,
                "assessment_ready": False,
            }
        if args.command in {"promotion-assess", "promotion-record"}:
            assessment = build_stable_promotion_assessment(
                runtime, _json_file(args.evidence),
            )
            if args.command == "promotion-assess":
                return assessment
            return ReleasePromotionStore(args.promotion_root).record_assessment(
                assessment, actor=args.actor,
            )
        store = ReleasePromotionStore(args.promotion_root)
        fingerprint = runtime.snapshot()["fingerprint"]
        if args.command == "promotion-status":
            return store.status(runtime_fingerprint=fingerprint, limit=args.limit)
        return store.review(
            args.assessment_id,
            runtime_fingerprint=fingerprint,
            actor=args.actor,
            decision=args.decision,
            rationale=args.rationale,
            evidence_references=args.evidence_reference,
        )
    if args.command in {
        "connector-sync-plan", "connector-sync-run", "connector-sync-commit",
        "connector-sync-status", "connector-sync-quarantine-resolve",
    }:
        connector_registry = build_box_connector_registry(runtime)
        sync_store = ConnectorSyncStore(args.sync_root)
        if args.command == "connector-sync-plan":
            return build_sync_plan(
                runtime,
                connector_registry.definition(args.connector_id),
                sync_store,
                entity_id=args.entity,
                stream_id=args.stream,
                sync_mode=args.mode,
                window_start=args.window_start,
                window_end=args.window_end,
                request_base=_json_file(args.request_base) if args.request_base else None,
            )
        if args.command == "connector-sync-run":
            return execute_sync_plan(
                runtime, connector_registry, sync_store, _json_file(args.plan), actor=args.actor,
            )
        fingerprint = runtime.snapshot()["fingerprint"]
        if args.command == "connector-sync-commit":
            return sync_store.commit_checkpoint(
                args.attempt_id,
                runtime_fingerprint=fingerprint,
                actor=args.actor,
                rationale=args.rationale,
                evidence_references=args.evidence_reference,
            )
        if args.command == "connector-sync-status":
            return sync_store.status(runtime_fingerprint=fingerprint, limit=args.limit)
        return sync_store.resolve_quarantine(
            args.attempt_id,
            runtime_fingerprint=fingerprint,
            actor=args.actor,
            resolution=args.resolution,
            rationale=args.rationale,
            replacement_attempt_id=args.replacement_attempt_id,
        )
    if args.command == "airwallex-webhook-status":
        return AirwallexWebhookStore(args.webhook_root).status(
            runtime_fingerprint=runtime.snapshot()["fingerprint"], limit=args.limit,
        )
    if args.command == "airwallex-webhook-process":
        if not isinstance(args.limit, int) or isinstance(args.limit, bool) or not 1 <= args.limit <= 100:
            raise AirwallexWebhookError(
                "webhook process limit must be 1-100",
                error_type="invalid_webhook_process_limit", http_status=400,
            )
        if args.shadow_output is not None and args.limit != 1:
            raise AirwallexWebhookError(
                "webhook Shadow output requires --limit 1",
                error_type="invalid_webhook_shadow_output", http_status=400,
            )
        if args.shadow_output is not None and args.shadow_output.exists():
            raise AirwallexWebhookError(
                "webhook Shadow output already exists",
                error_type="invalid_webhook_shadow_output", http_status=409,
            )
        request_base = _json_file(args.request_base)
        forbidden = {
            "mode", "default_entity_id", "expense_ids", "webhook_contexts",
        }.intersection(request_base)
        if forbidden:
            raise AirwallexWebhookError(
                "webhook request base contains worker-controlled fields: " + ", ".join(sorted(forbidden)),
                error_type="invalid_webhook_request_base", http_status=400,
            )
        store = AirwallexWebhookStore(args.webhook_root)
        fingerprint = runtime.snapshot()["fingerprint"]
        processed = []
        shadow_observation = None
        for _ in range(args.limit):
            claim = store.claim_next(runtime_fingerprint=fingerprint, actor=args.actor)
            if claim is None:
                break
            try:
                connector_request = {
                    **request_base,
                    "mode": "refetch",
                    "default_entity_id": claim["entity_id"],
                    "expense_ids": [claim["raw_expense_id"]],
                    "webhook_contexts": [{
                        "receipt_id": claim["receipt_id"],
                        "event_name": claim["event_name"],
                        "event_created_at": claim["event_created_at"],
                        "expense_id_sha256": claim["expense_id_sha256"],
                        "body_sha256": claim["body_sha256"],
                        "runtime_fingerprint": claim["runtime_fingerprint"],
                    }],
                }
                result = dispatch_box_pipeline_request(runtime, {
                    "pipeline_id": "finance.expense_evidence_review",
                    "payload": {
                        "entity_id": claim["entity_id"],
                        "connector_request": connector_request,
                    },
                })
                briefing = result.get("founder_briefing") or {}
                if args.shadow_output is not None:
                    shadow_observation = write_airwallex_shadow_observation(
                        runtime, result, args.shadow_output,
                    )
                processed.append(store.record_success(
                    claim,
                    actor=args.actor,
                    result_summary={
                        "ready": bool(result.get("ready")),
                        "blocked_at": result.get("blocked_at"),
                        "record_count": int(briefing.get("record_count") or 0),
                        "state_change_count": int(briefing.get("state_change_count") or 0),
                        "network_access_performed": bool(result.get("network_access_performed")),
                        "external_actions_performed": False,
                    },
                ))
            except Exception as exc:
                processed.append(store.record_failure(claim, exc, actor=args.actor))
                break
        return {
            "schema_version": 1,
            "processed": processed,
            "processed_count": len(processed),
            "succeeded_count": sum(item.get("status") == "succeeded" for item in processed),
            "failed_count": sum(item.get("status") != "succeeded" for item in processed),
            "shadow_observation": shadow_observation,
            "raw_expense_ids_included": False,
            "secret_values_included": False,
            "expense_claims_created": False,
            "posting_performed": False,
            "payment_performed": False,
            "external_actions_performed": False,
        }
    if args.command == "airwallex-webhook-quarantine-resolve":
        return AirwallexWebhookStore(args.webhook_root).resolve_quarantine(
            args.receipt_id,
            runtime_fingerprint=runtime.snapshot()["fingerprint"],
            actor=args.actor,
            resolution=args.resolution,
            rationale=args.rationale,
            evidence_references=args.evidence_reference,
        )
    if args.command == "deployment-smoke":
        return run_deployment_smoke(
            args.config, args.packs, timeout_seconds=args.timeout_seconds,
        )
    if args.command == "context":
        return build_box_context(
            runtime,
            scope=args.scope,
            entity_id=args.entity,
            entity_ids=args.entities,
        )
    if args.command == "services":
        return build_box_bootstrap(runtime, registry)
    if args.command == "connectors":
        return {"connectors": build_box_connector_registry(runtime).catalog(runtime)}
    if args.command == "import":
        return build_box_connector_registry(runtime).dispatch(
            runtime,
            args.connector_id,
            _json_file(args.request),
        )
    if args.command == "commerce-pipeline":
        return run_commerce_import_analysis_pipeline(
            runtime,
            args.connector_id,
            _json_file(args.request),
        )
    if args.command == "pipeline":
        request = _json_file(args.request)
        source_verification = None
        if args.verify_source_runs:
            source_verification = PipelineRunStore(
                args.runs_root
            ).verify_month_close_portfolio_sources(
                request, runtime_fingerprint=runtime.snapshot()["fingerprint"],
            )
        result = dispatch_box_pipeline_request(runtime, request)
        if source_verification is not None:
            result["source_run_ledger_verified"] = True
            result["source_run_ledger_verification"] = source_verification
            result.setdefault("lineage", {})["source_run_ledger_verified"] = True
            result["lineage"]["source_attempt_ids"] = [
                item["attempt_id"] for item in source_verification["sources"]
            ]
        if not args.record:
            return result
        record = PipelineRunStore(args.runs_root).record(
            runtime.snapshot(), request, result, actor=args.actor,
        )
        return {"pipeline_result": result, "run_record": record}
    if args.command == "pipeline-runs":
        return {"runs": PipelineRunStore(args.runs_root).list(
            runtime_fingerprint=runtime.snapshot()["fingerprint"],
            pipeline_id=args.pipeline_id,
            entity_id=args.entity,
            limit=args.limit,
        )}
    if args.command == "pipeline-run-show":
        record = PipelineRunStore(args.runs_root).get(
            args.attempt_id, runtime_fingerprint=runtime.snapshot()["fingerprint"],
        )
        if record is None:
            raise PipelineRunStoreError("pipeline run attempt was not found for this Box")
        return {"run": record}
    if args.command == "pipeline-run-review":
        return {"run": PipelineRunStore(args.runs_root).review(
            args.attempt_id,
            runtime_fingerprint=runtime.snapshot()["fingerprint"],
            gate=args.gate,
            decision=args.decision,
            actor=args.actor,
            rationale=args.rationale,
            evidence_references=args.evidence_reference,
        )}
    if args.command == "pipeline-review-queue":
        return {"review_tasks": PipelineRunStore(args.runs_root).review_queue(
            runtime_fingerprint=runtime.snapshot()["fingerprint"],
            pipeline_id=args.pipeline_id,
            entity_id=args.entity,
            limit=args.limit,
        )}
    if args.command == "pipeline-runs-verify":
        return {"integrity": PipelineRunStore(args.runs_root).verify(
            runtime_fingerprint=runtime.snapshot()["fingerprint"],
        )}
    if args.command in {"pipeline-schedule-inspect", "pipeline-schedule-run", "pipeline-observability"}:
        now = None
        if args.now:
            try:
                now = datetime.fromisoformat(args.now.replace("Z", "+00:00"))
            except ValueError as exc:
                raise PipelineScheduleError("--now must be an ISO-8601 timestamp") from exc
            if now.tzinfo is None:
                raise PipelineScheduleError("--now must include a timezone offset")
        store = PipelineRunStore(args.runs_root)
        if args.command == "pipeline-observability":
            result = build_pipeline_observability(
                runtime, store, schedule_path=args.schedule, now=now,
                stale_review_hours=args.stale_review_hours,
            )
            if args.prometheus:
                return {
                    "format": "prometheus_text_0_0_4",
                    "metrics": render_pipeline_prometheus(result),
                    "raw_financial_data_included": False,
                    "external_actions_performed": False,
                }
            return result
        if args.command == "pipeline-schedule-inspect":
            return inspect_pipeline_schedule(args.schedule, runtime, store, now=now)
        return run_due_pipeline_schedule(
            args.schedule, runtime, store, actor=args.actor,
            now=now, job_id=args.job_id,
        )
    if args.command == "doctor":
        return diagnose_box(
            runtime,
            registry,
            as_of=args.as_of,
            tax_applicability_review_paths=args.tax_applicability_review,
            tax_applicability_review_dir=args.tax_applicability_review_dir,
            tax_applicability_registry_receipt=(
                args.tax_applicability_registry_receipt
            ),
            pilot_readiness_review=args.pilot_readiness_review,
            pilot_data_handoff_review=args.pilot_data_handoff_review,
            pilot_shadow_run_registration=args.pilot_shadow_run_registration,
            pilot_shadow_observation_review=args.pilot_shadow_observation_review,
            pilot_shadow_entity_reports=args.pilot_shadow_entity_report,
            pilot_shadow_portfolio_review=args.pilot_shadow_portfolio_review,
            pilot_shadow_series_review=args.pilot_shadow_series_review,
            pilot_shadow_series_evidence_root=(
                args.pilot_shadow_series_evidence_root
            ),
            pipeline_runs_root=args.pipeline_runs_root,
        )
    if args.command == "tax-rule-status":
        return evaluate_tax_rule_lifecycle(runtime, as_of=args.as_of)
    if args.command == "dispatch":
        return dispatch_box_service_request(runtime, registry, _json_file(args.request))
    if args.command == "cfo-metrics-evaluate":
        return registry.dispatch(
            runtime,
            "core.evaluate_cfo_metrics",
            _json_file(args.request),
            entity_id=args.entity,
        )
    if args.command == "tax-calendar":
        anchors: dict[str, Any] = {}
        if args.financial_year_end:
            anchors["financial_year_end"] = args.financial_year_end
        if args.gst_period_end:
            anchors["gst_period_end"] = args.gst_period_end
        return build_tax_calendar(
            runtime,
            args.entity,
            period_year=args.period_year,
            anchors=anchors,
            as_of=args.as_of,
        )
    raise RuntimeError(f"Unhandled command: {args.command}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = _run(args)
    except (
        BoxConfigError,
        BoxRuntimeError,
        BoxUpgradeError,
        ApiAuthError,
        BoxEvalError,
        BoxPipelineError,
        BoxScaffoldError,
        BoxServiceRequestError,
        CfoMetricEvaluationError,
        ActivationWorkspaceError,
        ActivationRunbookError,
        BoxHandoffVerifyError,
        BoxHandoffUnpackError,
        SourceKitError,
        SourceKitUnpackError,
        StarterWorkspaceError,
        TrialWorkspaceError,
        ConnectorError,
        ConnectorAccessProbeError,
        ConnectorAccessRegistryError,
        ConnectorScaffoldError,
        DistributionVerifyError,
        DeploymentSmokeError,
        DeploymentAssetError,
        RuntimeStorageError,
        ConnectorSyncError,
        AirwallexWebhookError,
        ReleasePromotionError,
        ShadowCloseArtifactError,
        ConnectorShadowArtifactError,
        MultiEntityShadowCloseError,
        PackServiceError,
        PipelineRunStoreError,
        PipelineScheduleError,
        TaxCalendarError,
        TaxPackLifecycleError,
        TaxApplicabilityArtifactError,
        PilotReadinessError,
        PilotDataHandoffError,
        PilotShadowRunError,
        PilotShadowObservationError,
        PilotShadowSeriesError,
        PilotShadowNextPeriodError,
        OSError,
        json.JSONDecodeError,
        ValueError,
    ) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps({"ok": True, "result": result}, ensure_ascii=False, indent=2))
    if args.command == "doctor" and not result["ready"]:
        return 3
    if args.command == "upgrade-check" and not result["compatible"]:
        return 4
    if args.command == "eval" and not result["passed"]:
        return 5
    if args.command == "release-candidate-audit" and not result["passed"]:
        return 6
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
