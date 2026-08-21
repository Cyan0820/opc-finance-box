from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.box_runtime import BoxRuntime
from src.connector_http import (
    ConnectorHttpError, HttpResponse, fetch_amazon_seller_marketplace_evidence_pages,
    fetch_amazon_seller_transaction_pages,
)
from src.connector_sdk import ConnectorError
from src.connector_testkit import run_connector_contract_test
from src.default_connectors import build_box_connector_registry


ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / "packs" / "connectors" / "amazon_seller"
BOX = ROOT / "examples" / "boxes" / "us_marketplace_amazon_seller_c_corp.json"


def _response(payload, status=200, headers=None):
    return HttpResponse(
        status=status, headers=headers or {}, body=json.dumps(payload).encode("utf-8"),
    )


def _fetch_kwargs():
    return {
        "client_id": "private-client-id",
        "client_secret": "private-client-secret",
        "refresh_token": "private-refresh-token",
        "region": "NA",
        "environment": "production",
        "marketplace_id": "ATVPDKIKX0DER",
        "posted_after": "2026-07-01T00:00:00Z",
        "posted_before": "2026-08-01T00:00:00Z",
    }


class AmazonSellerHttpTests(unittest.TestCase):
    def test_marketplace_evidence_uses_one_lwa_token_and_three_fixed_read_only_sources(self):
        requests = []
        responses = [
            _response({"access_token": "ephemeral-access"}),
            _response({
                "orders": [{"orderId": "123-1234567-1234567"}],
                "pagination": {"nextToken": "orders-next"},
            }),
            _response({"orders": []}),
            _response({
                "payload": {
                    "granularity": {
                        "granularityType": "Marketplace", "granularityId": "ATVPDKIKX0DER",
                    },
                    "inventorySummaries": [{"sellerSku": "SKU-1"}],
                },
                "pagination": {"nextToken": "inventory-next"},
            }),
            _response({
                "payload": {
                    "granularity": {
                        "granularityType": "Marketplace", "granularityId": "ATVPDKIKX0DER",
                    },
                    "inventorySummaries": [],
                },
            }),
            _response({
                "payload": {
                    "transactions": [{}], "nextToken": "finances-next",
                },
            }),
            _response({"payload": {"transactions": []}}),
        ]

        def transport(request):
            requests.append(request)
            return responses.pop(0)

        kwargs = _fetch_kwargs()
        result = fetch_amazon_seller_marketplace_evidence_pages(
            client_id=kwargs["client_id"], client_secret=kwargs["client_secret"],
            refresh_token=kwargs["refresh_token"], region=kwargs["region"],
            environment=kwargs["environment"], marketplace_id=kwargs["marketplace_id"],
            interval_start=kwargs["posted_after"], interval_end=kwargs["posted_before"],
            max_order_pages=2, max_inventory_pages=2, max_transaction_pages=2,
            transport=transport, sleeper=lambda _: None,
        )
        self.assertEqual(result["order_count"], 1)
        self.assertEqual(result["inventory_count"], 1)
        self.assertEqual(result["transaction_count"], 1)
        self.assertEqual(result["lwa_token_exchange_count"], 1)
        self.assertEqual(result["order_page_count"], 2)
        self.assertEqual(len(requests), 7)
        self.assertEqual(sum(item.url == "https://api.amazon.com/auth/o2/token" for item in requests), 1)
        self.assertIn("/orders/2026-01-01/orders?", requests[1].url)
        self.assertIn("includedData=FULFILLMENT", requests[1].url)
        self.assertNotIn("BUYER", requests[1].url)
        self.assertNotIn("PROCEEDS", requests[1].url)
        self.assertIn("paginationToken=orders-next", requests[2].url)
        self.assertIn("/fba/inventory/v1/summaries?", requests[3].url)
        self.assertIn("details=true", requests[3].url)
        self.assertIn("nextToken=inventory-next", requests[4].url)
        self.assertIn("/finances/2024-06-19/transactions?", requests[5].url)
        self.assertIn("nextToken=finances-next", requests[6].url)
        self.assertTrue(all("private-" not in item.url for item in requests))

    def test_marketplace_evidence_rejects_bad_scope_structure_and_limits(self):
        kwargs = _fetch_kwargs()
        common = {
            "client_id": kwargs["client_id"], "client_secret": kwargs["client_secret"],
            "refresh_token": kwargs["refresh_token"], "region": kwargs["region"],
            "environment": kwargs["environment"], "marketplace_id": kwargs["marketplace_id"],
            "interval_start": kwargs["posted_after"], "interval_end": kwargs["posted_before"],
        }
        for override in (
            {"orders_time_basis": "guessed"}, {"max_order_pages": 21},
            {"interval_end": "2026-09-01T00:00:01Z"},
        ):
            with self.subTest(override=override), self.assertRaises(ConnectorHttpError):
                fetch_amazon_seller_marketplace_evidence_pages(**{**common, **override})

        responses = [
            _response({"access_token": "ephemeral"}),
            _response({"orders": []}),
            _response({
                "payload": {
                    "granularity": {
                        "granularityType": "Marketplace", "granularityId": "WRONG",
                    },
                    "inventorySummaries": [],
                },
            }),
        ]
        with self.assertRaisesRegex(ConnectorHttpError, "Inventory page 1"):
            fetch_amazon_seller_marketplace_evidence_pages(
                **common, transport=lambda _: responses.pop(0), sleeper=lambda _: None,
            )

    def test_lwa_fixed_region_endpoint_and_same_endpoint_cursor_pagination(self):
        requests = []
        responses = [
            _response({"access_token": "ephemeral-access", "token_type": "bearer"}),
            _response({
                "payload": {"transactions": [{}], "nextToken": "opaque-next"},
                "links": [{"href": "https://attacker.invalid/steal", "rel": "next"}],
            }),
            _response({"payload": {"transactions": []}}),
        ]

        def transport(request):
            requests.append(request)
            return responses.pop(0)

        result = fetch_amazon_seller_transaction_pages(
            **_fetch_kwargs(), max_pages=2, transport=transport, sleeper=lambda _: None,
        )
        self.assertEqual(result["page_count"], 2)
        self.assertEqual(result["total_items"], 1)
        self.assertTrue(result["lwa_token_exchange_performed"])
        self.assertFalse(result["response_links_followed"])
        self.assertEqual(requests[0].url, "https://api.amazon.com/auth/o2/token")
        self.assertEqual(requests[0].method, "POST")
        self.assertTrue(requests[0].body)
        self.assertIn(
            "https://sellingpartnerapi-na.amazon.com/finances/2024-06-19/transactions?",
            requests[1].url,
        )
        self.assertIn("postedAfter=2026-07-01T00%3A00%3A00Z", requests[1].url)
        self.assertIn("marketplaceId=ATVPDKIKX0DER", requests[1].url)
        self.assertIn("nextToken=opaque-next", requests[2].url)
        self.assertTrue(all("attacker.invalid" not in item.url for item in requests))
        self.assertTrue(all("private-refresh-token" not in item.url for item in requests))
        self.assertEqual(requests[1].headers["x-amz-access-token"], "ephemeral-access")
        self.assertNotIn("Authorization", requests[1].headers)

    def test_retry_repeated_token_and_errors_fail_closed_without_secret_leakage(self):
        responses = [
            _response({}, status=429, headers={"Retry-After": "0"}),
            _response({"access_token": "ephemeral"}),
            _response({}, status=500),
            _response({"payload": {"transactions": []}}),
        ]
        delays = []
        result = fetch_amazon_seller_transaction_pages(
            **_fetch_kwargs(), transport=lambda _: responses.pop(0), sleeper=delays.append,
        )
        self.assertEqual(result["retry_count"], 2)
        self.assertEqual(result["rate_limit_count"], 1)
        self.assertEqual(len(delays), 2)

        repeated = [
            _response({"access_token": "ephemeral"}),
            _response({"payload": {"transactions": [], "nextToken": "same"}}),
            _response({"payload": {"transactions": [], "nextToken": "same"}}),
        ]
        with self.assertRaisesRegex(ConnectorHttpError, "token repeated"):
            fetch_amazon_seller_transaction_pages(
                **_fetch_kwargs(), transport=lambda _: repeated.pop(0), sleeper=lambda _: None,
            )

        def unauthorized(_request):
            return HttpResponse(401, {}, b'{"private":"response-private"}')

        with self.assertRaises(ConnectorHttpError) as caught:
            fetch_amazon_seller_transaction_pages(
                **_fetch_kwargs(), transport=unauthorized, sleeper=lambda _: None,
            )
        message = str(caught.exception)
        for secret in ("private-client-id", "private-client-secret", "private-refresh-token", "response-private"):
            self.assertNotIn(secret, message)

    def test_region_window_status_and_limits_are_bounded(self):
        for override in (
            {"region": "CUSTOM"},
            {"environment": "custom"},
            {"marketplace_id": "bad/id"},
            {"transaction_status": "UNKNOWN"},
            {"max_pages": 21},
            {"posted_before": "2027-08-01T00:00:00Z"},
            {"client_secret": ""},
        ):
            kwargs = _fetch_kwargs()
            kwargs.update(override)
            with self.subTest(override=override), self.assertRaises(ConnectorHttpError):
                fetch_amazon_seller_transaction_pages(**kwargs)


