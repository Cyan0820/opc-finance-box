from __future__ import annotations

import base64
import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.box_runtime import BoxRuntime
from src.connector_http import (
    ConnectorHttpError,
    HttpResponse,
    fetch_woocommerce_order_refund_pages,
)
from src.connector_sdk import ConnectorError
from src.connector_testkit import run_connector_contract_test
from src.default_connectors import build_box_connector_registry


ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / "packs" / "connectors" / "woocommerce"
BOX = ROOT / "examples" / "boxes" / "us_dtc_woocommerce_c_corp.json"


def _response(payload, *, status=200, headers=None):
    return HttpResponse(
        status=status,
        headers=headers or {},
        body=json.dumps(payload).encode("utf-8"),
    )


def _fetch_kwargs():
    return {
        "site_origin": "https://shop.example.com/store",
        "consumer_key": "ck_12345678",
        "consumer_secret": "cs_12345678",
        "modified_after": "2026-07-31T23:59:59.999999Z",
        "modified_before": "2026-09-01T00:00:00.000000Z",
        "refund_after": "2026-07-31T23:59:59.999999Z",
        "refund_before": "2026-09-01T00:00:00.000000Z",
    }


class WooCommerceHttpTests(unittest.TestCase):
    def test_fixed_collection_paths_basic_auth_and_local_pagination(self):
        requests = []
        responses = [
            _response([{"id": 1}], headers={
                "X-WP-Total": "2", "X-WP-TotalPages": "2",
                "Link": '<https://attacker.invalid/steal>; rel="next"',
            }),
            _response([{"id": 2}], headers={
                "X-WP-Total": "2", "X-WP-TotalPages": "2",
            }),
            _response([{"id": 3}], headers={
                "X-WP-Total": "1", "X-WP-TotalPages": "1",
            }),
        ]

        def transport(request):
            requests.append(request)
            return responses.pop(0)

        result = fetch_woocommerce_order_refund_pages(
            **_fetch_kwargs(), page_size=1, max_pages=2,
            transport=transport, sleeper=lambda _: None,
        )
        self.assertEqual(result["order_page_count"], 2)
        self.assertEqual(result["refund_page_count"], 1)
        self.assertEqual(result["order_total"], 2)
        self.assertEqual(result["refund_total"], 1)
        self.assertEqual(len(requests), 3)
        self.assertIn("/store/wp-json/wc/v3/orders?", requests[0].url)
        self.assertIn("orderby=modified", requests[0].url)
        self.assertIn("page=2", requests[1].url)
        self.assertIn("/store/wp-json/wc/v3/refunds?", requests[2].url)
        self.assertIn("orderby=date", requests[2].url)
        self.assertTrue(all("attacker.invalid" not in item.url for item in requests))
        self.assertTrue(all("ck_12345678" not in item.url for item in requests))
        self.assertTrue(all("cs_12345678" not in item.url for item in requests))
        self.assertTrue(all(item.headers["Authorization"].startswith("Basic ") for item in requests))

    def test_retry_limits_and_errors_are_sanitized(self):
        calls = []
        responses = [
            _response({}, status=429, headers={"Retry-After": "0"}),
            _response([], headers={"X-WP-Total": "0", "X-WP-TotalPages": "0"}),
            _response({}, status=500),
            _response([], headers={"X-WP-Total": "0", "X-WP-TotalPages": "0"}),
        ]
        delays = []

        def transport(request):
            calls.append(request)
            return responses.pop(0)

        result = fetch_woocommerce_order_refund_pages(
            **_fetch_kwargs(), transport=transport, sleeper=delays.append,
        )
        self.assertEqual(result["retry_count"], 2)
        self.assertEqual(result["rate_limit_count"], 1)
        self.assertEqual(len(delays), 2)
        self.assertEqual(len(calls), 4)

        def unauthorized(_request):
            return HttpResponse(401, {}, b'{"secret":"private-response"}')

        with self.assertRaises(ConnectorHttpError) as caught:
            fetch_woocommerce_order_refund_pages(
                **_fetch_kwargs(), transport=unauthorized, sleeper=lambda _: None,
            )
        message = str(caught.exception)
        self.assertNotIn("ck_12345678", message)
        self.assertNotIn("cs_12345678", message)
        self.assertNotIn("private-response", message)

    def test_site_credentials_and_pagination_fail_closed(self):
        invalid = (
            {"site_origin": "http://shop.example.com"},
            {"site_origin": "https://127.0.0.1"},
            {"site_origin": "https://localhost.example.com:8443"},
            {"site_origin": "https://user:pass@shop.example.com"},
            {"site_origin": "https://shop.example.com?next=https://attacker.invalid"},
            {"consumer_key": "not-a-key"},
            {"consumer_secret": "not-a-secret"},
            {"page_size": 101},
            {"max_pages": 101},
        )
        for override in invalid:
            kwargs = _fetch_kwargs()
            kwargs.update(override)
            with self.subTest(override=override), self.assertRaises(ConnectorHttpError):
                fetch_woocommerce_order_refund_pages(**kwargs)

        responses = [
            _response([], headers={"X-WP-Total": "10001", "X-WP-TotalPages": "101"}),
        ]
        with self.assertRaisesRegex(ConnectorHttpError, "shorten the interval"):
            fetch_woocommerce_order_refund_pages(
                **_fetch_kwargs(), transport=lambda _: responses.pop(0), sleeper=lambda _: None,
            )


