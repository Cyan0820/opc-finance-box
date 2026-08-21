import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook

from src.planning import build_planning_analysis, parse_plan_workbook, planning_payload


class PlanningTests(unittest.TestCase):
    def test_overseas_entity_forecasts_in_functional_currency_without_mixing_cny(self):
        result = build_planning_analysis(
            [
                {"period": "2026-03", "scenario": "基准", "direction": "收入", "amount": 500, "currency": "USD"},
                {"period": "2026-03", "scenario": "基准", "direction": "收入", "amount": 9999, "currency": "CNY"},
            ],
            bank_transactions=[
                {"transaction_date": "2026-02-28", "currency": "USD", "balance": 1000},
                {"transaction_date": "2026-02-28", "currency": "CNY", "balance": 99999},
            ],
            payroll_rows=[
                {"period": "2026-02", "currency": "USD", "gross_salary": 100, "total_employer_cost": 120},
            ],
            company_profile={"base_currency": "USD", "cash_planning": {"minimum_buffer_usd": 300}},
            as_of_period="2026-02",
        )
        self.assertEqual(result["functional_currency"], "USD")
        self.assertEqual(result["opening_cash"], 1000)
        self.assertEqual(result["minimum_buffer"], 300)
        self.assertEqual(result["forecast"][0]["inflows"], 500)
        self.assertIsNone(result["opening_cash_cny"])
        self.assertEqual(result["variance"][0]["actual"], 120)

    def test_row_format_parses_business_friendly_budget(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "预算.xlsx"
            workbook = Workbook()
            sheet = workbook.active
            sheet.append(["月份", "游戏", "类别", "收支方向", "金额", "情景", "已承诺"])
            sheet.append(["2026-09", "游戏甲", "广告投放", "支出", 100000, "基准", "是"])
            workbook.save(path)
            rows = parse_plan_workbook(path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].category, "投放")
        self.assertTrue(rows[0].committed)

    def test_horizontal_month_format_is_supported(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "滚动预测.xlsx"
            workbook = Workbook()
            sheet = workbook.active
            sheet.append(["游戏", "类别", "收支方向", "2026-09", "2026-10"])
            sheet.append(["游戏乙", "收入", "收入", 200000, 210000])
            workbook.save(path)
            rows = parse_plan_workbook(path)
        self.assertEqual([row.period for row in rows], ["2026-09", "2026-10"])

    def test_cash_forecast_flags_buffer_breach_and_keeps_currency_guardrail(self):
        lines = [
            {"period": "2026-09", "scenario": "基准", "category": "收入", "direction": "收入",
             "amount": 100, "currency": "CNY", "probability": 1, "committed": False, "anomalies": []},
            {"period": "2026-09", "scenario": "基准", "category": "人力", "direction": "支出",
             "amount": 500, "currency": "CNY", "probability": 1, "committed": True, "anomalies": []},
            {"period": "2026-09", "scenario": "基准", "category": "收入", "direction": "收入",
             "amount": 9999, "currency": "USD", "probability": 1, "committed": False, "anomalies": []},
        ]
        result = build_planning_analysis(
            lines, company_profile={"cash_planning": {"opening_cash_cny": 1000, "minimum_buffer_cny": 700}},
            as_of_period="2026-08",
        )
        self.assertEqual(result["forecast"][0]["ending_cash"], 600)
        self.assertEqual(result["buffer_breach_period"], "2026-09")
        self.assertIn("外币", result["guardrail"])

    def test_variance_compares_settlement_purchase_and_payroll_actuals(self):
        result = build_planning_analysis(
            [{"period": "2026-01", "scenario": "基准", "category": "收入", "direction": "收入",
              "amount": 900, "currency": "CNY", "probability": 1, "anomalies": []}],
            settlements=[{"period": "2026-01", "currency": "CNY", "settlement_amount": 1000}],
            as_of_period="2026-01",
        )
        income = next(row for row in result["variance"] if row["category"] == "收入")
        self.assertEqual(income["variance"], 100)
        self.assertTrue(income["favorable"])

    def test_collection_commitments_are_a_separate_cash_scenario(self):
        result = build_planning_analysis(
            [],
            settlements=[{"id": "S1", "entity_id": "cn_studio", "period": "2026-01", "currency": "CNY", "net_receivable": 500}],
            company_profile={"base_currency": "CNY", "cash_planning": {"opening_cash_cny": 1000}},
            as_of_period="2026-01",
            collection_actions=[{
                "id": "C1", "entity_id": "cn_studio", "settlement_id": "S1",
                "action_type": "回款承诺", "promised_date": "2026-02-15",
                "promised_amount": 300, "currency": "CNY", "recorded_at": "2026-01-20T00:00:00Z",
            }],
        )
        first = result["forecast"][0]
        self.assertEqual(first["ending_cash"], 1000)
        self.assertEqual(first["collection_commitments"], 300)
        self.assertEqual(first["ending_cash_with_commitments"], 1300)
        self.assertIn("不改法定应收", result["guardrail"])

    def test_disputed_old_promise_is_not_used_in_cash_scenario(self):
        actions = [{
            "id": "C1", "entity_id": "cn_studio", "settlement_id": "S1", "action_type": "回款承诺",
            "promised_date": "2026-02-15", "promised_amount": 300, "currency": "CNY",
            "recorded_at": "2026-01-20T00:00:00Z",
        }, {
            "id": "C2", "entity_id": "cn_studio", "settlement_id": "S1", "action_type": "争议登记",
            "recorded_at": "2026-01-25T00:00:00Z",
        }]
        result = build_planning_analysis(
            [], settlements=[{"id": "S1", "entity_id": "cn_studio", "currency": "CNY", "net_receivable": 500}],
            company_profile={"cash_planning": {"opening_cash_cny": 1000}}, as_of_period="2026-01",
            collection_actions=actions,
        )
        self.assertEqual(result["collection_commitment_total"], 0)

    def test_payload_summary(self):
        payload = planning_payload([{"period": "2026-09", "scenario": "基准", "project": "游戏甲",
                                     "direction": "支出", "amount": 50, "committed": True}])
        self.assertEqual(payload["summary"]["committed_expense"], 50)


if __name__ == "__main__":
    unittest.main()
