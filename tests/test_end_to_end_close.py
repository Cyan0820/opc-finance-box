import unittest

from src.close_control import assess_close
from src.company_profile import profile_gaps
from src.finance_ops import build_finance_ops, create_bank_reconciliation_review


PROFILE = {
    "company_name": "匿名正向现金流游戏公司", "credit_code": "91310000TEST000001",
    "registered_city": "上海市", "base_currency": "CNY", "accounting_standard": "小企业会计准则",
    "vat_taxpayer_type": "一般纳税人", "vat_filing_frequency": "月度",
    "external_accountant": {"provider": "外部会计服务机构"},
    "tax_policy": {"shanghai_vat_pilot_status": "未纳入试点"},
    "asset_policy": {"monthly_attestation": {"2026-01": "无新增及处置"}},
    "fx_policy": {"month_end_rates": {"2026-01": {}}},
}


class EndToEndCloseTests(unittest.TestCase):
    def _finance(self):
        settlements = [{
            "id": "S1", "period": "2026-01", "scope": "国内", "game": "游戏甲",
            "channel": "渠道A", "currency": "CNY", "settlement_amount": 1000,
        }]
        purchases = [{
            "id": "P1", "order_date": "2026-01-10", "project": "游戏甲", "vendor": "供应商A",
            "category": "素材制作", "item": "素材", "currency": "CNY", "ordered_amount": 100,
            "accepted_amount": 100, "invoice_amount": 100, "paid_amount": 100,
            "delivery_status": "已验收", "anomalies": [],
        }]
        bank = [
            {"id": "B1", "transaction_date": "2026-01-20", "account_masked": "6222****0001", "currency": "CNY", "amount": 1000,
             "direction": "收入", "status": "高置信匹配", "suggested_match": {
                 "type": "应收到账", "target": "游戏甲 / 渠道A / 2026-01", "difference": 0,
             }},
            {"id": "B2", "transaction_date": "2026-01-31", "account_masked": "6222****0001", "currency": "CNY", "amount": 100,
             "balance": 1400,
             "direction": "支出", "status": "高置信匹配", "suggested_match": {
                 "type": "应付付款", "target": "供应商A / 素材 / P1", "difference": 0,
             }},
        ]
        invoices = [{"invoice_date": "2026-01-15", "anomalies": [], "verification_status": "已查验"}]
        payroll = [{
            "period": "2026-01", "department": "研发", "project": "",
            "gross_salary": 200, "social_security": 10, "housing_fund": 10,
            "calculated_iit": 0, "net_salary": 180, "anomalies": [],
        }]
        opening = [
            {"period": "2026-01", "account": "1002 银行存款", "account_code": "1002",
             "account_name": "银行存款", "category": "资产", "opening_debit": 500, "opening_credit": 0},
            {"period": "2026-01", "account": "3001 实收资本", "account_code": "3001",
             "account_name": "实收资本", "category": "权益", "opening_debit": 0, "opening_credit": 500},
        ]
        bank_review = create_bank_reconciliation_review(
            bank, "2026-01", "6222****0001", "CNY", 1400,
            actor="财务负责人", rationale="已核对银行期末余额和总账余额", evidence=["银行对账单", "总账余额"],
        )
        return build_finance_ops(
            settlements, "2026-01", purchases, bank, invoices, payroll, PROFILE, opening,
            bank_reconciliation_reviews=[bank_review],
        )

    def test_complete_company_can_reach_human_close_gate(self):
        finance = self._finance()
        self.assertEqual(finance["close"]["blocked"], 0)
        self.assertEqual(next(task for task in finance["close"]["tasks"] if task["id"] == "C15")["status"], "待处理")
        state = {"voucher_reviews": {
            voucher["id"]: {"decision": "接受"} for voucher in finance["vouchers"]
            if voucher["balanced"] and voucher["status"] != "阻塞"
        }}
        assessment = assess_close(finance, state, profile_gaps(PROFILE))
        self.assertTrue(assessment["can_close"])

    def test_unreviewed_vouchers_block_close_with_plain_reason(self):
        assessment = assess_close(self._finance(), {"voucher_reviews": {}}, [])
        self.assertFalse(assessment["can_close"])
        self.assertTrue(any("凭证未接受" in blocker for blocker in assessment["blockers"]))

    def test_missing_asset_attestation_blocks_close(self):
        profile = {**PROFILE, "asset_policy": {"monthly_attestation": {}}}
        finance = build_finance_ops([], "2026-01", company_profile=profile)
        asset = next(task for task in finance["close"]["tasks"] if task["id"] == "C09")
        self.assertEqual(asset["status"], "阻塞")


if __name__ == "__main__":
    unittest.main()
