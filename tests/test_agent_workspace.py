import unittest

from src.agent_workspace import (
    build_confirmation_queue, build_deliverable_register, latest_period,
    plan_safe_document_automations,
)


class AgentWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.goal = {
            "id": "GOAL-123456789ABC", "period": "2026-08", "data_mode": "live",
            "actions": [{
                "id": "C14", "title": "形成税务申报工作底稿", "owner": "税务服务机构",
                "status": "等待确认", "blockers": [], "evidence": [],
                "automation": {"required_role": "税务服务机构", "level": "需确认"},
                "decision_support": {"business_impact": "影响申报口径", "agent_recommendation": "逐字段复核"},
                "artifacts": [{
                    "name": "税务申报工作底稿", "reference": "api:/api/tax-return-workbook?period=2026-08",
                    "status": "草稿待确认", "evidence_state": "待人工确认",
                }],
            }],
        }
        self.datasets = {
            "payment_requests": [{
                "id": "PAY-1", "status": "待批准", "amount": 2_000_000, "currency": "CNY",
                "purpose": "渠道投放", "evidence": ["合同", "验收"], "blockers": [],
                "agent_recommendation": "核对账户",
            }, {
                "id": "PAY-2", "status": "待批准", "amount": 300_000, "currency": "USD",
                "purpose": "海外买量", "evidence": ["合同", "验收"], "blockers": [],
            }],
            "expense_claims": [], "purchases": [], "asset_cards": [], "accruals": [],
            "game_revenue_policies": [], "tax_filing_reviews": [],
        }
        self.finance = {"vouchers": [], "period_state": {"voucher_reviews": {}}}

    def test_high_value_items_rank_first_and_currency_is_not_mixed(self):
        queue = build_confirmation_queue(self.goal, self.datasets, self.finance)
        self.assertEqual(queue["items"][0]["source_id"], "PAY-1")
        self.assertEqual(queue["items"][0]["priority"], "高")
        self.assertEqual(queue["amount_exposure_by_currency"], [
            {"currency": "CNY", "value": 2_000_000.0},
            {"currency": "USD", "value": 300_000.0},
        ])

    def test_blocked_payment_has_no_approval_action(self):
        self.datasets["payment_requests"] = [{
            "id": "PAY-BLOCKED", "status": "阻塞", "amount": 5_000_000, "currency": "CNY",
            "purpose": "预付款", "evidence": [], "blockers": ["缺少付款依据"],
        }]
        queue = build_confirmation_queue(self.goal, self.datasets, self.finance)
        payment = next(item for item in queue["items"] if item["source_id"] == "PAY-BLOCKED")
        self.assertEqual(payment["priority"], "紧急")
        self.assertIsNone(payment["decision"])

    def test_pending_purchase_acceptance_is_in_unified_queue(self):
        self.datasets["purchases"] = [{
            "id": "PO-1", "po_number": "PO-1", "item": "角色立绘", "vendor": "美术供应商",
            "ordered_amount": 200_000, "accepted_amount": 0, "currency": "CNY",
            "acceptance_status": "已交付待验收", "anomalies": [],
        }]
        queue = build_confirmation_queue(self.goal, self.datasets, self.finance)
        acceptance = next(item for item in queue["items"] if item["source_type"] == "purchase_acceptance")
        self.assertEqual(acceptance["required_role"], "实际接收交付的业务负责人")
        self.assertIn("全部验收", acceptance["decision"]["choices"])

    def test_milestone_delivery_is_the_acceptance_target(self):
        self.datasets["purchases"] = [{
            "id": "PO-M1", "entity_id": "cn_studio", "po_number": "PO-M1",
            "ordered_amount": 100000, "currency": "CNY", "milestones": [{"id": "MS-1"}],
            "acceptance_status": "待交付", "anomalies": [],
        }]
        self.datasets["purchase_deliveries"] = [{
            "id": "DEL-1", "entity_id": "cn_studio", "purchase_id": "PO-M1",
            "po_number": "PO-M1", "milestone_id": "MS-1", "milestone_title": "角色立绘",
            "delivered_amount": 100000, "currency": "CNY", "status": "已交付待验收",
            "acceptance_criteria": "源文件和清单完整", "acceptance_owner": "美术负责人",
            "evidence": ["交付清单"],
        }]
        queue = build_confirmation_queue(self.goal, self.datasets, self.finance)
        acceptance = next(item for item in queue["items"] if item["source_type"] == "purchase_acceptance")
        self.assertEqual(acceptance["source_id"], "DEL-1")
        self.assertEqual(acceptance["required_role"], "美术负责人")
        self.assertEqual(acceptance["decision"]["id_fields"]["delivery_id"], "DEL-1")
        self.assertEqual(sum(item["source_type"] == "purchase_acceptance" for item in queue["items"]), 1)

    def test_procurement_request_surfaces_budget_and_sourcing_review(self):
        self.datasets["procurement_requests"] = [{
            "id": "PR-1", "entity_id": "cn_studio", "status": "待批准",
            "description": "美术外包", "amount": 180_000, "currency": "CNY",
            "requester": "制作人", "sourcing_method": "竞争比价",
            "selected_vendor": "供应商B", "warnings": ["非最低价中选，理由待独立审批"],
            "budget_snapshot": {"available_amount": 300_000}, "evidence": ["三方报价"],
        }]
        queue = build_confirmation_queue(self.goal, self.datasets, self.finance)
        request = next(item for item in queue["items"] if item["source_type"] == "procurement_request")
        self.assertEqual(request["decision"]["endpoint"], "/api/procurement-request-decision")
        self.assertEqual(request["metadata"]["warnings"], ["非最低价中选，理由待独立审批"])
        self.assertEqual(request["metadata"]["budget_snapshot"]["available_amount"], 300_000)

    def test_blocked_procurement_request_cannot_be_approved(self):
        self.datasets["procurement_requests"] = [{
            "id": "PR-BLOCK", "entity_id": "sg_publisher", "status": "阻塞",
            "description": "投放服务", "amount": 90_000, "currency": "USD",
            "blockers": ["未找到足额同口径预算"],
        }]
        queue = build_confirmation_queue(self.goal, self.datasets, self.finance)
        request = next(item for item in queue["items"] if item["source_type"] == "procurement_request")
        self.assertIsNone(request["decision"])

    def test_vendor_bank_change_requires_independent_verification(self):
        self.datasets["vendor_bank_changes"] = [{
            "id": "VBANK-1", "entity_id": "cn_studio", "status": "待批准",
            "vendor": "供应商A", "account_masked": "•••• 9012", "bank_name": "测试银行",
            "currency": "CNY", "change_type": "变更", "evidence": ["盖章账户函", "联系人"],
        }]
        queue = build_confirmation_queue(self.goal, self.datasets, self.finance)
        item = next(row for row in queue["items"] if row["source_type"] == "vendor_bank_change")
        self.assertEqual(item["decision"]["endpoint"], "/api/vendor-bank-change-decision")
        self.assertIn("回拨", item["recommendation"])

    def test_deliverable_draft_is_not_claimed_as_complete(self):
        register = build_deliverable_register(self.goal, self.finance)
        self.assertEqual(register["items"][0]["status"], "草稿待确认")
        self.assertEqual(register["complete_count"], 0)
        self.assertEqual(register["generated_count"], 0)

    def test_latest_period_reads_high_volume_business_datasets(self):
        datasets = {
            "settlements": [{"period": "2026-07"}],
            "bank_transactions": [{"transaction_date": "2026-08-12"}],
            "plan_lines": [{"period": "2027-12"}],
        }
        self.assertEqual(latest_period(datasets), "2026-08")

    def test_latest_period_reads_procurement_and_vendor_account_work(self):
        self.assertEqual(latest_period({
            "procurement_requests": [{"period": "2026-09"}],
            "vendor_bank_changes": [{"requested_at": "2026-08-14T10:00:00+00:00"}],
        }), "2026-09")

    def test_purchase_warning_does_not_overlap_hard_blocker_count(self):
        self.datasets["payment_requests"] = []
        self.datasets["purchases"] = [{
            "id": "PO-WARN", "item": "美术外包", "ordered_amount": 150_000, "currency": "CNY",
            "acceptance_status": "已交付待验收", "anomalies": ["疑似已发生未开票：月结需判断暂估"],
        }]
        queue = build_confirmation_queue(self.goal, self.datasets, self.finance)
        acceptance = next(item for item in queue["items"] if item["source_type"] == "purchase_acceptance")
        self.assertTrue(acceptance["decision"])
        self.assertFalse(acceptance["blockers"])
        self.assertEqual(acceptance["metadata"]["warnings"], ["疑似已发生未开票：月结需判断暂估"])

    def test_agent_plans_preview_recognition_but_never_auto_commit(self):
        documents = [{
            "id": "DOC-1234567890ABCDEF", "status": "已识别待确认", "recognition": None,
            "entity_scope": {"status": "confirmed", "entity_id": "cn_studio"},
            "classification": {
                "document_type": "bank_statement", "confidence": 0.96,
                "capability": "结构化识别", "periods": ["2026-08"],
            },
        }]
        actions = plan_safe_document_automations(documents, fallback_period="2026-08")
        self.assertEqual(actions[0]["action"], "recognize_preview")
        self.assertFalse(actions[0]["commit_allowed"])

    def test_agent_does_not_guess_ambiguous_document_period(self):
        documents = [{
            "id": "DOC-1234567890ABCDEF", "status": "已识别待确认", "recognition": None,
            "entity_scope": {"status": "confirmed", "entity_id": "cn_studio"},
            "classification": {
                "document_type": "settlement", "confidence": 0.96,
                "capability": "结构化识别", "periods": ["2026-07", "2026-08"],
            },
        }]
        self.assertEqual(plan_safe_document_automations(documents), [])

    def test_agent_requires_human_confirmed_entity_before_preview_recognition(self):
        documents = [{
            "id": "DOC-1234567890ABCDEF", "status": "已识别待确认", "recognition": None,
            "entity_scope": {"status": "suggested", "entity_id": "sg_publisher"},
            "classification": {
                "document_type": "bank_statement", "confidence": 0.96,
                "capability": "结构化识别", "periods": ["2026-08"],
            },
        }]
        self.assertEqual(plan_safe_document_automations(documents), [])
        documents[0]["entity_scope"]["status"] = "confirmed"
        actions = plan_safe_document_automations(documents)
        self.assertEqual(actions[0]["entity_id"], "sg_publisher")

    def test_ocr_document_is_blocked_until_original_is_confirmed(self):
        documents = [{
            "id": "DOC-1234567890ABCDEF", "original_filename": "银行对账单.pdf",
            "status": "已解析待入账", "entity_scope": {"entity_id": "cn_studio"},
            "classification": {"document_type": "bank_statement_document", "label": "银行 PDF/图片对账单", "confidence": 0.9},
            "recognition": {"records": [{"transaction_id": "T1"}], "corrections": []},
        }]
        queue = build_confirmation_queue(self.goal, self.datasets, self.finance, inbox_documents=documents)
        item = next(row for row in queue["items"] if row["source_type"] == "inbox_document")
        self.assertEqual(item["status"], "阻塞")
        self.assertIn("对照原件", item["blockers"][0])
        self.assertEqual(item["decision"]["method"], "NAVIGATE")

    def test_acceptance_evidence_is_not_treated_as_acceptance_conclusion(self):
        documents = [{
            "id": "DOC-1234567890ABCDEF", "original_filename": "验收证明.pdf",
            "status": "已提取待归档", "entity_scope": {"entity_id": "cn_studio"},
            "classification": {"document_type": "acceptance_evidence", "label": "交付/验收证据", "confidence": 0.9},
            "recognition": {"evidence_only": True},
        }]
        queue = build_confirmation_queue(self.goal, self.datasets, self.finance, inbox_documents=documents)
        item = next(row for row in queue["items"] if row["source_type"] == "inbox_document")
        self.assertIn("尚未关联", item["blockers"][0])
        self.assertIn("不会自动形成验收结论", item["recommendation"])


if __name__ == "__main__":
    unittest.main()
