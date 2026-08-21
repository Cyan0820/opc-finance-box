import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook

from src.payroll import cumulative_withholding_tax, parse_payroll_workbook
from src.project_labor import build_project_labor_cost_view


class PayrollTests(unittest.TestCase):
    def test_cumulative_withholding_first_bracket(self):
        result = cumulative_withholding_tax(
            cumulative_income=20_000,
            cumulative_tax_exempt=0,
            cumulative_basic_deduction=5_000,
            cumulative_special=2_000,
            cumulative_special_additional=1_000,
            cumulative_other=0,
            cumulative_relief=0,
            tax_paid_ytd=0,
        )
        self.assertEqual(result['cumulative_taxable_income'], 12_000)
        self.assertEqual(result['rate'], 0.03)
        self.assertEqual(result['current_tax'], 360)

    def test_cumulative_tax_subtracts_tax_already_withheld(self):
        result = cumulative_withholding_tax(
            cumulative_income=120_000,
            cumulative_tax_exempt=0,
            cumulative_basic_deduction=30_000,
            cumulative_special=12_000,
            cumulative_special_additional=6_000,
            cumulative_other=0,
            cumulative_relief=0,
            tax_paid_ytd=2_000,
        )
        self.assertEqual(result['cumulative_taxable_income'], 72_000)
        self.assertEqual(result['rate'], 0.10)
        self.assertEqual(result['cumulative_tax'], 4_680)
        self.assertEqual(result['current_tax'], 2_680)

    def test_sg_payroll_import_validates_local_result_without_guessing_statutory_rate(self):
        book = Workbook()
        sheet = book.active
        sheet.append([
            "Employee ID", "Employee Name", "Department", "Gross Salary",
            "Employee Deductions", "Withholding Tax", "Net Salary",
            "Employer CPF", "SDL", "Currency",
        ])
        sheet.append(["E001", "Alice", "Studio", 10_000, 1_000, 0, 9_000, 1_700, 25, "SGD"])
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "sg-payroll.xlsx"
            book.save(path)
            rows = parse_payroll_workbook(path, "2026-01", "SG", "USD")
        self.assertEqual(rows[0].payroll_basis, "IMPORTED_LOCAL_PAYROLL")
        self.assertEqual(rows[0].calculated_iit, 0)
        self.assertEqual(rows[0].employee_deductions, 1_000)
        self.assertEqual(rows[0].employer_contributions, 1_700)
        self.assertEqual(rows[0].total_employer_cost, 11_725)
        self.assertEqual(rows[0].anomalies, [])

    def test_project_allocation_import_requires_explicit_evidence_fields(self):
        book = Workbook()
        sheet = book.active
        sheet.append([
            "工号", "姓名", "应发工资", "实发工资", "项目", "项目分摊比例",
            "本项目工时", "本期总工时", "工时证据", "证据类型", "活动性质",
        ])
        sheet.append([
            "E001", "成员甲", 10000, 10000, "G001", 0.75,
            120, 160, "TS-2026-07；APPROVAL-01", "已批准月度工时表", "研发活动",
        ])
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "payroll-allocation.xlsx"
            book.save(path)
            rows = parse_payroll_workbook(path, "2026-07")
        imported = rows[0]
        self.assertEqual(imported.allocation_ratio, 0.75)
        self.assertEqual(imported.timesheet_hours, 120)
        self.assertEqual(imported.total_hours, 160)
        self.assertEqual(imported.allocation_evidence, ["TS-2026-07", "APPROVAL-01"])
        view = build_project_labor_cost_view([imported.__dict__], "2026-07")
        self.assertEqual(view["rows"][0]["project_cost_candidate"], 7500)


if __name__ == '__main__':
    unittest.main()
