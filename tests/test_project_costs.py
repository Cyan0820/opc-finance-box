import copy
import unittest

from src.project_costs import build_project_procurement_cost_view


class ProjectProcurementCostTests(unittest.TestCase):
    def _datasets(self):
        request = {
            "id": "PR-1", "entity_id": "cn_studio", "project": "G001", "category": "美术外包",
            "amount": 600, "currency": "CNY", "period": "2026-07", "status": "已下单",
            "budget_snapshot": {"budget_found": True, "source_line_ids": ["B-1"]},
        }
        purchase = {
            "id": "PO-1", "entity_id": "cn_studio", "procurement_request_id": "PR-1",
            "project": "G001", "category": "美术外包", "item": "角色原画", "currency": "CNY",
            "ordered_amount": 600, "accepted_amount": 300, "invoice_amount": 200, "paid_amount": 150,
            "milestones": [{"id": "MS-1", "amount": 600}],
        }
        return {
            "plan_lines": [{
                "id": "B-1", "entity_id": "cn_studio", "project": "G001", "category": "美术外包",
                "period": "2026-07", "currency": "CNY", "scenario": "基准", "direction": "支出",
                "amount": 1000, "anomalies": [],
            }],
            "procurement_requests": [request],
            "purchases": [purchase],
            "purchase_deliveries": [
                {
                    "id": "DEL-1", "entity_id": "cn_studio", "purchase_id": "PO-1", "milestone_id": "MS-1",
                    "currency": "CNY", "delivered_amount": 300, "accepted_amount": 300, "status": "已验收",
                    "period": "2026-07", "evidence": ["交付清单"], "acceptance_evidence": ["验收单"],
                },
                {
                    "id": "DEL-2", "entity_id": "cn_studio", "purchase_id": "PO-1", "milestone_id": "MS-1",
                    "currency": "CNY", "delivered_amount": 100, "accepted_amount": 0, "status": "已交付待验收",
                    "evidence": ["交付邮件"],
                },
            ],
            "invoices": [{
                "id": "INV-1", "entity_id": "cn_studio", "currency": "CNY", "total_amount": 200,
                "verification_status": "已查验", "anomalies": [], "purchase_match": {"purchase_id": "PO-1"},
            }],
            "cash_allocations": [],
        }

    def test_full_chain_separates_commitment_actual_invoice_and_payment(self):
        result = build_project_procurement_cost_view(self._datasets(), "2026-07")
        row = result["rows"][0]
        self.assertEqual(row["budget_amount"], 1000)
        self.assertEqual(row["approved_request_amount"], 600)
        self.assertEqual(row["budget_remaining"], 400)
        self.assertEqual(row["committed_order_amount"], 600)
        self.assertEqual(row["open_commitment"], 300)
        self.assertEqual(row["delivered_pending_acceptance"], 100)
        self.assertEqual(row["accepted_actual"], 300)
        self.assertEqual(row["pending_invoice"], 100)
        self.assertEqual(row["pending_payment"], 50)
        self.assertEqual(row["paid_amount"], 150)
        self.assertEqual(result["actual_cost_lines"][0]["basis"], "accepted_delivery_event")

    def test_delivery_and_payment_do_not_create_cost(self):
        data = self._datasets()
        data["purchase_deliveries"] = [data["purchase_deliveries"][1]]
        data["purchases"][0].update({"accepted_amount": 0, "invoice_amount": 0, "paid_amount": 500})
        data["invoices"] = []
        result = build_project_procurement_cost_view(data, "2026-07")
        row = result["rows"][0]
        self.assertEqual(row["accepted_actual"], 0)
        self.assertEqual(result["actual_cost_lines"], [])
        self.assertEqual(row["delivered_pending_acceptance"], 100)
        self.assertEqual(row["open_commitment"], 600)

    def test_does_not_guess_project_or_cross_legal_entity(self):
        data = self._datasets()
        data["purchases"].append({
            "id": "PO-NO-PROJECT", "entity_id": "cn_studio", "project": "待分配项目",
            "ordered_amount": 600, "accepted_amount": 600, "currency": "CNY", "order_date": "2026-07-01",
        })
        data["purchase_deliveries"].append({
            "id": "DEL-SG", "entity_id": "sg_publisher", "purchase_id": "PO-1", "milestone_id": "MS-1",
            "delivered_amount": 300, "accepted_amount": 300, "status": "已验收", "period": "2026-07",
            "evidence": ["wrong owner"], "acceptance_evidence": ["wrong owner"],
        })
        result = build_project_procurement_cost_view(data, "2026-07")
        self.assertEqual(len(result["rows"]), 1)
        self.assertEqual(result["rows"][0]["accepted_actual"], 300)
        self.assertEqual(result["summary"]["gaps"]["order_missing_project"], 1)

    def test_acceptance_period_drives_actual_but_total_acceptance_reduces_commitment(self):
        data = self._datasets()
        prior = copy.deepcopy(data["purchase_deliveries"][0])
        prior.update({"id": "DEL-PRIOR", "accepted_amount": 100, "delivered_amount": 100, "period": "2026-06"})
        data["purchase_deliveries"].insert(0, prior)
        data["purchases"][0]["accepted_amount"] = 400
        result = build_project_procurement_cost_view(data, "2026-07")
        row = result["rows"][0]
        self.assertEqual(row["accepted_actual"], 300)
        self.assertEqual(row["open_commitment"], 200)

    def test_legacy_explicit_acceptance_is_visible_but_not_an_approved_commitment(self):
        data = self._datasets()
        data["procurement_requests"] = []
        data["purchase_deliveries"] = []
        data["purchases"][0].pop("procurement_request_id")
        data["purchases"][0]["order_date"] = "2026-07-01"
        result = build_project_procurement_cost_view(data, "2026-07")
        row = result["rows"][0]
        self.assertEqual(row["accepted_actual"], 300)
        self.assertEqual(row["committed_order_amount"], 0)
        self.assertEqual(row["open_commitment"], 0)
        self.assertIn("历史验收金额缺逐事件验收证据", row["issues"])

    def test_license_acceptance_does_not_enter_generic_procurement_actual(self):
        data = self._datasets()
        data["purchases"][0]["contract_facts"] = {
            "cost_type": "IP license", "contract_reference": "CTR-1",
            "service_start": "2026-07-01", "service_end": "2027-06-30",
        }
        data["purchases"][0]["cost_policy"] = {
            "status": "已批准", "classification": "递延",
        }
        result = build_project_procurement_cost_view(data, "2026-07")
        self.assertEqual(result["rows"][0]["accepted_actual"], 0)
        self.assertEqual(result["actual_cost_lines"], [])
        self.assertTrue(any("期间释放候选桥" in issue for issue in result["rows"][0]["issues"]))


if __name__ == "__main__":
    unittest.main()
