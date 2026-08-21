import unittest

from src.game_prepaid_costs import build_game_prepaid_cost_view


class GamePrepaidCostTests(unittest.TestCase):
    def _datasets(self):
        purchase = {
            "id": "PO-LIC-1", "entity_id": "cn_studio", "project": "G001", "currency": "CNY",
            "ordered_amount": 1200, "accepted_amount": 1200, "paid_amount": 1200,
            "contract_facts": {
                "cost_type": "IP license", "contract_reference": "CTR-IP-001",
                "contract_evidence": ["合同扫描件", "权利清单"],
                "service_start": "2026-01-01", "service_end": "2026-12-31",
                "period_evidence": [
                    {"period": "2026-01", "evidence": ["一月权利有效确认"]},
                    {"period": "2026-03", "evidence": ["三月权利有效确认"]},
                ],
            },
            "cost_policy": {
                "status": "已批准", "approved_by": "会计服务机构",
                "classification": "递延", "allocation_method": "按服务期间直线释放",
                "cost_basis_amount": 1200, "evidence": ["会计政策审批单 AP-001"],
            },
            "acceptance_history": [{"decision": "全部验收", "evidence": ["验收单"]}],
        }
        return {
            "purchases": [purchase],
            "purchase_deliveries": [{
                "id": "DEL-LIC-1", "entity_id": "cn_studio", "purchase_id": "PO-LIC-1",
                "status": "已验收", "accepted_amount": 1200, "acceptance_evidence": ["验收单"],
            }],
            "invoices": [{
                "id": "INV-LIC-1", "entity_id": "cn_studio", "total_amount": 1200,
                "verification_status": "已查验", "anomalies": [], "purchase_match": {"purchase_id": "PO-LIC-1"},
            }],
            "cash_allocations": [{
                "id": "PAY-LIC-1", "entity_id": "cn_studio", "target_type": "payable",
                "target_id": "PO-LIC-1", "amount": 1200, "status": "已核销",
            }],
            "asset_cards": [],
        }

    def test_approved_policy_and_period_evidence_create_release_candidate(self):
        result = build_game_prepaid_cost_view(self._datasets(), "2026-01")
        candidate = result["candidates"][0]
        self.assertEqual(candidate["classification_candidate"], "递延")
        self.assertEqual(candidate["current_period_release_candidate"], 100)
        self.assertEqual(candidate["paid_amount"], 1200)
        self.assertIn("不决定", candidate["payment_effect"])
        self.assertEqual(result["project_impacts"][0]["project_cost_impact_candidate"], 100)
        self.assertEqual(result["project_impacts"][0]["contribution_effect"], -100)

    def test_month_without_service_evidence_is_not_released_or_shifted(self):
        result = build_game_prepaid_cost_view(self._datasets(), "2026-02")
        february = next(row for row in result["release_schedule"] if row["period"] == "2026-02")
        march = next(row for row in result["release_schedule"] if row["period"] == "2026-03")
        self.assertEqual(february["planned_release"], 100)
        self.assertEqual(february["release_candidate"], 0)
        self.assertEqual(march["release_candidate"], 100)
        self.assertEqual(result["project_impacts"], [])

    def test_payment_never_creates_prepaid_or_capitalized_candidate(self):
        data = self._datasets()
        purchase = data["purchases"][0]
        purchase.pop("cost_policy")
        purchase["accepted_amount"] = 0
        data["purchase_deliveries"] = []
        data["invoices"] = []
        result = build_game_prepaid_cost_view(data, "2026-01")
        candidate = result["candidates"][0]
        self.assertEqual(candidate["classification_candidate"], "待判断")
        self.assertEqual(candidate["current_period_release_candidate"], 0)
        self.assertEqual(candidate["paid_amount"], 1200)
        self.assertTrue(candidate["issues"])

    def test_expense_candidate_requires_explicit_period_inside_service_term(self):
        data = self._datasets()
        policy = data["purchases"][0]["cost_policy"]
        policy.update({
            "classification": "费用化", "allocation_method": "指定期间一次费用化",
            "expense_period": "2027-01",
        })
        result = build_game_prepaid_cost_view(data, "2026-01")
        self.assertIn("费用期间不在合同服务期间内", result["candidates"][0]["issues"])
        self.assertEqual(result["release_schedule"], [])

    def test_entities_and_currencies_are_separate(self):
        data = self._datasets()
        overseas = {**data["purchases"][0], "id": "PO-SG", "entity_id": "sg_publisher", "currency": "USD"}
        overseas["contract_facts"] = {**overseas["contract_facts"], "contract_reference": "CTR-SG"}
        data["purchases"].append(overseas)
        data["purchase_deliveries"].append({
            "id": "DEL-SG", "entity_id": "sg_publisher", "purchase_id": "PO-SG", "status": "已验收",
            "accepted_amount": 1200, "acceptance_evidence": ["SG acceptance"],
        })
        data["invoices"].append({
            "id": "INV-SG", "entity_id": "sg_publisher", "total_amount": 1200,
            "verification_status": "有效", "anomalies": [], "purchase_match": {"purchase_id": "PO-SG"},
        })
        result = build_game_prepaid_cost_view(data, "2026-01")
        self.assertEqual(set(result["summary"]["scopes"]), {("cn_studio", "CNY"), ("sg_publisher", "USD")})
        self.assertEqual(len(result["project_impacts"]), 2)

    def test_asset_card_stays_blocked_without_explicit_evidence_and_policy(self):
        data = {"purchases": [], "purchase_deliveries": [], "invoices": [], "cash_allocations": [], "asset_cards": [{
            "id": "AST-1", "entity_id": "cn_studio", "project": "G001", "original_currency": "CNY",
            "asset_type": "无形资产", "original_cost": 5000,
            "contract_facts": {
                "cost_type": "游戏授权费", "contract_reference": "CTR-1", "contract_evidence": ["合同"],
                "service_start": "2026-01-01", "service_end": "2026-12-31",
            },
        }]}
        result = build_game_prepaid_cost_view(data, "2026-01")
        candidate = result["candidates"][0]
        self.assertEqual(candidate["classification_candidate"], "待判断")
        self.assertIn("缺少已批准且有证据的会计政策", candidate["issues"])
        self.assertIn("不判断无形资产", candidate["accounting_boundary"])


if __name__ == "__main__":
    unittest.main()
