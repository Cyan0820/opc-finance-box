from __future__ import annotations

import unittest
from pathlib import Path

from src.box_doctor import diagnose_box
from src.box_runtime import BoxRuntime
from src.tax_pack_lifecycle import (
    TaxPackLifecycleError,
    build_tax_applicability_questionnaire,
    evaluate_tax_rule_lifecycle,
    source_freshness_from_bundle,
)


ROOT = Path(__file__).resolve().parents[1]


class TaxPackLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.runtime = BoxRuntime(
            ROOT / "examples" / "boxes" / "global_game_studio.json",
            ROOT / "packs",
        )

    def test_rule_lifecycle_has_current_review_due_and_expired_states(self):
        current = evaluate_tax_rule_lifecycle(self.runtime, as_of="2026-08-14")
        self.assertEqual(current["counts"], {"current": 2, "review_due": 0, "expired": 0})
        self.assertTrue(current["calendar_release_allowed"])
        self.assertFalse(current["external_filing_release_allowed"])
        self.assertTrue(all(
            item["expires_at"] == "2027-02-09" and item["status"] == "current"
            for item in current["entities"]
        ))

        review_due = evaluate_tax_rule_lifecycle(self.runtime, as_of="2027-01-15")
        self.assertEqual(review_due["counts"]["review_due"], 2)
        self.assertTrue(review_due["calendar_release_allowed"])

        expired = evaluate_tax_rule_lifecycle(self.runtime, as_of="2027-02-10")
        self.assertEqual(expired["counts"]["expired"], 2)
        self.assertFalse(expired["calendar_release_allowed"])
        self.assertTrue(all(
            not item["external_filing_release_allowed"]
            and not item["tax_calculation_performed"]
            and not item["external_actions_performed"]
            for item in expired["entities"]
        ))
        with self.assertRaisesRegex(TaxPackLifecycleError, "cannot predate"):
            evaluate_tax_rule_lifecycle(self.runtime, as_of="2026-08-12")

    def test_source_freshness_uses_pack_policy_instead_of_a_global_constant(self):
        bundle = self.runtime.tax_rules("cn_studio")
        bundle["rules"]["review_policy"] = {
            **bundle["rules"]["review_policy"],
            "max_age_days": 60,
            "warning_days_before_expiry": 10,
        }
        review_due = source_freshness_from_bundle(bundle, "2026-10-02")
        self.assertEqual(review_due["status"], "review_due")
        self.assertEqual(review_due["max_age_days"], 60)
        expired = source_freshness_from_bundle(bundle, "2026-10-13")
        self.assertEqual(expired["status"], "source_review_expired")
        self.assertFalse(expired["calendar_release_allowed"])

    def test_questionnaire_is_pack_scoped_unanswered_and_identifier_free(self):
        questionnaire = build_tax_applicability_questionnaire(self.runtime)
        self.assertEqual(len(questionnaire["entities"]), 2)
        self.assertTrue(questionnaire["template_only"])
        self.assertFalse(questionnaire["raw_tax_identifiers_requested"])
        for entity in questionnaire["entities"]:
            self.assertEqual(entity["question_count"], 5)
            self.assertEqual(entity["unanswered_count"], 5)
            self.assertFalse(entity["tax_applicability_determined"])
            self.assertTrue(entity["authority_scope"])
            self.assertTrue(entity["scope_note"])
            self.assertTrue(all(
                question["answer"] is None
                and question["evidence_references"] == []
                and question["source_ids"]
                and question["review_gate"] in {
                    "tax_registration_confirmation", "tax_advisor_review",
                }
                for question in entity["questions"]
            ))
        def keys(value):
            if isinstance(value, dict):
                return set(value) | set().union(*(keys(item) for item in value.values()))
            if isinstance(value, list):
                return set().union(*(keys(item) for item in value))
            return set()

        self.assertTrue({"answer", "evidence_references"}.issubset(keys(questionnaire)))
        self.assertTrue(keys(questionnaire).isdisjoint({
            "tax_number", "vat_number", "ein", "brn", "rsin", "kvk_number",
            "company_number", "tax_id", "tax_identifier",
        }))

    def test_doctor_warns_on_expiry_without_blocking_internal_demo(self):
        report = diagnose_box(
            self.runtime,
            python_version=(3, 11, 0),
            dependency_probe={"openpyxl": True, "pypdf": True},
            executable_probe={"tesseract": True, "pdftoppm": True},
            as_of="2027-02-10",
        )
        lifecycle = next(
            item for item in report["checks"] if item["check_id"] == "tax.rule_lifecycle"
        )
        self.assertEqual(lifecycle["status"], "warning")
        self.assertEqual(
            {item["status"] for item in lifecycle["details"]["entities"]}, {"expired"}
        )
        self.assertTrue(report["ready"])
        self.assertTrue(report["ready_for_internal_demo"])
        self.assertFalse(report["ready_for_external_filing"])
