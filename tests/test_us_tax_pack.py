from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from src.box_runtime import BoxRuntime
from src.default_services import build_default_service_registry
from src.pack_services import PackServiceError
from src.us_tax_services import US_EVIDENCE_BY_RULE


ROOT = Path(__file__).resolve().parents[1]
PACKS = ROOT / "packs"
US_BOX = ROOT / "examples" / "boxes" / "us_dtc_shopify_stripe_c_corp.json"


def _evidence(source_reference: str) -> dict[str, str]:
    return {"source_reference": source_reference, "captured_at": "2026-08-13"}


class UsFederalTaxPackTests(unittest.TestCase):
    def setUp(self):
        self.runtime = BoxRuntime(US_BOX, PACKS)
        self.services = build_default_service_registry()
        self.source_max_age_days = self.runtime.tax_rules("us_store")["rules"][
            "review_policy"
        ]["max_age_days"]

    def _dispatch(self, service_id, payload):
        return self.services.dispatch(
            self.runtime, service_id, payload, entity_id="us_store",
        )["output"]

    def test_registration_profile_requires_classification_and_ein_evidence(self):
        output = self._dispatch("tax.us_federal.registration_profile", {"as_of": "2026-08-13"})
        self.assertFalse(output["ready"])
        self.assertEqual(output["applicability"], "in_scope_c_corporation")
        self.assertEqual(output["federal_tax_classification"]["status"], "needs_evidence")
        self.assertEqual(output["ein_registration"]["review_status"], "needs_evidence")
        self.assertFalse(output["ein_registration"]["raw_identifier_collected"])
        self.assertFalse(output["tax_calculation_performed"])
        self.assertFalse(output["filing_performed"])
        self.assertFalse(output["external_submission_enabled"])

    def test_confirmed_profile_is_source_backed_but_not_a_tax_determination(self):
        output = self._dispatch("tax.us_federal.registration_profile", {
            "as_of": "2026-08-13",
            "federal_tax_classification": "c_corporation",
            "classification_evidence": _evidence("formation-and-classification-review"),
            "ein_status": "confirmed",
            "ein_evidence": _evidence("IRS EIN confirmation reference; value withheld"),
        })
        self.assertTrue(output["ready"], output)
        self.assertEqual(output["source_freshness"]["status"], "current")
        self.assertFalse(output["classification_determination_performed"])
        self.assertTrue(all(
            source["url"].startswith("https://www.irs.gov/")
            for source in output["official_sources"]
        ))
        with self.assertRaisesRegex(ValueError, "raw EIN"):
            self._dispatch("tax.us_federal.registration_profile", {"ein_number": "12-3456789"})

    def test_non_c_corporation_is_explicitly_outside_scope(self):
        output = self._dispatch("tax.us_federal.registration_profile", {
            "as_of": "2026-08-13",
            "federal_tax_classification": "s_corporation",
            "classification_evidence": _evidence("S election evidence"),
            "ein_status": "confirmed",
            "ein_evidence": _evidence("IRS EIN confirmation reference"),
        })
        self.assertFalse(output["ready"])
        self.assertEqual(output["applicability"], "outside_pack_scope")
        self.assertEqual(
            output["federal_tax_classification"]["status"], "outside_pack_scope",
        )

    def test_source_freshness_boundary_and_expiry_fail_closed(self):
        verified = date(2026, 8, 13)
        base = {
            "federal_tax_classification": "c_corporation",
            "classification_evidence": _evidence("classification"),
            "ein_status": "confirmed",
            "ein_evidence": _evidence("ein-confirmation"),
        }
        boundary = dict(base, as_of=(verified + timedelta(days=self.source_max_age_days)).isoformat())
        self.assertTrue(self._dispatch("tax.us_federal.registration_profile", boundary)["ready"])
        expired = dict(base, as_of=(verified + timedelta(days=self.source_max_age_days + 1)).isoformat())
        output = self._dispatch("tax.us_federal.registration_profile", expired)
        self.assertFalse(output["ready"])
        self.assertEqual(output["source_freshness"]["status"], "source_review_expired")

    def test_evidence_checklist_handles_missing_complete_duplicate_and_unknown_inputs(self):
        missing = self._dispatch("tax.us_federal.evidence_checklist", {
            "as_of": "2026-08-13", "provided_evidence": [],
        })
        self.assertFalse(missing["ready"])
        self.assertIn("required evidence is missing", missing["blockers"])

        all_ids = sorted({
            evidence_id
            for values in US_EVIDENCE_BY_RULE.values()
            for evidence_id in values
        })
        provided = [
            {"evidence_id": evidence_id, **_evidence(f"evidence:{evidence_id}")}
            for evidence_id in all_ids
        ]
        complete = self._dispatch("tax.us_federal.evidence_checklist", {
            "as_of": "2026-08-13", "provided_evidence": provided,
        })
        self.assertTrue(complete["ready"], complete)
        self.assertTrue(all(item["complete"] for item in complete["items"]))
        self.assertFalse(complete["filing_performed"])

        duplicate = self._dispatch("tax.us_federal.evidence_checklist", {
            "as_of": "2026-08-13", "provided_evidence": provided + [dict(provided[0])],
        })
        self.assertFalse(duplicate["ready"])
        self.assertEqual(duplicate["duplicate_evidence_ids"], [provided[0]["evidence_id"]])

        unknown = self._dispatch("tax.us_federal.evidence_checklist", {
            "as_of": "2026-08-13",
            "provided_evidence": provided + [{"evidence_id": "invented", **_evidence("unknown")}],
        })
        self.assertFalse(unknown["ready"])
        self.assertEqual(unknown["unknown_evidence_ids"], ["invented"])

    def test_calendar_exposes_manual_configuration_without_invented_dates(self):
        output = self._dispatch("tax.us_federal.build_calendar", {
            "period_year": 2025, "as_of": "2026-08-13",
        })
        self.assertFalse(output["ready"])
        self.assertTrue(output["c_corporation_scope_confirmed"])
        self.assertEqual(output["task_count"], 2)
        self.assertTrue(all(task["status"] == "needs_configuration" for task in output["tasks"]))
        self.assertTrue(all(task["candidate_due_date"] is None for task in output["tasks"]))
        self.assertTrue(all(task["review_gate"] == "tax_advisor_review" for task in output["tasks"]))
        self.assertFalse(output["tax_calculation_performed"])
        self.assertFalse(output["filing_performed"])

    def test_wrong_entity_is_rejected_even_when_pack_is_selected_elsewhere(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = json.loads(US_BOX.read_text(encoding="utf-8"))
            config["connector_bindings"] = [{
                "connector_pack": pack_id,
                "entity_ids": [config["entities"][0]["id"]],
            } for pack_id in config["connectors"]]
            config["reporting_currency"] = "USD"
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
                    runtime, "tax.us_federal.registration_profile", {}, entity_id="cn_entity",
                )

    def test_pack_keeps_required_review_gates_and_design_maturity(self):
        snapshot = self.runtime.snapshot()
        pack = next(item for item in snapshot["packs"] if item["id"] == "jurisdiction.us_federal")
        self.assertEqual(pack["status"], "experimental")
        entity = self.runtime.entities.get("us_store")
        self.assertEqual(entity.tax_readiness, "design")
        self.assertTrue({
            "tax_registration_confirmation", "tax_advisor_review", "tax_filing_release",
        } <= set(snapshot["manual_review_gates"]))


if __name__ == "__main__":
    unittest.main()
