import unittest

from src.tax_returns import build_tax_returns


class TaxReturnsTests(unittest.TestCase):
    def setUp(self):
        self.profile = {
            "company_name": "测试游戏公司", "credit_code": "91310000TEST000001", "registered_city": "上海市",
            "vat_taxpayer_type": "一般纳税人", "vat_filing_frequency": "月度",
            "cit_filing_frequency": "季度", "cit_collection_method": "查账征收",
            "micro_enterprise_candidate": "待核验", "accounting_standard": "小企业会计准则",
            "fx_policy": {"month_end_rates": {"2026-07": {"USD": 7.2}}},
        }

    def test_workspace_contains_core_returns_and_never_claims_direct_upload(self):
        workspace = build_tax_returns([], "2026-07", self.profile)
        self.assertEqual(workspace["summary"]["form_count"], 5)
        self.assertEqual(workspace["summary"]["direct_upload_ready"], 0)
        self.assertEqual({row["form_code"] for row in workspace["returns"]}, {
            "VAT-RETURN", "A200000", "A01103", "IIT-WITHHOLD", "FIN-STATEMENTS",
        })

    def test_settlement_is_only_a_vat_candidate(self):
        workspace = build_tax_returns([
            {"period": "2026-07", "currency": "CNY", "settlement_amount": 1000, "scope": "国内"},
            {"period": "2026-07", "currency": "USD", "settlement_amount": 100, "scope": "海外"},
        ], "2026-07", self.profile, cross_border={"all_reviewed": True})
        vat = workspace["returns"][0]
        candidate = next(field for field in vat["fields"] if field["code"] == "VAT-SALES-CAND")
        self.assertEqual(candidate["value"], 1720)
        self.assertEqual(candidate["status"], "候选")
        self.assertIn("不可直接上传", vat["transport"])

    def test_stamp_tax_uses_purchases_only_as_candidates(self):
        workspace = build_tax_returns([], "2026-07", self.profile, purchases=[{
            "po_number": "PO-1", "item": "美术外包", "vendor": "工作室", "order_date": "2026-07-03",
        }])
        stamp = next(row for row in workspace["returns"] if row["form_code"] == "A01103")
        self.assertEqual(len(stamp["schedules"]), 1)
        self.assertEqual(stamp["schedules"][0]["tax_item"], "待按合同实质分类")
        self.assertIn("属地", stamp["transport"])

    def test_iit_workpaper_preserves_masked_identity(self):
        workspace = build_tax_returns([], "2026-07", self.profile, payroll_rows=[{
            "period": "2026-07", "employee_masked": "员工-abcd1234", "gross_salary": 20000,
            "calculated_iit": 500, "special_deduction": 1000, "other_deduction": 0, "anomalies": [],
        }])
        iit = next(row for row in workspace["returns"] if row["form_code"] == "IIT-WITHHOLD")
        self.assertEqual(iit["schedules"][0]["employee"], "员工-abcd1234")
        self.assertIn("实名", "".join(iit["blockers"]))

    def test_vat_form_version_changes_at_2026_february(self):
        january = build_tax_returns([], "2026-01", self.profile)["returns"][0]
        february = build_tax_returns([], "2026-02", self.profile)["returns"][0]
        self.assertIn("2025年第2号", january["version"])
        self.assertIn("2026年第6号", february["version"])

    def test_new_a200000_contains_payroll_and_export_supplementary_fields(self):
        workspace = build_tax_returns([], "2026-07", self.profile, payroll_rows=[{
            "period": "2026-07", "gross_salary": 100, "calculated_iit": 1, "anomalies": [],
        }])
        cit = next(row for row in workspace["returns"] if row["form_code"] == "A200000")
        codes = {field["code"] for field in cit["fields"]}
        self.assertIn("CIT-PAYROLL-COST", codes)
        self.assertIn("CIT-EXPORT-MODE", codes)
        self.assertIn("CIT-L01-REVENUE", codes)
        self.assertIn("CIT-L17-NONOP-EXPENSE", codes)


if __name__ == "__main__":
    unittest.main()
