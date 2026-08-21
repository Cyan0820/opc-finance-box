from __future__ import annotations

import base64
import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.connector_http import (
    ConnectorHttpError, HttpResponse, fetch_paypal_transaction_pages,
)
from src.connector_sdk import ConnectorError
from src.connector_testkit import run_connector_contract_test
from src.default_connectors import build_box_connector_registry
from src.box_runtime import BoxRuntime


ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / "packs" / "connectors" / "paypal"
BOX = ROOT / "examples" / "boxes" / "us_dtc_paypal_c_corp.json"


def _response(payload, status=200, headers=None):
    return HttpResponse(
        status=status, headers=headers or {},
        body=json.dumps(payload).encode("utf-8"),
    )


class PayPalHttpTests(unittest.TestCase):
    def test_oauth_and_local_pagination_ignore_hateoas_links(self):
        requests = []
        responses = [
            _response({"access_token": "ephemeral-token", "token_type": "Bearer"}),
            _response({
                "page": 1, "total_items": 2, "total_pages": 2,
                "transaction_details": [{"transaction_info": {"transaction_id": "one"}}],
                "links": [{"href": "https://attacker.invalid/steal", "rel": "next"}],
            }),
            _response({
                "page": 2, "total_items": 2, "total_pages": 2,
                "transaction_details": [{"transaction_info": {"transaction_id": "two"}}],
                "links": [],
            }),
        ]

        def transport(request):
            requests.append(request)
            return responses.pop(0)

        result = fetch_paypal_transaction_pages(
            client_id="client-id", client_secret="client-secret",
            environment="sandbox",
            interval_start="2026-08-01T00:00:00Z",
            interval_end="2026-08-31T23:59:59.999999Z",
            page_size=1, max_pages=2, transport=transport, sleeper=lambda _: None,
        )
        self.assertEqual(result["page_count"], 2)
        self.assertEqual(result["total_items"], 2)
        self.assertTrue(result["oauth_token_exchange_performed"])
        self.assertEqual(requests[0].method, "POST")
        self.assertEqual(requests[0].url, "https://api-m.sandbox.paypal.com/v1/oauth2/token")
        self.assertEqual(requests[1].method, "GET")
        self.assertIn("fields=transaction_info", requests[1].url)
        self.assertIn("balance_affecting_records_only=Y", requests[1].url)
        self.assertIn("page=2", requests[2].url)
        self.assertTrue(all("attacker.invalid" not in request.url for request in requests))
        self.assertEqual(requests[1].headers["Authorization"], "Bearer ephemeral-token")

    def test_retry_and_errors_are_bounded_and_sanitized(self):
        calls = []
        responses = [
            _response({}, status=429, headers={"Retry-After": "0"}),
            _response({"access_token": "ephemeral", "token_type": "bearer"}),
            _response({}, status=500),
            _response({"total_items": 0, "total_pages": 0, "transaction_details": []}),
        ]

        def transport(request):
            calls.append(request)
            return responses.pop(0)

        delays = []
        result = fetch_paypal_transaction_pages(
            client_id="client-id", client_secret="client-secret", environment="production",
            interval_start="2026-08-01T00:00:00Z",
            interval_end="2026-08-31T23:59:59Z",
            transport=transport, sleeper=delays.append,
        )
        self.assertEqual(result["retry_count"], 2)
        self.assertEqual(result["rate_limit_count"], 1)
        self.assertEqual(len(delays), 2)
        self.assertEqual(len(calls), 4)

        def unauthorized(_request):
            return HttpResponse(401, {}, b'{"secret":"response-private-value"}')

        with self.assertRaises(ConnectorHttpError) as caught:
            fetch_paypal_transaction_pages(
                client_id="private-client", client_secret="private-secret", environment="production",
                interval_start="2026-08-01T00:00:00Z",
                interval_end="2026-08-31T23:59:59Z",
                transport=unauthorized, sleeper=lambda _: None,
            )
        message = str(caught.exception)
        self.assertNotIn("private-client", message)
        self.assertNotIn("private-secret", message)
        self.assertNotIn("response-private-value", message)

    def test_limits_fail_closed(self):
        responses = [
            _response({"access_token": "ephemeral", "token_type": "Bearer"}),
            _response({"total_items": 10001, "total_pages": 20, "transaction_details": []}),
        ]
        with self.assertRaisesRegex(ConnectorHttpError, "10,000"):
            fetch_paypal_transaction_pages(
                client_id="client", client_secret="secret", environment="production",
                interval_start="2026-08-01T00:00:00Z",
                interval_end="2026-08-31T23:59:59Z",
                transport=lambda _request: responses.pop(0), sleeper=lambda _: None,
            )
        for kwargs in (
            {"environment": "custom"}, {"client_id": ""}, {"page_size": 501}, {"max_pages": 21},
        ):
            base = {
                "client_id": "client", "client_secret": "secret", "environment": "sandbox",
                "interval_start": "2026-08-01T00:00:00Z",
                "interval_end": "2026-08-31T23:59:59Z",
            }
            base.update(kwargs)
            with self.assertRaises(ConnectorHttpError):
                fetch_paypal_transaction_pages(**base)


