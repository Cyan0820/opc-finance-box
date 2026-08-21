import unittest

from src.banking import banking_payload, suggest_matches


class BankingTests(unittest.TestCase):
    def test_receipt_matches_game_settlement_by_amount_and_channel(self):
        transactions = [{
            "id": "B1", "direction": "收入", "currency": "CNY", "amount": 1000,
            "counterparty": "渠道A", "summary": "游戏甲 1月结算款", "status": "待认领",
        }]
        settlements = [{
            "id": "S1", "period": "2026-01", "game": "游戏甲", "channel": "渠道A",
            "currency": "CNY", "net_receivable": 1000,
        }]
        matched = suggest_matches(transactions, settlements, [])
        self.assertEqual(matched[0]["status"], "高置信匹配")
        self.assertEqual(matched[0]["suggested_match"]["type"], "应收到账")
        self.assertEqual(matched[0]["suggested_match"]["difference"], 0)

    def test_payment_matches_purchase_by_amount_and_vendor(self):
        transactions = [{
            "id": "B2", "direction": "支出", "currency": "CNY", "amount": 5000,
            "counterparty": "素材供应商", "summary": "视频制作款", "status": "待认领",
        }]
        purchases = [{
            "id": "P1", "po_number": "PO1", "vendor": "素材供应商", "item": "视频制作",
            "currency": "CNY", "ordered_amount": 5000, "accepted_amount": 5000,
            "invoice_amount": 5000,
        }]
        matched = suggest_matches(transactions, [], purchases)
        self.assertEqual(matched[0]["status"], "高置信匹配")
        self.assertEqual(matched[0]["suggested_match"]["type"], "应付付款")

    def test_currency_mismatch_never_auto_matches(self):
        transactions = [{
            "id": "B3", "direction": "收入", "currency": "USD", "amount": 1000,
            "counterparty": "渠道A", "summary": "游戏甲", "status": "待认领",
        }]
        settlements = [{
            "id": "S1", "period": "2026-01", "game": "游戏甲", "channel": "渠道A",
            "currency": "CNY", "net_receivable": 1000,
        }]
        matched = suggest_matches(transactions, settlements, [])
        self.assertEqual(matched[0]["status"], "待认领")

    def test_summary_keeps_receipts_and_payments_separate(self):
        payload = banking_payload([
            {"direction": "收入", "amount": 100, "currency": "CNY", "status": "高置信匹配"},
            {"direction": "支出", "amount": 40, "currency": "CNY", "status": "待认领"},
        ])
        self.assertEqual(payload["summary"]["receipt_amount"], 100)
        self.assertEqual(payload["summary"]["payment_amount"], 40)
        self.assertEqual(payload["summary"]["pending_count"], 1)

    def test_existing_allocation_reduces_next_receipt_suggestion(self):
        transactions = [{
            "id": "B4", "entity_id": "cn_studio", "direction": "收入", "currency": "CNY",
            "amount": 400, "counterparty": "渠道A", "summary": "游戏甲部分回款",
        }]
        settlements = [{
            "id": "S4", "entity_id": "cn_studio", "release_status": "released",
            "period": "2026-01", "game": "游戏甲", "channel": "渠道A",
            "currency": "CNY", "net_receivable": 1000,
        }]
        allocations = [{
            "entity_id": "cn_studio", "target_type": "receivable", "target_id": "S4",
            "amount": 600, "status": "部分核销",
        }]
        match = suggest_matches(transactions, settlements, [], allocations)[0]["suggested_match"]
        self.assertEqual(match["original_expected_amount"], 1000)
        self.assertEqual(match["allocated_before"], 600)
        self.assertEqual(match["expected_amount"], 400)
        self.assertEqual(match["suggested_allocation_amount"], 400)

    def test_cross_entity_and_unreleased_receivables_are_not_suggested(self):
        transaction = [{
            "id": "B5", "entity_id": "cn_studio", "direction": "收入", "currency": "USD",
            "amount": 100, "counterparty": "App Store", "summary": "Game settlement",
        }]
        settlements = [
            {"id": "SG", "entity_id": "sg_publisher", "release_status": "released", "game": "Game", "channel": "App Store", "currency": "USD", "net_receivable": 100},
            {"id": "CN-PENDING", "entity_id": "cn_studio", "release_status": "ready_for_review", "game": "Game", "channel": "App Store", "currency": "USD", "net_receivable": 100},
        ]
        result = suggest_matches(transaction, settlements, [])[0]
        self.assertEqual(result["status"], "待认领")
        self.assertIsNone(result["suggested_match"])


if __name__ == "__main__":
    unittest.main()
