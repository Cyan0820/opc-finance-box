import unittest

from src.game_portfolio import build_game_collection_portfolio


class GameCollectionPortfolioTests(unittest.TestCase):
    def test_same_game_groups_entities_but_keeps_currency_and_ownership(self):
        datasets = {
            "settlements": [
                {"id": "CN-S", "entity_id": "cn_studio", "game": "国服名", "period": "2026-01", "currency": "CNY", "net_receivable": 100, "management_game_id": "GAME-1", "management_game_name": "同一游戏"},
                {"id": "SG-S", "entity_id": "sg_publisher", "game": "Global Name", "period": "2026-01", "currency": "USD", "net_receivable": 50, "management_game_id": "GAME-1", "management_game_name": "同一游戏"},
            ],
            "cash_allocations": [
                {"id": "CN-A", "entity_id": "cn_studio", "transaction_id": "CN-B", "target_type": "receivable", "target_id": "CN-S", "currency": "CNY", "amount": 40, "status": "部分核销"},
                {"id": "SG-A", "entity_id": "sg_publisher", "transaction_id": "SG-B", "target_type": "receivable", "target_id": "SG-S", "currency": "USD", "amount": 50, "status": "已核销"},
            ],
            "master_records": [], "collection_actions": [],
        }
        result = build_game_collection_portfolio(datasets, as_of="2026-02-01")
        self.assertEqual(result["summary"]["game_count"], 1)
        self.assertEqual(result["summary"]["multi_entity_game_count"], 1)
        self.assertEqual({row["entity_id"] for row in result["rows"]}, {"cn_studio", "sg_publisher"})
        self.assertEqual({row["currency"] for row in result["rows"]}, {"CNY", "USD"})
        cn = next(row for row in result["rows"] if row["entity_id"] == "cn_studio")
        sg = next(row for row in result["rows"] if row["entity_id"] == "sg_publisher")
        self.assertEqual((cn["received"], cn["outstanding"]), (40, 60))
        self.assertEqual((sg["received"], sg["outstanding"]), (50, 0))
        self.assertFalse(result["books_merged"])
        self.assertTrue(result["statutory_actions_require_entity"])

    def test_same_target_id_never_cross_applies_between_entities(self):
        result = build_game_collection_portfolio({
            "settlements": [
                {"id": "SAME", "entity_id": "cn_studio", "game": "G", "period": "2026-01", "currency": "CNY", "net_receivable": 100},
                {"id": "SAME", "entity_id": "sg_publisher", "game": "G", "period": "2026-01", "currency": "CNY", "net_receivable": 200},
            ],
            "cash_allocations": [{"entity_id": "cn_studio", "target_type": "receivable", "target_id": "SAME", "amount": 100, "status": "已核销"}],
        }, as_of="2026-02-01")
        values = {row["entity_id"]: row for row in result["rows"]}
        self.assertEqual(values["cn_studio"]["outstanding"], 0)
        self.assertEqual(values["sg_publisher"]["outstanding"], 200)

    def test_valid_promise_scenario_rolls_up_without_overwriting_baseline(self):
        datasets = {
            "settlements": [{
                "id": "S", "entity_id": "cn_studio", "game": "G", "period": "2026-02",
                "currency": "CNY", "net_receivable": 1000,
            }],
            "cash_allocations": [],
            "collection_actions": [{
                "entity_id": "cn_studio", "settlement_id": "S", "action_type": "回款承诺",
                "promised_date": "2026-04-15", "promised_amount": 350,
                "recorded_at": "2026-03-10T00:00:00+00:00", "owner": "发行",
            }],
        }
        result = build_game_collection_portfolio(datasets, as_of="2026-03-31")
        row = result["rows"][0]
        self.assertEqual(row["outstanding"], 1000)
        self.assertEqual(row["effective_promised_amount"], 350)
        self.assertEqual(row["promise_scenario_outstanding"], 650)
        self.assertFalse(result["promise_scenario_overwrites_baseline"])
        self.assertEqual(result["summary"]["valid_promise_count"], 1)


if __name__ == "__main__":
    unittest.main()
