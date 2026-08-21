from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from src.box_runtime import BoxRuntime
from src.default_connectors import build_box_connector_registry
from src.default_services import build_default_service_registry


ROOT = Path(__file__).resolve().parents[1]


class DtcIntegrationServiceTests(unittest.TestCase):
    def setUp(self):
        self.runtime = BoxRuntime(
            ROOT / "examples" / "boxes" / "cn_dtc_shopify_stripe_store.json", ROOT / "packs",
        )
        connectors = build_box_connector_registry(self.runtime)
        shopify_request = json.loads(
            (ROOT / "packs" / "connectors" / "shopify" / "fixture-orders.json").read_text()
        )
        shopify = connectors.dispatch(self.runtime, "shopify.orders", shopify_request)["batch"]["datasets"]
        stripe_request = json.loads(
            (ROOT / "packs" / "connectors" / "stripe" / "fixture-balance-transactions.json").read_text()
        )
        stripe_request["objects"][0].update({"amount": 10450, "fee": 333, "net": 10117})
        stripe_request["objects"][1].update({"amount": -2090, "fee": 0, "net": -2090})
        stripe = connectors.dispatch(
            self.runtime, "stripe.balance_transactions", stripe_request,
        )["batch"]["datasets"]["payments.stripe_balance_transactions"]
        evidence = {"source_file": "processor-links.csv", "batch_id": "links-demo"}
        self.payload = {
            "shopify_transactions": shopify["commerce.shopify_transactions"],
            "stripe_balance_transactions": stripe,
            "processor_links": [
                {
                    "entity_id": "cn_dtc_company",
                    "shopify_transaction_id": "gid://shopify/OrderTransaction/2001",
                    "stripe_source_object_id": "ch_demo_001",
                    "evidence": evidence,
                },
                {
                    "entity_id": "cn_dtc_company",
                    "shopify_transaction_id": "gid://shopify/OrderTransaction/2002",
                    "stripe_source_object_id": "re_demo_001",
                    "evidence": evidence,
                },
            ],
            "currency_minor_units": {"USD": 2},
        }
        self.services = build_default_service_registry()

    def _run(self, payload=None):
        return self.services.dispatch(
            self.runtime,
            "dtc.reconcile_shopify_stripe_activity",
            payload or self.payload,
            entity_id="cn_dtc_company",
        )["output"]

    def test_explicit_links_reconcile_collection_refund_fees_and_net(self):
        output = self._run()
        self.assertTrue(output["ready"], output)
        self.assertTrue(all(row["status"] == "matched" for row in output["reconciliation"]))
        summary = output["currency_summary"][0]
        self.assertEqual(summary["shopify_collection_minor"], 10450)
        self.assertEqual(summary["shopify_refund_minor"], 2090)
        self.assertEqual(summary["stripe_gross_minor"], 8360)
        self.assertEqual(summary["stripe_fee_minor"], 333)
        self.assertEqual(summary["stripe_net_minor"], 8027)
        self.assertEqual(summary["matched_count"], 2)
        self.assertFalse(output["ready_for_revenue_recognition"])
        self.assertFalse(output["posting_performed"])
        self.assertTrue(output["candidate_only"])

    def test_missing_link_amount_mismatch_and_duplicate_link_are_blockers(self):
        payload = copy.deepcopy(self.payload)
        payload["processor_links"] = payload["processor_links"][:1]
        output = self._run(payload)
        self.assertFalse(output["ready"])
        self.assertEqual(output["reconciliation"][1]["status"], "missing_processor_link")

        payload = copy.deepcopy(self.payload)
        payload["stripe_balance_transactions"][0]["amount_minor"] = 10000
        payload["stripe_balance_transactions"][0]["net_minor"] = 9667
        output = self._run(payload)
        self.assertFalse(output["ready"])
        self.assertEqual(output["reconciliation"][0]["status"], "amount_currency_or_category_mismatch")

        payload = copy.deepcopy(self.payload)
        payload["processor_links"].append(copy.deepcopy(payload["processor_links"][0]))
        output = self._run(payload)
        self.assertFalse(output["ready"])
        self.assertEqual(
            output["duplicate_inputs"]["linked_shopify_transaction_ids"],
            ["gid://shopify/OrderTransaction/2001"],
        )

    def test_currency_exponent_is_explicit_and_precision_fails_closed(self):
        payload = copy.deepcopy(self.payload)
        payload["currency_minor_units"] = {}
        with self.assertRaisesRegex(ValueError, "non-empty"):
            self._run(payload)
        payload = copy.deepcopy(self.payload)
        payload["currency_minor_units"] = {"USD": 0}
        with self.assertRaisesRegex(ValueError, "more precision"):
            self._run(payload)

    def test_failed_shopify_transaction_is_excluded_not_treated_as_cash(self):
        payload = copy.deepcopy(self.payload)
        payload["shopify_transactions"][0]["status"] = "FAILURE"
        payload["processor_links"] = payload["processor_links"][1:]
        output = self._run(payload)
        self.assertTrue(output["ready"], output)
        self.assertEqual(
            output["excluded_transaction_ids"], ["gid://shopify/OrderTransaction/2001"],
        )
        self.assertEqual(output["currency_summary"][0]["shopify_collection_minor"], 0)


if __name__ == "__main__":
    unittest.main()
