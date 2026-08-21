import json
import hashlib
import os
import shlex
import shutil
import stat
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from src.activation_workspace import (
    ActivationWorkspaceError,
    COMMANDS_NAME,
    DIRECTORIES,
    ENV_NAME,
    LEGACY_DIRECTORIES,
    MANIFEST_NAME,
    READINESS_DIRECTORY_ENV_NAMES,
    V2_DIRECTORIES,
    V3_DIRECTORIES,
    build_initialized_activation_status,
    initialize_activation_workspace,
    verify_activation_workspace,
)
from src.box_runtime import BoxRuntime
from src.cli import build_parser
from src.default_services import build_default_service_registry
from src.pilot_shadow_series import (
    assemble_pilot_shadow_series,
    review_pilot_shadow_series,
)
from tests import test_pilot_shadow_series as pilot_series_test_helpers


ROOT = Path(__file__).resolve().parents[1]


class ActivationWorkspaceTests(unittest.TestCase):
    def _runtime(self, name="global_game_studio.json"):
        return BoxRuntime(ROOT / "examples" / "boxes" / name, ROOT / "packs")

    def _initialize(self, parent: Path, name="global_game_studio.json"):
        destination = parent / "private activation workspace"
        result = initialize_activation_workspace(
            self._runtime(name),
            ROOT / "examples" / "boxes" / name,
            destination,
            period="2026-08",
            facts_as_of="2026-08-14",
            prepared_by="activation-preparer",
        )
        return destination, result

    def _downgrade_commands_to_v1(self, workspace: Path, manifest: dict) -> None:
        commands_path = workspace / COMMANDS_NAME
        commands = json.loads(commands_path.read_text(encoding="utf-8"))
        commands["schema_version"] = 1
        commands.pop("stage_sequence")
        commands["steps"] = [
            item for item in commands["steps"]
            if (
                item["step_id"].startswith("tax-")
                or item["step_id"].startswith("connector-")
                or item["step_id"] in {
                    "pilot-readiness-complete",
                    "pilot-readiness-review",
                    "workspace-status",
                }
            )
        ]
        for item in commands["steps"]:
            item.pop("requires_operator_edit", None)
        body = (json.dumps(commands, ensure_ascii=False, indent=2) + "\n").encode()
        commands_path.write_bytes(body)
        commands_path.chmod(0o600)
        for item in manifest["initial_files"]:
            if item["relative_path"] == COMMANDS_NAME:
                item["initial_sha256"] = hashlib.sha256(body).hexdigest()
                break

    def test_initializes_private_non_evidence_workspace_for_multi_entity_game(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            parent = Path(temp_dir).resolve()
            workspace, result = self._initialize(parent)
            self.assertTrue(result["initialized"])
            self.assertEqual(result["entity_count"], 2)
            self.assertEqual(result["network_connector_pack_count"], 0)
            self.assertEqual(result["directory_count"], len(DIRECTORIES))
            self.assertEqual(result["initial_file_count"], 6)
            self.assertEqual(result["review_artifact_count"], 0)
            self.assertEqual(result["connector_baseline_count"], 0)
            self.assertEqual(result["connector_baseline_workpaper_count"], 0)
            self.assertFalse(result["credentials_included"])
            self.assertFalse(result["financial_source_files_copied"])
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(workspace.stat().st_mode), 0o700)
            for relative in DIRECTORIES:
                directory = workspace / relative
                self.assertTrue(directory.is_dir())
                if os.name != "nt":
                    self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
            for path in workspace.rglob("*"):
                if path.is_file() and os.name != "nt":
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

            manifest = json.loads((workspace / MANIFEST_NAME).read_text(encoding="utf-8"))
            self.assertEqual(manifest["schema_version"], 5)
            self.assertEqual(manifest["entity_ids"], ["cn_studio", "sg_publisher"])
            self.assertEqual(manifest["network_connector_pack_ids"], [])
            self.assertNotIn(str(workspace), json.dumps(manifest))
            self.assertFalse(manifest["review_artifacts_created"])
            self.assertFalse(manifest["connector_baselines_created"])
            self.assertTrue((workspace / "tax" / "workpapers" / "cn_studio.json").is_file())
            self.assertTrue((workspace / "tax" / "workpapers" / "sg_publisher.json").is_file())
            self.assertEqual(list((workspace / "tax" / "reviews").iterdir()), [])
            self.assertEqual(list((workspace / "connector-shadow" / "reviews").iterdir()), [])
            self.assertEqual(list((workspace / "runbook").iterdir()), [])

            verified = verify_activation_workspace(self._runtime(), workspace)
            self.assertTrue(verified["valid"])
            self.assertEqual(verified["command_contract_schema_version"], 2)
            self.assertEqual(verified["tax_workpaper_count"], 2)
            self.assertFalse(verified["paths_returned"])
            self.assertFalse(verified["commands_executed"])

    def test_network_dtc_scope_is_prepared_but_no_baseline_or_credentials_are_faked(self):
        name = "sg_dtc_shopify_stripe_wise_store.json"
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace, result = self._initialize(Path(temp_dir).resolve(), name)
            self.assertEqual(result["network_connector_pack_count"], 3)
            self.assertEqual(result["connector_baseline_workpaper_count"], 3)
            manifest = json.loads((workspace / MANIFEST_NAME).read_text(encoding="utf-8"))
            self.assertEqual(
                manifest["network_connector_pack_ids"],
                ["connector.shopify", "connector.stripe", "connector.wise"],
            )
            self.assertEqual(list((workspace / "connector-shadow" / "baselines").iterdir()), [])
            connector_workpapers = list(
                (workspace / "connector-shadow" / "workpapers").glob("*.json")
            )
            self.assertEqual(len(connector_workpapers), 3)
            connector_workpapers_by_pipeline = {
                item["pipeline_id"]: item
                for item in (
                    json.loads(path.read_text(encoding="utf-8"))
                    for path in connector_workpapers
                )
            }
            self.assertEqual(
                set(connector_workpapers_by_pipeline),
                {
                    "dtc.shopify_stripe_month_close",
                    "finance.bank_statement_close",
                    "stripe.daily_close",
                },
            )
            for connector_workpaper in connector_workpapers_by_pipeline.values():
                self.assertTrue(connector_workpaper["template_only"])
                self.assertFalse(connector_workpaper["finalization_ready"])
            self.assertFalse(manifest["connector_baselines_created"])
            environment = (workspace / ENV_NAME).read_text(encoding="utf-8")
            self.assertIn("OPC_CONNECTOR_SHADOW_REVIEW_DIR=", environment)
            self.assertNotIn("OPC_SHOPIFY_ADMIN_TOKEN=", environment)
            self.assertNotIn("OPC_STRIPE_RESTRICTED_KEY=", environment)
            self.assertNotIn("OPC_WISE_ACCESS_TOKEN=", environment)
            status = build_initialized_activation_status(
                self._runtime(name),
                build_default_service_registry(),
                workspace,
                as_of="2026-08-14",
            )
            self.assertEqual(
                status["activation"]["summary"]["current_wave_stage_ids"],
                ["tax_applicability", "connector_configuration"],
            )
            self.assertFalse(status["control_boundary"]["private_paths_returned"])
            self.assertFalse(status["activation"]["summary"]["ready_for_external_filing"])
            self.assertEqual(
                status["connector_access"]["summary"]["expected_scope_count"], 3,
            )
            self.assertEqual(status["connector_access"]["schema_version"], 3)
            self.assertEqual(
                status["connector_access"]["summary"][
                    "warning_days_before_expiry"
                ],
                7,
            )
            self.assertEqual(
                status["connector_access"]["summary"]["renewal_due_count"], 0,
            )
            self.assertEqual(
                status["connector_access_alerts"]["warning_count"], 3,
            )
            self.assertEqual(
                status["connector_access_alerts"]["critical_count"], 0,
            )
            self.assertFalse(
                status["connector_access_alerts"]["notifications_sent"],
            )
            self.assertFalse(
                status["connector_access_alerts"]["schedule_installed"],
            )
            self.assertEqual(
                status["connector_access"]["counts"]["not_initialized"], 3,
            )
            self.assertFalse(
                status["connector_access"]["summary"][
                    "ready_for_bounded_shadow_dispatch"
                ]
            )
            self.assertNotIn(str(workspace), json.dumps(status))

            commands = json.loads((workspace / COMMANDS_NAME).read_text(encoding="utf-8"))
            connector_steps = [
                item for item in commands["steps"]
                if item["step_id"].startswith("connector-")
            ]
            self.assertEqual(len(connector_steps), 38)
            self.assertEqual(
                {item["action"] for item in connector_steps},
                {"edit_private_json", "run_cli"},
            )
            for pipeline_id in connector_workpapers_by_pipeline:
                scope = f"sg_store:{pipeline_id}"
                expected_steps = {
                    "connector-baseline-complete",
                    "connector-baseline-finalize",
                    "connector-shadow-assess",
                    "connector-shadow-review",
                }
                if pipeline_id in {
                    "dtc.shopify_stripe_month_close", "stripe.daily_close",
                }:
                    expected_steps.update({
                        "connector-shadow-request-init",
                        "connector-shadow-request-complete",
                        "connector-shadow-request-verify",
                        "connector-shadow-observe",
                    })
                if pipeline_id == "finance.bank_statement_close":
                    expected_steps.update({
                        "connector-shadow-request-init",
                        "connector-shadow-request-verify",
                        "connector-shadow-observe",
                    })
                self.assertEqual({
                    item["step_id"].split(":", 1)[0]
                    for item in connector_steps
                    if item["step_id"].endswith(scope)
                }, expected_steps)
            access_steps = [
                item for item in connector_steps
                if item["step_id"].startswith("connector-access-")
            ]
            self.assertEqual(len(access_steps), 15)
            self.assertEqual({
                item["step_id"].split(":", 1)[0] for item in access_steps
            }, {
                "connector-access-request-init",
                "connector-access-request-complete",
                "connector-access-request-verify",
                "connector-access-probe",
                "connector-access-receipt-verify",
            })
            for pack_id in (
                "connector.shopify", "connector.stripe", "connector.wise",
            ):
                self.assertEqual(sum(
                    item["step_id"].endswith(f"{pack_id}:sg_store")
                    for item in access_steps
                ), 5)
            monthly_observe = next(
                item for item in connector_steps
                if item.get("argv", [None, None])[1] == "shopify-monthly-shadow-observe"
            )
            self.assertEqual(monthly_observe["argv"][1], "shopify-monthly-shadow-observe")
            self.assertIn("--shopify-access-receipt", monthly_observe["argv"])
            self.assertIn("--stripe-access-receipt", monthly_observe["argv"])
            self.assertFalse(monthly_observe["requires_operator_edit"])
            monthly_request_init = next(
                item for item in connector_steps
                if item.get("argv", [None, None])[1]
                == "shopify-monthly-shadow-request-init"
            )
            self.assertEqual(
                monthly_request_init["argv"][1],
                "shopify-monthly-shadow-request-init",
            )
            self.assertFalse(monthly_request_init["requires_operator_edit"])
            monthly_request_edit = next(
                item for item in connector_steps
                if item["step_id"].endswith(
                    "sg_store:dtc.shopify_stripe_month_close"
                )
                and item["step_id"].startswith("connector-shadow-request-complete:")
            )
            self.assertTrue(monthly_request_edit["requires_operator_edit"])
            stripe_request_init = next(
                item for item in connector_steps
                if item.get("argv", [None, None])[1] == "stripe-shadow-request-init"
            )
            self.assertFalse(stripe_request_init["requires_operator_edit"])
            stripe_observe = next(
                item for item in connector_steps
                if item.get("argv", [None, None])[1] == "stripe-shadow-observe"
            )
            self.assertFalse(stripe_observe["requires_operator_edit"])
            self.assertIn("--access-request", stripe_observe["argv"])
            self.assertIn("--access-receipt", stripe_observe["argv"])
            monthly_stripe_request = monthly_observe["argv"][
                monthly_observe["argv"].index("--stripe-access-request") + 1
            ]
            daily_stripe_request = stripe_observe["argv"][
                stripe_observe["argv"].index("--access-request") + 1
            ]
            monthly_stripe_receipt = monthly_observe["argv"][
                monthly_observe["argv"].index("--stripe-access-receipt") + 1
            ]
            daily_stripe_receipt = stripe_observe["argv"][
                stripe_observe["argv"].index("--access-receipt") + 1
            ]
            self.assertEqual(monthly_stripe_request, daily_stripe_request)
            self.assertEqual(monthly_stripe_receipt, daily_stripe_receipt)
            stripe_assess = next(
                item for item in connector_steps
                if item["step_id"].endswith("sg_store:stripe.daily_close")
                and item["step_id"].startswith("connector-shadow-assess:")
            )
            self.assertEqual(stripe_assess["argv"][4], stripe_observe["argv"][-1])
            wise_request_init = next(
                item for item in connector_steps
                if item.get("argv", [None, None])[1] == "wise-shadow-request-init"
            )
            wise_request_verify = next(
                item for item in connector_steps
                if item.get("argv", [None, None])[1] == "wise-shadow-request-verify"
            )
            wise_observe = next(
                item for item in connector_steps
                if item.get("argv", [None, None])[1] == "wise-shadow-observe"
            )
            self.assertFalse(wise_request_init["requires_operator_edit"])
            self.assertFalse(wise_request_verify["requires_operator_edit"])
            self.assertFalse(wise_observe["requires_operator_edit"])
            self.assertIn("--access-request", wise_observe["argv"])
            self.assertIn("--access-receipt", wise_observe["argv"])
            self.assertFalse(any(
                item["step_id"].startswith("connector-shadow-request-complete:")
                and item["step_id"].endswith(
                    "sg_store:finance.bank_statement_close"
                )
                for item in connector_steps
            ))
            wise_assess = next(
                item for item in connector_steps
                if item["step_id"].endswith(
                    "sg_store:finance.bank_statement_close"
                )
                and item["step_id"].startswith("connector-shadow-assess:")
            )
            self.assertEqual(wise_assess["argv"][4], wise_observe["argv"][-1])
            serialized_commands = json.dumps(commands, ensure_ascii=False)
            self.assertNotIn("OPC_SHOPIFY_ADMIN_TOKEN", serialized_commands)
            self.assertNotIn("OPC_STRIPE_RESTRICTED_KEY", serialized_commands)
            self.assertNotIn("OPC_WISE_ACCESS_TOKEN", serialized_commands)

    def test_xero_activation_generates_private_request_chain_for_each_bound_entity(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace, result = self._initialize(
                Path(temp_dir).resolve(), "global_game_studio_xero.json",
            )
            self.assertEqual(result["network_connector_pack_count"], 1)
            self.assertEqual(result["connector_baseline_workpaper_count"], 2)
            commands = json.loads((workspace / COMMANDS_NAME).read_text(encoding="utf-8"))
            connector_steps = [
                item for item in commands["steps"]
                if item["step_id"].startswith("connector-")
            ]
            self.assertEqual(len(connector_steps), 24)
            for entity_id in ("cn_studio", "sg_publisher"):
                scope = f"{entity_id}:finance.trial_balance_review"
                scoped = [
                    item for item in connector_steps
                    if item["step_id"].endswith(scope)
                ]
                self.assertEqual({
                    item["step_id"].split(":", 1)[0]
                    for item in scoped
                }, {
                    "connector-baseline-complete",
                    "connector-baseline-finalize",
                    "connector-shadow-request-init",
                    "connector-shadow-request-verify",
                    "connector-shadow-observe",
                    "connector-shadow-assess",
                    "connector-shadow-review",
                })
                self.assertFalse(any(
                    item["step_id"].startswith("connector-shadow-request-complete:")
                    for item in scoped
                ))
                init = next(
                    item for item in scoped
                    if item.get("argv", [None, None])[1] == "xero-shadow-request-init"
                )
                verify = next(
                    item for item in scoped
                    if item.get("argv", [None, None])[1] == "xero-shadow-request-verify"
                )
                observe = next(
                    item for item in scoped
                    if item.get("argv", [None, None])[1] == "xero-shadow-observe"
                )
                assess = next(
                    item for item in scoped
                    if item["step_id"].startswith("connector-shadow-assess:")
                )
                self.assertEqual(init["argv"][4], entity_id)
                self.assertFalse(init["requires_operator_edit"])
                self.assertFalse(verify["requires_operator_edit"])
                self.assertFalse(observe["requires_operator_edit"])
                self.assertIn("--access-request", observe["argv"])
                self.assertIn("--access-receipt", observe["argv"])
                self.assertEqual(assess["argv"][4], observe["argv"][-1])
                access_scoped = [
                    item for item in connector_steps
                    if item["step_id"].endswith(
                        f"connector.xero:{entity_id}"
                    )
                ]
                self.assertEqual(len(access_scoped), 5)
                self.assertEqual({
                    item["step_id"].split(":", 1)[0]
                    for item in access_scoped
                }, {
                    "connector-access-request-init",
                    "connector-access-request-complete",
                    "connector-access-request-verify",
                    "connector-access-probe",
                    "connector-access-receipt-verify",
                })
            serialized = json.dumps(commands, ensure_ascii=False)
            self.assertNotIn("OPC_XERO_CLIENT_ID", serialized)
            self.assertNotIn("OPC_XERO_CLIENT_SECRET", serialized)
            self.assertNotIn("OPC_XERO_TENANT_ID", serialized)
            self.assertTrue(verify_activation_workspace(
                self._runtime("global_game_studio_xero.json"), workspace,
            )["valid"])

    def test_paypal_activation_uses_safe_request_and_observation_without_manual_json(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace, result = self._initialize(
                Path(temp_dir).resolve(), "us_dtc_paypal_c_corp.json",
            )
            self.assertEqual(result["network_connector_pack_count"], 1)
            self.assertEqual(result["connector_baseline_workpaper_count"], 1)
            commands = json.loads((workspace / COMMANDS_NAME).read_text(encoding="utf-8"))
            scope = "us_dtc_company:paypal.transaction_close"
            scoped = [
                item for item in commands["steps"]
                if item["step_id"].startswith("connector-")
                and item["step_id"].endswith(scope)
            ]
            self.assertEqual(len(scoped), 7)
            self.assertEqual({
                item["step_id"].split(":", 1)[0] for item in scoped
            }, {
                "connector-baseline-complete", "connector-baseline-finalize",
                "connector-shadow-request-init", "connector-shadow-request-verify",
                "connector-shadow-observe", "connector-shadow-assess",
                "connector-shadow-review",
            })
            self.assertFalse(any(
                item["step_id"].startswith("connector-shadow-request-complete:")
                for item in scoped
            ))
            commands_by_name = {
                item.get("argv", [None, None])[1]: item
                for item in scoped if item.get("action") == "run_cli"
            }
            for command in (
                "paypal-shadow-request-init", "paypal-shadow-request-verify",
                "paypal-shadow-observe",
            ):
                self.assertIn(command, commands_by_name)
                self.assertFalse(commands_by_name[command]["requires_operator_edit"])
            self.assertIn(
                "--access-request", commands_by_name["paypal-shadow-observe"]["argv"],
            )
            self.assertIn(
                "--access-receipt", commands_by_name["paypal-shadow-observe"]["argv"],
            )
            access_scoped = [
                item for item in commands["steps"]
                if item["step_id"].endswith(
                    "connector.paypal:us_dtc_company"
                )
            ]
            self.assertEqual(len(access_scoped), 5)
            self.assertEqual({
                item["step_id"].split(":", 1)[0] for item in access_scoped
            }, {
                "connector-access-request-init",
                "connector-access-request-complete",
                "connector-access-request-verify",
                "connector-access-probe",
                "connector-access-receipt-verify",
            })
            self.assertEqual(
                commands_by_name["connector-shadow-assess"]["argv"][4],
                commands_by_name["paypal-shadow-observe"]["argv"][-1],
            )
            serialized = json.dumps(commands, ensure_ascii=False)
            self.assertNotIn("OPC_PAYPAL_CLIENT_ID", serialized)
            self.assertNotIn("OPC_PAYPAL_CLIENT_SECRET", serialized)
            self.assertTrue(verify_activation_workspace(
                self._runtime("us_dtc_paypal_c_corp.json"), workspace,
            )["valid"])

    def test_woocommerce_activation_uses_safe_request_and_observation_without_manual_json(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace, result = self._initialize(
                Path(temp_dir).resolve(), "us_dtc_woocommerce_c_corp.json",
            )
            self.assertEqual(result["network_connector_pack_count"], 1)
            self.assertEqual(result["connector_baseline_workpaper_count"], 1)
            commands = json.loads((workspace / COMMANDS_NAME).read_text(encoding="utf-8"))
            scope = "us_dtc_company:woocommerce.order_refund_close"
            scoped = [
                item for item in commands["steps"]
                if item["step_id"].startswith("connector-")
                and item["step_id"].endswith(scope)
            ]
            self.assertEqual(len(scoped), 7)
            self.assertEqual({
                item["step_id"].split(":", 1)[0] for item in scoped
            }, {
                "connector-baseline-complete", "connector-baseline-finalize",
                "connector-shadow-request-init", "connector-shadow-request-verify",
                "connector-shadow-observe", "connector-shadow-assess",
                "connector-shadow-review",
            })
            self.assertFalse(any(
                item["step_id"].startswith("connector-shadow-request-complete:")
                for item in scoped
            ))
            commands_by_name = {
                item.get("argv", [None, None])[1]: item
                for item in scoped if item.get("action") == "run_cli"
            }
            for command in (
                "woocommerce-shadow-request-init",
                "woocommerce-shadow-request-verify", "woocommerce-shadow-observe",
            ):
                self.assertIn(command, commands_by_name)
                self.assertFalse(commands_by_name[command]["requires_operator_edit"])
            self.assertIn(
                "--access-request",
                commands_by_name["woocommerce-shadow-observe"]["argv"],
            )
            self.assertIn(
                "--access-receipt",
                commands_by_name["woocommerce-shadow-observe"]["argv"],
            )
            access_scoped = [
                item for item in commands["steps"]
                if item["step_id"].endswith(
                    "connector.woocommerce:us_dtc_company"
                )
            ]
            self.assertEqual(len(access_scoped), 5)
            self.assertEqual({
                item["step_id"].split(":", 1)[0] for item in access_scoped
            }, {
                "connector-access-request-init",
                "connector-access-request-complete",
                "connector-access-request-verify",
                "connector-access-probe",
                "connector-access-receipt-verify",
            })
            self.assertEqual(
                commands_by_name["connector-shadow-assess"]["argv"][4],
                commands_by_name["woocommerce-shadow-observe"]["argv"][-1],
            )
            serialized = json.dumps(commands, ensure_ascii=False)
            self.assertNotIn("OPC_WOOCOMMERCE_SITE_ORIGIN", serialized)
            self.assertNotIn("OPC_WOOCOMMERCE_CONSUMER_KEY", serialized)
            self.assertNotIn("OPC_WOOCOMMERCE_CONSUMER_SECRET", serialized)
            self.assertTrue(verify_activation_workspace(
                self._runtime("us_dtc_woocommerce_c_corp.json"), workspace,
            )["valid"])

    def test_shipbob_activation_uses_safe_request_and_observation_without_manual_json(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace, _ = self._initialize(
                Path(temp_dir).resolve(),
                "us_dtc_shopify_stripe_shipbob_c_corp.json",
            )
            commands = json.loads((workspace / COMMANDS_NAME).read_text(encoding="utf-8"))
            scope = "us_dtc_company:commerce.shipbob_fulfillment_close"
            scoped = [
                item for item in commands["steps"]
                if item["step_id"].startswith("connector-")
                and item["step_id"].endswith(scope)
            ]
            self.assertEqual(len(scoped), 7)
            self.assertEqual({
                item["step_id"].split(":", 1)[0] for item in scoped
            }, {
                "connector-baseline-complete", "connector-baseline-finalize",
                "connector-shadow-request-init", "connector-shadow-request-verify",
                "connector-shadow-observe", "connector-shadow-assess",
                "connector-shadow-review",
            })
            self.assertFalse(any(
                item["step_id"].startswith("connector-shadow-request-complete:")
                for item in scoped
            ))
            commands_by_name = {
                item.get("argv", [None, None])[1]: item
                for item in scoped if item.get("action") == "run_cli"
            }
            for command in (
                "shipbob-shadow-request-init", "shipbob-shadow-request-verify",
                "shipbob-shadow-observe",
            ):
                self.assertIn(command, commands_by_name)
                self.assertFalse(commands_by_name[command]["requires_operator_edit"])
            self.assertEqual(
                commands_by_name["connector-shadow-assess"]["argv"][4],
                commands_by_name["shipbob-shadow-observe"]["argv"][-1],
            )
            self.assertIn(
                "--access-request",
                commands_by_name["shipbob-shadow-observe"]["argv"],
            )
            self.assertIn(
                "--access-receipt",
                commands_by_name["shipbob-shadow-observe"]["argv"],
            )
            access_scoped = [
                item for item in commands["steps"]
                if item["step_id"].endswith(
                    "connector.shipbob:us_dtc_company"
                )
            ]
            self.assertEqual(len(access_scoped), 5)
            serialized = json.dumps(commands, ensure_ascii=False)
            self.assertNotIn("OPC_SHIPBOB_ACCESS_TOKEN", serialized)
            self.assertTrue(verify_activation_workspace(
                self._runtime("us_dtc_shopify_stripe_shipbob_c_corp.json"), workspace,
            )["valid"])

    def test_amazon_seller_activation_selects_marketplace_then_uses_safe_observation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace, _ = self._initialize(
                Path(temp_dir).resolve(), "us_marketplace_amazon_seller_c_corp.json",
            )
            commands = json.loads((workspace / COMMANDS_NAME).read_text(encoding="utf-8"))
            scope = "us_amazon_marketplace_company:amazon_seller.marketplace_close"
            scoped = [
                item for item in commands["steps"]
                if item["step_id"].startswith("connector-")
                and item["step_id"].endswith(scope)
            ]
            self.assertEqual(len(scoped), 7)
            self.assertFalse(any(
                item["step_id"].startswith("connector-shadow-request-complete:")
                for item in scoped
            ))
            commands_by_name = {
                item.get("argv", [None, None])[1]: item
                for item in scoped if item.get("action") == "run_cli"
            }
            for command in (
                "amazon-seller-shadow-request-init",
                "amazon-seller-shadow-request-verify",
                "amazon-seller-shadow-observe",
            ):
                self.assertIn(command, commands_by_name)
            self.assertTrue(
                commands_by_name["amazon-seller-shadow-request-init"]["requires_operator_edit"]
            )
            self.assertIn(
                "REPLACE_WITH_MARKETPLACE_ID",
                commands_by_name["amazon-seller-shadow-request-init"]["argv"],
            )
            self.assertFalse(
                commands_by_name["amazon-seller-shadow-request-verify"]["requires_operator_edit"]
            )
            self.assertFalse(
                commands_by_name["amazon-seller-shadow-observe"]["requires_operator_edit"]
            )
            self.assertEqual(
                commands_by_name["connector-shadow-assess"]["argv"][4],
                commands_by_name["amazon-seller-shadow-observe"]["argv"][-1],
            )
            self.assertIn(
                "--access-request",
                commands_by_name["amazon-seller-shadow-observe"]["argv"],
            )
            self.assertIn(
                "--access-receipt",
                commands_by_name["amazon-seller-shadow-observe"]["argv"],
            )
            access_scoped = [
                item for item in commands["steps"]
                if item["step_id"].endswith(
                    "connector.amazon_seller:us_amazon_marketplace_company"
                )
            ]
            self.assertEqual(len(access_scoped), 5)
            serialized = json.dumps(commands, ensure_ascii=False)
            for secret_name in (
                "OPC_AMAZON_SELLER_CLIENT_ID", "OPC_AMAZON_SELLER_CLIENT_SECRET",
                "OPC_AMAZON_SELLER_REFRESH_TOKEN", "OPC_AMAZON_SELLER_ID",
            ):
                self.assertNotIn(secret_name, serialized)
            self.assertTrue(verify_activation_workspace(
                self._runtime("us_marketplace_amazon_seller_c_corp.json"), workspace,
            )["valid"])

    def test_status_mounts_only_readiness_directories_and_reads_empty_promotion_ledger(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace, _ = self._initialize(Path(temp_dir).resolve())
            promotion_root = workspace / "promotion" / "ledger"
            self.assertEqual(list(promotion_root.iterdir()), [])

            status = build_initialized_activation_status(
                self._runtime(),
                build_default_service_registry(),
                workspace,
                as_of="2026-08-14",
            )
            stages = {
                item["stage_id"]: item
                for item in status["activation"]["stages"]
            }
            self.assertEqual(
                stages["stable_promotion"]["evidence_status"],
                "promotion_assessment_missing",
            )
            self.assertEqual(list(promotion_root.iterdir()), [])
            self.assertNotIn(str(workspace), json.dumps(status))

            fake_activation = {"as_of": "2026-08-14"}
            with patch(
                "src.activation_workspace.build_activation_workspace",
                return_value=fake_activation,
            ) as builder:
                projected = build_initialized_activation_status(
                    self._runtime(),
                    build_default_service_registry(),
                    workspace,
                    as_of="2026-08-14",
                )
            environment = builder.call_args.kwargs["environ"]
            self.assertEqual(set(environment), set(READINESS_DIRECTORY_ENV_NAMES))
            self.assertTrue(all(Path(value).is_dir() for value in environment.values()))
            self.assertNotIn("OPC_ACTIVATION_RUNBOOK_ROOT", environment)
            self.assertNotIn("OPC_FINANCE_PIPELINE_RUNS_ROOT", environment)
            self.assertIs(projected["activation"], fake_activation)

    def test_status_consumes_reviewed_observation_and_series_from_workspace_directories(self):
        name = "cn_dtc_shopify_stripe_store.json"
        runtime = self._runtime(name)
        with tempfile.TemporaryDirectory() as temp_dir:
            parent = Path(temp_dir).resolve()
            workspace, _ = self._initialize(parent, name)
            staging = parent / "evidence staging"
            staging.mkdir()
            evidence_root = workspace / "pilot" / "series-periods"
            runs_root = workspace / "pipeline-runs"
            helper = pilot_series_test_helpers.PilotShadowSeriesTests(
                methodName="runTest"
            )
            helper.setUp()
            helper.build_period(
                staging, evidence_root, runs_root, "2026-07", multiplier=1,
                runtime=runtime,
            )
            helper.build_period(
                staging, evidence_root, runs_root, "2026-08", multiplier=2,
                runtime=runtime,
            )

            latest = evidence_root / "2026-08"
            copies = {
                latest / "pilot-readiness-review.json": (
                    workspace / "pilot" / "readiness" / "reviewed.json"
                ),
                latest / "data-handoff-review.json": (
                    workspace / "pilot" / "handoff" / "reviewed.json"
                ),
                latest / "shadow-run-registration.json": (
                    workspace / "pilot" / "registrations" / "first-run.json"
                ),
                latest / "reviewed-observation.json": (
                    workspace / "pilot" / "observations" / "first-reviewed.json"
                ),
                latest / "entity-reports" / "cn_dtc_company.json": (
                    workspace / "pilot" / "entity-reports" / "cn_dtc_company.json"
                ),
            }
            for source, destination in copies.items():
                shutil.copyfile(source, destination)
                destination.chmod(0o600)

            series_receipt = staging / "series-receipt.json"
            series_review = (
                workspace / "pilot" / "observations" / "series-reviewed.json"
            )
            assemble_pilot_shadow_series(
                runtime, evidence_root, runs_root, series_receipt,
            )
            review_pilot_shadow_series(
                runtime,
                series_receipt,
                series_review,
                decision="approved-for-promotion-evidence",
                actor="workspace-continuity-reviewer",
                rationale=(
                    "Independent review confirms the exact two-period workspace evidence."
                ),
                evidence_references=["audit://activation-workspace/series"],
            )
            for directory in workspace.rglob("*"):
                if directory.is_dir():
                    directory.chmod(0o700)

            status = build_initialized_activation_status(
                runtime,
                build_default_service_registry(),
                workspace,
                as_of=datetime.now(timezone.utc).date().isoformat(),
            )
            stages = {
                item["stage_id"]: item
                for item in status["activation"]["stages"]
            }
            self.assertEqual(
                stages["shadow_observation"]["evidence_status"], "current"
            )
            self.assertEqual(
                stages["consecutive_shadow_series"]["evidence_status"], "current"
            )
            self.assertEqual(
                stages["consecutive_shadow_series"]["work_status"], "completed"
            )
            self.assertNotIn(str(workspace), json.dumps(status))

    def test_generated_command_argv_and_shell_preview_remain_equivalent_with_spaces(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace, _ = self._initialize(Path(temp_dir).resolve())
            commands = json.loads((workspace / COMMANDS_NAME).read_text(encoding="utf-8"))
            self.assertEqual(commands["schema_version"], 2)
            self.assertEqual(
                commands["stage_sequence"],
                [
                    "tax_applicability",
                    "connector_shadow_evidence",
                    "pilot_readiness",
                    "data_handoff",
                    "shadow_run_registration",
                    "shadow_close_reports",
                    "shadow_observation",
                    "consecutive_shadow_series",
                    "stable_promotion",
                ],
            )
            runnable = [item for item in commands["steps"] if item["action"] == "run_cli"]
            self.assertGreater(len(runnable), 0)
            parser = build_parser()
            for item in runnable:
                self.assertEqual(shlex.split(item["shell_preview"]), item["argv"])
                self.assertEqual(
                    item["requires_operator_edit"],
                    any("REPLACE_WITH_" in argument for argument in item["argv"]),
                )
                parser.parse_args(item["argv"][1:])
                self.assertFalse(item["command_executed"])
            step_ids = {item["step_id"] for item in commands["steps"]}
            self.assertTrue({
                "data-handoff-init",
                "shadow-run-register",
                "shadow-portfolio-assemble",
                "shadow-observation-assemble",
                "shadow-series-assemble",
                "promotion-template",
                "promotion-review",
                "promotion-ledger-verify",
            }.issubset(step_ids))
            registration = next(
                item for item in runnable if item["step_id"] == "shadow-run-register"
            )
            parsed_registration = parser.parse_args(registration["argv"][1:])
            self.assertEqual(
                parsed_registration.entity_attempt,
                [
                    "cn_studio=REPLACE_WITH_REVIEWED_ATTEMPT_ID",
                    "sg_publisher=REPLACE_WITH_REVIEWED_ATTEMPT_ID",
                ],
            )
            self.assertNotIn("REPLACE_WITH_ALLOWED_DECISION", json.dumps(commands))
            self.assertFalse(commands["contains_credentials"])
            self.assertFalse(commands["contains_financial_values"])
            self.assertFalse(commands["external_actions_performed"])

    def test_single_entity_chain_omits_portfolio_and_stripe_only_profile_parses(self):
        name = "cn_dtc_stripe_store.json"
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace, _ = self._initialize(Path(temp_dir).resolve(), name)
            workpaper = json.loads(next(
                (workspace / "connector-shadow" / "workpapers").glob("*.json")
            ).read_text(encoding="utf-8"))
            self.assertEqual(workpaper["pipeline_id"], "stripe.daily_close")
            commands = json.loads((workspace / COMMANDS_NAME).read_text(encoding="utf-8"))
            step_ids = {item["step_id"] for item in commands["steps"]}
            self.assertFalse(any("portfolio" in step_id for step_id in step_ids))
            parser = build_parser()
            for item in commands["steps"]:
                if item["action"] == "run_cli":
                    parser.parse_args(item["argv"][1:])
            self.assertEqual(list((workspace / "pilot" / "entity-reports").iterdir()), [])
            self.assertEqual(list((workspace / "pilot" / "shadow-reports").iterdir()), [])
            self.assertEqual(list((workspace / "promotion" / "ledger").iterdir()), [])

    def test_v2_workspace_and_legacy_command_contract_remain_verifiable(self):
        name = "sg_dtc_shopify_stripe_wise_store.json"
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace, _ = self._initialize(Path(temp_dir).resolve(), name)
            manifest_path = workspace / MANIFEST_NAME
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["schema_version"] = 2
            manifest["directory_contract"] = list(V2_DIRECTORIES)
            self._downgrade_commands_to_v1(workspace, manifest)
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            manifest_path.chmod(0o600)
            verified = verify_activation_workspace(self._runtime(name), workspace)
            self.assertTrue(verified["valid"])
            self.assertEqual(verified["command_contract_schema_version"], 1)
            self.assertEqual(verified["connector_baseline_workpaper_count"], 3)
            status = build_initialized_activation_status(
                self._runtime(name),
                build_default_service_registry(),
                workspace,
                as_of="2026-08-14",
            )
            legacy_alerts = status["connector_access_alerts"]
            self.assertEqual(legacy_alerts["alert_count"], 1)
            self.assertEqual(
                legacy_alerts["alerts"][0]["alert_id"],
                "connector-access:registry:migration-required",
            )
            self.assertFalse(legacy_alerts["notifications_sent"])

    def test_v3_workspace_and_full_command_contract_remain_verifiable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace, _ = self._initialize(Path(temp_dir).resolve())
            manifest_path = workspace / MANIFEST_NAME
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["schema_version"] = 3
            manifest["directory_contract"] = list(V3_DIRECTORIES)
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            manifest_path.chmod(0o600)
            (workspace / "runbook").rmdir()
            verified = verify_activation_workspace(self._runtime(), workspace)
            self.assertTrue(verified["valid"])
            self.assertEqual(verified["command_contract_schema_version"], 2)

    def test_rebinding_manifest_hash_cannot_authorize_a_modified_v27_command(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace, _ = self._initialize(Path(temp_dir).resolve())
            commands_path = workspace / COMMANDS_NAME
            commands = json.loads(commands_path.read_text(encoding="utf-8"))
            review = next(
                item for item in commands["steps"]
                if item["step_id"] == "promotion-review"
            )
            decision_index = review["argv"].index("--decision") + 1
            review["argv"][decision_index] = "approved"
            review["shell_preview"] = shlex.join(review["argv"])
            body = (json.dumps(commands, ensure_ascii=False, indent=2) + "\n").encode()
            commands_path.write_bytes(body)
            commands_path.chmod(0o600)
            manifest_path = workspace / MANIFEST_NAME
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            for item in manifest["initial_files"]:
                if item["relative_path"] == COMMANDS_NAME:
                    item["initial_sha256"] = hashlib.sha256(body).hexdigest()
                    break
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            manifest_path.chmod(0o600)
            with self.assertRaisesRegex(
                ActivationWorkspaceError, "changed from the current Box template",
            ):
                verify_activation_workspace(self._runtime(), workspace)

    def test_refuses_relative_existing_tampered_and_nonprivate_workspaces(self):
        runtime = self._runtime()
        with self.assertRaisesRegex(ActivationWorkspaceError, "must be absolute"):
            initialize_activation_workspace(
                runtime,
                ROOT / "examples" / "boxes" / "global_game_studio.json",
                Path("relative/private"),
                period="2026-08", facts_as_of="2026-08-14",
                prepared_by="activation-preparer",
            )
        with tempfile.TemporaryDirectory() as temp_dir:
            parent = Path(temp_dir).resolve()
            existing = parent / "existing"
            existing.mkdir()
            sentinel = existing / "sentinel.txt"
            sentinel.write_text("preserve", encoding="utf-8")
            with self.assertRaisesRegex(ActivationWorkspaceError, "must not already exist"):
                initialize_activation_workspace(
                    runtime,
                    ROOT / "examples" / "boxes" / "global_game_studio.json",
                    existing,
                    period="2026-08", facts_as_of="2026-08-14",
                    prepared_by="activation-preparer",
                )
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve")

            workspace, _ = self._initialize(parent)
            commands = workspace / COMMANDS_NAME
            commands.write_text("{}\n", encoding="utf-8")
            commands.chmod(0o600)
            with self.assertRaisesRegex(ActivationWorkspaceError, "immutable file changed"):
                verify_activation_workspace(runtime, workspace)

            commands.unlink()
            os.symlink(parent / "outside.json", commands)
            with self.assertRaisesRegex(ActivationWorkspaceError, "symbolic links"):
                verify_activation_workspace(runtime, workspace)

    def test_tax_workpaper_raw_identifier_tampering_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            parent = Path(temp_dir).resolve()
            workspace, _ = self._initialize(parent)
            workpaper_path = workspace / "tax" / "workpapers" / "cn_studio.json"
            workpaper = json.loads(workpaper_path.read_text(encoding="utf-8"))
            workpaper["entity"]["tax_number"] = "raw-sensitive-value"
            workpaper_path.write_text(
                json.dumps(workpaper, ensure_ascii=False), encoding="utf-8",
            )
            workpaper_path.chmod(0o600)
            with self.assertRaisesRegex(ActivationWorkspaceError, "tax workpaper is invalid"):
                verify_activation_workspace(self._runtime(), workspace)

    def test_invalid_inputs_are_transactional_and_box_or_permission_changes_fail_closed(self):
        runtime = self._runtime()
        with tempfile.TemporaryDirectory() as temp_dir:
            parent = Path(temp_dir).resolve()
            invalid_destination = parent / "invalid period"
            with self.assertRaisesRegex(Exception, "YYYY-MM"):
                initialize_activation_workspace(
                    runtime,
                    ROOT / "examples" / "boxes" / "global_game_studio.json",
                    invalid_destination,
                    period="2026-13-extra", facts_as_of="2026-08-14",
                    prepared_by="activation-preparer",
                )
            self.assertFalse(invalid_destination.exists())

            workspace, _ = self._initialize(parent)
            with self.assertRaisesRegex(
                ActivationWorkspaceError, "current Box contract",
            ):
                verify_activation_workspace(
                    self._runtime("sg_dtc_shopify_stripe_wise_store.json"),
                    workspace,
                )

            if os.name != "nt":
                workspace.chmod(0o755)
                with self.assertRaisesRegex(ActivationWorkspaceError, "mode 0700"):
                    verify_activation_workspace(runtime, workspace)
                workspace.chmod(0o700)

                private_directory = workspace / "tax" / "reviews"
                private_directory.chmod(0o755)
                with self.assertRaisesRegex(ActivationWorkspaceError, "mode 0700"):
                    verify_activation_workspace(runtime, workspace)
                private_directory.chmod(0o700)

    def test_pilot_workpaper_cannot_claim_credentials_values_or_external_authority(self):
        unsafe_fields = (
            "contains_credentials",
            "contains_raw_source_identifiers",
            "contains_raw_tax_identifiers",
            "contains_financial_values",
            "external_actions_authorized",
        )
        for unsafe_field in unsafe_fields:
            with self.subTest(field=unsafe_field), tempfile.TemporaryDirectory() as temp_dir:
                workspace, _ = self._initialize(Path(temp_dir).resolve())
                path = workspace / "pilot" / "readiness" / "workpaper.json"
                workpaper = json.loads(path.read_text(encoding="utf-8"))
                workpaper[unsafe_field] = True
                path.write_text(
                    json.dumps(workpaper, ensure_ascii=False), encoding="utf-8",
                )
                path.chmod(0o600)
                with self.assertRaisesRegex(
                    ActivationWorkspaceError, "private input boundary",
                ):
                    verify_activation_workspace(self._runtime(), workspace)

    def test_connector_workpaper_tampering_fails_and_v1_workspace_remains_verifiable(self):
        name = "sg_dtc_shopify_stripe_wise_store.json"
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace, _ = self._initialize(Path(temp_dir).resolve(), name)
            workpaper_path = next(
                (workspace / "connector-shadow" / "workpapers").glob("*.json")
            )
            workpaper = json.loads(workpaper_path.read_text(encoding="utf-8"))
            workpaper["covered_pack_ids"] = ["connector.stripe"]
            workpaper_path.write_text(json.dumps(workpaper), encoding="utf-8")
            workpaper_path.chmod(0o600)
            with self.assertRaisesRegex(
                ActivationWorkspaceError, "Connector baseline workpaper is invalid",
            ):
                verify_activation_workspace(self._runtime(name), workspace)

        with tempfile.TemporaryDirectory() as temp_dir:
            workspace, _ = self._initialize(Path(temp_dir).resolve())
            manifest_path = workspace / MANIFEST_NAME
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["schema_version"] = 1
            manifest.pop("connector_baseline_workpaper_count")
            manifest["directory_contract"] = list(LEGACY_DIRECTORIES)
            self._downgrade_commands_to_v1(workspace, manifest)
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            manifest_path.chmod(0o600)
            (workspace / "connector-shadow" / "workpapers").rmdir()
            verified = verify_activation_workspace(self._runtime(), workspace)
            self.assertTrue(verified["valid"])
            self.assertEqual(verified["command_contract_schema_version"], 1)
            self.assertEqual(verified["connector_baseline_workpaper_count"], 0)


if __name__ == "__main__":
    unittest.main()
