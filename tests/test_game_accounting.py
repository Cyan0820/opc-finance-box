import unittest

from src.game_accounting import build_game_revenue_vouchers, build_revenue_recognition, create_revenue_policy, review_revenue_policy


FACTS = {"controls_pricing": True, "responsible_for_fulfillment": True, "bears_refund_risk": True, "controls_virtual_goods": True}


class GameAccountingTests(unittest.TestCase):
    def policy(self, method="按服务期直线确认", months=2, presentation="净额法"):
        policy = create_revenue_policy(
            "游戏A", "App Store", "订阅", presentation, method, "2026-01", "财务",
            ["渠道协议", "用户条款"], service_months=months, role_facts=FACTS,
        )
        return review_revenue_policy(policy, "批准", "会计", "口径与履约事实一致")

    def test_subscription_is_deferred_over_service_period(self):
        settlement = {"id": "S1", "period": "2026-01", "game": "游戏A", "channel": "App Store", "currency": "CNY", "net_receivable": 100}
        january = build_revenue_recognition([settlement], [self.policy()], "2026-01")["rows"][0]
        february = build_revenue_recognition([settlement], [self.policy()], "2026-02")["rows"][0]
        self.assertEqual(january["recognized_revenue"], 50)
        self.assertEqual(february["recognized_revenue"], 50)

    def test_consumption_method_blocks_without_consumption_data(self):
        settlement = {"id": "S1", "period": "2026-01", "game": "游戏A", "channel": "App Store", "currency": "CNY", "net_receivable": 100}
        result = build_revenue_recognition([settlement], [self.policy("按消耗确认", 0)], "2026-01")
        self.assertTrue(result["blockers"])
        self.assertEqual(result["rows"][0]["status"], "阻塞")

    def test_missing_policy_never_defaults_from_platform_name(self):
        settlement = {"id": "S1", "period": "2026-01", "game": "游戏A", "channel": "App Store", "currency": "CNY", "net_receivable": 100}
        result = build_revenue_recognition([settlement], [], "2026-01")
        self.assertEqual(len(result["rows"]), 1)
        self.assertEqual(result["rows"][0]["status"], "阻塞")
        self.assertIsNone(result["rows"][0]["recognized_revenue"])
        self.assertIn("缺少已批准", result["blockers"][0]["reason"])

    def test_foreign_revenue_uses_approved_period_rate(self):
        settlement = {"id": "S1", "period": "2026-01", "game": "游戏A", "channel": "App Store", "currency": "USD", "net_receivable": 100}
        recognition = build_revenue_recognition([settlement], [self.policy()], "2026-01")
        voucher = build_game_revenue_vouchers(recognition, "2026-01", {"USD": 7.2})[0]
        self.assertEqual(voucher["debit"][0]["amount"], 360)
        self.assertEqual(voucher["fx_rate"], 7.2)

    def test_agent_explains_gross_net_impact(self):
        policy = create_revenue_policy(
            "游戏A", "App Store", "内购", "净额法", "即时确认", "2026-01", "财务",
            ["协议"], role_facts=FACTS,
        )
        self.assertEqual(policy["agent_judgement"]["recommended_presentation"], "总额法")
        self.assertIn("毛利率", policy["agent_judgement"]["impact"])

    def test_approved_recognition_drives_voucher_amount(self):
        settlement = {"id": "S1", "period": "2026-01", "game": "游戏A", "channel": "App Store", "currency": "CNY", "net_receivable": 100}
        recognition = build_revenue_recognition([settlement], [self.policy()], "2026-01")
        voucher = build_game_revenue_vouchers(recognition, "2026-01")[0]
        self.assertEqual(voucher["original_amount"], 50)
        self.assertEqual(voucher["status"], "待复核")


if __name__ == "__main__":
    unittest.main()
