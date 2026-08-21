from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ProjectLaborUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
        cls.javascript = (ROOT / "public" / "app.js").read_text(encoding="utf-8")

    def test_labor_bridge_is_first_class_in_analysis(self):
        for marker in (
            "项目人力成本桥", "project-labor-table", "隐私汇总", "工资分配", "雇主成本",
            "项目成本候选", "员工扣款", "代扣代缴", "工时 / 比例证据", "研发工时候选",
        ):
            self.assertIn(marker, self.html)

    def test_person_level_pay_is_not_rendered(self):
        render = self.javascript[self.javascript.index("function renderProjectLaborCosts"):]
        for forbidden in ("employee_masked", "department", "employee_id", "gross_salary_detail"):
            self.assertNotIn(forbidden, render)
        self.assertIn("不增加成本", render)
        self.assertIn("research_treatment", render)

    def test_project_profit_receives_labor_candidate(self):
        self.assertIn("人力成本候选", self.html)
        self.assertIn("project_labor_costs", self.javascript)
        self.assertIn("fmt(x.payroll||0,'CNY')", self.javascript)


if __name__ == "__main__":
    unittest.main()
