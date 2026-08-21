import unittest

from src.demo_scenarios import build_demo_payload, build_group_demo_payload, load_demo_scenarios
from src.server import DEMO_SCENARIOS


class DemoScenarioTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scenarios = load_demo_scenarios(DEMO_SCENARIOS)

    def test_two_named_scenarios_exist(self):
        self.assertEqual(set(self.scenarios), {"domestic", "overseas"})
        self.assertEqual(self.scenarios["domestic"]["label"], "国服示例")
        self.assertEqual(self.scenarios["overseas"]["label"], "海外示例")

    def test_domestic_scenario_has_full_finance_chain(self):
        payload = build_demo_payload(self.scenarios["domestic"])
        self.assertTrue(payload["settlements"]["records"])
        self.assertTrue(payload["procurement"]["records"])
        self.assertTrue(payload["banking"]["transactions"])
        self.assertTrue(payload["invoices"]["records"])
        self.assertTrue(payload["payroll"]["records"])
        self.assertEqual(payload["finance_ops"]["tax_pack"]["overseas_settlement_records"], 0)
        ios = [row for row in payload["settlements"]["records"] if row["platform"] == "iOS"]
        self.assertEqual(len(ios), 2)
        self.assertTrue(all(row["channel"] == "App Store 中国区" and row["currency"] == "CNY" for row in ios))
        self.assertTrue(any(row["counterparty"] == "App Store 中国区" for row in payload["banking"]["transactions"]))
        self.assertTrue(payload["business_flows"]["payment_request_records"])
        self.assertTrue(payload["datasets"]["vendor_bank_changes"])
        self.assertTrue(payload["business_flows"]["expense_claim_records"])
        self.assertTrue(payload["datasets"]["collection_actions"])
        self.assertTrue(payload["datasets"]["procurement_requests"])
        self.assertTrue(payload["datasets"]["purchase_deliveries"])
        self.assertTrue(any(row.get("procurement_request_id") for row in payload["datasets"]["purchases"]))
        self.assertEqual(payload["datasets"]["purchase_deliveries"][0]["status"], "已交付待验收")
        self.assertTrue(payload["datasets"]["vendor_bank_changes"])
        self.assertTrue(payload["business_flows"]["payment_request_records"][0]["vendor_bank_binding"])
        self.assertGreater(payload["planning"]["collection_commitment_total"], 0)
        self.assertTrue(all(row["entity_id"] == "cn_studio" for row in payload["business_flows"]["payment_request_records"] + payload["business_flows"]["expense_claim_records"]))

    def test_overseas_scenario_is_multi_currency_and_cross_border(self):
        payload = build_demo_payload(self.scenarios["overseas"])
        self.assertEqual(payload["company_profile"]["entity_id"], "sg_publisher")
        self.assertEqual(set(payload["settlements"]["summary"]["currencies"]), {"USD"})
        self.assertTrue(payload["company_profile"]["fx_policy"]["month_end_rates"]["2026-02"]["USD"])
        self.assertEqual(payload["finance_ops"]["tax_pack"]["jurisdiction"], "SG")
        self.assertEqual(payload["finance_ops"]["tax_pack"]["tax_pack"], "jurisdiction.sg")
        self.assertEqual(payload["finance_ops"]["tax_pack"]["returns_workspace"]["returns"], [])
        self.assertIn("不会生成中国税表", payload["finance_ops"]["tax_pack"]["filing_guardrail"])
        self.assertGreater(payload["bp"]["totals"]["revenue"], 0)
        self.assertTrue(payload["business_flows"]["payment_request_records"])
        self.assertTrue(all(row["entity_id"] == "sg_publisher" for row in payload["business_flows"]["payment_request_records"] + payload["business_flows"]["expense_claim_records"]))

    def test_group_view_preserves_legal_entity_ownership(self):
        payload = build_group_demo_payload(self.scenarios)
        self.assertEqual(payload["scope_mode"], "group")
        self.assertEqual({row["entity_id"] for row in payload["entities"]}, {"cn_studio", "sg_publisher"})
        valid_ids = {"cn_studio", "sg_publisher"}
        for name in ("settlements", "purchases", "bank_transactions", "invoices", "payroll_rows", "payment_requests", "expense_claims", "collection_actions", "procurement_requests", "purchase_deliveries", "vendor_bank_changes"):
            self.assertTrue({row["entity_id"] for row in payload["datasets"][name]} <= valid_ids)
        self.assertEqual({row["entity_id"] for row in payload["datasets"]["settlements"]}, valid_ids)
        self.assertEqual({row["entity_id"] for row in payload["datasets"]["bank_transactions"]}, valid_ids)
        self.assertEqual({row["entity_id"] for row in payload["datasets"]["payment_requests"]}, valid_ids)
        self.assertEqual({row["entity_id"] for row in payload["datasets"]["expense_claims"]}, valid_ids)
        self.assertEqual({row["entity_id"] for row in payload["datasets"]["collection_actions"]}, valid_ids)
        self.assertEqual({row["entity_id"] for row in payload["datasets"]["vendor_bank_changes"]}, valid_ids)
        self.assertEqual({row["entity_id"] for row in payload["datasets"]["purchase_deliveries"]}, valid_ids)
        self.assertIn("税务申报", payload["statutory_guardrail"])
        self.assertIn("抵销", payload["elimination_policy"]["principle"])
        portfolio = payload["bp"]["game_collection_portfolio"]
        shared = [row for row in portfolio["rows"] if row["management_game_id"] == "GAME-GLOBAL-001"]
        self.assertEqual({row["entity_id"] for row in shared}, valid_ids)
        self.assertEqual({row["currency"] for row in shared}, {"CNY", "USD"})
        self.assertEqual({row["entity_id"] for row in payload["datasets"]["cash_allocations"]}, valid_ids)
        self.assertTrue(all(row["target_type"] == "receivable" for row in payload["datasets"]["cash_allocations"]))


if __name__ == "__main__":
    unittest.main()
