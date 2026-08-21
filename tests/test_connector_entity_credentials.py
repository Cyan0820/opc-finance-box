from __future__ import annotations

import json
import unittest

from src.connector_entity_credentials import (
    AMAZON_SELLER_BINDINGS_ENV,
    ConnectorEntityCredentialError,
    PAYPAL_BINDINGS_ENV,
    SHIPBOB_BINDINGS_ENV,
    WOOCOMMERCE_BINDINGS_ENV,
    access_credentials_configured,
    resolve_amazon_seller_entity_credentials,
    resolve_paypal_entity_credentials,
    resolve_shipbob_entity_credentials,
    resolve_woocommerce_entity_credentials,
)


class ConnectorEntityCredentialTests(unittest.TestCase):
    def test_paypal_binding_selects_only_one_entity_and_dynamic_aliases(self):
        bindings = {
            "us_company": {
                "environment": "production",
                "app_id": "APPID_1234",
                "account_id": "2ABCD3EFGH4JK",
                "client_id_env": "OPC_PAYPAL_US_CLIENT_ID",
                "client_secret_env": "OPC_PAYPAL_US_CLIENT_SECRET",
            },
            "other_company": {
                "environment": "sandbox",
                "app_id": "OTHER_1234",
                "account_id": "3ABCD4EFGH5JK",
                "client_id_env": "OPC_PAYPAL_OTHER_CLIENT_ID",
                "client_secret_env": "OPC_PAYPAL_OTHER_CLIENT_SECRET",
            },
        }
        environment = {
            PAYPAL_BINDINGS_ENV: json.dumps(bindings),
            "OPC_PAYPAL_US_CLIENT_ID": "private-client",
            "OPC_PAYPAL_US_CLIENT_SECRET": "private-secret",
        }
        resolved = resolve_paypal_entity_credentials(
            "us_company", environment, legacy_environment="production",
        )
        self.assertTrue(resolved["entity_binding_used"])
        self.assertTrue(resolved["configured"])
        self.assertEqual(resolved["environment"], "production")
        self.assertEqual(
            resolved["env_names"],
            (
                PAYPAL_BINDINGS_ENV,
                "OPC_PAYPAL_US_CLIENT_ID",
                "OPC_PAYPAL_US_CLIENT_SECRET",
            ),
        )
        selected = json.loads(resolved["fingerprint_values"][PAYPAL_BINDINGS_ENV])
        self.assertEqual(selected, bindings["us_company"])
        self.assertNotIn("other_company", resolved["fingerprint_values"][PAYPAL_BINDINGS_ENV])

        with self.assertRaisesRegex(ConnectorEntityCredentialError, "does not match"):
            resolve_paypal_entity_credentials(
                "us_company", environment, legacy_environment="sandbox",
            )

    def test_paypal_legacy_fetch_remains_supported_but_access_requires_binding(self):
        environment = {
            "OPC_PAYPAL_CLIENT_ID": "legacy-client",
            "OPC_PAYPAL_CLIENT_SECRET": "legacy-secret",
        }
        legacy = resolve_paypal_entity_credentials(
            "us_company", environment, legacy_environment="sandbox",
        )
        self.assertFalse(legacy["entity_binding_used"])
        self.assertTrue(legacy["configured"])
        self.assertFalse(
            access_credentials_configured("connector.paypal", "us_company", environment)
        )
        with self.assertRaisesRegex(ConnectorEntityCredentialError, PAYPAL_BINDINGS_ENV):
            resolve_paypal_entity_credentials(
                "us_company",
                environment,
                legacy_environment="sandbox",
                require_entity_binding=True,
            )

    def test_woocommerce_binding_normalizes_origin_and_requires_declared_read_key(self):
        binding = {
            "us_company": {
                "site_origin": "https://SHOP.Example.com/store/",
                "key_permission": "read",
                "consumer_key_env": "OPC_WC_US_KEY",
                "consumer_secret_env": "OPC_WC_US_SECRET",
            },
        }
        environment = {
            WOOCOMMERCE_BINDINGS_ENV: json.dumps(binding),
            "OPC_WC_US_KEY": "ck_private",
            "OPC_WC_US_SECRET": "cs_private",
        }
        resolved = resolve_woocommerce_entity_credentials("us_company", environment)
        self.assertTrue(resolved["configured"])
        self.assertEqual(resolved["site_origin"], "https://shop.example.com/store")
        selected = json.loads(
            resolved["fingerprint_values"][WOOCOMMERCE_BINDINGS_ENV]
        )
        self.assertEqual(selected["site_origin"], "https://shop.example.com/store")
        self.assertEqual(selected["key_permission"], "read")

        unsafe = json.loads(json.dumps(binding))
        unsafe["us_company"]["key_permission"] = "read_write"
        with self.assertRaisesRegex(ConnectorEntityCredentialError, "must be read"):
            resolve_woocommerce_entity_credentials(
                "us_company",
                {**environment, WOOCOMMERCE_BINDINGS_ENV: json.dumps(unsafe)},
            )

        private_origin = json.loads(json.dumps(binding))
        private_origin["us_company"]["site_origin"] = "http://127.0.0.1"
        with self.assertRaisesRegex(ConnectorEntityCredentialError, "public HTTPS"):
            resolve_woocommerce_entity_credentials(
                "us_company",
                {**environment, WOOCOMMERCE_BINDINGS_ENV: json.dumps(private_origin)},
            )

    def test_dynamic_aliases_must_be_distinct_opc_environment_names(self):
        binding = {
            "us_company": {
                "site_origin": "https://shop.example.com",
                "key_permission": "read",
                "consumer_key_env": WOOCOMMERCE_BINDINGS_ENV,
                "consumer_secret_env": "NOT_AN_OPC_ALIAS",
            },
        }
        with self.assertRaises(ConnectorEntityCredentialError):
            resolve_woocommerce_entity_credentials(
                "us_company", {WOOCOMMERCE_BINDINGS_ENV: json.dumps(binding)},
            )

    def test_shipbob_binding_selects_entity_channel_and_token_alias(self):
        bindings = {
            "us_company": {
                "environment": "production",
                "channel_id": 100102,
                "token_env": "OPC_SHIPBOB_US_TOKEN",
            },
            "other_company": {
                "environment": "sandbox",
                "channel_id": 100103,
                "token_env": "OPC_SHIPBOB_OTHER_TOKEN",
            },
        }
        environment = {
            SHIPBOB_BINDINGS_ENV: json.dumps(bindings),
            "OPC_SHIPBOB_US_TOKEN": "private-token",
        }
        resolved = resolve_shipbob_entity_credentials(
            "us_company", environment, legacy_environment="production",
        )
        self.assertTrue(resolved["configured"])
        self.assertEqual(resolved["channel_id"], 100102)
        self.assertEqual(
            resolved["env_names"],
            (SHIPBOB_BINDINGS_ENV, "OPC_SHIPBOB_US_TOKEN"),
        )
        selected = json.loads(
            resolved["fingerprint_values"][SHIPBOB_BINDINGS_ENV]
        )
        self.assertEqual(selected, bindings["us_company"])
        with self.assertRaisesRegex(ConnectorEntityCredentialError, "does not match"):
            resolve_shipbob_entity_credentials(
                "us_company", environment, legacy_environment="sandbox",
            )
        self.assertFalse(access_credentials_configured(
            "connector.shipbob", "us_company",
            {"OPC_SHIPBOB_ACCESS_TOKEN": "legacy-token"},
        ))

    def test_amazon_binding_selects_entity_seller_marketplaces_and_aliases(self):
        selected = {
            "environment": "production",
            "region": "NA",
            "seller_id": "A1SELLER12345",
            "marketplace_ids": ["ATVPDKIKX0DER", "A2EUQ1WTGCTBG2"],
            "client_id_env": "OPC_AMAZON_US_CLIENT_ID",
            "client_secret_env": "OPC_AMAZON_US_CLIENT_SECRET",
            "refresh_token_env": "OPC_AMAZON_US_REFRESH_TOKEN",
        }
        environment = {
            AMAZON_SELLER_BINDINGS_ENV: json.dumps({
                "us_company": selected,
                "other_company": {
                    **selected,
                    "seller_id": "A2SELLER12345",
                    "client_id_env": "OPC_AMAZON_OTHER_CLIENT_ID",
                    "client_secret_env": "OPC_AMAZON_OTHER_CLIENT_SECRET",
                    "refresh_token_env": "OPC_AMAZON_OTHER_REFRESH_TOKEN",
                },
            }),
            "OPC_AMAZON_US_CLIENT_ID": "private-client",
            "OPC_AMAZON_US_CLIENT_SECRET": "private-secret",
            "OPC_AMAZON_US_REFRESH_TOKEN": "private-refresh",
        }
        resolved = resolve_amazon_seller_entity_credentials(
            "us_company", environment, legacy_environment="production",
        )
        self.assertTrue(resolved["configured"])
        self.assertEqual(resolved["seller_id"], "A1SELLER12345")
        self.assertEqual(
            resolved["marketplace_ids"],
            ["A2EUQ1WTGCTBG2", "ATVPDKIKX0DER"],
        )
        self.assertEqual(len(resolved["env_names"]), 4)
        fingerprint_binding = json.loads(
            resolved["fingerprint_values"][AMAZON_SELLER_BINDINGS_ENV]
        )
        self.assertNotIn("other_company", fingerprint_binding)
        self.assertEqual(
            fingerprint_binding["marketplace_ids"],
            ["A2EUQ1WTGCTBG2", "ATVPDKIKX0DER"],
        )
        with self.assertRaisesRegex(ConnectorEntityCredentialError, "must be NA"):
            bad = {"us_company": {**selected, "region": "CUSTOM"}}
            resolve_amazon_seller_entity_credentials(
                "us_company",
                {**environment, AMAZON_SELLER_BINDINGS_ENV: json.dumps(bad)},
            )


if __name__ == "__main__":
    unittest.main()
