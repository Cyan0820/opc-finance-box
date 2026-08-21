import json
import unittest
from pathlib import Path

from src.commerce import (
    CommerceDataError,
    CommerceOrder,
    CommerceReturnReceipt,
    build_commerce_analysis,
    build_return_inventory_reconciliation,
)


ROOT = Path(__file__).resolve().parents[1]


class CommerceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.demo = json.loads((ROOT / "data" / "commerce_demo.json").read_text(encoding="utf-8"))

    def test_order_metrics_keep_tax_out_of_revenue(self):
        order = CommerceOrder.from_dict(self.demo["orders"][0])
        metrics = order.metrics()
        self.assertEqual(float(metrics["customer_charge"]), 104.5)
        self.assertEqual(float(metrics["net_revenue_ex_tax"]), 95)
        self.assertEqual(float(metrics["tax_evidence_net"]), 9.5)
        self.assertEqual(float(metrics["contribution_before_channel_fees"]), 46)

    def test_demo_orders_reconcile_to_payout(self):
        analysis = build_commerce_analysis(self.demo["orders"], self.demo["settlements"])
        self.assertTrue(analysis["ready"])
        row = analysis["reconciliations"][0]
        self.assertEqual(row["status"], "已核对")
        self.assertEqual(row["net_revenue_ex_tax"], 330)
        self.assertEqual(row["net_processor_inflow"], 363)
        self.assertEqual(row["reported_payout"], 320)
        self.assertEqual(row["contribution_after_channel_fees"], 135)

    def test_payout_equation_difference_is_reported(self):
        settlements = [dict(self.demo["settlements"][0], payout=319)]
        analysis = build_commerce_analysis(self.demo["orders"], settlements)
        row = analysis["reconciliations"][0]
        self.assertEqual(row["status"], "存在差异")
        self.assertEqual(row["payout_difference"], -1)
        self.assertFalse(analysis["ready"])
        self.assertTrue(any(issue["severity"] == "high" for issue in analysis["issues"]))

    def test_missing_settlement_is_a_blocker(self):
        analysis = build_commerce_analysis(self.demo["orders"], [])
        self.assertFalse(analysis["ready"])
        self.assertEqual(analysis["reconciliations"][0]["status"], "缺少订单或结算")

    def test_destination_summary_does_not_claim_tax_due(self):
        analysis = build_commerce_analysis(self.demo["orders"], self.demo["settlements"])
        self.assertEqual({row["destination_country"] for row in analysis["destination_summary"]}, {"US", "DE", "GB"})
        self.assertTrue(all("不等同于应纳税额" in row["tax_status"] for row in analysis["destination_summary"]))

    def test_duplicate_order_ids_block_readiness(self):
        orders = self.demo["orders"] + [dict(self.demo["orders"][0])]
        analysis = build_commerce_analysis(orders, self.demo["settlements"])
        self.assertFalse(analysis["ready"])
        self.assertTrue(any(issue["type"] == "duplicate_order_id" for issue in analysis["issues"]))

    def test_invalid_discount_and_refund_is_rejected(self):
        row = dict(self.demo["orders"][0], discounts_ex_tax=80, refunds_ex_tax=30)
        with self.assertRaises(CommerceDataError):
            CommerceOrder.from_dict(row)

    def test_unknown_legal_entity_blocks_pack_output(self):
        analysis = build_commerce_analysis(
            self.demo["orders"], self.demo["settlements"], allowed_entity_ids={"different_entity"}
        )
        self.assertFalse(analysis["ready"])
        self.assertTrue(any(issue["type"] == "unknown_legal_entity" for issue in analysis["issues"]))


