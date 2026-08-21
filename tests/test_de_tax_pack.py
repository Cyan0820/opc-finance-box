from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from src.box_runtime import BoxRuntime
from src.de_tax_services import DE_EVIDENCE_BY_RULE
from src.default_services import build_default_service_registry
from src.pack_services import PackServiceError


ROOT = Path(__file__).resolve().parents[1]
PACKS = ROOT / "packs"
DE_BOX = ROOT / "examples" / "boxes" / "de_dtc_shopify_stripe_gmbh.json"


def _evidence(reference: str) -> dict[str, str]:
    return {"source_reference": reference, "captured_at": "2026-08-14"}


class GermanyTaxPackTests(unittest.TestCase):
    def setUp(self):
        self.runtime = BoxRuntime(DE_BOX, PACKS)
        self.services = build_default_service_registry()
        self.source_max_age_days = self.runtime.tax_rules("de_store")["rules"][
            "review_policy"
        ]["max_age_days"]

    def _dispatch(self, service_id, payload):
        return self.services.dispatch(
            self.runtime, service_id, payload, entity_id="de_store",
        )["output"]

    def _complete_profile(self, **overrides):
        payload = {
            "as_of": "2026-08-14",
            "entity_type": "limited_liability_company_gmbh",
            "entity_type_evidence": _evidence("Commercial Register GmbH status"),
            "tax_residency_evidence": _evidence("German residency advisor review"),
            "commercial_register_status": "confirmed",
            "commercial_register_evidence": _evidence("Commercial Register status"),
            "corporation_tax_status": "confirmed",
            "corporation_tax_evidence": _evidence("Corporation tax registration"),
            "trade_tax_status": "confirmed",
            "trade_tax_evidence": _evidence("Trade tax registration"),
            "vat_status": "not_registered",
            "payroll_status": "not_registered",
        }
        payload.update(overrides)
        return payload

    def test_registration_requires_evidence_and_rejects_raw_identifiers(self):
        missing = self._dispatch(
            "tax.de_limited_liability_company.registration_profile",
            {"as_of": "2026-08-14"},
        )
        self.assertFalse(missing["ready"])
        self.assertEqual(
            missing["applicability"],
            "in_scope_limited_liability_company_gmbh",
        )
        for field in (
            "commercial_register_number", "handelsregisternummer",
            "company_number", "tax_number", "steuernummer", "tax_id",
            "vat_number", "ust_idnr", "ust_id_nr",
            "business_identification_number", "wirtschafts_id",
        ):
            with self.subTest(field=field), self.assertRaisesRegex(
                ValueError, "raw Commercial Register number",
            ):
                self._dispatch(
                    "tax.de_limited_liability_company.registration_profile",
                    {field: "sensitive"},
                )

    def test_complete_profile_is_evidence_not_tax_conclusion(self):
        output = self._dispatch(
            "tax.de_limited_liability_company.registration_profile",
            self._complete_profile(),
        )
        self.assertTrue(output["ready"], output)
        self.assertEqual(output["tax_readiness"], "design")
        self.assertFalse(output["raw_company_identifier_collected"])
        for field in (
            "german_tax_residency_determined", "corporation_tax_rate_determined",
            "solidarity_surcharge_determined", "trade_tax_base_determined",
            "municipal_trade_tax_rate_determined",
            "vat_registration_liability_determined",
            "vat_supply_classification_performed", "oss_or_ioss_scheme_determined",
            "payroll_tax_determined", "dividend_tax_determined",
            "tax_calculation_performed", "filing_performed", "payment_performed",
            "external_submission_enabled",
        ):
            self.assertFalse(output[field], field)

    def test_outside_scope_and_source_expiry_fail_closed(self):
        for entity_type in (
            "entrepreneurial_company_ug", "stock_corporation_ag",
            "gmbh_and_co_kg", "branch", "partnership", "sole_trader",
        ):
            with self.subTest(entity_type=entity_type):
                outside = self._dispatch(
                    "tax.de_limited_liability_company.registration_profile",
                    self._complete_profile(entity_type=entity_type),
                )
                self.assertFalse(outside["ready"])
                self.assertEqual(outside["applicability"], "outside_pack_scope")
        boundary = self._complete_profile(
            as_of=(date(2026, 8, 14) + timedelta(days=self.source_max_age_days)).isoformat(),
        )
        self.assertTrue(self._dispatch(
            "tax.de_limited_liability_company.registration_profile", boundary,
        )["ready"])
        expired = self._complete_profile(
            as_of=(date(2026, 8, 14) + timedelta(days=self.source_max_age_days + 1)).isoformat(),
        )
        result = self._dispatch(
            "tax.de_limited_liability_company.registration_profile", expired,
        )
        self.assertFalse(result["ready"])
        self.assertEqual(result["source_freshness"]["status"], "source_review_expired")

    def test_evidence_checklist_rejects_missing_duplicate_and_unknown(self):
        missing = self._dispatch(
            "tax.de_limited_liability_company.evidence_checklist",
            {"as_of": "2026-08-14", "provided_evidence": []},
        )
        self.assertIn("required evidence is missing", missing["blockers"])
        ids = sorted({item for values in DE_EVIDENCE_BY_RULE.values() for item in values})
        provided = [
            {"evidence_id": item, **_evidence(f"evidence:{item}")} for item in ids
        ]
        complete = self._dispatch(
            "tax.de_limited_liability_company.evidence_checklist",
            {"as_of": "2026-08-14", "provided_evidence": provided},
        )
        self.assertTrue(complete["ready"], complete)
        duplicate = self._dispatch(
            "tax.de_limited_liability_company.evidence_checklist",
            {"as_of": "2026-08-14", "provided_evidence": [*provided, dict(provided[0])]},
        )
        self.assertEqual(duplicate["duplicate_evidence_ids"], [provided[0]["evidence_id"]])
        unknown = self._dispatch(
            "tax.de_limited_liability_company.evidence_checklist",
            {"as_of": "2026-08-14", "provided_evidence": [
                *provided, {"evidence_id": "invented", **_evidence("x")},
            ]},
        )
        self.assertEqual(unknown["unknown_evidence_ids"], ["invented"])

    def test_calendar_keeps_all_four_dates_manual(self):
        output = self._dispatch(
            "tax.de_limited_liability_company.build_calendar",
            {"period_year": 2026, "as_of": "2026-08-14"},
        )
        self.assertFalse(output["ready"])
        self.assertTrue(output["limited_liability_company_gmbh_scope_confirmed"])
        self.assertEqual(output["task_count"], 4)
        self.assertEqual(
            {task["rule_id"] for task in output["tasks"]},
            {
                "de.corporation_tax.return.calendar",
                "de.trade_tax.return.calendar",
                "de.vat.advance_return_payment.calendar",
                "de.company_register.financial_statements.calendar",
            },
        )
        self.assertTrue(all(
            task["candidate_due_date"] is None
            and task["candidate_only"]
            and not task["filing_completed"]
            for task in output["tasks"]
        ))
        self.assertFalse(output["tax_calculation_performed"])
        self.assertFalse(output["filing_performed"])

    def test_wrong_entity_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = json.loads(DE_BOX.read_text(encoding="utf-8"))
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
                    "tax.de_limited_liability_company.registration_profile",
                    {},
                    entity_id="cn_entity",
                )


if __name__ == "__main__":
    unittest.main()
