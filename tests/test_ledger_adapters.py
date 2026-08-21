import unittest

from src.accounting_engine import posted_trial_balance, roll_forward_opening_balances
from src.finance_ops import build_finance_ops
from src.ledger_adapters import create_adapter_review, functional_rate, get_ledger_adapter


class LedgerAdapterTests(unittest.TestCase):
    def setUp(self):
        self.profile = {
            "entity_id": "sg_publisher", "company_name": "Singapore Publisher",
            "jurisdiction": "SG", "functional_currency": "USD", "accounting_basis": "SFRS",
            "tax_pack": "jurisdiction.sg", "tax_readiness": "design",
        }
        self.adapter = get_ledger_adapter(self.profile)

    def test_sg_voucher_uses_usd_and_not_cn_accounts(self):
        finance = build_finance_ops([{
            "period": "2026-01", "game": "Pixel Odyssey", "channel": "App Store",
            "currency": "USD", "settlement_amount": 1000,
        }], "2026-01", company_profile=self.profile)
        voucher = finance["vouchers"][0]
        self.assertEqual(voucher["functional_currency"], "USD")
        self.assertEqual(voucher["debit"][0]["account_code"], "1200")
        self.assertEqual(voucher["credit"][0]["account_code"], "4000")
        self.assertNotIn("1122", str(voucher))

    def test_explicit_inverse_rate_converts_to_functional_currency(self):
        self.assertAlmostEqual(functional_rate("CNY", self.adapter, {"USD/CNY": 7.2}), 1 / 7.2)
        self.assertEqual(functional_rate("EUR", self.adapter, {"EUR/USD": 1.08}), 1.08)
        self.assertEqual(functional_rate("EUR", self.adapter, {"EUR": 1.08}), 0)

    def test_current_mapping_review_unlocks_posting_readiness(self):
        review = create_adapter_review(
            self.adapter, "sg_publisher", "批准", "当地会计服务机构",
            "已核对本位币、适用准则与全部科目角色映射", ["签字版科目映射表"],
        )
        finance = build_finance_ops([], "2026-01", company_profile=self.profile, ledger_adapter_reviews=[review])
        self.assertTrue(finance["ledger_adapter"]["posting_ready"])
        self.assertTrue(finance["posting"]["allowed"])

    def test_stale_review_does_not_unlock_changed_adapter(self):
        review = create_adapter_review(
            self.adapter, "sg_publisher", "批准", "当地会计服务机构",
            "已核对本位币、适用准则与全部科目角色映射", ["签字版科目映射表"],
        )
        review["adapter_fingerprint"] = "stale"
        finance = build_finance_ops([], "2026-01", company_profile=self.profile, ledger_adapter_reviews=[review])
        self.assertFalse(finance["ledger_adapter"]["posting_ready"])

    def test_sg_roll_forward_uses_role_metadata_and_sg_profit_account(self):
        posted = posted_trial_balance([{
            "period": "2026-01", "entity_id": "sg_publisher", "status": "已过账",
            "source_voucher_id": "V1",
            "debit": [self.adapter.line("trade_receivable", 100, "App Store")],
            "credit": [self.adapter.line("game_revenue", 100, "Pixel Odyssey")],
        }], "2026-01", "sg_publisher")
        opening = [
            {"period": "2026-01", "account": "1100 Cash at bank / 银行存款", "category": "资产", "opening_debit": 500},
            {"period": "2026-01", "account": "3000 Share capital / 股本", "category": "权益", "opening_credit": 500},
        ]
        carry = roll_forward_opening_balances(
            opening, posted, "2026-01", "当地会计", "3100 Retained earnings / 留存收益",
        )
        self.assertTrue(any(row["account"].startswith("3100") and row["opening_credit"] == 100 for row in carry["records"]))
        self.assertFalse(any(row["account"].startswith("4000") for row in carry["records"]))


if __name__ == "__main__":
    unittest.main()
