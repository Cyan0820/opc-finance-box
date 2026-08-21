from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from src.box_runtime import BoxRuntime
from src.ca_tax_services import CA_EVIDENCE_BY_RULE
from src.default_services import build_default_service_registry
from src.pack_services import PackServiceError


ROOT = Path(__file__).resolve().parents[1]
PACKS = ROOT / "packs"
CA_BOX = ROOT / "examples" / "boxes" / "ca_dtc_shopify_stripe_federal_corporation.json"


def _evidence(reference: str) -> dict[str, str]:
    return {"source_reference": reference, "captured_at": "2026-08-13"}


class CanadaTaxPackTests(unittest.TestCase):
    def setUp(self):
        self.runtime = BoxRuntime(CA_BOX, PACKS)
        self.services = build_default_service_registry()
        self.source_max_age_days = self.runtime.tax_rules("ca_store")["rules"][
            "review_policy"
        ]["max_age_days"]

    def _dispatch(self, service_id, payload):
        return self.services.dispatch(
            self.runtime, service_id, payload, entity_id="ca_store",
        )["output"]

    def _complete_profile(self, **overrides):
        payload = {
            "as_of": "2026-08-13",
            "entity_type": "federal_corporation",
            "entity_type_evidence": _evidence("Corporations Canada status"),
            "tax_residency_evidence": _evidence("advisor residency review"),
            "business_number_status": "confirmed",
            "business_number_evidence": _evidence("CRA BN confirmation"),
            "corporation_income_tax_status": "confirmed",
            "corporation_income_tax_evidence": _evidence("CRA RC account confirmation"),
            "gst_hst_status": "not_registered",
            "payroll_status": "not_registered",
        }
        payload.update(overrides)
        return payload

    def test_registration_requires_entity_residency_and_account_evidence(self):
        missing = self._dispatch(
            "tax.ca_federal_corporation.registration_profile",
            {"as_of": "2026-08-13"},
        )
        self.assertFalse(missing["ready"])
        self.assertEqual(
            missing["applicability"], "in_scope_federal_corporation",
        )
        self.assertFalse(missing["raw_company_identifier_collected"])
        for field in (
            "corporation_number", "business_number", "bn", "tax_id",
            "program_account", "gst_hst_number",
        ):
            with self.subTest(field=field), self.assertRaisesRegex(
                ValueError, "raw corporation number",
            ):
                self._dispatch(
                    "tax.ca_federal_corporation.registration_profile",
                    {field: "sensitive"},
                )

    def test_complete_profile_is_evidence_not_ccpc_or_tax_conclusion(self):
        output = self._dispatch(
            "tax.ca_federal_corporation.registration_profile",
            self._complete_profile(),
        )
        self.assertTrue(output["ready"], output)
        self.assertEqual(output["tax_readiness"], "design")
        self.assertTrue(all(
            source["url"].startswith("https://")
            for source in output["official_sources"]
        ))
        self.assertFalse(output["canadian_tax_residency_determined"])
        self.assertFalse(output["ccpc_status_determined"])
        self.assertFalse(output["small_business_deduction_eligibility_determined"])
        self.assertFalse(output["gst_hst_registration_liability_determined"])
        self.assertFalse(output["gst_hst_supply_classification_performed"])
        self.assertFalse(output["corporation_tax_rate_determined"])
        self.assertFalse(output["tax_calculation_performed"])
        self.assertFalse(output["filing_performed"])
        self.assertFalse(output["payment_performed"])
        self.assertFalse(output["external_submission_enabled"])

    def test_outside_scope_and_expired_sources_fail_closed(self):
        outside = self._dispatch(
            "tax.ca_federal_corporation.registration_profile",
            self._complete_profile(entity_type="provincial_corporation"),
        )
        self.assertFalse(outside["ready"])
        self.assertEqual(outside["applicability"], "outside_pack_scope")
        boundary = self._complete_profile(
            as_of=(
                date(2026, 8, 13) + timedelta(days=self.source_max_age_days)
            ).isoformat(),
        )
        self.assertTrue(self._dispatch(
            "tax.ca_federal_corporation.registration_profile", boundary,
        )["ready"])
        expired = self._complete_profile(
            as_of=(
                date(2026, 8, 13) + timedelta(days=self.source_max_age_days + 1)
            ).isoformat(),
        )
        result = self._dispatch(
            "tax.ca_federal_corporation.registration_profile", expired,
        )
        self.assertFalse(result["ready"])
        self.assertEqual(
            result["source_freshness"]["status"], "source_review_expired",
        )

    def test_evidence_checklist_rejects_missing_duplicate_and_unknown(self):
        missing = self._dispatch(
            "tax.ca_federal_corporation.evidence_checklist",
            {"as_of": "2026-08-13", "provided_evidence": []},
        )
        self.assertIn("required evidence is missing", missing["blockers"])
        ids = sorted({
            item for values in CA_EVIDENCE_BY_RULE.values() for item in values
        })
        provided = [
            {"evidence_id": evidence_id, **_evidence(f"evidence:{evidence_id}")}
            for evidence_id in ids
        ]
        complete = self._dispatch(
            "tax.ca_federal_corporation.evidence_checklist",
            {"as_of": "2026-08-13", "provided_evidence": provided},
        )
        self.assertTrue(complete["ready"], complete)
        duplicate = self._dispatch(
            "tax.ca_federal_corporation.evidence_checklist",
            {
                "as_of": "2026-08-13",
                "provided_evidence": [*provided, dict(provided[0])],
            },
        )
        self.assertEqual(
            duplicate["duplicate_evidence_ids"], [provided[0]["evidence_id"]],
        )
        unknown = self._dispatch(
            "tax.ca_federal_corporation.evidence_checklist",
            {
                "as_of": "2026-08-13",
                "provided_evidence": [
                    *provided,
                    {"evidence_id": "invented", **_evidence("x")},
                ],
            },
        )
        self.assertEqual(unknown["unknown_evidence_ids"], ["invented"])

    def test_calendar_calculates_t2_and_explicit_annual_return_anchor_only(self):
        output = self._dispatch(
            "tax.ca_federal_corporation.build_calendar",
            {
                "period_year": 2026,
                "as_of": "2026-08-13",
                "anchors": {
                    "federal_corporation_anniversary_date": "2026-09-15",
                },
            },
        )
        self.assertFalse(output["ready"])
        self.assertTrue(output["federal_corporation_scope_confirmed"])
        self.assertEqual(output["task_count"], 4)
        by_rule = {task["rule_id"]: task for task in output["tasks"]}
        self.assertEqual(
            by_rule["ca.t2.return.calendar"]["candidate_due_date"],
            "2027-06-30",
        )
        self.assertIsNone(
            by_rule["ca.corporation_tax.balance.calendar"]["candidate_due_date"]
        )
        self.assertIsNone(
            by_rule["ca.gst_hst.return_payment.calendar"]["candidate_due_date"]
        )
        self.assertEqual(
            by_rule["ca.corporations_canada.annual_return.calendar"]
            ["candidate_due_date"],
            "2026-11-14",
        )
        self.assertFalse(output["ccpc_status_determined"])
        self.assertFalse(output["tax_calculation_performed"])
        self.assertFalse(output["payment_performed"])
        self.assertFalse(output["filing_performed"])

    def test_month_end_semantics_and_wrong_entity_fail_closed(self):
        config = json.loads(CA_BOX.read_text(encoding="utf-8"))
        config["entities"][0]["fiscal_year_end"] = "08-31"
        with tempfile.TemporaryDirectory() as temp_dir:
            ca_path = Path(temp_dir) / "ca.json"
            ca_path.write_text(json.dumps(config), encoding="utf-8")
            runtime = BoxRuntime(ca_path, PACKS)
            output = self.services.dispatch(
                runtime,
                "tax.ca_federal_corporation.build_calendar",
                {"period_year": 2026, "as_of": "2026-08-13"},
                entity_id="ca_store",
            )["output"]
            by_rule = {task["rule_id"]: task for task in output["tasks"]}
            self.assertEqual(
                by_rule["ca.t2.return.calendar"]["candidate_due_date"],
                "2027-02-28",
            )

            config["features"].append("feature.multi_entity")
            config["connector_bindings"] = [{
                "connector_pack": pack_id,
                "entity_ids": [config["entities"][0]["id"]],
            } for pack_id in config["connectors"]]
            config["entities"].append({
                "id": "cn_entity",
                "name": "CN Entity",
                "jurisdiction": "CN",
                "functional_currency": "CNY",
                "accounting_basis": "PRC_GAAP",
                "fiscal_year_end": "12-31",
                "tax_pack": "jurisdiction.cn_mainland",
                "tax_registrations": ["corporate_income_tax"],
            })
            ca_path.write_text(json.dumps(config), encoding="utf-8")
            runtime = BoxRuntime(ca_path, PACKS)
            with self.assertRaisesRegex(
                PackServiceError, "uses jurisdiction.cn_mainland",
            ):
                self.services.dispatch(
                    runtime,
                    "tax.ca_federal_corporation.registration_profile",
                    {},
                    entity_id="cn_entity",
                )
        pack = next(
            item for item in self.runtime.snapshot()["packs"]
            if item["id"] == "jurisdiction.ca_federal_corporation"
        )
        self.assertEqual(pack["status"], "experimental")
        self.assertEqual(self.runtime.entities.get("ca_store").tax_readiness, "design")


if __name__ == "__main__":
    unittest.main()
