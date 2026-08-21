import unittest
from pathlib import Path

from src.box_runtime import BoxRuntime
from src.default_services import build_default_service_registry


ROOT = Path(__file__).resolve().parents[1]


class CommerceServicesTests(unittest.TestCase):
    def setUp(self):
        self.runtime = BoxRuntime(ROOT / "examples" / "boxes" / "cn_dtc_store.json", ROOT / "packs")
        self.registry = build_default_service_registry()
        self.orders = [{
            "order_id": "O1",
            "entity_id": "cn_dtc_company",
            "period": "2026-07",
            "channel": "DTC",
            "destination_country": "US",
            "currency": "USD",
            "merchandise_gross_ex_tax": 100,
            "discounts_ex_tax": 10,
            "shipping_income_ex_tax": 5,
            "tax_collected": 9.5,
            "refunds_ex_tax": 20,
            "refunded_tax": 2,
            "cogs": 35,
            "fulfillment_cost": 6,
            "shipping_cost": 8,
        }]

    def test_refund_service_calculates_rate_without_requiring_settlement(self):
        result = self.registry.dispatch(
            self.runtime,
            "commerce.refund_summary",
            {"orders": self.orders},
        )["output"]
        self.assertTrue(result["ready"])
        self.assertEqual(result["refund_summary"][0]["refund_rate_ex_tax"], 0.2)
        self.assertEqual(result["refund_summary"][0]["refunded_order_count"], 1)

    def test_fulfillment_service_does_not_claim_inventory_valuation(self):
        result = self.registry.dispatch(
            self.runtime,
            "commerce.fulfillment_cost_summary",
            {"orders": self.orders},
        )["output"]
        row = result["fulfillment_summary"][0]
        self.assertEqual(row["fulfillment_cost"], 6)
        self.assertEqual(row["shipping_cost"], 8)
        self.assertIn("not a replacement", result["guardrail"])

    def test_destination_service_is_evidence_only(self):
        result = self.registry.dispatch(
            self.runtime,
            "dtc.destination_evidence",
            {"orders": self.orders},
        )["output"]
        row = result["destination_summary"][0]
        self.assertEqual(row["destination_country"], "US")
        self.assertEqual(row["tax_evidence_net"], 7.5)
        self.assertEqual(row["tax_status"], "evidence_only_not_registration_or_tax_due")

    def test_return_service_is_pack_exposed_and_candidate_only(self):
        result = self.registry.dispatch(
            self.runtime,
            "commerce.reconcile_return_inventory",
            {
                "returns": [{
                    "return_id": "R1", "order_id": "O1", "entity_id": "cn_dtc_company",
                    "period": "2026-07", "channel": "DTC", "sku": "SKU-1",
                    "currency": "USD", "authorized_quantity": 1, "refunded_quantity": 1,
                    "refund_amount_ex_tax": 20, "refunded_tax": 2,
                    "evidence": {"source_file": "returns.csv"},
                }],
                "return_receipts": [{
                    "receipt_id": "RR1", "return_id": "R1", "entity_id": "cn_dtc_company",
                    "period": "2026-07", "sku": "SKU-1", "warehouse": "WH-1",
                    "received_quantity": 1, "disposition": "inspection_pending",
                    "evidence": {"source_file": "receipts.csv"},
                }],
            },
        )
        self.assertEqual(result["output"]["review_gate"], "return_disposition_review")
        self.assertTrue(result["output"]["ready"])
        self.assertEqual(result["output"]["reconciliations"][0]["status"], "inspection_pending")
        self.assertFalse(result["output"]["inventory_adjustment_performed"])

    def test_import_landed_cost_service_excludes_import_tax_and_never_posts(self):
        result = self.registry.dispatch(
            self.runtime,
            "commerce.build_import_landed_cost_candidates",
            {
                "import_costs": [{
                    "entry_line_id": "IMP-L1", "import_entry_id": "IMP-1",
                    "entity_id": "cn_dtc_company", "period": "2026-07",
                    "sku": "SKU-1", "warehouse": "WH-1",
                    "origin_country": "CN", "destination_country": "US",
                    "currency": "USD", "quantity": 10,
                    "declared_value": 100, "inbound_freight": 10,
                    "insurance": 2, "customs_duty": 20, "import_tax": 13,
                    "brokerage": 3, "evidence": {"source_file": "entry.pdf"},
                }],
            },
        )["output"]
        self.assertTrue(result["ready"])
        self.assertEqual(result["review_gate"], "import_landed_cost_policy")
        self.assertEqual(result["candidates"][0]["inventory_landed_cost_candidate"], 135.0)
        self.assertEqual(result["candidates"][0]["import_tax_evidence"], 13.0)
        self.assertFalse(result["import_tax_recoverability_determined"])
        self.assertFalse(result["inventory_or_ledger_adjustment_performed"])


if __name__ == "__main__":
    unittest.main()
