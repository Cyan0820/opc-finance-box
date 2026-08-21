import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from src.box_config import BoxConfigError, load_jurisdiction_rules
from src.box_runtime import BoxRuntime
from src.default_services import build_default_service_registry
from src.pack_services import PackServiceError
from src.tax_calendar import TaxCalendarError, add_months, build_tax_calendar


ROOT = Path(__file__).resolve().parents[1]
PACKS = ROOT / "packs"
GAME_BOX = ROOT / "examples" / "boxes" / "global_game_studio.json"


class TaxCalendarTests(unittest.TestCase):
    def setUp(self):
        self.runtime = BoxRuntime(GAME_BOX, PACKS)

    def test_add_months_preserves_month_end(self):
        self.assertEqual(add_months(date(2027, 1, 31), 1), date(2027, 2, 28))
        self.assertEqual(add_months(date(2028, 1, 31), 1), date(2028, 2, 29))
        self.assertEqual(add_months(date(2026, 12, 31), 3), date(2027, 3, 31))

    def test_sg_cit_deadlines_derive_from_entity_fye(self):
        result = build_tax_calendar(
            self.runtime,
            "sg_publisher",
            period_year=2026,
            as_of="2026-08-13",
        )
        due_by_rule = {
            task["rule_id"]: task["candidate_due_date"]
            for task in result["tasks"]
            if task["candidate_due_date"]
        }
        self.assertEqual(due_by_rule["sg.cit.calendar.eci"], "2027-03-31")
        self.assertEqual(due_by_rule["sg.cit.calendar.annual_return"], "2027-11-30")
        self.assertFalse(result["ready"])

    def test_sg_gst_review_registration_does_not_create_a_deadline(self):
        result = build_tax_calendar(
            self.runtime,
            "sg_publisher",
            period_year=2026,
            anchors={"gst_period_end": ["2026-09-30"]},
            as_of="2026-08-13",
        )
        gst_task = next(task for task in result["tasks"] if task["rule_id"].startswith("sg.gst.calendar"))
        self.assertEqual(gst_task["status"], "needs_registration_confirmation")
        self.assertIsNone(gst_task["candidate_due_date"])

    def test_confirmed_gst_entity_gets_one_candidate_per_period(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "box.json"
            config = json.loads(GAME_BOX.read_text(encoding="utf-8"))
            config["entities"][1]["tax_registrations"] = ["corporate_income_tax", "gst_registered"]
            config_path.write_text(json.dumps(config), encoding="utf-8")
            runtime = BoxRuntime(config_path, PACKS)
            result = build_tax_calendar(
                runtime,
                "sg_publisher",
                period_year=2026,
                anchors={"gst_period_end": ["2026-09-30", "2026-12-31"]},
                as_of="2026-08-13",
            )
        gst_tasks = [task for task in result["tasks"] if task["rule_id"].startswith("sg.gst.calendar")]
        self.assertEqual(
            [task["candidate_due_date"] for task in gst_tasks],
            ["2026-10-31", "2027-01-31"],
        )
        self.assertTrue(all(task["official_sources"][0]["url"].startswith("https://") for task in gst_tasks))

    def test_cn_calendar_exposes_configuration_without_invented_dates(self):
        result = build_tax_calendar(
            self.runtime,
            "cn_studio",
            period_year=2026,
            as_of="2026-08-13",
        )
        self.assertFalse(result["ready"])
        self.assertEqual(result["task_count"], 2)
        self.assertTrue(all(task["status"] == "needs_configuration" for task in result["tasks"]))
        self.assertTrue(all(task["candidate_due_date"] is None for task in result["tasks"]))
        self.assertTrue(all(task["human_review_required"] for task in result["tasks"]))

    def test_duplicate_anchor_dates_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "box.json"
            config = json.loads(GAME_BOX.read_text(encoding="utf-8"))
            config["entities"][1]["tax_registrations"] = ["gst_registered"]
            config_path.write_text(json.dumps(config), encoding="utf-8")
            runtime = BoxRuntime(config_path, PACKS)
            with self.assertRaises(TaxCalendarError):
                build_tax_calendar(
                    runtime,
                    "sg_publisher",
                    anchors={"gst_period_end": ["2026-09-30", "2026-09-30"]},
                )

    def test_calendar_rule_without_schedule_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "rules.json"
            path.write_text(json.dumps({
                "schema_version": 1,
                "jurisdiction": "SG",
                "verified_at": "2026-08-13",
                "sources": [{
                    "id": "official",
                    "authority": "Authority",
                    "title": "Official source",
                    "url": "https://example.gov/rule",
                }],
                "rules": [{
                    "id": "sg.bad.calendar",
                    "source_ids": ["official"],
                    "automation_level": "calendar",
                    "human_review_required": True,
                    "review_gate": "tax_advisor_review",
                }],
            }), encoding="utf-8")
            with self.assertRaises(BoxConfigError):
                load_jurisdiction_rules(path, "SG")

    def test_days_after_date_requires_positive_integer_days(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "rules.json"
            path.write_text(json.dumps({
                "schema_version": 1,
                "jurisdiction": "CA",
                "verified_at": "2026-08-13",
                "review_policy": {
                    "max_age_days": 180,
                    "warning_days_before_expiry": 30,
                    "expiry_effect": "block_external_filing_and_calendar_release",
                    "reverification_triggers": ["authority_source_change"],
                },
                "applicability_review_policy": {
                    "max_age_days": 365,
                    "warning_days_before_expiry": 30,
                    "expiry_effect": "block_calendar_and_external_filing_release",
                    "reverification_triggers": [
                        "pack_upgrade", "entity_applicability_change",
                        "tax_registration_change",
                    ],
                },
                "sources": [{
                    "id": "official",
                    "authority": "Authority",
                    "title": "Official source",
                    "url": "https://example.gov/rule",
                }],
                "rules": [{
                    "id": "ca.bad.calendar",
                    "source_ids": ["official"],
                    "automation_level": "calendar",
                    "human_review_required": True,
                    "review_gate": "tax_advisor_review",
                    "schedule": {
                        "kind": "days_after_date",
                        "anchor": "anniversary_date",
                        "days": 0,
                    },
                }],
            }), encoding="utf-8")
            with self.assertRaisesRegex(BoxConfigError, "positive integer days"):
                load_jurisdiction_rules(path, "CA")

    def test_tax_calendar_services_are_filtered_by_selected_pack(self):
        services = build_default_service_registry().catalog(self.runtime)
        service_ids = {service["service_id"] for service in services}
        self.assertIn("tax.cn.build_calendar", service_ids)
        self.assertIn("tax.sg.build_calendar", service_ids)

    def test_sg_service_dispatch_keeps_statutory_entity_scope(self):
        result = build_default_service_registry().dispatch(
            self.runtime,
            "tax.sg.build_calendar",
            {"period_year": 2026, "as_of": "2026-08-13"},
            entity_id="sg_publisher",
        )
        self.assertEqual(result["service"]["entity_ids"], ["sg_publisher"])
        self.assertEqual(result["output"]["entity"]["jurisdiction"], "SG")

    def test_jurisdiction_service_cannot_run_for_entity_using_another_tax_pack(self):
        with self.assertRaisesRegex(PackServiceError, "but entity cn_studio uses jurisdiction.cn_mainland"):
            build_default_service_registry().dispatch(
                self.runtime,
                "tax.sg.build_calendar",
                {"period_year": 2026},
                entity_id="cn_studio",
            )


if __name__ == "__main__":
    unittest.main()
