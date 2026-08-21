import json
import unittest
from pathlib import Path

from src.box_runtime import BoxRuntime
from src.connector_onboarding import build_connector_onboarding


ROOT = Path(__file__).resolve().parents[1]


class ConnectorOnboardingTests(unittest.TestCase):
    def runtime(self, config: str) -> BoxRuntime:
        return BoxRuntime(ROOT / "examples" / "boxes" / config, ROOT / "packs")

    def test_shopify_stripe_readiness_returns_names_and_booleans_not_values(self):
        result = build_connector_onboarding(
            self.runtime("cn_dtc_shopify_stripe_store.json"),
            environ={
                "OPC_SHOPIFY_ADMIN_TOKEN": "sensitive-shopify-value",
                "OPC_STRIPE_RESTRICTED_KEY": "sensitive-stripe-value",
            },
        )
        self.assertEqual(result["summary"]["pipeline_connector_count"], 10)
        self.assertEqual(result["summary"]["network_connector_count"], 4)
        self.assertEqual(result["summary"]["blocked_connector_count"], 0)
        self.assertTrue({
            "shopify.orders", "shopify.monthly_order_evidence",
        } <= {
            item["connector_id"] for item in result["pipeline_connectors"]
        })
        self.assertEqual(
            {item["env_name"] for item in result["summary"]["required_env"]},
            {"OPC_SHOPIFY_ADMIN_TOKEN", "OPC_STRIPE_RESTRICTED_KEY"},
        )
        serialized = __import__("json").dumps(result)
        self.assertNotIn("sensitive-shopify-value", serialized)
        self.assertNotIn("sensitive-stripe-value", serialized)
        self.assertFalse(result["control_boundary"]["connector_dispatched"])
        self.assertEqual(result["artifact_type"], "opc_finance_box_connector_preflight")
        self.assertEqual(result["schema_version"], 3)
        self.assertEqual(result["summary"]["provider_group_count"], 3)
        self.assertEqual(result["summary"]["network_provider_group_count"], 2)
        self.assertEqual(result["summary"]["blocked_provider_group_count"], 0)
        groups = {item["pack_id"]: item for item in result["provider_groups"]}
        self.assertEqual(groups["connector.shopify"]["dataset_connector_count"], 2)
        self.assertEqual(
            groups["connector.shopify"]["diagnostic_status"],
            "ready_to_initialize_private_access_probe_request",
        )
        self.assertIn(
            "connector-access-request-init",
            groups["connector.shopify"]["next_action"]["command_template"],
        )
        stages = {
            item["stage_id"]: item for item in groups["connector.shopify"]["stages"]
        }
        self.assertEqual(stages["provider_access_probe"]["status"], "available")
        self.assertEqual(stages["private_shadow_request"]["status"], "locked")
        self.assertFalse(result["control_boundary"]["provider_access_probe_performed"])
        self.assertFalse(result["control_boundary"]["commands_executed"])

    def test_missing_network_credentials_fail_closed_without_running_shadow(self):
        result = build_connector_onboarding(
            self.runtime("cn_dtc_shopify_stripe_store.json"), environ={},
        )
        network = [item for item in result["pipeline_connectors"] if item["network_access"]]
        self.assertTrue(network)
        self.assertTrue(all(
            item["readiness"] == "blocked_missing_credential_reference" for item in network
        ))
        self.assertTrue(all(item["shadow_run_performed"] is False for item in network))
        self.assertEqual(result["summary"]["blocked_connector_count"], 4)
        self.assertEqual(result["summary"]["blocked_provider_group_count"], 2)
        network_groups = [
            item for item in result["provider_groups"] if item["network_access"]
        ]
        self.assertEqual(len(network_groups), 2)
        self.assertTrue(all(
            item["diagnostic_status"] == "blocked_missing_credential_reference"
            for item in network_groups
        ))
        self.assertTrue(all(
            item["next_action"]["command_template"] is None
            for item in network_groups
        ))

    def test_wise_and_xero_require_complete_credential_groups_then_offer_access_probe(self):
        cases = [
            (
                "sg_dtc_wise_store.json",
                "connector.wise",
                {
                    "OPC_WISE_ACCESS_TOKEN": "private-token",
                    "OPC_WISE_ENTITY_BINDINGS_JSON": "private-bindings",
                },
            ),
            (
                "global_game_studio_xero.json",
                "connector.xero",
                {
                    "OPC_XERO_ACCESS_TOKEN": "private-token",
                    "OPC_XERO_ENTITY_BINDINGS_JSON": "private-bindings",
                },
            ),
        ]
        for config, pack_id, environment in cases:
            with self.subTest(pack_id=pack_id):
                complete = build_connector_onboarding(
                    self.runtime(config), environ=environment,
                )
                group = next(
                    item for item in complete["provider_groups"]
                    if item["pack_id"] == pack_id
                )
                self.assertEqual(
                    group["diagnostic_status"],
                    "ready_to_initialize_private_access_probe_request",
                )
                self.assertIn(
                    f"--pack {pack_id}",
                    group["next_action"]["command_template"],
                )
                partial = build_connector_onboarding(
                    self.runtime(config),
                    environ={next(iter(environment)): "private-token"},
                )
                partial_group = next(
                    item for item in partial["provider_groups"]
                    if item["pack_id"] == pack_id
                )
                self.assertEqual(
                    partial_group["diagnostic_status"],
                    "blocked_missing_credential_reference",
                )

    def test_dynamic_connectors_require_entity_alias_bindings_for_access_probe(self):
        cases = [
            (
                "us_dtc_paypal_c_corp.json",
                "connector.paypal",
                {
                    "OPC_PAYPAL_ENTITY_BINDINGS_JSON": json.dumps({
                        "us_dtc_company": {
                            "environment": "production",
                            "app_id": "APPID_1234",
                            "account_id": "2ABCD3EFGH4JK",
                            "client_id_env": "OPC_PAYPAL_US_CLIENT_ID",
                            "client_secret_env": "OPC_PAYPAL_US_CLIENT_SECRET",
                        },
                    }),
                    "OPC_PAYPAL_US_CLIENT_ID": "private-client",
                    "OPC_PAYPAL_US_CLIENT_SECRET": "private-secret",
                },
                {
                    "OPC_PAYPAL_CLIENT_ID": "legacy-client",
                    "OPC_PAYPAL_CLIENT_SECRET": "legacy-secret",
                },
            ),
            (
                "us_dtc_woocommerce_c_corp.json",
                "connector.woocommerce",
                {
                    "OPC_WOOCOMMERCE_ENTITY_BINDINGS_JSON": json.dumps({
                        "us_dtc_company": {
                            "site_origin": "https://shop.example.com/store",
                            "key_permission": "read",
                            "consumer_key_env": "OPC_WC_US_KEY",
                            "consumer_secret_env": "OPC_WC_US_SECRET",
                        },
                    }),
                    "OPC_WC_US_KEY": "ck_private",
                    "OPC_WC_US_SECRET": "cs_private",
                },
                {
                    "OPC_WOOCOMMERCE_SITE_ORIGIN": "https://shop.example.com",
                    "OPC_WOOCOMMERCE_CONSUMER_KEY": "ck_legacy",
                    "OPC_WOOCOMMERCE_CONSUMER_SECRET": "cs_legacy",
                },
            ),
            (
                "us_dtc_shopify_stripe_shipbob_c_corp.json",
                "connector.shipbob",
                {
                    "OPC_SHIPBOB_ENTITY_BINDINGS_JSON": json.dumps({
                        "us_dtc_company": {
                            "environment": "production",
                            "channel_id": 100102,
                            "token_env": "OPC_SHIPBOB_US_TOKEN",
                        },
                    }),
                    "OPC_SHIPBOB_US_TOKEN": "private-token",
                },
                {"OPC_SHIPBOB_ACCESS_TOKEN": "legacy-token"},
            ),
            (
                "us_marketplace_amazon_seller_c_corp.json",
                "connector.amazon_seller",
                {
                    "OPC_AMAZON_SELLER_ENTITY_BINDINGS_JSON": json.dumps({
                        "us_amazon_marketplace_company": {
                            "environment": "production",
                            "region": "NA",
                            "seller_id": "A1SELLER12345",
                            "marketplace_ids": ["ATVPDKIKX0DER"],
                            "client_id_env": "OPC_AMAZON_US_CLIENT_ID",
                            "client_secret_env": "OPC_AMAZON_US_CLIENT_SECRET",
                            "refresh_token_env": "OPC_AMAZON_US_REFRESH_TOKEN",
                        },
                    }),
                    "OPC_AMAZON_US_CLIENT_ID": "private-client",
                    "OPC_AMAZON_US_CLIENT_SECRET": "private-secret",
                    "OPC_AMAZON_US_REFRESH_TOKEN": "private-refresh",
                },
                {
                    "OPC_AMAZON_SELLER_CLIENT_ID": "legacy-client",
                    "OPC_AMAZON_SELLER_CLIENT_SECRET": "legacy-secret",
                    "OPC_AMAZON_SELLER_REFRESH_TOKEN": "legacy-refresh",
                    "OPC_AMAZON_SELLER_REGION": "NA",
                    "OPC_AMAZON_SELLER_ID": "A1SELLER12345",
                    "OPC_AMAZON_SELLER_MARKETPLACE_IDS_JSON": json.dumps([
                        "ATVPDKIKX0DER",
                    ]),
                },
            ),
        ]
        for config, pack_id, environment, legacy_environment in cases:
            with self.subTest(pack_id=pack_id):
                ready = build_connector_onboarding(
                    self.runtime(config), environ=environment,
                )
                group = next(
                    item for item in ready["provider_groups"]
                    if item["pack_id"] == pack_id
                )
                self.assertEqual(
                    group["diagnostic_status"],
                    "ready_to_initialize_private_access_probe_request",
                )
                self.assertIn(
                    f"--pack {pack_id}",
                    group["next_action"]["command_template"],
                )
                self.assertTrue(all(
                    item["configured"] for item in group["credential_status"]
                ))
                legacy = build_connector_onboarding(
                    self.runtime(config), environ=legacy_environment,
                )
                legacy_group = next(
                    item for item in legacy["provider_groups"]
                    if item["pack_id"] == pack_id
                )
                self.assertEqual(
                    legacy_group["diagnostic_status"],
                    "blocked_missing_credential_reference",
                )
                serialized = json.dumps(ready)
                for private in environment.values():
                    self.assertNotIn(str(private), serialized)

    def test_game_primary_workflow_excludes_unreferenced_commerce_connectors(self):
        result = build_connector_onboarding(
            self.runtime("global_game_studio.json"), environ={},
        )
        primary_ids = {item["connector_id"] for item in result["pipeline_connectors"]}
        self.assertEqual(primary_ids, {
            "file.bank_statement",
            "file.general_ledger",
            "file.trial_balance",
            "file.app_store_settlements",
            "file.domestic_game_settlements",
            "file.google_play_settlements",
        })
        unreferenced_ids = {
            item["connector_id"] for item in result["available_unreferenced_connectors"]
        }
        self.assertIn("file.commerce", unreferenced_ids)
        self.assertTrue(
            result["control_boundary"]["unreferenced_connectors_hidden_from_primary_workflow"]
        )

    def test_marketplace_primary_workflow_keeps_only_marketplace_allowed_sources(self):
        result = build_connector_onboarding(
            self.runtime("cn_marketplace_store.json"), environ={},
        )
        self.assertEqual(
            {item["connector_id"] for item in result["pipeline_connectors"]},
            {
                "file.bank_statement", "file.general_ledger", "file.trial_balance",
                "example.marketplace_api_payload",
                "file.marketplace_commerce",
            },
        )


if __name__ == "__main__":
    unittest.main()
