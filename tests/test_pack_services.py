import unittest
from pathlib import Path

from src.box_runtime import BoxRuntime
from src.default_services import build_default_service_registry
from src.pack_services import PackServiceError, PackServiceRegistry, ServiceDefinition


ROOT = Path(__file__).resolve().parents[1]


class PackServiceTests(unittest.TestCase):
    def setUp(self):
        self.dtc = BoxRuntime(ROOT / "examples" / "boxes" / "cn_dtc_store.json", ROOT / "packs")
        self.game = BoxRuntime(ROOT / "examples" / "boxes" / "global_game_studio.json", ROOT / "packs")
        self.registry = build_default_service_registry()

    def test_catalog_only_exposes_services_enabled_by_box(self):
        core_services = {
            "agent.build_plan_snapshot",
            "agent.create_approval_event_draft",
            "agent.create_goal_draft",
            "core.cash_forecast",
            "core.close_readiness",
            "core.procure_to_pay_summary",
            "core.reconcile_bank_activity",
            "core.reconcile_accounting_close_exports",
            "core.build_month_close_control",
            "core.discover_first_close_configuration",
            "core.evaluate_cfo_metrics",
            "core.validate_trial_balance_import",
            "core.validate_evidence_lineage",
        }
        self.assertEqual(
            {item["service_id"] for item in self.registry.catalog(self.dtc)},
            core_services | {
                "connector.extract_image_ocr",
                "connector.extract_text_pdf",
                "commerce.analyze",
                "commerce.order_to_cash",
                "commerce.refund_summary",
                "commerce.reconcile_return_inventory",
                "commerce.build_import_landed_cost_candidates",
                "commerce.fulfillment_cost_summary",
                "commerce.calculate_inventory_cost",
                "commerce.import_and_analyze",
                "dtc.reconcile_payments",
                "dtc.reconcile_refunds",
                "dtc.destination_evidence",
                "tax.cn.build_calendar",
                "tax.cn.vat_workpaper",
                "tax.cn.cit_prepaid_workpaper",
                "tax.cn.stamp_tax_workpaper",
                "tax.cn.iit_withholding_workpaper",
            },
        )
        self.assertEqual(
            {item["service_id"] for item in self.registry.catalog(self.game)},
            core_services | {
                "connector.extract_image_ocr",
                "connector.extract_text_pdf",
                "game.analyze_kpis",
                "game.reconcile_channel_settlements",
                "game.project_profitability",
                "game.ltv_roi_review",
                "game.draft_revenue_policy",
                "game.calculate_revenue_recognition",
                "app_store.reconcile_fees",
                "app_store.receivable",
                "google_play.reconcile_fees",
                "google_play.receivable",
                "domestic_game.reconcile_share",
                "domestic_game.receivable",
                "entity.translate_management_balances",
                "entity.build_month_close_portfolio",
                "entity.review_intercompany_adjustments",
                "entity.consolidate_management_view",
                "tax.cn.build_calendar",
                "tax.cn.vat_workpaper",
                "tax.cn.cit_prepaid_workpaper",
                "tax.cn.stamp_tax_workpaper",
                "tax.cn.iit_withholding_workpaper",
                "tax.sg.build_calendar",
                "tax.sg.registration_profile",
                "tax.sg.evidence_checklist",
            },
        )

    def test_commerce_service_dispatches_with_management_entity_scope(self):
        result = self.registry.dispatch(self.dtc, "commerce.analyze", {
            "orders": [{
                "order_id": "O1", "entity_id": "cn_dtc_company", "period": "2026-07",
                "channel": "DTC", "destination_country": "US", "currency": "USD",
                "merchandise_gross_ex_tax": 100,
            }],
            "settlements": [{
                "settlement_id": "S1", "entity_id": "cn_dtc_company", "period": "2026-07",
                "channel": "DTC", "currency": "USD", "reported_order_inflow": 100,
                "payout": 100,
            }],
        })
        self.assertTrue(result["output"]["ready"])
        self.assertEqual(result["service"]["entity_ids"], ["cn_dtc_company"])
        self.assertTrue(result["service"]["deterministic"])

    def test_service_from_unselected_pack_is_rejected(self):
        with self.assertRaises(PackServiceError):
            self.registry.dispatch(self.game, "commerce.analyze", {})

    def test_external_service_requires_configured_gate_and_approval(self):
        registry = PackServiceRegistry()
        registry.register(ServiceDefinition(
            service_id="test.external",
            pack_id="core.finance",
            capability="finance.record_to_report",
            display_name="Test external action",
            handler=lambda payload, context: {"done": True},
            deterministic=True,
            action_class="external",
            entity_scope="statutory",
            review_gate="external_filing",
        ))
        with self.assertRaises(PackServiceError):
            registry.dispatch(self.dtc, "test.external", {}, entity_id="cn_dtc_company")
        result = registry.dispatch(
            self.dtc,
            "test.external",
            {},
            entity_id="cn_dtc_company",
            approval={
                "gate": "external_filing",
                "decision": "approved",
                "approved_by": "有权申报人",
                "approved_at": "2026-08-13T12:00:00+08:00",
            },
        )
        self.assertTrue(result["output"]["done"])
        self.assertEqual(result["service"]["approval"]["approved_by"], "有权申报人")

    def test_mutating_service_without_review_gate_cannot_register(self):
        registry = PackServiceRegistry()
        with self.assertRaises(PackServiceError):
            registry.register(ServiceDefinition(
                service_id="bad.mutation",
                pack_id="core.finance",
                capability="finance.record_to_report",
                display_name="Bad mutation",
                handler=lambda payload, context: {},
                deterministic=True,
                action_class="mutating",
                entity_scope="none",
            ))

    def test_game_kpi_service_requires_entity_scoped_records(self):
        blocked = self.registry.dispatch(self.game, "game.analyze_kpis", {
            "rows": [{"id": "K1", "mau": 100, "payers": 10, "gross_bookings": 1000}],
        })
        self.assertFalse(blocked["output"]["ready"])
        ready = self.registry.dispatch(self.game, "game.analyze_kpis", {
            "rows": [{
                "id": "K1", "entity_id": "cn_studio", "project_code": "G1", "status": "可用",
                "mau": 100, "payers": 10, "gross_bookings": 1000, "marketing_spend": 500,
            }],
            "game_codes": ["G1"],
        })
        self.assertTrue(ready["output"]["ready"])
        self.assertEqual(ready["output"]["rows"][0]["gross_roas"], 2)

    def test_game_revenue_policy_is_a_statutory_draft(self):
        payload = {
            "game": "G1", "channel": "App Store", "revenue_stream": "游戏内购",
            "presentation": "净额法", "recognition_method": "即时确认",
            "effective_from": "2026-07", "actor": "负责人", "evidence": ["渠道协议"],
            "role_facts": {
                "controls_pricing": False,
                "responsible_for_fulfillment": False,
                "bears_refund_risk": False,
                "controls_virtual_goods": True,
            },
        }
        with self.assertRaises(PackServiceError):
            self.registry.dispatch(self.game, "game.draft_revenue_policy", payload)
        result = self.registry.dispatch(
            self.game, "game.draft_revenue_policy", payload, entity_id="cn_studio"
        )
        self.assertEqual(result["output"]["policy"]["entity_id"], "cn_studio")
        self.assertEqual(result["output"]["output_status"], "draft_pending_accountant_review")

    def test_game_revenue_recognition_uses_approved_policy_inside_entity(self):
        result = self.registry.dispatch(
            self.game,
            "game.calculate_revenue_recognition",
            {
                "target_period": "2026-07",
                "settlements": [{
                    "id": "SET-1", "entity_id": "cn_studio", "period": "2026-07",
                    "game": "G1", "channel": "App Store", "currency": "CNY",
                    "gross": 120, "refunds": 0, "net_receivable": 100,
                }],
                "policies": [{
                    "id": "POL-1", "entity_id": "cn_studio", "status": "已批准",
                    "game": "G1", "channel": "App Store", "effective_from": "2026-07",
                    "presentation": "净额法", "recognition_method": "即时确认",
                }],
            },
            entity_id="cn_studio",
        )
        self.assertTrue(result["output"]["ready"])
        self.assertEqual(result["output"]["summary_by_currency"][0]["recognized_revenue"], 100)
        self.assertEqual(result["output"]["entity_id"], "cn_studio")


if __name__ == "__main__":
    unittest.main()
