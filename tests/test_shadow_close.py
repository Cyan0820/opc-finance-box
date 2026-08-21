import unittest

from src.demo_scenarios import build_demo_payload, load_demo_scenarios
from src.server import DEMO_SCENARIOS
from src.shadow_close import (
    compare_shadow_close,
    review_shadow_close,
    validate_shadow_close_report,
)


class ShadowCloseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        scenarios = load_demo_scenarios(DEMO_SCENARIOS)
        cls.payload = build_demo_payload(scenarios["domestic"])
        cls.finance = cls.payload["finance_ops"]

    def baseline(self):
        statements = self.finance["financial_statements"]
        rows = []
        for item in statements["detail"]:
            code, name = item["account"].split(" ", 1)
            rows.append({
                "domain": "trial_balance", "entity_id": "cn_studio", "period": "2026-02",
                "key": code, "name": name,
                "value": item["closing_debit"] - item["closing_credit"],
                "absolute_tolerance": 1, "percent_tolerance": 0.001,
            })
        for code, name, value in (
            ("BS_ASSETS", "资产总额", statements["balance_sheet"]["assets"]),
            ("BS_LIABILITIES", "负债总额", statements["balance_sheet"]["liabilities"]),
            ("BS_EQUITY", "权益（含本期利润）", statements["balance_sheet"]["liabilities_and_equity"] - statements["balance_sheet"]["liabilities"]),
            ("IS_REVENUE", "营业收入", statements["income_statement"]["revenue"]),
            ("IS_EXPENSES", "成本费用", statements["income_statement"]["expenses"]),
            ("IS_PROFIT", "利润总额", statements["income_statement"]["profit_before_tax"]),
        ):
            rows.append({"domain": "statement", "entity_id": "cn_studio", "period": "2026-02", "key": code, "name": name, "value": value, "absolute_tolerance": 1, "percent_tolerance": 0.001})
        return {"id": "SHADOW-cn_studio-2026-02", "entity_id": "cn_studio", "period": "2026-02", "source_fingerprint": "fixture", "rows": rows}

    def test_matching_human_close_can_be_independently_signed(self):
        report = compare_shadow_close(self.baseline(), self.finance)
        self.assertEqual(report["exception_count"], 0)
        self.assertEqual(report["status"], "一致待签认")
        review = review_shadow_close(report, "验证通过", "独立复核人", "已核对人工报表和总账", ["关账包"])
        current = compare_shadow_close(self.baseline(), self.finance, [review])
        self.assertTrue(current["review_current"])

    def test_variance_is_explained_and_cannot_be_signed_as_clean(self):
        baseline = self.baseline()
        baseline["rows"][0]["value"] += 10000
        report = compare_shadow_close(baseline, self.finance)
        self.assertGreater(report["exception_count"], 0)
        self.assertTrue(any(row["status"] == "需解释" for row in report["comparisons"]))
        with self.assertRaisesRegex(ValueError, "不能签认"):
            review_shadow_close(report, "验证通过", "独立复核人", "已核对但仍有差异", [])
        with self.assertRaisesRegex(ValueError, "证据"):
            review_shadow_close(report, "接受差异", "独立复核人", "差异属于历史口径差异", [])

    def test_non_finite_or_negative_tolerance_fails_closed(self):
        baseline = self.baseline()
        baseline["rows"][0]["absolute_tolerance"] = -1
        with self.assertRaisesRegex(ValueError, "有限非负数"):
            compare_shadow_close(baseline, self.finance)
        baseline = self.baseline()
        baseline["rows"][0]["percent_tolerance"] = float("nan")
        with self.assertRaisesRegex(ValueError, "有限非负数"):
            compare_shadow_close(baseline, self.finance)

    def test_accepted_variance_requires_one_evidenced_resolution_per_exception(self):
        baseline = self.baseline()
        baseline["rows"][0]["value"] += 10000
        report = compare_shadow_close(baseline, self.finance)
        exceptions = [
            {
                "domain": item["domain"],
                "key": item["key"],
                "classification": "accounting_policy",
                "rationale": "人工基准使用历史政策口径，已核对并记录影响",
                "evidence_references": ["差异处置单-001"],
            }
            for item in report["comparisons"]
            if item["status"] != "一致"
        ]
        review = review_shadow_close(
            report,
            "接受差异",
            "独立复核人",
            "已核对差异影响与处置证据",
            ["复核签认包"],
            exceptions,
        )
        self.assertEqual(review["exception_resolutions"], exceptions)
        current = compare_shadow_close(baseline, self.finance, [review])
        self.assertTrue(current["review_current"])

    def test_report_review_expires_when_agent_result_changes(self):
        baseline = self.baseline()
        first = compare_shadow_close(baseline, self.finance)
        review = review_shadow_close(first, "验证通过", "独立复核人", "已核对人工报表和总账", ["关账包"])
        changed = dict(self.finance)
        changed["financial_statements"] = dict(self.finance["financial_statements"])
        changed["financial_statements"]["income_statement"] = dict(self.finance["financial_statements"]["income_statement"])
        changed["financial_statements"]["income_statement"]["revenue"] += 1
        current = compare_shadow_close(baseline, changed, [review])
        self.assertFalse(current["review_current"])

    def test_report_review_is_bound_to_baseline_entity_and_period(self):
        baseline = self.baseline()
        report = compare_shadow_close(baseline, self.finance)
        review = review_shadow_close(
            report, "验证通过", "独立复核人", "已核对人工报表和总账", ["关账包"],
        )
        for field, value in (
            ("id", "SHADOW-cn_studio-2026-03"),
            ("entity_id", "another_entity"),
            ("period", "2026-03"),
        ):
            tampered = dict(baseline)
            tampered[field] = value
            current = compare_shadow_close(tampered, self.finance, [review])
            self.assertFalse(current["review_current"], field)

    def test_serialized_report_must_pass_internal_counts_and_fingerprint_before_review(self):
        report = compare_shadow_close(self.baseline(), self.finance)
        self.assertTrue(validate_shadow_close_report(report)["valid"])
        tampered = dict(report)
        tampered["comparisons"] = [dict(item) for item in report["comparisons"]]
        tampered["comparisons"][0]["manual_value"] += 1
        with self.assertRaisesRegex(ValueError, "差异与人工/Agent金额|差异与人工/Agent 金额|指纹"):
            review_shadow_close(
                tampered,
                "验证通过",
                "独立复核人",
                "已核对人工报表和总账",
                ["关账包"],
            )
        tampered = dict(report)
        tampered["comparison_count"] += 1
        with self.assertRaisesRegex(ValueError, "比较总数"):
            validate_shadow_close_report(tampered)


if __name__ == "__main__":
    unittest.main()