class PayPalProviderTests(unittest.TestCase):
    def setUp(self):
        self.runtime = BoxRuntime(BOX, ROOT / "packs")
        self.registry = build_box_connector_registry(self.runtime)
        self.fixture = json.loads((PACK / "fixture-transactions.json").read_text(encoding="utf-8"))

    def test_contract_and_minimized_records(self):
        report = run_connector_contract_test(
            self.registry, self.runtime, "paypal.transaction_activity", self.fixture,
            expected_minimum_counts={"payments.paypal_balance_activity": 4},
        )
        self.assertTrue(report["passed"], report)
        batch = self.registry.dispatch(
            self.runtime, "paypal.transaction_activity", self.fixture,
        )["batch"]
        rows = batch["datasets"]["payments.paypal_balance_activity"]
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0]["net_when_same_currency"], "96.51")
        self.assertTrue(rows[1]["refund_candidate"])
        self.assertEqual(rows[1]["reference_transaction_key"], rows[0]["paypal_transaction_key"])
        serialized = json.dumps(batch, ensure_ascii=False).lower()
        for forbidden in (
            "fixture@example.invalid", "private street", "private product",
            "paypal-private-sale-001", "private-invoice-1001", "attacker.invalid",
        ):
            self.assertNotIn(forbidden, serialized)
        source = batch["source"]
        self.assertFalse(source["payer_identity_retained"])
        self.assertFalse(source["raw_source_ids_retained"])
        self.assertFalse(source["business_write_api_called"])

    def test_fetch_mode_selects_current_entity_credential_aliases(self):
        request = copy.deepcopy(self.fixture)
        request["mode"] = "fetch"
        request["environment"] = "production"
        request.pop("transaction_pages")
        calls = []
        responses = [
            _response({"access_token": "ephemeral", "token_type": "Bearer"}),
            _response({
                "total_items": 0,
                "total_pages": 0,
                "transaction_details": [],
            }),
        ]

        def transport(http_request):
            calls.append(http_request)
            return responses.pop(0)

        definition = self.registry.definition("paypal.transaction_activity")
        provider_globals = definition.handler.__globals__
        old_transport = provider_globals["HTTP_TRANSPORT"]
        old_sleeper = provider_globals["HTTP_SLEEPER"]
        provider_globals["HTTP_TRANSPORT"] = transport
        provider_globals["HTTP_SLEEPER"] = lambda seconds: None
        environment = {
            "OPC_PAYPAL_ENTITY_BINDINGS_JSON": json.dumps({
                "us_dtc_company": {
                    "environment": "production",
                    "app_id": "APPID_1234",
                    "account_id": "2ABCD3EFGH4JK",
                    "client_id_env": "OPC_PAYPAL_US_CLIENT_ID",
                    "client_secret_env": "OPC_PAYPAL_US_CLIENT_SECRET",
                },
            }),
            "OPC_PAYPAL_US_CLIENT_ID": "entity-client",
            "OPC_PAYPAL_US_CLIENT_SECRET": "entity-secret",
        }
        try:
            with patch.dict(os.environ, environment, clear=True):
                result = self.registry.dispatch(
                    self.runtime, "paypal.transaction_activity", request,
                )
        finally:
            provider_globals["HTTP_TRANSPORT"] = old_transport
            provider_globals["HTTP_SLEEPER"] = old_sleeper
        self.assertEqual(result["batch"]["source"]["environment"], "production")
        self.assertEqual(len(calls), 2)
        expected = base64.b64encode(b"entity-client:entity-secret").decode("ascii")
        self.assertEqual(calls[0].headers["Authorization"], f"Basic {expected}")
        serialized = json.dumps(result)
        self.assertNotIn("entity-client", serialized)
        self.assertNotIn("entity-secret", serialized)
        self.assertNotIn("2ABCD3EFGH4JK", serialized)

    def test_provider_contract_declares_current_access_receipt_gate(self):
        contract = json.loads(
            (PACK / "provider-contract.json").read_text(encoding="utf-8")
        )
        access = contract["access_probe_contract"]
        self.assertEqual(access["receipt_schema"], 2)
        self.assertTrue(access["balance_values_requested"])
        self.assertFalse(access["balance_values_retained"])
        self.assertTrue(access["current_receipt_required_for_shadow_dispatch"])
        self.assertTrue(
            contract["entity_credential_binding_required_for_access_probe_and_shadow"]
        )

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
                "connector_pack": "connector.paypal",
                "entity_ids": ["us_dtc_company", "us_second_company"],
            },
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "paypal-multi.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            runtime = BoxRuntime(config_path, ROOT / "packs")
            registry = build_box_connector_registry(runtime)
            request = copy.deepcopy(self.fixture)
            request["mode"] = "fetch"
            request["environment"] = "production"
            request.pop("transaction_pages")
            with patch.dict(os.environ, {
                "OPC_PAYPAL_CLIENT_ID": "legacy-client",
                "OPC_PAYPAL_CLIENT_SECRET": "legacy-secret",
            }, clear=True), self.assertRaisesRegex(
                ConnectorError, "OPC_PAYPAL_ENTITY_BINDINGS_JSON",
            ):
                registry.dispatch(
                    runtime, "paypal.transaction_activity", request,
                )

    def test_secret_entity_window_and_duplicate_controls_fail_closed(self):
        request = copy.deepcopy(self.fixture)
        request["client_secret"] = "inline-private"
        with self.assertRaisesRegex(ConnectorError, "must not be passed"):
            self.registry.dispatch(self.runtime, "paypal.transaction_activity", request)

        request = copy.deepcopy(self.fixture)
        request["default_entity_id"] = "other"
        with self.assertRaisesRegex(ConnectorError, "default_entity_id"):
            self.registry.dispatch(self.runtime, "paypal.transaction_activity", request)

        request = copy.deepcopy(self.fixture)
        request["interval_end"] = "2026-09-02T00:00:01Z"
        with self.assertRaisesRegex(ConnectorError, "31 days"):
            self.registry.dispatch(self.runtime, "paypal.transaction_activity", request)

        request = copy.deepcopy(self.fixture)
        details = request["transaction_pages"][0]["transaction_details"]
        details.append(copy.deepcopy(details[0]))
        result = self.registry.dispatch(
            self.runtime, "paypal.transaction_activity", request,
        )["batch"]
        self.assertFalse(result["quality"]["ready"])
        self.assertEqual(len(result["quality"]["duplicate_business_keys"]), 1)


if __name__ == "__main__":
    unittest.main()
