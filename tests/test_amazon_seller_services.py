from __future__ import annotations

import json
import unittest
from pathlib import Path

from src.box_runtime import BoxRuntime
from src.default_connectors import build_box_connector_registry
from src.default_services import build_default_service_registry


ROOT = Path(__file__).resolve().parents[1]
BOX = ROOT / "examples" / "boxes" / "us_marketplace_amazon_seller_c_corp.json"
FIXTURE = ROOT / "packs" / "connectors" / "amazon_seller" / "fixture-transactions.json"
MARKETPLACE_FIXTURE = (
    ROOT / "packs" / "connectors" / "amazon_seller" / "fixture-marketplace-evidence.json"
)


class AmazonSellerServicesTests(unittest.TestCase):
    def setUp(self):
        self.runtime = BoxRuntime(BOX, ROOT / "packs")
        request = json.loads(FIXTURE.read_text(encoding="utf-8"))
        batch = build_box_connector_registry(self.runtime).dispatch(
            self.runtime, "amazon_seller.transaction_activity", request,
        )["batch"]
        self.rows = batch["datasets"]["commerce.amazon_seller_transactions"]
        marketplace_request = json.loads(MARKETPLACE_FIXTURE.read_text(encoding="utf-8"))
        marketplace_batch = build_box_connector_registry(self.runtime).dispatch(
            self.runtime, "amazon_seller.marketplace_evidence", marketplace_request,
        )["batch"]
        self.marketplace_source = marketplace_batch["source"]
        self.orders = marketplace_batch["datasets"]["commerce.amazon_seller_orders"]
        self.inventory = marketplace_batch["datasets"]["commerce.amazon_seller_inventory"]
        self.marketplace_transactions = marketplace_batch["datasets"][
            "commerce.amazon_seller_transactions"
        ]

    def test_marketplace_cross_source_differences_are_candidates_not_completeness_claims(self):
        output = build_default_service_registry().dispatch(
            self.runtime, "amazon_seller.reconcile_marketplace_evidence",
            {
                "orders": self.orders,
                "inventory": self.inventory,
                "transactions": self.marketplace_transactions,
                "source_scope": {
                    key: self.marketplace_source[key] for key in (
                        "canonical_month_period", "canonical_month_scope", "marketplace_id",
                        "interval_start", "interval_end", "orders_time_basis",
                        "inventory_observed_at", "inventory_observation_type",
                    )
                },
            },
            entity_id="us_amazon_marketplace_company",
        )["output"]
        self.assertTrue(output["ready"])
        self.assertEqual(output["order_count"], 3)
        self.assertEqual(output["inventory_sku_count"], 2)
        self.assertEqual(output["transaction_count"], 2)
        self.assertEqual(output["order_status_counts"], {"CANCELLED": 1, "SHIPPED": 2})
        self.assertEqual(output["period"], "2026-08")
        self.assertTrue(output["canonical_month_scope"])
        self.assertEqual(output["marketplace_id"], "ATVPDKIKX0DER")
        self.assertEqual(output["eligible_three_way_order_count"], 1)
        self.assertEqual(output["matched_three_way_order_count"], 1)
        self.assertEqual(output["three_way_match_rate"], "1")
        self.assertEqual(output["unmatched_three_way_order_keys"], [])
        self.assertEqual(len(output["finance_without_order_keys"]), 1)
        self.assertEqual(len(output["shipped_order_without_finance_keys"]), 1)
        self.assertEqual(output["fba_order_sku_without_inventory_keys"], [])
        self.assertEqual(len(output["inventory_sku_without_window_order_keys"]), 1)
        self.assertEqual(output["inventory_quantity_summary"]["total_quantity"], 13)
        self.assertEqual(output["inventory_quantity_field_missing_keys"], [])
        self.assertTrue(output["current_inventory_not_historical_period_end"])
        self.assertTrue(output["hashed_cross_source_keys_generated"])
        self.assertFalse(output["hashed_cross_source_keys_human_reviewed"])
        self.assertTrue(output["three_way_scope_match_is_not_completeness_claim"])
        self.assertFalse(output["order_or_financial_completeness_proven"])
        self.assertFalse(output["posting_or_inventory_adjustment_performed"])
        self.assertFalse(output["external_actions_performed"])

    def test_marketplace_cross_source_duplicate_and_entity_controls_fail_closed(self):
        registry = build_default_service_registry()
        output = registry.dispatch(
            self.runtime, "amazon_seller.reconcile_marketplace_evidence",
            {
                "orders": [self.orders[0], self.orders[0]],
                "inventory": self.inventory,
                "transactions": self.marketplace_transactions,
            }, entity_id="us_amazon_marketplace_company",
        )["output"]
        self.assertFalse(output["ready"])
        self.assertIn({"code": "duplicate_amazon_order_key"}, output["blockers"])
        with self.assertRaisesRegex(ValueError, "outside statutory entity"):
            registry.dispatch(
                self.runtime, "amazon_seller.reconcile_marketplace_evidence",
                {
                    "orders": [dict(self.orders[0], entity_id="other")],
                    "inventory": self.inventory,
                    "transactions": self.marketplace_transactions,
                }, entity_id="us_amazon_marketplace_company",
            )

    def test_currency_status_component_and_candidate_summaries_are_deterministic(self):
        registry = build_default_service_registry()
        first = registry.dispatch(
            self.runtime, "amazon_seller.summarize_transaction_activity",
            {"transactions": self.rows}, entity_id="us_amazon_marketplace_company",
        )["output"]
        second = registry.dispatch(
            self.runtime, "amazon_seller.summarize_transaction_activity",
            {"transactions": list(reversed(self.rows))}, entity_id="us_amazon_marketplace_company",
        )["output"]
        self.assertTrue(first["ready"])
        self.assertEqual(first["transaction_count"], 3)
        self.assertEqual(first["status_counts"], {"DEFERRED": 1, "RELEASED": 2})
        self.assertEqual(first["currency_summary"][0]["net_activity"], "55.00")
        self.assertEqual(first["currency_summary"][0]["released_activity"], "70.00")
        self.assertEqual(first["currency_summary"][0]["deferred_activity"], "-15.00")
        self.assertEqual(len(first["refund_candidate_keys"]), 1)
        self.assertEqual(len(first["fee_candidate_keys"]), 2)
        self.assertTrue(first["nested_component_double_counting_prohibited"])
        self.assertFalse(first["revenue_recognition_performed"])
        self.assertEqual(first, second)

    def test_entity_scope_and_duplicate_keys_fail_closed(self):
        registry = build_default_service_registry()
        wrong = [dict(self.rows[0], entity_id="other")]
        with self.assertRaisesRegex(ValueError, "outside statutory entity"):
            registry.dispatch(
                self.runtime, "amazon_seller.summarize_transaction_activity",
                {"transactions": wrong}, entity_id="us_amazon_marketplace_company",
            )
        output = registry.dispatch(
            self.runtime, "amazon_seller.summarize_transaction_activity",
            {"transactions": [self.rows[0], self.rows[0]]},
            entity_id="us_amazon_marketplace_company",
        )["output"]
        self.assertFalse(output["ready"])
        self.assertEqual(output["blockers"], [{"code": "duplicate_amazon_transaction_key"}])


if __name__ == "__main__":
    unittest.main()
