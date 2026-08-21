from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.box_pipeline import dispatch_box_pipeline_request
from src.box_runtime import BoxRuntime
from src.connector_shadow_artifacts import (
    assess_connector_shadow_artifacts,
    build_connector_shadow_baseline_plan,
    build_connector_shadow_baseline_workpaper,
    finalize_connector_shadow_baseline_workpaper,
    review_connector_shadow_artifact,
    verify_connector_shadow_artifact,
)


ROOT = Path(__file__).resolve().parents[1]
BOX = ROOT / "examples" / "boxes" / "us_marketplace_amazon_seller_c_corp.json"
REQUEST = ROOT / "examples" / "pipelines" / "amazon_seller_marketplace_close_fixture.json"


class AmazonSellerShadowArtifactTests(unittest.TestCase):
    def setUp(self):
        self.runtime = BoxRuntime(BOX, ROOT / "packs")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def test_plan_and_real_shadow_contract_cover_amazon_seller_pack(self):
        plan = build_connector_shadow_baseline_plan(self.runtime)
        profile = next(
            item for item in plan["profiles"]
            if item["pipeline_id"] == "amazon_seller.marketplace_close"
        )
        self.assertEqual(profile["covered_pack_ids"], ["connector.amazon_seller"])
        self.assertEqual(
            profile["source_connector_ids"], ["amazon_seller.marketplace_evidence"],
        )

        request = json.loads(REQUEST.read_text(encoding="utf-8"))
        result = dispatch_box_pipeline_request(self.runtime, request)
        source = result["connector_batches"]["amazon_seller.marketplace_evidence"]["source"]
        source["kind"] = "api"
        source["network_access_performed"] = True
        source["lwa_token_exchange_performed"] = True
        source["lwa_token_exchange_count"] = 1
        result["network_access_performed"] = True

        workpaper_path = Path(self.temp.name) / "amazon-workpaper.json"
        created = build_connector_shadow_baseline_workpaper(
            self.runtime,
            pipeline_id="amazon_seller.marketplace_close",
            entity_id="us_amazon_marketplace_company",
            sample_period="2026-08",
            prepared_by="independent-amazon-source-preparer",
            output=workpaper_path,
        )
        self.assertEqual(created["source_count"], 1)
        workpaper = json.loads(workpaper_path.read_text(encoding="utf-8"))
        workpaper["source_independence"] = {
            "prepared_from_independent_source": True,
            "pipeline_output_used_as_baseline": False,
            "source_scope_confirmed": True,
        }
        workpaper["anonymization"]["private_source_evidence_retained"] = True
        workpaper["source_expectations"][0]["expected_record_count"] = 3
        workpaper["source_expectations"][0]["evidence_references"] = [
            "private-export://amazon-seller/us/2026-08/orders-inventory-finances-counts",
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
            "workpaper://amazon-seller/us/2026-08/independent-finances-scope",
        ]
        workpaper["finalization_ready"] = True
        workpaper_path.write_text(json.dumps(workpaper), encoding="utf-8")

        baseline_path = Path(self.temp.name) / "amazon-baseline.json"
        finalized = finalize_connector_shadow_baseline_workpaper(
            self.runtime, workpaper_path, baseline_path,
        )
        self.assertTrue(finalized["real_sample_evidence"])
        result_path = Path(self.temp.name) / "amazon-result.json"
        result_path.write_text(json.dumps({"ok": True, "result": result}), encoding="utf-8")
        assessment_path = Path(self.temp.name) / "amazon-assessment.json"
        assessed = assess_connector_shadow_artifacts(
            self.runtime, baseline_path, result_path, assessment_path,
        )
        self.assertTrue(assessed["passed"])
        self.assertEqual(assessed["control_count"], len(expected_controls))
        reviewed_path = Path(self.temp.name) / "amazon-reviewed.json"
        review_connector_shadow_artifact(
            self.runtime,
            assessment_path,
            reviewed_path,
            decision="passed",
            actor="independent-amazon-shadow-reviewer",
            rationale="Amazon Seller 来源计数、主体卖家 Marketplace 绑定和只读控制均已独立复核",
            evidence_references=["review://amazon-seller/us/2026-08/final"],
        )
        verified = verify_connector_shadow_artifact(self.runtime, reviewed_path)
        self.assertTrue(verified["valid"])
        self.assertEqual(verified["covered_pack_ids"], ["connector.amazon_seller"])


if __name__ == "__main__":
    unittest.main()
