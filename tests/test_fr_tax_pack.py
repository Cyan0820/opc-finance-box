from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from src.box_runtime import BoxRuntime
from src.default_services import build_default_service_registry
from src.fr_tax_services import FR_EVIDENCE_BY_RULE
from src.pack_services import PackServiceError


ROOT = Path(__file__).resolve().parents[1]
PACKS = ROOT / "packs"
FR_BOX = ROOT / "examples" / "boxes" / "fr_dtc_shopify_stripe_sasu.json"


def _evidence(reference: str) -> dict[str, str]:
    return {"source_reference": reference, "captured_at": "2026-08-14"}


class FranceTaxPackTests(unittest.TestCase):
    def setUp(self):
        self.runtime = BoxRuntime(FR_BOX, PACKS)
        self.services = build_default_service_registry()
        self.source_max_age_days = self.runtime.tax_rules("fr_store")["rules"][
            "review_policy"
        ]["max_age_days"]

    def _dispatch(self, service_id, payload):
        return self.services.dispatch(
            self.runtime, service_id, payload, entity_id="fr_store",
        )["output"]

    def _complete_profile(self, **overrides):
        payload = {
            "as_of": "2026-08-14",
            "entity_type": "single_member_simplified_joint_stock_company_sasu",
            "entity_type_evidence": _evidence("RNE SASU legal form"),
            "tax_residency_evidence": _evidence("French residency advisor review"),
            "rne_status": "confirmed",
            "rne_evidence": _evidence("RNE company status"),
            "profit_tax_regime": "corporate_income_tax_is",
            "profit_tax_regime_evidence": _evidence("IS regime confirmation"),
            "corporate_income_tax_status": "confirmed",
            "corporate_income_tax_evidence": _evidence("IS account registration"),
            "vat_status": "not_registered",
            "payroll_status": "not_registered",
        }
        payload.update(overrides)
        return payload

    def test_registration_requires_evidence_and_rejects_raw_identifiers(self):
        missing = self._dispatch(
            "tax.fr_single_member_simplified_joint_stock_company.registration_profile",
            {"as_of": "2026-08-14"},
        )
        self.assertFalse(missing["ready"])
        self.assertEqual(
            missing["applicability"],
            "in_scope_single_member_simplified_joint_stock_company_sasu",
        )
        for field in (
            "siren", "siret", "rne_number", "company_number", "tax_number",
            "numero_fiscal", "vat_number", "tva_number", "fr_vat_number",
            "intra_community_vat_number",
        ):
            with self.subTest(field=field), self.assertRaisesRegex(
                ValueError, "raw SIREN",
            ):
                self._dispatch(
                    "tax.fr_single_member_simplified_joint_stock_company.registration_profile",
                    {field: "sensitive"},
                )

    def test_complete_profile_is_evidence_not_tax_conclusion(self):
        output = self._dispatch(
            "tax.fr_single_member_simplified_joint_stock_company.registration_profile",
            self._complete_profile(),
        )
        self.assertTrue(output["ready"], output)
        self.assertEqual(output["tax_readiness"], "design")
        self.assertEqual(output["profit_tax_regime"]["status"], "confirmed")
        self.assertFalse(output["profit_tax_regime"]["determined_by_system"])
        self.assertFalse(output["raw_company_identifier_collected"])
        for field in (
            "french_tax_residency_determined", "profit_tax_regime_determined",
            "corporate_income_tax_rate_determined",
            "small_company_rate_eligibility_determined",
            "corporate_income_tax_installments_determined",
            "vat_registration_liability_determined", "vat_regime_determined",
            "vat_supply_classification_performed", "oss_or_ioss_scheme_determined",
            "cfe_or_cvae_liability_determined",
            "payroll_or_social_contributions_determined",
            "dividend_or_personal_tax_determined", "tax_calculation_performed",
            "filing_performed", "payment_performed", "external_submission_enabled",
        ):
            self.assertFalse(output[field], field)

    def test_ir_option_is_allowed_only_with_evidence(self):
        missing = self._dispatch(
            "tax.fr_single_member_simplified_joint_stock_company.registration_profile",
            self._complete_profile(
                profit_tax_regime="income_tax_ir_option",
                profit_tax_regime_evidence=None,
                corporate_income_tax_status="not_registered",
                corporate_income_tax_evidence=None,
            ),
        )
        self.assertFalse(missing["ready"])
        self.assertEqual(missing["profit_tax_regime"]["status"], "needs_evidence")
        confirmed = self._dispatch(
            "tax.fr_single_member_simplified_joint_stock_company.registration_profile",
            self._complete_profile(
                profit_tax_regime="income_tax_ir_option",
                profit_tax_regime_evidence=_evidence("IR option confirmation"),
                corporate_income_tax_status="not_registered",
                corporate_income_tax_evidence=None,
            ),
        )
        self.assertTrue(confirmed["ready"], confirmed)
        self.assertFalse(confirmed["profit_tax_regime_determined"])

    def test_outside_scope_and_source_expiry_fail_closed(self):
        for entity_type in (
            "simplified_joint_stock_company_sas",
            "single_member_limited_liability_company_eurl",
            "limited_liability_company_sarl", "public_limited_company_sa",
            "branch", "sole_trader",
        ):
            with self.subTest(entity_type=entity_type):
                outside = self._dispatch(
                    "tax.fr_single_member_simplified_joint_stock_company.registration_profile",
                    self._complete_profile(entity_type=entity_type),
                )
                self.assertFalse(outside["ready"])
                self.assertEqual(outside["applicability"], "outside_pack_scope")
        boundary = self._complete_profile(
            as_of=(date(2026, 8, 14) + timedelta(days=self.source_max_age_days)).isoformat(),
        )
        self.assertTrue(self._dispatch(
            "tax.fr_single_member_simplified_joint_stock_company.registration_profile",
            boundary,
        )["ready"])
        expired = self._complete_profile(
            as_of=(date(2026, 8, 14) + timedelta(days=self.source_max_age_days + 1)).isoformat(),
        )
        result = self._dispatch(
            "tax.fr_single_member_simplified_joint_stock_company.registration_profile",
            expired,
        )
        self.assertFalse(result["ready"])
        self.assertEqual(result["source_freshness"]["status"], "source_review_expired")

    def test_evidence_checklist_rejects_missing_duplicate_and_unknown(self):
        missing = self._dispatch(
            "tax.fr_single_member_simplified_joint_stock_company.evidence_checklist",
            {"as_of": "2026-08-14", "provided_evidence": []},
        )
        self.assertIn("required evidence is missing", missing["blockers"])
        ids = sorted({item for values in FR_EVIDENCE_BY_RULE.values() for item in values})
        provided = [
            {"evidence_id": item, **_evidence(f"evidence:{item}")} for item in ids
        ]
        complete = self._dispatch(
            "tax.fr_single_member_simplified_joint_stock_company.evidence_checklist",
            {"as_of": "2026-08-14", "provided_evidence": provided},
        )
        self.assertTrue(complete["ready"], complete)
        duplicate = self._dispatch(
            "tax.fr_single_member_simplified_joint_stock_company.evidence_checklist",
            {"as_of": "2026-08-14", "provided_evidence": [*provided, dict(provided[0])]},
        )
        self.assertEqual(duplicate["duplicate_evidence_ids"], [provided[0]["evidence_id"]])
        unknown = self._dispatch(
            "tax.fr_single_member_simplified_joint_stock_company.evidence_checklist",
            {"as_of": "2026-08-14", "provided_evidence": [
                *provided, {"evidence_id": "invented", **_evidence("x")},
            ]},
        )
        self.assertEqual(unknown["unknown_evidence_ids"], ["invented"])

    def test_calendar_keeps_all_four_dates_manual(self):
        output = self._dispatch(
            "tax.fr_single_member_simplified_joint_stock_company.build_calendar",
            {"period_year": 2026, "as_of": "2026-08-14"},
        )
        self.assertFalse(output["ready"])
        self.assertTrue(
            output["single_member_simplified_joint_stock_company_sasu_scope_confirmed"]
        )
        self.assertEqual(output["task_count"], 4)
        self.assertEqual(
            {task["rule_id"] for task in output["tasks"]},
            {
                "fr.profit_tax.return.calendar",
                "fr.corporate_income_tax.payment.calendar",
                "fr.vat.return_payment.calendar",
                "fr.annual_accounts.filing.calendar",
            },
        )
        self.assertTrue(all(
            task["candidate_due_date"] is None
            and task["candidate_only"]
            and not task["filing_completed"]
            for task in output["tasks"]
        ))
        self.assertFalse(output["profit_tax_regime_determined"])
        self.assertFalse(output["tax_calculation_performed"])
        self.assertFalse(output["filing_performed"])

    def test_wrong_entity_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = json.loads(FR_BOX.read_text(encoding="utf-8"))
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
                    "tax.fr_single_member_simplified_joint_stock_company.registration_profile",
                    {},
                    entity_id="cn_entity",
                )


if __name__ == "__main__":
    unittest.main()
