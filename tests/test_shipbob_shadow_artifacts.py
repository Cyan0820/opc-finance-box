from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.box_pipeline import dispatch_box_pipeline_request
from src.box_runtime import BoxRuntime
from src.connector_shadow_artifacts import (
    ConnectorShadowArtifactError,
    assess_connector_shadow_artifacts,
    build_connector_shadow_baseline_plan,
    build_connector_shadow_baseline_workpaper,
    finalize_connector_shadow_baseline_workpaper,
    write_shipbob_shadow_observation,
)
from src.connector_shadow_registry import build_connector_shadow_registry_workspace


ROOT = Path(__file__).resolve().parents[1]
BOX = ROOT / "examples" / "boxes" / "us_dtc_shopify_stripe_shipbob_c_corp.json"
REQUEST = ROOT / "examples" / "pipelines" / "shipbob_fulfillment_close_fixture.json"


class ShipBobShadowArtifactTests(unittest.TestCase):
    def setUp(self):
        self.runtime = BoxRuntime(BOX, ROOT / "packs")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def _real_baseline(self) -> Path:
        workpaper_path = Path(self.temp.name) / "shipbob-workpaper.json"
        created = build_connector_shadow_baseline_workpaper(
            self.runtime,
            pipeline_id="commerce.shipbob_fulfillment_close",
            entity_id="us_dtc_company",
            sample_period="2026-08",
            prepared_by="independent-3pl-source-preparer",
            output=workpaper_path,
        )
        self.assertEqual(created["source_count"], 1)
        self.assertEqual(created["control_count"], 15)
        workpaper = json.loads(workpaper_path.read_text(encoding="utf-8"))
        workpaper["source_independence"] = {
            "prepared_from_independent_source": True,
            "pipeline_output_used_as_baseline": False,
            "source_scope_confirmed": True,
        }
        workpaper["anonymization"]["private_source_evidence_retained"] = True
        workpaper["source_expectations"][0]["expected_record_count"] = 4
        workpaper["source_expectations"][0]["evidence_references"] = [
            "private-export://shipbob/us-dtc/2026-08/source-counts",
        ]
        expected_controls = {
            "pipeline_ready": True,
            "order_count": 1,
            "shipment_count": 1,
            "return_count": 1,
            "return_item_count": 1,
            "unfulfilled_order_count": 0,
            "unprocessed_return_item_count": 0,
            "cross_window_return_reference_count": 0,
            "network_fulfillment_read_performed": True,
            "monthly_half_open_window": True,
            "entity_scope_matched": True,
            "customer_pii_excluded": True,
            "raw_source_ids_excluded": True,
            "write_api_disabled": True,
            "posting_and_inventory_actions_disabled": True,
        }
        for item in workpaper["control_expectations"]:
            item["expected_value"] = expected_controls[item["control_id"]]
        workpaper["evidence_references"] = [
            "workpaper://shipbob/us-dtc/2026-08/source-scope",
        ]
        workpaper["finalization_ready"] = True
        workpaper_path.write_text(json.dumps(workpaper), encoding="utf-8")
        baseline_path = Path(self.temp.name) / "shipbob-real-baseline.json"
        finalized = finalize_connector_shadow_baseline_workpaper(
            self.runtime, workpaper_path, baseline_path,
        )
        self.assertTrue(finalized["real_sample_evidence"])
        return baseline_path

    def test_plan_and_registry_support_shipbob_instead_of_permanent_unsupported_state(self):
        plan = build_connector_shadow_baseline_plan(self.runtime)
        profile = next(
            item for item in plan["profiles"]
            if item["pipeline_id"] == "commerce.shipbob_fulfillment_close"
        )
        self.assertEqual(profile["covered_pack_ids"], ["connector.shipbob"])
        self.assertEqual(profile["source_connector_ids"], ["shipbob.fulfillment"])
        self.assertEqual(profile["entity_ids"], ["us_dtc_company"])
        registry = build_connector_shadow_registry_workspace(
            self.runtime, None, as_of="2026-08-15",
        )
        coverage = next(
            item for item in registry["pack_coverage"]
            if item["pack_id"] == "connector.shipbob"
        )
        self.assertEqual(coverage["status"], "missing_current_evidence")

    def test_real_baseline_rejects_fixture_but_accepts_equivalent_live_safe_controls(self):
        baseline_path = self._real_baseline()
        request = json.loads(REQUEST.read_text(encoding="utf-8"))
        result = dispatch_box_pipeline_request(self.runtime, request)
        fixture_path = Path(self.temp.name) / "fixture-result.json"
        fixture_path.write_text(json.dumps(result), encoding="utf-8")
        fixture_summary = assess_connector_shadow_artifacts(
            self.runtime, baseline_path, fixture_path,
            Path(self.temp.name) / "fixture-assessment.json",
        )
        self.assertFalse(fixture_summary["passed"])

        live_result = json.loads(json.dumps(result))
        live_result["network_access_performed"] = True
        source = live_result["connector_batches"]["shipbob.fulfillment"]["source"]
        source.update({
            "kind": "api",
            "environment": "production",
            "network_access_performed": True,
        })
        live_path = Path(self.temp.name) / "live-result.json"
        live_path.write_text(json.dumps(live_result), encoding="utf-8")
        assessment_path = Path(self.temp.name) / "live-assessment.json"
        live_summary = assess_connector_shadow_artifacts(
            self.runtime, baseline_path, live_path, assessment_path,
        )
        self.assertTrue(live_summary["passed"], json.loads(assessment_path.read_text()))
        assessment = json.loads(assessment_path.read_text(encoding="utf-8"))
        serialized = json.dumps(assessment, ensure_ascii=False).lower()
        for forbidden in (
            "7.77", "3.25", "private-tracking-001",
            "fixture@example.invalid", "sku-tshirt-blue",
        ):
            self.assertNotIn(forbidden, serialized)
        self.assertFalse(assessment["financial_amounts_included"])
        self.assertFalse(assessment["raw_source_values_included"])

    def _live_result(self) -> dict:
        request = json.loads(REQUEST.read_text(encoding="utf-8"))
        result = dispatch_box_pipeline_request(self.runtime, request)
        result["network_access_performed"] = True
        source = result["connector_batches"]["shipbob.fulfillment"]["source"]
        source.update({
            "kind": "api",
            "environment": "production",
            "network_access_performed": True,
            "rate_limit_count": 0,
            "retry_delay_seconds_total": 0.0,
            "retry_after_honored": True,
        })
        return result

    def test_safe_observation_excludes_amounts_merchant_and_inventory_values_then_assesses(self):
        baseline_path = self._real_baseline()
        observation_path = Path(self.temp.name) / "shipbob-safe-observation.json"
        summary = write_shipbob_shadow_observation(
            self.runtime, self._live_result(), observation_path,
        )
        self.assertEqual(summary["order_count"], 1)
        self.assertEqual(summary["shipment_count"], 1)
        self.assertEqual(summary["return_count"], 1)
        self.assertEqual(summary["return_item_count"], 1)
        self.assertTrue(summary["pipeline_ready"])
        observation = json.loads(observation_path.read_text(encoding="utf-8"))
        serialized = json.dumps(observation, ensure_ascii=False).lower()
        for forbidden in (
            "7.77", "3.25", "private-tracking-001", "fixture@example.invalid",
            "sku-tshirt-blue", "us-west-3pl", "restock", "delivered",
        ):
            self.assertNotIn(forbidden, serialized)
        self.assertFalse(observation["raw_source_ids_included"])
        self.assertFalse(observation["merchant_account_values_included"])
        self.assertFalse(observation["customer_or_inventory_values_included"])
        self.assertFalse(observation["financial_amounts_included"])
        assessment_path = Path(self.temp.name) / "shipbob-safe-assessment.json"
        assessed = assess_connector_shadow_artifacts(
            self.runtime, baseline_path, observation_path, assessment_path,
        )
        self.assertTrue(assessed["passed"], json.loads(
            assessment_path.read_text(encoding="utf-8")
        ))

    def test_observation_rejects_fixture_dirty_batch_and_tamper(self):
        request = json.loads(REQUEST.read_text(encoding="utf-8"))
        fixture = dispatch_box_pipeline_request(self.runtime, request)
        with self.assertRaisesRegex(ConnectorShadowArtifactError, "real minimized"):
            write_shipbob_shadow_observation(
                self.runtime, fixture, Path(self.temp.name) / "never-fixture.json",
            )
        dirty = self._live_result()
        dirty["connector_batches"]["shipbob.fulfillment"]["quality"][
            "rejected_count"
        ] = 1
        with self.assertRaisesRegex(ConnectorShadowArtifactError, "clean non-empty"):
            write_shipbob_shadow_observation(
                self.runtime, dirty, Path(self.temp.name) / "never-dirty.json",
            )

        baseline_path = self._real_baseline()
        observation_path = Path(self.temp.name) / "shipbob-safe.json"
        write_shipbob_shadow_observation(
            self.runtime, self._live_result(), observation_path,
        )
        observation = json.loads(observation_path.read_text(encoding="utf-8"))
        observation["financial_amounts_included"] = True
        tampered_path = Path(self.temp.name) / "shipbob-tampered.json"
        tampered_path.write_text(json.dumps(observation), encoding="utf-8")
        with self.assertRaisesRegex(
            ConnectorShadowArtifactError, "integrity or privacy",
        ):
            assess_connector_shadow_artifacts(
                self.runtime, baseline_path, tampered_path,
                Path(self.temp.name) / "never-assessment.json",
            )


if __name__ == "__main__":
    unittest.main()