class AmazonSellerProviderTests(unittest.TestCase):
    def setUp(self):
        self.runtime = BoxRuntime(BOX, ROOT / "packs")
        self.registry = build_box_connector_registry(self.runtime)
        self.fixture = json.loads((PACK / "fixture-transactions.json").read_text(encoding="utf-8"))
        self.marketplace_fixture = json.loads(
            (PACK / "fixture-marketplace-evidence.json").read_text(encoding="utf-8")
        )

    def test_marketplace_contract_joins_hashed_orders_skus_and_drops_private_fields(self):
        report = run_connector_contract_test(
            self.registry, self.runtime, "amazon_seller.marketplace_evidence",
            self.marketplace_fixture,
            expected_minimum_counts={
                "commerce.amazon_seller_orders": 3,
                "commerce.amazon_seller_inventory": 2,
                "commerce.amazon_seller_transactions": 2,
            },
        )
        self.assertTrue(report["passed"], report)
        batch = self.registry.dispatch(
            self.runtime, "amazon_seller.marketplace_evidence", self.marketplace_fixture,
        )["batch"]
        orders = batch["datasets"]["commerce.amazon_seller_orders"]
        inventory = batch["datasets"]["commerce.amazon_seller_inventory"]
        transactions = batch["datasets"]["commerce.amazon_seller_transactions"]
        order_reference = next(
            item["key"] for item in transactions[0]["related_keys"]
            if item["type"] == "ORDER_ID"
        )
        self.assertEqual(orders[0]["amazon_order_key"], order_reference)
        self.assertEqual(orders[0]["items"][0]["amazon_sku_key"], inventory[0]["amazon_sku_key"])
        self.assertEqual(inventory[0]["total_quantity"], 10)
        self.assertEqual(inventory[0]["fulfillable_quantity"], 7)
        self.assertEqual(batch["source"]["orders_included_data"], ["FULFILLMENT"])
        self.assertEqual(batch["source"]["canonical_month_period"], "2026-08")
        self.assertTrue(batch["source"]["canonical_month_scope"])
        self.assertEqual(batch["source"]["interval_semantics"], "half_open_utc")
        self.assertFalse(batch["source"]["buyer_recipient_or_address_retained"])
        self.assertFalse(batch["source"]["proceeds_expense_tax_payment_or_tracking_requested"])
        serialized = json.dumps(batch, ensure_ascii=False).lower()
        for forbidden in (
            "private buyer", "private-buyer@example.invalid", "private recipient",
            "private address", "private product title", "private-sku-a",
            "b0private01", "private-fnsku-a", "private store",
            "111-2345678-1234567", "private-marketplace-transaction-001",
            "must-not-survive",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_marketplace_inventory_optional_quantities_are_explicitly_zero_and_disclosed(self):
        request = copy.deepcopy(self.marketplace_fixture)
        details = request["inventory_pages"][0]["payload"]["inventorySummaries"][0][
            "inventoryDetails"
        ]
        details.pop("inboundReceivingQuantity")
        details["reservedQuantity"].pop("pendingCustomerOrderQuantity")
        batch = self.registry.dispatch(
            self.runtime, "amazon_seller.marketplace_evidence", request,
        )["batch"]
        self.assertTrue(batch["quality"]["ready"], batch["quality"])
        row = batch["datasets"]["commerce.amazon_seller_inventory"][0]
        self.assertEqual(row["inbound_receiving_quantity"], 0)
        self.assertEqual(row["pending_customer_order_quantity"], 0)
        self.assertNotIn("inbound_receiving_quantity", row["quantity_fields_present"])
        self.assertNotIn("pending_customer_order_quantity", row["quantity_fields_present"])

    def test_contract_minimizes_customer_product_store_and_raw_identifiers(self):
        report = run_connector_contract_test(
            self.registry, self.runtime, "amazon_seller.transaction_activity", self.fixture,
            expected_minimum_counts={"commerce.amazon_seller_transactions": 3},
        )
        self.assertTrue(report["passed"], report)
        batch = self.registry.dispatch(
            self.runtime, "amazon_seller.transaction_activity", self.fixture,
        )["batch"]
        rows = batch["datasets"]["commerce.amazon_seller_transactions"]
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["amount"], "90.00")
        self.assertEqual(rows[0]["item_count"], 1)
        self.assertEqual(rows[0]["transaction_status"], "RELEASED")
        self.assertTrue(all(len(item["key"]) == 64 for item in rows[0]["related_keys"]))
        serialized = json.dumps(batch, ensure_ascii=False).lower()
        for forbidden in (
            "private-amazon-seller-001", "private-order-1001", "private-product-alpha",
            "private-sku-01", "private-asin-01", "private store", "named customer",
            "private-opaque-next-token", "attacker.invalid",
        ):
            self.assertNotIn(forbidden, serialized)
        source = batch["source"]
        self.assertFalse(source["customer_or_address_retained"])
        self.assertFalse(source["product_identity_or_description_retained"])
        self.assertFalse(source["raw_seller_or_business_ids_retained"])
        self.assertFalse(source["business_write_api_called"])

    def test_binding_window_duplicate_and_structure_controls_fail_closed(self):
        request = copy.deepcopy(self.fixture)
        request["refresh_token"] = "inline-private"
        with self.assertRaisesRegex(ConnectorError, "must not be passed"):
            self.registry.dispatch(self.runtime, "amazon_seller.transaction_activity", request)

        request = copy.deepcopy(self.fixture)
        request["default_entity_id"] = "other"
        with self.assertRaisesRegex(ConnectorError, "default_entity_id"):
            self.registry.dispatch(self.runtime, "amazon_seller.transaction_activity", request)

        request = copy.deepcopy(self.fixture)
        request["interval_end"] = "2026-09-02T00:00:01Z"
        with self.assertRaisesRegex(ConnectorError, "31 days"):
            self.registry.dispatch(self.runtime, "amazon_seller.transaction_activity", request)

        request = copy.deepcopy(self.fixture)
        request["transaction_pages"][0]["payload"]["transactions"][0][
            "sellingPartnerMetadata"
        ]["sellingPartnerId"] = "WRONG-SELLER"
        batch = self.registry.dispatch(
            self.runtime, "amazon_seller.transaction_activity", request,
        )["batch"]
        self.assertFalse(batch["quality"]["ready"])
        self.assertEqual(batch["quality"]["rejected_count"], 1)

        request = copy.deepcopy(self.fixture)
        transaction = request["transaction_pages"][0]["payload"]["transactions"][0]
        transaction["sellingPartnerMetadata"].pop("marketplaceId")
        transaction["marketplaceDetails"] = "not-an-object"
        batch = self.registry.dispatch(
            self.runtime, "amazon_seller.transaction_activity", request,
        )["batch"]
        self.assertFalse(batch["quality"]["ready"])
        self.assertEqual(batch["quality"]["rejected_count"], 1)
        self.assertIn(
            "marketplaceDetails", batch["quality"]["rejected_rows"][0]["reason"],
        )

        request = copy.deepcopy(self.fixture)
        first = request["transaction_pages"][0]["payload"]["transactions"][0]
        request["transaction_pages"][0]["payload"]["transactions"].append(copy.deepcopy(first))
        batch = self.registry.dispatch(
            self.runtime, "amazon_seller.transaction_activity", request,
        )["batch"]
        self.assertFalse(batch["quality"]["ready"])
        self.assertEqual(len(batch["quality"]["duplicate_business_keys"]), 1)

    def test_multi_entity_fetch_selects_aliases_and_rejects_legacy_fallback(self):
        config = json.loads(BOX.read_text(encoding="utf-8"))
        second = copy.deepcopy(config["entities"][0])
        second["id"] = "us_second_company"
        second["name"] = "Second seller company"
        config["entities"].append(second)
        config["features"] = ["feature.multi_entity"]
        config["connector_bindings"] = [
            {
                "connector_pack": "connector.file_import",
                "entity_ids": [
                    "us_amazon_marketplace_company", "us_second_company",
                ],
            },
            {
                "connector_pack": "connector.amazon_seller",
                "entity_ids": [
                    "us_amazon_marketplace_company", "us_second_company",
                ],
            },
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "box.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            runtime = BoxRuntime(config_path, ROOT / "packs")
            registry = build_box_connector_registry(runtime)
            definition = registry.definition("amazon_seller.marketplace_evidence")
            request = {
                "mode": "fetch",
                "default_entity_id": "us_amazon_marketplace_company",
                "environment": "production",
                "marketplace_id": "ATVPDKIKX0DER",
                "interval_start": "2026-07-01T00:00:00Z",
                "interval_end": "2026-08-01T00:00:00Z",
                "max_order_pages": 2,
                "max_inventory_pages": 2,
                "max_transaction_pages": 2,
            }
            calls = []
            responses = [
                _response({"access_token": "ephemeral-access"}),
                _response({"orders": [], "pagination": {}}),
                _response({"payload": {
                    "granularity": {
                        "granularityType": "Marketplace",
                        "granularityId": "ATVPDKIKX0DER",
                    },
                    "inventorySummaries": [],
                }}),
                _response({"payload": {"transactions": []}}),
            ]

            def transport(http_request):
                calls.append(http_request)
                return responses.pop(0)

            definition.handler.__globals__["HTTP_TRANSPORT"] = transport
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
                    "us_second_company": {
                        **selected,
                        "seller_id": "A2SELLER12345",
                        "client_id_env": "OPC_AMAZON_SECOND_CLIENT_ID",
                        "client_secret_env": "OPC_AMAZON_SECOND_CLIENT_SECRET",
                        "refresh_token_env": "OPC_AMAZON_SECOND_REFRESH_TOKEN",
                    },
                }),
                "OPC_AMAZON_US_CLIENT_ID": "private-client",
                "OPC_AMAZON_US_CLIENT_SECRET": "private-secret",
                "OPC_AMAZON_US_REFRESH_TOKEN": "private-refresh",
            }
            with patch.dict("os.environ", environment, clear=True):
                result = registry.dispatch(
                    runtime, "amazon_seller.marketplace_evidence", request,
                )
            self.assertTrue(result["batch"]["source"]["network_access_performed"])
            self.assertTrue(
                result["batch"]["source"]["entity_credential_binding_used"]
            )
            self.assertIn("private-client", calls[0].body.decode())
            serialized = json.dumps(result)
            for private in (
                "private-client", "private-secret", "private-refresh",
                "A1SELLER12345",
            ):
                self.assertNotIn(private, serialized)
            with patch.dict("os.environ", {
                "OPC_AMAZON_SELLER_CLIENT_ID": "legacy-client",
                "OPC_AMAZON_SELLER_CLIENT_SECRET": "legacy-secret",
                "OPC_AMAZON_SELLER_REFRESH_TOKEN": "legacy-refresh",
                "OPC_AMAZON_SELLER_REGION": "NA",
                "OPC_AMAZON_SELLER_ID": "A1SELLER12345",
                "OPC_AMAZON_SELLER_MARKETPLACE_IDS_JSON": json.dumps([
                    "ATVPDKIKX0DER",
                ]),
            }, clear=True), self.assertRaisesRegex(
                ConnectorError, "ENTITY_BINDINGS_JSON",
            ):
                registry.dispatch(
                    runtime, "amazon_seller.marketplace_evidence", request,
                )


if __name__ == "__main__":
    unittest.main()
