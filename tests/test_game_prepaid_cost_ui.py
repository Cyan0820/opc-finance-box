from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class GamePrepaidCostUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
        cls.javascript = (ROOT / "public" / "app.js").read_text(encoding="utf-8")

    def test_candidate_bridge_and_release_schedule_are_visible(self):
        for marker in (
            "授权与云资源成本候选桥", "prepaid-cost-table", "prepaid-release-table",
            "合同服务期", "候选分类 / 政策", "本期释放候选", "未释放候选", "期间释放表",
        ):
            self.assertIn(marker, self.html)

    def test_payment_and_accounting_boundaries_are_explicit(self):
        for marker in (
            "不决定资本化或费用化", "accounting_boundary", "未过账",
        ):
            self.assertIn(marker, self.javascript)
        self.assertIn("没有当期权利或服务证据", self.html)

    def test_project_profit_receives_release_candidate(self):
        self.assertIn("授权/云释放候选", self.html)
        self.assertIn("game_prepaid_costs", self.javascript)
        self.assertIn("special_cost_release_candidate", self.javascript)


if __name__ == "__main__":
    unittest.main()
