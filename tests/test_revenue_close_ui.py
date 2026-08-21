from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]


class RevenueCloseUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
        cls.javascript = (ROOT / "public" / "app.js").read_text(encoding="utf-8")

    def test_income_close_is_a_first_class_statutory_workspace(self):
        for marker in (
            'view-revenue-close', 'revenue-close-metrics', 'revenue-close-list',
            '合同口径', '业务复核', '形成应收',
        ):
            self.assertIn(marker, self.html)
        self.assertIn("'revenue-close'", self.javascript)

    def test_import_routes_to_candidate_review_and_review_releases_receivable(self):
        for marker in (
            "/api/revenue-close?entity_id=", "/api/revenue-close-review",
            "尚未形成应收，请核对系统结论", "批准后才形成应收",
            "确认并形成应收",
        ):
            self.assertIn(marker, self.javascript)

    def test_css_and_javascript_cache_versions_match(self):
        versions = []
        for asset in ("styles.css", "i18n.js", "app.js"):
            match = re.search(rf'{re.escape(asset)}\?v=([^"\s]+)', self.html)
            self.assertIsNotNone(match)
            versions.append(match.group(1))
        self.assertEqual(len(set(versions)), 1)


if __name__ == "__main__":
    unittest.main()
