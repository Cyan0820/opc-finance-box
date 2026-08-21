from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from src.box_runtime import BoxRuntime
from src.default_connectors import build_box_connector_registry
from src.default_services import build_default_service_registry


ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / "packs" / "connectors" / "shopify"


class ShopifyServicesTests(unittest.TestCase):
    def setUp(self):
        self.runtime = BoxRuntime(
            ROOT / "examples" / "boxes" / "cn_dtc_shopify_stripe_store.json", ROOT / "packs",
        )
        fixture = json.loads((PACK / "fixture-orders.json").read_text(encoding="utf-8"))
        batch = build_box_connector_registry(self.runtime).dispatch(
            self.runtime, "shopify.orders", fixture,
        )["batch"]
        self.payload = {
            "orders": batch["datasets"]["commerce.shopify_orders"],
            "transactions": batch["datasets"]["commerce.shopify_transactions"],
            "refunds": batch["datasets"]["commerce.shopify_refunds"],
            "include_test_orders": True,
        }
        self.services = build_default_service_registry()

    def _run(self, payload=None):
        return self.services.dispatch(
            self.runtime,
            "shopify.summarize_order_activity",
            payload or self.payload,
            entity_id="cn_dtc_company",
        )["output"]

    def test_order_payment_refund_facts_reconcile_in_both_money_views(self):
        output = self._run()
        self.assertTrue(output["ready"], output)
        self.assertTrue(output["ready_for_order_to_cash_review"])
        self.assertFalse(output["ready_for_commerce_margin"])
        views = {row["money_view"]: row for row in output["currency_summary"]}
        self.assertEqual(views["shop_money"]["currency"], "USD")
        self.assertEqual(views["shop_money"]["reported_received"], "104.50")
        self.assertEqual(views["shop_money"]["successful_collections"], "104.50")
        self.assertEqual(views["shop_money"]["reported_refunded"], "20.90")
        self.assertEqual(views["shop_money"]["successful_refund_transactions"], "20.90")
        self.assertEqual(views["presentment_money"]["currency"], "EUR")
        self.assertEqual(views["presentment_money"]["current_order_total"], "76.91")
        self.assertFalse(output["revenue_recognition_performed"])
        self.assertFalse(output["margin_calculation_performed"])
        self.assertTrue(output["enrichment_required"]["missing_values_are_not_zero"])

    def test_test_orders_are_excluded_by_default_and_cannot_support_live_facts(self):
        payload = dict(self.payload)
        payload.pop("include_test_orders")
        output = self._run(payload)
        self.assertFalse(output["ready"])
        self.assertEqual(output["excluded_test_order_ids"], ["gid://shopify/Order/1001"])
        self.assertEqual(output["currency_summary"], [])

    def test_failed_transaction_and_money_difference_are_visible_blockers(self):
        payload = copy.deepcopy(self.payload)
        payload["transactions"][0]["status"] = "FAILURE"
        output = self._run(payload)
        self.assertFalse(output["ready"])
        review = output["order_reviews"][0]
        self.assertTrue(any("successful_collection_vs_total_received" in item for item in review["exceptions"]))
        self.assertTrue(any("pending_or_failed_transactions" in item for item in review["exceptions"]))
        self.assertTrue(output["founder_briefing"]["risk_signals"])

    def test_orphans_duplicates_entity_and_evidence_fail_closed(self):
        payload = copy.deepcopy(self.payload)
        payload["transactions"][0]["order_id"] = "missing-order"
        output = self._run(payload)
        self.assertFalse(output["ready"])
        self.assertEqual(output["orphan_transaction_ids"], ["gid://shopify/OrderTransaction/2001"])

        payload = copy.deepcopy(self.payload)
        payload["refunds"].append(copy.deepcopy(payload["refunds"][0]))
        output = self._run(payload)
        self.assertFalse(output["ready"])
        self.assertEqual(output["duplicate_inputs"]["refund_ids"], ["gid://shopify/Refund/3001"])

        payload = copy.deepcopy(self.payload)
        payload["orders"][0]["entity_id"] = "another"
        with self.assertRaisesRegex(ValueError, "outside statutory entity"):
            self._run(payload)

        payload = copy.deepcopy(self.payload)
        payload["orders"][0].pop("evidence")
        with self.assertRaisesRegex(ValueError, "requires source_file and batch_id evidence"):
            self._run(payload)


if __name__ == "__main__":
    unittest.main()
