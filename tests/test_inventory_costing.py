import unittest
from pathlib import Path

from src.box_runtime import BoxRuntime
from src.default_services import build_default_service_registry


ROOT = Path(__file__).resolve().parents[1]


def event(event_id, quantity, unit_cost=None, occurred_at="2026-07-01"):
    row = {
        "id": event_id,
        "entity_id": "cn_dtc_company",
        "sku": "SKU-1",
        "warehouse": "WH-1",
        "currency": "CNY",
        "occurred_at": occurred_at,
        "quantity": quantity,
        "evidence": [event_id],
    }
    if unit_cost is not None:
        row["unit_cost"] = unit_cost
    return row


class InventoryCostingTests(unittest.TestCase):
    def setUp(self):
        self.runtime = BoxRuntime(ROOT / "examples" / "boxes" / "cn_dtc_store.json", ROOT / "packs")
        self.registry = build_default_service_registry()

    def test_fifo_consumes_oldest_layers(self):
        result = self.registry.dispatch(
            self.runtime,
            "commerce.calculate_inventory_cost",
            {
                "method": "FIFO",
                "opening_layers": [event("OPEN", 10, 5, "2026-06-30")],
                "receipts": [event("REC", 10, 7, "2026-07-10")],
                "fulfillments": [event("SHIP", 12, occurred_at="2026-07-20")],
            },
            entity_id="cn_dtc_company",
        )["output"]
        self.assertTrue(result["ready"])
        self.assertEqual(result["fulfillment_costs"][0]["cost"], 64)
        self.assertEqual(result["ending_inventory"][0]["remaining_quantity"], 8)
        self.assertEqual(result["ending_inventory"][0]["ending_value"], 56)
        self.assertFalse(result["posting_performed"])

    def test_weighted_average_uses_current_average(self):
        result = self.registry.dispatch(
            self.runtime,
            "commerce.calculate_inventory_cost",
            {
                "method": "WEIGHTED_AVERAGE",
                "opening_layers": [event("OPEN", 10, 5, "2026-06-30")],
                "receipts": [event("REC", 10, 7, "2026-07-10")],
                "fulfillments": [event("SHIP", 12, occurred_at="2026-07-20")],
            },
            entity_id="cn_dtc_company",
        )["output"]
        self.assertTrue(result["ready"])
        self.assertEqual(result["fulfillment_costs"][0]["average_unit_cost"], 6)
        self.assertEqual(result["fulfillment_costs"][0]["cost"], 72)
        self.assertEqual(result["ending_inventory"][0]["ending_value"], 48)

    def test_negative_inventory_blocks_costing(self):
        result = self.registry.dispatch(
            self.runtime,
            "commerce.calculate_inventory_cost",
            {
                "opening_layers": [event("OPEN", 2, 5)],
                "fulfillments": [event("SHIP", 3, occurred_at="2026-07-20")],
            },
            entity_id="cn_dtc_company",
        )["output"]
        self.assertFalse(result["ready"])
        self.assertEqual(result["issues"][0]["reason"], "negative inventory would result")
        self.assertFalse(result["fulfillment_costs"])

    def test_foreign_currency_requires_translation_before_statutory_costing(self):
        foreign = event("OPEN", 2, 5)
        foreign["currency"] = "USD"
        with self.assertRaisesRegex(ValueError, "requires approved translation"):
            self.registry.dispatch(
                self.runtime,
                "commerce.calculate_inventory_cost",
                {"opening_layers": [foreign]},
                entity_id="cn_dtc_company",
            )


if __name__ == "__main__":
    unittest.main()
