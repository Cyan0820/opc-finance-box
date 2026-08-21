import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook

from src.procurement import (
    apply_acceptance_decision, apply_delivery_acceptance_decision,
    create_procurement_request, create_purchase_order_from_request, decide_procurement_request,
    parse_purchase_workbook, procurement_budget_snapshot, procurement_payload,
    procurement_workflow_payload, record_purchase_delivery,
)


def make_order_register(path: Path) -> Path:
    book = Workbook()
    sheet = book.active
    sheet.title = "采购订单"
    sheet.append(["订单编号", "下单日期", "项目", "供应商", "名称", "平标", "单价", "订单金额", "币种", "备注"])
    for index in range(14):
        po = f"PO-{index // 3 + 1:03d}"
        quantity = 167_115 if index == 0 else 1_000 + index
        unit_price = 0.1206 if index == 0 else 10
        amount = round(quantity * unit_price, 2)
        note = "交付已上传，未开票" if index < 4 else "待验收"
        sheet.append([po, "2026-07-01", "游戏甲", f"供应商{index + 1}", None, quantity, unit_price, amount, "CNY", note])
    book.save(path)
    return path


class ProcurementTests(unittest.TestCase):
    def _approved_request(self):
        request = {
            "id": "PR-CHAIN", "entity_id": "cn_studio", "project": "游戏A", "category": "素材制作",
            "description": "版本美术外包", "amount": 100000, "currency": "CNY", "status": "已批准",
            "selected_vendor": "供应商A", "budget_snapshot": {"source_line_ids": ["B1"]},
        }
        return request

    def test_approved_request_creates_order_with_budget_and_milestones(self):
        updated, order = create_purchase_order_from_request(
            self._approved_request(), po_number="PO-2026-001", order_date="2026-03-01", actor="采购经办",
            item="角色立绘与商店页", evidence=["双方订单确认"], milestones=[
                {"title": "角色立绘", "amount": 60000, "due_date": "2026-03-15", "acceptance_criteria": "源文件齐全且通过评审", "owner": "美术负责人"},
                {"title": "商店页", "amount": 40000, "due_date": "2026-03-25", "acceptance_criteria": "尺寸和语言版本齐全", "owner": "发行负责人"},
            ],
        )
        self.assertEqual(updated["status"], "已下单")
        self.assertEqual(order["procurement_request_id"], "PR-CHAIN")
        self.assertEqual(order["evidence"]["budget_snapshot"]["source_line_ids"], ["B1"])
        self.assertEqual(sum(row["amount"] for row in order["milestones"]), 100000)
        with self.assertRaisesRegex(ValueError, "合计"):
            create_purchase_order_from_request(
                self._approved_request(), po_number="PO-BAD", order_date="2026-03-01", actor="采购经办",
                evidence=["订单"], milestones=[{"title": "一期", "amount": 90000, "due_date": "2026-03-15", "acceptance_criteria": "完成全部素材", "owner": "制作人"}],
            )

    def test_delivery_must_be_recorded_before_milestone_acceptance(self):
        _, order = create_purchase_order_from_request(
            self._approved_request(), po_number="PO-2026-002", order_date="2026-03-01", actor="采购经办",
            evidence=["双方订单确认"], milestones=[
                {"title": "全部交付", "amount": 100000, "due_date": "2026-03-20", "acceptance_criteria": "源文件与清单完整", "owner": "制作人"},
            ],
        )
        milestone_id = order["milestones"][0]["id"]
        delivery = record_purchase_delivery(
            order, milestone_id=milestone_id, delivered_amount=100000, delivery_date="2026-03-18",
            delivered_by="供应商A", evidence=["交付清单", "文件哈希"], existing_deliveries=[],
        )
        self.assertEqual(delivery["status"], "已交付待验收")
        accepted_order, accepted_delivery = apply_delivery_acceptance_decision(
            order, delivery, "全部验收", "制作人", evidence=["验收截图"], period="2026-03",
            all_deliveries=[delivery],
        )
        self.assertEqual(accepted_delivery["status"], "已验收")
        self.assertEqual(accepted_order["accepted_amount"], 100000)
        self.assertEqual(accepted_order["acceptance_history"][0]["delivery_id"], delivery["id"])
        self.assertTrue(accepted_order["workflow"]["can_request_invoice"])
        with self.assertRaisesRegex(ValueError, "已处理"):
            apply_delivery_acceptance_decision(accepted_order, accepted_delivery, "全部验收", "制作人", evidence=["重复"])

    def test_delivery_over_milestone_and_acceptance_without_evidence_are_blocked(self):
        _, order = create_purchase_order_from_request(
            self._approved_request(), po_number="PO-2026-003", order_date="2026-03-01", actor="采购经办",
            evidence=["订单"], milestones=[
                {"title": "全部交付", "amount": 100000, "due_date": "2026-03-20", "acceptance_criteria": "完成全部工作", "owner": "制作人"},
            ],
        )
        milestone_id = order["milestones"][0]["id"]
        with self.assertRaisesRegex(ValueError, "超过里程碑"):
            record_purchase_delivery(
                order, milestone_id=milestone_id, delivered_amount=100001, delivery_date="2026-03-18",
                delivered_by="供应商A", evidence=["交付清单"], existing_deliveries=[],
            )
        delivery = record_purchase_delivery(
            order, milestone_id=milestone_id, delivered_amount=100000, delivery_date="2026-03-18",
            delivered_by="供应商A", evidence=["交付清单"], existing_deliveries=[],
        )
        with self.assertRaisesRegex(ValueError, "验收证据"):
            apply_delivery_acceptance_decision(order, delivery, "全部验收", "制作人", all_deliveries=[delivery])

    def test_workflow_payload_keeps_entity_scope(self):
        payload = procurement_workflow_payload(
            [{"id": "R1", "entity_id": "cn_studio", "status": "已批准"}, {"id": "R2", "entity_id": "sg_publisher", "status": "已批准"}],
            [{"id": "P1", "entity_id": "cn_studio", "procurement_request_id": "R1", "acceptance_status": "待交付"}],
            [{"id": "D1", "entity_id": "cn_studio", "status": "已交付待验收"}], entity_id="cn_studio",
        )
        self.assertEqual([row["id"] for row in payload["requests"]], ["R1"])
        self.assertEqual(payload["summary"]["delivered_to_accept"], 1)

    def test_procurement_request_reserves_matching_budget_and_requires_independent_approval(self):
        snapshot = procurement_budget_snapshot(
            [{"id": "B1", "entity_id": "cn_studio", "project": "游戏A", "category": "素材制作", "period": "2026-03", "currency": "CNY", "direction": "支出", "amount": 100000, "scenario": "基准"}],
            [{"entity_id": "cn_studio", "project": "游戏A", "category": "素材制作", "period": "2026-03", "currency": "CNY", "amount": 20000, "status": "已批准"}],
            entity_id="cn_studio", project="游戏A", category="素材制作", period="2026-03", currency="CNY",
        )
        self.assertEqual(snapshot["available_amount"], 80000)
        ordered_snapshot = procurement_budget_snapshot(
            [{"id": "B1", "entity_id": "cn_studio", "project": "游戏A", "category": "素材制作", "period": "2026-03", "currency": "CNY", "direction": "支出", "amount": 100000, "scenario": "基准"}],
            [{"entity_id": "cn_studio", "project": "游戏A", "category": "素材制作", "period": "2026-03", "currency": "CNY", "amount": 20000, "status": "已下单"}],
            entity_id="cn_studio", project="游戏A", category="素材制作", period="2026-03", currency="CNY",
        )
        self.assertEqual(ordered_snapshot["available_amount"], 80000)
        request = create_procurement_request(
            entity_id="cn_studio", project="游戏A", category="素材制作", description="版本PV素材制作",
            amount=60000, currency="CNY", period="2026-03", needed_by="2026-03-20", requester="制作人",
            sourcing_method="竞争比价", selected_vendor="供应商B",
            quotes=[
                {"vendor": "供应商A", "amount": 50000, "currency": "CNY"},
                {"vendor": "供应商B", "amount": 60000, "currency": "CNY"},
                {"vendor": "供应商C", "amount": 65000, "currency": "CNY"},
            ], evidence=["需求说明", "三家报价"], budget_snapshot=snapshot,
            selection_rationale="供应商B历史交付稳定且本次档期可以满足",
        )
        self.assertEqual(request["status"], "待批准")
        self.assertIn("非最低价", request["warnings"][0])
        with self.assertRaisesRegex(ValueError, "不能审批自己"):
            decide_procurement_request(request, "批准", "制作人", "本人确认业务确有需要")
        approved = decide_procurement_request(request, "批准", "财务负责人", "预算充足且非最低价理由可接受")
        self.assertEqual(approved["status"], "已批准")

    def test_procurement_request_blocks_unsupported_single_source_and_budget_gap(self):
        snapshot = procurement_budget_snapshot(
            [], [], entity_id="sg_publisher", project="Global Game", category="Art",
            period="2026-03", currency="USD",
        )
        request = create_procurement_request(
            entity_id="sg_publisher", project="Global Game", category="Art", description="Launch key art",
            amount=5000, currency="USD", period="2026-03", needed_by="2026-03-20", requester="Producer",
            sourcing_method="单一来源", selected_vendor="Vendor A", quotes=[], evidence=[], budget_snapshot=snapshot,
        )
        self.assertEqual(request["status"], "阻塞")
        self.assertEqual(len(request["blockers"]), 2)
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.order_sample = make_order_register(Path(self.temp.name) / "虚构采购订单.xlsx")

    def tearDown(self):
        self.temp.cleanup()

    def test_partial_acceptance_records_evidence_and_drives_next_actions(self):
        purchase = {
            "id": "P-1", "po_number": "PO-001", "ordered_amount": 10000,
            "accepted_amount": None, "invoice_amount": None, "paid_amount": None,
            "anomalies": ["缺少验收证据"],
        }
        updated = apply_acceptance_decision(
            purchase, "部分验收", "制作人", accepted_amount=6000,
            evidence=["交付包 v1.2", "验收截图"], note="首批角色素材通过", period="2026-07",
        )
        self.assertEqual(updated["acceptance_status"], "部分验收")
        self.assertEqual(updated["accepted_amount"], 6000)
        self.assertEqual(updated["workflow"]["remaining_to_accept"], 4000)
        self.assertTrue(updated["workflow"]["accrual_candidate"])
        self.assertTrue(updated["workflow"]["can_request_invoice"])
        self.assertFalse(updated["workflow"]["can_pay"])
        self.assertEqual(updated["accepted_by"], "制作人")
        self.assertEqual(len(updated["acceptance_history"]), 1)

    def test_acceptance_rejects_unsupported_or_unsafe_decisions(self):
        purchase = {"id": "P-1", "ordered_amount": 10000, "accepted_amount": None, "invoice_amount": None, "paid_amount": None, "anomalies": []}
        with self.assertRaisesRegex(ValueError, "证据"):
            apply_acceptance_decision(purchase, "全部验收", "制作人")
        with self.assertRaisesRegex(ValueError, "小于订单金额"):
            apply_acceptance_decision(purchase, "部分验收", "制作人", accepted_amount=10000, evidence=["验收单"])
        with self.assertRaisesRegex(ValueError, "具体原因"):
            apply_acceptance_decision(purchase, "退回整改", "制作人", note="不好")

    def test_full_acceptance_allows_invoice_and_accrual_but_not_payment_before_invoice(self):
        purchase = {"id": "P-2", "ordered_amount": 5000, "accepted_amount": None, "invoice_amount": None, "paid_amount": None, "anomalies": []}
        updated = apply_acceptance_decision(purchase, "全部验收", "项目负责人", evidence=["验收单.pdf"], period="2026-07")
        self.assertEqual(updated["acceptance_status"], "已验收")
        self.assertEqual(updated["accepted_amount"], 5000)
        self.assertTrue(updated["workflow"]["can_request_invoice"])
        self.assertTrue(updated["workflow"]["accrual_candidate"])
        self.assertFalse(updated["workflow"]["can_pay"])

    def test_order_register_parses_named_quantity_columns(self):
        records = parse_purchase_workbook(self.order_sample)
        self.assertEqual(len(records), 14)
        self.assertEqual(records[0].po_number, "PO-001")
        self.assertEqual(records[0].item, "平标")
        self.assertEqual(records[0].quantity, 167115)
        self.assertAlmostEqual(records[0].unit_price, 0.1206)
        self.assertAlmostEqual(records[0].ordered_amount, 20154.07)

    def test_register_identifies_uninvoiced_accrual_candidates(self):
        records = parse_purchase_workbook(self.order_sample)
        candidates = [record for record in records if any("暂估" in anomaly for anomaly in record.anomalies)]
        self.assertEqual(len(candidates), 4)
        self.assertTrue(all(record.invoice_status == "未开票" for record in candidates))

    def test_procurement_summary_never_invents_acceptance_or_payment(self):
        payload = procurement_payload(parse_purchase_workbook(self.order_sample))
        self.assertEqual(payload["summary"]["po_count"], 5)
        self.assertEqual(payload["summary"]["accepted_amount"], 0)
        self.assertEqual(payload["summary"]["paid_amount"], 0)
        self.assertEqual(payload["summary"]["exception_count"], 4)


if __name__ == "__main__":
    unittest.main()
