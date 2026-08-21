import copy
import json
import unittest
from pathlib import Path

from src.box_compiler import compile_box, preflight_pipeline_request
from src.box_pipeline import BoxPipelineError, dispatch_box_pipeline_request
from src.box_runtime import BoxRuntime


ROOT = Path(__file__).resolve().parents[1]


class MultiEntityMonthClosePortfolioTests(unittest.TestCase):
    def setUp(self):
        self.runtime = BoxRuntime(
            ROOT / "examples" / "boxes" / "global_game_studio.json",
            ROOT / "packs",
        )
        self.request = json.loads(
            (ROOT / "examples" / "pipelines" / "multi_entity_month_close_portfolio_fixture.json")
            .read_text(encoding="utf-8")
        )

    def test_pipeline_builds_deterministic_pre_elimination_founder_portfolio(self):
        first = dispatch_box_pipeline_request(self.runtime, self.request)
        reordered = copy.deepcopy(self.request)
        reordered["payload"]["entity_ids"].reverse()
        reordered["payload"]["entity_close_controls"].reverse()
        second = dispatch_box_pipeline_request(self.runtime, reordered)
        self.assertTrue(first["ready"], first)
        self.assertEqual(first["pipeline"]["run_id"], second["pipeline"]["run_id"])
        self.assertEqual(first["pipeline"]["required_review_gates"], [
            "month_close_portfolio_review",
        ])
        briefing = first["founder_briefing"]
        self.assertEqual(briefing["entity_count"], 2)
        self.assertEqual(briefing["ready_entity_count"], 2)
        self.assertEqual(briefing["management_portfolio_totals"]["cash"], 644)
        self.assertEqual(briefing["management_portfolio_totals"]["revenue"], 1710)
        self.assertFalse(briefing["cross_entity_native_currency_netting_performed"])
        self.assertFalse(briefing["consolidated_financial_statements_produced"])
        self.assertFalse(first["source_access_performed"])
        self.assertFalse(first["posting_performed"])
        self.assertFalse(first["period_close_performed"])

    def test_pipeline_blocks_nonready_entity_and_suppresses_total(self):
        request = copy.deepcopy(self.request)
        source = request["payload"]["entity_close_controls"][1]
        source["close_control_ready_for_review"] = False
        source["blockers"] = [{"type": "bank_gl_balance_difference"}]
        result = dispatch_box_pipeline_request(self.runtime, request)
        self.assertFalse(result["ready"])
        self.assertEqual(result["blocked_at"], "month_close_portfolio")
        self.assertIsNone(result["founder_briefing"]["management_portfolio_totals"])
        self.assertEqual(result["founder_briefing"]["ready_entity_count"], 1)

    def test_pipeline_rejects_duplicate_or_unconfigured_entity_scope(self):
        duplicate = copy.deepcopy(self.request)
        duplicate["payload"]["entity_ids"] = ["cn_studio", "cn_studio"]
        with self.assertRaisesRegex(BoxPipelineError, "must be unique"):
            dispatch_box_pipeline_request(self.runtime, duplicate)
        unknown = copy.deepcopy(self.request)
        unknown["payload"]["entity_ids"] = ["cn_studio", "other_company"]
        with self.assertRaisesRegex(BoxPipelineError, "unconfigured entities"):
            dispatch_box_pipeline_request(self.runtime, unknown)

    def test_preflight_accepts_fixture_and_template_remains_fail_closed(self):
        accepted = preflight_pipeline_request(self.runtime, self.request)
        self.assertTrue(accepted["ready_to_dispatch"], accepted)
        compiled = compile_box(self.runtime)
        templates = [
            item for item in compiled["pipeline_request_templates"]["templates"]
            if item["pipeline_id"] == "finance.multi_entity_month_close_portfolio"
        ]
        self.assertEqual(len(templates), 1)
        self.assertEqual(templates[0]["entity_scope"], "management")
        self.assertNotIn("entity_id", templates[0])
        blocked = preflight_pipeline_request(self.runtime, templates[0]["request"])
        self.assertFalse(blocked["ready_to_dispatch"])
        self.assertTrue(blocked["placeholder_paths"])
        excluded = [
            item for item in compiled["pipeline_schedule_template"]["excluded_templates"]
            if item["pipeline_id"] == "finance.multi_entity_month_close_portfolio"
        ]
        self.assertEqual(len(excluded), 1)


if __name__ == "__main__":
    unittest.main()
