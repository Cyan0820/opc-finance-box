import json
import shlex
import unittest
from pathlib import Path

from src.activation_orchestrator import (
    build_activation_stage_contracts,
    build_activation_workspace,
    project_activation_stages,
)
from src.box_runtime import BoxRuntime
from src.default_services import build_default_service_registry
from src.cli import build_parser


ROOT = Path(__file__).resolve().parents[1]


class ActivationOrchestratorTests(unittest.TestCase):
    def _workspace(self, box_name: str, environ=None):
        runtime = BoxRuntime(
            ROOT / "examples" / "boxes" / box_name,
            ROOT / "packs",
        )
        return build_activation_workspace(
            runtime,
            build_default_service_registry(),
            runs_root=ROOT / ".test-missing-runs",
            environ=environ or {},
            as_of="2026-08-14",
        )

    def test_file_only_box_skips_connector_shadow_and_starts_with_tax(self):
        result = self._workspace("global_game_studio.json")
        self.assertEqual(result["summary"]["stage_count"], 11)
        self.assertEqual(
            result["summary"]["current_wave_stage_ids"],
            ["tax_applicability"],
        )
        stages = {item["stage_id"]: item for item in result["stages"]}
        self.assertEqual(stages["connector_configuration"]["work_status"], "completed")
        self.assertEqual(stages["connector_shadow_evidence"]["work_status"], "completed")
        self.assertEqual(stages["connector_shadow_evidence"]["evidence_status"], "not_required")
        self.assertEqual(stages["pilot_readiness"]["work_status"], "blocked_by_dependency")
        self.assertEqual(
            stages["pilot_readiness"]["unmet_dependency_ids"],
            ["tax_applicability"],
        )

    def test_network_box_exposes_parallel_tax_and_connector_work_wave(self):
        result = self._workspace("sg_dtc_shopify_stripe_wise_store.json")
        self.assertEqual(
            result["summary"]["current_wave_stage_ids"],
            ["tax_applicability", "connector_configuration"],
        )
        current = {item["stage_id"]: item for item in result["current_wave"]}
        self.assertEqual(
            current["tax_applicability"]["independent_review_role"],
            "local_tax_advisor",
        )
        self.assertIn("tax-applicability-init", current["tax_applicability"]["recommended_command"])
        self.assertEqual(
            current["connector_configuration"]["operator_role"],
            "connector_operator",
        )
        self.assertFalse(result["summary"]["ready_for_external_filing"])

    def test_projection_rejects_stage_drift_and_enforces_dependencies(self):
        contracts = build_activation_stage_contracts()
        readiness = [{
            "stage_order": index,
            "stage_id": stage_id,
            "display_name": stage_id,
            "status": "test",
            "gate_passed": stage_id in {
                "pack_contracts", "tax_rule_sources", "connector_configuration",
                "connector_shadow_evidence",
            },
            "evidence_complete": False,
            "required_evidence": "test evidence",
        } for index, stage_id in enumerate(contracts, start=1)]
        projected = project_activation_stages(readiness, contracts)
        by_id = {item["stage_id"]: item for item in projected}
        self.assertEqual(by_id["tax_applicability"]["work_status"], "ready_to_work")
        self.assertEqual(by_id["pilot_readiness"]["work_status"], "blocked_by_dependency")
        self.assertEqual(by_id["pilot_readiness"]["unmet_dependency_ids"], ["tax_applicability"])
        with self.assertRaisesRegex(ValueError, "contract mismatch"):
            project_activation_stages(readiness[:-1], contracts)

    def test_workspace_is_command_only_and_returns_no_private_state(self):
        secret = "activation-super-secret"
        result = self._workspace(
            "global_game_studio_xero.json",
            {
                "OPC_XERO_ACCESS_TOKEN": secret,
                "OPC_XERO_ENTITY_BINDINGS_JSON": secret,
            },
        )
        serialized = json.dumps(result)
        self.assertNotIn(secret, serialized)
        self.assertFalse(result["control_boundary"]["configured_private_paths_returned"])
        self.assertFalse(result["control_boundary"]["credential_values_returned"])
        self.assertTrue(result["control_boundary"]["commands_are_templates_only"])
        self.assertFalse(result["control_boundary"]["commands_executed"])
        self.assertFalse(result["control_boundary"]["external_actions_performed"])

    def test_every_compiled_command_template_matches_the_real_cli_parser(self):
        parser = build_parser()
        command_names = set()
        for contract in build_activation_stage_contracts().values():
            for command in contract["commands"]:
                argv = shlex.split(command)
                self.assertEqual(argv[0], "opc-finance-box")
                argv = [
                    "dtc.shopify_stripe_daily_close" if item == "PIPELINE_ID" else item
                    for item in argv
                ]
                try:
                    parsed = parser.parse_args(argv[1:])
                except SystemExit as exc:
                    self.fail(f"command template does not parse: {command} ({exc})")
                self.assertEqual(parsed.command, argv[1])
                command_names.add(parsed.command)
        self.assertIn("pilot-shadow-period-archive", command_names)
        self.assertIn("pilot-shadow-next-period-init", command_names)
        self.assertIn("pilot-shadow-next-period-verify", command_names)
        self.assertIn("pilot-shadow-period-runbook-status", command_names)
        self.assertIn("pilot-shadow-period-runbook-verify", command_names)


if __name__ == "__main__":
    unittest.main()
