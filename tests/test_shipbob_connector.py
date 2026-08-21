from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.box_runtime import BoxRuntime
from src.connector_http import HttpResponse
from src.connector_sdk import ConnectorError
from src.connector_testkit import run_connector_contract_test
from src.default_connectors import build_box_connector_registry


ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / "packs" / "connectors" / "shipbob"
BOX = ROOT / "examples" / "boxes" / "us_dtc_shopify_stripe_shipbob_c_corp.json"


class ShipBobConnectorTests(unittest.TestCase):
    def setUp(self):
        self.runtime = BoxRuntime(BOX, ROOT / "packs")
        self.registry = build_box_connector_registry(self.runtime)
        self.fixture = json.loads((PACK / "fixture-fulfillment.json").read_text(encoding="utf-8"))

    def test_fixture_contract_is_entity_scoped_idempotent_and_pii_minimized(self):
        report = run_connector_contract_test(
            self.registry, self.runtime, "shipbob.fulfillment", self.fixture,
            expected_minimum_counts={
                "commerce.shipbob_orders": 1,
                "commerce.shipbob_shipments": 1,
                "commerce.shipbob_returns": 1,
                "commerce.shipbob_return_items": 1,
            },
        )
        self.assertTrue(report["passed"], report)
        batch = self.registry.dispatch(
            self.runtime, "shipbob.fulfillment", self.fixture,
        )["batch"]
        order = batch["datasets"]["commerce.shipbob_orders"][0]
        shipment = batch["datasets"]["commerce.shipbob_shipments"][0]
        self.assertEqual(order["destination_country"], "US")
        self.assertEqual(shipment["fulfillment_invoice"], {"amount": "7.77", "currency": "USD"})
        for value in (
            order["order_key"], order["order_number_hash"], shipment["shipment_key"],
            shipment["tracking_number_hash"],
        ):
            self.assertRegex(value, r"^[0-9a-f]{64}$")
        serialized = json.dumps(batch, ensure_ascii=False).lower()
        for forbidden in (
            "fixture customer", "fixture@example.invalid", "private street", "private city",
            "555-0100", "private-tracking-001", "private-return-tracking-001",
            "private-barcode", "private note", "private.invalid/photo",
        ):
            self.assertNotIn(forbidden, serialized)
        self.assertFalse(batch["source"]["write_api_called"])

    def test_catalog_declares_fixed_read_only_network_contract(self):
        connector = next(
            item for item in self.registry.catalog(self.runtime)
            if item["connector_id"] == "shipbob.fulfillment"
        )
        self.assertTrue(connector["network_access"])
        self.assertEqual(connector["credential_env"], ["OPC_SHIPBOB_ACCESS_TOKEN"])
        self.assertEqual(connector["sync_window"]["max_incremental_days"], 31)
        self.assertEqual(connector["dataset_types"], [
            "commerce.shipbob_orders", "commerce.shipbob_shipments",
            "commerce.shipbob_returns", "commerce.shipbob_return_items",
        ])

    def test_inline_secret_wrong_entity_and_invalid_window_fail_closed(self):
        with self.assertRaisesRegex(ConnectorError, "must not be passed"):
            self.registry.dispatch(
                self.runtime, "shipbob.fulfillment", dict(self.fixture, access_token="private"),
            )
        with self.assertRaisesRegex(ConnectorError, "valid default_entity_id"):
            self.registry.dispatch(
                self.runtime, "shipbob.fulfillment", dict(self.fixture, default_entity_id="other"),
            )
        with self.assertRaisesRegex(ConnectorError, "31 days"):
            self.registry.dispatch(self.runtime, "shipbob.fulfillment", dict(
                self.fixture,
                interval_start="2026-01-01T00:00:00Z",
                interval_end="2026-03-01T00:00:00Z",
            ))
        with self.assertRaisesRegex(ConnectorError, "timezone offset"):
            self.registry.dispatch(self.runtime, "shipbob.fulfillment", dict(
                self.fixture, interval_start="2026-08-01T00:00:00",
            ))

    def test_out_of_window_or_bad_nested_data_rejects_whole_parent_without_orphans(self):
        request = copy.deepcopy(self.fixture)
        request["orders"][0]["created_date"] = "2026-07-31T23:59:59Z"
        batch = self.registry.dispatch(self.runtime, "shipbob.fulfillment", request)["batch"]
        self.assertFalse(batch["quality"]["ready"])
        self.assertEqual(batch["datasets"]["commerce.shipbob_orders"], [])
        self.assertEqual(batch["datasets"]["commerce.shipbob_shipments"], [])

        request = copy.deepcopy(self.fixture)
        request["returns"][0]["inventory"][0]["quantity"] = -1
        batch = self.registry.dispatch(self.runtime, "shipbob.fulfillment", request)["batch"]
        self.assertFalse(batch["quality"]["ready"])
        self.assertEqual(batch["datasets"]["commerce.shipbob_returns"], [])
        self.assertEqual(batch["datasets"]["commerce.shipbob_return_items"], [])

    def test_fetch_retries_builds_integer_return_cursor_and_never_follows_next_url(self):
        definition = self.registry.definition("shipbob.fulfillment")
        responses = [
            HttpResponse(200, {"Total-Pages": "1"}, json.dumps(self.fixture["orders"]).encode()),
            HttpResponse(429, {"Retry-After": "1"}, b"private response body"),
            HttpResponse(200, {}, json.dumps({
                "items": self.fixture["returns"],
                "next": "https://attacker.invalid/return?Cursor=999",
            }).encode()),
            HttpResponse(200, {}, json.dumps({"items": [], "next": None}).encode()),
        ]
        calls, sleeps = [], []

        def transport(request):
            calls.append(request)
            return responses.pop(0)

        definition.handler.__globals__["HTTP_TRANSPORT"] = transport
        definition.handler.__globals__["HTTP_SLEEPER"] = sleeps.append
        request = {
            "mode": "fetch", "default_entity_id": "us_dtc_company",
            "environment": "sandbox",
            "interval_start": "2026-08-01T00:00:00Z",
            "interval_end": "2026-09-01T00:00:00Z",
            "page_size": 100, "max_pages": 5,
        }
        with patch.dict("os.environ", {"OPC_SHIPBOB_ACCESS_TOKEN": "ENV_PRIVATE_TOKEN"}, clear=False):
            result = self.registry.dispatch(self.runtime, "shipbob.fulfillment", request)
        self.assertTrue(result["batch"]["quality"]["ready"])
        self.assertEqual(result["batch"]["source"]["return_page_count"], 2)
        self.assertEqual(result["batch"]["source"]["retry_count"], 1)
        self.assertEqual(sleeps, [1.0])
        self.assertIn("Cursor=1", calls[1].url)
        self.assertIn("Cursor=2", calls[3].url)
        self.assertNotIn("attacker.invalid", calls[3].url)
        self.assertTrue(all(call.url.startswith("https://sandbox-api.shipbob.com/2026-07/") for call in calls))
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("ENV_PRIVATE_TOKEN", serialized)
        self.assertNotIn("private response body", serialized)

    def test_multi_entity_fetch_requires_alias_binding_and_sends_bound_channel(self):
        config = json.loads(BOX.read_text(encoding="utf-8"))
        second = copy.deepcopy(config["entities"][0])
        second["id"] = "us_second_company"
        second["name"] = "Second company"
        config["entities"].append(second)
        config["connectors"] = ["connector.file_import", "connector.shipbob"]
        config["features"] = ["feature.multi_entity"]
        config["connector_bindings"] = [
            {
                "connector_pack": "connector.file_import",
                "entity_ids": ["us_dtc_company", "us_second_company"],
            },
            {
                "connector_pack": "connector.shipbob",
                "entity_ids": ["us_dtc_company", "us_second_company"],
            },
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "box.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            runtime = BoxRuntime(config_path, ROOT / "packs")
            registry = build_box_connector_registry(runtime)
            definition = registry.definition("shipbob.fulfillment")
            request = {
                "mode": "fetch", "default_entity_id": "us_dtc_company",
                "environment": "production",
                "interval_start": "2026-08-01T00:00:00Z",
                "interval_end": "2026-09-01T00:00:00Z",
                "page_size": 100, "max_pages": 5,
            }
            calls = []
            responses = [
                HttpResponse(200, {"Total-Pages": "1"}, b"[]"),
                HttpResponse(200, {}, b'{"items":[],"next":null}'),
            ]

            def transport(http_request):
                calls.append(http_request)
                return responses.pop(0)

            definition.handler.__globals__["HTTP_TRANSPORT"] = transport
            environment = {
                "OPC_SHIPBOB_ENTITY_BINDINGS_JSON": json.dumps({
                    "us_dtc_company": {
                        "environment": "production",
                        "channel_id": 710,
                        "token_env": "OPC_SHIPBOB_US_TOKEN",
                    },
                    "us_second_company": {
                        "environment": "production",
                        "channel_id": 711,
                        "token_env": "OPC_SHIPBOB_SECOND_TOKEN",
                    },
                }),
                "OPC_SHIPBOB_US_TOKEN": "private-us-token",
                "OPC_SHIPBOB_SECOND_TOKEN": "private-second-token",
            }
            with patch.dict("os.environ", environment, clear=True):
                result = registry.dispatch(
                    runtime, "shipbob.fulfillment", request,
                )
            self.assertTrue(result["batch"]["source"]["network_access_performed"])
            self.assertTrue(result["batch"]["source"]["channel_header_used"])
            self.assertTrue(all(
                item.headers["shipbob_channel_id"] == "710" for item in calls
            ))
            self.assertNotIn('"channel_id"', json.dumps(result))
            with patch.dict(
                "os.environ",
                {"OPC_SHIPBOB_ACCESS_TOKEN": "legacy-root-token"},
                clear=True,
            ), self.assertRaisesRegex(ConnectorError, "ENTITY_BINDINGS_JSON"):
                registry.dispatch(runtime, "shipbob.fulfillment", request)


if __name__ == "__main__":
    unittest.main()
