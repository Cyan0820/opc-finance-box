from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from src.box_runtime import BoxRuntime
from src.default_services import build_default_service_registry
from src.hk_tax_services import HK_EVIDENCE_BY_RULE
from src.pack_services import PackServiceError


ROOT = Path(__file__).resolve().parents[1]
PACKS = ROOT / "packs"
HK_BOX = ROOT / "examples" / "boxes" / "hk_dtc_shopify_stripe_corporation.json"


def _evidence(reference: str) -> dict[str, str]:
    return {"source_reference": reference, "captured_at": "2026-08-13"}


class HongKongTaxPackTests(unittest.TestCase):
    def setUp(self):
        self.runtime = BoxRuntime(HK_BOX, PACKS)
        self.services = build_default_service_registry()
        self.source_max_age_days = self.runtime.tax_rules("hk_store")["rules"][
            "review_policy"
        ]["max_age_days"]

    def _dispatch(self, service_id, payload):
        return self.services.dispatch(
            self.runtime, service_id, payload, entity_id="hk_store",
        )["output"]

    def _complete_profile(self, **overrides):
        payload = {
            "as_of": "2026-08-13",
            "entity_type": "hong_kong_corporation",
            "entity_type_evidence": _evidence("corporation-evidence"),
            "brn_status": "confirmed",
            "brn_evidence": _evidence("BR certificate reference; identifier withheld"),
            "profits_tax_status": "confirmed",
            "profits_tax_evidence": _evidence("BIR51 scope evidence"),
        }
        payload.update(overrides)
        return payload

    def test_registration_profile_requires_evidence_and_never_collects_raw_brn(self):
        missing = self._dispatch("tax.hk.registration_profile", {"as_of": "2026-08-13"})
        self.assertFalse(missing["ready"])
        self.assertEqual(missing["applicability"], "in_scope_hong_kong_corporation")
        self.assertEqual(missing["business_registration"]["review_status"], "needs_evidence")
        self.assertFalse(missing["business_registration"]["raw_identifier_collected"])
        with self.assertRaisesRegex(ValueError, "raw BRN"):
            self._dispatch("tax.hk.registration_profile", {"brn": "12345678"})

    def test_complete_profile_is_source_backed_but_not_a_tax_conclusion(self):
        output = self._dispatch("tax.hk.registration_profile", self._complete_profile())
        self.assertTrue(output["ready"], output)
        self.assertEqual(output["tax_readiness"], "design")
        self.assertTrue(all(
            source["url"].startswith("https://www.ird.gov.hk/")
            for source in output["official_sources"]
        ))
        self.assertFalse(output["territorial_source_determination_performed"])
        self.assertFalse(output["two_tier_eligibility_determined"])
        self.assertFalse(output["tax_calculation_performed"])
        self.assertFalse(output["filing_performed"])
        self.assertFalse(output["external_submission_enabled"])

    def test_non_corporation_is_explicitly_outside_scope(self):
        output = self._dispatch("tax.hk.registration_profile", self._complete_profile(
            entity_type="unincorporated_business",
        ))
        self.assertFalse(output["ready"])
        self.assertEqual(output["applicability"], "outside_pack_scope")

    def test_source_freshness_boundary_and_expiry_fail_closed(self):
        verified = date(2026, 8, 13)
        boundary = self._complete_profile(
            as_of=(verified + timedelta(days=self.source_max_age_days)).isoformat(),
        )
        self.assertTrue(self._dispatch("tax.hk.registration_profile", boundary)["ready"])
        expired = self._complete_profile(
            as_of=(verified + timedelta(days=self.source_max_age_days + 1)).isoformat(),
        )
        output = self._dispatch("tax.hk.registration_profile", expired)
        self.assertFalse(output["ready"])
        self.assertEqual(output["source_freshness"]["status"], "source_review_expired")

    def test_evidence_checklist_handles_missing_complete_duplicate_and_unknown(self):
        missing = self._dispatch("tax.hk.evidence_checklist", {
            "as_of": "2026-08-13", "provided_evidence": [],
        })
        self.assertFalse(missing["ready"])
        self.assertIn("required evidence is missing", missing["blockers"])
        ids = sorted({item for values in HK_EVIDENCE_BY_RULE.values() for item in values})
        provided = [
            {"evidence_id": item, **_evidence(f"evidence:{item}")} for item in ids
        ]
        complete = self._dispatch("tax.hk.evidence_checklist", {
            "as_of": "2026-08-13", "provided_evidence": provided,
        })
        self.assertTrue(complete["ready"], complete)
        self.assertFalse(complete["territorial_source_determination_performed"])
        self.assertFalse(complete["filing_performed"])
        duplicate = self._dispatch("tax.hk.evidence_checklist", {
            "as_of": "2026-08-13", "provided_evidence": [*provided, dict(provided[0])],
        })
        self.assertEqual(duplicate["duplicate_evidence_ids"], [provided[0]["evidence_id"]])
        unknown = self._dispatch("tax.hk.evidence_checklist", {
            "as_of": "2026-08-13",
            "provided_evidence": [*provided, {"evidence_id": "invented", **_evidence("x")}],
        })
        self.assertEqual(unknown["unknown_evidence_ids"], ["invented"])

    def test_calendar_requires_manual_configuration_and_never_invents_dates(self):
        output = self._dispatch("tax.hk.build_calendar", {
            "period_year": 2025, "as_of": "2026-08-13",
        })
        self.assertFalse(output["ready"])
        self.assertTrue(output["corporation_scope_confirmed"])
        self.assertEqual(output["task_count"], 2)
        self.assertTrue(all(task["status"] == "needs_configuration" for task in output["tasks"]))
        self.assertTrue(all(task["candidate_due_date"] is None for task in output["tasks"]))
        self.assertFalse(output["tax_calculation_performed"])
        self.assertFalse(output["filing_performed"])

    def test_wrong_entity_is_rejected_and_pack_remains_design(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = json.loads(HK_BOX.read_text(encoding="utf-8"))
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
                    runtime, "tax.hk.registration_profile", {}, entity_id="cn_entity",
                )
        snapshot = self.runtime.snapshot()
        pack = next(item for item in snapshot["packs"] if item["id"] == "jurisdiction.hk")
        self.assertEqual(pack["status"], "experimental")
        self.assertEqual(self.runtime.entities.get("hk_store").tax_readiness, "design")


if __name__ == "__main__":
    unittest.main()
