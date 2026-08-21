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
    write_amazon_seller_shadow_observation,
)


ROOT = Path(__file__).resolve().parents[1]
BOX = ROOT / "examples" / "boxes" / "us_marketplace_amazon_seller_c_corp.json"
REQUEST = ROOT / "examples" / "pipelines" / "amazon_seller_marketplace_close_fixture.json"


class AmazonSellerShadowObservationTests(unittest.TestCase):
    def setUp(self):
        self.runtime = BoxRuntime(BOX, ROOT / "packs")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def _baseline(self) -> Path:
        workpaper_path = Path(self.temp.name) / "amazon-workpaper.json"
        build_connector_shadow_baseline_workpaper(
            self.runtime,
            pipeline_id="amazon_seller.marketplace_close",
            entity_id="us_amazon_marketplace_company",
            sample_period="2026-07",
            prepared_by="independent-amazon-source-preparer",
            output=workpaper_path,
        )
        workpaper = json.loads(workpaper_path.read_text(encoding="utf-8"))
        workpaper["source_independence"] = {
            "prepared_from_independent_source": True,
            "pipeline_output_used_as_baseline": False,
            "source_scope_confirmed": True,
        }
        workpaper["anonymization"]["private_source_evidence_retained"] = True
        workpaper["source_expectations"][0]["expected_record_count"] = 3
        workpaper["source_expectations"][0]["evidence_references"] = [
            "private-export://amazon-seller/us/2026-07/three-source-counts",
        ]
        expected_controls = {
            "pipeline_ready": True,
            "order_count": 1,
            "inventory_sku_count": 1,
            "transaction_count": 1,
            "finance_without_order_count": 0,
            "shipped_order_without_finance_count": 0,
            "fba_order_sku_without_inventory_count": 0,
            "inventory_quantity_field_missing_count": 0,
            "network_three_source_read_performed": True,
            "single_lwa_exchange_in_memory": True,
            "monthly_orders_finances_half_open_window": True,
            "current_inventory_not_historical_period_end": True,
            "entity_seller_marketplace_scope_matched": True,
            "buyer_recipient_product_raw_ids_excluded": True,
            "orders_restricted_datasets_not_requested": True,
            "fixed_regional_read_only_transport_controls": True,
            "business_write_posting_revenue_tax_inventory_actions_disabled": True,
        }
        for item in workpaper["control_expectations"]:
            item["expected_value"] = expected_controls[item["control_id"]]
        workpaper["evidence_references"] = [
            "workpaper://amazon-seller/us/2026-07/independent-three-source-scope",
        ]
        workpaper["finalization_ready"] = True
        workpaper_path.write_text(json.dumps(workpaper), encoding="utf-8")
        baseline_path = Path(self.temp.name) / "amazon-baseline.json"
        finalized = finalize_connector_shadow_baseline_workpaper(
            self.runtime, workpaper_path, baseline_path,
        )
        self.assertTrue(finalized["real_sample_evidence"])
        return baseline_path

    def _live_result(self) -> dict:
        request = json.loads(REQUEST.read_text(encoding="utf-8"))
        connector = request["payload"]["amazon_seller_marketplace_request"]
        connector["interval_start"] = "2026-07-01T00:00:00Z"
        connector["interval_end"] = "2026-08-01T00:00:00Z"
        connector["fixture_inventory_observed_at"] = "2026-08-01T00:05:00Z"
        order = connector["order_pages"][0]["orders"][0]
        order["createdTime"] = "2026-07-05T10:00:00Z"
        order["lastUpdatedTime"] = "2026-07-06T10:00:00Z"
        connector["inventory_pages"][0]["payload"]["inventorySummaries"][0][
            "lastUpdatedTime"
        ] = "2026-08-01T00:01:00Z"
        connector["transaction_pages"][0]["payload"]["transactions"][0][
            "postedDate"
        ] = "2026-07-07T00:00:00Z"
        result = dispatch_box_pipeline_request(self.runtime, request)
        result["network_access_performed"] = True
        source = result["connector_batches"]["amazon_seller.marketplace_evidence"]["source"]
        source.update({
            "kind": "api",
            "environment": "production",
            "network_access_performed": True,
            "lwa_token_exchange_performed": True,
            "lwa_token_exchange_count": 1,
            "order_count": 1,
            "inventory_count": 1,
            "transaction_count": 1,
            "rate_limit_count": 0,
            "retry_delay_seconds_total": 0.0,
            "retry_after_honored": True,
            "response_links_followed": False,
        })
        return result

    def test_safe_observation_excludes_amounts_marketplace_inventory_and_raw_values_then_assesses(self):
        baseline_path = self._baseline()
        observation_path = Path(self.temp.name) / "amazon-safe-observation.json"
        summary = write_amazon_seller_shadow_observation(
            self.runtime, self._live_result(), observation_path,
        )
        self.assertEqual(summary["order_count"], 1)
        self.assertEqual(summary["inventory_sku_count"], 1)
        self.assertEqual(summary["transaction_count"], 1)
        self.assertTrue(summary["pipeline_ready"])
        observation = json.loads(observation_path.read_text(encoding="utf-8"))
        serialized = json.dumps(observation, ensure_ascii=False).lower()
        for forbidden in (
            "atvpdkikx0der", "pipeline-private-seller", "pipeline-private-order",
            "pipeline-private-sku", "pipeline-private-asin", "private marketplace",
            "90.00", "100.00", "fulfillable_quantity", "total_quantity",
            '"na"',
        ):
            self.assertNotIn(forbidden, serialized)
        self.assertFalse(observation["raw_source_ids_included"])
        self.assertFalse(observation["seller_region_or_marketplace_values_included"])
        self.assertFalse(observation["buyer_product_or_inventory_values_included"])
        self.assertFalse(observation["financial_amounts_included"])
        assessed_path = Path(self.temp.name) / "amazon-safe-assessment.json"
        assessed = assess_connector_shadow_artifacts(
            self.runtime, baseline_path, observation_path, assessed_path,
        )
        self.assertTrue(assessed["passed"], json.loads(
            assessed_path.read_text(encoding="utf-8")
        ))

    def test_observation_rejects_fixture_dirty_batch_and_tamper(self):
        fixture = dispatch_box_pipeline_request(
            self.runtime, json.loads(REQUEST.read_text(encoding="utf-8")),
        )
        with self.assertRaisesRegex(ConnectorShadowArtifactError, "real minimized"):
            write_amazon_seller_shadow_observation(
                self.runtime, fixture, Path(self.temp.name) / "never-fixture.json",
            )
        dirty = self._live_result()
        dirty["connector_batches"]["amazon_seller.marketplace_evidence"]["quality"][
            "rejected_count"
        ] = 1
        with self.assertRaisesRegex(ConnectorShadowArtifactError, "clean non-empty"):
            write_amazon_seller_shadow_observation(
                self.runtime, dirty, Path(self.temp.name) / "never-dirty.json",
            )

        baseline_path = self._baseline()
        observation_path = Path(self.temp.name) / "amazon-safe.json"
        write_amazon_seller_shadow_observation(
            self.runtime, self._live_result(), observation_path,
        )
        observation = json.loads(observation_path.read_text(encoding="utf-8"))
        observation["financial_amounts_included"] = True
        tampered_path = Path(self.temp.name) / "amazon-tampered.json"
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
