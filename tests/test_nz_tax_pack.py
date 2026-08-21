from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from src.box_runtime import BoxRuntime
from src.default_services import build_default_service_registry
from src.nz_tax_services import NZ_EVIDENCE_BY_RULE
from src.pack_services import PackServiceError


ROOT = Path(__file__).resolve().parents[1]
PACKS = ROOT / "packs"
NZ_BOX = ROOT / "examples" / "boxes" / "nz_dtc_shopify_stripe_limited_company.json"


def _evidence(reference: str) -> dict[str, str]:
    return {"source_reference": reference, "captured_at": "2026-08-13"}


class NewZealandTaxPackTests(unittest.TestCase):
    def setUp(self):
        self.runtime = BoxRuntime(NZ_BOX, PACKS)
        self.services = build_default_service_registry()
        self.source_max_age_days = self.runtime.tax_rules("nz_store")["rules"][
            "review_policy"
        ]["max_age_days"]

    def _dispatch(self, service_id, payload):
        return self.services.dispatch(
            self.runtime, service_id, payload, entity_id="nz_store",
        )["output"]

    def _complete_profile(self, **overrides):
        payload = {
            "as_of": "2026-08-13",
            "entity_type": "limited_company",
            "entity_type_evidence": _evidence("Companies Register status"),
            "tax_residency_evidence": _evidence("advisor residency review"),
            "company_ird_status": "confirmed",
            "company_ird_evidence": _evidence("IRD registration confirmation"),
            "company_income_tax_status": "confirmed",
            "company_income_tax_evidence": _evidence("IR4 filing scope"),
            "gst_status": "not_registered",
            "employer_status": "not_registered",
        }
        payload.update(overrides)
        return payload

    def test_registration_requires_evidence_and_rejects_raw_identifiers(self):
        missing = self._dispatch(
            "tax.nz_limited_company.registration_profile",
            {"as_of": "2026-08-13"},
        )
        self.assertFalse(missing["ready"])
        self.assertEqual(missing["applicability"], "in_scope_limited_company")
        for field in (
            "company_number", "nzbn", "ird_number", "tax_id", "gst_number",
        ):
            with self.subTest(field=field), self.assertRaisesRegex(
                ValueError, "raw company number",
            ):
                self._dispatch(
                    "tax.nz_limited_company.registration_profile",
                    {field: "sensitive"},
                )

    def test_complete_profile_never_determines_residency_gst_or_tax(self):
        output = self._dispatch(
            "tax.nz_limited_company.registration_profile",
            self._complete_profile(),
        )
        self.assertTrue(output["ready"], output)
        self.assertEqual(output["tax_readiness"], "design")
        self.assertFalse(output["raw_company_identifier_collected"])
        self.assertFalse(output["new_zealand_tax_residency_determined"])
        self.assertFalse(output["look_through_company_status_determined"])
        self.assertFalse(output["gst_registration_liability_determined"])
        self.assertFalse(output["gst_supply_classification_performed"])
        self.assertFalse(output["corporation_tax_rate_determined"])
        self.assertFalse(output["provisional_tax_determined"])
        self.assertFalse(output["tax_calculation_performed"])
        self.assertFalse(output["filing_performed"])
        self.assertFalse(output["payment_performed"])
        self.assertFalse(output["external_submission_enabled"])

    def test_outside_scope_and_source_expiry_fail_closed(self):
        outside = self._dispatch(
            "tax.nz_limited_company.registration_profile",
            self._complete_profile(entity_type="look_through_company"),
        )
        self.assertFalse(outside["ready"])
        self.assertEqual(outside["applicability"], "outside_pack_scope")
        boundary = self._complete_profile(
            as_of=(date(2026, 8, 13) + timedelta(days=self.source_max_age_days)).isoformat(),
        )
        self.assertTrue(self._dispatch(
            "tax.nz_limited_company.registration_profile", boundary,
        )["ready"])
        expired = self._complete_profile(
            as_of=(date(2026, 8, 13) + timedelta(days=self.source_max_age_days + 1)).isoformat(),
        )
        result = self._dispatch(
            "tax.nz_limited_company.registration_profile", expired,
        )
        self.assertFalse(result["ready"])
        self.assertEqual(result["source_freshness"]["status"], "source_review_expired")

    def test_evidence_checklist_rejects_missing_duplicate_and_unknown(self):
        missing = self._dispatch(
            "tax.nz_limited_company.evidence_checklist",
            {"as_of": "2026-08-13", "provided_evidence": []},
        )
        self.assertIn("required evidence is missing", missing["blockers"])
        ids = sorted({item for values in NZ_EVIDENCE_BY_RULE.values() for item in values})
        provided = [
            {"evidence_id": evidence_id, **_evidence(f"evidence:{evidence_id}")}
            for evidence_id in ids
        ]
        complete = self._dispatch(
            "tax.nz_limited_company.evidence_checklist",
            {"as_of": "2026-08-13", "provided_evidence": provided},
        )
        self.assertTrue(complete["ready"], complete)
        duplicate = self._dispatch(
            "tax.nz_limited_company.evidence_checklist",
            {"as_of": "2026-08-13", "provided_evidence": [*provided, dict(provided[0])]},
        )
        self.assertEqual(duplicate["duplicate_evidence_ids"], [provided[0]["evidence_id"]])
        unknown = self._dispatch(
            "tax.nz_limited_company.evidence_checklist",
            {"as_of": "2026-08-13", "provided_evidence": [
                *provided, {"evidence_id": "invented", **_evidence("x")},
            ]},
        )
        self.assertEqual(unknown["unknown_evidence_ids"], ["invented"])

    def test_calendar_keeps_all_conditional_dates_manual(self):
        output = self._dispatch(
            "tax.nz_limited_company.build_calendar",
            {"period_year": 2026, "as_of": "2026-08-13"},
        )
        self.assertFalse(output["ready"])
        self.assertTrue(output["limited_company_scope_confirmed"])
        self.assertEqual(output["task_count"], 4)
        self.assertTrue(all(
            task["candidate_due_date"] is None for task in output["tasks"]
        ))
        self.assertTrue(all(task["candidate_only"] for task in output["tasks"]))
        self.assertFalse(output["gst_registration_liability_determined"])
        self.assertFalse(output["provisional_tax_determined"])
        self.assertFalse(output["tax_calculation_performed"])
        self.assertFalse(output["filing_performed"])
        self.assertFalse(output["payment_performed"])

    def test_wrong_entity_and_pack_maturity_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = json.loads(NZ_BOX.read_text(encoding="utf-8"))
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
                    runtime, "tax.nz_limited_company.registration_profile", {},
                    entity_id="cn_entity",
                )
        pack = next(
            item for item in self.runtime.snapshot()["packs"]
            if item["id"] == "jurisdiction.nz_limited_company"
        )
        self.assertEqual(pack["status"], "experimental")
        self.assertEqual(self.runtime.entities.get("nz_store").tax_readiness, "design")


if __name__ == "__main__":
    unittest.main()
