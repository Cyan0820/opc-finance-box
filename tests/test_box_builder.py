import unittest
import io
import json
import zipfile
import hashlib
import os
import stat
import tempfile
from pathlib import Path

from src.box_builder import (
    build_box_candidate_bundle, build_box_starter_catalog,
    list_box_builder_options, preview_box_candidate, write_box_candidate_bundle,
)
from src.box_config import load_pack_catalog
from src.handoff_verify import BoxHandoffVerifyError, verify_box_candidate_bundle


ROOT = Path(__file__).resolve().parents[1]
PACKS = ROOT / "packs"


class BoxBuilderTests(unittest.TestCase):
    def test_options_are_installed_pack_driven_and_expose_tax_maturity(self):
        options = list_box_builder_options(load_pack_catalog(PACKS))
        self.assertEqual(options["schema_version"], 3)
        self.assertEqual(
            {profile["id"] for profile in options["profiles"]},
            {"game", "dtc", "marketplace"},
        )
        jurisdictions = {item["country_code"]: item for item in options["jurisdictions"]}
        self.assertEqual(
            set(jurisdictions), {"AE", "AU", "CA", "CN", "DE", "FR", "SG", "US", "HK", "GB", "IE", "JP", "KR", "NL", "NZ"},
        )
        self.assertEqual(jurisdictions["CN"]["tax_readiness"], "workpaper")
        self.assertEqual(jurisdictions["US"]["tax_readiness"], "design")
        self.assertEqual(jurisdictions["US"]["rules_verified_at"], "2026-08-13")
        self.assertEqual(jurisdictions["US"]["review_policy"]["max_age_days"], 180)
        self.assertEqual(
            jurisdictions["US"]["applicability_review_policy"]["max_age_days"], 365,
        )
        self.assertEqual(jurisdictions["SG"]["starter"]["tax_registrations"], [])
        self.assertEqual(jurisdictions["GB"]["starter"]["functional_currency"], "GBP")
        self.assertEqual(jurisdictions["AU"]["starter"]["functional_currency"], "AUD")
        self.assertEqual(jurisdictions["AU"]["starter"]["accounting_basis"], "AASB")
        self.assertEqual(jurisdictions["CA"]["starter"]["functional_currency"], "CAD")
        self.assertEqual(jurisdictions["CA"]["starter"]["accounting_basis"], "ASPE")
        self.assertEqual(jurisdictions["NZ"]["starter"]["functional_currency"], "NZD")
        self.assertEqual(jurisdictions["NZ"]["starter"]["accounting_basis"], "NZ_GAAP")
        self.assertEqual(jurisdictions["NZ"]["starter"]["fiscal_year_end"], "03-31")
        self.assertEqual(jurisdictions["IE"]["starter"]["functional_currency"], "EUR")
        self.assertEqual(jurisdictions["NL"]["starter"]["functional_currency"], "EUR")
        self.assertEqual(jurisdictions["NL"]["starter"]["accounting_basis"], "Dutch_GAAP")
        self.assertEqual(jurisdictions["DE"]["starter"]["functional_currency"], "EUR")
        self.assertEqual(jurisdictions["DE"]["starter"]["accounting_basis"], "German_GAAP")
        self.assertEqual(jurisdictions["FR"]["starter"]["functional_currency"], "EUR")
        self.assertEqual(jurisdictions["FR"]["starter"]["accounting_basis"], "French_GAAP")
        self.assertEqual(jurisdictions["JP"]["starter"]["functional_currency"], "JPY")
        self.assertEqual(jurisdictions["JP"]["starter"]["accounting_basis"], "JGAAP")
        self.assertEqual(jurisdictions["KR"]["starter"]["functional_currency"], "KRW")
        self.assertEqual(jurisdictions["KR"]["starter"]["accounting_basis"], "K_GAAP")
        self.assertEqual(jurisdictions["AE"]["starter"]["functional_currency"], "AED")
        self.assertEqual(jurisdictions["AE"]["starter"]["accounting_basis"], "IFRS")
        self.assertTrue(options["control_boundary"]["tax_country_never_implies_filing_readiness"])
        profiles = {item["id"]: item for item in options["profiles"]}
        self.assertIn("xero", profiles["game"]["allowed_integrations"])
        self.assertIn("xero", profiles["dtc"]["allowed_integrations"])
        self.assertIn("xero", profiles["marketplace"]["allowed_integrations"])
        self.assertIn("amazon_seller", profiles["marketplace"]["allowed_integrations"])
        self.assertNotIn("amazon_seller", profiles["dtc"]["allowed_integrations"])
        self.assertEqual(
            set(profiles["dtc"]["allowed_integrations"]),
            {
                item["id"] for item in options["integration_presets"]
                if item["id"] != "amazon_seller"
            },
        )
        dtc_starters = [
            item for item in options["starter_catalog"]["entries"]
            if item["profile_id"] == "dtc"
        ]
        self.assertTrue(all(
            "shopify_stripe_wise_airwallex" in item["allowed_integrations"]
            for item in dtc_starters
        ))
        starter_catalog = options["starter_catalog"]
        self.assertTrue(starter_catalog["complete"])
        self.assertEqual(starter_catalog["profile_count"], 3)
        self.assertEqual(starter_catalog["jurisdiction_count"], 15)
        self.assertEqual(starter_catalog["ready_combination_count"], 45)
        self.assertEqual(starter_catalog["unavailable_combinations"], [])
        self.assertTrue(options["control_boundary"]["starter_catalog_is_pack_driven"])
        binding_policy = options["connector_binding_policy"]
        self.assertEqual(
            binding_policy["default_connector_pack"], "connector.file_import",
        )
        self.assertEqual(
            binding_policy["single_credential_connector_packs"],
            ["connector.shopify", "connector.stripe"],
        )
        self.assertTrue(binding_policy["complete_bindings_required_when_explicit"])
        self.assertTrue(
            binding_policy["wrong_entity_dispatch_rejected_before_provider_call"],
        )
        self.assertTrue(
            options["control_boundary"]
            ["browser_builder_can_emit_explicit_connector_bindings"],
        )
        download_policy = options["handoff_download_policy"]
        self.assertEqual(download_policy["schema_version"], 2)
        self.assertEqual(download_policy["digest_algorithm"], "SHA-256")
        self.assertEqual(
            download_policy["digest_header"], "X-OPC-Handoff-SHA256",
        )
        self.assertTrue(download_policy["client_digest_required_before_download"])
        self.assertTrue(
            download_policy["missing_or_mismatched_metadata_blocks_download"],
        )
        self.assertEqual(download_policy["receipt_schema_version"], 1)
        self.assertEqual(
            download_policy["receipt_filename_suffix"], ".browser-receipt.json",
        )
        self.assertEqual(
            download_policy["recipient_verifier_command"], "handoff-receipt-verify",
        )
        self.assertEqual(download_policy["recipient_private_file_mode"], "0600")
        self.assertFalse(download_policy["receipt_is_digital_signature"])
        self.assertTrue(
            options["control_boundary"]
            ["browser_bundle_bytes_must_be_verified_before_download"],
        )
        self.assertTrue(
            options["control_boundary"]
            ["portable_browser_receipt_must_be_formally_reverified"],
        )

    def test_multi_entity_browser_candidate_preserves_exact_connector_bindings(self):
        spec = {
            "name": "全球独立站浏览器候选",
            "business_type": "commerce",
            "channels": ["dtc"],
            "integrations": ["xero", "shopify_stripe", "wise"],
            "data_mode": "demo",
            "reporting_currency": "EUR",
            "entities": [
                {
                    "id": "cn_ops", "name": "中国运营主体（待确认）",
                    "tax_country": "CN", "tax_pack": "jurisdiction.cn_mainland",
                    "functional_currency": "CNY", "accounting_basis": "PRC_GAAP",
                    "fiscal_year_end": "12-31", "tax_registrations": [],
                },
                {
                    "id": "nl_sales", "name": "荷兰销售主体（待确认）",
                    "tax_country": "NL",
                    "tax_pack": "jurisdiction.nl_private_limited_company",
                    "functional_currency": "EUR", "accounting_basis": "Dutch_GAAP",
                    "fiscal_year_end": "12-31", "tax_registrations": [],
                },
                {
                    "id": "us_ops", "name": "美国运营主体（待确认）",
                    "tax_country": "US", "tax_pack": "jurisdiction.us_federal",
                    "functional_currency": "USD", "accounting_basis": "US_GAAP",
                    "fiscal_year_end": "12-31", "tax_registrations": [],
                },
            ],
            "connector_bindings": [
                {
                    "connector_pack": "connector.file_import",
                    "entity_ids": ["cn_ops", "nl_sales", "us_ops"],
                },
                {"connector_pack": "connector.shopify", "entity_ids": ["nl_sales"]},
                {"connector_pack": "connector.stripe", "entity_ids": ["nl_sales"]},
                {"connector_pack": "connector.wise", "entity_ids": ["us_ops"]},
                {"connector_pack": "connector.xero", "entity_ids": ["cn_ops"]},
            ],
        }
        preview = preview_box_candidate(spec, PACKS)
        self.assertEqual(
            preview["config"]["connector_bindings"], spec["connector_bindings"],
        )
        self.assertEqual(preview["spec"], spec)
        self.assertIn("feature.multi_entity", preview["config"]["features"])
        shopify_close = next(
            item for item in preview["candidate"]["pipelines"]
            if item["pipeline_id"] == "dtc.shopify_stripe_daily_close"
        )
        self.assertEqual(shopify_close["eligible_entity_ids"], ["nl_sales"])
        self.assertFalse(preview["control_boundary"]["connector_dispatch_performed"])

        unsafe = json.loads(json.dumps(spec))
        for binding in unsafe["connector_bindings"]:
            if binding["connector_pack"] == "connector.shopify":
                binding["entity_ids"] = ["nl_sales", "us_ops"]
        with self.assertRaisesRegex(
            ValueError, "connector.shopify to exactly one entity",
        ):
            preview_box_candidate(unsafe, PACKS)

    def test_every_product_country_starter_previews_and_compiles_with_safe_boundaries(self):
        catalog = build_box_starter_catalog(load_pack_catalog(PACKS))
        expected_pairs = {
            (profile, country)
            for profile in {"game", "dtc", "marketplace"}
            for country in {"AE", "AU", "CA", "CN", "DE", "FR", "GB", "HK", "IE", "JP", "KR", "NL", "NZ", "SG", "US"}
        }
        self.assertEqual(
            {(item["profile_id"], item["country_code"]) for item in catalog["entries"]},
            expected_pairs,
        )
        self.assertTrue(catalog["control_boundary"]["every_entry_contract_checked"])
        for starter in catalog["entries"]:
            with self.subTest(starter=starter["id"]):
                self.assertRegex(starter["starter_spec_sha256"], r"^[0-9a-f]{64}$")
                self.assertFalse(starter["filing_ready"])
                self.assertTrue(starter["requires_local_confirmation"])
                preview = preview_box_candidate(starter["starter_spec"], PACKS)
                entity = preview["config"]["entities"][0]
                self.assertEqual(entity["jurisdiction"], starter["country_code"])
                self.assertEqual(entity["tax_pack"], starter["jurisdiction_id"])
                self.assertEqual(entity["tax_registrations"], [])
                self.assertEqual(
                    preview["candidate"]["tax_readiness"][entity["id"]],
                    starter["tax_readiness"],
                )
                self.assertGreater(len(preview["candidate"]["pipelines"]), 0)
                self.assertFalse(preview["control_boundary"]["external_actions_performed"])
                self.assertFalse(preview["control_boundary"]["persistent_files_written"])

    def test_dtc_candidate_uses_explicit_country_pack_without_mutating_runtime(self):
        spec = {
            "name": "我的美国独立站候选",
            "business_type": "commerce",
            "channels": ["dtc"],
            "integrations": ["shopify_stripe"],
            "data_mode": "demo",
            "entities": [{
                "id": "us_store",
                "name": "美国经营主体候选",
                "tax_country": "US",
                "tax_pack": "jurisdiction.us_federal",
                "functional_currency": "USD",
                "accounting_basis": "US_GAAP",
                "fiscal_year_end": "12-31",
                "tax_registrations": [],
            }],
        }
        result = preview_box_candidate(spec, PACKS)
        repeated = preview_box_candidate(spec, PACKS)
        self.assertTrue(result["valid"])
        self.assertEqual(result["config"]["entities"][0]["tax_pack"], "jurisdiction.us_federal")
        self.assertEqual(result["candidate"]["product"]["workbench"]["profile"], "commerce_dtc")
        self.assertIn("dtc.shopify_stripe_daily_close", {
            item["pipeline_id"] for item in result["candidate"]["pipelines"]
        })
        self.assertEqual(result["candidate"]["tax_readiness"]["us_store"], "design")
        self.assertFalse(result["control_boundary"]["active_runtime_changed"])
        self.assertFalse(result["control_boundary"]["persistent_files_written"])
        self.assertFalse(result["control_boundary"]["external_actions_performed"])
        self.assertEqual(
            result["candidate"]["runtime_fingerprint"],
            repeated["candidate"]["runtime_fingerprint"],
        )
        checklist = result["candidate"]["setup_checklist"]
        self.assertEqual(checklist["counts"]["total"], len(result["candidate"]["setup_tasks"]))
        self.assertGreater(checklist["counts"]["blocking"], 0)
        self.assertFalse(checklist["completion_is_release_approval"])
        self.assertTrue(all(
            task["secret_values_included"] is False
            for group in checklist["groups"] for task in group["tasks"]
        ))
        connector_tasks = [
            task for group in checklist["groups"] for task in group["tasks"]
            if task["category"] == "connector_runtime"
        ]
        self.assertTrue(connector_tasks)
        self.assertTrue(all(task["credential_env"] for task in connector_tasks))

    def test_marketplace_candidate_cannot_silently_select_unsupported_country(self):
        with self.assertRaisesRegex(ValueError, "No installed tax pack"):
            preview_box_candidate({
                "name": "未支持国家候选",
                "business_type": "commerce",
                "channels": ["marketplace"],
                "entities": [{
                    "id": "store",
                    "name": "主体",
                    "tax_country": "ES",
                    "functional_currency": "EUR",
                    "accounting_basis": "IFRS",
                }],
            }, PACKS)

    def test_candidate_size_and_entity_count_are_bounded(self):
        with self.assertRaisesRegex(ValueError, "20 legal entities"):
            preview_box_candidate({
                "name": "too-many",
                "business_type": "commerce",
                "channels": ["dtc"],
                "entities": [{} for _ in range(21)],
            }, PACKS)

    def test_unknown_or_secret_fields_are_rejected_before_preview_or_bundle(self):
        base = {
            "name": "unsafe", "business_type": "commerce", "channels": ["dtc"],
            "entities": [{
                "id": "cn_store", "name": "主体", "tax_country": "CN",
                "functional_currency": "CNY", "accounting_basis": "PRC_GAAP",
            }],
        }
        with self.assertRaisesRegex(ValueError, "unsupported fields"):
            preview_box_candidate({**base, "api_token": "must-not-enter"}, PACKS)
        unsafe_entity = json.loads(json.dumps(base))
        unsafe_entity["entities"][0]["client_secret"] = "must-not-enter"
        with self.assertRaisesRegex(ValueError, "unsupported fields"):
            build_box_candidate_bundle(unsafe_entity, PACKS)

    def test_bundle_is_deterministic_and_manifest_verifies_every_handoff_file(self):
        spec = json.loads((ROOT / "examples" / "box_specs" / "dtc_cn.json").read_text())
        first, filename, manifest = build_box_candidate_bundle(spec, PACKS)
        second, repeated_filename, repeated_manifest = build_box_candidate_bundle(spec, PACKS)
        self.assertEqual(first, second)
        self.assertEqual(filename, repeated_filename)
        self.assertEqual(manifest, repeated_manifest)
        self.assertRegex(filename, r"^opc-finance-box-[0-9a-f]{12}\.zip$")
        with zipfile.ZipFile(io.BytesIO(first)) as archive:
            names = set(archive.namelist())
            self.assertIn("box-spec.json", names)
            self.assertIn("box.json", names)
            self.assertIn("HANDOFF.md", names)
            self.assertIn("ACTIVATION.md", names)
            self.assertIn("setup-checklist.json", names)
            self.assertIn("bundle-manifest.json", names)
            self.assertIn("compiled/pipeline-catalog.json", names)
            self.assertIn("compiled/release-gates.json", names)
            self.assertIn("compiled/deployment-environment-contract.json", names)
            self.assertIn("compiled/runtime-data-contract.json", names)
            self.assertIn("compiled/connector-sync-policy.json", names)
            self.assertIn("compiled/stable-promotion-policy.json", names)
            self.assertIn("compiled/stable-promotion-evidence-templates.json", names)
            self.assertIn("compiled/stable-promotion-evidence.schema.json", names)
            self.assertIn(
                "compiled/pilot-shadow-observation-artifact.schema.json", names,
            )
            self.assertIn(
                "compiled/pilot-shadow-series-artifact.schema.json", names,
            )
            self.assertIn("compiled/production-readiness-plan.json", names)
            self.assertIn("deployment/Dockerfile", names)
            self.assertIn("deployment/Dockerfile.dockerignore", names)
            self.assertIn("deployment/compose.example.yaml", names)
            embedded = json.loads(archive.read("bundle-manifest.json"))
            self.assertEqual(embedded["schema_version"], 2)
            self.assertFalse(embedded["secret_values_included"])
            self.assertTrue(embedded["deployment_assets_included"])
            self.assertTrue(embedded["activation_guide_included"])
            handoff = archive.read("HANDOFF.md").decode("utf-8")
            self.assertIn("deployment-assets-verify", handoff)
            self.assertIn("deployment-smoke", handoff)
            self.assertIn("handoff-unpack", handoff)
            self.assertIn("handoff-unpack-verify", handoff)
            self.assertIn("ACTIVATION.md", handoff)
            activation = archive.read("ACTIVATION.md").decode("utf-8")
            self.assertIn("activation-init", activation)
            self.assertIn("activation-runbook-status", activation)
            self.assertIn("activation-runbook-record", activation)
            self.assertIn("永不替代", activation)
            for record in embedded["files"]:
                body = archive.read(record["path"])
                self.assertEqual(len(body), record["size_bytes"])
                self.assertEqual(hashlib.sha256(body).hexdigest(), record["sha256"])

    def test_bundle_writer_is_exclusive_private_and_returns_reproducible_digest(self):
        spec = json.loads((ROOT / "examples" / "box_specs" / "dtc_cn.json").read_text())
        expected, _, _ = build_box_candidate_bundle(spec, PACKS)
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir).resolve() / "first-customer.zip"
            result = write_box_candidate_bundle(spec, PACKS, output)
            self.assertTrue(result["written"])
            self.assertEqual(result["sha256"], hashlib.sha256(expected).hexdigest())
            self.assertEqual(output.read_bytes(), expected)
            self.assertTrue(result["activation_guide_included"])
            self.assertFalse(result["secret_values_included"])
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
            with self.assertRaisesRegex(ValueError, "refusing to overwrite"):
                write_box_candidate_bundle(spec, PACKS, output)
            with self.assertRaisesRegex(ValueError, "zip suffix"):
                write_box_candidate_bundle(spec, PACKS, output.with_suffix(".json"))

    def test_handoff_verifier_binds_private_archive_to_installed_pack_catalog(self):
        spec = json.loads((ROOT / "examples" / "box_specs" / "dtc_cn.json").read_text())
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir).resolve() / "customer-handoff.zip"
            written = write_box_candidate_bundle(spec, PACKS, output)
            verified = verify_box_candidate_bundle(output, PACKS)
            self.assertTrue(verified["valid"])
            self.assertEqual(verified["bundle_sha256"], written["sha256"])
            self.assertEqual(verified["runtime_fingerprint"], written["runtime_fingerprint"])
            self.assertEqual(verified["member_count"], 55)
            self.assertEqual(verified["manifest_file_count"], 54)
            self.assertTrue(verified["reproducible_with_installed_packs"])
            self.assertTrue(verified["archive_bytes_match_current_builder"])
            self.assertFalse(verified["archive_extracted"])
            self.assertFalse(verified["paths_returned"])
            self.assertFalse(verified["secret_values_included"])
            self.assertFalse(verified["financial_values_returned"])
            self.assertFalse(verified["external_actions_performed"])
            if os.name != "nt":
                output.chmod(0o644)
                with self.assertRaisesRegex(BoxHandoffVerifyError, "owner-private"):
                    verify_box_candidate_bundle(output, PACKS)
                output.chmod(0o600)
                symlink = output.with_name("handoff-link.zip")
                os.symlink(output, symlink)
                with self.assertRaisesRegex(BoxHandoffVerifyError, "regular file"):
                    verify_box_candidate_bundle(symlink, PACKS)

    def test_handoff_verifier_rejects_hash_path_and_self_consistent_content_tampering(self):
        spec = json.loads((ROOT / "examples" / "box_specs" / "dtc_cn.json").read_text())
        body, _, _ = build_box_candidate_bundle(spec, PACKS)

        def rewrite(files: dict[str, bytes]) -> bytes:
            stream = io.BytesIO()
            with zipfile.ZipFile(
                stream, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9,
            ) as archive:
                for name, content in sorted(files.items()):
                    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                    info.create_system = 3
                    info.compress_type = zipfile.ZIP_DEFLATED
                    info.external_attr = 0o100644 << 16
                    archive.writestr(info, content)
            return stream.getvalue()

        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            files = {name: archive.read(name) for name in archive.namelist()}
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            hash_tampered = root / "hash-tampered.zip"
            hash_files = dict(files)
            hash_files["HANDOFF.md"] += b"\nchanged\n"
            hash_tampered.write_bytes(rewrite(hash_files))
            hash_tampered.chmod(0o600)
            with self.assertRaisesRegex(BoxHandoffVerifyError, "does not match"):
                verify_box_candidate_bundle(hash_tampered, PACKS)

            unsafe = root / "unsafe.zip"
            unsafe_files = dict(files)
            unsafe_files["../escape"] = b"bad"
            unsafe.write_bytes(rewrite(unsafe_files))
            unsafe.chmod(0o600)
            with self.assertRaisesRegex(BoxHandoffVerifyError, "unsafe member path"):
                verify_box_candidate_bundle(unsafe, PACKS)

            consistent = root / "self-consistent-tamper.zip"
            consistent_files = dict(files)
            consistent_files["HANDOFF.md"] += b"\nchanged but rehashed\n"
            manifest = json.loads(consistent_files["bundle-manifest.json"])
            record = next(item for item in manifest["files"] if item["path"] == "HANDOFF.md")
            record["size_bytes"] = len(consistent_files["HANDOFF.md"])
            record["sha256"] = hashlib.sha256(consistent_files["HANDOFF.md"]).hexdigest()
            consistent_files["bundle-manifest.json"] = (
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
            ).encode("utf-8")
            consistent.write_bytes(rewrite(consistent_files))
            consistent.chmod(0o600)
            with self.assertRaisesRegex(BoxHandoffVerifyError, "does not reproduce"):
                verify_box_candidate_bundle(consistent, PACKS)

    def test_multi_entity_candidate_keeps_each_tax_pack_and_adds_isolation_feature(self):
        result = preview_box_candidate({
            "name": "跨境独立站候选",
            "business_type": "commerce",
            "channels": ["dtc"],
            "data_mode": "demo",
            "reporting_currency": "CNY",
            "entities": [
                {
                    "id": "cn_store", "name": "中国主体", "tax_country": "CN",
                    "tax_pack": "jurisdiction.cn_mainland",
                    "functional_currency": "CNY", "accounting_basis": "PRC_GAAP",
                },
                {
                    "id": "sg_store", "name": "新加坡主体", "tax_country": "SG",
                    "tax_pack": "jurisdiction.sg",
                    "functional_currency": "SGD", "accounting_basis": "SFRS",
                },
            ],
        }, PACKS)
        self.assertEqual(
            {entity["tax_pack"] for entity in result["config"]["entities"]},
            {"jurisdiction.cn_mainland", "jurisdiction.sg"},
        )
        self.assertIn("feature.multi_entity", result["config"]["features"])
        self.assertEqual(result["config"]["reporting_currency"], "CNY")
        channel_templates = [
            item for item in result["candidate"]["pipelines"]
            if item["pipeline_id"] == "commerce.channel_close"
        ]
        self.assertEqual(len(channel_templates), 1)
        self.assertEqual(len(result["candidate"]["entities"]), 2)


if __name__ == "__main__":
    unittest.main()
