from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from src.box_runtime import BoxRuntime
from src.default_connectors import build_box_connector_registry
from src.default_services import build_default_service_registry


ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / "packs" / "connectors" / "shipbob"
BOX = ROOT / "examples" / "boxes" / "us_dtc_shopify_stripe_shipbob_c_corp.json"


class ShipBobServicesTests(unittest.TestCase):
    def setUp(self):
        self.runtime = BoxRuntime(BOX, ROOT / "packs")
        fixture = json.loads((PACK / "fixture-fulfillment.json").read_text(encoding="utf-8"))
        batch = build_box_connector_registry(self.runtime).dispatch(
            self.runtime, "shipbob.fulfillment", fixture,
        )["batch"]
        datasets = batch["datasets"]
        self.payload = {
            "orders": datasets["commerce.shipbob_orders"],
            "shipments": datasets["commerce.shipbob_shipments"],
            "returns": datasets["commerce.shipbob_returns"],
            "return_items": datasets["commerce.shipbob_return_items"],
        }
        self.services = build_default_service_registry()

    def _run(self, payload=None):
        return self.services.dispatch(
            self.runtime, "shipbob.summarize_fulfillment_evidence",
            payload or self.payload, entity_id="us_dtc_company",
        )["output"]

    def test_summary_is_deterministic_candidate_without_posting_or_inventory_action(self):
        output = self._run()
        self.assertTrue(output["ready"], output)
        self.assertEqual(output["counts"], {
            "orders": 1, "shipments": 1, "returns": 1, "return_items": 1,
        })
        self.assertEqual(output["fulfillment_invoice_summary"], [
            {"currency": "USD", "amount": "7.77"},
        ])
        self.assertEqual(output["return_disposition_candidates"], [{
            "warehouse": "US-WEST-3PL", "sku": "SKU-TSHIRT-BLUE",
            "action": "Restock", "quantity": 1,
        }])
        self.assertFalse(output["revenue_recognition_performed"])
        self.assertFalse(output["inventory_adjustment_performed"])
        self.assertFalse(output["posting_performed"])
        self.assertFalse(output["external_actions_performed"])

    def test_cross_window_return_reference_is_visible_but_not_a_blocker(self):
        payload = copy.deepcopy(self.payload)
        payload["shipments"] = []
        payload["orders"] = []
        output = self._run(payload)
        self.assertTrue(output["ready"], output)
        self.assertEqual(
            output["cross_window_return_references"],
            [payload["returns"][0]["original_shipment_key"]],
        )
        self.assertEqual(output["blockers"], [])

    def test_orphan_shipment_or_return_item_blocks_and_entity_mismatch_rejects(self):
        payload = copy.deepcopy(self.payload)
        payload["shipments"][0]["order_key"] = "missing-order"
        output = self._run(payload)
        self.assertFalse(output["ready"])
        self.assertEqual(output["structural_exceptions"]["missing_order_keys"], ["missing-order"])

        payload = copy.deepcopy(self.payload)
        payload["return_items"][0]["return_key"] = "missing-return"
        output = self._run(payload)
        self.assertFalse(output["ready"])
        self.assertEqual(output["structural_exceptions"]["missing_return_keys"], ["missing-return"])

        payload = copy.deepcopy(self.payload)
        payload["orders"][0]["entity_id"] = "other"
        with self.assertRaisesRegex(ValueError, "statutory entity"):
            self._run(payload)


if __name__ == "__main__":
    unittest.main()
