import unittest

from src.business_flows import build_receivables_register
from src.finance_ops import build_finance_ops
from src.revenue_close import prepare_settlement_candidates, review_settlement_candidates


def settlement(**patch):
    row = {
        "id": "S1", "entity_id": "cn_studio", "period": "2026-07",
        "game": "星海远征", "platform": "iOS", "channel": "App Store 中国区",
        "currency": "CNY", "gross": 1000, "refunds": 0, "share_base": 1000,
        "share_rate": 0.7, "settlement_amount": 700, "withholding_tax": 0,
        "net_receivable": 700, "anomalies": [], "evidence": {"channel_net": 1000},
    }
    row.update(patch)
    return row


def channel_rule(**patch):
    row = {
        "id": "CH1", "entity_id": "cn_studio", "record_type": "channel",
        "code": "APP-CN", "name": "App Store 中国区", "project_code": "G001",
        "platform": "iOS", "currency": "CNY", "share_rate": 0.7,
        "settlement_formula": "share_base_x_rate", "contract_reference": "evidence://contracts/app-cn-2026",
        "payment_days": 45, "effective_period": "2026-01", "active": True,
    }
    row.update(patch)
    return row


MASTER = [
    {"id": "G1", "entity_id": "cn_studio", "record_type": "game", "code": "G001", "name": "星海远征", "active": True},
    channel_rule(),
]


class RevenueCloseTests(unittest.TestCase):
    def test_matching_contract_and_formula_create_reviewable_candidate_not_receivable(self):
        candidate = prepare_settlement_candidates([settlement()], MASTER)[0]
        self.assertEqual(candidate["release_status"], "ready_for_review")
        self.assertEqual(candidate["contract_match"]["rule_id"], "CH1")
        self.assertTrue(candidate["commercial_reconciliation"]["passed"])
        self.assertEqual(build_receivables_register([candidate], [])["rows"], [])

    def test_missing_contract_or_contract_difference_fail_closed(self):
        missing = prepare_settlement_candidates([settlement()], MASTER[:1])[0]
        self.assertEqual(missing["release_status"], "blocked")
        self.assertIn("缺少已生效渠道规则", "；".join(missing["release_blockers"]))
        mismatch = prepare_settlement_candidates(
            [settlement(settlement_amount=650, net_receivable=650)], MASTER,
        )[0]
        self.assertEqual(mismatch["release_status"], "blocked")
        self.assertIn("结算公式差异", "；".join(mismatch["release_blockers"]))

    def test_review_releases_receivable_with_control_fingerprint(self):
        candidate = prepare_settlement_candidates([settlement()], MASTER)[0]
        updated, released = review_settlement_candidates(
            [candidate], ["S1"], "批准", "业务负责人", "已核对渠道协议及结算后台",
        )
        self.assertEqual(updated[0]["release_status"], "released")
        self.assertEqual(released[0]["status"], "已核对")
        self.assertEqual(len(released[0]["commercial_control_fingerprint"]), 64)
        receivable = build_receivables_register(released, [])["rows"][0]
        self.assertEqual(receivable["outstanding"], 700)

    def test_blocked_candidate_cannot_be_approved(self):
        candidate = prepare_settlement_candidates([settlement()], MASTER[:1])[0]
        with self.assertRaisesRegex(ValueError, "仍有阻塞"):
            review_settlement_candidates(
                [candidate], ["S1"], "批准", "业务负责人", "先忽略规则继续入账",
            )

    def test_unreleased_candidate_is_excluded_from_finance_ops(self):
        candidate = prepare_settlement_candidates([settlement()], MASTER)[0]
        finance = build_finance_ops([candidate], "2026-07", company_profile={"entity_id": "cn_studio"})
        self.assertEqual(finance["data_coverage"]["settlement_records"], 0)
        self.assertFalse(any(voucher["type"] == "收入结算" for voucher in finance["vouchers"]))


if __name__ == "__main__":
    unittest.main()
