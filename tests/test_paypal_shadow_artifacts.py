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
    write_paypal_shadow_observation,
)
from src.connector_shadow_registry import build_connector_shadow_registry_workspace


ROOT = Path(__file__).resolve().parents[1]
BOX = ROOT / "examples" / "boxes" / "us_dtc_paypal_c_corp.json"
REQUEST = ROOT / "examples" / "pipelines" / "paypal_transaction_close_fixture.json"


class PayPalShadowArtifactTests(unittest.TestCase):
    def setUp(self):
        self.runtime = BoxRuntime(BOX, ROOT / "packs")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def _real_baseline(self) -> Path:
        workpaper_path = Path(self.temp.name) / "paypal-workpaper.json"
        created = build_connector_shadow_baseline_workpaper(
            self.runtime,
            pipeline_id="paypal.transaction_close",
            entity_id="us_dtc_company",
            sample_period="2026-08",
            prepared_by="independent-paypal-source-preparer",
            output=workpaper_path,
        )
        self.assertEqual(created["source_count"], 1)
        self.assertEqual(created["control_count"], 14)
        workpaper = json.loads(workpaper_path.read_text(encoding="utf-8"))
        workpaper["source_independence"] = {
            "prepared_from_independent_source": True,
            "pipeline_output_used_as_baseline": False,
            "source_scope_confirmed": True,
        }
        workpaper["anonymization"]["private_source_evidence_retained"] = True
        workpaper["source_expectations"][0]["expected_record_count"] = 3
        workpaper["source_expectations"][0]["evidence_references"] = [
            "private-export://paypal/us-dtc/2026-08/source-counts",
        ]
        expected_controls = {
            "pipeline_ready": True,
            "transaction_count": 3,
            "refund_candidate_count": 1,
            "reversal_candidate_count": 0,
            "reference_review_required_count": 0,
            "cross_currency_fee_count": 0,
            "network_transaction_search_performed": True,
            "oauth_exchange_in_memory": True,
            "monthly_half_open_window": True,
            "entity_scope_matched": True,
            "transaction_info_only": True,
            "customer_pii_and_free_text_excluded": True,
            "raw_source_ids_excluded": True,
            "business_write_posting_actions_disabled": True,
        }
        for item in workpaper["control_expectations"]:
            item["expected_value"] = expected_controls[item["control_id"]]
        workpaper["evidence_references"] = [
            "workpaper://paypal/us-dtc/2026-08/source-scope",
        ]
        workpaper["finalization_ready"] = True
        workpaper_path.write_text(json.dumps(workpaper), encoding="utf-8")
        baseline_path = Path(self.temp.name) / "paypal-real-baseline.json"
        finalized = finalize_connector_shadow_baseline_workpaper(
            self.runtime, workpaper_path, baseline_path,
        )
        self.assertTrue(finalized["real_sample_evidence"])
        return baseline_path

    def test_plan_and_registry_support_paypal_as_a_real_profile(self):
        plan = build_connector_shadow_baseline_plan(self.runtime)
        profile = next(
            item for item in plan["profiles"]
            if item["pipeline_id"] == "paypal.transaction_close"
        )
        self.assertEqual(profile["covered_pack_ids"], ["connector.paypal"])
        self.assertEqual(profile["source_connector_ids"], ["paypal.transaction_activity"])
        self.assertEqual(profile["entity_ids"], ["us_dtc_company"])
        registry = build_connector_shadow_registry_workspace(
            self.runtime, None, as_of="2026-08-15",
        )
        coverage = next(
            item for item in registry["pack_coverage"]
            if item["pack_id"] == "connector.paypal"
        )
        self.assertEqual(coverage["status"], "missing_current_evidence")

    def test_real_baseline_rejects_fixture_and_accepts_equivalent_live_safe_controls(self):
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
        source = live_result["connector_batches"]["paypal.transaction_activity"]["source"]
        source.update({
            "kind": "api",
            "environment": "production",
            "network_access_performed": True,
            "oauth_token_exchange_performed": True,
            "oauth_token_persisted": False,
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
            "96.51", "25.00", "paypal-demo-sale-001", "fixture@example.invalid",
        ):
            self.assertNotIn(forbidden, serialized)
        self.assertFalse(assessment["financial_amounts_included"])
        self.assertFalse(assessment["raw_source_values_included"])

    def test_safe_observation_excludes_amounts_customers_and_raw_ids_then_assesses(self):
        baseline_path = self._real_baseline()
        request = json.loads(REQUEST.read_text(encoding="utf-8"))
        result = dispatch_box_pipeline_request(self.runtime, request)
        result["network_access_performed"] = True
        source = result["connector_batches"]["paypal.transaction_activity"]["source"]
        source.update({
            "kind": "api",
            "environment": "production",
            "api_end_inclusive": "2026-08-31T23:59:59.999999Z",
            "total_items": 3,
            "network_access_performed": True,
            "oauth_token_exchange_performed": True,
            "rate_limit_count": 0,
            "retry_delay_seconds_total": 0.0,
            "retry_after_honored": True,
        })
        observation_path = Path(self.temp.name) / "paypal-safe-observation.json"
        summary = write_paypal_shadow_observation(
            self.runtime, result, observation_path,
        )
        self.assertEqual(summary["transaction_count"], 3)
        self.assertTrue(summary["pipeline_ready"])
        observation = json.loads(observation_path.read_text(encoding="utf-8"))
        serialized = json.dumps(observation, ensure_ascii=False).lower()
        for forbidden in (
            "96.51", "100.00", "25.00", "paypal-demo-sale-001",
            "fixture@example.invalid", "private street",
        ):
            self.assertNotIn(forbidden, serialized)
        self.assertFalse(observation["raw_source_ids_included"])
        self.assertFalse(observation["customer_pii_or_free_text_included"])
        self.assertFalse(observation["financial_amounts_included"])
        assessment_path = Path(self.temp.name) / "paypal-safe-assessment.json"
        assessed = assess_connector_shadow_artifacts(
            self.runtime, baseline_path, observation_path, assessment_path,
        )
        self.assertTrue(assessed["passed"], json.loads(
            assessment_path.read_text(encoding="utf-8")
        ))
        observation["financial_amounts_included"] = True
        tampered_path = Path(self.temp.name) / "paypal-tampered-observation.json"
        tampered_path.write_text(json.dumps(observation), encoding="utf-8")
        with self.assertRaisesRegex(
            ConnectorShadowArtifactError, "integrity or privacy",
        ):
            assess_connector_shadow_artifacts(
                self.runtime, baseline_path, tampered_path,
                Path(self.temp.name) / "never.json",
            )


if __name__ == "__main__":
    unittest.main()