class CommerceReturnInventoryTests(unittest.TestCase):
    @staticmethod
    def _return(**overrides):
        row = {
            "return_id": "RET-1", "order_id": "ORDER-1", "entity_id": "entity-1",
            "period": "2026-07", "channel": "DTC", "sku": "SKU-1", "currency": "USD",
            "authorized_quantity": 2, "refunded_quantity": 2,
            "refund_amount_ex_tax": 20, "refunded_tax": 2,
            "evidence": {"source_file": "returns.csv"},
        }
        row.update(overrides)
        return row

    @staticmethod
    def _receipt(**overrides):
        row = {
            "receipt_id": "RCPT-1", "return_id": "RET-1", "entity_id": "entity-1",
            "period": "2026-07", "sku": "SKU-1", "warehouse": "WH-1",
            "received_quantity": 2, "disposition": "restockable",
            "evidence": {"source_file": "receipts.csv"},
        }
        row.update(overrides)
        return row

    def test_full_return_is_reconciled_and_only_creates_restock_candidate(self):
        result = build_return_inventory_reconciliation(
            [self._return()], [self._receipt()], allowed_entity_ids={"entity-1"},
        )
        self.assertTrue(result["ready"], result)
        self.assertEqual(result["reconciliations"][0]["status"], "reconciled")
        self.assertEqual(result["warehouse_disposition_summary"][0]["received_quantity"], 2)
        self.assertEqual(result["restock_candidates"][0]["candidate_status"], "requires_inventory_review")
        self.assertFalse(result["inventory_adjustment_performed"])
        self.assertFalse(result["refund_posting_performed"])

    def test_refunded_without_physical_receipt_is_high_risk(self):
        result = build_return_inventory_reconciliation([self._return()], [])
        self.assertFalse(result["ready"])
        self.assertEqual(result["reconciliations"][0]["status"], "refunded_without_receipt")
        self.assertEqual(result["issues"][0]["severity"], "high")

    def test_open_authorization_is_visible_but_not_fabricated_as_failure(self):
        result = build_return_inventory_reconciliation(
            [self._return(refunded_quantity=1, refund_amount_ex_tax=10)],
            [self._receipt(received_quantity=1)],
        )
        self.assertTrue(result["ready"])
        self.assertEqual(result["reconciliations"][0]["status"], "open_authorization")
        self.assertEqual(result["issues"][0]["severity"], "warning")

    def test_multiwarehouse_disposition_is_preserved(self):
        receipts = [
            self._receipt(receipt_id="RCPT-1", received_quantity=1, warehouse="WH-A"),
            self._receipt(
                receipt_id="RCPT-2", received_quantity=1, warehouse="WH-B",
                disposition="damaged",
            ),
        ]
        result = build_return_inventory_reconciliation([self._return()], receipts)
        self.assertTrue(result["ready"], result)
        self.assertEqual(
            {(row["warehouse"], row["disposition"]) for row in result["warehouse_disposition_summary"]},
            {("WH-A", "restockable"), ("WH-B", "damaged")},
        )
        self.assertEqual(len(result["restock_candidates"]), 1)

    def test_orphan_receipt_and_cross_entity_are_blocking(self):
        result = build_return_inventory_reconciliation(
            [], [self._receipt(entity_id="entity-2")], allowed_entity_ids={"entity-1"},
        )
        self.assertFalse(result["ready"])
        self.assertEqual(
            {issue["type"] for issue in result["issues"]},
            {"unknown_legal_entity", "orphan_return_receipt"},
        )

    def test_over_received_and_invalid_disposition_are_rejected(self):
        result = build_return_inventory_reconciliation(
            [self._return(authorized_quantity=1, refunded_quantity=1)],
            [self._receipt(received_quantity=2)],
        )
        self.assertFalse(result["ready"])
        self.assertEqual(result["reconciliations"][0]["status"], "over_received")
        with self.assertRaisesRegex(CommerceDataError, "disposition must be one of"):
            CommerceReturnReceipt.from_dict(self._receipt(disposition="sellable"))

    def test_empty_return_activity_is_ready_and_explicit(self):
        result = build_return_inventory_reconciliation([], [])
        self.assertTrue(result["ready"])
        self.assertTrue(result["no_return_activity"])

    def test_order_refund_without_matching_return_detail_requires_policy_exception(self):
        order = {
            "order_id": "ORDER-1", "entity_id": "entity-1", "period": "2026-07",
            "channel": "DTC", "destination_country": "US", "currency": "USD",
            "merchandise_gross_ex_tax": 100, "discounts_ex_tax": 0,
            "shipping_income_ex_tax": 0, "tax_collected": 10,
            "refunds_ex_tax": 20, "refunded_tax": 2, "cogs": 40,
            "fulfillment_cost": 5, "shipping_cost": 5,
        }
        result = build_return_inventory_reconciliation([], [], order_rows=[order])
        self.assertFalse(result["ready"])
        issue = next(item for item in result["issues"] if item["type"] == "return_refund_amount_mismatch")
        self.assertTrue(issue["policy_exception_required"])


if __name__ == "__main__":
    unittest.main()