class WooCommerceProviderTests(unittest.TestCase):
    def setUp(self):
        self.runtime = BoxRuntime(BOX, ROOT / "packs")
        self.registry = build_box_connector_registry(self.runtime)
        self.fixture = json.loads((PACK / "fixture-order-refunds.json").read_text(encoding="utf-8"))

    def test_contract_minimizes_customer_product_and_source_identifiers(self):
        report = run_connector_contract_test(
            self.registry,
            self.runtime,
            "woocommerce.order_refund_activity",
            self.fixture,
            expected_minimum_counts={
                "commerce.woocommerce_orders": 2,
                "commerce.woocommerce_refunds": 1,
            },
        )
        self.assertTrue(report["passed"], report)
        batch = self.registry.dispatch(
            self.runtime, "woocommerce.order_refund_activity", self.fixture,
        )["batch"]
        orders = batch["datasets"]["commerce.woocommerce_orders"]
        refunds = batch["datasets"]["commerce.woocommerce_refunds"]
        self.assertEqual(len(orders), 2)
        self.assertEqual(len(refunds), 1)
        self.assertEqual(orders[0]["destination_country"], "US")
        self.assertEqual(orders[0]["line_item_count"], 1)
        self.assertEqual(orders[0]["quantity_total"], 2)
        self.assertEqual(orders[0]["lifetime_refund_total"], "20.00")
        self.assertEqual(refunds[0]["parent_order_key"], orders[0]["woocommerce_order_key"])
        serialized = json.dumps(batch, ensure_ascii=False).lower()
        for forbidden in (
            "private.customer@example.invalid", "100 private street", "private browser agent",
            "private product name", "private-sku-001", "private-wc-transaction-1001",
            "private-order-1001", "private customer complaint", "attacker.invalid",
        ):
            self.assertNotIn(forbidden, serialized)
        source = batch["source"]
        self.assertFalse(source["customer_identity_retained"])
        self.assertFalse(source["product_identity_or_name_retained"])
        self.assertFalse(source["raw_source_ids_retained"])
        self.assertFalse(source["business_write_api_called"])

    def test_fetch_mode_selects_current_entity_site_and_credential_aliases(self):
        request = copy.deepcopy(self.fixture)
        request["mode"] = "fetch"
        request.pop("order_pages")
        request.pop("refund_pages")
        calls = []
        responses = [
            _response([], headers={"X-WP-Total": "0", "X-WP-TotalPages": "0"}),
            _response([], headers={"X-WP-Total": "0", "X-WP-TotalPages": "0"}),
        ]

        def transport(http_request):
            calls.append(http_request)
            return responses.pop(0)

        definition = self.registry.definition("woocommerce.order_refund_activity")
        provider_globals = definition.handler.__globals__
        old_transport = provider_globals["HTTP_TRANSPORT"]
        old_sleeper = provider_globals["HTTP_SLEEPER"]
        provider_globals["HTTP_TRANSPORT"] = transport
        provider_globals["HTTP_SLEEPER"] = lambda seconds: None
        environment = {
            "OPC_WOOCOMMERCE_ENTITY_BINDINGS_JSON": json.dumps({
                "us_dtc_company": {
                    "site_origin": "https://shop.example.com/store",
                    "key_permission": "read",
                    "consumer_key_env": "OPC_WC_US_KEY",
                    "consumer_secret_env": "OPC_WC_US_SECRET",
                },
            }),
            "OPC_WC_US_KEY": "ck_ENTITYKEY123",
            "OPC_WC_US_SECRET": "cs_ENTITYSECRET123",
        }
        try:
            with patch.dict(os.environ, environment, clear=True):
                result = self.registry.dispatch(
                    self.runtime, "woocommerce.order_refund_activity", request,
                )
        finally:
            provider_globals["HTTP_TRANSPORT"] = old_transport
            provider_globals["HTTP_SLEEPER"] = old_sleeper
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(
            call.url.startswith("https://shop.example.com/store/wp-json/wc/v3/")
            for call in calls
        ))
        expected = base64.b64encode(
            b"ck_ENTITYKEY123:cs_ENTITYSECRET123"
        ).decode("ascii")
        self.assertTrue(all(
            call.headers["Authorization"] == f"Basic {expected}" for call in calls
        ))
        serialized = json.dumps(result)
        self.assertNotIn("shop.example.com", serialized)
        self.assertNotIn("ck_ENTITYKEY123", serialized)
        self.assertNotIn("cs_ENTITYSECRET123", serialized)

    def test_provider_contract_keeps_write_permission_claim_honest(self):
        contract = json.loads(
            (PACK / "provider-contract.json").read_text(encoding="utf-8")
        )
        access = contract["access_probe_contract"]
        self.assertEqual(access["receipt_schema"], 2)
        self.assertFalse(access["financial_values_requested"])
        self.assertFalse(access["write_permission_provider_verified"])
        self.assertTrue(access["independent_key_permission_review_required"])
        self.assertTrue(access["current_receipt_required_for_shadow_dispatch"])

    def test_multi_entity_fetch_refuses_legacy_root_credentials(self):
        config = json.loads(BOX.read_text(encoding="utf-8"))
        second = copy.deepcopy(config["entities"][0])
        second["id"] = "us_second_company"
        second["name"] = "Second US company"
        config["entities"].append(second)
        config["features"] = ["feature.multi_entity"]
        config["connector_bindings"] = [
            {
                "connector_pack": "connector.file_import",
                "entity_ids": ["us_dtc_company", "us_second_company"],
            },
            {
                "connector_pack": "connector.woocommerce",
                "entity_ids": ["us_dtc_company", "us_second_company"],
            },
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "woocommerce-multi.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            runtime = BoxRuntime(config_path, ROOT / "packs")
            registry = build_box_connector_registry(runtime)
            request = copy.deepcopy(self.fixture)
            request["mode"] = "fetch"
            request.pop("order_pages")
            request.pop("refund_pages")
            with patch.dict(os.environ, {
                "OPC_WOOCOMMERCE_SITE_ORIGIN": "https://shop.example.com",
                "OPC_WOOCOMMERCE_CONSUMER_KEY": "ck_LEGACYKEY123",
                "OPC_WOOCOMMERCE_CONSUMER_SECRET": "cs_LEGACYSECRET123",
            }, clear=True), self.assertRaisesRegex(
                ConnectorError, "OPC_WOOCOMMERCE_ENTITY_BINDINGS_JSON",
            ):
                registry.dispatch(
                    runtime, "woocommerce.order_refund_activity", request,
                )

    def test_secret_entity_window_and_duplicate_controls_fail_closed(self):
        request = copy.deepcopy(self.fixture)
        request["consumer_secret"] = "inline-private"
        with self.assertRaisesRegex(ConnectorError, "must not be passed"):
            self.registry.dispatch(self.runtime, "woocommerce.order_refund_activity", request)

        request = copy.deepcopy(self.fixture)
        request["default_entity_id"] = "other"
        with self.assertRaisesRegex(ConnectorError, "default_entity_id"):
            self.registry.dispatch(self.runtime, "woocommerce.order_refund_activity", request)

        request = copy.deepcopy(self.fixture)
        request["interval_end"] = "2026-09-02T00:00:01Z"
        with self.assertRaisesRegex(ConnectorError, "31 days"):
            self.registry.dispatch(self.runtime, "woocommerce.order_refund_activity", request)

        request = copy.deepcopy(self.fixture)
        request["order_pages"][0].append(copy.deepcopy(request["order_pages"][0][0]))
        result = self.registry.dispatch(
            self.runtime, "woocommerce.order_refund_activity", request,
        )["batch"]
        self.assertFalse(result["quality"]["ready"])
        self.assertEqual(len(result["quality"]["duplicate_business_keys"]), 1)

        request = copy.deepcopy(self.fixture)
        request["refund_pages"][0][0]["line_items"] = "not-a-list"
        result = self.registry.dispatch(
            self.runtime, "woocommerce.order_refund_activity", request,
        )["batch"]
        self.assertFalse(result["quality"]["ready"])
        self.assertEqual(result["quality"]["rejected_count"], 1)
        self.assertEqual(len(result["quality"]["rejected_rows"]), 1)


if __name__ == "__main__":
    unittest.main()
