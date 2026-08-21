import unittest
from pathlib import Path

from src.box_runtime import BoxRuntime
from src.default_services import build_default_service_registry
from src.tax_profile_services import EVIDENCE_BY_RULE


ROOT = Path(__file__).resolve().parents[1]


class TaxProfileServicesTests(unittest.TestCase):
    def setUp(self):
        self.runtime = BoxRuntime(ROOT / "examples" / "boxes" / "global_game_studio.json", ROOT / "packs")
        self.registry = build_default_service_registry()

    def test_sg_profile_keeps_gst_review_state_unconfirmed(self):
        result = self.registry.dispatch(
            self.runtime,
            "tax.sg.registration_profile",
            {},
            entity_id="sg_publisher",
        )["output"]
        self.assertFalse(result["ready"])
        self.assertEqual(result["registrations"]["corporate_income_tax"], "confirmed")
        self.assertEqual(result["registrations"]["gst"], "needs_confirmation")
        self.assertFalse(result["determination_performed"])
        self.assertTrue(result["official_sources"][0]["url"].startswith("https://"))

    def test_evidence_checklist_is_source_backed_and_incomplete_by_default(self):
        result = self.registry.dispatch(
            self.runtime,
            "tax.sg.evidence_checklist",
            {},
            entity_id="sg_publisher",
        )["output"]
        self.assertFalse(result["ready"])
        self.assertEqual(len(result["items"]), 4)
        self.assertTrue(all(item["human_review_required"] for item in result["items"]))
        self.assertTrue(all(item["official_sources"] for item in result["items"]))
        self.assertFalse(result["filing_or_tax_calculation_performed"])

    def test_checklist_can_be_completed_with_explicit_evidence_ids(self):
        evidence_ids = {
            evidence_id: "evidence://demo"
            for values in EVIDENCE_BY_RULE.values()
            for evidence_id in values
        }
        result = self.registry.dispatch(
            self.runtime,
            "tax.sg.evidence_checklist",
            {"provided_evidence": evidence_ids},
            entity_id="sg_publisher",
        )["output"]
        self.assertTrue(result["ready"])


if __name__ == "__main__":
    unittest.main()
