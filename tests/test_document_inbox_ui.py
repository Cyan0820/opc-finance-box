import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DocumentInboxUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.javascript = (ROOT / "public" / "app.js").read_text(encoding="utf-8")
        cls.styles = (ROOT / "public" / "styles.css").read_text(encoding="utf-8")

    def test_bank_pdf_confirmation_and_evidence_linking_are_real_controls(self):
        self.assertIn("bank_statement_document:'银行 PDF/图片对账单'", self.javascript)
        self.assertIn("/api/inbox-correct", self.javascript)
        self.assertIn("confirmed_against_original:true", self.javascript)
        self.assertIn("/api/inbox-link", self.javascript)
        self.assertIn("同主体采购/验收记录", self.javascript)
        self.assertIn("settlement_reconciliation_evidence:'收入结算核对底稿'", self.javascript)
        self.assertIn(".document-preview-row", self.styles)

    def test_ocr_commit_button_requires_source_confirmation(self):
        self.assertIn("sourceConfirmed=!['invoice_document','bank_statement_document']", self.javascript)
        self.assertIn("&&scopeConfirmed&&sourceConfirmed", self.javascript)

    def test_inbox_confirmation_queue_navigates_instead_of_posting_to_get(self):
        self.assertIn("item.decision.method==='NAVIGATE'", self.javascript)
        self.assertIn("setView(item.decision.navigation_view||'documents')", self.javascript)


if __name__ == "__main__":
    unittest.main()
