import json
import tempfile
import unittest
from pathlib import Path

from src.box_runtime import BoxRuntime
from src.default_services import build_default_service_registry
from src.tax_workspace import build_tax_workspace
from src.tax_applicability_artifacts import (
    build_tax_applicability_workpaper, import_tax_applicability_review,
    review_tax_applicability_workpaper,
    write_tax_applicability_registry_receipt,
)


ROOT = Path(__file__).resolve().parents[1]
PACKS = ROOT / "packs"


class TaxWorkspaceTests(unittest.TestCase):
    def workspace(self, config: str, **kwargs):
        return build_tax_workspace(
            BoxRuntime(ROOT / "examples" / "boxes" / config, PACKS),
            build_default_service_registry(),
            period_year=kwargs.get("period_year", 2026),
            as_of=kwargs.get("as_of", "2026-08-13"),
            anchors=kwargs.get("anchors"),
            applicability_review_dir=kwargs.get("applicability_review_dir"),
            applicability_registry_receipt=kwargs.get(
                "applicability_registry_receipt"
            ),
        )

    def test_game_workspace_keeps_cn_and_sg_entities_separate(self):
        result = self.workspace("global_game_studio.json")
        self.assertEqual(result["summary"]["entity_count"], 2)
        self.assertEqual(result["summary"]["jurisdiction_count"], 2)
        self.assertEqual(result["summary"]["calendar_task_count"], 5)
        self.assertEqual(result["summary"]["registration_evidence_required_count"], 2)
        self.assertEqual(result["summary"]["registration_configuration_gap_count"], 1)
        by_entity = {item["entity"]["entity_id"]: item for item in result["entities"]}
        self.assertEqual(by_entity["cn_studio"]["tax_pack"]["tax_readiness"], "workpaper")
        self.assertEqual(by_entity["sg_publisher"]["tax_pack"]["tax_readiness"], "design")
        self.assertEqual(by_entity["cn_studio"]["calendar"]["task_count"], 2)
        self.assertEqual(by_entity["sg_publisher"]["calendar"]["task_count"], 3)
        self.assertTrue(result["control_boundary"]["statutory_books_kept_separate"])
        self.assertEqual(result["schema_version"], 4)
        self.assertEqual(result["summary"]["applicability_review_attached_count"], 0)
        self.assertEqual(result["summary"]["calendar_release_ready_entity_count"], 0)
        self.assertTrue(all(
            item["rule_lifecycle"]["status"] == "current"
            and item["applicability_review_requirement"]["review"]["status"]
            == "not_attached"
            and not item["calendar_release_ready"]
            for item in result["entities"]
        ))

    def test_workspace_reads_only_entity_named_reviews_and_returns_safe_summary(self):
        runtime = BoxRuntime(
            ROOT / "examples" / "boxes" / "global_game_studio.json", PACKS,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            review_dir = Path(temp_dir) / "reviews"
            review_dir.mkdir()
            workpaper = build_tax_applicability_workpaper(
                runtime, "cn_studio", prepared_by="cn-tax-operator",
                facts_as_of="2026-08-14",
            )
            answers = {
                "legal_form_and_pack_scope": "confirmed_in_scope",
                "tax_residency_and_permanent_establishment": "confirmed_in_scope",
                "direct_and_indirect_tax_registrations": "confirmed_complete",
                "fiscal_year_and_return_periods": "confirmed",
                "special_cross_border_and_group_regimes": "reviewed_no_additional_scope",
            }
            for question in workpaper["entity"]["questions"]:
                question["answer"] = answers[question["question_id"]]
                question["evidence_references"] = [
                    f"evidence://cn-private/{question['question_id']}"
                ]
            workpaper_path = Path(temp_dir) / "cn-workpaper.json"
            workpaper_path.write_text(json.dumps(workpaper), encoding="utf-8")
            workpaper_path.chmod(0o600)
            review_tax_applicability_workpaper(
                runtime, workpaper_path, review_dir / "cn_studio.json",
                decision="approved-in-scope", actor="cn-local-tax-reviewer",
                rationale="PRIVATE-RATIONALE-MUST-NOT-LEAK",
                evidence_references=["advisor://cn-private/memo"],
            )
            result = build_tax_workspace(
                runtime, build_default_service_registry(), period_year=2026,
                as_of="2026-08-14", applicability_review_dir=review_dir,
            )
        by_entity = {item["entity"]["entity_id"]: item for item in result["entities"]}
        cn_review = by_entity["cn_studio"]["applicability_review_requirement"]["review"]
        self.assertEqual(cn_review["status"], "current")
        self.assertTrue(cn_review["applicability_gate_passed"])
        self.assertFalse(by_entity["cn_studio"]["calendar_release_ready"])
        self.assertEqual(
            by_entity["sg_publisher"]["applicability_review_requirement"]["review"]["status"],
            "not_attached",
        )
        self.assertNotIn("PRIVATE-RATIONALE-MUST-NOT-LEAK", json.dumps(result))
        self.assertNotIn("confirmed_in_scope", json.dumps(result))
        self.assertFalse(result["control_boundary"]["private_applicability_answers_returned"])
        self.assertFalse(
            result["applicability_review_registry"]["activation_receipt_configured"]
        )

    def test_workspace_requires_matching_registry_receipt_for_runtime_release(self):
        runtime = BoxRuntime(
            ROOT / "examples" / "boxes" / "global_game_studio.json", PACKS,
        )
        answers = {
            "legal_form_and_pack_scope": "confirmed_in_scope",
            "tax_residency_and_permanent_establishment": "confirmed_in_scope",
            "direct_and_indirect_tax_registrations": "confirmed_complete",
            "fiscal_year_and_return_periods": "confirmed",
            "special_cross_border_and_group_regimes": "reviewed_no_additional_scope",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            review_dir = root / "reviews"
            review_dir.mkdir()
            for entity_id in ("cn_studio", "sg_publisher"):
                workpaper = build_tax_applicability_workpaper(
                    runtime, entity_id, prepared_by=f"{entity_id}-preparer",
                    facts_as_of="2026-08-14",
                )
                for question in workpaper["entity"]["questions"]:
                    question["answer"] = answers[question["question_id"]]
                    question["evidence_references"] = [
                        f"evidence://workspace/{entity_id}/{question['question_id']}"
                    ]
                workpaper_path = root / f"{entity_id}-workpaper.json"
                source_review = root / f"{entity_id}-source.json"
                workpaper_path.write_text(json.dumps(workpaper), encoding="utf-8")
                workpaper_path.chmod(0o600)
                review_tax_applicability_workpaper(
                    runtime, workpaper_path, source_review,
                    decision="approved-in-scope", actor=f"{entity_id}-reviewer",
                    rationale="PRIVATE-WORKSPACE-RECEIPT-RATIONALE",
                    evidence_references=[f"advisor://workspace/{entity_id}"],
                )
                import_tax_applicability_review(
                    runtime, source_review, review_dir, as_of="2026-08-14",
                )
            receipt = root / "registry-receipt.json"
            write_tax_applicability_registry_receipt(
                runtime, review_dir, receipt,
                actor="registry-controller", as_of="2026-08-14",
            )
            result = build_tax_workspace(
                runtime, build_default_service_registry(), period_year=2026,
                as_of="2026-08-14", applicability_review_dir=review_dir,
                applicability_registry_receipt=receipt,
            )
        registry = result["applicability_review_registry"]
        self.assertTrue(registry["activation_receipt_configured"])
        self.assertTrue(registry["activation_receipt_valid"])
        self.assertTrue(registry["ready_for_calendar_release"])
        self.assertTrue(registry["activation"]["registry_unchanged"])
        self.assertFalse(registry["activation"]["digital_signature_verified"])
        self.assertFalse(registry["activation"]["filing_authorization_granted"])
        self.assertEqual(
            result["summary"]["calendar_release_ready_entity_count"], 2
        )
        self.assertNotIn("PRIVATE-WORKSPACE", json.dumps(result))

    def test_workspace_rejects_relative_or_missing_review_directories(self):
        runtime = BoxRuntime(
            ROOT / "examples" / "boxes" / "global_game_studio.json", PACKS,
        )
        services = build_default_service_registry()
        with self.assertRaisesRegex(ValueError, "absolute path"):
            build_tax_workspace(
                runtime, services, as_of="2026-08-14",
                applicability_review_dir="relative/reviews",
            )
        with self.assertRaisesRegex(ValueError, "requires a configured review directory"):
            build_tax_workspace(
                runtime, services, as_of="2026-08-14",
                applicability_registry_receipt="/private/receipt.json",
            )
        with tempfile.TemporaryDirectory() as temp_dir:
            review_dir = Path(temp_dir) / "reviews"
            review_dir.mkdir()
            missing_receipt = Path(temp_dir) / "PRIVATE-MISSING-RECEIPT.json"
            result = build_tax_workspace(
                runtime, services, as_of="2026-08-14",
                applicability_review_dir=review_dir,
                applicability_registry_receipt=missing_receipt,
            )
        activation = result["applicability_review_registry"]["activation"]
        self.assertEqual(activation["status"], "invalid")
        self.assertRegex(activation["error_sha256"], r"^[0-9a-f]{64}$")
        self.assertNotIn("PRIVATE-MISSING", json.dumps(result))
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(ValueError, "real directory"):
                build_tax_workspace(
                    runtime, services, as_of="2026-08-14",
                    applicability_review_dir=Path(temp_dir) / "missing",
                )

    def test_uk_workspace_exposes_services_sources_and_mixed_calendar_controls(self):
        result = self.workspace("uk_dtc_shopify_stripe_ltd.json")
        item = result["entities"][0]
        self.assertEqual(item["entity"]["jurisdiction"], "GB")
        self.assertEqual(item["tax_pack"]["pack_id"], "jurisdiction.uk_limited_company")
        self.assertEqual(item["tax_pack"]["tax_readiness"], "design")
        self.assertEqual(item["services"]["dispatch_endpoint"], "/api/box/services/dispatch")
        self.assertEqual(
            item["services"]["registration_profile"]["service_id"],
            "tax.uk_limited_company.registration_profile",
        )
        self.assertTrue(all(
            source["url"].startswith("https://www.gov.uk/")
            for source in item["official_sources"]
        ))
        by_rule = {task["rule_id"]: task for task in item["calendar"]["tasks"]}
        self.assertEqual(
            by_rule["uk.corporation_tax.ct600.calendar"]["candidate_due_date"],
            "2027-12-31",
        )
        self.assertIsNone(
            by_rule["uk.corporation_tax.payment.calendar"]["candidate_due_date"]
        )
        self.assertFalse(item["calendar"]["tax_calculation_performed"])
        self.assertFalse(item["calendar"]["filing_performed"])

    def test_australia_workspace_preserves_ato_and_asic_manual_gates(self):
        result = self.workspace("au_dtc_shopify_stripe_pty_ltd.json")
        item = result["entities"][0]
        self.assertEqual(item["entity"]["jurisdiction"], "AU")
        self.assertEqual(
            item["tax_pack"]["pack_id"], "jurisdiction.au_proprietary_company",
        )
        self.assertEqual(item["tax_pack"]["tax_readiness"], "design")
        self.assertEqual(
            item["services"]["registration_profile"]["service_id"],
            "tax.au_proprietary_company.registration_profile",
        )
        self.assertTrue(all(
            source["url"].startswith("https://") for source in item["official_sources"]
        ))
        by_rule = {task["rule_id"]: task for task in item["calendar"]["tasks"]}
        self.assertIsNone(
            by_rule["au.company_tax.return_and_payment.calendar"]["candidate_due_date"]
        )
        self.assertIsNone(by_rule["au.gst.bas.calendar"]["candidate_due_date"])
        self.assertEqual(
            by_rule["au.asic.annual_review.calendar"]["missing_configuration"],
            ["asic_annual_review_date"],
        )
        self.assertFalse(item["calendar"]["tax_calculation_performed"])
        self.assertFalse(item["calendar"]["payment_performed"])

    def test_canada_workspace_calculates_only_t2_candidate(self):
        result = self.workspace("ca_dtc_shopify_stripe_federal_corporation.json")
        item = result["entities"][0]
        self.assertEqual(item["entity"]["jurisdiction"], "CA")
        self.assertEqual(
            item["tax_pack"]["pack_id"],
            "jurisdiction.ca_federal_corporation",
        )
        self.assertEqual(item["tax_pack"]["tax_readiness"], "design")
        self.assertEqual(
            item["services"]["registration_profile"]["service_id"],
            "tax.ca_federal_corporation.registration_profile",
        )
        by_rule = {
            task["rule_id"]: task for task in item["calendar"]["tasks"]
        }
        self.assertEqual(
            by_rule["ca.t2.return.calendar"]["candidate_due_date"],
            "2027-06-30",
        )
        self.assertIsNone(
            by_rule["ca.corporation_tax.balance.calendar"]["candidate_due_date"]
        )
        self.assertIsNone(
            by_rule["ca.corporations_canada.annual_return.calendar"]
            ["candidate_due_date"]
        )
        self.assertFalse(
            item["calendar"]["determinations"]["ccpc_status_determined"]
        )
        self.assertFalse(item["calendar"]["tax_calculation_performed"])
        self.assertFalse(item["calendar"]["filing_performed"])

    def test_new_zealand_workspace_keeps_all_dates_under_review(self):
        result = self.workspace("nz_dtc_shopify_stripe_limited_company.json")
        item = result["entities"][0]
        self.assertEqual(item["entity"]["jurisdiction"], "NZ")
        self.assertEqual(
            item["tax_pack"]["pack_id"], "jurisdiction.nz_limited_company",
        )
        self.assertEqual(item["tax_pack"]["tax_readiness"], "design")
        self.assertEqual(
            item["services"]["registration_profile"]["service_id"],
            "tax.nz_limited_company.registration_profile",
        )
        self.assertEqual(item["calendar"]["task_count"], 4)
        self.assertTrue(all(
            task["candidate_due_date"] is None
            for task in item["calendar"]["tasks"]
        ))
        self.assertFalse(
            item["calendar"]["determinations"]
            ["new_zealand_tax_residency_determined"]
        )
        self.assertFalse(item["calendar"]["tax_calculation_performed"])
        self.assertFalse(item["calendar"]["filing_performed"])

    def test_ireland_workspace_requires_cro_ard_and_keeps_tax_dates_manual(self):
        result = self.workspace("ie_dtc_shopify_stripe_ltd.json")
        item = result["entities"][0]
        self.assertEqual(item["entity"]["jurisdiction"], "IE")
        self.assertEqual(
            item["tax_pack"]["pack_id"],
            "jurisdiction.ie_private_limited_company",
        )
        self.assertEqual(
            item["services"]["registration_profile"]["service_id"],
            "tax.ie_private_limited_company.registration_profile",
        )
        self.assertEqual(item["calendar"]["task_count"], 4)
        by_rule = {task["rule_id"]: task for task in item["calendar"]["tasks"]}
        self.assertEqual(
            by_rule["ie.cro.annual_return.calendar"]["missing_configuration"],
            ["cro_annual_return_date"],
        )
        self.assertIsNone(by_rule["ie.ct1.return.calendar"]["candidate_due_date"])
        self.assertFalse(
            item["calendar"]["determinations"]["irish_tax_residency_determined"]
        )
        self.assertFalse(item["calendar"]["tax_calculation_performed"])

    def test_netherlands_workspace_keeps_vpb_vat_and_kvk_dates_manual(self):
        result = self.workspace("nl_dtc_shopify_stripe_bv.json")
        item = result["entities"][0]
        self.assertEqual(item["entity"]["jurisdiction"], "NL")
        self.assertEqual(
            item["tax_pack"]["pack_id"],
            "jurisdiction.nl_private_limited_company",
        )
        self.assertEqual(
            item["services"]["registration_profile"]["service_id"],
            "tax.nl_private_limited_company.registration_profile",
        )
        self.assertEqual(item["calendar"]["task_count"], 3)
        self.assertTrue(all(
            task["candidate_due_date"] is None
            for task in item["calendar"]["tasks"]
        ))
        self.assertEqual(item["anchor_contracts"], [])
        self.assertFalse(item["calendar"]["tax_calculation_performed"])
        self.assertFalse(item["calendar"]["filing_performed"])

    def test_germany_workspace_keeps_all_statutory_dates_manual(self):
        result = self.workspace(
            "de_dtc_shopify_stripe_gmbh.json", as_of="2026-08-14",
        )
        item = result["entities"][0]
        self.assertEqual(item["entity"]["jurisdiction"], "DE")
        self.assertEqual(
            item["tax_pack"]["pack_id"],
            "jurisdiction.de_limited_liability_company",
        )
        self.assertEqual(
            item["services"]["registration_profile"]["service_id"],
            "tax.de_limited_liability_company.registration_profile",
        )
        self.assertEqual(item["calendar"]["task_count"], 4)
        self.assertTrue(all(
            task["candidate_due_date"] is None
            for task in item["calendar"]["tasks"]
        ))
        self.assertEqual(item["anchor_contracts"], [])
        self.assertFalse(
            item["calendar"]["determinations"]
            ["municipal_trade_tax_rate_determined"]
        )
        self.assertFalse(item["calendar"]["tax_calculation_performed"])
        self.assertFalse(item["calendar"]["filing_performed"])

    def test_france_sasu_workspace_keeps_tax_regime_and_dates_under_review(self):
        result = self.workspace(
            "fr_dtc_shopify_stripe_sasu.json", as_of="2026-08-14",
        )
        item = result["entities"][0]
        self.assertEqual(item["entity"]["jurisdiction"], "FR")
        self.assertEqual(
            item["tax_pack"]["pack_id"],
            "jurisdiction.fr_single_member_simplified_joint_stock_company",
        )
        self.assertEqual(
            item["services"]["registration_profile"]["service_id"],
            "tax.fr_single_member_simplified_joint_stock_company.registration_profile",
        )
        self.assertEqual(item["calendar"]["task_count"], 4)
        self.assertTrue(all(
            task["candidate_due_date"] is None
            for task in item["calendar"]["tasks"]
        ))
        self.assertEqual(item["anchor_contracts"], [])
        self.assertFalse(
            item["calendar"]["determinations"]["profit_tax_regime_determined"]
        )
        self.assertFalse(item["calendar"]["tax_calculation_performed"])
        self.assertFalse(item["calendar"]["filing_performed"])

    def test_japan_workspace_keeps_national_local_consumption_and_withholding_manual(self):
        result = self.workspace(
            "jp_dtc_shopify_stripe_kk.json", as_of="2026-08-16",
        )
        item = result["entities"][0]
        self.assertEqual(item["entity"]["jurisdiction"], "JP")
        self.assertEqual(
            item["tax_pack"]["pack_id"],
            "jurisdiction.jp_domestic_corporation",
        )
        self.assertEqual(
            item["services"]["registration_profile"]["service_id"],
            "tax.jp_domestic_corporation.registration_profile",
        )
        self.assertEqual(item["calendar"]["task_count"], 4)
        self.assertTrue(all(
            task["candidate_due_date"] is None
            for task in item["calendar"]["tasks"]
        ))
        self.assertEqual(item["anchor_contracts"], [])
        self.assertFalse(
            item["calendar"]["determinations"]
            ["consumption_taxable_person_status_determined"]
        )
        self.assertFalse(item["calendar"]["tax_calculation_performed"])
        self.assertFalse(item["calendar"]["filing_performed"])

    def test_korea_workspace_keeps_corporate_local_vat_invoice_and_withholding_manual(self):
        result = self.workspace(
            "kr_dtc_shopify_stripe_jusik_hoesa.json", as_of="2026-08-16",
        )
        item = result["entities"][0]
        self.assertEqual(item["entity"]["jurisdiction"], "KR")
        self.assertEqual(
            item["tax_pack"]["pack_id"],
            "jurisdiction.kr_domestic_corporation",
        )
        self.assertEqual(
            item["services"]["registration_profile"]["service_id"],
            "tax.kr_domestic_corporation.registration_profile",
        )
        self.assertEqual(item["calendar"]["task_count"], 5)
        self.assertTrue(all(
            task["candidate_due_date"] is None
            for task in item["calendar"]["tasks"]
        ))
        self.assertEqual(item["anchor_contracts"], [])
        self.assertFalse(
            item["calendar"]["determinations"]["vat_taxpayer_status_determined"]
        )
        self.assertFalse(
            item["calendar"]["determinations"]
            ["etax_invoice_obligation_or_deadline_determined"]
        )
        self.assertFalse(item["calendar"]["tax_calculation_performed"])
        self.assertFalse(item["calendar"]["filing_performed"])

    def test_uae_workspace_keeps_corporate_tax_vat_and_free_zone_status_manual(self):
        result = self.workspace(
            "ae_dtc_shopify_stripe_free_zone_company.json", as_of="2026-08-16",
        )
        item = result["entities"][0]
        self.assertEqual(item["entity"]["jurisdiction"], "AE")
        self.assertEqual(
            item["tax_pack"]["pack_id"],
            "jurisdiction.ae_domestic_juridical_person",
        )
        self.assertEqual(
            item["services"]["registration_profile"]["service_id"],
            "tax.ae_domestic_juridical_person.registration_profile",
        )
        self.assertEqual(item["calendar"]["task_count"], 2)
        self.assertTrue(all(
            task["candidate_due_date"] is None
            for task in item["calendar"]["tasks"]
        ))
        self.assertEqual(item["anchor_contracts"], [])
        self.assertFalse(
            item["calendar"]["determinations"]
            ["qualifying_free_zone_person_status_determined"]
        )
        self.assertFalse(
            item["calendar"]["determinations"]["vat_registration_liability_determined"]
        )
        self.assertFalse(item["calendar"]["tax_calculation_performed"])
        self.assertFalse(item["calendar"]["filing_performed"])

    def test_ireland_anchor_preview_calculates_candidate_without_persisting(self):
        preview = self.workspace(
            "ie_dtc_shopify_stripe_ltd.json",
            anchors={"ie_store": {"cro_annual_return_date": "2026-09-30"}},
        )
        item = preview["entities"][0]
        contract = {
            entry["anchor"]: entry for entry in item["anchor_contracts"]
        }
        self.assertTrue(contract["cro_annual_return_date"]["editable"])
        self.assertEqual(item["provided_anchors"], {
            "cro_annual_return_date": "2026-09-30",
        })
        by_rule = {task["rule_id"]: task for task in item["calendar"]["tasks"]}
        self.assertEqual(
            by_rule["ie.cro.annual_return.calendar"]["candidate_due_date"],
            "2026-11-25",
        )
        self.assertEqual(preview["summary"]["preview_anchor_count"], 1)
        self.assertFalse(preview["anchor_preview"]["persistent_write_performed"])
        self.assertFalse(preview["anchor_preview"]["values_are_evidence_confirmation"])

        fresh = self.workspace("ie_dtc_shopify_stripe_ltd.json")
        fresh_task = next(
            task for task in fresh["entities"][0]["calendar"]["tasks"]
            if task["rule_id"] == "ie.cro.annual_return.calendar"
        )
        self.assertIsNone(fresh_task["candidate_due_date"])
        self.assertEqual(fresh["entities"][0]["provided_anchors"], {})

    def test_canada_anchor_preview_uses_sixty_day_rule(self):
        result = self.workspace(
            "ca_dtc_shopify_stripe_federal_corporation.json",
            anchors={
                "ca_store": {
                    "federal_corporation_anniversary_date": "2026-01-31",
                },
            },
        )
        task = next(
            task for task in result["entities"][0]["calendar"]["tasks"]
            if task["rule_id"] == "ca.corporations_canada.annual_return.calendar"
        )
        self.assertEqual(task["candidate_due_date"], "2026-04-01")
        contracts = {
            item["anchor"]: item
            for item in result["entities"][0]["anchor_contracts"]
        }
        self.assertFalse(contracts["financial_year_end"]["editable"])
        self.assertTrue(contracts["financial_year_end"]["implicit_from_entity"])

    def test_anchor_preview_rejects_unknown_implicit_and_invalid_values(self):
        cases = (
            ({"missing": {"cro_annual_return_date": "2026-09-30"}}, "unknown entity_id"),
            ({"ie_store": {"financial_year_end": "2026-12-31"}}, "not an editable"),
            ({"ie_store": {"unexpected_date": "2026-09-30"}}, "not an editable"),
            ({"ie_store": {"cro_annual_return_date": "30/09/2026"}}, "YYYY-MM-DD"),
            ({"ie_store": {"cro_annual_return_date": ["2026-09-30"]}}, "one YYYY-MM-DD"),
        )
        for anchors, message in cases:
            with self.subTest(anchors=anchors):
                with self.assertRaisesRegex(ValueError, message):
                    self.workspace(
                        "ie_dtc_shopify_stripe_ltd.json", anchors=anchors,
                    )

    def test_workspace_templates_are_secret_and_identifier_free(self):
        result = self.workspace("uk_dtc_shopify_stripe_ltd.json")
        serialized = json.dumps(result).lower()
        for forbidden in (
            "utr_number", "company_number\"", "vat_number\"", "api_key", "token\"",
        ):
            self.assertNotIn(forbidden, serialized)
        boundary = result["control_boundary"]
        self.assertFalse(boundary["raw_tax_identifiers_requested"])
        self.assertFalse(boundary["evidence_values_accepted"])
        self.assertFalse(boundary["external_submission_enabled"])
        self.assertFalse(boundary["registration_codes_are_evidence_confirmation"])
        self.assertFalse(boundary["anchor_values_persisted"])
        self.assertFalse(boundary["anchor_values_are_evidence_confirmation"])
        self.assertFalse(boundary["preview_changes_box_config"])

    def test_invalid_period_or_as_of_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "four-digit year"):
            self.workspace("cn_dtc_store.json", period_year=99)
        with self.assertRaisesRegex(ValueError, "YYYY-MM-DD"):
            self.workspace("cn_dtc_store.json", as_of="13/08/2026")


if __name__ == "__main__":
    unittest.main()
