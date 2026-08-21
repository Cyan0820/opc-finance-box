from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from src.ae_tax_services import AE_EVIDENCE_BY_RULE
from src.box_runtime import BoxRuntime
from src.default_services import build_default_service_registry
from src.pack_services import PackServiceError


ROOT = Path(__file__).resolve().parents[1]
PACKS = ROOT / "packs"
AE_BOX = ROOT / "examples" / "boxes" / "ae_dtc_shopify_stripe_free_zone_company.json"


def _evidence(reference: str) -> dict[str, str]:
    return {"source_reference": reference, "captured_at": "2026-08-16"}


class UaeTaxPackTests(unittest.TestCase):
    def setUp(self):
        self.runtime = BoxRuntime(AE_BOX, PACKS)
        self.services = build_default_service_registry()
        self.source_max_age_days = self.runtime.tax_rules("ae_store")["rules"][
            "review_policy"
        ]["max_age_days"]

    def _dispatch(self, service_id, payload):
        return self.services.dispatch(
            self.runtime, service_id, payload, entity_id="ae_store",
        )["output"]

    def _complete_profile(self, **overrides):
        payload = {
            "as_of": "2026-08-16",
            "entity_type": "free_zone_juridical_person",
            "entity_type_evidence": _evidence("licensing authority legal form"),
            "tax_residency_evidence": _evidence("UAE residency advisor review"),
            "trade_licence_status": "confirmed",
            "trade_licence_evidence": _evidence("current trade licence"),
            "corporate_tax_status": "confirmed",
            "corporate_tax_evidence": _evidence("corporate tax registration"),
            "free_zone_status": "confirmed",
            "free_zone_evidence": _evidence("free zone person review"),
            "vat_status": "not_registered",
        }
        payload.update(overrides)
        return payload

    def test_registration_requires_evidence_and_rejects_raw_identifiers(self):
        missing = self._dispatch(
            "tax.ae_domestic_juridical_person.registration_profile",
            {"as_of": "2026-08-16"},
        )
        self.assertFalse(missing["ready"])
        self.assertEqual(
            missing["applicability"], "in_scope_uae_domestic_juridical_person",
        )
        for field in (
            "trade_licence_number", "trade_license_number", "company_number",
            "corporate_tax_trn", "vat_trn", "tax_registration_number", "tax_id",
            "emaratax_user_id", "electronic_certificate", "emirates_id",
            "passport_number",
        ):
            with self.subTest(field=field), self.assertRaisesRegex(
                ValueError, "raw UAE licence",
            ):
                self._dispatch(
                    "tax.ae_domestic_juridical_person.registration_profile",
                    {field: "sensitive"},
                )

    def test_mainland_and_free_zone_profiles_never_make_tax_conclusions(self):
        for entity_type in (
            "mainland_limited_liability_company",
            "mainland_single_person_limited_liability_company",
            "free_zone_juridical_person",
        ):
            overrides = {"entity_type": entity_type}
            if entity_type != "free_zone_juridical_person":
                overrides.update({
                    "free_zone_status": "not_applicable",
                    "free_zone_evidence": None,
                })
            with self.subTest(entity_type=entity_type):
                output = self._dispatch(
                    "tax.ae_domestic_juridical_person.registration_profile",
                    self._complete_profile(**overrides),
                )
                self.assertTrue(output["ready"], output)
                self.assertEqual(output["tax_readiness"], "design")
                self.assertFalse(output["raw_licence_identity_or_tax_identifier_collected"])
                for field in (
                    "uae_tax_residency_determined",
                    "effective_management_and_control_determined",
                    "entity_type_determined_by_system",
                    "corporate_tax_registration_or_liability_determined",
                    "corporate_tax_rate_or_amount_determined",
                    "qualifying_free_zone_person_status_determined",
                    "qualifying_or_excluded_income_determined",
                    "free_zone_substance_or_de_minimis_determined",
                    "small_business_relief_or_exempt_status_determined",
                    "tax_group_eligibility_determined",
                    "accounting_standard_method_or_audit_requirement_determined",
                    "vat_registration_liability_determined",
                    "vat_supply_classification_performed",
                    "vat_rate_input_recovery_or_amount_determined",
                    "cross_border_or_permanent_establishment_determined",
                    "transfer_pricing_or_customs_determined",
                    "withholding_tax_treatment_determined",
                    "payroll_or_social_insurance_determined",
                    "tax_calculation_performed", "filing_performed",
                    "payment_performed", "external_submission_enabled",
                ):
                    self.assertFalse(output[field], field)

    def test_free_zone_and_vat_claims_need_evidence(self):
        output = self._dispatch(
            "tax.ae_domestic_juridical_person.registration_profile",
            self._complete_profile(free_zone_evidence=None, vat_status="confirmed"),
        )
        self.assertFalse(output["ready"])
        self.assertEqual(
            output["registrations"]["free_zone"]["review_status"], "needs_evidence",
        )
        self.assertEqual(output["registrations"]["vat"]["review_status"], "needs_evidence")
        confirmed = self._dispatch(
            "tax.ae_domestic_juridical_person.registration_profile",
            self._complete_profile(
                vat_status="confirmed", vat_evidence=_evidence("VAT registration"),
            ),
        )
        self.assertTrue(confirmed["ready"], confirmed)
        self.assertFalse(confirmed["qualifying_free_zone_person_status_determined"])
        self.assertFalse(confirmed["vat_registration_liability_determined"])

    def test_outside_scope_and_source_expiry_fail_closed(self):
        for entity_type in (
            "public_joint_stock_company", "private_joint_stock_company",
            "partnership", "foreign_juridical_person", "branch", "natural_person",
            "sole_establishment", "nonprofit",
        ):
            with self.subTest(entity_type=entity_type):
                outside = self._dispatch(
                    "tax.ae_domestic_juridical_person.registration_profile",
                    self._complete_profile(entity_type=entity_type),
                )
                self.assertFalse(outside["ready"])
                self.assertEqual(outside["applicability"], "outside_pack_scope")
        boundary = self._complete_profile(
            as_of=(date(2026, 8, 16) + timedelta(days=self.source_max_age_days)).isoformat(),
        )
        self.assertTrue(self._dispatch(
            "tax.ae_domestic_juridical_person.registration_profile", boundary,
        )["ready"])
        expired = self._complete_profile(
            as_of=(date(2026, 8, 16) + timedelta(days=self.source_max_age_days + 1)).isoformat(),
        )
        result = self._dispatch(
            "tax.ae_domestic_juridical_person.registration_profile", expired,
        )
        self.assertFalse(result["ready"])
        self.assertEqual(result["source_freshness"]["status"], "source_review_expired")

    def test_evidence_checklist_rejects_missing_duplicate_and_unknown(self):
        missing = self._dispatch(
            "tax.ae_domestic_juridical_person.evidence_checklist",
            {"as_of": "2026-08-16", "provided_evidence": []},
        )
        self.assertIn("required evidence is missing", missing["blockers"])
        ids = sorted({item for values in AE_EVIDENCE_BY_RULE.values() for item in values})
        provided = [
            {"evidence_id": item, **_evidence(f"evidence:{item}")} for item in ids
        ]
        complete = self._dispatch(
            "tax.ae_domestic_juridical_person.evidence_checklist",
            {"as_of": "2026-08-16", "provided_evidence": provided},
        )
        self.assertTrue(complete["ready"], complete)
        duplicate = self._dispatch(
            "tax.ae_domestic_juridical_person.evidence_checklist",
            {"as_of": "2026-08-16", "provided_evidence": [*provided, dict(provided[0])]},
        )
        self.assertEqual(duplicate["duplicate_evidence_ids"], [provided[0]["evidence_id"]])
        unknown = self._dispatch(
            "tax.ae_domestic_juridical_person.evidence_checklist",
            {"as_of": "2026-08-16", "provided_evidence": [
                *provided, {"evidence_id": "invented", **_evidence("x")},
            ]},
        )
        self.assertEqual(unknown["unknown_evidence_ids"], ["invented"])

    def test_calendar_keeps_corporate_tax_and_vat_dates_manual(self):
        output = self._dispatch(
            "tax.ae_domestic_juridical_person.build_calendar",
            {"period_year": 2026, "as_of": "2026-08-16"},
        )
        self.assertFalse(output["ready"])
        self.assertTrue(output["domestic_juridical_person_scope_confirmed"])
        self.assertEqual(output["task_count"], 2)
        by_rule = {task["rule_id"]: task for task in output["tasks"]}
        self.assertEqual(set(by_rule), {
            "ae.corporate_tax.return_payment.calendar",
            "ae.vat.return_payment.calendar",
        })
        self.assertTrue(all(
            task["candidate_due_date"] is None
            and task["candidate_only"]
            and not task["filing_completed"]
            for task in output["tasks"]
        ))
        self.assertEqual(
            by_rule["ae.corporate_tax.return_payment.calendar"]["status"],
            "needs_configuration",
        )
        self.assertEqual(
            by_rule["ae.vat.return_payment.calendar"]["status"],
            "needs_registration_confirmation",
        )
        self.assertFalse(output["qualifying_free_zone_person_status_determined"])
        self.assertFalse(output["tax_calculation_performed"])
        self.assertFalse(output["filing_performed"])

    def test_wrong_entity_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = json.loads(AE_BOX.read_text(encoding="utf-8"))
            config["connector_bindings"] = [{
                "connector_pack": pack_id,
                "entity_ids": [config["entities"][0]["id"]],
            } for pack_id in config["connectors"]]
            config["features"].append("feature.multi_entity")
            config["entities"].append({
                "id": "cn_entity", "name": "CN Entity", "jurisdiction": "CN",
                "functional_currency": "CNY", "accounting_basis": "PRC_GAAP",
                "fiscal_year_end": "12-31", "tax_pack": "jurisdiction.cn_mainland",
                "tax_registrations": ["corporate_income_tax"],
            })
            path = Path(temp_dir) / "box.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            runtime = BoxRuntime(path, PACKS)
            with self.assertRaisesRegex(
                PackServiceError, "uses jurisdiction.cn_mainland",
            ):
                self.services.dispatch(
                    runtime,
                    "tax.ae_domestic_juridical_person.registration_profile",
                    {}, entity_id="cn_entity",
                )


if __name__ == "__main__":
    unittest.main()
