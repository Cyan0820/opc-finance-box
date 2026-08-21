from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from src.box_runtime import BoxRuntime
from src.default_services import build_default_service_registry
from src.kr_tax_services import KR_EVIDENCE_BY_RULE
from src.pack_services import PackServiceError


ROOT = Path(__file__).resolve().parents[1]
PACKS = ROOT / "packs"
KR_BOX = ROOT / "examples" / "boxes" / "kr_dtc_shopify_stripe_jusik_hoesa.json"


def _evidence(reference: str) -> dict[str, str]:
    return {"source_reference": reference, "captured_at": "2026-08-16"}


class KoreaTaxPackTests(unittest.TestCase):
    def setUp(self):
        self.runtime = BoxRuntime(KR_BOX, PACKS)
        self.services = build_default_service_registry()
        self.source_max_age_days = self.runtime.tax_rules("kr_store")["rules"][
            "review_policy"
        ]["max_age_days"]

    def _dispatch(self, service_id, payload):
        return self.services.dispatch(
            self.runtime, service_id, payload, entity_id="kr_store",
        )["output"]

    def _complete_profile(self, **overrides):
        payload = {
            "as_of": "2026-08-16",
            "entity_type": "stock_company_jusik_hoesa",
            "entity_type_evidence": _evidence("commercial registry legal form"),
            "tax_residency_evidence": _evidence("Korea residency advisor review"),
            "corporate_registry_status": "confirmed",
            "corporate_registry_evidence": _evidence("corporate registry status"),
            "business_registration_status": "confirmed",
            "business_registration_evidence": _evidence("business registration"),
            "corporate_income_tax_status": "confirmed",
            "corporate_income_tax_evidence": _evidence("corporate income tax"),
            "local_corporate_income_tax_status": "confirmed",
            "local_corporate_income_tax_evidence": _evidence("local corporate income tax"),
            "vat_status": "not_registered",
            "etax_invoice_status": "not_registered",
            "withholding_status": "not_registered",
        }
        payload.update(overrides)
        return payload

    def test_registration_requires_evidence_and_rejects_raw_identifiers(self):
        missing = self._dispatch(
            "tax.kr_domestic_corporation.registration_profile",
            {"as_of": "2026-08-16"},
        )
        self.assertFalse(missing["ready"])
        self.assertEqual(
            missing["applicability"], "in_scope_korea_domestic_for_profit_corporation",
        )
        for field in (
            "corporate_registration_number", "company_registration_number",
            "business_registration_number", "taxpayer_id", "tax_id",
            "hometax_user_id", "wetax_user_id", "electronic_certificate",
        ):
            with self.subTest(field=field), self.assertRaisesRegex(
                ValueError, "raw Korean corporate",
            ):
                self._dispatch(
                    "tax.kr_domestic_corporation.registration_profile",
                    {field: "sensitive"},
                )

    def test_supported_profiles_are_evidence_not_tax_conclusions(self):
        for entity_type in (
            "stock_company_jusik_hoesa",
            "limited_company_yuhan_hoesa",
            "limited_liability_company_yuhan_chaegim_hoesa",
        ):
            with self.subTest(entity_type=entity_type):
                output = self._dispatch(
                    "tax.kr_domestic_corporation.registration_profile",
                    self._complete_profile(entity_type=entity_type),
                )
                self.assertTrue(output["ready"], output)
                self.assertEqual(output["tax_readiness"], "design")
                self.assertFalse(output["raw_company_or_tax_identifier_collected"])
                self.assertFalse(output["entity_type"]["determined_by_system"])
                for field in (
                    "korean_tax_residency_determined",
                    "entity_type_determined_by_system",
                    "corporate_income_tax_liability_determined",
                    "corporate_income_tax_rate_or_amount_determined",
                    "interim_payment_applicability_determined",
                    "local_tax_authority_or_allocation_determined",
                    "local_corporate_income_tax_rate_or_amount_determined",
                    "vat_taxpayer_status_determined",
                    "vat_supply_classification_performed",
                    "vat_rate_or_amount_determined",
                    "etax_invoice_obligation_or_deadline_determined",
                    "withholding_obligation_or_deadline_determined",
                    "sme_or_tax_incentive_eligibility_determined",
                    "foreign_investment_or_group_taxation_determined",
                    "cross_border_or_permanent_establishment_determined",
                    "transfer_pricing_or_customs_determined",
                    "payroll_or_social_insurance_determined",
                    "tax_calculation_performed", "filing_performed",
                    "payment_performed", "external_submission_enabled",
                ):
                    self.assertFalse(output[field], field)

    def test_optional_registration_claims_need_evidence(self):
        output = self._dispatch(
            "tax.kr_domestic_corporation.registration_profile",
            self._complete_profile(
                vat_status="confirmed", etax_invoice_status="confirmed",
                withholding_status="confirmed",
            ),
        )
        self.assertFalse(output["ready"])
        self.assertEqual(output["registrations"]["vat"]["review_status"], "needs_evidence")
        confirmed = self._dispatch(
            "tax.kr_domestic_corporation.registration_profile",
            self._complete_profile(
                vat_status="confirmed", vat_evidence=_evidence("VAT status"),
                etax_invoice_status="confirmed",
                etax_invoice_evidence=_evidence("e-tax invoice obligation"),
                withholding_status="confirmed",
                withholding_evidence=_evidence("withholding obligation"),
            ),
        )
        self.assertTrue(confirmed["ready"], confirmed)
        self.assertFalse(confirmed["vat_taxpayer_status_determined"])
        self.assertFalse(confirmed["etax_invoice_obligation_or_deadline_determined"])

    def test_outside_scope_and_source_expiry_fail_closed(self):
        for entity_type in (
            "general_partnership_hapmyeong_hoesa",
            "limited_partnership_hapja_hoesa",
            "foreign_corporation", "branch", "sole_proprietor", "nonprofit",
        ):
            with self.subTest(entity_type=entity_type):
                outside = self._dispatch(
                    "tax.kr_domestic_corporation.registration_profile",
                    self._complete_profile(entity_type=entity_type),
                )
                self.assertFalse(outside["ready"])
                self.assertEqual(outside["applicability"], "outside_pack_scope")
        boundary = self._complete_profile(
            as_of=(date(2026, 8, 16) + timedelta(days=self.source_max_age_days)).isoformat(),
        )
        self.assertTrue(self._dispatch(
            "tax.kr_domestic_corporation.registration_profile", boundary,
        )["ready"])
        expired = self._complete_profile(
            as_of=(date(2026, 8, 16) + timedelta(days=self.source_max_age_days + 1)).isoformat(),
        )
        result = self._dispatch(
            "tax.kr_domestic_corporation.registration_profile", expired,
        )
        self.assertFalse(result["ready"])
        self.assertEqual(result["source_freshness"]["status"], "source_review_expired")

    def test_evidence_checklist_rejects_missing_duplicate_and_unknown(self):
        missing = self._dispatch(
            "tax.kr_domestic_corporation.evidence_checklist",
            {"as_of": "2026-08-16", "provided_evidence": []},
        )
        self.assertIn("required evidence is missing", missing["blockers"])
        ids = sorted({item for values in KR_EVIDENCE_BY_RULE.values() for item in values})
        provided = [
            {"evidence_id": item, **_evidence(f"evidence:{item}")} for item in ids
        ]
        complete = self._dispatch(
            "tax.kr_domestic_corporation.evidence_checklist",
            {"as_of": "2026-08-16", "provided_evidence": provided},
        )
        self.assertTrue(complete["ready"], complete)
        duplicate = self._dispatch(
            "tax.kr_domestic_corporation.evidence_checklist",
            {"as_of": "2026-08-16", "provided_evidence": [*provided, dict(provided[0])]},
        )
        self.assertEqual(duplicate["duplicate_evidence_ids"], [provided[0]["evidence_id"]])
        unknown = self._dispatch(
            "tax.kr_domestic_corporation.evidence_checklist",
            {"as_of": "2026-08-16", "provided_evidence": [
                *provided, {"evidence_id": "invented", **_evidence("x")},
            ]},
        )
        self.assertEqual(unknown["unknown_evidence_ids"], ["invented"])

    def test_calendar_keeps_all_korea_dates_manual(self):
        output = self._dispatch(
            "tax.kr_domestic_corporation.build_calendar",
            {"period_year": 2026, "as_of": "2026-08-16"},
        )
        self.assertFalse(output["ready"])
        self.assertTrue(output["domestic_for_profit_corporation_scope_confirmed"])
        self.assertEqual(output["task_count"], 5)
        by_rule = {task["rule_id"]: task for task in output["tasks"]}
        self.assertEqual(set(by_rule), {
            "kr.corporate_income_tax.return_payment.calendar",
            "kr.local_corporate_income_tax.return_payment.calendar",
            "kr.vat.return_payment.calendar",
            "kr.withholding.payment.calendar",
            "kr.etax_invoice.issue_transmit.calendar",
        })
        self.assertTrue(all(
            task["candidate_due_date"] is None
            and task["candidate_only"]
            and not task["filing_completed"]
            for task in output["tasks"]
        ))
        self.assertEqual(
            by_rule["kr.corporate_income_tax.return_payment.calendar"]["status"],
            "needs_configuration",
        )
        self.assertEqual(
            by_rule["kr.vat.return_payment.calendar"]["status"],
            "needs_registration_confirmation",
        )
        self.assertFalse(output["tax_calculation_performed"])
        self.assertFalse(output["filing_performed"])

    def test_wrong_entity_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = json.loads(KR_BOX.read_text(encoding="utf-8"))
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
                    "tax.kr_domestic_corporation.registration_profile",
                    {}, entity_id="cn_entity",
                )


if __name__ == "__main__":
    unittest.main()
