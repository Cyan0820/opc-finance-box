import tempfile
import unittest
from pathlib import Path

from src.box_config import load_pack_catalog, resolve_box
from src.box_scaffold import BoxScaffoldError, create_box_config, list_box_options


ROOT = Path(__file__).resolve().parents[1]


class BoxScaffoldTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = load_pack_catalog(ROOT / "packs")

    def test_options_are_derived_from_installed_packs(self):
        options = list_box_options(self.catalog)
        country_codes = {item["country_code"] for item in options["jurisdictions"]}
        self.assertEqual(
            country_codes, {"AE", "AU", "CA", "CN", "DE", "FR", "GB", "HK", "IE", "JP", "KR", "NL", "NZ", "SG", "US"},
        )
        self.assertTrue(all("tax_readiness" in item for item in options["jurisdictions"]))
        self.assertTrue(all(
            item["rules_verified_at"] and item["review_policy"]["max_age_days"] == 180
            for item in options["jurisdictions"]
        ))
        self.assertTrue(all(
            item["review_policy"]["expiry_effect"]
            == "block_external_filing_and_calendar_release"
            for item in options["jurisdictions"]
        ))
        self.assertTrue(all(
            item["applicability_review_policy"]["max_age_days"] == 365
            and item["applicability_review_policy"]["expiry_effect"]
            == "block_calendar_and_external_filing_release"
            for item in options["jurisdictions"]
        ))
        self.assertIn("shopify_stripe", {
            item["id"] for item in options["integration_presets"]
        })
        self.assertIn("xero", {
            item["id"] for item in options["integration_presets"]
        })
        self.assertIn("paypal", {
            item["id"] for item in options["integration_presets"]
        })

    def test_dtc_aliases_create_a_strict_valid_config(self):
        spec = {
            "name": "DTC",
            "business_type": "commerce",
            "channels": ["dtc"],
            "entities": [{
                "id": "store",
                "name": "Store",
                "tax_country": "CN",
                "functional_currency": "CNY",
                "accounting_basis": "PRC_GAAP",
            }],
        }
        config = create_box_config(spec, self.catalog)
        self.assertEqual(config["business_models"], ["industry.commerce"])
        self.assertEqual(config["channels"], ["channel.dtc_storefront"])
        self.assertEqual(config["entities"][0]["tax_pack"], "jurisdiction.cn_mainland")
        self.assertEqual(config["reporting_currency"], "CNY")
        self.assertEqual(resolve_box(config, self.catalog)["name"], "DTC")

    def test_multiple_entities_add_feature_and_require_reporting_currency(self):
        entities = [
            {"id": "cn", "name": "CN", "tax_country": "CN", "functional_currency": "CNY", "accounting_basis": "PRC_GAAP"},
            {"id": "sg", "name": "SG", "tax_country": "SG", "functional_currency": "USD", "accounting_basis": "SFRS"},
        ]
        with self.assertRaisesRegex(BoxScaffoldError, "reporting_currency"):
            create_box_config({"name": "Game", "business_type": "game", "entities": entities}, self.catalog)
        config = create_box_config({
            "name": "Game",
            "business_type": "game",
            "reporting_currency": "CNY",
            "entities": entities,
        }, self.catalog)
        self.assertIn("feature.multi_entity", config["features"])

    def test_shopify_stripe_preset_expands_complete_editable_stack(self):
        spec = {
            "name": "Shopify DTC",
            "business_type": "ecommerce",
            "channels": ["dtc"],
            "integrations": ["shopify_stripe"],
            "entities": [{
                "id": "store", "name": "Store", "tax_country": "CN",
                "functional_currency": "CNY", "accounting_basis": "PRC_GAAP",
            }],
        }
        config = create_box_config(spec, self.catalog)
        self.assertEqual(config["connectors"], [
            "connector.file_import", "connector.shopify", "connector.stripe",
        ])
        self.assertEqual(config["features"], ["feature.shopify_stripe_order_to_cash"])
        resolved = resolve_box(config, self.catalog)
        self.assertIn("integration.shopify_stripe_order_to_cash", resolved["capabilities"])

    def test_same_shopify_stack_can_select_singapore_tax_pack(self):
        config = create_box_config({
            "name": "SG Shopify DTC",
            "business_type": "commerce",
            "channels": ["dtc"],
            "integrations": ["shopify_stripe"],
            "entities": [{
                "id": "sg_store", "name": "SG Store", "tax_country": "SG",
                "functional_currency": "USD", "accounting_basis": "SFRS",
            }],
        }, self.catalog)
        self.assertEqual(config["entities"][0]["tax_pack"], "jurisdiction.sg")
        self.assertEqual(config["reporting_currency"], "USD")
        self.assertIn("connector.shopify", config["connectors"])

    def test_xero_preset_is_industry_and_tax_country_orthogonal(self):
        config = create_box_config({
            "name": "Game Xero",
            "business_type": "game",
            "channels": ["app_store"],
            "integrations": ["xero"],
            "entities": [{
                "id": "game_company", "name": "Game Company", "tax_country": "NL",
                "functional_currency": "EUR", "accounting_basis": "Dutch_GAAP",
            }],
        }, self.catalog)
        self.assertEqual(config["connectors"], ["connector.file_import", "connector.xero"])
        self.assertEqual(config["entities"][0]["tax_pack"], "jurisdiction.nl_private_limited_company")

        combined = create_box_config({
            "name": "DTC Full",
            "business_type": "commerce",
            "channels": ["dtc"],
            "integrations": ["shopify_stripe", "xero"],
            "entities": [{
                "id": "store", "name": "Store", "tax_country": "US",
                "functional_currency": "USD", "accounting_basis": "US_GAAP",
            }],
        }, self.catalog)
        self.assertEqual(combined["connectors"], [
            "connector.file_import", "connector.shopify", "connector.stripe", "connector.xero",
        ])

    def test_same_shopify_stack_can_select_hong_kong_tax_pack(self):
        spec = __import__("json").loads(
            (ROOT / "examples" / "box_specs" / "shopify_stripe_hk.json").read_text(
                encoding="utf-8"
            )
        )
        config = create_box_config(spec, self.catalog)
        self.assertEqual(config["entities"][0]["tax_pack"], "jurisdiction.hk")
        self.assertEqual(config["reporting_currency"], "HKD")
        self.assertIn("feature.shopify_stripe_order_to_cash", config["features"])

    def test_same_shopify_stack_can_select_uk_limited_company_pack(self):
        spec = __import__("json").loads(
            (ROOT / "examples" / "box_specs" / "shopify_stripe_uk_ltd.json").read_text(
                encoding="utf-8"
            )
        )
        config = create_box_config(spec, self.catalog)
        self.assertEqual(
            config["entities"][0]["tax_pack"], "jurisdiction.uk_limited_company",
        )
        self.assertEqual(config["reporting_currency"], "GBP")

    def test_same_shopify_stack_can_select_australia_proprietary_company_pack(self):
        spec = __import__("json").loads(
            (ROOT / "examples" / "box_specs" / "shopify_stripe_au_pty_ltd.json").read_text(
                encoding="utf-8"
            )
        )
        config = create_box_config(spec, self.catalog)
        self.assertEqual(
            config["entities"][0]["tax_pack"],
            "jurisdiction.au_proprietary_company",
        )
        self.assertEqual(config["reporting_currency"], "AUD")

    def test_same_shopify_stack_can_select_canada_federal_corporation_pack(self):
        spec = __import__("json").loads(
            (
                ROOT / "examples" / "box_specs"
                / "shopify_stripe_ca_federal_corporation.json"
            ).read_text(encoding="utf-8")
        )
        config = create_box_config(spec, self.catalog)
        self.assertEqual(
            config["entities"][0]["tax_pack"],
            "jurisdiction.ca_federal_corporation",
        )
        self.assertEqual(config["reporting_currency"], "CAD")

    def test_same_shopify_stack_can_select_new_zealand_limited_company_pack(self):
        spec = __import__("json").loads(
            (
                ROOT / "examples" / "box_specs"
                / "shopify_stripe_nz_limited_company.json"
            ).read_text(encoding="utf-8")
        )
        config = create_box_config(spec, self.catalog)
        self.assertEqual(
            config["entities"][0]["tax_pack"],
            "jurisdiction.nz_limited_company",
        )
        self.assertEqual(config["reporting_currency"], "NZD")

    def test_same_shopify_stack_can_select_germany_gmbh_pack(self):
        spec = __import__("json").loads(
            (
                ROOT / "examples" / "box_specs"
                / "shopify_stripe_de_gmbh.json"
            ).read_text(encoding="utf-8")
        )
        config = create_box_config(spec, self.catalog)
        self.assertEqual(
            config["entities"][0]["tax_pack"],
            "jurisdiction.de_limited_liability_company",
        )
        self.assertEqual(config["reporting_currency"], "EUR")

    def test_same_shopify_stack_can_select_france_sasu_pack(self):
        spec = __import__("json").loads(
            (
                ROOT / "examples" / "box_specs"
                / "shopify_stripe_fr_sasu.json"
            ).read_text(encoding="utf-8")
        )
        config = create_box_config(spec, self.catalog)
        self.assertEqual(
            config["entities"][0]["tax_pack"],
            "jurisdiction.fr_single_member_simplified_joint_stock_company",
        )
        self.assertEqual(config["reporting_currency"], "EUR")

    def test_unknown_integration_preset_lists_available_stacks(self):
        with self.assertRaisesRegex(
            BoxScaffoldError,
            "available: airwallex, amazon_seller, paypal, shipbob, shopify, shopify_stripe, shopify_stripe_wise, shopify_stripe_wise_airwallex, shopify_stripe_xero, stripe, wise, woocommerce, xero",
        ):
            create_box_config({
                "name": "Bad Integration",
                "business_type": "commerce",
                "channels": ["dtc"],
                "integrations": ["unknown"],
                "entities": [{
                    "id": "store", "name": "Store", "tax_country": "CN",
                    "functional_currency": "CNY", "accounting_basis": "PRC_GAAP",
                }],
            }, self.catalog)

    def test_unsupported_country_lists_installed_options(self):
        with self.assertRaisesRegex(
            BoxScaffoldError, "available: AE, AU, CA, CN, DE, FR, GB, HK, IE, JP, KR, NL, NZ, SG, US",
        ):
            create_box_config({
                "name": "DE Box",
                "business_type": "commerce",
                "entities": [{
                    "id": "es",
                    "name": "ES",
                    "tax_country": "ES",
                    "functional_currency": "EUR",
                    "accounting_basis": "LOCAL_GAAP",
                }],
            }, self.catalog)

    def test_wrong_channel_for_business_is_rejected_by_pack_dependencies(self):
        with self.assertRaisesRegex(BoxScaffoldError, "requires industry.game_studio"):
            create_box_config({
                "name": "Bad",
                "business_type": "commerce",
                "channels": ["app_store"],
                "entities": [{
                    "id": "cn",
                    "name": "CN",
                    "tax_country": "CN",
                    "functional_currency": "CNY",
                    "accounting_basis": "PRC_GAAP",
                }],
            }, self.catalog)

    def test_duplicate_country_packs_only_block_implicit_country_selection(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for path in (ROOT / "packs").rglob("*"):
                if not path.is_file():
                    continue
                target = root / path.relative_to(ROOT / "packs")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(path.read_bytes())
            regional = root / "jurisdictions" / "cn_special"
            regional.mkdir(parents=True)
            manifest = __import__("json").loads(
                (root / "jurisdictions" / "cn_mainland" / "manifest.json").read_text(encoding="utf-8")
            )
            manifest.update({"id": "jurisdiction.cn_special", "display_name": "CN Special"})
            (regional / "manifest.json").write_text(
                __import__("json").dumps(manifest, ensure_ascii=False), encoding="utf-8"
            )
            (regional / "rules.json").write_bytes(
                (root / "jurisdictions" / "cn_mainland" / "rules.json").read_bytes()
            )
            catalog = load_pack_catalog(root)
            base = {
                "name": "Regional", "business_type": "commerce",
                "entities": [{
                    "id": "cn", "name": "CN", "tax_country": "CN",
                    "functional_currency": "CNY", "accounting_basis": "PRC_GAAP",
                }],
            }
            with self.assertRaisesRegex(BoxScaffoldError, "Multiple tax packs exist for CN"):
                create_box_config(base, catalog)
            base["entities"][0]["tax_pack"] = "jurisdiction.cn_special"
            config = create_box_config(base, catalog)
            self.assertEqual(config["entities"][0]["tax_pack"], "jurisdiction.cn_special")

    def test_airwallex_preset_is_industry_and_tax_country_orthogonal(self):
        spec = __import__("json").loads((
            ROOT / "examples" / "box_specs" / "shopify_stripe_wise_airwallex_sg.json"
        ).read_text(encoding="utf-8"))
        config = create_box_config(spec, self.catalog)
        self.assertIn("connector.airwallex", config["connectors"])
        self.assertEqual(config["entities"][0]["tax_pack"], "jurisdiction.sg")
        self.assertEqual(config["reporting_currency"], "SGD")


if __name__ == "__main__":
    unittest.main()
