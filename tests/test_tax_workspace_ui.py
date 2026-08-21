import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class TaxWorkspaceUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
        cls.javascript = (ROOT / "public" / "app.js").read_text(encoding="utf-8")
        cls.styles = (ROOT / "public" / "styles.css").read_text(encoding="utf-8")

    def test_workspace_is_available_to_game_and_pack_driven_boxes(self):
        self.assertIn('data-module="tax-packs" data-view="tax-workspace"', self.html)
        for element_id in (
            "view-tax-workspace",
            "tax-workspace-year",
            "tax-workspace-as-of",
            "tax-workspace-refresh",
            "tax-workspace-metrics",
            "tax-workspace-entities",
            "tax-workspace-detail",
        ):
            self.assertIn(f'id="{element_id}"', self.html)
        self.assertGreaterEqual(self.javascript.count("'tax-workspace'"), 4)
        self.assertIn("'tax-packs':{title:'税务能力包工作台'", self.javascript)

    def test_workspace_is_read_only_and_does_not_dispatch_templates(self):
        self.assertIn("/api/box/tax/workspace", self.javascript)
        self.assertIn("复制请求模板", self.javascript)
        self.assertIn("这里只复制可编辑 JSON，不会直接 dispatch", self.javascript)
        self.assertNotIn("fetch(services.dispatch_endpoint", self.javascript)
        self.assertNotIn("fetch('/api/box/services/dispatch'", self.javascript)

    def test_date_fact_editor_is_in_memory_get_preview_only(self):
        for marker in (
            "data-tax-anchor", "data-tax-anchor-preview",
            "taxWorkspaceAnchorPayload", "query.set('anchors'",
            "仅本次预览", "不保存、不修改 Box 配置",
        ):
            self.assertIn(marker, self.javascript)
        self.assertNotIn("localStorage", self.javascript)
        self.assertNotIn("sessionStorage", self.javascript)
        self.assertIn(".tax-anchor-panel", self.styles)
        self.assertIn(".tax-anchor-grid", self.styles)

    def test_workspace_preserves_tax_control_boundaries(self):
        for phrase in (
            "配置不是登记证据",
            "候选日期也不是申报或付款授权",
            "系统不收集税号原值",
            "税额计算：未执行",
            "申报：未执行",
            "付款：未执行",
            "外部提交：未启用",
            "不构成证据或发布日期批准",
        ):
            self.assertIn(phrase, self.html + self.javascript)
        self.assertIn("registration_evidence_required_count", self.javascript)
        self.assertIn("ruleLifecycle=item.rule_lifecycle", self.javascript)
        self.assertIn("applicability=item.applicability_review_requirement", self.javascript)
        self.assertIn("registry=data.applicability_review_registry", self.javascript)
        self.assertIn("私有文件权限", self.javascript)
        self.assertIn("轮换收据已绑定当前目录", self.javascript)
        self.assertIn("轮换收据未配置或无效", self.javascript)
        self.assertIn("匹配轮换收据", self.javascript)
        self.assertIn("私有回答、复核理由和证据引用不会返回浏览器", self.javascript)

    def test_workspace_layout_has_entity_calendar_source_and_mobile_styles(self):
        for selector in (
            ".tax-workspace-entities",
            ".tax-entity-tab",
            ".tax-calendar-task",
            ".tax-source-list",
            ".tax-rule-list",
            ".tax-non-actions",
        ):
            self.assertIn(selector, self.styles)
        self.assertIn("@media(max-width:760px)", self.styles)


if __name__ == "__main__":
    unittest.main()
