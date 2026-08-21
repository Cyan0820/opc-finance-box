import unittest

from src.commerce_import_costs import (
    CommerceImportCost,
    ImportCostDataError,
    build_import_landed_cost_candidates,
)


class CommerceImportCostTests(unittest.TestCase):
    @staticmethod
    def _row(**overrides):
        row = {
            "entry_line_id": "LINE-1", "import_entry_id": "ENTRY-1",
            "entity_id": "entity-1", "period": "2026-07", "sku": "SKU-1",
            "warehouse": "WH-A", "origin_country": "CN", "destination_country": "US",
            "currency": "USD", "quantity": 10, "declared_value": 100,
            "inbound_freight": 20, "insurance": 2, "customs_duty": 8,
            "import_tax": 10, "brokerage": 5,
            "evidence": {"source_file": "entry.csv"},
        }
        row.update(overrides)
        return row

    def test_candidate_excludes_import_tax_and_never_posts(self):
        result = build_import_landed_cost_candidates([self._row()], allowed_entity_ids={"entity-1"})
        self.assertTrue(result["ready"])
        candidate = result["candidates"][0]
        self.assertEqual(candidate["inventory_landed_cost_candidate"], 135)
        self.assertEqual(candidate["unit_landed_cost_candidate"], 13.5)
        self.assertEqual(candidate["import_tax_evidence"], 10)
        self.assertFalse(result["import_tax_recoverability_determined"])
        self.assertFalse(result["inventory_or_ledger_adjustment_performed"])

    def test_multiwarehouse_and_currency_never_mix(self):
        result = build_import_landed_cost_candidates([
            self._row(),
            self._row(entry_line_id="LINE-2", warehouse="WH-B", currency="EUR"),
        ])
        self.assertEqual(len(result["candidates"]), 2)
        self.assertEqual({row["warehouse"] for row in result["candidates"]}, {"WH-A", "WH-B"})

    def test_duplicate_and_unknown_entity_block(self):
        result = build_import_landed_cost_candidates(
            [self._row(), self._row()], allowed_entity_ids={"other"},
        )
        self.assertFalse(result["ready"])
        self.assertEqual({issue["type"] for issue in result["issues"]}, {
            "duplicate_import_cost_line", "unknown_legal_entity",
        })

    def test_invalid_country_and_zero_quantity_are_rejected(self):
        with self.assertRaisesRegex(ImportCostDataError, "ISO alpha-2"):
            CommerceImportCost.from_dict(self._row(origin_country="China"))
        with self.assertRaisesRegex(ImportCostDataError, "positive"):
            CommerceImportCost.from_dict(self._row(quantity=0))

    def test_empty_activity_is_ready_and_explicit(self):
        result = build_import_landed_cost_candidates([])
        self.assertTrue(result["ready"])
        self.assertTrue(result["no_import_activity"])


if __name__ == "__main__":
    unittest.main()
