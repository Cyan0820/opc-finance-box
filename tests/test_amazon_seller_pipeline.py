from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from src.box_compiler import build_pipeline_runtime_catalog, compile_box_file
from src.box_pipeline import dispatch_box_pipeline_request
from src.box_runtime import BoxRuntime


ROOT = Path(__file__).resolve().parents[1]
BOX = ROOT / "examples" / "boxes" / "us_marketplace_amazon_seller_c_corp.json"
FIXTURE = ROOT / "examples" / "pipelines" / "amazon_seller_transaction_close_fixture.json"
MARKETPLACE_FIXTURE = (
    ROOT / "examples" / "pipelines" / "amazon_seller_marketplace_close_fixture.json"
)


class AmazonSellerPipelineTests(unittest.TestCase):
    def setUp(self):
        self.runtime = BoxRuntime(BOX, ROOT / "packs")
        self.request = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def test_pipeline_preserves_marketplace_specific_gates_and_non_posting_boundary(self):
        result = dispatch_box_pipeline_request(self.runtime, self.request)
        self.assertTrue(result["ready"])
        self.assertEqual(result["pipeline"]["pipeline_id"], "amazon_seller.transaction_close")
        self.assertEqual(result["founder_briefing"]["deferred_transaction_count"], 1)
        self.assertEqual(result["founder_briefing"]["refund_candidate_count"], 1)
        self.assertEqual(result["founder_briefing"]["fee_candidate_count"], 1)
        self.assertTrue(result["founder_briefing"]["revenue_claim_prohibited"])
        self.assertFalse(result["external_actions_performed"])
        self.assertIn(
            "amazon_seller_settlement_completeness_review",
            result["pipeline"]["required_review_gates"],
        )

    def test_quality_failure_stops_before_service(self):
        request = copy.deepcopy(self.request)
        transaction = request["payload"]["amazon_seller_request"]["transaction_pages"][0][
            "payload"
        ]["transactions"][0]
        transaction["transactionStatus"] = "UNKNOWN"
        result = dispatch_box_pipeline_request(self.runtime, request)
        self.assertFalse(result["ready"])
        self.assertEqual(result["blocked_at"], "quality_gate")
        self.assertEqual(result["services"], {})

    def test_compiler_catalog_template_and_pack_boundary(self):
        compiled = compile_box_file(BOX, ROOT / "packs")
        pipeline = next(
            item for item in compiled["pipelines"]
            if item["pipeline_id"] == "amazon_seller.transaction_close"
        )
        self.assertEqual(pipeline["required_connectors"], ["amazon_seller.transaction_activity"])
        self.assertEqual(
            pipeline["required_services"], ["amazon_seller.summarize_transaction_activity"],
        )
        template = next(
            item for item in compiled["pipeline_request_templates"]["templates"]
            if item["pipeline_id"] == "amazon_seller.transaction_close"
        )
        self.assertEqual(template["entity_id"], "us_amazon_marketplace_company")
        self.assertIn("amazon_seller_request", template["request"]["payload"])
        catalog = build_pipeline_runtime_catalog(self.runtime)
        self.assertIn(
            "amazon_seller.transaction_close",
            {item["pipeline_id"] for item in catalog["pipelines"]},
        )
        generic_marketplace = BoxRuntime(
            ROOT / "examples" / "boxes" / "cn_marketplace_store.json", ROOT / "packs",
        )
        self.assertNotIn(
            "amazon_seller.transaction_close",
            {item["pipeline_id"] for item in build_pipeline_runtime_catalog(generic_marketplace)["pipelines"]},
        )

    def test_marketplace_pipeline_cross_checks_three_minimized_sources_without_actions(self):
        request = json.loads(MARKETPLACE_FIXTURE.read_text(encoding="utf-8"))
        result = dispatch_box_pipeline_request(self.runtime, request)
        self.assertTrue(result["ready"])
        self.assertEqual(result["pipeline"]["pipeline_id"], "amazon_seller.marketplace_close")
        self.assertEqual(
            result["lineage"]["dataset_counts"],
            {
                "commerce.amazon_seller_orders": 1,
                "commerce.amazon_seller_inventory": 1,
                "commerce.amazon_seller_transactions": 1,
            },
        )
        self.assertEqual(result["founder_briefing"]["finance_without_order_count"], 0)
        self.assertEqual(result["founder_briefing"]["shipped_order_without_finance_count"], 0)
        self.assertEqual(result["founder_briefing"]["fba_order_sku_without_inventory_count"], 0)
        self.assertTrue(
            result["founder_briefing"]["current_inventory_not_historical_period_end"]
        )
        self.assertTrue(result["founder_briefing"]["revenue_tax_settlement_claim_prohibited"])
        self.assertEqual(result["founder_briefing"]["period"], "2026-08")
        self.assertTrue(result["founder_briefing"]["canonical_month_scope"])
        self.assertEqual(result["founder_briefing"]["eligible_three_way_order_count"], 1)
        self.assertEqual(result["founder_briefing"]["matched_three_way_order_count"], 1)
        self.assertEqual(result["founder_briefing"]["three_way_match_rate"], "1")
        self.assertFalse(result["external_actions_performed"])
        source = result["connector_batches"]["amazon_seller.marketplace_evidence"]["source"]
        self.assertEqual(source["canonical_month_period"], "2026-08")
        self.assertTrue(source["canonical_month_scope"])
        self.assertEqual(source["interval_semantics"], "half_open_utc")
        self.assertEqual(source["orders_included_data"], ["FULFILLMENT"])
        self.assertFalse(source["buyer_recipient_or_address_retained"])
        self.assertFalse(source["product_title_or_raw_identity_retained"])
        self.assertEqual(result["lineage"]["period"], "2026-08")
        self.assertEqual(result["lineage"]["marketplace_id"], "ATVPDKIKX0DER")
        assembly = result["cfo_metric_operand_assembly"]["assemblies"][0]
        self.assertEqual(assembly["pending_control_type_ids"], [
            "hashed_cross_source_keys_reviewed",
        ])

    def test_marketplace_pipeline_quality_failure_stops_before_reconciliation(self):
        request = json.loads(MARKETPLACE_FIXTURE.read_text(encoding="utf-8"))
        request["payload"]["amazon_seller_marketplace_request"]["order_pages"][0][
            "orders"
        ][0]["salesChannel"]["marketplaceId"] = "A1F83G8C2ARO7P"
        result = dispatch_box_pipeline_request(self.runtime, request)
        self.assertFalse(result["ready"])
        self.assertEqual(result["blocked_at"], "quality_gate")
        self.assertEqual(result["services"], {})

    def test_noncanonical_window_keeps_three_way_metric_unavailable(self):
        request = json.loads(MARKETPLACE_FIXTURE.read_text(encoding="utf-8"))
        request["payload"]["amazon_seller_marketplace_request"]["interval_start"] = (
            "2026-08-02T00:00:00Z"
        )
        result = dispatch_box_pipeline_request(self.runtime, request)
        self.assertTrue(result["ready"])
        source = result["connector_batches"]["amazon_seller.marketplace_evidence"]["source"]
        self.assertIsNone(source["canonical_month_period"])
        self.assertFalse(source["canonical_month_scope"])
        self.assertIsNone(result["lineage"]["period"])
        assembly = result["cfo_metric_operand_assembly"]
        self.assertEqual(
            assembly["coverage_status"], "not_available_source_not_ready_or_empty",
        )
        self.assertEqual(assembly["assembly_count"], 0)
        self.assertEqual(
            assembly["coverage_blocker_type_ids"],
            ["canonical_month_scope_not_satisfied"],
        )

    def test_marketplace_compiler_catalog_template_and_pack_boundary(self):
        compiled = compile_box_file(BOX, ROOT / "packs")
        pipeline = next(
            item for item in compiled["pipelines"]
            if item["pipeline_id"] == "amazon_seller.marketplace_close"
        )
        self.assertEqual(
            pipeline["required_connectors"], ["amazon_seller.marketplace_evidence"],
        )
        self.assertEqual(
            pipeline["required_services"], ["amazon_seller.reconcile_marketplace_evidence"],
        )
        template = next(
            item for item in compiled["pipeline_request_templates"]["templates"]
            if item["pipeline_id"] == "amazon_seller.marketplace_close"
        )
        self.assertEqual(template["entity_id"], "us_amazon_marketplace_company")
        self.assertIn("amazon_seller_marketplace_request", template["request"]["payload"])
        catalog = build_pipeline_runtime_catalog(self.runtime)
        self.assertIn(
            "amazon_seller.marketplace_close",
            {item["pipeline_id"] for item in catalog["pipelines"]},
        )
        generic_marketplace = BoxRuntime(
            ROOT / "examples" / "boxes" / "cn_marketplace_store.json", ROOT / "packs",
        )
        self.assertNotIn(
            "amazon_seller.marketplace_close",
            {
                item["pipeline_id"]
                for item in build_pipeline_runtime_catalog(generic_marketplace)["pipelines"]
            },
        )


if __name__ == "__main__":
    unittest.main()
