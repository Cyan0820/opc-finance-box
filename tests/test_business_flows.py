import unittest

from src.business_flows import (
    build_flow_overview, build_payables_register, build_payroll_payables, build_receivables_register, create_cash_allocation,
    create_collection_action, create_expense_claim, create_payment_request, decide_expense_claim, decide_payment_request,
)


class BusinessFlowTests(unittest.TestCase):
    def setUp(self):
        self.settlement = {"id": "S1", "period": "2026-01", "game": "G", "channel": "Apple", "currency": "USD", "net_receivable": 1000}
        self.receipt1 = {"id": "B1", "transaction_id": "T1", "direction": "收入", "currency": "USD", "amount": 600}
        self.receipt2 = {"id": "B2", "transaction_id": "T2", "direction": "收入", "currency": "USD", "amount": 400}

    def test_partial_and_multiple_receipts_close_one_receivable(self):
        first = create_cash_allocation(self.receipt1, "receivable", self.settlement, 600, [], "财务")
        second = create_cash_allocation(self.receipt2, "receivable", self.settlement, 400, [first], "财务")
        register = build_receivables_register([self.settlement], [first, second], "2026-03-01")
        self.assertEqual(register["rows"][0]["status"], "已回款")
        self.assertEqual(register["rows"][0]["allocated_receipts"], 1000)

    def test_channel_master_payment_days_drive_due_date(self):
        register = build_receivables_register(
            [self.settlement], [], "2026-04-01",
            [{"record_type": "channel", "name": "Apple", "payment_days": 45, "active": True}],
        )
        row = register["rows"][0]
        self.assertEqual(row["due_date"], "2026-03-17")
        self.assertIn("渠道主数据", row["due_date_basis"])

    def test_collection_promise_is_append_only_current_state_and_missed_alert(self):
        receivable = build_receivables_register([self.settlement], [], "2026-02-01")["rows"][0]
        first = create_collection_action(
            receivable, "回款承诺", "渠道运营", "财务", action_date="2026-02-01",
            promised_date="2026-02-20", promised_amount=600, note="渠道邮件确认本月付款",
        )
        second = create_collection_action(
            receivable, "回款承诺", "渠道运营", "财务", action_date="2026-02-21",
            promised_date="2026-03-10", promised_amount=500, note="渠道更新付款审批进度",
            existing_actions=[first],
        )
        self.assertEqual(second["supersedes_action_id"], first["id"])
        register = build_receivables_register(
            [self.settlement], [], "2026-03-11", collection_actions=[first, second],
        )
        row = register["rows"][0]
        self.assertEqual(row["promised_amount"], 500)
        self.assertTrue(row["promise_missed"])
        self.assertEqual(register["missed_promise_count"], 1)
        self.assertEqual(row["collection_priority"], "P0")
        self.assertEqual(row["effective_promised_amount"], 0)

    def test_collection_priority_is_ranked_within_entity_and_currency(self):
        settlements = [
            {"id": "OLD", "entity_id": "cn_studio", "period": "2026-01", "game": "G", "channel": "Old", "currency": "CNY", "net_receivable": 100},
            {"id": "DUE", "entity_id": "cn_studio", "period": "2026-02", "game": "G", "channel": "Due", "currency": "CNY", "net_receivable": 500},
            {"id": "USD", "entity_id": "cn_studio", "period": "2026-01", "game": "G", "channel": "USD", "currency": "USD", "net_receivable": 1000},
        ]
        register = build_receivables_register(settlements, [], "2026-03-29")
        rows = {row["id"]: row for row in register["rows"]}
        self.assertEqual(rows["OLD"]["collection_priority"], "P1")
        self.assertEqual(rows["DUE"]["collection_priority"], "P2")
        self.assertEqual(rows["OLD"]["collection_priority_rank"], 1)
        self.assertEqual(rows["DUE"]["collection_priority_rank"], 2)
        usd_queue = next(queue for queue in register["priority_queues"] if queue["currency"] == "USD")
        self.assertEqual(usd_queue["items"][0]["collection_priority_rank"], 1)
        self.assertEqual(register["due_soon_count"], 1)

    def test_valid_promise_is_a_scenario_and_does_not_reduce_outstanding(self):
        promise = {
            "entity_id": "", "settlement_id": "S1", "action_type": "回款承诺",
            "promised_date": "2026-04-15", "promised_amount": 300,
            "recorded_at": "2026-03-01T00:00:00+00:00", "owner": "渠道运营",
        }
        row = build_receivables_register(
            [self.settlement], [], "2026-03-31", collection_actions=[promise],
        )["rows"][0]
        self.assertEqual(row["outstanding"], 1000)
        self.assertEqual(row["effective_promised_amount"], 300)
        self.assertEqual(row["promise_scenario_outstanding"], 700)

    def test_collection_action_preserves_entity_and_validates_amount(self):
        receivable = build_receivables_register(
            [{**self.settlement, "entity_id": "sg_publisher"}], [], "2026-02-01",
        )["rows"][0]
        action = create_collection_action(
            receivable, "争议登记", "海外发行", "财务", action_date="2026-02-02",
            note="平台扣款明细与合同口径不一致", dispute_reason="平台扣款明细待补",
        )
        self.assertEqual(action["entity_id"], "sg_publisher")
        with self.assertRaisesRegex(ValueError, "超过当前未回款"):
            create_collection_action(
                receivable, "回款承诺", "海外发行", "财务", action_date="2026-02-02",
                promised_date="2026-03-01", promised_amount=1001, note="对方承诺下月支付款项",
            )

    def test_dispute_suspends_old_promise_until_a_new_promise_is_recorded(self):
        promise = {
            "id": "C1", "entity_id": "", "settlement_id": "S1", "action_type": "回款承诺",
            "promised_date": "2026-03-20", "promised_amount": 500,
            "recorded_at": "2026-02-01T00:00:00+00:00", "owner": "渠道运营",
        }
        dispute = {
            "id": "C2", "entity_id": "", "settlement_id": "S1", "action_type": "争议登记",
            "recorded_at": "2026-02-10T00:00:00+00:00", "owner": "渠道运营",
            "dispute_reason": "平台扣款差异待核对",
        }
        row = build_receivables_register(
            [self.settlement], [], "2026-03-01", collection_actions=[promise, dispute],
        )["rows"][0]
        self.assertTrue(row["promise_suspended_by_dispute"])
        self.assertIsNone(row["promised_date"])
        self.assertEqual(row["collection_status"], "争议处理中")
        self.assertEqual(row["collection_priority"], "P0")
        self.assertEqual(row["effective_promised_amount"], 0)

    def test_currency_mismatch_and_duplicate_allocation_are_blocked(self):
        with self.assertRaises(ValueError):
            create_cash_allocation({**self.receipt1, "currency": "CNY"}, "receivable", self.settlement, 600, [], "财务")
        first = create_cash_allocation(self.receipt1, "receivable", self.settlement, 600, [], "财务")
        with self.assertRaises(ValueError):
            create_cash_allocation(self.receipt1, "receivable", self.settlement, 1, [first], "财务")

    def test_cross_entity_allocation_is_blocked(self):
        transaction = {**self.receipt1, "entity_id": "sg_publisher"}
        target = {**self.settlement, "entity_id": "cn_studio"}
        with self.assertRaisesRegex(ValueError, "禁止跨主体核销"):
            create_cash_allocation(transaction, "receivable", target, 600, [], "财务")

    def test_historical_payment_without_authorization_is_visible_not_rejected(self):
        transaction = {"id": "B3", "direction": "支出", "currency": "CNY", "amount": 100}
        payable = {"id": "P3", "currency": "CNY", "payable_amount": 100}
        allocation = create_cash_allocation(transaction, "payable", payable, 100, [], "财务")
        self.assertTrue(allocation["authorization_gap"])
        self.assertIn("待补付款授权", allocation["status"])
        authorization = {
            "id": "PAY-APPROVED-001", "status": "已批准", "entity_id": "cn_studio",
            "target_type": "payable", "target_id": "P3", "currency": "CNY", "amount": 100,
        }
        authorized = create_cash_allocation(
            {**transaction, "id": "B4", "entity_id": "cn_studio"}, "payable",
            {**payable, "entity_id": "cn_studio"}, 100, [], "财务",
            authorization_reference="PAY-APPROVED-001", authorization=authorization,
        )
        self.assertFalse(authorized["authorization_gap"])

    def test_fake_or_overused_payment_authorization_is_rejected(self):
        transaction = {"id": "B3", "entity_id": "cn_studio", "direction": "支出", "currency": "CNY", "amount": 100}
        payable = {"id": "P3", "entity_id": "cn_studio", "currency": "CNY", "payable_amount": 100}
        with self.assertRaisesRegex(ValueError, "不存在"):
            create_cash_allocation(
                transaction, "payable", payable, 100, [], "财务",
                authorization_reference="MADE-UP",
            )
        authorization = {
            "id": "PAY-1", "status": "已批准", "entity_id": "cn_studio",
            "target_type": "payable", "target_id": "P3", "currency": "CNY", "amount": 100,
        }
        prior = {"entity_id": "cn_studio", "amount": 60, "status": "已核销", "authorization_reference": "PAY-1"}
        with self.assertRaisesRegex(ValueError, "批准金额"):
            create_cash_allocation(
                transaction, "payable", payable, 50, [prior], "财务",
                authorization_reference="PAY-1", authorization=authorization,
            )

    def test_payable_reconciles_declared_and_bank_paid_without_double_counting(self):
        purchase = {"id": "P1", "accepted_amount": 100, "invoice_amount": 100, "paid_amount": 100, "currency": "CNY", "anomalies": []}
        invoice = {"total_amount": 100, "verification_status": "已查验", "anomalies": [], "purchase_match": {"purchase_id": "P1"}}
        allocation = {"target_type": "payable", "target_id": "P1", "amount": 100, "status": "已核销"}
        row = build_payables_register([purchase], [invoice], [allocation])["rows"][0]
        self.assertEqual(row["reconciled_paid_amount"], 100)
        self.assertEqual(row["status"], "已付清")

    def test_payment_requires_acceptance_verified_invoice_and_human_approval(self):
        payable = {"id": "P1", "currency": "CNY", "accepted_amount": 100, "verified_invoice_amount": 100, "outstanding": 100}
        request = create_payment_request("payable", payable, 100, "经办人", evidence=["验收单", "发票"])
        self.assertEqual(request["status"], "待批准")
        approved = decide_payment_request(request, "批准", "负责人", "证据齐全同意付款")
        self.assertEqual(approved["status"], "已批准")
        blocked = create_payment_request("payable", {**payable, "verified_invoice_amount": 0}, 100, "经办人", evidence=["验收单"])
        self.assertEqual(blocked["status"], "阻塞")
        with self.assertRaisesRegex(ValueError, "不能审批自己"):
            decide_payment_request(request, "批准", "经办人", "本人确认可以支付")

    def test_payable_exposes_only_acceptance_and_verified_invoice_common_capacity(self):
        purchase = {
            "id": "P1", "accepted_amount": 100, "invoice_amount": 100,
            "paid_amount": 20, "currency": "CNY", "anomalies": [],
        }
        invoices = [{
            "total_amount": 60, "verification_status": "已查验", "anomalies": [],
            "purchase_match": {"purchase_id": "P1", "eligible_for_payment": True},
        }, {
            "total_amount": 40, "verification_status": "待查验", "anomalies": ["尚未完成发票查验"],
            "purchase_match": {"purchase_id": "P1", "eligible_for_payment": True},
        }]
        row = build_payables_register([purchase], invoices, [])["rows"][0]
        self.assertEqual(row["payment_eligible_amount"], 60)
        self.assertEqual(row["payment_available_amount"], 40)
        blocked = create_payment_request("payable", row, 50, "经办人", evidence=["验收单", "发票"])
        self.assertEqual(blocked["status"], "阻塞")
        self.assertTrue(any("共同支持" in item for item in blocked["blockers"]))

    def test_duplicate_pending_payment_requests_cannot_exceed_outstanding(self):
        payable = {"id": "P1", "entity_id": "cn_studio", "currency": "CNY", "accepted_amount": 100, "verified_invoice_amount": 100, "outstanding": 100}
        first = create_payment_request("payable", payable, 70, "经办人", evidence=["验收单", "发票"])
        second = create_payment_request(
            "payable", payable, 40, "经办人", evidence=["验收单", "发票"],
            existing_requests=[first],
        )
        self.assertEqual(second["status"], "阻塞")
        self.assertTrue(any("超过当前未付余额" in item for item in second["blockers"]))

    def test_expense_claim_needs_evidence_then_can_be_partially_approved(self):
        claim = create_expense_claim("员工A", "2026-01-10", 500, "CNY", "游戏G", "差旅", "出差拜访渠道", ["发票"], "员工A", "cn_studio")
        self.assertEqual(claim["entity_id"], "cn_studio")
        approved = decide_expense_claim(claim, "批准", "项目负责人", "业务真实且符合预算", 450)
        self.assertEqual(approved["approved_amount"], 450)
        self.assertEqual(approved["status"], "已批准待付款")
        with self.assertRaisesRegex(ValueError, "不能审批自己"):
            decide_expense_claim(claim, "批准", "员工A", "本人确认费用真实", 450)

    def test_payroll_is_aggregated_into_privacy_safe_payment_batch(self):
        rows = [
            {"period": "2026-01", "employee_masked": "员工-A", "net_salary": 100, "anomalies": []},
            {"period": "2026-01", "employee_masked": "员工-B", "net_salary": 200, "anomalies": []},
        ]
        batch = build_payroll_payables(rows, [
            {"target_type": "payroll", "target_id": "PAYROLL-2026-01-CNY", "amount": 100, "status": "已核销"}
        ])["rows"][0]
        self.assertEqual(batch["approved_amount"], 300)
        self.assertEqual(batch["outstanding"], 200)
        self.assertNotIn("employee_masked", batch)

    def test_payroll_batches_do_not_merge_legal_entities(self):
        rows = [
            {"entity_id": "cn_studio", "period": "2026-01", "currency": "CNY", "net_salary": 100, "anomalies": []},
            {"entity_id": "sg_publisher", "period": "2026-01", "currency": "CNY", "net_salary": 200, "anomalies": []},
        ]
        batches = build_payroll_payables(rows, [])["rows"]
        self.assertEqual({item["entity_id"] for item in batches}, {"cn_studio", "sg_publisher"})
        self.assertEqual({item["approved_amount"] for item in batches}, {100, 200})

    def test_payment_request_preserves_target_entity(self):
        payable = {"id": "P-CN", "entity_id": "cn_studio", "currency": "CNY", "accepted_amount": 100, "verified_invoice_amount": 100, "outstanding": 100}
        request = create_payment_request("payable", payable, 100, "经办人", evidence=["验收单", "发票"])
        self.assertEqual(request["entity_id"], "cn_studio")

    def test_payable_payment_binds_approved_vendor_account_fingerprint(self):
        payable = {
            "id": "P-CN", "entity_id": "cn_studio", "vendor": "供应商A", "currency": "CNY",
            "accepted_amount": 100, "verified_invoice_amount": 100, "outstanding": 100,
        }
        account = {
            "id": "VBANK-1", "entity_id": "cn_studio", "vendor": "供应商A", "currency": "CNY",
            "status": "已批准", "account_masked": "•••• 9012", "account_fingerprint": "abc123",
            "beneficiary_name": "供应商A有限公司", "bank_name": "测试银行",
        }
        request = create_payment_request(
            "payable", payable, 100, "经办人", evidence=["验收单", "发票"],
            vendor_bank_accounts=[account], bank_account_id="VBANK-1", require_approved_vendor_account=True,
        )
        self.assertEqual(request["status"], "待批准")
        self.assertEqual(request["vendor_bank_binding"]["account_fingerprint"], "abc123")
        approved = decide_payment_request(request, "批准", "负责人", "已复核当前有效收款账户", [account])
        self.assertEqual(approved["status"], "已批准")
        with self.assertRaisesRegex(ValueError, "已停用或变更"):
            decide_payment_request(request, "批准", "负责人", "尝试批准已失效的收款账户", [{**account, "status": "已停用"}])
        blocked = create_payment_request(
            "payable", payable, 100, "经办人", evidence=["验收单", "发票"],
            vendor_bank_accounts=[account], require_approved_vendor_account=True,
        )
        self.assertEqual(blocked["status"], "阻塞")
        self.assertIn("独立复核", "；".join(blocked["blockers"]))

    def test_overview_surfaces_unallocated_bank_and_workflow_alerts(self):
        overview = build_flow_overview({
            "settlements": [self.settlement], "purchases": [], "invoices": [], "payroll_rows": [],
            "cash_allocations": [], "bank_transactions": [{"id": "B9", "status": "待认领"}],
            "payment_requests": [], "expense_claims": [],
        }, "2026-04-01")
        self.assertEqual(overview["bank_unallocated_count"], 1)
        self.assertTrue(any(item["type"] == "未认领流水" for item in overview["alerts"]))

    def test_group_overview_never_cross_applies_same_ids_between_entities(self):
        settlements = [
            {"id": "SAME", "entity_id": "cn_studio", "period": "2026-01", "currency": "CNY", "net_receivable": 100},
            {"id": "SAME", "entity_id": "sg_publisher", "period": "2026-01", "currency": "CNY", "net_receivable": 200},
        ]
        allocations = [{
            "id": "A1", "entity_id": "cn_studio", "target_type": "receivable",
            "target_id": "SAME", "amount": 100, "status": "已核销",
        }]
        rows = build_receivables_register(settlements, allocations, "2026-03-01")["rows"]
        by_entity = {row["entity_id"]: row for row in rows}
        self.assertEqual(by_entity["cn_studio"]["outstanding"], 0)
        self.assertEqual(by_entity["sg_publisher"]["outstanding"], 200)


if __name__ == "__main__":
    unittest.main()
