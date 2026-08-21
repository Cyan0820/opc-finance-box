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
PACK = ROOT / "packs" / "connectors" / "shopify"


class ShopifyConnectorTests(unittest.TestCase):
    def setUp(self):
        self.runtime = BoxRuntime(
            ROOT / "examples" / "boxes" / "cn_dtc_shopify_stripe_store.json", ROOT / "packs",
        )
        self.registry = build_box_connector_registry(self.runtime)
        self.fixture = json.loads((PACK / "fixture-orders.json").read_text(encoding="utf-8"))

    def test_fixture_contract_preserves_order_transaction_refund_and_multicurrency(self):
        report = run_connector_contract_test(
            self.registry, self.runtime, "shopify.orders", self.fixture,
            expected_minimum_counts={
                "commerce.shopify_orders": 1,
                "commerce.shopify_transactions": 2,
                "commerce.shopify_refunds": 1,
            },
        )
        self.assertTrue(report["passed"], report)
        batch = self.registry.dispatch(self.runtime, "shopify.orders", self.fixture)["batch"]
        order = batch["datasets"]["commerce.shopify_orders"][0]
        self.assertEqual(order["destination_country"], "DE")
        self.assertEqual(order["money"]["totalPriceSet"]["shop_money"], {
            "amount": "104.50", "currency": "USD",
        })
        self.assertEqual(order["money"]["totalPriceSet"]["presentment_money"], {
            "amount": "96.14", "currency": "EUR",
        })
        transaction = batch["datasets"]["commerce.shopify_transactions"][1]
        self.assertEqual(transaction["kind"], "REFUND")
        self.assertEqual(transaction["status"], "SUCCESS")
        self.assertEqual(transaction["parent_transaction_id"], "gid://shopify/OrderTransaction/2001")
        serialized = json.dumps(batch, ensure_ascii=False).lower()
        for forbidden in ("email", "first_name", "last_name", "address1", "phone"):
            self.assertNotIn(forbidden, serialized)

    def test_catalog_declares_read_only_network_secret_reference(self):
        connector = next(
            item for item in self.registry.catalog(self.runtime) if item["connector_id"] == "shopify.orders"
        )
        self.assertTrue(connector["network_access"])
        self.assertEqual(connector["credential_env"], ["OPC_SHOPIFY_ADMIN_TOKEN"])
        self.assertEqual(connector["dataset_types"], [
            "commerce.shopify_orders", "commerce.shopify_transactions", "commerce.shopify_refunds",
        ])
        contract = json.loads((PACK / "provider-contract.json").read_text(encoding="utf-8"))
        self.assertTrue(contract["access_probe"]["operator_network_opt_in_required"])
        self.assertFalse(contract["access_probe"]["write_scopes_allowed"])
        self.assertFalse(contract["access_probe"]["unrelated_read_scopes_allowed"])
        self.assertTrue(
            contract["access_probe"]["private_receipt_required_for_live_shadow"]
        )
        self.assertEqual(contract["access_probe"]["receipt_maximum_age_days"], 30)

    def test_inline_secret_entity_override_and_bad_store_domain_are_rejected(self):
        request = dict(self.fixture, access_token="private")
        with self.assertRaisesRegex(ConnectorError, "must not be passed"):
            self.registry.dispatch(self.runtime, "shopify.orders", request)
        request = dict(self.fixture, default_entity_id="another")
        with self.assertRaisesRegex(ConnectorError, "valid default_entity_id"):
            self.registry.dispatch(self.runtime, "shopify.orders", request)
        request = dict(self.fixture, shop_domain="shop.myshopify.com.attacker.test")
        with self.assertRaisesRegex(ConnectorError, "myshopify.com"):
            self.registry.dispatch(self.runtime, "shopify.orders", request)

    def test_fetch_paginates_retries_checks_version_and_never_returns_secret(self):
        definition = next(item for item in self.registry.definitions() if item.connector_id == "shopify.orders")
        order = self.fixture["objects"][0]
        responses = [
            HttpResponse(429, {}, b"private rate limit body"),
            HttpResponse(200, {"X-Shopify-API-Version": "2026-07"}, json.dumps({
                "data": {"orders": {"nodes": [order], "pageInfo": {
                    "hasNextPage": True, "endCursor": "NEXT-1",
                }}},
            }).encode()),
            HttpResponse(200, {"X-Shopify-API-Version": "2026-07"}, json.dumps({
                "data": {"orders": {"nodes": [], "pageInfo": {
                    "hasNextPage": False, "endCursor": "NEXT-2",
                }}},
            }).encode()),
        ]
        calls, sleeps = [], []

        def transport(request):
            calls.append(request)
            return responses.pop(0)

        definition.handler.__globals__["HTTP_TRANSPORT"] = transport
        definition.handler.__globals__["HTTP_SLEEPER"] = sleeps.append
        request = {
            "mode": "fetch", "default_entity_id": "cn_dtc_company",
            "shop_domain": "opc-demo.myshopify.com",
            "created_at_gte": "2026-08-01T00:00:00Z",
            "created_at_lt": "2026-09-01T00:00:00Z",
        }
        with patch.dict("os.environ", {"OPC_SHOPIFY_ADMIN_TOKEN": "shpat_ENV_PRIVATE"}, clear=False):
            result = self.registry.dispatch(self.runtime, "shopify.orders", request)
        self.assertTrue(result["batch"]["quality"]["ready"])
        self.assertEqual(result["batch"]["source"]["page_count"], 2)
        self.assertEqual(result["batch"]["source"]["retry_count"], 1)
        self.assertEqual(sleeps, [1])
        self.assertEqual(json.loads(calls[-1].body)["variables"]["after"], "NEXT-1")
        self.assertEqual(calls[-1].url, "https://opc-demo.myshopify.com/admin/api/2026-07/graphql.json")
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("shpat_ENV_PRIVATE", serialized)
        self.assertNotIn("X-Shopify-Access-Token", serialized)

    def test_bad_nested_money_rejects_whole_order_without_orphan_records(self):
        request = json.loads(json.dumps(self.fixture))
        request["objects"][0]["transactions"][0]["amountSet"]["shopMoney"]["amount"] = "NaN"
        result = self.registry.dispatch(self.runtime, "shopify.orders", request)["batch"]
        self.assertFalse(result["quality"]["ready"])
        self.assertEqual(result["quality"]["record_count"], 0)
        self.assertEqual(result["quality"]["rejected_count"], 1)

    def test_fetch_window_requires_timezone_and_strict_order(self):
        definition = self.registry.definition("shopify.orders")
        with self.assertRaisesRegex(ConnectorError, "timezone offset"):
            definition.handler({
                "mode": "fetch", "default_entity_id": "cn_dtc_company",
                "shop_domain": "opc-demo.myshopify.com",
                "created_at_gte": "2026-08-01T00:00:00",
                "created_at_lt": "2026-08-02T00:00:00Z",
            }, type("Context", (), {"allowed_entity_ids": frozenset({"cn_dtc_company"})})())
        with self.assertRaisesRegex(ConnectorError, "earlier"):
            definition.handler({
                "mode": "fetch", "default_entity_id": "cn_dtc_company",
                "shop_domain": "opc-demo.myshopify.com",
                "created_at_gte": "2026-08-02T00:00:00Z",
                "created_at_lt": "2026-08-01T00:00:00Z",
            }, type("Context", (), {"allowed_entity_ids": frozenset({"cn_dtc_company"})})())


if __name__ == "__main__":
    unittest.main()
