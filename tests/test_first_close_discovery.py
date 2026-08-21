from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from src.box_compiler import preflight_pipeline_request
from src.box_pipeline import BoxPipelineError, dispatch_box_pipeline_request
from src.box_runtime import BoxRuntime
from src.default_services import build_default_service_registry


ROOT = Path(__file__).resolve().parents[1]


class FirstCloseDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.runtime = BoxRuntime(
            ROOT / "examples" / "boxes" / "cn_dtc_store.json", ROOT / "packs",
        )
        self.request = json.loads(
            (ROOT / "examples" / "pipelines" / "first_close_discovery_fixture.json").read_text(
                encoding="utf-8"
            )
        )

    def test_discovers_exact_sources_and_stable_fail_closed_starters(self):
        first = dispatch_box_pipeline_request(self.runtime, self.request)
        second = dispatch_box_pipeline_request(self.runtime, self.request)
        self.assertTrue(first["ready"], first)
        self.assertEqual(first["pipeline"]["run_id"], second["pipeline"]["run_id"])
        self.assertEqual(first["pipeline"]["required_review_gates"], [
            "bank_statement_mapping_review", "accounting_export_mapping_review",
            "trial_balance_control_total_review", "first_close_configuration_review",
        ])
        output = first["services"]["first_close_configuration_discovery"]["output"]
        self.assertEqual(len(output["bank_account_inventory"]), 1)
        self.assertEqual(len(output["account_inventory"]), 4)
        self.assertTrue(all(
            item["matched"] and item["account_names_consistent"]
            for item in output["ledger_trial_balance_movement_reconciliation"]
        ))
        self.assertFalse(output["account_classification_inferred"])
        self.assertFalse(output["bank_gl_mapping_inferred"])
        self.assertFalse(output["transaction_matching_performed"])
        starter = first["configuration_starter"]["next_request"]
        self.assertEqual(starter["pipeline_id"], "finance.month_close_control")
        self.assertEqual(
            starter["payload"]["bank_gl_mappings"][0]["bank_source_fingerprint"],
            output["bank_account_inventory"][0]["bank_source_fingerprint"],
        )
        self.assertTrue(starter["payload"]["account_mappings"][0]["statement_group"].startswith("REPLACE_"))
        self.assertTrue(starter["payload"]["bank_gl_mappings"][0]["gl_account_code"].startswith("REPLACE_"))
        self.assertFalse(first["ledger_modified"])
        self.assertFalse(first["posting_performed"])
        self.assertFalse(first["period_close_performed"])

    def test_generated_next_request_is_intentionally_blocked_by_preflight(self):
        result = dispatch_box_pipeline_request(self.runtime, self.request)
        next_request = result["configuration_starter"]["next_request"]
        preflight = preflight_pipeline_request(self.runtime, next_request)
        self.assertFalse(preflight["ready_to_dispatch"])
        self.assertIn("request still contains fail-closed placeholders", preflight["blockers"])
        self.assertTrue(preflight["placeholder_paths"])
        self.assertEqual(preflight["forbidden_secret_paths"], [])
        self.assertFalse(preflight["dispatch_performed"])

    def test_preflight_rejects_wrong_named_connector_without_source_access(self):
        request = copy.deepcopy(self.request)
        request["payload"]["bank_connector_id"] = "file.general_ledger"
        result = preflight_pipeline_request(self.runtime, request)
        self.assertFalse(result["ready_to_dispatch"])
        self.assertIn(
            "payload.bank_connector_id is not allowed by this Pipeline", result["blockers"],
        )
        self.assertFalse(result["source_access_performed"])

    def test_preflight_requires_all_three_named_sources(self):
        request = copy.deepcopy(self.request)
        del request["payload"]["trial_balance_connector_id"]
        del request["payload"]["trial_balance_connector_request"]
        result = preflight_pipeline_request(self.runtime, request)
        self.assertFalse(result["ready_to_dispatch"])
        self.assertIn("payload.trial_balance_connector_id is required", result["blockers"])
        self.assertIn(
            "payload.trial_balance_connector_request must be a JSON object", result["blockers"],
        )

    def test_gl_trial_difference_stops_before_configuration_review(self):
        with tempfile.TemporaryDirectory() as folder:
            source = (ROOT / "examples" / "accounting" / "month_close_trial_balance.csv").read_text(
                encoding="utf-8"
            )
            path = Path(folder) / "trial.csv"
            path.write_text(
                source.replace(
                    "6602,管理费用,0,0,120.5,0,120.5,0",
                    "6602,管理费用,0,0,121.5,0,120.5,0",
                ),
                encoding="utf-8",
            )
            request = copy.deepcopy(self.request)
            request["payload"]["trial_balance_connector_request"]["path"] = str(path)
            result = dispatch_box_pipeline_request(self.runtime, request)
        self.assertFalse(result["ready"])
        self.assertEqual(result["blocked_at"], "source_discovery")
        issues = result["services"]["first_close_configuration_discovery"]["output"]["issues"]
        self.assertIn("ledger_trial_balance_mismatch", {item["type"] for item in issues})

    def test_cross_entity_connector_override_fails_before_source_access(self):
        request = copy.deepcopy(self.request)
        request["payload"]["bank_connector_request"]["default_entity_id"] = "other"
        with self.assertRaisesRegex(BoxPipelineError, "does not match"):
            dispatch_box_pipeline_request(self.runtime, request)

    def test_service_is_statutory_read_only_and_review_gated(self):
        pipeline = dispatch_box_pipeline_request(self.runtime, self.request)
        discovered = pipeline["services"]["first_close_configuration_discovery"]
        self.assertEqual(discovered["service"]["action_class"], "read")
        self.assertEqual(discovered["service"]["entity_ids"], ["cn_dtc_company"])
        definition = next(
            item for item in build_default_service_registry().catalog(self.runtime)
            if item["service_id"] == "core.discover_first_close_configuration"
        )
        self.assertEqual(definition["review_gate"], "first_close_configuration_review")


if __name__ == "__main__":
    unittest.main()
