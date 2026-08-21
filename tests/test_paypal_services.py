from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from src.box_runtime import BoxRuntime
from src.default_connectors import build_box_connector_registry
from src.default_services import build_default_service_registry


ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / "packs" / "connectors" / "paypal"
BOX = ROOT / "examples" / "boxes" / "us_dtc_paypal_c_corp.json"


class PayPalServicesTests(unittest.TestCase):
    def setUp(self):
        self.runtime = BoxRuntime(BOX, ROOT / "packs")
        fixture = json.loads((PACK / "fixture-transactions.json").read_text(encoding="utf-8"))
        batch = build_box_connector_registry(self.runtime).dispatch(
            self.runtime, "paypal.transaction_activity", fixture,
        )["batch"]
        self.transactions = batch["datasets"]["payments.paypal_balance_activity"]
        self.services = build_default_service_registry()

    def _run(self, transactions=None):
        return self.services.dispatch(
            self.runtime, "paypal.summarize_transaction_activity",
            {"transactions": self.transactions if transactions is None else transactions},
            entity_id="us_dtc_company",
        )["output"]

    def test_summary_preserves_currency_and_never_posts_or_recognizes_revenue(self):
        output = self._run()
        self.assertTrue(output["ready"], output)
        self.assertEqual(output["transaction_count"], 4)
        self.assertEqual(output["refund_candidate_count"], 1)
        usd = next(item for item in output["currency_summary"] if item["currency"] == "USD")
        self.assertEqual(usd["amount"], "15.00")
        self.assertEqual(usd["fee"], "-3.49")
        self.assertEqual(usd["refund_outflow"], "25.00")
        self.assertEqual(usd["withdrawal_or_transfer_outflow"], "60.00")
        self.assertTrue(output["cross_currency_total_prohibited"])
        self.assertFalse(output["revenue_recognition_performed"])
        self.assertFalse(output["bank_reconciliation_performed"])
        self.assertFalse(output["posting_performed"])
        self.assertFalse(output["external_actions_performed"])

    def test_cross_currency_fee_is_separate_and_missing_refund_reference_is_visible(self):
        rows = copy.deepcopy(self.transactions)
        rows[0]["fee_currency"] = "EUR"
        rows[0]["net_when_same_currency"] = None
        rows[1]["reference_transaction_key"] = None
        output = self._run(rows)
        self.assertTrue(output["ready"], output)
        self.assertEqual(output["cross_currency_fee_count"], 1)
        self.assertEqual(output["reference_review_required_count"], 1)

    def test_arithmetic_duplicate_and_entity_mismatch_fail_closed(self):
        rows = copy.deepcopy(self.transactions)
        rows[0]["net_when_same_currency"] = "999.00"
        output = self._run(rows)
        self.assertFalse(output["ready"])
        self.assertEqual(output["blockers"][0]["code"], "paypal_amount_fee_net_mismatch")

        output = self._run(self.transactions + [copy.deepcopy(self.transactions[0])])
        self.assertFalse(output["ready"])
        self.assertEqual(output["blockers"][0]["code"], "duplicate_paypal_transaction")

        rows = copy.deepcopy(self.transactions)
        rows[0]["entity_id"] = "other"
        with self.assertRaisesRegex(ValueError, "statutory entity"):
            self._run(rows)


if __name__ == "__main__":
    unittest.main()
