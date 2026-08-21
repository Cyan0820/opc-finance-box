import unittest

from src.finance_qa import answer_finance_question


class FinanceQaTests(unittest.TestCase):
    def setUp(self):
        self.bp = {
            "period": "2026-07", "totals": {"revenue": 1000, "contribution": 600},
            "projects": [{"project_code": "G1", "project_name": "星海", "revenue": 1000, "direct_cost": 400, "contribution": 600, "contribution_margin": .6, "evidence_count": 4, "kpis": {"gross_roas": 2.2}}],
            "change_vs_previous": {"revenue": 100, "contribution": 50},
            "variance": [{"category": "投放", "actual": 300, "budget": 200, "variance": 100, "favorable": False}],
            "planning": {"opening_cash_cny": 1000, "unpaid_purchase_commitments": 100, "runway_months": None, "buffer_breach_period": None, "forecast": [{"ending_cash": 900}]},
            "data_quality": {"kpi_record_count": 1, "unassigned_cost": 0, "payroll_record_count": 1, "purchase_record_count": 1},
            "change_attribution": {
                "confidence": .86,
                "dimension_contributors": [{"game": "星海", "channel": "Apple", "change": 100}],
                "operating_drivers": [{"project": "星海", "gross_change": 120, "mau_effect": 80, "payer_rate_effect": 30, "arppu_effect": 10, "residual": 0}],
                "limitations": [],
            },
            "business_flow_status": {
                "overdue_receivable_items": [{"game": "星海", "channel": "Apple", "outstanding": 88, "currency": "USD", "days_overdue": 40}],
                "pending_payment_items": [{"id": "PAY-1", "purpose": "美术外包尾款", "amount": 5000, "currency": "CNY"}],
            },
        }

    def test_answer_has_recommendation_evidence_confidence_and_tradeoffs(self):
        answer = answer_finance_question("这个月哪个游戏最赚钱？", self.bp)
        self.assertIn("星海", answer["answer"])
        self.assertTrue(answer["evidence"])
        self.assertTrue(any(option["recommended"] for option in answer["options"]))
        self.assertGreater(answer["confidence"], .8)

    def test_tax_answer_gives_inclination_without_pretending_to_file(self):
        answer = answer_finance_question("海外收入税务怎么处理？", self.bp)
        self.assertIn("待补证据", answer["recommendation"])
        self.assertIn("不替代", answer["scope"])

    def test_missing_cost_ledgers_reduce_confidence(self):
        bp = {**self.bp, "data_quality": {"kpi_record_count": 1, "unassigned_cost": 0, "payroll_record_count": 0, "purchase_record_count": 0}}
        answer = answer_finance_question("这个月哪个游戏最赚钱？", bp, onboarding={"ready_for_bp": False, "readiness_score": 25})
        self.assertLessEqual(answer["confidence"], .55)
        self.assertTrue(any("项目直接成本不完整" in gap for gap in answer["data_gaps"]))

    def test_change_question_returns_actual_driver_not_only_generic_advice(self):
        answer = answer_finance_question("为什么本月收入增长？", self.bp)
        self.assertIn("Apple", answer["answer"])
        self.assertIn("MAU", answer["answer"])
        self.assertEqual(answer["confidence"], .86)

    def test_receivable_question_keeps_original_currency_and_actions(self):
        answer = answer_finance_question("哪些钱该收还没收？", self.bp)
        self.assertIn("88.00 USD", answer["answer"])
        self.assertIn("逾期 40 天", answer["answer"])
        self.assertTrue(answer["next_actions"])

    def test_payment_approval_question_uses_pending_requests(self):
        answer = answer_finance_question("今天有哪些付款等我审批？", self.bp)
        self.assertIn("1 笔", answer["answer"])
        self.assertIn("美术外包尾款", answer["evidence"][0]["metric"])


if __name__ == "__main__":
    unittest.main()
