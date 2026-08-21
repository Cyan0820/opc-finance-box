from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from src.box_runtime import BoxRuntime
from src.default_services import build_default_service_registry
from src.pack_services import PackServiceError
from src.uk_tax_services import UK_EVIDENCE_BY_RULE


ROOT = Path(__file__).resolve().parents[1]
PACKS = ROOT / "packs"
UK_BOX = ROOT / "examples" / "boxes" / "uk_dtc_shopify_stripe_ltd.json"


def _evidence(reference: str) -> dict[str, str]:
    return {"source_reference": reference, "captured_at": "2026-08-13"}


class UnitedKingdomTaxPackTests(unittest.TestCase):
    def setUp(self):
        self.runtime = BoxRuntime(UK_BOX, PACKS)
        self.services = build_default_service_registry()
        self.source_max_age_days = self.runtime.tax_rules("uk_store")["rules"][
            "review_policy"
        ]["max_age_days"]

    def _dispatch(self, service_id, payload):
        return self.services.dispatch(
            self.runtime, service_id, payload, entity_id="uk_store",
        )["output"]

    def _complete_profile(self, **overrides):
        payload = {
            "as_of": "2026-08-13",
            "entity_type": "private_limited_company",
            "entity_type_evidence": _evidence("Companies House entity reference"),
            "corporation_tax_status": "confirmed",
            "corporation_tax_evidence": _evidence("HMRC service registration reference"),
            "vat_status": "not_registered",
        }
        payload.update(overrides)
        return payload

    def test_registration_requires_evidence_and_never_collects_identifiers(self):
        missing = self._dispatch(
            "tax.uk_limited_company.registration_profile", {"as_of": "2026-08-13"},
        )
        self.assertFalse(missing["ready"])
        self.assertEqual(missing["applicability"], "in_scope_private_limited_company")
        self.assertFalse(missing["corporation_tax_registration"]["raw_utr_collected"])
        self.assertFalse(missing["company_identifier_collected"])
        for field in ("utr", "company_number", "vat_number"):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "raw UTR"):
                self._dispatch(
                    "tax.uk_limited_company.registration_profile", {field: "sensitive"},
                )

    def test_complete_profile_is_officially_sourced_but_not_a_tax_conclusion(self):
        output = self._dispatch(
            "tax.uk_limited_company.registration_profile", self._complete_profile(),
        )
        self.assertTrue(output["ready"], output)
        self.assertEqual(output["tax_readiness"], "design")
        self.assertTrue(all(
            source["url"].startswith("https://www.gov.uk/")
            for source in output["official_sources"]
        ))
        self.assertFalse(output["vat_liability_determined"])
        self.assertFalse(output["corporation_tax_calculated"])
        self.assertFalse(output["filing_performed"])
        self.assertFalse(output["payment_performed"])
        self.assertFalse(output["external_submission_enabled"])

    def test_outside_scope_and_source_expiry_fail_closed(self):
        outside = self._dispatch(
            "tax.uk_limited_company.registration_profile",
            self._complete_profile(entity_type="sole_trader"),
        )
        self.assertFalse(outside["ready"])
        self.assertEqual(outside["applicability"], "outside_pack_scope")
        boundary = self._complete_profile(
            as_of=(date(2026, 8, 13) + timedelta(days=self.source_max_age_days)).isoformat(),
        )
        self.assertTrue(self._dispatch(
            "tax.uk_limited_company.registration_profile", boundary,
        )["ready"])
        expired = self._complete_profile(
            as_of=(date(2026, 8, 13) + timedelta(days=self.source_max_age_days + 1)).isoformat(),
        )
        output = self._dispatch("tax.uk_limited_company.registration_profile", expired)
        self.assertFalse(output["ready"])
        self.assertEqual(output["source_freshness"]["status"], "source_review_expired")

    def test_evidence_checklist_rejects_missing_duplicate_and_unknown(self):
        missing = self._dispatch("tax.uk_limited_company.evidence_checklist", {
            "as_of": "2026-08-13", "provided_evidence": [],
        })
        self.assertIn("required evidence is missing", missing["blockers"])
        ids = sorted({item for values in UK_EVIDENCE_BY_RULE.values() for item in values})
        provided = [
            {"evidence_id": item, **_evidence(f"evidence:{item}")} for item in ids
        ]
        complete = self._dispatch("tax.uk_limited_company.evidence_checklist", {
            "as_of": "2026-08-13", "provided_evidence": provided,
        })
        self.assertTrue(complete["ready"], complete)
        duplicate = self._dispatch("tax.uk_limited_company.evidence_checklist", {
            "as_of": "2026-08-13", "provided_evidence": [*provided, dict(provided[0])],
        })
        self.assertEqual(duplicate["duplicate_evidence_ids"], [provided[0]["evidence_id"]])
        unknown = self._dispatch("tax.uk_limited_company.evidence_checklist", {
            "as_of": "2026-08-13",
            "provided_evidence": [*provided, {"evidence_id": "invented", **_evidence("x")}],
        })
        self.assertEqual(unknown["unknown_evidence_ids"], ["invented"])

    def test_calendar_keeps_payment_and_vat_dates_manual(self):
        output = self._dispatch("tax.uk_limited_company.build_calendar", {
            "period_year": 2026, "as_of": "2026-08-13",
        })
        self.assertFalse(output["ready"])
        self.assertTrue(output["limited_company_scope_confirmed"])
        self.assertEqual(output["task_count"], 4)
        by_rule = {task["rule_id"]: task for task in output["tasks"]}
        self.assertEqual(
            by_rule["uk.corporation_tax.ct600.calendar"]["candidate_due_date"],
            "2027-12-31",
        )
        self.assertEqual(
            by_rule["uk.companies_house.private_accounts.calendar"]["candidate_due_date"],
            "2027-09-30",
        )
        self.assertIsNone(
            by_rule["uk.corporation_tax.payment.calendar"]["candidate_due_date"]
        )
        self.assertIsNone(by_rule["uk.vat.return.calendar"]["candidate_due_date"])
        self.assertFalse(output["corporation_tax_calculated"])
        self.assertFalse(output["payment_performed"])

    def test_wrong_entity_is_rejected_and_pack_remains_design(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = json.loads(UK_BOX.read_text(encoding="utf-8"))
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
                    runtime, "tax.uk_limited_company.registration_profile", {},
                    entity_id="cn_entity",
                )
        pack = next(
            item for item in self.runtime.snapshot()["packs"]
            if item["id"] == "jurisdiction.uk_limited_company"
        )
        self.assertEqual(pack["status"], "experimental")
        self.assertEqual(self.runtime.entities.get("uk_store").tax_readiness, "design")


if __name__ == "__main__":
    unittest.main()
