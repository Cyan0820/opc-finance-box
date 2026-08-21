from __future__ import annotations

import json
import hashlib
import os
import tempfile
import unittest
from pathlib import Path

from src.box_runtime import BoxRuntime
from src.connector_access_probe import (
    ConnectorAccessProbeError,
    initialize_connector_access_request,
    read_private_connector_access_request,
    read_private_connector_access_probe_receipt,
    renew_connector_access_probe_receipt,
    run_connector_access_probe,
    verify_private_connector_access_probe_receipt,
    verify_private_connector_access_probe_receipt_contract,
    verify_private_connector_access_request,
    write_connector_access_probe_receipt,
)
from src.connector_http import HttpResponse


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "examples" / "boxes" / "cn_dtc_shopify_stripe_store.json"


class ConnectorAccessProbeTests(unittest.TestCase):
    def setUp(self):
        self.runtime = BoxRuntime(CONFIG, ROOT / "packs")
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def _write_request(self, name: str, value: dict) -> Path:
        path = (self.root / name).resolve()
        path.write_text(json.dumps(value), encoding="utf-8")
        os.chmod(path, 0o600)
        return path

    def _shopify_request(self) -> dict:
        return {
            "schema_version": 1,
            "pack_id": "connector.shopify",
            "entity_id": "cn_dtc_company",
            "account_binding": {
                "mode": "store_domain",
                "shop_domain": "private-demo.myshopify.com",
            },
        }

    def _stripe_request(self, mode: str = "own_account") -> dict:
        return {
            "schema_version": 1,
            "pack_id": "connector.stripe",
            "entity_id": "cn_dtc_company",
            "account_binding": {
                "mode": mode,
                "account_id": "acct_123456789ABC",
            },
        }

    @staticmethod
    def _environment_binding_request(pack_id: str, entity_id: str) -> dict:
        return {
            "schema_version": 1,
            "pack_id": pack_id,
            "entity_id": entity_id,
            "account_binding": {"mode": "entity_environment_binding"},
        }

    def test_init_writes_private_incomplete_template_without_credentials(self):
        path = (self.root / "private" / "shopify-access.json").resolve()
        result = initialize_connector_access_request(
            self.runtime,
            pack_id="connector.shopify",
            entity_id="cn_dtc_company",
            output=path,
        )
        self.assertTrue(result["written"])
        self.assertTrue(result["template_only"])
        self.assertFalse(result["ready_for_network_probe"])
        self.assertFalse(result["provider_account_returned"])
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        private = read_private_connector_access_request(path)
        self.assertIn("REPLACE_WITH_PRIVATE_STORE", private["account_binding"]["shop_domain"])
        serialized = json.dumps(private)
        self.assertNotIn("token", serialized.lower())
        self.assertNotIn("credential", serialized.lower())

    def test_private_request_enforces_absolute_regular_mode_0600_and_entity_pack(self):
        path = self._write_request("shopify.json", self._shopify_request())
        verified = verify_private_connector_access_request(self.runtime, path)
        self.assertTrue(verified["valid"])
        self.assertFalse(verified["provider_account_returned"])
        self.assertNotIn("private-demo", json.dumps(verified))

        os.chmod(path, 0o644)
        with self.assertRaisesRegex(ConnectorAccessProbeError, "0600"):
            read_private_connector_access_request(path)
        with self.assertRaisesRegex(ConnectorAccessProbeError, "absolute"):
            read_private_connector_access_request(Path("relative.json"))

        outside = self._shopify_request()
        outside["entity_id"] = "outside-entity"
        outside_path = self._write_request("outside.json", outside)
        with self.assertRaisesRegex(ConnectorAccessProbeError, "outside-entity"):
            verify_private_connector_access_request(self.runtime, outside_path)

    def test_probe_without_explicit_network_flag_performs_no_transport_call(self):
        path = self._write_request("shopify.json", self._shopify_request())
        calls = []
        result = run_connector_access_probe(
            self.runtime,
            path,
            allow_network=False,
            environ={"OPC_SHOPIFY_ADMIN_TOKEN": "shpat_private_value"},
            transport=lambda request: calls.append(request),
        )
        self.assertEqual(result["status"], "network_authorization_required")
        self.assertEqual(calls, [])
        self.assertFalse(result["control_boundary"]["network_access_performed"])
        self.assertFalse(result["summary"]["ready_for_private_shadow_request"])

    def test_shopify_probe_binds_store_and_requires_exact_read_only_scope_set(self):
        path = self._write_request("shopify.json", self._shopify_request())
        calls = []

        def transport(request):
            calls.append(request)
            return HttpResponse(
                200,
                {"X-Shopify-API-Version": "2026-07"},
                json.dumps({
                    "data": {"currentAppInstallation": {"accessScopes": [
                        {"handle": "read_orders"},
                        {"handle": "read_all_orders"},
                    ]}},
                }).encode(),
            )

        result = run_connector_access_probe(
            self.runtime,
            path,
            allow_network=True,
            environ={"OPC_SHOPIFY_ADMIN_TOKEN": "shpat_private_value"},
            transport=transport,
            sleeper=lambda seconds: None,
            observed_at="2026-08-16T12:00:00+00:00",
        )
        self.assertEqual(result["status"], "passed")
        self.assertTrue(result["summary"]["ready_for_private_shadow_request"])
        self.assertTrue(result["provider_account_binding"]["verified"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            calls[0].url,
            "https://private-demo.myshopify.com/admin/api/2026-07/graphql.json",
        )
        self.assertEqual(calls[0].method, "POST")
        self.assertEqual(calls[0].headers["X-Shopify-Access-Token"], "shpat_private_value")
        serialized = json.dumps(result)
        self.assertNotIn("private-demo", serialized)
        self.assertNotIn("shpat_private_value", serialized)
        self.assertNotIn("read_all_orders", serialized)
        self.assertNotIn("granted_scopes", serialized)
        self.assertFalse(result["control_boundary"]["raw_provider_responses_returned"])
        self.assertFalse(result["control_boundary"]["financial_values_returned"])
        self.assertFalse(result["control_boundary"]["financial_reconciliation_inferred"])

    def test_shopify_extra_scope_blocks_least_privilege_without_returning_scope_names(self):
        path = self._write_request("shopify.json", self._shopify_request())
        response = HttpResponse(200, {}, json.dumps({
            "data": {"currentAppInstallation": {"accessScopes": [
                {"handle": "read_orders"}, {"handle": "read_customers"},
            ]}},
        }).encode())
        result = run_connector_access_probe(
            self.runtime,
            path,
            allow_network=True,
            environ={"OPC_SHOPIFY_ADMIN_TOKEN": "shpat_private_value"},
            transport=lambda request: response,
            sleeper=lambda seconds: None,
        )
        checks = {item["check_id"]: item for item in result["checks"]}
        self.assertFalse(checks["least_privilege_scope_set"]["passed"])
        self.assertEqual(result["status"], "blocked_provider_access")
        self.assertNotIn("read_customers", json.dumps(result))

    def test_stripe_probe_requires_rk_key_and_checks_account_balance_and_payout_access(self):
        path = self._write_request("stripe.json", self._stripe_request("connected_account"))
        calls = []
        responses = [
            HttpResponse(200, {}, b'{"object":"account","id":"acct_123456789ABC"}'),
            HttpResponse(200, {}, b'{"object":"list","data":[],"has_more":false}'),
            HttpResponse(200, {}, b'{"object":"list","data":[],"has_more":false}'),
        ]

        def transport(request):
            calls.append(request)
            return responses.pop(0)

        result = run_connector_access_probe(
            self.runtime,
            path,
            allow_network=True,
            environ={"OPC_STRIPE_RESTRICTED_KEY": "rk_live_1234567890PRIVATE"},
            transport=transport,
            sleeper=lambda seconds: None,
        )
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["provider"]["environment_mode"], "live")
        self.assertTrue(result["provider"]["connected_account_header_used"])
        self.assertEqual(
            calls[0].url,
            "https://api.stripe.com/v1/accounts/acct_123456789ABC",
        )
        self.assertNotIn("Stripe-Account", calls[0].headers)
        self.assertEqual(calls[1].headers["Stripe-Account"], "acct_123456789ABC")
        self.assertIn("limit=1", calls[1].url)
        self.assertEqual(calls[1].headers["Stripe-Version"], "2026-06-24.dahlia")
        serialized = json.dumps(result)
        self.assertNotIn("acct_123456789ABC", serialized)
        self.assertNotIn("rk_live_1234567890PRIVATE", serialized)

        with self.assertRaisesRegex(ConnectorAccessProbeError, "restricted key") as raised:
            run_connector_access_probe(
                self.runtime,
                path,
                allow_network=True,
                environ={"OPC_STRIPE_RESTRICTED_KEY": "sk_live_PRIVATE_SECRET_VALUE"},
                transport=lambda request: self.fail("secret keys must fail before transport"),
            )
        self.assertNotIn("sk_live_PRIVATE_SECRET_VALUE", str(raised.exception))

    def test_stripe_permission_denial_is_diagnostic_and_never_leaks_response_body(self):
        path = self._write_request("stripe.json", self._stripe_request())
        secret = "rk_test_1234567890PRIVATE"
        responses = [
            HttpResponse(403, {}, f'{{"error":"private {secret}"}}'.encode()),
            HttpResponse(200, {}, b'{"object":"list","data":[],"has_more":false}'),
            HttpResponse(403, {}, b'{"error":"private payout denial"}'),
        ]
        result = run_connector_access_probe(
            self.runtime,
            path,
            allow_network=True,
            environ={"OPC_STRIPE_RESTRICTED_KEY": secret},
            transport=lambda request: responses.pop(0),
            sleeper=lambda seconds: None,
        )
        self.assertEqual(result["status"], "blocked_provider_access")
        checks = {item["check_id"]: item for item in result["checks"]}
        self.assertFalse(checks["provider_account_binding"]["passed"])
        self.assertTrue(checks["balance_transactions_read"]["passed"])
        self.assertFalse(checks["payouts_read"]["passed"])
        serialized = json.dumps(result)
        self.assertNotIn(secret, serialized)
        self.assertNotIn("private payout denial", serialized)
        self.assertFalse(result["summary"]["ready_for_private_shadow_request"])

    def test_stripe_own_account_binding_uses_current_account_endpoint(self):
        path = self._write_request("stripe.json", self._stripe_request("own_account"))
        calls = []
        responses = [
            HttpResponse(200, {}, b'{"object":"account","id":"acct_123456789ABC"}'),
            HttpResponse(200, {}, b'{"object":"list","data":[],"has_more":false}'),
            HttpResponse(200, {}, b'{"object":"list","data":[],"has_more":false}'),
        ]

        def transport(request):
            calls.append(request)
            return responses.pop(0)

        result = run_connector_access_probe(
            self.runtime,
            path,
            allow_network=True,
            environ={"OPC_STRIPE_RESTRICTED_KEY": "rk_test_1234567890PRIVATE"},
            transport=transport,
            sleeper=lambda seconds: None,
        )
        self.assertEqual(result["status"], "passed")
        self.assertEqual(calls[0].url, "https://api.stripe.com/v1/account")
        self.assertTrue(all("Stripe-Account" not in call.headers for call in calls))

    def test_wise_probe_uses_entity_binding_and_reads_no_financial_statement(self):
        runtime = BoxRuntime(
            ROOT / "examples" / "boxes" / "sg_dtc_wise_store.json",
            ROOT / "packs",
        )
        request_path = self._write_request(
            "wise.json",
            self._environment_binding_request("connector.wise", "sg_store"),
        )
        bindings = json.dumps({
            "sg_store": {
                "profile_id": 123456,
                "business_name": "OPC Wise Demo Pte Ltd",
                "access_contract": "personal_token_eligible",
                "balances": {"SGD": {
                    "balance_id": 987654,
                    "account_reference_masked": "Wise SGD ••7654",
                }},
            },
        })
        responses = [
            HttpResponse(200, {}, b'{"id":123456,"type":"BUSINESS","businessName":"OPC Wise Demo Pte Ltd"}'),
            HttpResponse(200, {}, b'{"id":987654,"currency":"SGD","type":"STANDARD","amount":{"value":999999.99}}'),
        ]
        calls = []

        def transport(request):
            calls.append(request)
            return responses.pop(0)

        environment = {
            "OPC_WISE_ACCESS_TOKEN": "wise_private_token",
            "OPC_WISE_ENTITY_BINDINGS_JSON": bindings,
        }
        result = run_connector_access_probe(
            runtime,
            request_path,
            allow_network=True,
            environ=environment,
            transport=transport,
            sleeper=lambda seconds: None,
            observed_at="2026-08-16T12:00:00+00:00",
        )
        self.assertEqual(result["schema_version"], 2)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(len(calls), 2)
        self.assertTrue(all("balance-statements" not in call.url for call in calls))
        self.assertEqual(
            result["credential_reference"]["env_names"],
            ["OPC_WISE_ACCESS_TOKEN", "OPC_WISE_ENTITY_BINDINGS_JSON"],
        )
        self.assertFalse(result["provider"]["financial_values_requested"])
        serialized = json.dumps(result, ensure_ascii=False)
        for private in (
            "wise_private_token", "123456", "987654", "OPC Wise Demo Pte Ltd",
            "999999.99",
        ):
            self.assertNotIn(private, serialized)

        receipt_path = (self.root / "wise-receipt.json").resolve()
        responses.extend([
            HttpResponse(200, {}, b'{"id":123456,"type":"BUSINESS","businessName":"OPC Wise Demo Pte Ltd"}'),
            HttpResponse(200, {}, b'{"id":987654,"currency":"SGD","type":"STANDARD"}'),
        ])
        write_connector_access_probe_receipt(
            runtime,
            request_path,
            receipt_path,
            allow_network=True,
            environ=environment,
            transport=lambda request: responses.pop(0),
            sleeper=lambda seconds: None,
            observed_at="2026-08-16T12:00:00+00:00",
        )
        verified = verify_private_connector_access_probe_receipt(
            runtime,
            request_path,
            receipt_path,
            as_of="2026-08-16T13:00:00+00:00",
            environ=environment,
        )
        self.assertEqual(verified["schema_version"], 2)
        unrelated_binding = json.loads(bindings)
        unrelated_binding["unrelated_entity"] = {
            "profile_id": 444444,
            "business_name": "Unrelated Entity",
            "access_contract": "wise_partner_approved",
            "balances": {"USD": {
                "balance_id": 555555,
                "account_reference_masked": "Wise USD ••5555",
            }},
        }
        unrelated_verified = verify_private_connector_access_probe_receipt(
            runtime,
            request_path,
            receipt_path,
            as_of="2026-08-16T13:00:00+00:00",
            environ={
                **environment,
                "OPC_WISE_ENTITY_BINDINGS_JSON": json.dumps(unrelated_binding),
            },
        )
        self.assertTrue(unrelated_verified["valid"])
        changed_binding = json.loads(bindings)
        changed_binding["sg_store"]["balances"]["SGD"]["balance_id"] = 111111
        with self.assertRaisesRegex(ConnectorAccessProbeError, "has changed"):
            verify_private_connector_access_probe_receipt(
                runtime,
                request_path,
                receipt_path,
                as_of="2026-08-16T13:00:00+00:00",
                environ={
                    **environment,
                    "OPC_WISE_ENTITY_BINDINGS_JSON": json.dumps(changed_binding),
                },
            )

    def test_paypal_probe_binds_entity_app_account_and_discards_balance_values(self):
        runtime = BoxRuntime(
            ROOT / "examples" / "boxes" / "us_dtc_paypal_c_corp.json",
            ROOT / "packs",
        )
        request_path = self._write_request(
            "paypal.json",
            self._environment_binding_request(
                "connector.paypal", "us_dtc_company",
            ),
        )
        selected = {
            "environment": "production",
            "app_id": "APPID_1234",
            "account_id": "2ABCD3EFGH4JK",
            "client_id_env": "OPC_PAYPAL_US_CLIENT_ID",
            "client_secret_env": "OPC_PAYPAL_US_CLIENT_SECRET",
        }
        bindings = {"us_dtc_company": selected}
        environment = {
            "OPC_PAYPAL_ENTITY_BINDINGS_JSON": json.dumps(bindings),
            "OPC_PAYPAL_US_CLIENT_ID": "private-client-id",
            "OPC_PAYPAL_US_CLIENT_SECRET": "private-client-secret",
        }
        responses = [
            HttpResponse(200, {}, json.dumps({
                "access_token": "private-paypal-token",
                "token_type": "Bearer",
                "app_id": selected["app_id"],
                "scope": (
                    "openid "
                    "https://uri.paypal.com/services/reporting/search/read"
                ),
            }).encode()),
            HttpResponse(200, {}, json.dumps({
                "account_id": selected["account_id"],
                "balances": [{
                    "primary": True,
                    "total_balance": {
                        "currency_code": "USD", "value": "987654.32",
                    },
                }],
            }).encode()),
        ]
        calls = []

        def transport(request):
            calls.append(request)
            return responses.pop(0)

        result = run_connector_access_probe(
            runtime,
            request_path,
            allow_network=True,
            environ=environment,
            transport=transport,
            sleeper=lambda seconds: None,
            observed_at="2026-08-16T12:00:00+00:00",
        )
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["schema_version"], 2)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0].method, "POST")
        self.assertEqual(
            calls[0].url, "https://api-m.paypal.com/v1/oauth2/token",
        )
        self.assertEqual(calls[1].method, "GET")
        self.assertIn("/v1/reporting/balances?", calls[1].url)
        self.assertIn("currency_code=USD", calls[1].url)
        self.assertTrue(result["provider"]["balance_values_requested"])
        self.assertFalse(result["provider"]["balance_values_retained"])
        self.assertEqual(
            result["credential_reference"]["env_names"],
            [
                "OPC_PAYPAL_ENTITY_BINDINGS_JSON",
                "OPC_PAYPAL_US_CLIENT_ID",
                "OPC_PAYPAL_US_CLIENT_SECRET",
            ],
        )
        serialized = json.dumps(result)
        for private in (
            "private-paypal-token", "private-client-id", "private-client-secret",
            selected["app_id"], selected["account_id"], "987654.32",
        ):
            self.assertNotIn(private, serialized)

        receipt_path = (self.root / "paypal-receipt.json").resolve()
        receipt_responses = [
            HttpResponse(200, {}, json.dumps({
                "access_token": "rotating-ephemeral-token",
                "token_type": "Bearer",
                "app_id": selected["app_id"],
                "scope": "https://uri.paypal.com/services/reporting/search/read",
            }).encode()),
            HttpResponse(200, {}, json.dumps({
                "account_id": selected["account_id"], "balances": [],
            }).encode()),
        ]
        write_connector_access_probe_receipt(
            runtime,
            request_path,
            receipt_path,
            allow_network=True,
            environ=environment,
            transport=lambda request: receipt_responses.pop(0),
            sleeper=lambda seconds: None,
            observed_at="2026-08-16T12:00:00+00:00",
        )
        unrelated = dict(bindings)
        unrelated["other_company"] = {
            **selected,
            "environment": "sandbox",
            "app_id": "OTHER_1234",
            "account_id": "3ABCD4EFGH5JK",
            "client_id_env": "OPC_PAYPAL_OTHER_CLIENT_ID",
            "client_secret_env": "OPC_PAYPAL_OTHER_CLIENT_SECRET",
        }
        verified = verify_private_connector_access_probe_receipt(
            runtime,
            request_path,
            receipt_path,
            as_of="2026-08-16T13:00:00+00:00",
            environ={
                **environment,
                "OPC_PAYPAL_ENTITY_BINDINGS_JSON": json.dumps(unrelated),
            },
        )
        self.assertTrue(verified["valid"])
        with self.assertRaisesRegex(ConnectorAccessProbeError, "has changed"):
            verify_private_connector_access_probe_receipt(
                runtime,
                request_path,
                receipt_path,
                as_of="2026-08-16T13:00:00+00:00",
                environ={
                    **environment,
                    "OPC_PAYPAL_US_CLIENT_SECRET": "rotated-private-secret",
                },
            )

    def test_woocommerce_probe_reads_only_ids_and_keeps_write_permission_unverified(self):
        runtime = BoxRuntime(
            ROOT / "examples" / "boxes" / "us_dtc_woocommerce_c_corp.json",
            ROOT / "packs",
        )
        request_path = self._write_request(
            "woocommerce.json",
            self._environment_binding_request(
                "connector.woocommerce", "us_dtc_company",
            ),
        )
        selected = {
            "site_origin": "https://shop.example.com/store",
            "key_permission": "read",
            "consumer_key_env": "OPC_WC_US_KEY",
            "consumer_secret_env": "OPC_WC_US_SECRET",
        }
        environment = {
            "OPC_WOOCOMMERCE_ENTITY_BINDINGS_JSON": json.dumps({
                "us_dtc_company": selected,
            }),
            "OPC_WC_US_KEY": "ck_private_key",
            "OPC_WC_US_SECRET": "cs_private_secret",
        }
        responses = [
            HttpResponse(200, {}, b'[{"id":987654}]'),
            HttpResponse(200, {}, b'[{"id":654321}]'),
        ]
        calls = []

        def transport(request):
            calls.append(request)
            return responses.pop(0)

        result = run_connector_access_probe(
            runtime,
            request_path,
            allow_network=True,
            environ=environment,
            transport=transport,
            sleeper=lambda seconds: None,
            observed_at="2026-08-16T12:00:00+00:00",
        )
        self.assertEqual(result["status"], "passed")
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(call.method == "GET" for call in calls))
        self.assertIn("/store/wp-json/wc/v3/orders?", calls[0].url)
        self.assertIn("/store/wp-json/wc/v3/refunds?", calls[1].url)
        self.assertTrue(all("_fields=id" in call.url for call in calls))
        self.assertTrue(all("context=view" in call.url for call in calls))
        self.assertFalse(result["provider"]["financial_values_requested"])
        self.assertFalse(result["provider"]["write_permission_provider_verified"])
        serialized = json.dumps(result)
        for private in (
            "shop.example.com", "ck_private_key", "cs_private_secret",
            "987654", "654321",
        ):
            self.assertNotIn(private, serialized)

        receipt_path = (self.root / "woocommerce-receipt.json").resolve()
        receipt_responses = [
            HttpResponse(200, {}, b'[]'), HttpResponse(200, {}, b'[]'),
        ]
        write_connector_access_probe_receipt(
            runtime,
            request_path,
            receipt_path,
            allow_network=True,
            environ=environment,
            transport=lambda request: receipt_responses.pop(0),
            sleeper=lambda seconds: None,
            observed_at="2026-08-16T12:00:00+00:00",
        )
        verified = verify_private_connector_access_probe_receipt(
            runtime,
            request_path,
            receipt_path,
            as_of="2026-08-16T13:00:00+00:00",
            environ=environment,
        )
        self.assertTrue(verified["valid"])
        with self.assertRaisesRegex(ConnectorAccessProbeError, "has changed"):
            verify_private_connector_access_probe_receipt(
                runtime,
                request_path,
                receipt_path,
                as_of="2026-08-16T13:00:00+00:00",
                environ={**environment, "OPC_WC_US_KEY": "ck_rotated_key"},
            )

    def test_shipbob_probe_binds_entity_channel_and_exact_read_scope_set(self):
        runtime = BoxRuntime(
            ROOT / "examples" / "boxes" / "us_dtc_shopify_stripe_shipbob_c_corp.json",
            ROOT / "packs",
        )
        request_path = self._write_request(
            "shipbob.json",
            self._environment_binding_request(
                "connector.shipbob", "us_dtc_company",
            ),
        )
        selected = {
            "environment": "production",
            "channel_id": 100102,
            "token_env": "OPC_SHIPBOB_US_TOKEN",
        }
        environment = {
            "OPC_SHIPBOB_ENTITY_BINDINGS_JSON": json.dumps({
                "us_dtc_company": selected,
            }),
            "OPC_SHIPBOB_US_TOKEN": "private-shipbob-token",
        }
        responses = [HttpResponse(200, {}, json.dumps({
            "items": [{
                "id": selected["channel_id"],
                "name": "Private channel name",
                "application_name": "Private app name",
                "scopes": [
                    "channels_read", "orders_read", "fulfillments_read",
                    "returns_read",
                ],
            }],
            "next": None,
            "prev": None,
        }).encode())]
        calls = []

        def transport(request):
            calls.append(request)
            return responses.pop(0)

        receipt_path = (self.root / "shipbob-receipt.json").resolve()
        written = write_connector_access_probe_receipt(
            runtime,
            request_path,
            receipt_path,
            allow_network=True,
            environ=environment,
            transport=transport,
            sleeper=lambda seconds: None,
            observed_at="2026-08-16T12:00:00+00:00",
        )
        self.assertEqual(written["schema_version"], 2)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].method, "GET")
        self.assertEqual(
            calls[0].url,
            "https://api.shipbob.com/2026-07/channel?RecordsPerPage=50",
        )
        serialized = receipt_path.read_text(encoding="utf-8")
        for private in (
            "private-shipbob-token", "100102", "Private channel name",
            "Private app name",
        ):
            self.assertNotIn(private, serialized)
        verified = verify_private_connector_access_probe_receipt(
            runtime,
            request_path,
            receipt_path,
            as_of="2026-08-16T13:00:00+00:00",
            environ=environment,
        )
        self.assertTrue(verified["valid"])
        with self.assertRaisesRegex(ConnectorAccessProbeError, "has changed"):
            verify_private_connector_access_probe_receipt(
                runtime,
                request_path,
                receipt_path,
                as_of="2026-08-16T13:00:00+00:00",
                environ={**environment, "OPC_SHIPBOB_US_TOKEN": "rotated-token"},
            )

    def test_amazon_seller_probe_verifies_marketplace_and_four_read_endpoints(self):
        runtime = BoxRuntime(
            ROOT / "examples" / "boxes" / "us_marketplace_amazon_seller_c_corp.json",
            ROOT / "packs",
        )
        request_path = self._write_request(
            "amazon.json",
            self._environment_binding_request(
                "connector.amazon_seller", "us_amazon_marketplace_company",
            ),
        )
        selected = {
            "environment": "production",
            "region": "NA",
            "seller_id": "A1SELLER12345",
            "marketplace_ids": ["ATVPDKIKX0DER"],
            "client_id_env": "OPC_AMAZON_US_CLIENT_ID",
            "client_secret_env": "OPC_AMAZON_US_CLIENT_SECRET",
            "refresh_token_env": "OPC_AMAZON_US_REFRESH_TOKEN",
        }
        environment = {
            "OPC_AMAZON_SELLER_ENTITY_BINDINGS_JSON": json.dumps({
                "us_amazon_marketplace_company": selected,
            }),
            "OPC_AMAZON_US_CLIENT_ID": "private-client-id",
            "OPC_AMAZON_US_CLIENT_SECRET": "private-client-secret",
            "OPC_AMAZON_US_REFRESH_TOKEN": "private-refresh-token",
        }
        responses = [
            HttpResponse(200, {}, json.dumps({
                "access_token": "ephemeral-private-token", "token_type": "bearer",
            }).encode()),
            HttpResponse(200, {}, json.dumps({"payload": [{
                "marketplace": {
                    "id": "ATVPDKIKX0DER", "name": "Private marketplace name",
                },
                "participation": {"isParticipating": True, "isSuspended": False},
            }]}).encode()),
            HttpResponse(200, {}, b'{"orders":[],"pagination":{}}'),
            HttpResponse(200, {}, json.dumps({"payload": {
                "granularity": {
                    "granularityType": "Marketplace",
                    "granularityId": "ATVPDKIKX0DER",
                },
                "inventorySummaries": [],
            }}).encode()),
            HttpResponse(200, {}, b'{"payload":{"transactions":[]}}'),
        ]
        calls = []

        def transport(request):
            calls.append(request)
            return responses.pop(0)

        receipt_path = (self.root / "amazon-receipt.json").resolve()
        written = write_connector_access_probe_receipt(
            runtime,
            request_path,
            receipt_path,
            allow_network=True,
            environ=environment,
            transport=transport,
            sleeper=lambda seconds: None,
            observed_at="2026-08-16T12:00:00+00:00",
        )
        self.assertEqual(written["schema_version"], 2)
        self.assertEqual(len(calls), 5)
        self.assertEqual(calls[0].url, "https://api.amazon.com/auth/o2/token")
        self.assertIn("/sellers/v1/marketplaceParticipations", calls[1].url)
        self.assertIn("/orders/2026-01-01/orders?", calls[2].url)
        self.assertIn("/fba/inventory/v1/summaries?", calls[3].url)
        self.assertIn("/finances/2024-06-19/transactions?", calls[4].url)
        self.assertTrue(all(call.method == "GET" for call in calls[1:]))
        serialized = receipt_path.read_text(encoding="utf-8")
        for private in (
            "private-client-id", "private-client-secret", "private-refresh-token",
            "ephemeral-private-token", "A1SELLER12345", "ATVPDKIKX0DER",
            "Private marketplace name",
        ):
            self.assertNotIn(private, serialized)
        verified = verify_private_connector_access_probe_receipt(
            runtime,
            request_path,
            receipt_path,
            as_of="2026-08-16T13:00:00+00:00",
            environ=environment,
        )
        self.assertTrue(verified["valid"])
        with self.assertRaisesRegex(ConnectorAccessProbeError, "has changed"):
            verify_private_connector_access_probe_receipt(
                runtime,
                request_path,
                receipt_path,
                as_of="2026-08-16T13:00:00+00:00",
                environ={
                    **environment,
                    "OPC_AMAZON_US_REFRESH_TOKEN": "rotated-refresh-token",
                },
            )

    def test_xero_probe_verifies_organisation_and_trial_balance_read_scopes(self):
        runtime = BoxRuntime(
            ROOT / "examples" / "boxes" / "global_game_studio_xero.json",
            ROOT / "packs",
        )
        request_path = self._write_request(
            "xero.json",
            self._environment_binding_request("connector.xero", "cn_studio"),
        )
        tenant_id = "11111111-1111-4111-8111-111111111111"
        organisation_id = "22222222-2222-4222-8222-222222222222"
        environment = {
            "OPC_XERO_ACCESS_TOKEN": "xero_private_token",
            "OPC_XERO_ENTITY_BINDINGS_JSON": json.dumps({
                "cn_studio": {
                    "tenant_id": tenant_id,
                    "organisation_id": organisation_id,
                },
            }),
        }
        responses = [
            HttpResponse(200, {}, json.dumps({"Organisations": [{
                "OrganisationID": organisation_id,
                "BaseCurrency": "CNY",
                "Name": "Private Game Studio",
            }]}).encode()),
            HttpResponse(200, {}, b'{"Reports":[{"Rows":[],"private_amount":999999.99}]}'),
        ]
        calls = []

        def transport(request):
            calls.append(request)
            return responses.pop(0)

        receipt_path = (self.root / "xero-receipt.json").resolve()
        written = write_connector_access_probe_receipt(
            runtime,
            request_path,
            receipt_path,
            allow_network=True,
            environ=environment,
            transport=transport,
            sleeper=lambda seconds: None,
            observed_at="2026-08-16T12:00:00+00:00",
        )
        self.assertEqual(written["schema_version"], 2)
        self.assertEqual(len(calls), 2)
        self.assertEqual(
            calls[0].url,
            "https://api.xero.com/api.xro/2.0/Organisation",
        )
        self.assertEqual(calls[0].headers["xero-tenant-id"], tenant_id)
        self.assertEqual(
            calls[1].url,
            "https://api.xero.com/api.xro/2.0/Reports/TrialBalance?date=2026-08-16&paymentsOnly=false",
        )
        verified = verify_private_connector_access_probe_receipt(
            runtime,
            request_path,
            receipt_path,
            as_of="2026-08-16T13:00:00+00:00",
            environ=environment,
        )
        self.assertTrue(verified["ready_for_private_shadow_request"])
        serialized = receipt_path.read_text(encoding="utf-8")
        for private in (
            "xero_private_token", tenant_id, organisation_id,
            "Private Game Studio", "999999.99",
        ):
            self.assertNotIn(private, serialized)

    def test_passed_probe_can_be_persisted_and_reverified_without_private_values(self):
        request_path = self._write_request("stripe.json", self._stripe_request())
        receipt_path = (self.root / "stripe-receipt.json").resolve()
        responses = [
            HttpResponse(200, {}, b'{"object":"account","id":"acct_123456789ABC"}'),
            HttpResponse(200, {}, b'{"object":"list","data":[],"has_more":false}'),
            HttpResponse(200, {}, b'{"object":"list","data":[],"has_more":false}'),
        ]
        written = write_connector_access_probe_receipt(
            self.runtime,
            request_path,
            receipt_path,
            allow_network=True,
            environ={"OPC_STRIPE_RESTRICTED_KEY": "rk_test_1234567890PRIVATE"},
            transport=lambda request: responses.pop(0),
            sleeper=lambda seconds: None,
            observed_at="2026-08-16T12:00:00+00:00",
        )
        self.assertTrue(written["written"])
        self.assertTrue(written["ready_for_private_shadow_request"])
        self.assertFalse(written["receipt_is_digital_signature"])
        self.assertEqual(receipt_path.stat().st_mode & 0o777, 0o600)
        verified = verify_private_connector_access_probe_receipt(
            self.runtime,
            request_path,
            receipt_path,
            as_of="2026-08-16T13:00:00+00:00",
            environ={"OPC_STRIPE_RESTRICTED_KEY": "rk_test_1234567890PRIVATE"},
        )
        self.assertTrue(verified["valid"])
        self.assertEqual(verified["binding_mode"], "own_account")
        with self.assertRaisesRegex(ConnectorAccessProbeError, "has changed"):
            verify_private_connector_access_probe_receipt(
                self.runtime,
                request_path,
                receipt_path,
                as_of="2026-08-16T13:00:00+00:00",
                environ={"OPC_STRIPE_RESTRICTED_KEY": "rk_test_1234567890DIFFERENT"},
            )
        receipt = read_private_connector_access_probe_receipt(receipt_path)
        serialized = json.dumps(receipt)
        self.assertNotIn("acct_123456789ABC", serialized)
        self.assertNotIn("rk_test_1234567890PRIVATE", serialized)

    def test_v76_single_credential_receipt_remains_current_after_schema_v2_upgrade(self):
        request_path = self._write_request("legacy-stripe.json", self._stripe_request())
        receipt_path = (self.root / "legacy-stripe-receipt.json").resolve()
        key = "rk_test_1234567890PRIVATE"
        responses = [
            HttpResponse(200, {}, b'{"object":"account","id":"acct_123456789ABC"}'),
            HttpResponse(200, {}, b'{"object":"list","data":[],"has_more":false}'),
            HttpResponse(200, {}, b'{"object":"list","data":[],"has_more":false}'),
        ]
        write_connector_access_probe_receipt(
            self.runtime,
            request_path,
            receipt_path,
            allow_network=True,
            environ={"OPC_STRIPE_RESTRICTED_KEY": key},
            transport=lambda request: responses.pop(0),
            sleeper=lambda seconds: None,
            observed_at="2026-08-16T12:00:00+00:00",
        )
        receipt = read_private_connector_access_probe_receipt(receipt_path)

        def digest(value):
            body = json.dumps(
                value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            ).encode()
            return hashlib.sha256(body).hexdigest()

        legacy_credential_material = {
            "runtime_fingerprint": self.runtime.snapshot()["fingerprint"],
            "pack_id": "connector.stripe",
            "credential_value": key,
        }
        receipt["schema_version"] = 1
        receipt["credential_reference"] = {
            "env_name": "OPC_STRIPE_RESTRICTED_KEY",
            "configured": True,
            "fingerprint": f"sha256:{digest(legacy_credential_material)}",
        }
        receipt.pop("receipt_fingerprint")
        receipt["receipt_fingerprint"] = digest(receipt)
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        os.chmod(receipt_path, 0o600)
        verified = verify_private_connector_access_probe_receipt(
            self.runtime,
            request_path,
            receipt_path,
            as_of="2026-08-16T13:00:00+00:00",
            environ={"OPC_STRIPE_RESTRICTED_KEY": key},
        )
        self.assertEqual(verified["schema_version"], 1)
        self.assertTrue(verified["ready_for_private_shadow_request"])

    def test_receipt_verification_rejects_tampering_staleness_and_request_rebinding(self):
        request_path = self._write_request("shopify.json", self._shopify_request())
        receipt_path = (self.root / "shopify-receipt.json").resolve()
        response = HttpResponse(200, {"X-Shopify-API-Version": "2026-07"}, json.dumps({
            "data": {"currentAppInstallation": {"accessScopes": [
                {"handle": "read_orders"},
            ]}},
        }).encode())
        write_connector_access_probe_receipt(
            self.runtime,
            request_path,
            receipt_path,
            allow_network=True,
            environ={"OPC_SHOPIFY_ADMIN_TOKEN": "shpat_private_value"},
            transport=lambda request: response,
            sleeper=lambda seconds: None,
            observed_at="2026-08-01T00:00:00+00:00",
        )
        with self.assertRaisesRegex(ConnectorAccessProbeError, "older than 10 days"):
            verify_private_connector_access_probe_receipt(
                self.runtime,
                request_path,
                receipt_path,
                as_of="2026-08-16T00:00:00+00:00",
                maximum_age_days=10,
                environ={"OPC_SHOPIFY_ADMIN_TOKEN": "shpat_private_value"},
            )

        rebound = self._shopify_request()
        rebound["account_binding"]["shop_domain"] = "another-store.myshopify.com"
        rebound_path = self._write_request("shopify-rebound.json", rebound)
        with self.assertRaisesRegex(ConnectorAccessProbeError, "request_fingerprint"):
            verify_private_connector_access_probe_receipt(
                self.runtime,
                rebound_path,
                receipt_path,
                as_of="2026-08-02T00:00:00+00:00",
                environ={"OPC_SHOPIFY_ADMIN_TOKEN": "shpat_private_value"},
            )

        tampered = read_private_connector_access_probe_receipt(receipt_path)
        tampered["summary"]["ready_for_private_shadow_request"] = False
        receipt_path.write_text(json.dumps(tampered), encoding="utf-8")
        os.chmod(receipt_path, 0o600)
        with self.assertRaisesRegex(ConnectorAccessProbeError, "fingerprint"):
            verify_private_connector_access_probe_receipt(
                self.runtime,
                request_path,
                receipt_path,
                as_of="2026-08-02T00:00:00+00:00",
                environ={"OPC_SHOPIFY_ADMIN_TOKEN": "shpat_private_value"},
            )

    def test_receipt_creation_requires_explicit_network_authorization(self):
        request_path = self._write_request("stripe.json", self._stripe_request())
        with self.assertRaisesRegex(ConnectorAccessProbeError, "network authorization"):
            write_connector_access_probe_receipt(
                self.runtime,
                request_path,
                (self.root / "receipt.json").resolve(),
                allow_network=False,
                transport=lambda request: self.fail("transport must not run"),
            )

    def test_receipt_renewal_retains_prior_evidence_and_supports_key_rotation(self):
        request_path = self._write_request("stripe.json", self._stripe_request())
        receipt_path = (self.root / "stripe-receipt.json").resolve()
        old_key = "rk_test_1234567890OLDKEY"
        new_key = "rk_live_1234567890NEWKEY"

        def passed_responses():
            return [
                HttpResponse(200, {}, b'{"object":"account","id":"acct_123456789ABC"}'),
                HttpResponse(200, {}, b'{"object":"list","data":[],"has_more":false}'),
                HttpResponse(200, {}, b'{"object":"list","data":[],"has_more":false}'),
            ]

        old_responses = passed_responses()
        written = write_connector_access_probe_receipt(
            self.runtime,
            request_path,
            receipt_path,
            allow_network=True,
            environ={"OPC_STRIPE_RESTRICTED_KEY": old_key},
            transport=lambda request: old_responses.pop(0),
            sleeper=lambda seconds: None,
            observed_at="2026-07-01T12:00:00+00:00",
        )
        old_bytes = receipt_path.read_bytes()
        static = verify_private_connector_access_probe_receipt_contract(
            self.runtime, request_path, receipt_path,
        )
        self.assertTrue(static["valid_static_contract"])
        self.assertFalse(static["current_credential_binding_verified"])
        self.assertFalse(static["freshness_verified"])
        self.assertFalse(static["ready_for_private_shadow_request"])

        new_responses = passed_responses()
        renewed = renew_connector_access_probe_receipt(
            self.runtime,
            request_path,
            receipt_path,
            allow_network=True,
            environ={"OPC_STRIPE_RESTRICTED_KEY": new_key},
            transport=lambda request: new_responses.pop(0),
            sleeper=lambda seconds: None,
            observed_at="2026-08-16T12:00:00+00:00",
        )
        self.assertTrue(renewed["renewed"])
        self.assertTrue(renewed["superseded_receipt_retained"])
        self.assertTrue(renewed["renewal_atomic"])
        self.assertTrue(renewed["credential_rotation_supported"])
        self.assertFalse(renewed["archive_path_returned"])
        self.assertEqual(
            renewed["superseded_receipt_fingerprint"],
            written["receipt_fingerprint"],
        )
        self.assertNotEqual(
            renewed["receipt_fingerprint"], written["receipt_fingerprint"],
        )
        verify_private_connector_access_probe_receipt(
            self.runtime,
            request_path,
            receipt_path,
            as_of="2026-08-16T13:00:00+00:00",
            environ={"OPC_STRIPE_RESTRICTED_KEY": new_key},
        )
        archives = list(self.root.glob("stripe-receipt--superseded-*.json"))
        self.assertEqual(len(archives), 1)
        self.assertEqual(archives[0].read_bytes(), old_bytes)
        self.assertEqual(archives[0].stat().st_mode & 0o777, 0o600)
        self.assertEqual(receipt_path.stat().st_mode & 0o777, 0o600)
        self.assertFalse(any(self.root.glob(".*.renewing-*.tmp")))
        self.assertFalse(any(self.root.glob(".*.renewal-lock")))
        serialized = json.dumps(renewed)
        self.assertNotIn(old_key, serialized)
        self.assertNotIn(new_key, serialized)
        self.assertNotIn("acct_123456789ABC", serialized)

    def test_receipt_renewal_fails_before_rotation_when_probe_or_contract_fails(self):
        request_path = self._write_request("stripe.json", self._stripe_request())
        receipt_path = (self.root / "stripe-receipt.json").resolve()
        key = "rk_test_1234567890PRIVATE"
        responses = [
            HttpResponse(200, {}, b'{"object":"account","id":"acct_123456789ABC"}'),
            HttpResponse(200, {}, b'{"object":"list","data":[],"has_more":false}'),
            HttpResponse(200, {}, b'{"object":"list","data":[],"has_more":false}'),
        ]
        write_connector_access_probe_receipt(
            self.runtime,
            request_path,
            receipt_path,
            allow_network=True,
            environ={"OPC_STRIPE_RESTRICTED_KEY": key},
            transport=lambda request: responses.pop(0),
            sleeper=lambda seconds: None,
            observed_at="2026-08-01T12:00:00+00:00",
        )
        original = receipt_path.read_bytes()
        calls = []
        with self.assertRaisesRegex(ConnectorAccessProbeError, "network authorization"):
            renew_connector_access_probe_receipt(
                self.runtime,
                request_path,
                receipt_path,
                allow_network=False,
                transport=lambda request: calls.append(request),
            )
        self.assertEqual(calls, [])
        denied = [
            HttpResponse(403, {}, b'{"error":"denied"}'),
            HttpResponse(403, {}, b'{"error":"denied"}'),
            HttpResponse(403, {}, b'{"error":"denied"}'),
        ]
        with self.assertRaisesRegex(ConnectorAccessProbeError, "did not pass"):
            renew_connector_access_probe_receipt(
                self.runtime,
                request_path,
                receipt_path,
                allow_network=True,
                environ={"OPC_STRIPE_RESTRICTED_KEY": key},
                transport=lambda request: denied.pop(0),
                sleeper=lambda seconds: None,
                observed_at="2026-08-16T12:00:00+00:00",
            )
        self.assertEqual(receipt_path.read_bytes(), original)
        self.assertFalse(list(self.root.glob("stripe-receipt--superseded-*.json")))
        self.assertFalse(any(self.root.glob(".*.renewing-*.tmp")))
        self.assertFalse(any(self.root.glob(".*.renewal-lock")))

        tampered = read_private_connector_access_probe_receipt(receipt_path)
        tampered["summary"]["ready_for_private_shadow_request"] = False
        receipt_path.write_text(json.dumps(tampered), encoding="utf-8")
        os.chmod(receipt_path, 0o600)
        with self.assertRaisesRegex(ConnectorAccessProbeError, "fingerprint"):
            renew_connector_access_probe_receipt(
                self.runtime,
                request_path,
                receipt_path,
                allow_network=True,
                environ={"OPC_STRIPE_RESTRICTED_KEY": key},
                transport=lambda request: self.fail(
                    "tampered prior receipt must fail before transport"
                ),
            )

    def test_receipt_renewal_preserves_foreign_lock_and_rejects_symlink(self):
        request_path = self._write_request("stripe.json", self._stripe_request())
        receipt_path = (self.root / "stripe-receipt.json").resolve()
        key = "rk_test_1234567890PRIVATE"
        responses = [
            HttpResponse(200, {}, b'{"object":"account","id":"acct_123456789ABC"}'),
            HttpResponse(200, {}, b'{"object":"list","data":[],"has_more":false}'),
            HttpResponse(200, {}, b'{"object":"list","data":[],"has_more":false}'),
        ]
        write_connector_access_probe_receipt(
            self.runtime,
            request_path,
            receipt_path,
            allow_network=True,
            environ={"OPC_STRIPE_RESTRICTED_KEY": key},
            transport=lambda request: responses.pop(0),
            sleeper=lambda seconds: None,
            observed_at="2026-08-01T12:00:00+00:00",
        )
        original = receipt_path.read_bytes()
        marker = self.root / ".stripe-receipt.json.renewal-lock"
        marker.write_text("another renewal owns this marker", encoding="utf-8")
        os.chmod(marker, 0o600)
        with self.assertRaisesRegex(ConnectorAccessProbeError, "already in progress"):
            renew_connector_access_probe_receipt(
                self.runtime,
                request_path,
                receipt_path,
                allow_network=True,
                environ={"OPC_STRIPE_RESTRICTED_KEY": key},
                transport=lambda request: self.fail(
                    "a competing renewal must fail before transport"
                ),
            )
        self.assertTrue(marker.exists())
        self.assertEqual(receipt_path.read_bytes(), original)
        marker.unlink()

        alias = self.root / "stripe-receipt-alias.json"
        alias.symlink_to(receipt_path)
        with self.assertRaisesRegex(ConnectorAccessProbeError, "non-symlink"):
            renew_connector_access_probe_receipt(
                self.runtime,
                request_path,
                alias,
                allow_network=True,
                environ={"OPC_STRIPE_RESTRICTED_KEY": key},
                transport=lambda request: self.fail(
                    "a receipt symlink must fail before transport"
                ),
            )
        self.assertEqual(receipt_path.read_bytes(), original)
        self.assertFalse(list(self.root.glob("stripe-receipt--superseded-*.json")))


if __name__ == "__main__":
    unittest.main()
