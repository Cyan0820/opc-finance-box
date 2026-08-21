from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class BankReconciliationUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.javascript = (ROOT / "public" / "app.js").read_text(encoding="utf-8")
        cls.styles = (ROOT / "public" / "styles.css").read_text(encoding="utf-8")

    def test_account_level_review_is_an_executable_flow(self):
        for marker in (
            "data-bank-reconciliation", "function openBankReconciliationForm",
            "bank-recon-ledger", "bank-recon-deposits", "bank-recon-payments",
            "/api/bank-reconciliation-review", "await ensureFinanceOps(true)",
        ):
            self.assertIn(marker, self.javascript)
        self.assertIn(".bank-reconciliation-account", self.styles)

    def test_form_explains_adjusted_bank_and_ledger_equation(self):
        self.assertIn("调整后银行 = 银行期末 + 在途存款 − 未兑现付款 + 银行侧调整", self.javascript)
        self.assertIn("调整后账面 = 账面期末 + 账面侧调整", self.javascript)


if __name__ == "__main__":
    unittest.main()
