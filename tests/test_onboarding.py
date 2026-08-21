import unittest

from src.onboarding import build_onboarding


class OnboardingTests(unittest.TestCase):
    def test_readiness_explains_dependencies(self):
        profile = {"company_name": "测试", "credit_code": "913", "registered_city": "上海", "base_currency": "CNY", "accounting_standard": "小企业会计准则", "vat_taxpayer_type": "一般纳税人", "vat_filing_frequency": "月度", "external_accountant": {"provider": "代账"}}
        datasets = {name: [] for name in ("master_records", "game_kpis", "opening_balances", "settlements", "purchases", "bank_transactions", "invoices", "payroll_rows", "plan_lines")}
        result = build_onboarding(profile, datasets)
        self.assertFalse(result["ready_for_bp"])
        self.assertIn("游戏/项目主数据", result["blockers"])
        self.assertEqual(result["import_order"][0]["sheet"], "主体配置")


if __name__ == "__main__":
    unittest.main()
