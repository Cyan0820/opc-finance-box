import json
import tempfile
import unittest
from pathlib import Path

from src.ledger_store import LedgerStore


class LedgerStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = LedgerStore(Path(self.temp.name) / "ledger")

    def tearDown(self):
        self.temp.cleanup()

    def test_save_and_reload_dataset_with_audit(self):
        self.store.save_dataset("settlements", [{"id": "A", "period": "2026-01"}], "测试人", "单元测试")
        self.assertEqual(self.store.load_dataset("settlements")[0]["id"], "A")
        event = self.store.audit_events()[0]
        self.assertEqual(event["action"], "SAVE_DATASET")
        self.assertEqual(event["detail"]["record_count"], 1)

    def test_upsert_keeps_history_and_updates_same_id(self):
        self.store.upsert_dataset("purchases", [{"id": "A", "amount": 10}])
        self.store.upsert_dataset("purchases", [{"id": "B", "amount": 20}, {"id": "A", "amount": 15}])
        rows = {row["id"]: row for row in self.store.load_dataset("purchases")}
        self.assertEqual(set(rows), {"A", "B"})
        self.assertEqual(rows["A"]["amount"], 15)

    def test_upsert_same_business_id_is_isolated_by_legal_entity(self):
        self.store.upsert_dataset("purchases", [
            {"id": "PO-001", "entity_id": "cn_studio", "amount": 10},
        ])
        self.store.upsert_dataset("purchases", [
            {"id": "PO-001", "entity_id": "sg_publisher", "amount": 20},
        ])
        rows = self.store.load_dataset("purchases")
        self.assertEqual(len(rows), 2)
        self.assertEqual({row["entity_id"] for row in rows}, {"cn_studio", "sg_publisher"})

    def test_dataset_audit_records_legal_entity_scope(self):
        self.store.save_dataset("invoices", [
            {"id": "INV-1", "entity_id": "cn_studio"},
            {"id": "INV-2", "entity_id": "sg_publisher"},
        ])
        self.assertEqual(
            self.store.audit_events()[0]["detail"]["entity_ids"],
            ["cn_studio", "sg_publisher"],
        )

    def test_review_is_persistent_and_auditable(self):
        review = self.store.record_review("2026-01", "REV-202601-001", "接受", "财务负责人", "口径与上期一致")
        self.assertEqual(review["decision"], "接受")
        state = self.store.load_period("2026-01")
        self.assertEqual(state["voucher_reviews"]["REV-202601-001"]["rationale"], "口径与上期一致")
        self.assertIn("VOUCHER_REVIEW", [event["action"] for event in self.store.audit_events()])

    def test_period_path_traversal_is_rejected(self):
        with self.assertRaises(ValueError):
            self.store.load_period("../../etc/passwd")

    def test_same_period_is_isolated_by_legal_entity(self):
        self.store.save_period("2026-01", {"status": "已关账"}, "负责人A", "cn_studio")
        cn = self.store.load_period("2026-01", "cn_studio")
        sg = self.store.load_period("2026-01", "sg_publisher")
        self.assertEqual(cn["status"], "已关账")
        self.assertEqual(cn["entity_id"], "cn_studio")
        self.assertEqual(sg["status"], "开放")
        self.assertEqual(sg["entity_id"], "sg_publisher")

    def test_voucher_reviews_with_same_id_do_not_cross_entities(self):
        self.store.record_review(
            "2026-01", "REV-202601-001", "接受", "中国会计", entity_id="cn_studio",
        )
        self.assertIn("REV-202601-001", self.store.load_period("2026-01", "cn_studio")["voucher_reviews"])
        self.assertNotIn("REV-202601-001", self.store.load_period("2026-01", "sg_publisher")["voucher_reviews"])

    def test_entity_id_path_traversal_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "主体"):
            self.store.load_period("2026-01", "../other")

    def test_dataset_file_is_valid_json(self):
        self.store.save_dataset("invoices", [{"id": "INV-1"}])
        path = Path(self.temp.name) / "ledger" / "datasets" / "invoices.json"
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["record_count"], 1)


if __name__ == "__main__":
    unittest.main()
