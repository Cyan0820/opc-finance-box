import io
import unittest
import zipfile

from src.tax_filing_assist import build_filing_assist, build_filing_assist_package
from src.tax_workflow import review_tax_form


def workspace(pilot="待确认"):
    return {
        "entity_id": "cn_studio", "company_name": "上海游戏工作室", "period": "2026-07",
        "filing_profile": {"registered_city": "上海市徐汇区", "shanghai_vat_pilot_status": pilot},
        "returns": [{
            "form_code": "VAT-RETURN", "name": "增值税申报表", "version": "2026年第6号",
            "status": "待复核", "blockers": [], "fields": [{
                "code": "VAT-OUTPUT", "name": "销项税额", "value": 600, "status": "候选", "source": "销项台账",
            }], "official_source": "https://example.test", "review_role": "税务负责人",
        }],
    }


class TaxFilingAssistTests(unittest.TestCase):
    def test_shanghai_pilot_identity_must_be_confirmed(self):
        assist = build_filing_assist(workspace())
        self.assertEqual(assist["forms"][0]["contract_id"], "VAT-NATIONAL-GENERAL-2026-02")
        self.assertTrue(any("试点身份未确认" in item for item in assist["forms"][0]["blockers"]))

    def test_confirmed_pilot_selects_trial_contract(self):
        assist = build_filing_assist(workspace("已纳入试点"))
        self.assertEqual(assist["forms"][0]["contract_id"], "VAT-SH-PILOT-2026-06")

    def test_review_is_current_only_for_same_fingerprint(self):
        original = workspace("未纳入试点")
        review = review_tax_form(original, [], "VAT-RETURN", "同意草稿", "税务负责人", "逐栏核对候选值与证据")
        self.assertTrue(build_filing_assist(original, [review])["forms"][0]["review_current"])
        changed = workspace("未纳入试点")
        changed["returns"][0]["fields"][0]["value"] = 601
        self.assertFalse(build_filing_assist(changed, [review])["forms"][0]["review_current"])

    def test_package_contains_contract_mapping_and_receipt_template(self):
        current = workspace("未纳入试点")
        body = build_filing_assist_package(current, build_filing_assist(current))
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            names = set(archive.namelist())
        self.assertTrue(any(name.endswith("税务申报工作底稿.xlsx") for name in names))
        self.assertIn("02_表单契约与释放检查.json", names)
        self.assertIn("03_字段到官方栏次映射.csv", names)
        self.assertIn("04_回执登记模板.csv", names)


if __name__ == "__main__":
    unittest.main()
