import json
import tempfile
import unittest
from pathlib import Path

from src.box_runtime import BoxRuntime
from src.default_services import build_default_service_registry
from src.production_readiness import (
    build_production_readiness_plan, build_production_readiness_workspace,
)


ROOT = Path(__file__).resolve().parents[1]


class ProductionReadinessTests(unittest.TestCase):
    def runtime(self) -> BoxRuntime:
        return BoxRuntime(
            ROOT / "examples" / "boxes" / "global_game_studio_xero.json",
            ROOT / "packs",
        )

    def test_plan_is_deterministic_pack_driven_and_never_claims_release(self):
        runtime = self.runtime()
        first = build_production_readiness_plan(runtime)
        second = build_production_readiness_plan(runtime)
        self.assertEqual(first, second)
        self.assertEqual(first["artifact_type"], "production_readiness_plan")
        self.assertEqual(len(first["stages"]), 11)
        self.assertIn(
            "OPC_CONNECTOR_SHADOW_REVIEW_DIR",
            first["private_artifact_environment_names"],
        )
        self.assertIn(
            "OPC_STABLE_PROMOTION_ROOT",
            first["private_artifact_environment_names"],
        )
        self.assertTrue(first["pack_contracts"]["selected_contract_valid"])
        self.assertEqual(first["pack_contracts"]["installed_pack_count"], 36)
        self.assertEqual(first["pack_contracts"]["installed_capability_count"], 114)
        self.assertEqual(
            {item["jurisdiction"] for item in first["tax_entities"]}, {"CN", "SG"},
        )
        self.assertTrue(all(
            item["applicability_review_required"]
            and item["registration_evidence_required"]
            and not item["external_filing_ready"]
            for item in first["tax_entities"]
        ))
        self.assertFalse(first["pack_contracts"]["stable_release_ready"])
        self.assertFalse(first["control_boundary"]["external_filing_authorized"])

    def test_workspace_fails_closed_and_returns_no_private_values_or_paths(self):
        runtime = self.runtime()
        with tempfile.TemporaryDirectory() as temp_dir:
            result = build_production_readiness_workspace(
                runtime,
                build_default_service_registry(),
                runs_root=Path(temp_dir) / "runs",
                environ={},
                as_of="2026-08-14",
            )
            private_root = temp_dir
        self.assertTrue(result["summary"]["ready_for_internal_demo"])
        self.assertFalse(result["summary"]["ready_for_bounded_shadow"])
        self.assertFalse(result["summary"]["ready_for_stable_promotion"])
        self.assertFalse(result["summary"]["ready_for_external_filing"])
        self.assertEqual(result["summary"]["stage_count"], 11)
        stages = {item["stage_id"]: item for item in result["stages"]}
        self.assertEqual(stages["tax_rule_sources"]["status"], "current")
        self.assertTrue(stages["tax_rule_sources"]["gate_passed"])
        self.assertEqual(
            stages["tax_applicability"]["status"], "not_attached_or_not_activated",
        )
        self.assertEqual(
            stages["connector_configuration"]["status"],
            "blocked_missing_credential_reference",
        )
        self.assertEqual(stages["connector_shadow_evidence"]["status"], "missing")
        self.assertFalse(stages["connector_shadow_evidence"]["gate_passed"])
        self.assertEqual(stages["pilot_readiness"]["status"], "missing")
        self.assertEqual(stages["consecutive_shadow_series"]["facts"]["period_count"], 0)
        self.assertEqual(
            stages["stable_promotion"]["status"],
            "promotion_ledger_not_attached",
        )
        self.assertFalse(stages["stable_promotion"]["facts"]["ledger_configured"])
        serialized = json.dumps(result)
        self.assertNotIn(private_root, serialized)
        self.assertFalse(result["control_boundary"]["paths_returned"])
        self.assertFalse(result["control_boundary"]["credential_values_returned"])
        self.assertFalse(result["control_boundary"]["external_actions_performed"])

    def test_credentials_only_advance_connector_gate_not_shadow_evidence(self):
        result = build_production_readiness_workspace(
            self.runtime(),
            build_default_service_registry(),
            runs_root=ROOT / ".test-nonexistent-runs",
            environ={
                "OPC_XERO_ACCESS_TOKEN": "must-never-be-returned",
                "OPC_XERO_ENTITY_BINDINGS_JSON": "must-never-be-returned-either",
            },
            as_of="2026-08-14",
        )
        stage = next(
            item for item in result["stages"]
            if item["stage_id"] == "connector_configuration"
        )
        self.assertEqual(stage["status"], "credentials_ready_shadow_evidence_required")
        self.assertTrue(stage["gate_passed"])
        self.assertFalse(stage["evidence_complete"])
        self.assertFalse(stage["facts"]["shadow_run_performed"])
        serialized = json.dumps(result)
        self.assertNotIn("must-never-be-returned", serialized)
        self.assertFalse(result["summary"]["ready_for_bounded_shadow"])

    def test_configured_empty_and_unsafe_promotion_ledgers_fail_closed_without_writes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve() / "promotion"
            root.mkdir(mode=0o700)
            environment = {"OPC_STABLE_PROMOTION_ROOT": str(root)}
            result = build_production_readiness_workspace(
                self.runtime(),
                build_default_service_registry(),
                runs_root=Path(temp_dir) / "runs",
                environ=environment,
                as_of="2026-08-14",
            )
            stage = next(
                item for item in result["stages"]
                if item["stage_id"] == "stable_promotion"
            )
            self.assertEqual(stage["status"], "promotion_assessment_missing")
            self.assertTrue(stage["facts"]["ledger_integrity_valid"])
            self.assertEqual(list(root.iterdir()), [])
            self.assertFalse(result["summary"]["ready_for_stable_promotion"])

            root.chmod(0o755)
            invalid = build_production_readiness_workspace(
                self.runtime(),
                build_default_service_registry(),
                runs_root=Path(temp_dir) / "runs",
                environ=environment,
                as_of="2026-08-14",
            )
            invalid_stage = next(
                item for item in invalid["stages"]
                if item["stage_id"] == "stable_promotion"
            )
            self.assertEqual(invalid_stage["status"], "promotion_ledger_invalid")
            self.assertFalse(invalid_stage["facts"]["ledger_integrity_valid"])
            self.assertNotIn(str(root), json.dumps(invalid))


if __name__ == "__main__":
    unittest.main()
