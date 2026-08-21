import json
import tempfile
import unittest
from pathlib import Path

from src.box_config import (
    BoxConfigError,
    load_box_config,
    load_jurisdiction_rules,
    load_pack_catalog,
    resolve_box,
    resolve_box_file,
    validate_box_config,
)


ROOT = Path(__file__).resolve().parents[1]
PACKS = ROOT / "packs"
EXAMPLES = ROOT / "examples" / "boxes"


class BoxConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = load_pack_catalog(PACKS)

    def test_pack_catalog_has_composable_product_layers(self):
        kinds = {pack.kind for pack in self.catalog.all()}
        self.assertEqual(
            kinds,
            {"core", "industry", "channel", "jurisdiction", "connector", "feature"},
        )
        self.assertIn("industry.game_studio", self.catalog)
        self.assertIn("industry.commerce", self.catalog)
        self.assertIn("channel.dtc_storefront", self.catalog)
        self.assertIn("jurisdiction.cn_mainland", self.catalog)

    def test_global_game_example_resolves_two_legal_entities(self):
        resolved = resolve_box_file(EXAMPLES / "global_game_studio.json", PACKS)
        self.assertEqual([entity["jurisdiction"] for entity in resolved["entities"]], ["CN", "SG"])
        self.assertIn("game.channel_settlement", resolved["capabilities"])
        self.assertIn("entity.management_consolidation", resolved["capabilities"])
        self.assertIn("period_close", resolved["manual_review_gates"])
        self.assertTrue(any("jurisdiction.sg" in warning for warning in resolved["warnings"]))
        self.assertEqual(resolved["entities"][1]["tax_readiness"], "design")

    def test_dtc_example_combines_commerce_channel_and_cn_tax(self):
        resolved = resolve_box_file(EXAMPLES / "cn_dtc_store.json", PACKS)
        pack_ids = {pack["id"] for pack in resolved["packs"]}
        self.assertIn("industry.commerce", pack_ids)
        self.assertIn("channel.dtc_storefront", pack_ids)
        self.assertIn("jurisdiction.cn_mainland", pack_ids)
        self.assertIn("commerce.product_margin", resolved["capabilities"])
        self.assertIn("channel.dtc_destination_summary", resolved["capabilities"])

    def test_tax_pack_must_match_legal_entity_jurisdiction(self):
        config = load_box_config(EXAMPLES / "cn_dtc_store.json")
        config["entities"][0]["tax_pack"] = "jurisdiction.sg"
        errors = validate_box_config(config, self.catalog)
        self.assertTrue(any("does not match tax pack" in error for error in errors))

    def test_channel_dependency_prevents_wrong_industry_combination(self):
        config = load_box_config(EXAMPLES / "cn_dtc_store.json")
        config["business_models"] = ["industry.game_studio"]
        errors = validate_box_config(config, self.catalog)
        self.assertIn("pack channel.dtc_storefront requires industry.commerce", errors)

    def test_invalid_configuration_is_not_partially_resolved(self):
        config = load_box_config(EXAMPLES / "global_game_studio.json")
        config["entities"][0]["functional_currency"] = "CN"
        with self.assertRaises(BoxConfigError):
            resolve_box(config, self.catalog)

    def test_connector_bindings_are_complete_canonical_and_entity_scoped(self):
        config = load_box_config(EXAMPLES / "global_game_studio_xero.json")
        config["connector_bindings"] = [
            {"connector_pack": "connector.xero", "entity_ids": ["sg_publisher"]},
            {"connector_pack": "connector.file_import", "entity_ids": ["sg_publisher", "cn_studio"]},
        ]
        resolved = resolve_box(config, self.catalog)
        self.assertEqual(resolved["connector_binding_mode"], "explicit")
        self.assertEqual(resolved["connector_bindings"], [
            {"connector_pack": "connector.file_import", "entity_ids": ["cn_studio", "sg_publisher"]},
            {"connector_pack": "connector.xero", "entity_ids": ["sg_publisher"]},
        ])

    def test_connector_bindings_fail_closed_on_missing_unknown_and_duplicate_scope(self):
        config = load_box_config(EXAMPLES / "global_game_studio_xero.json")
        config["connector_bindings"] = [
            {"connector_pack": "connector.xero", "entity_ids": ["missing"]},
        ]
        errors = validate_box_config(config, self.catalog)
        self.assertTrue(any("missing: connector.file_import" in error for error in errors))
        self.assertTrue(any("unknown legal entities: missing" in error for error in errors))
        config["connector_bindings"] = [
            {"connector_pack": "connector.file_import", "entity_ids": ["cn_studio"]},
            {"connector_pack": "connector.file_import", "entity_ids": ["sg_publisher"]},
            {"connector_pack": "connector.xero", "entity_ids": ["sg_publisher"]},
        ]
        self.assertTrue(any(
            "duplicate connector binding" in error
            for error in validate_box_config(config, self.catalog)
        ))

    def test_alias_credential_connectors_allow_explicit_multi_entity_bindings(self):
        for filename, connector_pack in (
            ("us_dtc_paypal_c_corp.json", "connector.paypal"),
            ("us_dtc_woocommerce_c_corp.json", "connector.woocommerce"),
            ("us_dtc_shopify_stripe_shipbob_c_corp.json", "connector.shipbob"),
            ("us_marketplace_amazon_seller_c_corp.json", "connector.amazon_seller"),
        ):
            with self.subTest(connector_pack=connector_pack):
                config = load_box_config(EXAMPLES / filename)
                second = json.loads(json.dumps(config["entities"][0]))
                second["id"] = "us_second_company"
                second["name"] = "Second US legal entity (confirm)"
                config["entities"].append(second)
                config["features"] = ["feature.multi_entity"]
                config["connector_bindings"] = [
                    {
                        "connector_pack": "connector.file_import",
                        "entity_ids": ["us_dtc_company", "us_second_company"],
                    },
                    {
                        "connector_pack": connector_pack,
                        "entity_ids": ["us_dtc_company", "us_second_company"],
                    },
                ]
                if connector_pack == "connector.shipbob":
                    config["connectors"] = [
                        "connector.file_import", "connector.shipbob",
                    ]
                    config["connector_bindings"][0]["entity_ids"] = [
                        "us_dtc_company", "us_second_company",
                    ]
                elif connector_pack == "connector.amazon_seller":
                    config["entities"][0]["id"] = "us_dtc_company"
                    config["connectors"] = [
                        "connector.file_import", "connector.amazon_seller",
                    ]
                resolved = resolve_box(config, self.catalog)
                binding = next(
                    item for item in resolved["connector_bindings"]
                    if item["connector_pack"] == connector_pack
                )
                self.assertEqual(
                    binding["entity_ids"],
                    ["us_dtc_company", "us_second_company"],
                )

    def test_catalog_rejects_unknown_dependencies(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = root / "bad" / "manifest.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({
                "id": "industry.bad",
                "kind": "industry",
                "display_name": "Bad",
                "version": "0.1.0",
                "status": "experimental",
                "requires": ["core.missing"]
            }), encoding="utf-8")
            with self.assertRaises(BoxConfigError):
                load_pack_catalog(root)

    def test_jurisdiction_rule_cannot_reference_unknown_source(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "rules.json"
            path.write_text(json.dumps({
                "schema_version": 1,
                "jurisdiction": "CN",
                "verified_at": "2026-08-13",
                "sources": [{
                    "id": "official",
                    "authority": "Authority",
                    "title": "Official source",
                    "url": "https://example.gov/rule"
                }],
                "rules": [{
                    "id": "cn.bad",
                    "source_ids": ["missing"],
                    "automation_level": "workpaper",
                    "human_review_required": True
                }]
            }), encoding="utf-8")
            with self.assertRaises(BoxConfigError):
                load_jurisdiction_rules(path, "CN")

    def test_jurisdiction_rules_require_strict_review_policy(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "rules.json"
            payload = json.loads(
                (ROOT / "packs" / "jurisdictions" / "sg" / "rules.json").read_text(
                    encoding="utf-8"
                )
            )
            payload.pop("review_policy")
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(BoxConfigError, "review_policy"):
                load_jurisdiction_rules(path, "SG")
            payload["review_policy"] = {
                "max_age_days": 180,
                "warning_days_before_expiry": 180,
                "expiry_effect": "block_external_filing_and_calendar_release",
                "reverification_triggers": ["pack_upgrade"],
            }
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(BoxConfigError, "warning_days_before_expiry"):
                load_jurisdiction_rules(path, "SG")

    def test_jurisdiction_rules_require_strict_applicability_review_policy(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "rules.json"
            payload = json.loads(
                (ROOT / "packs" / "jurisdictions" / "sg" / "rules.json").read_text(
                    encoding="utf-8"
                )
            )
            payload.pop("applicability_review_policy")
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(BoxConfigError, "applicability_review_policy"):
                load_jurisdiction_rules(path, "SG")
            payload["applicability_review_policy"] = {
                "max_age_days": 365,
                "warning_days_before_expiry": 30,
                "expiry_effect": "block_calendar_and_external_filing_release",
                "reverification_triggers": ["pack_upgrade"],
            }
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(BoxConfigError, "reverification_triggers"):
                load_jurisdiction_rules(path, "SG")


if __name__ == "__main__":
    unittest.main()
