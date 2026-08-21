from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class GamePortfolioUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
        cls.javascript = (ROOT / "public" / "app.js").read_text(encoding="utf-8")

    def test_management_view_is_read_only_and_drills_to_entity_collection(self):
        for marker in (
            "game-collection-summary", "game-collection-table", "管理视图只读",
            "国服与海外回款与催收", "承诺回款情景", "催收优先级",
        ):
            self.assertIn(marker, self.html)
        for marker in (
            "renderGameCollections", "data-game-collection-entity", "进入主体回款",
            "suggested_allocation_amount", "remaining_amount", "剩余可核销",
            "collection_priority_label", "promise_scenario_outstanding", "进入主体催收",
        ):
            self.assertIn(marker, self.javascript)


if __name__ == "__main__":
    unittest.main()
