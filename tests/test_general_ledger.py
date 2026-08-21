import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook

from src.general_ledger import build_financial_statements, opening_balance_payload, parse_opening_balance_workbook


class GeneralLedgerTests(unittest.TestCase):
    def test_imports_balanced_opening_trial_balance(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "期初余额.xlsx"
            workbook = Workbook()
            sheet = workbook.active
            sheet.append(["期间", "科目编码", "科目名称", "期初借方", "期初贷方"])
            sheet.append(["2026-01", "1002", "银行存款", 500, 0])
            sheet.append(["2026-01", "3001", "实收资本", 0, 500])
            workbook.save(path)
            rows = parse_opening_balance_workbook(path)
        payload = opening_balance_payload(rows)
        self.assertEqual(payload["summary"]["difference"], 0)
        self.assertEqual(rows[0].category, "资产")

    def test_statement_rolls_opening_and_current_activity(self):
        opening = [
            {"period": "2026-01", "account": "1002 银行存款", "category": "资产",
             "opening_debit": 500, "opening_credit": 0},
            {"period": "2026-01", "account": "3001 实收资本", "category": "权益",
             "opening_debit": 0, "opening_credit": 500},
        ]
        trial = {"rows": [
            {"account": "1002 银行存款", "debit": 1000, "credit": 300, "net": 700},
            {"account": "5001 主营业务收入", "debit": 0, "credit": 1000, "net": -1000},
            {"account": "5602 管理费用", "debit": 300, "credit": 0, "net": 300},
        ]}
        statements = build_financial_statements(opening, trial, "2026-01")
        self.assertTrue(statements["balance_sheet"]["balanced"])
        self.assertEqual(statements["income_statement"]["profit_before_tax"], 700)

    def test_no_opening_balance_never_claims_complete_balance_sheet(self):
        statements = build_financial_statements([], {"rows": []}, "2026-01")
        self.assertFalse(statements["opening_available"])
        self.assertIn("期初余额", statements["guardrail"])


if __name__ == "__main__":
    unittest.main()
