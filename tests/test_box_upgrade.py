import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from src.box_compiler import compile_box_file, write_compiled_box
from src.box_upgrade import BoxUpgradeError, compare_compiled_box


ROOT = Path(__file__).resolve().parents[1]
PACKS = ROOT / "packs"


class BoxUpgradeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.current = compile_box_file(ROOT / "examples" / "boxes" / "global_game_studio.json", PACKS)

    def test_identical_compiled_contract_is_compatible_and_unchanged(self):
        result = compare_compiled_box(self.current, deepcopy(self.current))
        self.assertTrue(result["compatible"])
        self.assertTrue(result["unchanged"])
        self.assertFalse(result["requires_review"])

    def test_service_control_change_is_blocking(self):
        baseline = deepcopy(self.current)
        service = next(item for item in baseline["services"] if item["service_id"] == "core.close_readiness")
        service["review_gate"] = None
        result = compare_compiled_box(baseline, self.current)
        self.assertFalse(result["compatible"])
        change = next(item for item in result["changes"] if item["category"] == "service_contract_changed")
        self.assertEqual(change["field"], "review_gate")

    def test_pack_version_and_registration_change_require_review(self):
        baseline = deepcopy(self.current)
        next(item for item in baseline["lock"]["packs"] if item["id"] == "industry.game_studio")["version"] = "0.0.9"
        baseline["entities"][0]["tax_registrations"] = []
        result = compare_compiled_box(baseline, self.current)
        self.assertTrue(result["compatible"])
        self.assertFalse(result["unchanged"])
        self.assertGreaterEqual(result["counts"]["review"], 2)

    def test_removed_entity_is_blocking(self):
        baseline = deepcopy(self.current)
        current = deepcopy(self.current)
        current["entities"] = current["entities"][:1]
        result = compare_compiled_box(baseline, current)
        self.assertFalse(result["compatible"])
        self.assertIn("sg_publisher", {item["id"] for item in result["changes"]})

    def test_pipeline_control_change_is_blocking(self):
        baseline = compile_box_file(
            ROOT / "examples" / "boxes" / "cn_dtc_stripe_store.json", PACKS,
        )
        current = deepcopy(baseline)
        pipeline = next(item for item in current["pipelines"] if item["pipeline_id"] == "stripe.daily_close")
        pipeline["external_actions"] = True
        result = compare_compiled_box(baseline, current)
        self.assertFalse(result["compatible"])
        change = next(item for item in result["changes"] if item["category"] == "pipeline_contract_changed")
        self.assertEqual(change["field"], "external_actions")

    def test_legacy_connector_without_scope_normalizes_to_all_entities(self):
        baseline = deepcopy(self.current)
        for connector in baseline["connectors"]:
            connector.pop("entity_ids", None)
        result = compare_compiled_box(baseline, self.current)
        self.assertTrue(result["unchanged"], result)

    def test_reducing_connector_entity_binding_is_blocking(self):
        baseline = deepcopy(self.current)
        current = deepcopy(self.current)
        connector = next(
            item for item in current["connectors"]
            if item["connector_id"] == "file.bank_statement"
        )
        connector["entity_ids"] = ["cn_studio"]
        result = compare_compiled_box(baseline, current)
        self.assertFalse(result["compatible"])
        change = next(
            item for item in result["changes"]
            if item["category"] == "connector_entity_binding_reduced"
        )
        self.assertEqual(change["removed_entity_ids"], ["sg_publisher"])

    def test_game_pipeline_review_gate_change_is_blocking(self):
        baseline = deepcopy(self.current)
        current = deepcopy(self.current)
        pipeline = next(
            item for item in current["pipelines"]
            if item["pipeline_id"] == "game.channel_settlement_close"
        )
        pipeline["review_gates"] = ["channel_contract_mapping"]
        result = compare_compiled_box(baseline, current)
        self.assertFalse(result["compatible"])
        change = next(
            item for item in result["changes"]
            if item["category"] == "pipeline_contract_changed"
            and item["id"] == "game.channel_settlement_close"
        )
        self.assertEqual(change["field"], "review_gates")

    def test_marketplace_inventory_gate_removal_is_blocking(self):
        baseline = compile_box_file(
            ROOT / "examples" / "boxes" / "cn_marketplace_store.json", PACKS,
        )
        current = deepcopy(baseline)
        pipeline = next(
            item for item in current["pipelines"]
            if item["pipeline_id"] == "marketplace.channel_close"
        )
        pipeline["review_gates"].remove("marketplace_inventory_mapping")
        result = compare_compiled_box(baseline, current)
        self.assertFalse(result["compatible"])
        change = next(
            item for item in result["changes"]
            if item["category"] == "pipeline_contract_changed"
            and item["id"] == "marketplace.channel_close"
        )
        self.assertEqual(change["field"], "review_gates")

    def test_baseline_without_pipeline_catalog_treats_new_pipeline_as_review(self):
        current = compile_box_file(ROOT / "examples" / "boxes" / "cn_dtc_store.json", PACKS)
        baseline = deepcopy(current)
        baseline.pop("pipelines", None)
        result = compare_compiled_box(baseline, current)
        self.assertTrue(result["compatible"])
        self.assertTrue(any(item["category"] == "pipeline_added" for item in result["changes"]))

    def test_pipeline_review_policy_control_change_is_blocking(self):
        baseline = deepcopy(self.current)
        current = deepcopy(self.current)
        current["pipeline_run_policy"]["release_candidate_is_external_authorization"] = True
        result = compare_compiled_box(baseline, current)
        self.assertFalse(result["compatible"])
        change = next(
            item for item in result["changes"]
            if item["category"] == "pipeline_run_policy_changed"
        )
        self.assertEqual(change["field"], "release_candidate_is_external_authorization")

    def test_runtime_security_policy_control_change_is_blocking(self):
        baseline = deepcopy(self.current)
        current = deepcopy(self.current)
        current["runtime_security_policy"]["request_actor_override_allowed_when_authenticated"] = True
        result = compare_compiled_box(baseline, current)
        self.assertFalse(result["compatible"])
        change = next(
            item for item in result["changes"]
            if item["category"] == "runtime_security_policy_changed"
        )
        self.assertEqual(change["field"], "request_actor_override_allowed_when_authenticated")

    def test_stable_promotion_control_change_is_blocking(self):
        baseline = deepcopy(self.current)
        current = deepcopy(self.current)
        current["stable_promotion_policy"]["minimum_controls"]["shadow_close"][
            "minimum_match_rate"
        ] = 0.95
        result = compare_compiled_box(baseline, current)
        self.assertFalse(result["compatible"])
        change = next(
            item for item in result["changes"]
            if item["category"] == "stable_promotion_policy_changed"
        )
        self.assertEqual(change["field"], "minimum_controls")

    def test_stable_promotion_evidence_schema_change_is_blocking(self):
        baseline = deepcopy(self.current)
        current = deepcopy(self.current)
        current["stable_promotion_evidence_schema"]["additionalProperties"] = True
        result = compare_compiled_box(baseline, current)
        self.assertFalse(result["compatible"])
        self.assertIn(
            "stable_promotion_evidence_schema_changed",
            {item["category"] for item in result["changes"]},
        )

    def test_tax_applicability_review_contract_addition_and_change_are_audited(self):
        baseline = deepcopy(self.current)
        baseline.pop("tax_applicability_artifact_schema")
        added = compare_compiled_box(baseline, self.current)
        self.assertTrue(added["compatible"])
        self.assertIn(
            "tax_applicability_review_contract_added",
            {item["category"] for item in added["changes"]},
        )
        changed_current = deepcopy(self.current)
        changed_current["tax_applicability_artifact_schema"]["title"] = "Changed"
        changed = compare_compiled_box(self.current, changed_current)
        self.assertFalse(changed["compatible"])
        self.assertIn(
            "tax_applicability_review_contract_changed",
            {item["category"] for item in changed["changes"]},
        )

    def test_tax_applicability_artifact_security_addition_and_change_are_audited(self):
        baseline = deepcopy(self.current)
        baseline.pop("tax_applicability_artifact_security_policy")
        added = compare_compiled_box(baseline, self.current)
        self.assertTrue(added["compatible"])
        self.assertIn(
            "tax_applicability_artifact_security_policy_added",
            {item["category"] for item in added["changes"]},
        )
        changed_current = deepcopy(self.current)
        changed_current["tax_applicability_artifact_security_policy"]["read_policy"][
            "symbolic_links_allowed"
        ] = True
        changed = compare_compiled_box(self.current, changed_current)
        self.assertFalse(changed["compatible"])
        self.assertIn(
            "tax_applicability_artifact_security_policy_changed",
            {item["category"] for item in changed["changes"]},
        )

    def test_tax_applicability_registry_receipt_schema_is_upgrade_audited(self):
        baseline = deepcopy(self.current)
        baseline.pop("tax_applicability_registry_receipt_schema")
        added = compare_compiled_box(baseline, self.current)
        self.assertTrue(added["compatible"])
        self.assertIn(
            "tax_applicability_registry_receipt_schema_added",
            {item["category"] for item in added["changes"]},
        )
        changed_current = deepcopy(self.current)
        changed_current["tax_applicability_registry_receipt_schema"]["title"] = (
            "Changed"
        )
        changed = compare_compiled_box(self.current, changed_current)
        self.assertFalse(changed["compatible"])
        self.assertIn(
            "tax_applicability_registry_receipt_schema_changed",
            {item["category"] for item in changed["changes"]},
        )

    def test_pilot_readiness_plan_and_schema_are_upgrade_audited(self):
        baseline = deepcopy(self.current)
        baseline.pop("pilot_readiness_plan")
        baseline.pop("pilot_readiness_artifact_schema")
        added = compare_compiled_box(baseline, self.current)
        self.assertTrue(added["compatible"])
        self.assertTrue({
            "pilot_readiness_plan_added",
            "pilot_readiness_artifact_schema_added",
        } <= {item["category"] for item in added["changes"]})

        changed_current = deepcopy(self.current)
        changed_current["pilot_readiness_plan"]["entity_ids"] = ["cn_studio"]
        changed = compare_compiled_box(self.current, changed_current)
        self.assertFalse(changed["compatible"])
        self.assertIn(
            "pilot_readiness_plan_changed",
            {item["category"] for item in changed["changes"]},
        )

    def test_production_readiness_activation_contract_is_upgrade_audited(self):
        baseline = deepcopy(self.current)
        baseline.pop("production_readiness_plan")
        added = compare_compiled_box(baseline, self.current)
        self.assertTrue(added["compatible"])
        self.assertIn(
            "production_readiness_plan_added",
            {item["category"] for item in added["changes"]},
        )

        changed_current = deepcopy(self.current)
        changed_current["production_readiness_plan"]["stages"][2][
            "operator_contract"
        ]["operator_role"] = "changed_role"
        changed = compare_compiled_box(self.current, changed_current)
        self.assertFalse(changed["compatible"])
        self.assertIn(
            "production_readiness_plan_changed",
            {item["category"] for item in changed["changes"]},
        )

        workspace_changed = deepcopy(self.current)
        workspace_changed["production_readiness_plan"][
            "first_customer_workspace"
        ]["credentials_accepted"] = True
        changed = compare_compiled_box(self.current, workspace_changed)
        self.assertFalse(changed["compatible"])
        self.assertIn(
            "production_readiness_plan_changed",
            {item["category"] for item in changed["changes"]},
        )

    def test_pilot_data_handoff_plan_and_schema_are_upgrade_audited(self):
        baseline = deepcopy(self.current)
        baseline.pop("pilot_data_handoff_plan")
        baseline.pop("pilot_data_handoff_artifact_schema")
        added = compare_compiled_box(baseline, self.current)
        self.assertTrue(added["compatible"])
        self.assertTrue({
            "pilot_data_handoff_plan_added",
            "pilot_data_handoff_artifact_schema_added",
        } <= {item["category"] for item in added["changes"]})

        changed_current = deepcopy(self.current)
        changed_current["pilot_data_handoff_plan"]["transfer_modes"] = [
            "local_only"
        ]
        changed = compare_compiled_box(self.current, changed_current)
        self.assertFalse(changed["compatible"])
        self.assertIn(
            "pilot_data_handoff_plan_changed",
            {item["category"] for item in changed["changes"]},
        )

    def test_pilot_shadow_run_registration_schema_is_upgrade_audited(self):
        baseline = deepcopy(self.current)
        baseline.pop("pilot_shadow_run_registration_schema")
        added = compare_compiled_box(baseline, self.current)
        self.assertTrue(added["compatible"])
        self.assertIn(
            "pilot_shadow_run_registration_schema_added",
            {item["category"] for item in added["changes"]},
        )

        changed_current = deepcopy(self.current)
        changed_current["pilot_shadow_run_registration_schema"]["properties"][
            "posting_authorized"
        ] = {"const": True}
        changed = compare_compiled_box(self.current, changed_current)
        self.assertFalse(changed["compatible"])
        self.assertIn(
            "pilot_shadow_run_registration_schema_changed",
            {item["category"] for item in changed["changes"]},
        )

    def test_pilot_shadow_observation_schema_is_upgrade_audited(self):
        baseline = deepcopy(self.current)
        baseline.pop("pilot_shadow_observation_artifact_schema")
        added = compare_compiled_box(baseline, self.current)
        self.assertTrue(added["compatible"])
        self.assertIn(
            "pilot_shadow_observation_artifact_schema_added",
            {item["category"] for item in added["changes"]},
        )

        changed_current = deepcopy(self.current)
        changed_current["pilot_shadow_observation_artifact_schema"]["properties"][
            "posting_performed"
        ] = {"const": True}
        changed = compare_compiled_box(self.current, changed_current)
        self.assertFalse(changed["compatible"])
        self.assertIn(
            "pilot_shadow_observation_artifact_schema_changed",
            {item["category"] for item in changed["changes"]},
        )

    def test_pilot_shadow_series_schema_is_upgrade_audited(self):
        baseline = deepcopy(self.current)
        baseline.pop("pilot_shadow_series_artifact_schema")
        added = compare_compiled_box(baseline, self.current)
        self.assertTrue(added["compatible"])
        self.assertIn(
            "pilot_shadow_series_artifact_schema_added",
            {item["category"] for item in added["changes"]},
        )

        changed_current = deepcopy(self.current)
        changed_current["pilot_shadow_series_artifact_schema"]["properties"][
            "posting_performed"
        ] = {"const": True}
        changed = compare_compiled_box(self.current, changed_current)
        self.assertFalse(changed["compatible"])
        self.assertIn(
            "pilot_shadow_series_artifact_schema_changed",
            {item["category"] for item in changed["changes"]},
        )

    def test_cfo_control_overlay_is_upgrade_audited(self):
        baseline = deepcopy(self.current)
        baseline.pop("cfo_control_overlay")
        added = compare_compiled_box(baseline, self.current)
        self.assertTrue(added["compatible"])
        self.assertIn(
            "cfo_control_overlay_added",
            {item["category"] for item in added["changes"]},
        )

        changed_current = deepcopy(self.current)
        changed_current["cfo_control_overlay"][
            "monthly_control_objective_type_ids"
        ].append("unreviewed_control")
        changed = compare_compiled_box(self.current, changed_current)
        self.assertFalse(changed["compatible"])
        self.assertIn(
            "cfo_control_overlay_changed",
            {item["category"] for item in changed["changes"]},
        )

    def test_cfo_metric_catalog_is_upgrade_audited(self):
        baseline = deepcopy(self.current)
        baseline.pop("cfo_metric_catalog")
        added = compare_compiled_box(baseline, self.current)
        self.assertTrue(added["compatible"])
        self.assertIn(
            "cfo_metric_catalog_added",
            {item["category"] for item in added["changes"]},
        )

        changed_current = deepcopy(self.current)
        changed_current["cfo_metric_catalog"]["metric_definitions"][0][
            "formula"
        ]["missing_operand_policy"] = "assume_zero"
        changed = compare_compiled_box(self.current, changed_current)
        self.assertFalse(changed["compatible"])
        self.assertIn(
            "cfo_metric_catalog_changed",
            {item["category"] for item in changed["changes"]},
        )

    def test_tax_applicability_review_policy_addition_and_change_are_audited(self):
        baseline = deepcopy(self.current)
        for entity in baseline["jurisdiction_rules"]["entities"]:
            entity.pop("applicability_review_policy")
        added = compare_compiled_box(baseline, self.current)
        self.assertTrue(added["compatible"])
        self.assertEqual(
            sum(
                item["category"] == "tax_applicability_review_policy_added"
                for item in added["changes"]
            ),
            2,
        )
        changed_current = deepcopy(self.current)
        changed_current["jurisdiction_rules"]["entities"][0][
            "applicability_review_policy"
        ]["max_age_days"] = 180
        changed = compare_compiled_box(self.current, changed_current)
        self.assertFalse(changed["compatible"])
        self.assertIn(
            "tax_applicability_review_policy_changed",
            {item["category"] for item in changed["changes"]},
        )

    def test_additive_runtime_layout_change_requires_migration_review(self):
        baseline = deepcopy(self.current)
        baseline["runtime_data_contract"]["layout"]["current_version"] = 2
        baseline["runtime_data_contract"]["layout"]["stores"].pop("release_promotion")
        result = compare_compiled_box(baseline, self.current)
        self.assertTrue(result["compatible"])
        change = next(
            item for item in result["changes"]
            if item["category"] == "runtime_data_layout_changed"
        )
        self.assertEqual(change["added_stores"], ["release_promotion"])

    def test_writer_emits_upgrade_policy(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = write_compiled_box(self.current, temp_dir)
            self.assertIn("upgrade-policy.json", {path.name for path in paths})
            policy = json.loads((Path(temp_dir) / "upgrade-policy.json").read_text(encoding="utf-8"))
            self.assertEqual(policy["baseline_fingerprint"], self.current["lock"]["runtime_fingerprint"])

    def test_rejects_unknown_compiled_schema(self):
        with self.assertRaises(BoxUpgradeError):
            compare_compiled_box({"schema_version": 2}, self.current)


if __name__ == "__main__":
    unittest.main()
