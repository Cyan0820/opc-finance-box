import unittest

from src.accounting_engine import (
    asset_schedule, build_adjustment_vouchers, build_expense_vouchers, create_accrual, create_asset_card,
    post_reviewed_vouchers, posted_trial_balance, review_accounting_item, roll_forward_opening_balances,
)
from src.ledger_adapters import get_ledger_adapter


class AccountingEngineTests(unittest.TestCase):
    def test_asset_schedule_uses_exact_final_month_rounding(self):
        card = create_asset_card("电脑", "固定资产", "2026-01-10", 1000, 3, 0, "研发", "供应商", ["发票"], "财务")
        card = review_accounting_item(card, "批准", "会计", "使用期和类别合理")
        schedule = asset_schedule(card)
        self.assertEqual(schedule[0]["period"], "2026-02")
        self.assertEqual(sum(row["amount"] for row in schedule), 1000)
        self.assertEqual(schedule[-1]["closing_net_value"], 0)

    def test_accrual_generates_entry_and_next_month_reversal(self):
        accrual = create_accrual("2026-01", "美术外包", 500, "5602 管理费用", "供应商", "游戏A", ["验收单"], "财务")
        accrual = review_accounting_item(accrual, "批准", "会计", "服务已完成金额可靠")
        january = build_adjustment_vouchers([], [accrual], "2026-01")[0]
        february = build_adjustment_vouchers([], [accrual], "2026-02")[0]
        self.assertEqual(january["type"], "费用暂估")
        self.assertEqual(february["type"], "暂估冲回")
        self.assertEqual(january["debit"][0]["account"], february["credit"][0]["account"])

    def test_sg_accrual_and_reversal_use_usd_ledger_roles(self):
        adapter = get_ledger_adapter({
            "jurisdiction": "SG", "functional_currency": "USD", "accounting_basis": "SFRS",
        })
        accrual = create_accrual(
            "2026-01", "海外美术外包", 500, "", "Vendor", "Global Game",
            ["acceptance report"], "finance", currency="USD", functional_currency="USD",
            expense_role="cost_of_sales", ledger_adapter_id=adapter.id,
        )
        accrual = review_accounting_item(accrual, "批准", "SG accountant", "服务已完成且金额可靠")
        january = build_adjustment_vouchers([], [accrual], "2026-01", adapter)[0]
        february = build_adjustment_vouchers([], [accrual], "2026-02", adapter)[0]
        self.assertEqual(january["functional_currency"], "USD")
        self.assertEqual(january["debit"][0]["account_code"], "5000")
        self.assertEqual(january["credit"][0]["account_code"], "2200")
        self.assertEqual(january["debit"][0]["account"], february["credit"][0]["account"])

    def test_sg_asset_schedule_and_depreciation_preserve_functional_currency(self):
        adapter = get_ledger_adapter({
            "jurisdiction": "SG", "functional_currency": "USD", "accounting_basis": "SFRS",
        })
        card = create_asset_card(
            "Laptop", "固定资产", "2026-01-10", 7200, 3, 0, "Global Game", "Vendor",
            ["invoice", "acceptance"], "finance", currency="CNY", functional_currency="USD",
            fx_rate=1 / 7.2, ledger_adapter_id=adapter.id,
        )
        card = review_accounting_item(card, "批准", "SG accountant", "类别和三个月受益期已核对")
        self.assertEqual(card["functional_cost"], 1000)
        voucher = build_adjustment_vouchers([card], [], "2026-02", adapter)[0]
        self.assertEqual(voucher["functional_currency"], "USD")
        self.assertEqual(voucher["debit"][0]["account_code"], "6100")
        self.assertEqual(voucher["credit"][0]["account_code"], "1650")

    def test_sg_expense_claim_converts_to_usd_and_uses_employee_payable_role(self):
        adapter = get_ledger_adapter({
            "jurisdiction": "SG", "functional_currency": "USD", "accounting_basis": "SFRS",
        })
        claim = {
            "id": "E-SG", "claim_date": "2026-01-10", "status": "已批准待付款",
            "approved_amount": 720, "currency": "CNY", "project": "Global Game",
            "category": "差旅", "claimant": "Employee", "evidence": ["receipt"],
        }
        voucher = build_expense_vouchers([claim], "2026-01", adapter, {"USD/CNY": 7.2})[0]
        self.assertEqual(voucher["functional_amount"], 100)
        self.assertEqual(voucher["debit"][0]["account_code"], "6100")
        self.assertEqual(voucher["credit"][0]["account_code"], "2500")

    def test_only_reviewed_balanced_vouchers_post_and_are_idempotent(self):
        voucher = {"id": "V1", "date": "2026-01-31", "type": "收入", "summary": "收入", "source": "结算",
                   "debit": [{"account": "1122 应收账款", "amount": 100}],
                   "credit": [{"account": "5001 主营业务收入", "amount": 100}],
                   "balanced": True, "status": "待复核", "evidence": []}
        review = {"V1": {"decision": "接受", "actor": "会计"}}
        first = post_reviewed_vouchers([voucher], review, [], "2026-01", "会计")
        second = post_reviewed_vouchers([voucher], review, first["records"], "2026-01", "会计")
        self.assertEqual(len(first["created"]), 1)
        self.assertEqual(len(second["created"]), 0)
        trial = posted_trial_balance(first["records"], "2026-01")
        self.assertTrue(trial["balanced"])
        self.assertEqual(trial["posted_voucher_count"], 1)

    def test_same_voucher_id_can_post_separately_by_entity(self):
        voucher = {"id": "V1", "date": "2026-01-31", "type": "收入", "summary": "收入", "source": "结算",
                   "debit": [{"account": "1122", "amount": 100}],
                   "credit": [{"account": "5001", "amount": 100}], "balanced": True, "status": "待复核"}
        reviews = {"V1": {"decision": "接受", "actor": "会计"}}
        cn = post_reviewed_vouchers([voucher], reviews, [], "2026-01", "会计", "cn_studio")
        sg = post_reviewed_vouchers([voucher], reviews, cn["records"], "2026-01", "会计", "sg_publisher")
        self.assertEqual(len(sg["records"]), 2)
        self.assertEqual({row["entity_id"] for row in sg["records"]}, {"cn_studio", "sg_publisher"})

    def test_posted_voucher_cannot_be_silently_changed(self):
        voucher = {"id": "V1", "date": "2026-01-31", "summary": "收入", "debit": [{"account": "1122", "amount": 100}],
                   "credit": [{"account": "5001", "amount": 100}], "balanced": True, "status": "待复核"}
        review = {"V1": {"decision": "接受"}}
        posted = post_reviewed_vouchers([voucher], review, [], "2026-01", "会计")["records"]
        with self.assertRaises(ValueError):
            post_reviewed_vouchers([{**voucher, "summary": "被改写"}], review, posted, "2026-01", "会计")

    def test_approved_expense_claim_generates_employee_payable(self):
        claim = {"id": "E1", "claim_date": "2026-01-10", "status": "已批准待付款", "approved_amount": 100,
                 "currency": "CNY", "project": "游戏A", "category": "差旅", "claimant": "员工A", "evidence": ["发票"]}
        voucher = build_expense_vouchers([claim], "2026-01")[0]
        self.assertEqual(voucher["credit"][0]["account"], "2241 其他应付款")

    def test_closed_balance_rolls_into_next_period_opening(self):
        opening = [
            {"period": "2026-01", "account": "1002 银行存款", "account_code": "1002", "account_name": "银行存款", "category": "资产", "opening_debit": 500, "opening_credit": 0},
            {"period": "2026-01", "account": "3001 实收资本", "account_code": "3001", "account_name": "实收资本", "category": "权益", "opening_debit": 0, "opening_credit": 500},
        ]
        posted = {"balanced": True, "rows": [
            {"account": "1002 银行存款", "debit": 100, "credit": 0},
            {"account": "5001 主营业务收入", "debit": 0, "credit": 100},
        ]}
        carry = roll_forward_opening_balances(opening, posted, "2026-01", "会计")
        self.assertEqual(carry["period"], "2026-02")
        self.assertTrue(carry["balanced"])
        self.assertEqual(next(row for row in carry["records"] if row["account"].startswith("1002"))["opening_debit"], 600)
        self.assertEqual(next(row for row in carry["records"] if row["account"].startswith("3103"))["opening_credit"], 100)
        self.assertFalse(any(row["account"].startswith("5001") for row in carry["records"]))


if __name__ == "__main__":
    unittest.main()
