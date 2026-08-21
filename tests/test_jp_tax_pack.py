from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from src.box_runtime import BoxRuntime
from src.default_services import build_default_service_registry
from src.jp_tax_services import JP_EVIDENCE_BY_RULE
from src.pack_services import PackServiceError


ROOT = Path(__file__).resolve().parents[1]
PACKS = ROOT / "packs"
JP_BOX = ROOT / "examples" / "boxes" / "jp_dtc_shopify_stripe_kk.json"


def _evidence(reference: str) -> dict[str, str]:
    return {"source_reference": reference, "captured_at": "2026-08-16"}


class JapanTaxPackTests(unittest.TestCase):
    def setUp(self):
        self.runtime = BoxRuntime(JP_BOX, PACKS)
        self.services = build_default_service_registry()
        self.source_max_age_days = self.runtime.tax_rules("jp_store")["rules"][
            "review_policy"
        ]["max_age_days"]

    def _dispatch(self, service_id, payload):
        return self.services.dispatch(
            self.runtime, service_id, payload, entity_id="jp_store",
        )["output"]

    def _complete_profile(self, **overrides):
        payload = {
            "as_of": "2026-08-16",
            "entity_type": "stock_company_kabushiki_kaisha",
            "entity_type_evidence": _evidence("commercial registry KK form"),
            "tax_residency_evidence": _evidence("Japan residency advisor review"),
            "commercial_registry_status": "confirmed",
            "commercial_registry_evidence": _evidence("commercial registry status"),
            "corporation_tax_status": "confirmed",
            "corporation_tax_evidence": _evidence("corporation tax registration"),
            "local_corporate_tax_status": "confirmed",
            "local_corporate_tax_evidence": _evidence("local tax registration"),
            "consumption_tax_status": "not_registered",
            "qualified_invoice_issuer_status": "not_registered",
            "withholding_status": "not_registered",
            "blue_return_status": "not_registered",
        }
        payload.update(overrides)
        return payload

    def test_registration_requires_evidence_and_rejects_raw_identifiers(self):
        missing = self._dispatch(
            "tax.jp_domestic_corporation.registration_profile",
            {"as_of": "2026-08-16"},
        )
        self.assertFalse(missing["ready"])
        self.assertEqual(missing["applicability"], "in_scope_japan_domestic_kk_or_gk")
        for field in (
            "corporate_number", "houjin_bangou", "company_number", "tax_number",
            "tax_id", "qualified_invoice_issuer_number",
            "invoice_registration_number", "etax_user_identification_number",
            "eltax_user_id",
        ):
            with self.subTest(field=field), self.assertRaisesRegex(
                ValueError, "raw Japanese corporate",
            ):
                self._dispatch(
                    "tax.jp_domestic_corporation.registration_profile",
                    {field: "sensitive"},
                )

    def test_kk_and_gk_profiles_are_evidence_not_tax_conclusions(self):
        for entity_type in (
            "stock_company_kabushiki_kaisha",
            "limited_liability_company_godo_kaisha",
        ):
            with self.subTest(entity_type=entity_type):
                output = self._dispatch(
                    "tax.jp_domestic_corporation.registration_profile",
                    self._complete_profile(entity_type=entity_type),
                )
                self.assertTrue(output["ready"], output)
                self.assertEqual(output["tax_readiness"], "design")
                self.assertFalse(output["raw_company_or_tax_identifier_collected"])
                self.assertFalse(output["entity_type"]["determined_by_system"])
                for field in (
                    "japanese_tax_residency_determined",
                    "entity_type_determined_by_system",
                    "corporation_tax_liability_determined",
                    "corporation_tax_rate_or_amount_determined",
                    "defense_special_corporation_tax_determined",
                    "local_tax_authority_determined",
                    "local_corporate_tax_rate_or_amount_determined",
                    "blue_return_eligibility_determined",
                    "group_taxation_determined",
                    "consumption_taxable_person_status_determined",
                    "consumption_tax_supply_classification_performed",
                    "simplified_tax_system_eligibility_determined",
                    "qualified_invoice_issuer_status_determined",
                    "withholding_obligation_determined",
                    "withholding_special_deadline_determined",
                    "cross_border_or_permanent_establishment_determined",
                    "transfer_pricing_or_customs_determined",
                    "payroll_or_social_insurance_determined",
                    "tax_calculation_performed", "filing_performed",
                    "payment_performed", "external_submission_enabled",
                ):
                    self.assertFalse(output[field], field)

    def test_optional_registration_claims_need_evidence(self):
        output = self._dispatch(
            "tax.jp_domestic_corporation.registration_profile",
            self._complete_profile(
                consumption_tax_status="confirmed",
                qualified_invoice_issuer_status="confirmed",
                blue_return_status="confirmed",
            ),
        )
        self.assertFalse(output["ready"])
        self.assertEqual(
            output["registrations"]["consumption_tax"]["review_status"],
            "needs_evidence",
        )
        confirmed = self._dispatch(
            "tax.jp_domestic_corporation.registration_profile",
            self._complete_profile(
                consumption_tax_status="confirmed",
                consumption_tax_evidence=_evidence("consumption tax status"),
                qualified_invoice_issuer_status="confirmed",
                qualified_invoice_issuer_evidence=_evidence("invoice issuer status"),
                blue_return_status="confirmed",
                blue_return_evidence=_evidence("blue return approval status"),
            ),
        )
        self.assertTrue(confirmed["ready"], confirmed)
        self.assertFalse(confirmed["qualified_invoice_issuer_status_determined"])
        self.assertFalse(confirmed["blue_return_eligibility_determined"])

    def test_outside_scope_and_source_expiry_fail_closed(self):
        for entity_type in (
            "general_partnership_gomei_kaisha", "limited_partnership_goshi_kaisha",
            "foreign_corporation", "branch", "sole_proprietor",
        ):
            with self.subTest(entity_type=entity_type):
                outside = self._dispatch(
                    "tax.jp_domestic_corporation.registration_profile",
                    self._complete_profile(entity_type=entity_type),
                )
                self.assertFalse(outside["ready"])
                self.assertEqual(outside["applicability"], "outside_pack_scope")
        boundary = self._complete_profile(
            as_of=(date(2026, 8, 16) + timedelta(days=self.source_max_age_days)).isoformat(),
        )
        self.assertTrue(self._dispatch(
            "tax.jp_domestic_corporation.registration_profile", boundary,
        )["ready"])
        expired = self._complete_profile(
            as_of=(date(2026, 8, 16) + timedelta(days=self.source_max_age_days + 1)).isoformat(),
        )
        result = self._dispatch(
            "tax.jp_domestic_corporation.registration_profile", expired,
        )
        self.assertFalse(result["ready"])
        self.assertEqual(result["source_freshness"]["status"], "source_review_expired")

    def test_evidence_checklist_rejects_missing_duplicate_and_unknown(self):
        missing = self._dispatch(
            "tax.jp_domestic_corporation.evidence_checklist",
            {"as_of": "2026-08-16", "provided_evidence": []},
        )
        self.assertIn("required evidence is missing", missing["blockers"])
        ids = sorted({item for values in JP_EVIDENCE_BY_RULE.values() for item in values})
        provided = [
            {"evidence_id": item, **_evidence(f"evidence:{item}")} for item in ids
        ]
        complete = self._dispatch(
            "tax.jp_domestic_corporation.evidence_checklist",
            {"as_of": "2026-08-16", "provided_evidence": provided},
        )
        self.assertTrue(complete["ready"], complete)
        duplicate = self._dispatch(
            "tax.jp_domestic_corporation.evidence_checklist",
            {"as_of": "2026-08-16", "provided_evidence": [*provided, dict(provided[0])]},
        )
        self.assertEqual(duplicate["duplicate_evidence_ids"], [provided[0]["evidence_id"]])
        unknown = self._dispatch(
            "tax.jp_domestic_corporation.evidence_checklist",
            {"as_of": "2026-08-16", "provided_evidence": [
                *provided, {"evidence_id": "invented", **_evidence("x")},
            ]},
        )
        self.assertEqual(unknown["unknown_evidence_ids"], ["invented"])

    def test_calendar_keeps_all_japan_dates_manual(self):
        output = self._dispatch(
            "tax.jp_domestic_corporation.build_calendar",
            {"period_year": 2026, "as_of": "2026-08-16"},
        )
        self.assertFalse(output["ready"])
        self.assertTrue(output["domestic_kk_or_gk_scope_confirmed"])
        self.assertEqual(output["task_count"], 4)
        by_rule = {task["rule_id"]: task for task in output["tasks"]}
        self.assertEqual(set(by_rule), {
            "jp.corporation_tax.return_payment.calendar",
            "jp.local_corporate_tax.return_payment.calendar",
            "jp.consumption_tax.return_payment.calendar",
            "jp.withholding_tax.payment.calendar",
        })
        self.assertTrue(all(
            task["candidate_due_date"] is None
            and task["candidate_only"]
            and not task["filing_completed"]
            for task in output["tasks"]
        ))
        self.assertEqual(
            by_rule["jp.corporation_tax.return_payment.calendar"]["status"],
            "needs_configuration",
        )
        self.assertEqual(
            by_rule["jp.consumption_tax.return_payment.calendar"]["status"],
            "needs_registration_confirmation",
        )
        self.assertFalse(output["tax_calculation_performed"])
        self.assertFalse(output["filing_performed"])

    def test_wrong_entity_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = json.loads(JP_BOX.read_text(encoding="utf-8"))
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
                    "tax.jp_domestic_corporation.registration_profile",
                    {},
                    entity_id="cn_entity",
                )


if __name__ == "__main__":
    unittest.main()
