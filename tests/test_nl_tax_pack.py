from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from src.box_runtime import BoxRuntime
from src.default_services import build_default_service_registry
from src.nl_tax_services import NL_EVIDENCE_BY_RULE
from src.pack_services import PackServiceError


ROOT = Path(__file__).resolve().parents[1]
PACKS = ROOT / "packs"
NL_BOX = ROOT / "examples" / "boxes" / "nl_dtc_shopify_stripe_bv.json"


def _evidence(reference: str) -> dict[str, str]:
    return {"source_reference": reference, "captured_at": "2026-08-13"}


class NetherlandsTaxPackTests(unittest.TestCase):
    def setUp(self):
        self.runtime = BoxRuntime(NL_BOX, PACKS)
        self.services = build_default_service_registry()
        self.source_max_age_days = self.runtime.tax_rules("nl_store")["rules"][
            "review_policy"
        ]["max_age_days"]

    def _dispatch(self, service_id, payload):
        return self.services.dispatch(
            self.runtime, service_id, payload, entity_id="nl_store",
        )["output"]

    def _complete_profile(self, **overrides):
        payload = {
            "as_of": "2026-08-13",
            "entity_type": "private_limited_company_bv",
            "entity_type_evidence": _evidence("KVK BV company status"),
            "tax_residency_evidence": _evidence("Dutch residency advisor review"),
            "kvk_status": "confirmed",
            "kvk_evidence": _evidence("KVK registration"),
            "corporate_income_tax_status": "confirmed",
            "corporate_income_tax_evidence": _evidence("VPB registration"),
            "vat_status": "not_registered",
            "payroll_status": "not_registered",
        }
        payload.update(overrides)
        return payload

    def test_registration_requires_evidence_and_rejects_raw_identifiers(self):
        missing = self._dispatch(
            "tax.nl_private_limited_company.registration_profile",
            {"as_of": "2026-08-13"},
        )
        self.assertFalse(missing["ready"])
        self.assertEqual(
            missing["applicability"], "in_scope_private_limited_company_bv",
        )
        for field in (
            "kvk_number", "company_number", "rsin", "tax_number",
            "tax_id", "vat_number", "btw_number", "btw_id",
        ):
            with self.subTest(field=field), self.assertRaisesRegex(
                ValueError, "raw KVK number",
            ):
                self._dispatch(
                    "tax.nl_private_limited_company.registration_profile",
                    {field: "sensitive"},
                )

    def test_complete_profile_is_evidence_not_tax_conclusion(self):
        output = self._dispatch(
            "tax.nl_private_limited_company.registration_profile",
            self._complete_profile(),
        )
        self.assertTrue(output["ready"], output)
        self.assertEqual(output["tax_readiness"], "design")
        self.assertFalse(output["raw_company_identifier_collected"])
        for field in (
            "dutch_tax_residency_determined",
            "corporate_income_tax_rate_determined", "fiscal_unity_determined",
            "innovation_box_eligibility_determined",
            "vat_registration_liability_determined",
            "vat_supply_classification_performed", "oss_or_ioss_scheme_determined",
            "kor_eligibility_determined", "payroll_or_dga_salary_determined",
            "dividend_withholding_determined", "tax_calculation_performed",
            "filing_performed", "payment_performed", "external_submission_enabled",
        ):
            self.assertFalse(output[field], field)

    def test_outside_scope_and_source_expiry_fail_closed(self):
        outside = self._dispatch(
            "tax.nl_private_limited_company.registration_profile",
            self._complete_profile(entity_type="public_limited_company_nv"),
        )
        self.assertFalse(outside["ready"])
        self.assertEqual(outside["applicability"], "outside_pack_scope")
        boundary = self._complete_profile(
            as_of=(date(2026, 8, 13) + timedelta(days=self.source_max_age_days)).isoformat(),
        )
        self.assertTrue(self._dispatch(
            "tax.nl_private_limited_company.registration_profile", boundary,
        )["ready"])
        expired = self._complete_profile(
            as_of=(date(2026, 8, 13) + timedelta(days=self.source_max_age_days + 1)).isoformat(),
        )
        result = self._dispatch(
            "tax.nl_private_limited_company.registration_profile", expired,
        )
        self.assertFalse(result["ready"])
        self.assertEqual(result["source_freshness"]["status"], "source_review_expired")

    def test_evidence_checklist_rejects_missing_duplicate_and_unknown(self):
        missing = self._dispatch(
            "tax.nl_private_limited_company.evidence_checklist",
            {"as_of": "2026-08-13", "provided_evidence": []},
        )
        self.assertIn("required evidence is missing", missing["blockers"])
        ids = sorted({item for values in NL_EVIDENCE_BY_RULE.values() for item in values})
        provided = [
            {"evidence_id": item, **_evidence(f"evidence:{item}")} for item in ids
        ]
        complete = self._dispatch(
            "tax.nl_private_limited_company.evidence_checklist",
            {"as_of": "2026-08-13", "provided_evidence": provided},
        )
        self.assertTrue(complete["ready"], complete)
        duplicate = self._dispatch(
            "tax.nl_private_limited_company.evidence_checklist",
            {"as_of": "2026-08-13", "provided_evidence": [*provided, dict(provided[0])]},
        )
        self.assertEqual(duplicate["duplicate_evidence_ids"], [provided[0]["evidence_id"]])
        unknown = self._dispatch(
            "tax.nl_private_limited_company.evidence_checklist",
            {"as_of": "2026-08-13", "provided_evidence": [
                *provided, {"evidence_id": "invented", **_evidence("x")},
            ]},
        )
        self.assertEqual(unknown["unknown_evidence_ids"], ["invented"])

    def test_calendar_keeps_vpb_vat_and_kvk_dates_manual(self):
        output = self._dispatch(
            "tax.nl_private_limited_company.build_calendar",
            {"period_year": 2026, "as_of": "2026-08-13"},
        )
        self.assertFalse(output["ready"])
        self.assertTrue(output["private_limited_company_bv_scope_confirmed"])
        self.assertEqual(output["task_count"], 3)
        by_rule = {task["rule_id"]: task for task in output["tasks"]}
        self.assertEqual(
            set(by_rule), {
                "nl.vpb.return.calendar", "nl.vat.return_payment.calendar",
                "nl.kvk.financial_statements.calendar",
            },
        )
        self.assertTrue(all(
            task["candidate_due_date"] is None and task["candidate_only"]
            and not task["filing_completed"]
            for task in output["tasks"]
        ))
        self.assertEqual(
            by_rule["nl.vpb.return.calendar"]["status"], "needs_configuration",
        )
        self.assertEqual(
            by_rule["nl.vat.return_payment.calendar"]["status"],
            "needs_registration_confirmation",
        )
        self.assertFalse(output["tax_calculation_performed"])
        self.assertFalse(output["filing_performed"])

    def test_wrong_entity_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = json.loads(NL_BOX.read_text(encoding="utf-8"))
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
            with self.assertRaisesRegex(PackServiceError, "uses jurisdiction.cn_mainland"):
                self.services.dispatch(
                    runtime, "tax.nl_private_limited_company.registration_profile", {},
                    entity_id="cn_entity",
                )


if __name__ == "__main__":
    unittest.main()
