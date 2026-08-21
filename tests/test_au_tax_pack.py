from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from src.au_tax_services import AU_EVIDENCE_BY_RULE
from src.box_runtime import BoxRuntime
from src.default_services import build_default_service_registry
from src.pack_services import PackServiceError


ROOT = Path(__file__).resolve().parents[1]
PACKS = ROOT / "packs"
AU_BOX = ROOT / "examples" / "boxes" / "au_dtc_shopify_stripe_pty_ltd.json"


def _evidence(reference: str) -> dict[str, str]:
    return {"source_reference": reference, "captured_at": "2026-08-13"}


class AustraliaTaxPackTests(unittest.TestCase):
    def setUp(self):
        self.runtime = BoxRuntime(AU_BOX, PACKS)
        self.services = build_default_service_registry()
        self.source_max_age_days = self.runtime.tax_rules("au_store")["rules"][
            "review_policy"
        ]["max_age_days"]

    def _dispatch(self, service_id, payload):
        return self.services.dispatch(
            self.runtime, service_id, payload, entity_id="au_store",
        )["output"]

    def _complete_profile(self, **overrides):
        payload = {
            "as_of": "2026-08-13",
            "entity_type": "proprietary_company",
            "entity_type_evidence": _evidence("ASIC company extract"),
            "abn_status": "confirmed",
            "abn_evidence": _evidence("ABR registration confirmation"),
            "company_tfn_status": "confirmed",
            "company_tfn_evidence": _evidence("ATO TFN confirmation"),
            "gst_status": "not_registered",
            "payg_withholding_status": "not_registered",
        }
        payload.update(overrides)
        return payload

    def test_registration_requires_evidence_and_rejects_raw_identifiers(self):
        missing = self._dispatch(
            "tax.au_proprietary_company.registration_profile",
            {"as_of": "2026-08-13"},
        )
        self.assertFalse(missing["ready"])
        self.assertEqual(missing["applicability"], "in_scope_proprietary_company")
        self.assertFalse(missing["raw_company_identifier_collected"])
        for field in ("acn", "abn", "tfn", "tax_id"):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "raw ACN"):
                self._dispatch(
                    "tax.au_proprietary_company.registration_profile",
                    {field: "sensitive"},
                )

    def test_complete_profile_is_sourced_but_not_tax_or_gst_conclusion(self):
        output = self._dispatch(
            "tax.au_proprietary_company.registration_profile",
            self._complete_profile(),
        )
        self.assertTrue(output["ready"], output)
        self.assertEqual(output["tax_readiness"], "design")
        self.assertTrue(all(
            source["url"].startswith("https://") for source in output["official_sources"]
        ))
        self.assertFalse(output["gst_registration_liability_determined"])
        self.assertFalse(output["gst_supply_classification_performed"])
        self.assertFalse(output["company_tax_rate_determined"])
        self.assertFalse(output["tax_calculation_performed"])
        self.assertFalse(output["filing_performed"])
        self.assertFalse(output["payment_performed"])
        self.assertFalse(output["external_submission_enabled"])

    def test_outside_scope_and_expired_sources_fail_closed(self):
        outside = self._dispatch(
            "tax.au_proprietary_company.registration_profile",
            self._complete_profile(entity_type="sole_trader"),
        )
        self.assertFalse(outside["ready"])
        self.assertEqual(outside["applicability"], "outside_pack_scope")
        boundary = self._complete_profile(
            as_of=(date(2026, 8, 13) + timedelta(days=self.source_max_age_days)).isoformat(),
        )
        self.assertTrue(self._dispatch(
            "tax.au_proprietary_company.registration_profile", boundary,
        )["ready"])
        expired = self._complete_profile(
            as_of=(date(2026, 8, 13) + timedelta(days=self.source_max_age_days + 1)).isoformat(),
        )
        result = self._dispatch(
            "tax.au_proprietary_company.registration_profile", expired,
        )
        self.assertFalse(result["ready"])
        self.assertEqual(result["source_freshness"]["status"], "source_review_expired")

    def test_evidence_checklist_rejects_missing_duplicate_and_unknown(self):
        missing = self._dispatch(
            "tax.au_proprietary_company.evidence_checklist",
            {"as_of": "2026-08-13", "provided_evidence": []},
        )
        self.assertIn("required evidence is missing", missing["blockers"])
        ids = sorted({item for values in AU_EVIDENCE_BY_RULE.values() for item in values})
        provided = [
            {"evidence_id": evidence_id, **_evidence(f"evidence:{evidence_id}")}
            for evidence_id in ids
        ]
        complete = self._dispatch(
            "tax.au_proprietary_company.evidence_checklist",
            {"as_of": "2026-08-13", "provided_evidence": provided},
        )
        self.assertTrue(complete["ready"], complete)
        duplicate = self._dispatch(
            "tax.au_proprietary_company.evidence_checklist",
            {"as_of": "2026-08-13", "provided_evidence": [*provided, dict(provided[0])]},
        )
        self.assertEqual(duplicate["duplicate_evidence_ids"], [provided[0]["evidence_id"]])
        unknown = self._dispatch(
            "tax.au_proprietary_company.evidence_checklist",
            {"as_of": "2026-08-13", "provided_evidence": [
                *provided, {"evidence_id": "invented", **_evidence("x")},
            ]},
        )
        self.assertEqual(unknown["unknown_evidence_ids"], ["invented"])

    def test_calendar_keeps_ato_dates_manual_and_uses_explicit_asic_anchor(self):
        output = self._dispatch("tax.au_proprietary_company.build_calendar", {
            "period_year": 2026,
            "as_of": "2026-08-13",
            "anchors": {"asic_annual_review_date": "2026-09-15"},
        })
        self.assertFalse(output["ready"])
        self.assertTrue(output["proprietary_company_scope_confirmed"])
        self.assertEqual(output["task_count"], 3)
        by_rule = {task["rule_id"]: task for task in output["tasks"]}
        self.assertIsNone(
            by_rule["au.company_tax.return_and_payment.calendar"]["candidate_due_date"]
        )
        self.assertIsNone(by_rule["au.gst.bas.calendar"]["candidate_due_date"])
        self.assertEqual(
            by_rule["au.asic.annual_review.calendar"]["candidate_due_date"],
            "2026-11-15",
        )
        self.assertFalse(output["company_tax_rate_determined"])
        self.assertFalse(output["tax_calculation_performed"])
        self.assertFalse(output["payment_performed"])

    def test_missing_asic_anchor_and_wrong_entity_fail_closed(self):
        calendar = self._dispatch("tax.au_proprietary_company.build_calendar", {
            "period_year": 2026, "as_of": "2026-08-13",
        })
        by_rule = {task["rule_id"]: task for task in calendar["tasks"]}
        asic = by_rule["au.asic.annual_review.calendar"]
        self.assertEqual(asic["status"], "needs_configuration")
        self.assertEqual(asic["missing_configuration"], ["asic_annual_review_date"])
        with tempfile.TemporaryDirectory() as temp_dir:
            config = json.loads(AU_BOX.read_text(encoding="utf-8"))
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
                    runtime, "tax.au_proprietary_company.registration_profile", {},
                    entity_id="cn_entity",
                )
        pack = next(
            item for item in self.runtime.snapshot()["packs"]
            if item["id"] == "jurisdiction.au_proprietary_company"
        )
        self.assertEqual(pack["status"], "experimental")
        self.assertEqual(self.runtime.entities.get("au_store").tax_readiness, "design")


if __name__ == "__main__":
    unittest.main()
