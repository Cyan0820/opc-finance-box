import unittest

from src.first_close_readiness import build_first_close_readiness, make_not_applicable_declaration


ENTITY = {
    "id": "cn_studio", "name": "星火游戏（上海）有限公司", "jurisdiction": "CN",
    "functional_currency": "CNY", "accounting_basis": "CAS", "tax_readiness": "workpaper",
}


def empty_datasets():
    return {name: [] for name in (
        "settlements", "bank_transactions", "opening_balances", "purchases", "invoices",
        "payroll_rows", "plan_lines", "game_kpis", "onboarding_declarations",
    )}


class FirstCloseReadinessTests(unittest.TestCase):
    def test_candidate_document_does_not_count_as_completed_ledger(self):
        result = build_first_close_readiness(
            entity_id="cn_studio", period="2026-01", entity=ENTITY, profile_gaps=[],
            datasets=empty_datasets(), documents=[{
                "id": "DOC-1", "status": "已解析待入账",
                "entity_scope": {"status": "confirmed", "entity_id": "cn_studio"},
                "classification": {"document_type": "settlement", "periods": ["2026-01"]},
                "recognition": {"period": "2026-01"},
            }], master_records=[
                {"record_type": "game", "status": "可用"},
                {"record_type": "channel", "status": "可用"},
            ],
        )
        income = next(item for item in result["items"] if item["domain"] == "settlements")
        self.assertEqual(income["status"], "候选待处理")
        self.assertFalse(result["ready_for_shadow_close"])

    def test_complete_required_ledgers_and_evidenced_na_can_enter_shadow_close(self):
        datasets = empty_datasets()
        datasets.update({
            "settlements": [{"entity_id": "cn_studio", "period": "2026-01"}],
            "bank_transactions": [{"entity_id": "cn_studio", "transaction_date": "2026-01-31"}],
            "opening_balances": [{"entity_id": "cn_studio", "period": "2026-01"}],
        })
        for domain in ("purchases", "invoices", "payroll_rows"):
            datasets["onboarding_declarations"].append(make_not_applicable_declaration(
                entity_id="cn_studio", period="2026-01", domain=domain,
                decision="本期不适用", actor="复核人", rationale="经业务负责人确认本期确实没有相关业务",
                evidence=["本期业务清单"], now="2026-02-01T00:00:00+00:00",
            ))
        result = build_first_close_readiness(
            entity_id="cn_studio", period="2026-01", entity=ENTITY, profile_gaps=[],
            datasets=datasets, master_records=[
                {"record_type": "game", "status": "可用"},
                {"record_type": "channel", "status": "可用"},
            ],
        )
        self.assertTrue(result["ready_for_shadow_close"])
        self.assertFalse(result["ready_for_statutory_release"])

    def test_bank_and_opening_balance_cannot_be_declared_not_applicable(self):
        with self.assertRaisesRegex(ValueError, "不能声明"):
            make_not_applicable_declaration(
                entity_id="cn_studio", period="2026-01", domain="bank_transactions",
                decision="本期不适用", actor="复核人", rationale="本期没有银行账户业务",
                evidence=["确认函"], now="2026-02-01T00:00:00+00:00",
            )

    def test_not_applicable_requires_auditable_evidence(self):
        with self.assertRaisesRegex(ValueError, "证据"):
            make_not_applicable_declaration(
                entity_id="sg_publisher", period="2026-01", domain="invoices",
                decision="本期不适用", actor="复核人", rationale="海外主体本期没有供应商发票业务",
                evidence=[], now="2026-02-01T00:00:00+00:00",
            )

    def test_withdrawing_na_reopens_the_domain_blocker(self):
        datasets = empty_datasets()
        datasets["onboarding_declarations"] = [make_not_applicable_declaration(
            entity_id="cn_studio", period="2026-01", domain="purchases",
            decision="撤销不适用", actor="复核人", rationale="发现本期确有一笔云服务采购尚未导入",
            evidence=[], now="2026-02-02T00:00:00+00:00",
        )]
        result = build_first_close_readiness(
            entity_id="cn_studio", period="2026-01", entity=ENTITY, profile_gaps=[],
            datasets=datasets, master_records=[],
        )
        purchase = next(row for row in result["items"] if row["domain"] == "purchases")
        self.assertEqual(purchase["status"], "阻塞")
        self.assertIn("撤销", purchase["evidence"])

    def test_open_prior_period_purchase_is_included_in_current_month(self):
        datasets = empty_datasets()
        datasets["purchases"] = [{
            "entity_id": "cn_studio", "order_date": "2025-12-20",
            "acceptance_status": "已交付待验收", "payment_status": "未付款",
        }]
        result = build_first_close_readiness(
            entity_id="cn_studio", period="2026-01", entity=ENTITY, profile_gaps=[],
            datasets=datasets, master_records=[],
        )
        purchase = next(row for row in result["items"] if row["domain"] == "purchases")
        self.assertEqual(purchase["status"], "完成")
        self.assertEqual(purchase["record_count"], 1)

    def test_demo_legal_name_does_not_pass_entity_profile_gate(self):
        demo_entity = {**ENTITY, "name": "星火游戏（演示）有限公司"}
        result = build_first_close_readiness(
            entity_id="cn_studio", period="2026-01", entity=demo_entity, profile_gaps=[],
            datasets=empty_datasets(), master_records=[],
        )
        self.assertEqual(result["entity_profile"]["status"], "阻塞")
        self.assertIn("主体与核算规则", result["blockers"])
