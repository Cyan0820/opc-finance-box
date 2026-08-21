from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ProjectCostUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
        cls.javascript = (ROOT / "public" / "app.js").read_text(encoding="utf-8")

    def test_cost_bridge_is_first_class_in_analysis(self):
        for marker in (
            "项目采购成本桥", "project-cost-table", "批准占用", "订单承诺",
            "待验收交付", "已验收实际", "待开票", "待付款", "预算剩余",
        ):
            self.assertIn(marker, self.html)

    def test_project_profit_shows_actual_and_committed_views(self):
        for marker in ("已验收采购", "未履约承诺", "承诺后贡献"):
            self.assertIn(marker, self.html)
        for marker in (
            "project_procurement_costs", "procurement_actual", "procurement_open_commitment",
            "committed_contribution", "已付 ${fmt(row.paid_amount,row.currency)}，不冲成本",
        ):
            self.assertIn(marker, self.javascript)

    def test_scope_and_evidence_status_are_rendered(self):
        for marker in (
            "主体 / 币种分列", "row.entity_id", "row.currency", "row.control_status", "row.issues",
        ):
            self.assertIn(marker, self.html + self.javascript)


if __name__ == "__main__":
    unittest.main()
