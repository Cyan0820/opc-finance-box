from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.box_builder import build_box_starter_catalog
from src.box_config import load_pack_catalog
from src.handoff_unpack import verify_unpacked_box_candidate
from src.starter_workspace import (
    StarterWorkspaceError,
    initialize_box_starter_workspace,
    initialize_multi_entity_starter_workspace,
)


ROOT = Path(__file__).resolve().parents[1]
PACKS = ROOT / "packs"


class StarterWorkspaceTests(unittest.TestCase):
    def test_every_installed_profile_country_pair_initializes_and_reverifies(self):
        catalog = build_box_starter_catalog(load_pack_catalog(PACKS))
        self.assertEqual(catalog["ready_combination_count"], 45)
        self.assertEqual(catalog["unavailable_combinations"], [])
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name).resolve()
            observed: set[str] = set()
            representative_workspaces: dict[str, Path] = {}
            for entry in catalog["entries"]:
                destination = root / entry["id"]
                result = initialize_box_starter_workspace(
                    profile=entry["profile_id"],
                    country=entry["country_code"].lower(),
                    packs_root=PACKS,
                    destination_root=destination,
                    actor="starter-matrix-auditor",
                )
                observed.add(result["starter_id"])
                self.assertTrue(result["initialized"])
                self.assertTrue(result["workspace_verified"])
                self.assertEqual(result["compiled_file_count"], 42)
                self.assertEqual(result["workspace_file_count"], 56)
                self.assertEqual(result["workspace_directory_count"], 3)
                self.assertEqual(result["selected_integrations"], [])
                self.assertTrue(result["requires_local_confirmation"])
                self.assertTrue(result["tax_registrations_default_to_empty"])
                self.assertFalse(result["filing_ready"])
                self.assertFalse(result["source_bundle_materialized"])
                self.assertFalse(result["destination_path_returned"])
                representative_workspaces.setdefault(entry["profile_id"], destination)
                spec = json.loads((destination / "box-spec.json").read_text())
                config = json.loads((destination / "box.json").read_text())
                self.assertEqual(spec["entities"][0]["tax_country"], entry["country_code"])
                self.assertEqual(spec["entities"][0]["tax_pack"], entry["jurisdiction_id"])
                self.assertEqual(spec["entities"][0]["tax_registrations"], [])
                self.assertEqual(config["entities"][0]["tax_pack"], entry["jurisdiction_id"])
                self.assertEqual(config["data_mode"], "demo")
            self.assertEqual(observed, {entry["id"] for entry in catalog["entries"]})
            self.assertEqual(set(representative_workspaces), {"game", "dtc", "marketplace"})
            for destination in representative_workspaces.values():
                verified = verify_unpacked_box_candidate(destination, PACKS)
                self.assertTrue(verified["valid"])
                self.assertFalse(verified["source_bundle_required"])

    def test_allowed_integrations_and_identity_overrides_are_compiled(self):
        cases = (
            ("dtc", "NL", "shopify+stripe", {"connector.shopify", "connector.stripe"}),
            ("game", "CN", "xero", {"connector.xero"}),
            ("marketplace", "US", "wise", {"connector.wise"}),
        )
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name).resolve()
            for index, (profile, country, integration, expected) in enumerate(cases):
                destination = root / f"case-{index}"
                result = initialize_box_starter_workspace(
                    profile=profile,
                    country=country,
                    packs_root=PACKS,
                    destination_root=destination,
                    actor="starter-customization-auditor",
                    integrations=[integration],
                    name=f"Custom {profile} Box",
                    entity_id=f"custom_{profile}_entity",
                    entity_name=f"Custom {profile} legal entity",
                    data_mode="live",
                )
                config = json.loads((destination / "box.json").read_text())
                spec = json.loads((destination / "box-spec.json").read_text())
                self.assertEqual(config["name"], f"Custom {profile} Box")
                self.assertEqual(config["data_mode"], "live")
                self.assertEqual(config["entities"][0]["id"], f"custom_{profile}_entity")
                self.assertEqual(
                    config["entities"][0]["name"], f"Custom {profile} legal entity",
                )
                self.assertTrue(expected.issubset(config["connectors"]))
                self.assertEqual(spec["integrations"], result["selected_integrations"])
                self.assertTrue(result["workspace_verified"])

    def test_invalid_selection_and_existing_destination_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name).resolve()
            cases = (
                ({"profile": "unknown", "country": "NL"}, "unavailable"),
                ({"profile": "dtc", "country": "ZZ"}, "unavailable"),
                (
                    {"profile": "game", "country": "CN", "integrations": ["stripe"]},
                    "not allowed",
                ),
                (
                    {"profile": "dtc", "country": "NL", "integrations": ["wise", "wise"]},
                    "duplicated",
                ),
                (
                    {"profile": "dtc", "country": "NL", "entity_id": "Bad ID"},
                    "entity_id",
                ),
                (
                    {"profile": "dtc", "country": "NL", "data_mode": "staging"},
                    "data_mode",
                ),
            )
            for index, (overrides, message) in enumerate(cases):
                destination = root / f"invalid-{index}"
                args = {
                    "profile": "dtc",
                    "country": "NL",
                    "packs_root": PACKS,
                    "destination_root": destination,
                    "actor": "starter-negative-auditor",
                    **overrides,
                }
                with self.assertRaisesRegex(StarterWorkspaceError, message):
                    initialize_box_starter_workspace(**args)
                self.assertFalse(destination.exists())

            existing = root / "existing"
            existing.mkdir()
            marker = existing / "keep.txt"
            marker.write_text("keep", encoding="utf-8")
            with self.assertRaisesRegex(StarterWorkspaceError, "already exists"):
                initialize_box_starter_workspace(
                    profile="dtc",
                    country="NL",
                    packs_root=PACKS,
                    destination_root=existing,
                    actor="starter-negative-auditor",
                )
            self.assertEqual(marker.read_text(encoding="utf-8"), "keep")

    def test_same_selection_has_stable_spec_and_runtime_fingerprints(self):
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name).resolve()
            first = initialize_box_starter_workspace(
                profile="dtc",
                country="NL",
                packs_root=PACKS,
                destination_root=root / "first",
                actor="first-recipient",
                integrations=["shopify_stripe"],
            )
            second = initialize_box_starter_workspace(
                profile="dtc",
                country="nl",
                packs_root=PACKS,
                destination_root=root / "second",
                actor="second-recipient",
                integrations=["shopify-stripe"],
            )
            self.assertEqual(
                first["initialized_spec_sha256"], second["initialized_spec_sha256"],
            )
            self.assertEqual(first["runtime_fingerprint"], second["runtime_fingerprint"])
            self.assertNotEqual(
                first["workspace_receipt_sha256"], second["workspace_receipt_sha256"],
            )

    def test_multi_entity_composer_keeps_entity_tax_and_book_boundaries(self):
        with tempfile.TemporaryDirectory() as temp_name:
            destination = Path(temp_name).resolve() / "composed"
            result = initialize_multi_entity_starter_workspace(
                profile="dtc",
                entities=["CN=cn_operations", "NL=nl_sales", "US=us_marketplace"],
                packs_root=PACKS,
                destination_root=destination,
                actor="multi-entity-composer-auditor",
                entity_integrations=["nl_sales=shopify+stripe"],
                entity_names=[
                    "cn_operations=China operations entity (confirm)",
                    "nl_sales=Netherlands sales entity (confirm)",
                ],
                reporting_currency="usd",
                name="Cross-border DTC OPC",
                data_mode="live",
            )
            self.assertTrue(result["initialized"])
            self.assertEqual(result["composition_type"], "same_profile_multi_entity")
            self.assertEqual(result["entity_count"], 3)
            self.assertEqual(result["reporting_currency"], "USD")
            self.assertTrue(result["reporting_currency_explicit"])
            self.assertTrue(result["cross_currency"])
            self.assertTrue(result["multi_entity_feature_selected"])
            self.assertTrue(result["entity_books_separate"])
            self.assertFalse(result["cross_currency_aggregation_authorized"])
            self.assertFalse(result["fx_rates_added"])
            self.assertFalse(result["filing_ready"])
            self.assertTrue(result["workspace_verified"])
            self.assertEqual(result["compiled_file_count"], 42)
            self.assertEqual(result["workspace_file_count"], 56)

            spec = json.loads((destination / "box-spec.json").read_text())
            config = json.loads((destination / "box.json").read_text())
            self.assertEqual(config["name"], "Cross-border DTC OPC")
            self.assertEqual(config["data_mode"], "live")
            self.assertEqual(config["reporting_currency"], "USD")
            self.assertIn("feature.multi_entity", config["features"])
            self.assertTrue({"connector.shopify", "connector.stripe"} <= set(config["connectors"]))
            self.assertEqual(result["connector_binding_mode"], "explicit")
            self.assertEqual(config["connector_bindings"], [
                {
                    "connector_pack": "connector.file_import",
                    "entity_ids": ["cn_operations", "nl_sales", "us_marketplace"],
                },
                {"connector_pack": "connector.shopify", "entity_ids": ["nl_sales"]},
                {"connector_pack": "connector.stripe", "entity_ids": ["nl_sales"]},
            ])
            self.assertEqual(
                {entity["id"] for entity in config["entities"]},
                {"cn_operations", "nl_sales", "us_marketplace"},
            )
            self.assertEqual(
                {entity["tax_pack"] for entity in config["entities"]},
                {
                    "jurisdiction.cn_mainland",
                    "jurisdiction.nl_private_limited_company",
                    "jurisdiction.us_federal",
                },
            )
            self.assertTrue(all(entity["tax_registrations"] == [] for entity in spec["entities"]))
            verified = verify_unpacked_box_candidate(destination, PACKS)
            self.assertTrue(verified["valid"])
            self.assertTrue(verified["installed_pack_reproducible"])

    def test_multi_entity_composer_supports_same_country_and_currency(self):
        with tempfile.TemporaryDirectory() as temp_name:
            destination = Path(temp_name).resolve() / "same-country"
            result = initialize_multi_entity_starter_workspace(
                profile="marketplace",
                entities=["NL=nl_store_one", "NL=nl_store_two"],
                packs_root=PACKS,
                destination_root=destination,
                actor="same-country-composer-auditor",
            )
            self.assertEqual(result["entity_count"], 2)
            self.assertEqual(result["reporting_currency"], "EUR")
            self.assertFalse(result["reporting_currency_explicit"])
            self.assertFalse(result["cross_currency"])
            self.assertEqual(result["starter_ids"], ["marketplace.nl", "marketplace.nl"])
            config = json.loads((destination / "box.json").read_text())
            self.assertEqual(
                [entity["id"] for entity in config["entities"]],
                ["nl_store_one", "nl_store_two"],
            )
            self.assertEqual(
                [entity["tax_pack"] for entity in config["entities"]],
                ["jurisdiction.nl_private_limited_company"] * 2,
            )

    def test_multi_entity_composer_invalid_requests_fail_before_destination(self):
        cases = (
            ({"entities": ["CN"]}, "at least two"),
            ({"entities": ["CN", "NL"]}, "reporting_currency is required"),
            ({"entities": ["CN", "CN"]}, "duplicated"),
            ({"entities": ["CN=Bad", "NL=nl_entity"], "reporting_currency": "CNY"}, "selector id"),
            ({"entities": ["ZZ=zz_entity", "NL=nl_entity"], "reporting_currency": "EUR"}, "unavailable"),
            ({"entities": ["CN=cn_entity", "NL=nl_entity"], "reporting_currency": "EU"}, "3-letter"),
            ({"entities": ["CN=cn_entity", "NL=nl_entity"], "reporting_currency": "EUR", "integrations": ["wise", "wise"]}, "duplicated"),
            ({"entities": ["CN=cn_entity", "NL=nl_entity"], "reporting_currency": "EUR", "integrations": ["shopify_stripe"]}, "entity-integration"),
            ({"entities": ["CN=cn_entity", "NL=nl_entity"], "reporting_currency": "EUR", "entity_integrations": ["missing=xero"]}, "selected entity_id"),
            ({"entities": ["CN=cn_entity", "NL=nl_entity"], "reporting_currency": "EUR", "entity_integrations": ["cn_entity=shopify", "nl_entity=shopify"]}, "exactly one entity"),
            ({"entities": ["CN=cn_entity", "NL=nl_entity"], "reporting_currency": "EUR", "entity_names": ["missing=Name"]}, "selected entity_id"),
            ({"entities": ["CN=cn_entity", "NL=nl_entity"], "reporting_currency": "EUR", "data_mode": "staging"}, "data_mode"),
            ({"entities": [f"NL=nl_entity_{index}" for index in range(21)]}, "at most 20"),
        )
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name).resolve()
            for index, (overrides, message) in enumerate(cases):
                destination = root / f"invalid-compose-{index}"
                args = {
                    "profile": "dtc",
                    "entities": ["NL=nl_one", "NL=nl_two"],
                    "packs_root": PACKS,
                    "destination_root": destination,
                    "actor": "multi-entity-negative-auditor",
                    **overrides,
                }
                with self.assertRaisesRegex(StarterWorkspaceError, message):
                    initialize_multi_entity_starter_workspace(**args)
                self.assertFalse(destination.exists())

    def test_multi_entity_composer_is_deterministic_except_for_receipt_actor(self):
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name).resolve()
            common = {
                "profile": "game",
                "entities": ["CN=cn_studio", "SG=sg_publisher"],
                "packs_root": PACKS,
                "integrations": ["xero"],
                "reporting_currency": "CNY",
            }
            first = initialize_multi_entity_starter_workspace(
                **common,
                destination_root=root / "first",
                actor="first-multi-entity-recipient",
            )
            second = initialize_multi_entity_starter_workspace(
                **common,
                destination_root=root / "second",
                actor="second-multi-entity-recipient",
            )
            self.assertEqual(first["initialized_spec_sha256"], second["initialized_spec_sha256"])
            self.assertEqual(first["runtime_fingerprint"], second["runtime_fingerprint"])
            self.assertNotEqual(first["workspace_receipt_sha256"], second["workspace_receipt_sha256"])


if __name__ == "__main__":
    unittest.main()
