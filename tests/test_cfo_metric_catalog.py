import unittest

from src.cfo_metric_catalog import (
    CfoMetricCatalogError,
    build_cfo_metric_catalog,
)


class CfoMetricCatalogTests(unittest.TestCase):
    def test_game_catalog_combines_core_and_game_definitions(self):
        result = build_cfo_metric_catalog({
            "core.finance", "industry.game_studio", "channel.app_store",
        }, runtime_fingerprint="a" * 64)
        metric_ids = {
            item["metric_type_id"] for item in result["metric_definitions"]
        }
        self.assertEqual(result["business_model_type_ids"], ["game_studio"])
        self.assertEqual(result["metric_count"], 10)
        self.assertIn("cash_runway_months", metric_ids)
        self.assertIn("game_platform_net_revenue", metric_ids)
        self.assertIn("game_prepaid_release_evidence_coverage_ratio", metric_ids)
        self.assertNotIn("dtc_net_sales", metric_ids)
        self.assertEqual(result["runtime_fingerprint"], "a" * 64)
        self.assertEqual(result["catalog_version"], 5)
        self.assertEqual(
            result["evaluation_contract"]["service_id"],
            "core.evaluate_cfo_metrics",
        )
        self.assertTrue(result["deterministic_evaluator_available"])
        mapping_ids = {
            (item["source_type_id"], item["source_id"])
            for item in result["source_mapping_definitions"]
        }
        self.assertIn(("service", "game.project_profitability"), mapping_ids)
        self.assertIn(("pipeline", "game.channel_settlement_close"), mapping_ids)
        self.assertTrue(result["trusted_source_operand_assembly_available"])
        self.assertFalse(result["caller_supplied_source_results_accepted_for_assembly"])

    def test_dtc_and_marketplace_catalogs_keep_distinct_economics(self):
        dtc = build_cfo_metric_catalog({
            "core.finance", "industry.commerce", "channel.dtc_storefront",
        })
        marketplace = build_cfo_metric_catalog({
            "core.finance", "industry.commerce", "channel.marketplace_commerce",
        })
        dtc_ids = {item["metric_type_id"] for item in dtc["metric_definitions"]}
        marketplace_ids = {
            item["metric_type_id"] for item in marketplace["metric_definitions"]
        }
        self.assertIn("dtc_order_to_payout_reconciliation_coverage_ratio", dtc_ids)
        self.assertIn("dtc_inventory_days_on_hand", dtc_ids)
        self.assertNotIn("marketplace_fee_rate", dtc_ids)
        self.assertIn("marketplace_three_way_scope_match_rate", marketplace_ids)
        self.assertIn("marketplace_fee_rate", marketplace_ids)
        self.assertNotIn("dtc_net_sales", marketplace_ids)
        definitions = {
            item["metric_type_id"]: item for item in marketplace["metric_definitions"]
        }
        self.assertEqual(definitions["marketplace_fee_rate"]["definition_version"], 2)
        self.assertEqual(
            definitions["marketplace_fee_rate"]["formula"]["operand_type_ids"],
            ["marketplace_fees", "marketplace_gross_merchandise_sales_ex_tax"],
        )
        mappings = {
            item["source_id"]: item for item in marketplace["source_mapping_definitions"]
        }
        self.assertEqual(mappings["marketplace.channel_close"]["coverage_status"], "executable")
        self.assertNotIn("amazon_seller.marketplace_close", mappings)
        amazon_marketplace = build_cfo_metric_catalog({
            "core.finance", "industry.commerce", "channel.marketplace_commerce",
            "connector.amazon_seller",
        })
        mappings = {
            item["source_id"]: item
            for item in amazon_marketplace["source_mapping_definitions"]
        }
        self.assertEqual(
            mappings["amazon_seller.marketplace_close"]["metric_type_ids"],
            ["marketplace_three_way_scope_match_rate"],
        )
        self.assertEqual(
            mappings["amazon_seller.marketplace_close"]["human_control_type_ids"],
            ["hashed_cross_source_keys_reviewed"],
        )
        shopify_monthly = build_cfo_metric_catalog({
            "core.finance", "industry.commerce", "channel.dtc_storefront",
            "connector.shopify", "connector.stripe",
            "feature.shopify_stripe_order_to_cash",
        })
        mappings = {
            item["source_id"]: item
            for item in shopify_monthly["source_mapping_definitions"]
        }
        self.assertEqual(
            mappings["dtc.shopify_stripe_month_close"]["coverage_status"],
            "executable",
        )
        self.assertEqual(
            mappings["dtc.shopify_stripe_month_close"]["metric_type_ids"],
            ["dtc_net_sales", "dtc_refund_return_rate"],
        )
        self.assertEqual(
            mappings["dtc.shopify_stripe_daily_close"]["coverage_status"],
            "blocked_source_contract",
        )

    def test_combined_commerce_catalog_deduplicates_core_metrics(self):
        result = build_cfo_metric_catalog({
            "core.finance", "industry.commerce", "channel.dtc_storefront",
            "channel.marketplace_commerce", "connector.custom_fork",
        })
        metric_ids = [
            item["metric_type_id"] for item in result["metric_definitions"]
        ]
        self.assertEqual(
            result["business_model_type_ids"],
            ["dtc_store", "marketplace_seller"],
        )
        self.assertEqual(result["metric_count"], 16)
        self.assertEqual(len(metric_ids), len(set(metric_ids)))
        self.assertEqual(metric_ids.count("cash_runway_months"), 1)

    def test_definitions_fail_closed_on_missing_values_and_aggregation(self):
        result = build_cfo_metric_catalog({
            "core.finance", "industry.commerce", "channel.dtc_storefront",
        })
        definitions = {
            item["metric_type_id"]: item
            for item in result["metric_definitions"]
        }
        runway = definitions["cash_runway_months"]
        inventory_days = definitions["dtc_inventory_days_on_hand"]
        self.assertEqual(
            runway["formula"]["zero_denominator_policy"], "not_available",
        )
        self.assertEqual(
            runway["formula"]["nonpositive_denominator_policy"],
            "not_available",
        )
        self.assertEqual(runway["definition_version"], 2)
        self.assertEqual(
            inventory_days["formula"],
            {
                "operator_type_id": "safe_divide_scaled",
                "operand_type_ids": [
                    "average_inventory_cost",
                    "trailing_twelve_month_cost_of_goods_sold",
                ],
                "missing_operand_policy": "not_available",
                "zero_denominator_policy": "not_available",
                "scale": 365,
            },
        )
        self.assertEqual(
            result["missing_input_policy"],
            "not_available_never_zero_or_inferred",
        )
        self.assertTrue(
            result["connector_selection_does_not_reduce_required_data_domains"],
        )
        self.assertFalse(result["metric_values_returned"])
        self.assertFalse(result["formula_evaluated"])
        self.assertFalse(result["external_actions_performed"])

    def test_invalid_industry_or_fingerprint_fails_closed(self):
        with self.assertRaisesRegex(CfoMetricCatalogError, "exactly one"):
            build_cfo_metric_catalog({"core.finance"})
        with self.assertRaisesRegex(CfoMetricCatalogError, "DTC or Marketplace"):
            build_cfo_metric_catalog({"core.finance", "industry.commerce"})
        with self.assertRaisesRegex(CfoMetricCatalogError, "fingerprint"):
            build_cfo_metric_catalog(
                {"core.finance", "industry.game_studio"},
                runtime_fingerprint="bad",
            )


if __name__ == "__main__":
    unittest.main()
