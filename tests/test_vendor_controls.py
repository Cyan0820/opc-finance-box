import unittest

from src.vendor_controls import (
    approved_vendor_bank_accounts, create_vendor_bank_change, decide_vendor_bank_change,
)


class VendorBankControlTests(unittest.TestCase):
    def test_full_account_is_hashed_and_never_persisted(self):
        item = create_vendor_bank_change(
            entity_id="cn_studio", vendor="美术供应商", beneficiary_name="美术供应商有限公司",
            bank_name="测试银行", bank_country="CN", currency="CNY", account_number="6222 1234 5678 9012",
            requester="采购经办", evidence=["盖章账户函", "主数据联系人电话"],
        )
        self.assertEqual(item["status"], "待批准")
        self.assertEqual(item["account_masked"], "•••• 9012")
        self.assertNotIn("account_number", item)
        self.assertNotIn("6222123456789012", str(item))

    def test_independent_verification_approves_and_supersedes_old_account(self):
        old = create_vendor_bank_change(
            entity_id="sg_publisher", vendor="Global Art", beneficiary_name="Global Art Ltd",
            bank_name="Bank A", bank_country="SG", currency="USD", account_number="SG0011223344",
            requester="Buyer", evidence=["Signed letter", "Master contact"],
        )
        rows, old = decide_vendor_bank_change(
            [old], old["id"], "批准", "Finance", "Matched beneficiary and bank proof",
            "银行证明核对", "Bank letter REF-001",
        )
        new = create_vendor_bank_change(
            entity_id="sg_publisher", vendor="Global Art", beneficiary_name="Global Art Ltd",
            bank_name="Bank B", bank_country="SG", currency="USD", account_number="SG0099887766",
            requester="Buyer", evidence=["Signed change letter", "Master contact"], change_type="变更",
            previous_account_id=old["id"], existing_records=rows,
        )
        rows, approved = decide_vendor_bank_change(
            [*rows, new], new["id"], "批准", "Finance", "Called the master contact and checked bank letter",
            "回拨主数据联系人", "Call log REF-002",
        )
        self.assertEqual(approved["status"], "已批准")
        self.assertEqual(next(row for row in rows if row["id"] == old["id"])["status"], "已停用")
        self.assertEqual(len(approved_vendor_bank_accounts(rows, entity_id="sg_publisher", vendor="Global Art", currency="USD")), 1)

    def test_requester_cannot_approve_own_bank_change(self):
        item = create_vendor_bank_change(
            entity_id="cn_studio", vendor="供应商", beneficiary_name="供应商公司", bank_name="银行",
            bank_country="CN", currency="CNY", account_number="1234567890", requester="采购",
            evidence=["账户函", "联系人"],
        )
        with self.assertRaisesRegex(ValueError, "申请人以外"):
            decide_vendor_bank_change([item], item["id"], "批准", "采购", "本人确认账户真实有效", "银行证明核对", "证明编号123456")


if __name__ == "__main__":
    unittest.main()
