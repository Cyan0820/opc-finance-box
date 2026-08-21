import unittest
from pathlib import Path

from src.box_runtime import BoxRuntime
from src.default_services import build_default_service_registry


ROOT = Path(__file__).resolve().parents[1]


class MultiEntityServicesTests(unittest.TestCase):
    def setUp(self):
        self.runtime = BoxRuntime(ROOT / "examples" / "boxes" / "global_game_studio.json", ROOT / "packs")
        self.registry = build_default_service_registry()
        self.payload = {
            "period": "2026-07",
            "entity_balances": [
                {"entity_id": "cn_studio", "currency": "CNY", "revenue": 1000, "expenses": 600, "cash": 500, "receivables": 200, "payables": 100},
                {"entity_id": "sg_publisher", "currency": "USD", "revenue": 100, "expenses": 40, "cash": 20, "receivables": 10, "payables": 5},
            ],
            "fx_rates": {
                "USD": {"pnl_rate": 7.1, "closing_rate": 7.2, "source": "approved month rates", "as_of": "2026-07-31"}
            },
            "intercompany_adjustments": [],
        }
        self.portfolio_payload = {
            "period": "2026-07",
            "entity_close_controls": [
                {
                    "entity_id": "cn_studio",
                    "period": "2026-07",
                    "source_pipeline_id": "finance.month_close_control",
                    "source_run_id": "a" * 24,
                    "source_evidence": ["pipeline-ledger://runs/" + "a" * 24],
                    "close_control_ready_for_review": True,
                    "candidate_only": True,
                    "posting_performed": False,
                    "period_close_performed": False,
                    "blockers": [],
                    "currency_summaries": [{
                        "currency": "CNY", "bank_account_count": 1,
                        "cash": 500, "assets": 800, "liabilities": 200,
                        "revenue": 1000, "expenses": 600,
                        "profit_before_tax_candidate": 400,
                    }],
                },
                {
                    "entity_id": "sg_publisher",
                    "period": "2026-07",
                    "source_pipeline_id": "finance.month_close_control",
                    "source_run_id": "b" * 24,
                    "source_evidence": ["pipeline-ledger://runs/" + "b" * 24],
                    "close_control_ready_for_review": True,
                    "candidate_only": True,
                    "posting_performed": False,
                    "period_close_performed": False,
                    "blockers": [],
                    "currency_summaries": [{
                        "currency": "USD", "bank_account_count": 1,
                        "cash": 20, "assets": 50, "liabilities": 10,
                        "revenue": 100, "expenses": 40,
                        "profit_before_tax_candidate": 60,
                    }],
                },
            ],
            "fx_rates": {
                "USD": {
                    "period": "2026-07", "pnl_rate": 7.1, "closing_rate": 7.2,
                    "source_reference": "fx://approved/2026-07/USD-CNY",
                    "review_status": "approved", "reviewed_by": "finance_reviewer",
                    "evidence": ["month-average and closing-rate workpaper"],
                },
            },
        }

    def test_management_consolidation_uses_average_and_closing_rates(self):
        result = self.registry.dispatch(
            self.runtime,
            "entity.consolidate_management_view",
            self.payload,
        )["output"]
        self.assertTrue(result["ready"])
        self.assertEqual(result["management_totals"]["revenue"], 1710)
        self.assertEqual(result["management_totals"]["cash"], 644)
        self.assertEqual(result["management_totals"]["profit"], 826)
        self.assertFalse(result["statutory_books_modified"])

    def test_missing_fx_rate_blocks_consolidation(self):
        payload = {**self.payload, "fx_rates": {}}
        result = self.registry.dispatch(
            self.runtime,
            "entity.consolidate_management_view",
            payload,
        )["output"]
        self.assertFalse(result["ready"])
        self.assertTrue(any("missing FX rates for USD" in blocker for blocker in result["blockers"]))

    def test_unapproved_adjustment_is_not_applied(self):
        payload = {**self.payload, "intercompany_adjustments": [{
            "id": "IC-1",
            "from_entity_id": "cn_studio",
            "to_entity_id": "sg_publisher",
            "metric": "revenue",
            "amount_reporting_currency": -100,
            "approved": False,
            "evidence": ["intercompany invoice"],
        }]}
        result = self.registry.dispatch(
            self.runtime,
            "entity.consolidate_management_view",
            payload,
        )["output"]
        self.assertFalse(result["ready"])
        self.assertEqual(result["management_totals"]["revenue"], 1710)
        self.assertFalse(result["approved_adjustments"])

    def test_approved_adjustment_changes_management_view_only(self):
        payload = {**self.payload, "intercompany_adjustments": [{
            "id": "IC-1",
            "from_entity_id": "cn_studio",
            "to_entity_id": "sg_publisher",
            "metric": "revenue",
            "amount_reporting_currency": -100,
            "approved": True,
            "approved_by": "Finance owner",
            "approved_at": "2026-08-10T10:00:00+08:00",
            "evidence": ["intercompany agreement", "invoice"],
        }]}
        result = self.registry.dispatch(
            self.runtime,
            "entity.consolidate_management_view",
            payload,
        )["output"]
        self.assertTrue(result["ready"])
        self.assertEqual(result["management_totals"]["revenue"], 1610)
        self.assertFalse(result["translated_entities"][0]["statutory_books_modified"])

    def test_month_close_portfolio_preserves_native_views_and_uses_explicit_fx(self):
        result = self.registry.dispatch(
            self.runtime,
            "entity.build_month_close_portfolio",
            self.portfolio_payload,
        )["output"]
        self.assertTrue(result["ready"], result)
        self.assertEqual(result["ready_entity_count"], 2)
        self.assertEqual(result["management_portfolio_totals"]["revenue"], 1710)
        self.assertEqual(result["management_portfolio_totals"]["cash"], 644)
        self.assertEqual(result["management_portfolio_totals"]["profit_before_tax_candidate"], 826)
        self.assertEqual(
            {row["currency_summaries"][0]["currency"] for row in result["native_entity_candidates"]},
            {"CNY", "USD"},
        )
        self.assertTrue(result["pre_elimination_view"])
        self.assertFalse(result["consolidated_financial_statements_produced"])
        self.assertFalse(result["statutory_books_modified"])

    def test_month_close_portfolio_blocks_missing_entity_without_partial_total(self):
        payload = dict(self.portfolio_payload)
        payload["entity_close_controls"] = self.portfolio_payload["entity_close_controls"][:1]
        result = self.registry.dispatch(
            self.runtime,
            "entity.build_month_close_portfolio",
            payload,
        )["output"]
        self.assertFalse(result["ready"])
        self.assertIsNone(result["management_portfolio_totals"])
        self.assertIn("missing entity close controls: sg_publisher", result["blockers"])

    def test_month_close_portfolio_blocks_unapproved_or_wrong_period_fx(self):
        payload = {**self.portfolio_payload, "fx_rates": {"USD": {
            **self.portfolio_payload["fx_rates"]["USD"],
            "period": "2026-06", "review_status": "pending",
        }}}
        result = self.registry.dispatch(
            self.runtime,
            "entity.build_month_close_portfolio",
            payload,
        )["output"]
        self.assertFalse(result["ready"])
        self.assertIsNone(result["management_portfolio_totals"])
        self.assertTrue(any("USD FX rate" in blocker for blocker in result["blockers"]))

    def test_month_close_portfolio_blocks_nonready_close_and_never_nets_currencies(self):
        payload = dict(self.portfolio_payload)
        controls = [dict(item) for item in self.portfolio_payload["entity_close_controls"]]
        controls[1]["close_control_ready_for_review"] = False
        controls[1]["blockers"] = [{"type": "bank_gl_balance_difference"}]
        payload["entity_close_controls"] = controls
        result = self.registry.dispatch(
            self.runtime,
            "entity.build_month_close_portfolio",
            payload,
        )["output"]
        self.assertFalse(result["ready"])
        self.assertIsNone(result["management_portfolio_totals"])
        self.assertEqual(result["ready_entity_count"], 1)
        self.assertEqual(len(result["native_entity_candidates"]), 2)
        self.assertFalse(result["period_close_performed"])


if __name__ == "__main__":
    unittest.main()
