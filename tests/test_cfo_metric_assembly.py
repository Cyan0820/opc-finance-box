from __future__ import annotations

import json
import unittest
from pathlib import Path

from src.box_pipeline import dispatch_box_pipeline_request
from src.box_runtime import BoxRuntime
from src.default_services import build_default_service_registry


ROOT = Path(__file__).resolve().parents[1]


class CfoMetricAssemblyTests(unittest.TestCase):
    @staticmethod
    def _runtime(box_name: str) -> BoxRuntime:
        return BoxRuntime(ROOT / "examples" / "boxes" / box_name, ROOT / "packs")

    @staticmethod
    def _pipeline_fixture(name: str) -> dict:
        return json.loads(
            (ROOT / "examples" / "pipelines" / name).read_text(encoding="utf-8")
        )

    def test_commerce_pipeline_assembles_ex_tax_operands_but_never_implicit_fx(self):
        runtime = self._runtime("cn_dtc_api_store.json")
        result = dispatch_box_pipeline_request(
            runtime, self._pipeline_fixture("commerce_channel_close_fixture.json"),
        )
        collection = result["cfo_metric_operand_assembly"]
        self.assertEqual(collection["coverage_status"], "executable")
        self.assertEqual(collection["assembly_count"], 1)
        assembled = collection["assemblies"][0]
        self.assertEqual(assembled["entity_id"], "cn_dtc_company")
        self.assertEqual(assembled["period"], "2026-07")
        self.assertEqual(assembled["currency"], "USD")
        self.assertEqual(
            assembled["operand_values"]["gross_order_sales_ex_tax_including_shipping"],
            "105",
        )
        self.assertEqual(assembled["operand_values"]["discounts_and_refunds_ex_tax"], "20")
        self.assertEqual(assembled["operand_values"]["gross_merchandise_sales_ex_tax"], "100")
        self.assertEqual(assembled["operand_values"]["refunds_ex_tax"], "10")
        self.assertEqual(
            assembled["evaluation_status"],
            "blocked_source_currency_not_functional_currency",
        )
        self.assertIsNone(assembled["evaluation_request"])
        self.assertIsNone(assembled["evaluation_preview"])
        self.assertFalse(collection["implicit_currency_conversion_performed"])
        self.assertIn("tax_inclusive_policy_confirmed", assembled["pending_control_type_ids"])
        self.assertIn("landed_cost_policy_confirmed", assembled["pending_control_type_ids"])

    def test_month_close_pipeline_generates_available_authoritative_blocker_count(self):
        runtime = self._runtime("cn_dtc_shopify_stripe_store.json")
        result = dispatch_box_pipeline_request(
            runtime, self._pipeline_fixture("month_close_control_fixture.json"),
        )
        assembled = result["cfo_metric_operand_assembly"]["assemblies"][0]
        preview = assembled["evaluation_preview"]
        self.assertEqual(assembled["metric_type_ids"], ["close_blocker_count"])
        self.assertEqual(assembled["pending_control_type_ids"], [])
        self.assertEqual(preview["metric_results"][0]["status"], "available")
        self.assertEqual(
            preview["metric_results"][0]["value"],
            len(result["services"]["month_close_control"]["output"]["issues"]),
        )
        self.assertFalse(preview["authoritative_accounting_or_statutory_truth_inferred"])

    def test_game_service_binds_each_title_dimension_and_keeps_allocation_review_pending(self):
        runtime = self._runtime("global_game_studio.json")
        result = build_default_service_registry().dispatch(
            runtime,
            "game.project_profitability",
            {
                "revenues": [{
                    "id": "R1", "entity_id": "cn_studio", "project_code": "G1",
                    "period": "2026-07", "currency": "CNY", "amount": 1000,
                    "evidence": ["revenue-ledger"],
                }],
                "costs": [{
                    "id": "C1", "entity_id": "cn_studio", "project_code": "G1",
                    "period": "2026-07", "currency": "CNY", "amount": 600,
                    "evidence": ["direct-cost-ledger"],
                }],
            },
        )
        assembled = result["cfo_metric_operand_assembly"]["assemblies"][0]
        self.assertEqual(assembled["dimension_scope"], {
            "dimension_type_id": "game_title", "dimension_value_ids": ["G1"],
        })
        self.assertEqual(assembled["operand_values"], {
            "title_contribution": "400", "title_net_revenue": "1000",
        })
        self.assertEqual(
            assembled["evaluation_preview"]["metric_results"][0]["status"],
            "blocked_missing_controls",
        )
        self.assertEqual(
            assembled["pending_control_type_ids"],
            ["shared_cost_allocation_evidence_confirmed"],
        )
        self.assertEqual(
            assembled["evaluation_preview"]["dimension_scope"],
            assembled["dimension_scope"],
        )

    def test_generic_marketplace_assembles_fee_and_concentration_operands_for_review(self):
        runtime = self._runtime("cn_marketplace_store.json")
        pipeline = dispatch_box_pipeline_request(
            runtime, self._pipeline_fixture("marketplace_channel_close_fixture.json"),
        )
        result = pipeline["cfo_metric_operand_assembly"]
        self.assertEqual(result["coverage_status"], "executable")
        self.assertEqual(result["assembly_count"], 1)
        assembled = result["assemblies"][0]
        self.assertEqual(assembled["metric_type_ids"], [
            "marketplace_fee_rate", "marketplace_revenue_concentration_ratio",
        ])
        self.assertEqual(assembled["dimension_scope"], {
            "dimension_type_id": "marketplace_population",
            "dimension_value_ids": ["Demo Marketplace"],
        })
        self.assertEqual(assembled["operand_values"], {
            "marketplace_fees": "10",
            "marketplace_gross_merchandise_sales_ex_tax": "100",
            "total_marketplace_net_revenue": "80",
        })
        self.assertEqual(
            assembled["vector_operand_values"], {"net_revenue_by_marketplace": ["80"]},
        )
        self.assertEqual(assembled["pending_control_type_ids"], [
            "complete_marketplace_population_confirmed",
            "fee_types_and_tax_treatment_reviewed",
        ])
        self.assertEqual(
            [item["status"] for item in assembled["evaluation_preview"]["metric_results"]],
            ["blocked_missing_controls", "blocked_missing_controls"],
        )

    def test_amazon_month_scope_assembles_three_way_candidate_but_keeps_key_review_pending(self):
        runtime = self._runtime("us_marketplace_amazon_seller_c_corp.json")
        pipeline = dispatch_box_pipeline_request(
            runtime, self._pipeline_fixture("amazon_seller_marketplace_close_fixture.json"),
        )
        result = pipeline["cfo_metric_operand_assembly"]
        self.assertEqual(result["coverage_status"], "executable")
        self.assertEqual(result["assembly_count"], 1)
        assembled = result["assemblies"][0]
        self.assertEqual(assembled["period"], "2026-08")
        self.assertEqual(assembled["dimension_scope"], {
            "dimension_type_id": "marketplace",
            "dimension_value_ids": ["ATVPDKIKX0DER"],
        })
        self.assertEqual(assembled["metric_type_ids"], [
            "marketplace_three_way_scope_match_rate",
        ])
        self.assertEqual(assembled["operand_values"], {
            "eligible_marketplace_orders": "1",
            "orders_matched_across_orders_finances_and_inventory": "1",
        })
        self.assertEqual(assembled["confirmed_control_type_ids"], [
            "seller_marketplace_and_period_scope_identical",
        ])
        self.assertEqual(assembled["pending_control_type_ids"], [
            "hashed_cross_source_keys_reviewed",
        ])
        self.assertEqual(
            assembled["evaluation_preview"]["metric_results"][0]["status"],
            "blocked_missing_controls",
        )
        self.assertFalse(result["caller_supplied_source_result_accepted"])


if __name__ == "__main__":
    unittest.main()
