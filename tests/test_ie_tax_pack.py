from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from src.box_runtime import BoxRuntime
from src.default_services import build_default_service_registry
from src.ie_tax_services import IE_EVIDENCE_BY_RULE
from src.pack_services import PackServiceError


ROOT = Path(__file__).resolve().parents[1]
PACKS = ROOT / "packs"
IE_BOX = ROOT / "examples" / "boxes" / "ie_dtc_shopify_stripe_ltd.json"


def _evidence(reference: str) -> dict[str, str]:
    return {"source_reference": reference, "captured_at": "2026-08-13"}


class IrelandTaxPackTests(unittest.TestCase):
    def setUp(self):
        self.runtime = BoxRuntime(IE_BOX, PACKS)
        self.services = build_default_service_registry()
        self.source_max_age_days = self.runtime.tax_rules("ie_store")["rules"][
            "review_policy"
        ]["max_age_days"]

    def _dispatch(self, service_id, payload):
        return self.services.dispatch(
            self.runtime, service_id, payload, entity_id="ie_store",
        )["output"]

    def _complete_profile(self, **overrides):
        payload = {
            "as_of": "2026-08-13",
            "entity_type": "private_company_limited_by_shares",
            "entity_type_evidence": _evidence("CRO LTD company status"),
            "tax_residency_evidence": _evidence("Irish residency advisor review"),
            "tax_reference_status": "confirmed",
            "tax_reference_evidence": _evidence("Revenue tax registration"),
            "corporation_tax_status": "confirmed",
            "corporation_tax_evidence": _evidence("Corporation Tax registration"),
            "vat_status": "not_registered",
            "employer_status": "not_registered",
        }
        payload.update(overrides)
        return payload

    def test_registration_requires_evidence_and_rejects_raw_identifiers(self):
        missing = self._dispatch(
            "tax.ie_private_limited_company.registration_profile",
            {"as_of": "2026-08-13"},
        )
        self.assertFalse(missing["ready"])
        self.assertEqual(
            missing["applicability"], "in_scope_private_limited_company",
        )
        for field in ("company_number", "tax_reference", "tax_id", "vat_number"):
            with self.subTest(field=field), self.assertRaisesRegex(
                ValueError, "raw company number",
            ):
                self._dispatch(
                    "tax.ie_private_limited_company.registration_profile",
                    {field: "sensitive"},
                )

    def test_complete_profile_is_evidence_not_residency_vat_or_tax_conclusion(self):
        output = self._dispatch(
            "tax.ie_private_limited_company.registration_profile",
            self._complete_profile(),
        )
        self.assertTrue(output["ready"], output)
        self.assertEqual(output["tax_readiness"], "design")
        self.assertFalse(output["raw_company_identifier_collected"])
        self.assertFalse(output["irish_tax_residency_determined"])
        self.assertFalse(output["vat_registration_liability_determined"])
        self.assertFalse(output["vat_supply_classification_performed"])
        self.assertFalse(output["corporation_tax_rate_determined"])
        self.assertFalse(output["preliminary_tax_determined"])
        self.assertFalse(output["tax_calculation_performed"])
        self.assertFalse(output["filing_performed"])
        self.assertFalse(output["payment_performed"])
        self.assertFalse(output["external_submission_enabled"])

    def test_outside_scope_and_source_expiry_fail_closed(self):
        outside = self._dispatch(
            "tax.ie_private_limited_company.registration_profile",
            self._complete_profile(entity_type="dac"),
        )
        self.assertFalse(outside["ready"])
        self.assertEqual(outside["applicability"], "outside_pack_scope")
        boundary = self._complete_profile(
            as_of=(date(2026, 8, 13) + timedelta(days=self.source_max_age_days)).isoformat(),
        )
        self.assertTrue(self._dispatch(
            "tax.ie_private_limited_company.registration_profile", boundary,
        )["ready"])
        expired = self._complete_profile(
            as_of=(date(2026, 8, 13) + timedelta(days=self.source_max_age_days + 1)).isoformat(),
        )
        result = self._dispatch(
            "tax.ie_private_limited_company.registration_profile", expired,
        )
        self.assertFalse(result["ready"])
        self.assertEqual(result["source_freshness"]["status"], "source_review_expired")

    def test_evidence_checklist_rejects_missing_duplicate_and_unknown(self):
        missing = self._dispatch(
            "tax.ie_private_limited_company.evidence_checklist",
            {"as_of": "2026-08-13", "provided_evidence": []},
        )
        self.assertIn("required evidence is missing", missing["blockers"])
        ids = sorted({item for values in IE_EVIDENCE_BY_RULE.values() for item in values})
        provided = [
            {"evidence_id": item, **_evidence(f"evidence:{item}")} for item in ids
        ]
        complete = self._dispatch(
            "tax.ie_private_limited_company.evidence_checklist",
            {"as_of": "2026-08-13", "provided_evidence": provided},
        )
        self.assertTrue(complete["ready"], complete)
        duplicate = self._dispatch(
            "tax.ie_private_limited_company.evidence_checklist",
            {"as_of": "2026-08-13", "provided_evidence": [*provided, dict(provided[0])]},
        )
        self.assertEqual(duplicate["duplicate_evidence_ids"], [provided[0]["evidence_id"]])
        unknown = self._dispatch(
            "tax.ie_private_limited_company.evidence_checklist",
            {"as_of": "2026-08-13", "provided_evidence": [
                *provided, {"evidence_id": "invented", **_evidence("x")},
            ]},
        )
        self.assertEqual(unknown["unknown_evidence_ids"], ["invented"])

    def test_calendar_calculates_only_explicit_cro_ard_window(self):
        output = self._dispatch(
            "tax.ie_private_limited_company.build_calendar",
            {"period_year": 2026, "as_of": "2026-08-13", "anchors": {
                "cro_annual_return_date": "2026-09-30",
            }},
        )
        self.assertFalse(output["ready"])
        self.assertTrue(output["private_limited_company_scope_confirmed"])
        self.assertEqual(output["task_count"], 4)
        by_rule = {task["rule_id"]: task for task in output["tasks"]}
        self.assertEqual(
            by_rule["ie.cro.annual_return.calendar"]["candidate_due_date"],
            "2026-11-25",
        )
        for rule_id in (
            "ie.ct1.return.calendar", "ie.corporation_tax.payment.calendar",
            "ie.vat.return_payment.calendar",
        ):
            self.assertIsNone(by_rule[rule_id]["candidate_due_date"])
        self.assertFalse(output["preliminary_tax_determined"])
        self.assertFalse(output["tax_calculation_performed"])
        self.assertFalse(output["filing_performed"])

    def test_missing_ard_and_wrong_entity_fail_closed(self):
        calendar = self._dispatch(
            "tax.ie_private_limited_company.build_calendar",
            {"period_year": 2026, "as_of": "2026-08-13"},
        )
        task = next(
            item for item in calendar["tasks"]
            if item["rule_id"] == "ie.cro.annual_return.calendar"
        )
        self.assertEqual(task["status"], "needs_configuration")
        self.assertEqual(task["missing_configuration"], ["cro_annual_return_date"])
        with tempfile.TemporaryDirectory() as temp_dir:
            config = json.loads(IE_BOX.read_text(encoding="utf-8"))
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
                    runtime, "tax.ie_private_limited_company.registration_profile", {},
                    entity_id="cn_entity",
                )


if __name__ == "__main__":
    unittest.main()
