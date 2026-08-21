from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import patch

from src.box_runtime import BoxRuntime
from src.connector_http import HttpResponse
from src.connector_sdk import ConnectorError
from src.connector_testkit import run_connector_contract_test
from src.default_connectors import build_box_connector_registry


ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / "packs" / "connectors" / "stripe"


class StripeConnectorTests(unittest.TestCase):
    def setUp(self):
        self.runtime = BoxRuntime(
            ROOT / "examples" / "boxes" / "cn_dtc_stripe_store.json",
            ROOT / "packs",
        )
        self.registry = build_box_connector_registry(self.runtime)

    @staticmethod
    def _fixture(name: str) -> dict:
        return json.loads((PACK / name).read_text(encoding="utf-8"))

    def test_catalog_exposes_two_read_only_stripe_connectors(self):
        catalog = {item["connector_id"]: item for item in self.registry.catalog(self.runtime)}
        self.assertIn("stripe.balance_transactions", catalog)
        self.assertIn("stripe.payouts", catalog)
        self.assertTrue(catalog["stripe.balance_transactions"]["network_access"])
        self.assertEqual(
            catalog["stripe.balance_transactions"]["credential_env"],
            ["OPC_STRIPE_RESTRICTED_KEY"],
        )
        contract = json.loads((PACK / "provider-contract.json").read_text(encoding="utf-8"))
        self.assertEqual(contract["credential_type"], "restricted_api_key_rk_only")
        self.assertEqual(
            contract["access_probe"]["required_read_resources"],
            ["account", "balance_transactions", "payouts"],
        )
        self.assertFalse(contract["access_probe"]["secret_key_allowed"])
        self.assertTrue(
            contract["access_probe"]["private_receipt_required_for_live_shadow"]
        )
        self.assertEqual(contract["connected_account_dispatch_header"], "Stripe-Account")

    def test_balance_fixture_preserves_minor_units_refund_and_evidence(self):
        fixture = self._fixture("fixture-balance-transactions.json")
        report = run_connector_contract_test(
            self.registry,
            self.runtime,
            "stripe.balance_transactions",
            fixture,
            expected_minimum_counts={"payments.stripe_balance_transactions": 3},
        )
        self.assertTrue(report["passed"], report)
        result = self.registry.dispatch(self.runtime, "stripe.balance_transactions", fixture)
        rows = result["batch"]["datasets"]["payments.stripe_balance_transactions"]
        charge = next(row for row in rows if row["balance_transaction_id"] == "txn_demo_charge_001")
        refund = next(row for row in rows if row["balance_transaction_id"] == "txn_demo_refund_001")
        self.assertEqual(charge["amount_minor"], 10000)
        self.assertEqual(charge["fee_minor"], 320)
        self.assertEqual(charge["net_minor"], 9680)
        self.assertEqual(charge["currency"], "USD")
        self.assertEqual(refund["amount_minor"], -2500)
        self.assertEqual(refund["reporting_category"], "refund")
        self.assertNotIn("revenue", charge)
        self.assertEqual(charge["evidence"]["api_version"], "2026-06-24.dahlia")

    def test_payout_fixture_maps_bank_settlement_without_destination_details(self):
        result = self.registry.dispatch(
            self.runtime, "stripe.payouts", self._fixture("fixture-payouts.json"),
        )
        self.assertTrue(result["batch"]["quality"]["ready"])
        payout = result["batch"]["datasets"]["payments.stripe_payouts"][0]
        self.assertEqual(payout["payout_id"], "po_demo_001")
        self.assertEqual(payout["balance_transaction_id"], "txn_demo_payout_001")
        self.assertEqual(payout["status"], "paid")
        self.assertNotIn("destination", payout)

    def test_duplicate_ids_and_failed_payout_are_auditable(self):
        fixture = self._fixture("fixture-payouts.json")
        failed = dict(fixture["objects"][0])
        failed.update({
            "id": "po_demo_failed",
            "status": "failed",
            "failure_code": "account_closed",
            "failure_message": "Demo failure",
        })
        fixture["objects"] = [failed, failed]
        result = self.registry.dispatch(self.runtime, "stripe.payouts", fixture)
        self.assertFalse(result["batch"]["quality"]["ready"])
        self.assertEqual(result["batch"]["quality"]["record_count"], 1)
        self.assertEqual(len(result["batch"]["quality"]["duplicate_business_keys"]), 1)
        payout = result["batch"]["datasets"]["payments.stripe_payouts"][0]
        self.assertEqual(payout["failure_code"], "account_closed")

    def test_entity_scope_and_inline_credentials_fail_before_execution(self):
        fixture = self._fixture("fixture-payouts.json")
        fixture["default_entity_id"] = "another_company"
        with self.assertRaisesRegex(ConnectorError, "valid default_entity_id"):
            self.registry.dispatch(self.runtime, "stripe.payouts", fixture)
        fixture["default_entity_id"] = "cn_dtc_company"
        fixture["api_key"] = "rk_test_inline"
        with self.assertRaisesRegex(ConnectorError, "must not be passed"):
            self.registry.dispatch(self.runtime, "stripe.payouts", fixture)

    def test_fetch_uses_fixed_endpoint_version_pagination_and_env_only(self):
        definition = next(
            item for item in self.registry.definitions()
            if item.connector_id == "stripe.balance_transactions"
        )
        objects = self._fixture("fixture-balance-transactions.json")["objects"][:2]
        responses = [
            HttpResponse(429, {}, b"secret-looking retry body"),
            HttpResponse(200, {}, json.dumps({
                "object": "list", "url": "/v1/balance_transactions",
                "has_more": True, "data": [objects[0]],
            }).encode()),
            HttpResponse(200, {}, json.dumps({
                "object": "list", "url": "/v1/balance_transactions",
                "has_more": False, "data": [objects[1]],
            }).encode()),
        ]
        calls, sleeps = [], []

        def transport(request):
            calls.append(request)
            return responses.pop(0)

        definition.handler.__globals__["HTTP_TRANSPORT"] = transport
        definition.handler.__globals__["HTTP_SLEEPER"] = sleeps.append
        request = {
            "mode": "fetch",
            "default_entity_id": "cn_dtc_company",
            "created_gte": 1786406400,
            "created_lt": 1786665600,
            "stripe_account": "acct_123456789ABC",
        }
        with patch.dict("os.environ", {"OPC_STRIPE_RESTRICTED_KEY": "rk_test_ENV_PRIVATE"}, clear=False):
            result = self.registry.dispatch(self.runtime, "stripe.balance_transactions", request)
        self.assertTrue(result["batch"]["quality"]["ready"])
        self.assertEqual(result["batch"]["source"]["page_count"], 2)
        self.assertEqual(result["batch"]["source"]["retry_count"], 1)
        self.assertEqual(sleeps, [1])
        self.assertTrue(calls[0].url.startswith("https://api.stripe.com/v1/balance_transactions?"))
        self.assertIn("starting_after=txn_demo_charge_001", calls[-1].url)
        self.assertEqual(calls[-1].headers["Stripe-Version"], "2026-06-24.dahlia")
        self.assertEqual(calls[-1].headers["Stripe-Account"], "acct_123456789ABC")
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("rk_test_ENV_PRIVATE", serialized)
        self.assertNotIn("acct_123456789ABC", serialized)
        self.assertNotIn("Authorization", serialized)

    def test_fetch_missing_key_is_sanitized(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(ConnectorError, "credential is missing") as raised:
                self.registry.dispatch(self.runtime, "stripe.payouts", {
                    "mode": "fetch", "default_entity_id": "cn_dtc_company",
                })
        self.assertNotIn("OPC_STRIPE_RESTRICTED_KEY", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
