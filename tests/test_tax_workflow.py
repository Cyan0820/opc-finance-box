import unittest

from src.tax_workflow import build_tax_delivery, record_tax_submission, review_tax_form


WORKSPACE = {"period": "2026-02", "returns": [{
    "form_code": "VAT-RETURN", "name": "增值税申报表", "version": "2026年第6号",
    "status": "待复核", "blockers": [], "official_source": "https://example.test",
    "agent_position": "逐渠道判断", "review_role": "税务机构",
}]}


class TaxWorkflowTests(unittest.TestCase):
    def test_review_and_receipt_drive_real_submission_status(self):
        review = review_tax_form(WORKSPACE, [], "VAT-RETURN", "同意草稿", "税务负责人", "已核对销项进项和跨境证据")
        submitted = record_tax_submission(review, "申报成功", "有权申报人", "RECEIPT-001", ["回执.pdf"])
        delivery = build_tax_delivery(WORKSPACE, [submitted])
        self.assertTrue(delivery["complete"])
        self.assertEqual(delivery["submitted_count"], 1)

    def test_blocked_form_cannot_be_approved(self):
        blocked = {**WORKSPACE, "returns": [{**WORKSPACE["returns"][0], "blockers": ["缺销售分类"]}]}
        with self.assertRaises(ValueError):
            review_tax_form(blocked, [], "VAT-RETURN", "同意草稿", "税务负责人", "资料看起来没有问题")

    def test_export_or_review_does_not_claim_submission(self):
        review = review_tax_form(WORKSPACE, [], "VAT-RETURN", "同意草稿", "税务负责人", "已核对销项进项和跨境证据")
        delivery = build_tax_delivery(WORKSPACE, [review])
        self.assertFalse(delivery["complete"])
        self.assertEqual(delivery["forms"][0]["submission_status"], "未提交")

    def test_success_requires_receipt_evidence(self):
        review = review_tax_form(WORKSPACE, [], "VAT-RETURN", "同意草稿", "税务负责人", "已核对销项进项和跨境证据")
        with self.assertRaises(ValueError):
            record_tax_submission(review, "申报成功", "有权申报人", "RECEIPT-001", [])

    def test_review_id_is_scoped_by_legal_entity(self):
        cn = review_tax_form({**WORKSPACE, "entity_id": "cn_studio"}, [], "VAT-RETURN", "同意草稿", "税务负责人", "已核对销项进项和跨境证据")
        sg = review_tax_form({**WORKSPACE, "entity_id": "sg_publisher"}, [], "VAT-RETURN", "同意草稿", "税务负责人", "已核对销项进项和跨境证据")
        self.assertNotEqual(cn["id"], sg["id"])

    def test_delivery_does_not_reuse_another_entity_review(self):
        cn_workspace = {**WORKSPACE, "entity_id": "cn_studio"}
        sg_review = review_tax_form({**WORKSPACE, "entity_id": "sg_publisher"}, [], "VAT-RETURN", "同意草稿", "税务负责人", "已核对销项进项和跨境证据")
        delivery = build_tax_delivery(cn_workspace, [sg_review])
        self.assertEqual(delivery["entity_id"], "cn_studio")
        self.assertEqual(delivery["forms"][0]["review_status"], "未复核")


if __name__ == "__main__":
    unittest.main()
