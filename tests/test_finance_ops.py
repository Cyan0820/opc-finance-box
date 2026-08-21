import unittest

from src.finance_ops import (
    build_bank_reconciliation,
    build_bank_voucher_drafts,
    build_finance_ops,
    build_purchase_voucher_drafts,
    build_payroll_voucher_drafts,
    build_tax_pack,
    build_trial_balance,
    build_voucher_drafts,
    create_bank_reconciliation_review,
)


RECORDS = [
    {
        "period": "2026-01", "scope": "国内", "game": "游戏甲", "platform": "联运",
        "channel": "渠道A", "currency": "CNY", "settlement_amount": 1000,
    },
    {
        "period": "2026-01", "scope": "海外", "game": "游戏乙", "platform": "Android",
        "channel": "Google Play", "currency": "USD", "settlement_amount": 200,
    },
]


class FinanceOpsTests(unittest.TestCase):

    def test_non_cn_entity_never_receives_cn_filing_forms(self):
        profile = {
            "entity_id": "sg_publisher", "company_name": "Singapore Publisher",
            "jurisdiction": "SG", "tax_pack": "jurisdiction.sg", "tax_readiness": "design",
        }
        finance = build_finance_ops([{
            "entity_id": "sg_publisher", "period": "2026-01", "scope": "海外",
            "game": "Pixel Odyssey", "channel": "App Store", "currency": "USD", "settlement_amount": 1000,
        }], "2026-01", company_profile=profile)
        self.assertEqual(finance["tax_pack"]["jurisdiction"], "SG")
        self.assertEqual(finance["tax_pack"]["returns_workspace"]["returns"], [])
        self.assertIn("不会生成中国税表", finance["tax_pack"]["returns_workspace"]["guardrail"])
        self.assertEqual({source["authority"] for source in finance["sources"]}, {"ACRA", "IRAS"})
        self.assertEqual(finance["ledger_adapter"]["id"], "sg-internal-ledger-v1")
        self.assertEqual(finance["ledger_adapter"]["functional_currency"], "USD")
        self.assertFalse(finance["ledger_adapter"]["posting_ready"])
        self.assertIn("内部账簿角色映射", finance["statutory_ledger_guardrail"])
        tax_task = next(row for row in finance["close"]["tasks"] if row["id"] == "C14")
        self.assertIn("SG Tax Pack", tax_task["blockers"][0])
    def test_voucher_draft_keeps_foreign_currency_blocked(self):
        vouchers = build_voucher_drafts(RECORDS, "2026-01")
        self.assertEqual(len(vouchers), 2)
        cny = next(voucher for voucher in vouchers if voucher["original_currency"] == "CNY")
        usd = next(voucher for voucher in vouchers if voucher["original_currency"] == "USD")
        self.assertTrue(cny["balanced"])
        self.assertEqual(cny["debit"][0]["amount"], 1000)
        self.assertFalse(usd["balanced"])
        self.assertIn("USD/CNY", usd["blockers"][0])

    def test_configured_fx_rate_converts_foreign_voucher(self):
        usd = next(voucher for voucher in build_voucher_drafts(RECORDS, "2026-01", {"USD": 7.2})
                   if voucher["original_currency"] == "USD")
        self.assertTrue(usd["balanced"])
        self.assertEqual(usd["debit"][0]["amount"], 1440)
        self.assertEqual(usd["fx_rate"], 7.2)

    def test_human_review_includes_a_recommendation_and_tradeoffs(self):
        payload = build_finance_ops(RECORDS, "2026-01")
        review_tasks = [task for task in payload["close"]["tasks"] if task["decision_support"]]
        self.assertTrue(review_tasks)
        for task in review_tasks:
            support = task["decision_support"]
            self.assertTrue(support["recommendation"])
            self.assertGreater(support["confidence"], 0.5)
            self.assertTrue(any(option["recommended"] for option in support["options"]))

    def test_cross_border_tax_is_not_inferred_from_currency(self):
        tax_pack = build_tax_pack(RECORDS, "2026-01")
        self.assertEqual(tax_pack["cross_border"]["status"], "需专项判断")
        self.assertIn("不能仅凭收款币种", tax_pack["cross_border"]["note"])
        vat = tax_pack["items"][0]
        self.assertIn("2026", next(
            source["effective_date"] for source in build_finance_ops(RECORDS, "2026-01")["sources"]
            if source["id"] == "vat-law-2026"
        ))
        self.assertTrue(vat["decision_support"]["recommendation"])
        self.assertFalse(tax_pack["cross_border"]["all_reviewed"])

    def test_cross_border_channel_requires_review_decision_and_reviewer(self):
        profile = {"tax_policy": {"cross_border_reviews": {"Google Play": {
            "decision": "跨境免税", "reviewer": "税务服务机构", "evidence": "合同及境外消费地证明",
        }}}}
        tax_pack = build_tax_pack(RECORDS, "2026-01", profile)
        self.assertTrue(tax_pack["cross_border"]["all_reviewed"])
        self.assertEqual(tax_pack["cross_border"]["channels"][0]["status"], "已完成")

    def test_pending_cross_border_evidence_does_not_release_close_gate(self):
        profile = {"tax_policy": {"cross_border_reviews": {"Google Play": {
            "decision": "待补证据", "reviewer": "税务服务机构",
        }}}}
        tax_pack = build_tax_pack(RECORDS, "2026-01", profile)
        self.assertFalse(tax_pack["cross_border"]["all_reviewed"])

    def test_delivered_uninvoiced_purchase_generates_accrual_draft(self):
        purchases = [{
            "order_date": "2026-01-10", "project": "游戏乙", "vendor": "供应商A",
            "category": "素材制作", "item": "视频素材", "currency": "CNY",
            "ordered_amount": 5000, "accepted_amount": None, "invoice_amount": None,
            "delivery_status": "已交付待确认", "anomalies": ["疑似已发生未开票：月结需判断暂估"],
        }]
        drafts = build_purchase_voucher_drafts(purchases, "2026-01")
        self.assertEqual(len(drafts), 1)
        self.assertEqual(drafts[0]["type"], "采购暂估")
        self.assertEqual(drafts[0]["debit"][0]["amount"], 5000)
        self.assertIn("缺少正式验收金额", drafts[0]["blockers"][0])

    def test_trial_balance_excludes_blocked_drafts(self):
        vouchers = build_voucher_drafts(RECORDS, "2026-01")
        trial = build_trial_balance(vouchers)
        self.assertTrue(trial["balanced"])
        self.assertEqual(trial["total_debit"], 1000)
        self.assertEqual(len(trial["excluded_vouchers"]), 1)

    def test_high_confidence_receipt_generates_cash_voucher(self):
        transactions = [{
            "transaction_date": "2026-01-20", "currency": "CNY", "amount": 1000,
            "status": "高置信匹配", "direction": "收入",
            "suggested_match": {
                "type": "应收到账", "target": "游戏甲 / 渠道A / 2026-01",
                "difference": 0, "recommendation": "建议自动认领",
            },
        }]
        drafts = build_bank_voucher_drafts(transactions, "2026-01")
        self.assertEqual(len(drafts), 1)
        self.assertEqual(drafts[0]["debit"][0]["account"], "1002 银行存款")
        self.assertEqual(drafts[0]["credit"][0]["account"], "1122 应收账款")
        self.assertEqual(drafts[0]["status"], "待复核")

    def test_bank_reconciliation_flags_pending_transactions(self):
        reconciliation = build_bank_reconciliation([
            {"transaction_date": "2026-01-20", "currency": "CNY", "amount": 100,
             "direction": "收入", "status": "高置信匹配", "balance": 500},
            {"transaction_date": "2026-01-21", "currency": "CNY", "amount": 20,
             "direction": "支出", "status": "待认领", "balance": 480},
        ], "2026-01")
        self.assertEqual(reconciliation["pending_count"], 1)
        self.assertEqual(reconciliation["status"], "存在未认领流水")
        self.assertEqual(reconciliation["currencies"][0]["ending_balance"], 480)

    def test_account_level_bank_reconciliation_requires_current_balanced_review(self):
        transactions = [{
            "id": "B1", "transaction_date": "2026-01-31", "account_masked": "6222****1234",
            "currency": "CNY", "amount": 100, "direction": "收入", "status": "高置信匹配",
            "balance": 600,
        }]
        review = create_bank_reconciliation_review(
            transactions, "2026-01", "6222****1234", "CNY", 600,
            actor="财务负责人", rationale="已核对银行对账单和总账余额", evidence=["银行对账单", "总账余额"],
        )
        reconciliation = build_bank_reconciliation(transactions, "2026-01", [review])
        self.assertTrue(reconciliation["complete"])
        self.assertEqual(reconciliation["accounts"][0]["status"], "已调节并确认")

    def test_latest_bank_reconciliation_review_wins_and_source_change_invalidates_it(self):
        transactions = [{
            "id": "B1", "transaction_date": "2026-01-31", "account_masked": "6222****1234",
            "currency": "CNY", "amount": 100, "direction": "收入", "status": "高置信匹配",
            "balance": 600,
        }]
        confirmed = create_bank_reconciliation_review(
            transactions, "2026-01", "6222****1234", "CNY", 600,
            actor="财务负责人", rationale="已核对银行对账单和总账余额", evidence=["银行对账单"],
        )
        returned = create_bank_reconciliation_review(
            transactions, "2026-01", "6222****1234", "CNY", 600, decision="退回",
            actor="外部会计", rationale="未达项支持资料仍然不够完整", evidence=["复核清单"],
        )
        reconciliation = build_bank_reconciliation(transactions, "2026-01", [confirmed, returned])
        self.assertFalse(reconciliation["complete"])
        self.assertEqual(reconciliation["accounts"][0]["status"], "已退回待重做")
        changed = [{**transactions[0], "balance": 601}]
        invalidated = build_bank_reconciliation(changed, "2026-01", [returned])
        self.assertEqual(invalidated["accounts"][0]["status"], "复核已失效")

    def test_bank_reconciliation_rejects_signed_unpresented_amounts(self):
        transactions = [{
            "id": "B1", "transaction_date": "2026-01-31", "account_masked": "6222****1234",
            "currency": "CNY", "amount": 100, "direction": "收入", "status": "高置信匹配",
            "balance": 600,
        }]
        with self.assertRaisesRegex(ValueError, "非负金额"):
            create_bank_reconciliation_review(
                transactions, "2026-01", "6222****1234", "CNY", 600,
                deposits_in_transit=-1, actor="财务负责人",
                rationale="已核对银行对账单和总账余额", evidence=["银行对账单"],
            )

    def test_payroll_voucher_balances_net_tax_and_personal_deductions(self):
        drafts = build_payroll_voucher_drafts([{
            'department': '研发部', 'project': '', 'gross_salary': 20_000,
            'social_security': 1_500, 'housing_fund': 1_000,
            'calculated_iit': 500, 'net_salary': 17_000, 'rd_salary_candidate': 0,
        }], '2026-01')
        self.assertEqual(len(drafts), 1)
        self.assertTrue(drafts[0]['balanced'])
        self.assertEqual(sum(line['amount'] for line in drafts[0]['credit']), 20_000)

    def test_payroll_voucher_only_uses_selected_period(self):
        rows = [
            {"period": "2026-01", "department": "研发部", "gross_salary": 100,
             "net_salary": 100, "calculated_iit": 0},
            {"period": "2026-02", "department": "研发部", "gross_salary": 200,
             "net_salary": 200, "calculated_iit": 0},
        ]
        drafts = build_payroll_voucher_drafts(rows, "2026-02")
        self.assertEqual(drafts[0]["original_amount"], 200)


if __name__ == "__main__":
    unittest.main()
