import io
import json
import unittest
import zipfile

from src.accountant_pack import build_accountant_pack
from src.finance_ops import build_finance_ops


class AccountantPackTests(unittest.TestCase):
    def test_review_pack_contains_rules_judgement_and_audit(self):
        profile = {"entity_id": "cn_studio", "company_name": "测试游戏公司"}
        finance = build_finance_ops([{
            "period": "2026-01", "scope": "国内", "game": "游戏甲", "channel": "渠道A",
            "currency": "CNY", "settlement_amount": 1000, "entity_id": "cn_studio",
        }], "2026-01", company_profile=profile)
        voucher_id = finance["vouchers"][0]["id"]
        body = build_accountant_pack(
            finance,
            {"status": "复核中", "voucher_reviews": {voucher_id: {
                "decision": "接受", "actor": "财务负责人", "rationale": "与上期一致",
            }}},
            [{
                "action": "VOUCHER_REVIEW", "target": voucher_id,
                "detail": {"entity_id": "cn_studio", "period": "2026-01"},
            }],
            {"settlements": [{"entity_id": "cn_studio"}]}, profile,
            entity={
                "id": "cn_studio", "name": "测试游戏公司", "jurisdiction": "CN",
                "functional_currency": "CNY", "accounting_basis": "PRC_GAAP",
            },
        )
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            names = archive.namelist()
            self.assertIn("03_凭证草稿.csv", names)
            self.assertIn("06_规则版本.json", names)
            self.assertIn("00_交付清单.json", names)
            self.assertIn("13_银行余额调节.json", names)
            manifest = json.loads(archive.read("00_交付清单.json"))
            self.assertEqual(manifest["entity_id"], "cn_studio")
            self.assertEqual(manifest["data_scope"], "single_legal_entity")
            csv_text = archive.read("03_凭证草稿.csv").decode("utf-8-sig")
            self.assertIn("Agent建议", csv_text)
            self.assertIn("与上期一致", csv_text)
            audit = json.loads(archive.read("07_审计日志.json"))
            self.assertEqual(audit[0]["action"], "VOUCHER_REVIEW")

    def test_pack_excludes_other_entity_dataset_and_audit_rows(self):
        profile = {"entity_id": "cn_studio", "company_name": "中国主体"}
        finance = build_finance_ops([{
            "entity_id": "cn_studio", "period": "2026-01", "scope": "国内",
            "game": "游戏甲", "channel": "iOS App Store", "currency": "CNY", "settlement_amount": 1000,
        }], "2026-01", company_profile=profile)
        body = build_accountant_pack(
            finance, {"entity_id": "cn_studio", "status": "开放", "voucher_reviews": {}},
            [
                {"action": "CN", "target": "cn_studio:2026-01", "detail": {"entity_id": "cn_studio", "period": "2026-01"}},
                {"action": "SG", "target": "sg_publisher:2026-01", "detail": {"entity_id": "sg_publisher", "period": "2026-01"}},
            ],
            {"settlements": [
                {"entity_id": "cn_studio", "id": "CN-1"}, {"entity_id": "sg_publisher", "id": "SG-1"},
            ]}, profile,
            entity={"id": "cn_studio", "name": "中国主体", "jurisdiction": "CN"},
        )
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            audit = json.loads(archive.read("07_审计日志.json"))
            self.assertEqual([row["action"] for row in audit], ["CN"])
            listing = archive.read("08_数据集清单.csv").decode("utf-8-sig")
            self.assertIn("settlements,1", listing)


if __name__ == "__main__":
    unittest.main()
