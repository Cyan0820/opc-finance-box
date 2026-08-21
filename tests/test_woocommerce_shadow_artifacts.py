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
    build_connector_shadow_baseline_workpaper,
    finalize_connector_shadow_baseline_workpaper,
    write_woocommerce_shadow_observation,
)


ROOT = Path(__file__).resolve().parents[1]
BOX = ROOT / "examples" / "boxes" / "us_dtc_woocommerce_c_corp.json"
REQUEST = ROOT / "examples" / "pipelines" / "woocommerce_order_refund_close_fixture.json"


class WooCommerceShadowArtifactTests(unittest.TestCase):
    def setUp(self):
        self.runtime = BoxRuntime(BOX, ROOT / "packs")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def _real_baseline(self) -> Path:
        workpaper_path = Path(self.temp.name) / "woocommerce-workpaper.json"
        build_connector_shadow_baseline_workpaper(
            self.runtime,
            pipeline_id="woocommerce.order_refund_close",
            entity_id="us_dtc_company",
            sample_period="2026-08",
            prepared_by="independent-woocommerce-source-preparer",
            output=workpaper_path,
        )
        workpaper = json.loads(workpaper_path.read_text(encoding="utf-8"))
        workpaper["source_independence"] = {
            "prepared_from_independent_source": True,
            "pipeline_output_used_as_baseline": False,
            "source_scope_confirmed": True,
        }
        workpaper["anonymization"]["private_source_evidence_retained"] = True
        workpaper["source_expectations"][0]["expected_record_count"] = 2
        workpaper["source_expectations"][0]["evidence_references"] = [
            "private-export://woocommerce/us-dtc/2026-08/source-counts",
        ]
        expected_controls = {
            "pipeline_ready": True,
            "order_count": 1,
            "refund_event_count": 1,
            "orphan_refund_count": 0,
            "arithmetic_exception_count": 0,
            "destination_review_required_count": 0,
            "unpaid_or_unconfirmed_order_count": 0,
            "network_order_refund_read_performed": True,
            "monthly_half_open_window": True,
            "entity_site_scope_matched": True,
            "customer_pii_excluded": True,
            "product_detail_excluded": True,
            "raw_source_ids_excluded": True,
            "fixed_read_only_transport_controls": True,
            "business_write_posting_revenue_tax_actions_disabled": True,
        }
        for item in workpaper["control_expectations"]:
            item["expected_value"] = expected_controls[item["control_id"]]
        workpaper["evidence_references"] = [
            "workpaper://woocommerce/us-dtc/2026-08/source-scope",
        ]
        workpaper["finalization_ready"] = True
        workpaper_path.write_text(json.dumps(workpaper), encoding="utf-8")
        baseline_path = Path(self.temp.name) / "woocommerce-real-baseline.json"
        finalized = finalize_connector_shadow_baseline_workpaper(
            self.runtime, workpaper_path, baseline_path,
        )
        self.assertTrue(finalized["real_sample_evidence"])
        return baseline_path

    def _live_result(self) -> dict:
        request = json.loads(REQUEST.read_text(encoding="utf-8"))
        result = dispatch_box_pipeline_request(self.runtime, request)
        result["network_access_performed"] = True
        source = result["connector_batches"]["woocommerce.order_refund_activity"]["source"]
        source.update({
            "kind": "api",
            "network_access_performed": True,
            "basic_auth_header_used": True,
            "order_total": 1,
            "refund_total": 1,
            "rate_limit_count": 0,
            "retry_delay_seconds_total": 0.0,
            "retry_after_honored": True,
        })
        return result

    def test_safe_observation_excludes_amounts_site_and_business_values_then_assesses(self):
        baseline_path = self._real_baseline()
        result = self._live_result()
        observation_path = Path(self.temp.name) / "woocommerce-safe-observation.json"
        summary = write_woocommerce_shadow_observation(
            self.runtime, result, observation_path,
        )
        self.assertEqual(summary["order_count"], 1)
        self.assertEqual(summary["refund_event_count"], 1)
        self.assertTrue(summary["pipeline_ready"])
        observation = json.loads(observation_path.read_text(encoding="utf-8"))
        serialized = json.dumps(observation, ensure_ascii=False).lower()
        for forbidden in (
            "102.20", "20.00", "card_gateway", "wc-order-demo-001",
            "private.example", "alice", "demo product",
        ):
            self.assertNotIn(forbidden, serialized)
        self.assertFalse(observation["raw_source_ids_included"])
        self.assertFalse(observation["site_origin_included"])
        self.assertFalse(observation["customer_or_product_values_included"])
        self.assertFalse(observation["financial_amounts_included"])
        assessment_path = Path(self.temp.name) / "woocommerce-safe-assessment.json"
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
            write_woocommerce_shadow_observation(
                self.runtime, fixture, Path(self.temp.name) / "never-fixture.json",
            )
        dirty = self._live_result()
        dirty["connector_batches"]["woocommerce.order_refund_activity"]["quality"][
            "rejected_count"
        ] = 1
        with self.assertRaisesRegex(ConnectorShadowArtifactError, "clean non-empty"):
            write_woocommerce_shadow_observation(
                self.runtime, dirty, Path(self.temp.name) / "never-dirty.json",
            )

        baseline_path = self._real_baseline()
        observation_path = Path(self.temp.name) / "woocommerce-safe.json"
        write_woocommerce_shadow_observation(
            self.runtime, self._live_result(), observation_path,
        )
        observation = json.loads(observation_path.read_text(encoding="utf-8"))
        observation["financial_amounts_included"] = True
        tampered_path = Path(self.temp.name) / "woocommerce-tampered.json"
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
