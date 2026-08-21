import unittest

from src.business_partner import build_bp_analysis


PROFILE = {"cash_planning": {"opening_cash_cny": 1000000, "minimum_buffer_cny": 100000}}


class BusinessPartnerTests(unittest.TestCase):
    def _datasets(self):
        return {
            "master_records": [
                {"record_type": "game", "code": "G001", "name": "星海", "stage": "运营", "active": True},
                {"record_type": "game", "code": "G002", "name": "远征", "stage": "运营", "active": True},
            ],
            "settlements": [
                {"id": "S1", "period": "2026-06", "game": "G001", "currency": "CNY", "settlement_amount": 800, "gross": 2000},
                {"id": "S2", "period": "2026-07", "game": "G001", "currency": "CNY", "settlement_amount": 1000, "gross": 2500},
                {"id": "S3", "period": "2026-07", "game": "G002", "currency": "CNY", "settlement_amount": 400, "gross": 800},
            ],
            "purchases": [
                {"id": "P1", "order_date": "2026-07-10", "project": "G001", "category": "广告投放", "currency": "CNY", "accepted_amount": 200},
                {"id": "P2", "order_date": "2026-07-11", "project": "G002", "category": "美术外包", "currency": "CNY", "accepted_amount": 300},
            ],
            "payroll_rows": [
                {"id": "H1", "period": "2026-07", "project": "G001", "gross_salary": 200, "rd_ratio": 1, "allocation_evidence": ["TS-H1"]},
                {"id": "H2", "period": "2026-07", "project": "", "gross_salary": 100},
            ],
            "game_kpis": [{"period": "2026-07", "project_code": "G001", "mau": 100, "payers": 10, "gross_bookings": 2500, "marketing_spend": 200, "status": "可用"}],
            "plan_lines": [], "bank_transactions": [], "invoices": [], "opening_balances": [],
        }

    def test_project_pnl_keeps_public_cost_unallocated(self):
        result = build_bp_analysis(self._datasets(), PROFILE, "2026-07")
        game = next(row for row in result["projects"] if row["project_code"] == "G001")
        public = next(row for row in result["projects"] if row["project_code"] == "公司公共/待分配")
        self.assertEqual(game["contribution"], 600)
        self.assertEqual(public["direct_cost"], 100)
        self.assertEqual(game["kpis"]["gross_roas"], 12.5)
        self.assertEqual(result["change_vs_previous"]["revenue"], 600)
        self.assertIn("change_attribution", result)
        self.assertIn("proactive_insights", result)
        marketing = result["marketing_finance"]
        self.assertEqual(marketing["totals"]["media_spend"], 200)
        self.assertEqual(marketing["totals"]["finance_spend"], 200)
        self.assertEqual(marketing["projects"][0]["payer_cpa"], 20)
        self.assertEqual(marketing["projects"][0]["gate_status"], "门槛待配置")
        self.assertIn("实时出价", marketing["boundary"]["not_owned"])
        self.assertEqual(game["procurement_actual"], 200)
        self.assertEqual(game["procurement_open_commitment"], 0)
        self.assertEqual(game["committed_contribution"], 600)
        self.assertIn("project_procurement_costs", result)
        self.assertIn("project_labor_costs", result)
        self.assertIn("game_prepaid_costs", result)
        self.assertEqual(game["payroll"], 200)
        self.assertEqual(result["data_quality"]["labor_evidence_gap_count"], 1)
        self.assertTrue(any(message["title"] == "项目人力成本仍有证据缺口" for message in result["management_messages"]))

    def test_marketing_finance_reconciles_before_roi_decision(self):
        data = self._datasets()
        data["game_kpis"][0]["marketing_spend"] = 500
        result = build_bp_analysis(data, PROFILE, "2026-07")
        marketing = next(row for row in result["marketing_finance"]["projects"] if row["project_code"] == "G001")
        self.assertEqual(marketing["reconciliation_gap"], 300)
        self.assertEqual(marketing["gate_status"], "门槛待配置")
        # 300 is below the minimum materiality threshold of 1,000 in the sample.
        data["game_kpis"][0]["marketing_spend"] = 2_500
        result = build_bp_analysis(data, PROFILE, "2026-07")
        marketing = next(row for row in result["marketing_finance"]["projects"] if row["project_code"] == "G001")
        self.assertEqual(marketing["gate_status"], "先对账")

    def test_foreign_currency_without_rate_is_not_silently_added(self):
        data = self._datasets()
        data["settlements"].append({"id": "S4", "period": "2026-07", "game": "G001", "currency": "USD", "settlement_amount": 999})
        result = build_bp_analysis(data, PROFILE, "2026-07")
        self.assertEqual(result["totals"]["revenue"], 1400)
        # Revenue, gross bookings and refund fields all remain in their original currency until a rate is supplied.
        self.assertEqual(result["data_quality"]["unconverted_count"], 3)

    def test_order_without_acceptance_is_commitment_not_actual_cost(self):
        data = self._datasets()
        data["plan_lines"] = [{
            "id": "B1", "entity_id": "cn_studio", "project": "G001", "category": "美术外包",
            "period": "2026-07", "currency": "CNY", "scenario": "基准", "direction": "支出",
            "amount": 500,
        }]
        data["procurement_requests"] = [{
            "id": "PR1", "entity_id": "cn_studio", "project": "G001", "category": "美术外包",
            "period": "2026-07", "currency": "CNY", "amount": 300, "status": "已下单",
            "budget_snapshot": {"budget_found": True, "source_line_ids": ["B1"]},
        }]
        data["purchases"].append({
            "id": "PO1", "entity_id": "cn_studio", "procurement_request_id": "PR1",
            "project": "G001", "category": "美术外包", "currency": "CNY",
            "order_date": "2026-07-15", "ordered_amount": 300, "accepted_amount": None,
            "milestones": [{"id": "MS1", "amount": 300}],
        })
        result = build_bp_analysis(data, PROFILE, "2026-07")
        game = next(row for row in result["projects"] if row["project_code"] == "G001")
        self.assertEqual(game["procurement_actual"], 200)
        self.assertEqual(game["direct_cost"], 400)
        self.assertEqual(game["procurement_open_commitment"], 300)
        self.assertEqual(game["committed_contribution"], 300)

    def test_license_purchase_uses_policy_release_without_double_counting_acceptance(self):
        data = self._datasets()
        data["purchases"] = [{
            "id": "LIC1", "entity_id": "cn_studio", "project": "G001", "category": "IP授权",
            "currency": "CNY", "accepted_amount": 1200, "paid_amount": 1200, "order_date": "2026-07-01",
            "contract_facts": {
                "cost_type": "IP license", "contract_reference": "CTR-1", "contract_evidence": ["合同"],
                "service_start": "2026-07-01", "service_end": "2027-06-30",
                "period_evidence": [{"period": "2026-07", "evidence": ["七月权利有效"]}],
            },
            "cost_policy": {
                "status": "已批准", "approved_by": "会计", "classification": "递延",
                "allocation_method": "按服务期间直线释放", "cost_basis_amount": 1200, "evidence": ["政策审批"],
            },
            "acceptance_history": [{"decision": "全部验收"}],
        }]
        data["purchase_deliveries"] = [{
            "id": "D-LIC", "entity_id": "cn_studio", "purchase_id": "LIC1", "status": "已验收",
            "accepted_amount": 1200, "acceptance_evidence": ["验收单"], "period": "2026-07",
        }]
        data["invoices"] = [{
            "id": "I-LIC", "entity_id": "cn_studio", "total_amount": 1200,
            "verification_status": "已查验", "anomalies": [], "purchase_match": {"purchase_id": "LIC1"},
        }]
        result = build_bp_analysis(data, PROFILE, "2026-07")
        game = next(row for row in result["projects"] if row["project_code"] == "G001")
        self.assertEqual(game["procurement_actual"], 0)
        self.assertEqual(game["special_cost_release_candidate"], 100)
        self.assertEqual(game["direct_cost"], 300)
        self.assertEqual(game["contribution"], 700)


if __name__ == "__main__":
    unittest.main()
