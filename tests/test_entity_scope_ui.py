from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class EntityScopeUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.javascript = (ROOT / "public" / "app.js").read_text(encoding="utf-8")

    def test_formal_workspaces_expose_one_shared_legal_entity_scope(self):
        marker = "const STATUTORY_VIEWS=new Set(["
        self.assertIn(marker, self.javascript)
        for view in (
            "'overview'", "'procurement'", "'banking'", "'invoices'", "'payroll'",
            "'planning'", "'records'", "'exceptions'", "'close'", "'vouchers'",
            "'ledger'", "'tax'", "'review'",
        ):
            self.assertIn(view, self.javascript[self.javascript.index(marker):])

    def test_formal_uploads_bind_selected_legal_entity(self):
        self.assertIn("form.append('entity_id',entityId)", self.javascript)
        for endpoint in (
            "/api/import", "/api/configured-import", "/api/procurement-import",
            "/api/bank-import", "/api/invoice-import", "/api/payroll-import",
            "/api/planning-import", "/api/kpi-import", "/api/onboarding-import",
        ):
            self.assertIn(endpoint, self.javascript)

    def test_entity_switch_recomputes_each_statutory_business_workspace(self):
        for marker in (
            "scopedProcurementData", "scopedBankData", "scopedInvoiceData",
            "scopedPayrollData", "settlementPayload(scopedRows(original.records))",
            "if(state.view==='planning')await loadPlanning()",
        ):
            self.assertIn(marker, self.javascript)

    def test_planning_requests_selected_entity(self):
        self.assertIn("&entity_id=${encodeURIComponent(entityId)}", self.javascript)
        self.assertIn("p.functional_currency||'CNY'", self.javascript)


if __name__ == "__main__":
    unittest.main()
