from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from src.box_runtime import BoxRuntime
from src.default_connectors import build_box_connector_registry
from src.default_services import build_default_service_registry


ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / "packs" / "connectors" / "woocommerce"
BOX = ROOT / "examples" / "boxes" / "us_dtc_woocommerce_c_corp.json"


class WooCommerceServicesTests(unittest.TestCase):
    def setUp(self):
        self.runtime = BoxRuntime(BOX, ROOT / "packs")
        fixture = json.loads((PACK / "fixture-order-refunds.json").read_text(encoding="utf-8"))
        batch = build_box_connector_registry(self.runtime).dispatch(
            self.runtime, "woocommerce.order_refund_activity", fixture,
        )["batch"]
        self.orders = batch["datasets"]["commerce.woocommerce_orders"]
        self.refunds = batch["datasets"]["commerce.woocommerce_refunds"]
        self.services = build_default_service_registry()

    def _run(self, *, orders=None, refunds=None):
        return self.services.dispatch(
            self.runtime,
            "woocommerce.summarize_order_refund_activity",
            {
                "orders": self.orders if orders is None else orders,
                "refunds": self.refunds if refunds is None else refunds,
            },
            entity_id="us_dtc_company",
        )["output"]

    def test_summary_keeps_currencies_separate_and_remains_candidate_only(self):
        output = self._run()
        self.assertTrue(output["ready"], output)
        self.assertEqual(output["order_count"], 2)
        self.assertEqual(output["refund_event_count"], 1)
        self.assertEqual(output["status_counts"], {"pending": 1, "processing": 1})
        usd = next(item for item in output["currency_summary"] if item["currency"] == "USD")
        eur = next(item for item in output["currency_summary"] if item["currency"] == "EUR")
        self.assertEqual(usd["order_total"], "102.20")
        self.assertEqual(usd["order_tax"], "8.00")
        self.assertEqual(usd["window_refund_event_total"], "20.00")
        self.assertEqual(eur["order_total"], "116.00")
        self.assertEqual(eur["window_refund_event_total"], "0")
        self.assertEqual(output["unpaid_or_unconfirmed_order_count"], 1)
        self.assertTrue(output["cross_currency_total_prohibited"])
        self.assertFalse(output["payment_settlement_inferred"])
        self.assertFalse(output["revenue_recognition_performed"])
        self.assertFalse(output["tax_liability_determined"])
        self.assertFalse(output["posting_performed"])
        self.assertFalse(output["external_actions_performed"])

    def test_duplicate_orphan_and_arithmetic_controls_block(self):
        output = self._run(orders=self.orders + [copy.deepcopy(self.orders[0])])
        self.assertFalse(output["ready"])
        self.assertEqual(output["blockers"][0]["code"], "duplicate_woocommerce_business_key")

        refunds = copy.deepcopy(self.refunds)
        refunds[0]["parent_order_key"] = "missing-order"
        output = self._run(refunds=refunds)
        self.assertFalse(output["ready"])
        self.assertEqual(output["blockers"][0]["code"], "woocommerce_refund_parent_order_missing")

        orders = copy.deepcopy(self.orders)
        orders[0]["total_tax"] = "999.00"
        output = self._run(orders=orders)
        self.assertFalse(output["ready"])
        self.assertEqual(output["blockers"][0]["code"], "woocommerce_order_refund_arithmetic_invalid")

    def test_entity_and_evidence_scope_fail_closed(self):
        orders = copy.deepcopy(self.orders)
        orders[0]["entity_id"] = "other"
        with self.assertRaisesRegex(ValueError, "statutory entity"):
            self._run(orders=orders)

        orders = copy.deepcopy(self.orders)
        orders[0]["evidence"] = {}
        with self.assertRaisesRegex(ValueError, "requires source_file and batch_id evidence"):
            self._run(orders=orders)


if __name__ == "__main__":
    unittest.main()
