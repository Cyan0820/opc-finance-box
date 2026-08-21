import unittest
from pathlib import Path

from src.box_config import load_pack_catalog
from src.pack_audit import audit_pack_catalog


ROOT = Path(__file__).resolve().parents[1]


class PackAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = audit_pack_catalog(load_pack_catalog(ROOT / "packs"))

    def test_all_registered_review_gates_are_declared_by_pack_dependencies(self):
        self.assertTrue(self.result["contract_valid"])
        self.assertFalse(self.result["contract_failures"])

    def test_audit_does_not_confuse_declarations_with_implementations(self):
        self.assertGreater(self.result["coverage_counts"]["executable"], 0)
        self.assertEqual(self.result["coverage_counts"]["declared_only"], 0)
        self.assertTrue(self.result["complete_implementation"])
        self.assertFalse(self.result["stable_release_ready"])

    def test_runtime_guardrails_count_as_code_providers(self):
        core = next(pack for pack in self.result["packs"] if pack["pack_id"] == "core.finance")
        multi_currency = next(
            item for item in core["capability_coverage"]
            if item["capability"] == "finance.multi_currency"
        )
        self.assertEqual(multi_currency["implementation_status"], "executable")
        self.assertTrue(multi_currency["providers"]["runtime_guardrail"])

    def test_cfo_metric_capability_is_bound_to_deterministic_service(self):
        core = next(pack for pack in self.result["packs"] if pack["pack_id"] == "core.finance")
        cfo_metrics = next(
            item for item in core["capability_coverage"]
            if item["capability"] == "finance.cfo_metrics"
        )
        self.assertEqual(cfo_metrics["implementation_status"], "executable")
        self.assertEqual(
            cfo_metrics["providers"]["services"],
            ["core.evaluate_cfo_metrics"],
        )

    def test_game_channel_pack_is_fully_bound(self):
        app_store = next(pack for pack in self.result["packs"] if pack["pack_id"] == "channel.app_store")
        self.assertTrue(app_store["complete_implementation"])
        self.assertFalse(app_store["incomplete_capabilities"])


if __name__ == "__main__":
    unittest.main()
