from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class BusinessFlowsUiTests(unittest.TestCase):
    def test_workspace_has_complete_business_flow_entry_points(self):
        html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
        script = (ROOT / "public" / "app.js").read_text(encoding="utf-8")
        for marker in ("view-flows", "flow-entity", "receivable-list", "payable-list", "payment-request-list", "expense-claim-list", "allocation-list", "asset-new", "accrual-new"):
            self.assertIn(marker, html)
        for endpoint in ("/api/business-flows", "/api/payment-request", "/api/expense-claim", "/api/cash-allocation", "/api/collection-action", "/api/procurement-request", "/api/procurement-workflow", "/api/purchase-order", "/api/purchase-delivery", "/api/vendor-bank-change", "/api/vendor-bank-accounts", "/api/asset-card", "/api/accrual"):
            self.assertIn(endpoint, script)
        self.assertIn("C08:'flows'", script)
        self.assertIn("procurement-request-new", html)
        self.assertIn("vendor-bank-change-new", html)
        self.assertIn("三方", script)
        self.assertIn("交付里程碑", script)
        self.assertIn("登记交付不等于验收", script)

    def test_cross_currency_payables_are_presented_by_currency(self):
        script = (ROOT / "public" / "app.js").read_text(encoding="utf-8")
        self.assertIn("function currencyOutstanding", script)
        self.assertNotIn("reduce((a,x)=>a+Math.max(0,Number(x.outstanding||0)),0);el('flow-metrics')", script)

    def test_money_actions_use_entity_scoped_approval_and_system_authorization(self):
        script = (ROOT / "public" / "app.js").read_text(encoding="utf-8")
        for marker in (
            "已批准付款申请（支出必选）", "data-flow-payment-decision",
            "data-flow-expense-decision", "function openPaymentDecisionForm",
            "function openExpenseDecisionForm", "全球管理汇总只读",
        ):
            self.assertIn(marker, script)


if __name__ == "__main__":
    unittest.main()
