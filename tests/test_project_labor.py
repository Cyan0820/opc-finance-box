import json
import unittest

from src.project_labor import build_project_labor_cost_view


class ProjectLaborCostTests(unittest.TestCase):
    def _row(self, **changes):
        row = {
            "id": "PAY-1", "entity_id": "cn_studio", "period": "2026-07", "currency": "CNY",
            "employee_masked": "员工-secret", "department": "研发一部", "gross_salary": 10000,
            "employer_contributions": 2000, "employer_levies": 100, "other_employer_cost": 0,
            "total_employer_cost": 12100, "employee_deductions": 1200, "withholding_tax": 300,
            "project_allocations": [{
                "project": "G001", "ratio": 0.75, "hours": 120, "total_hours": 160,
                "evidence": ["TIMESHEET-2026-07"], "evidence_type": "已批准工时表",
                "method": "工时比例", "activity_type": "研发活动", "research_ratio": 0.6,
            }],
        }
        row.update(changes)
        return row

    def test_allocates_salary_and_employer_cost_but_not_withholding_as_extra_cost(self):
        result = build_project_labor_cost_view([self._row()], "2026-07")
        project = result["rows"][0]
        scope = result["summary"]["by_entity_currency"][0]
        self.assertEqual(project["gross_salary"], 7500)
        self.assertEqual(project["employer_cost"], 1575)
        self.assertEqual(project["project_cost_candidate"], 9075)
        self.assertEqual(project["employee_deductions"], 900)
        self.assertEqual(project["withholding_tax"], 225)
        self.assertEqual(project["research_cost_candidate"], 7260)
        self.assertEqual(scope["unallocated_project_cost"], 3025)
        self.assertEqual(scope["total_employer_cost"], 12100)

    def test_missing_evidence_never_guesses_from_department_project_or_amount(self):
        row = self._row(project_allocations=None, project="G001", rd_ratio=0.8)
        result = build_project_labor_cost_view([row], "2026-07")
        self.assertEqual(result["rows"], [])
        scope = result["summary"]["by_entity_currency"][0]
        self.assertEqual(scope["allocated_project_cost"], 0)
        self.assertEqual(scope["unallocated_project_cost"], 12100)
        self.assertEqual(result["summary"]["gaps"]["invalid_or_missing_allocation_evidence"], 1)

    def test_ratio_over_one_or_cross_period_is_excluded(self):
        bad = self._row(project_allocations=[{
            "project": "G001", "ratio": 0.7, "evidence": ["A"],
        }, {
            "project": "G002", "ratio": 0.6, "evidence": ["B"],
        }])
        prior = self._row(id="PAY-PRIOR", period="2026-06")
        result = build_project_labor_cost_view([bad, prior], "2026-07")
        self.assertEqual(result["rows"], [])
        self.assertEqual(result["summary"]["by_entity_currency"][0]["unallocated_project_cost"], 12100)

    def test_entities_and_currencies_are_never_merged(self):
        overseas = self._row(
            id="PAY-SG", entity_id="sg_publisher", currency="USD", gross_salary=2000,
            employer_contributions=300, employer_levies=0, total_employer_cost=2300,
        )
        result = build_project_labor_cost_view([self._row(), overseas], "2026-07")
        self.assertEqual(
            {(row["entity_id"], row["currency"]) for row in result["rows"]},
            {("cn_studio", "CNY"), ("sg_publisher", "USD")},
        )
        self.assertEqual(len(result["summary"]["by_entity_currency"]), 2)

    def test_output_contains_no_person_or_individual_payroll_detail(self):
        serialized = json.dumps(build_project_labor_cost_view([self._row()], "2026-07"), ensure_ascii=False)
        for secret in ("员工-secret", "研发一部", "PAY-1"):
            self.assertNotIn(secret, serialized)
        self.assertNotIn("project_cost_lines", serialized)
        self.assertFalse(json.loads(serialized)["privacy"]["personal_rows_returned"])


if __name__ == "__main__":
    unittest.main()
