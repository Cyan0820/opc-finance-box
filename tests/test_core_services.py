import unittest
from pathlib import Path

from src.box_runtime import BoxRuntime
from src.default_services import build_default_service_registry
from src.pack_services import PackServiceError


ROOT = Path(__file__).resolve().parents[1]


class CoreServicesTests(unittest.TestCase):
    def setUp(self):
        self.runtime = BoxRuntime(ROOT / "examples" / "boxes" / "global_game_studio.json", ROOT / "packs")
        self.registry = build_default_service_registry()

    def test_cash_forecast_uses_entity_functional_currency_without_mixing(self):
        result = self.registry.dispatch(
            self.runtime,
            "core.cash_forecast",
            {
                "as_of_period": "2026-07",
                "opening_cash": 1000,
                "minimum_buffer": 600,
                "horizon_months": 3,
                "forecast_lines": [
                    {"id": "F1", "entity_id": "sg_publisher", "period": "2026-08", "direction": "inflow", "amount": 200, "currency": "USD"},
                    {"id": "F2", "entity_id": "sg_publisher", "period": "2026-08", "direction": "outflow", "amount": 700, "currency": "USD"},
                ],
            },
            entity_id="sg_publisher",
        )
        output = result["output"]
        self.assertTrue(output["ready"])
        self.assertEqual(output["currency"], "USD")
        self.assertEqual(output["forecast"][0]["ending_cash"], 500)
        self.assertEqual(output["buffer_breach_period"], "2026-08")

    def test_cash_forecast_blocks_unapproved_fx_mixing(self):
        result = self.registry.dispatch(
            self.runtime,
            "core.cash_forecast",
            {
                "as_of_period": "2026-07",
                "opening_cash": 1000,
                "forecast_lines": [
                    {"id": "F1", "entity_id": "cn_studio", "period": "2026-08", "direction": "inflow", "amount": 200, "currency": "USD"},
                ],
            },
            entity_id="cn_studio",
        )
        self.assertFalse(result["output"]["ready"])
        self.assertEqual(result["output"]["rejected_lines"][0]["reason"], "currency USD requires an explicit FX translation policy")

    def test_close_readiness_never_closes_without_period_close_approval(self):
        controls = {
            key: True for key in (
                "source_data_complete", "bank_reconciled", "receivables_reconciled",
                "payables_reconciled", "vouchers_balanced", "vouchers_reviewed",
                "tax_workpaper_reviewed",
            )
        }
        result = self.registry.dispatch(
            self.runtime,
            "core.close_readiness",
            {"period": "2026-07", "controls": controls},
            entity_id="cn_studio",
        )["output"]
        self.assertTrue(result["ready_for_period_close_approval"])
        self.assertFalse(result["can_close"])
        self.assertIn("period_close_approval", result["blockers"])

    def test_procurement_summary_keeps_currencies_separate(self):
        result = self.registry.dispatch(
            self.runtime,
            "core.procure_to_pay_summary",
            {"purchases": [
                {"id": "P1", "entity_id": "cn_studio", "currency": "CNY", "ordered_amount": 100, "accepted_amount": 80, "invoice_amount": 60, "paid_amount": 30, "evidence": ["po"]},
                {"id": "P2", "entity_id": "cn_studio", "currency": "USD", "ordered_amount": 10, "accepted_amount": 10, "invoice_amount": 10, "paid_amount": 0, "evidence": ["po"]},
            ]},
            entity_id="cn_studio",
        )["output"]
        self.assertEqual([row["currency"] for row in result["currency_summaries"]], ["CNY", "USD"])
        self.assertEqual(result["currency_summaries"][0]["invoiced_unpaid"], 30)

    def test_statutory_service_rejects_another_entity_records(self):
        with self.assertRaisesRegex(ValueError, "outside statutory entity"):
            self.registry.dispatch(
                self.runtime,
                "core.reconcile_bank_activity",
                {"period": "2026-07", "transactions": [{"id": "B1", "entity_id": "sg_publisher"}]},
                entity_id="cn_studio",
            )

    def test_evidence_service_can_validate_management_scope_without_erasing_entities(self):
        result = self.registry.dispatch(
            self.runtime,
            "core.validate_evidence_lineage",
            {"datasets": {"orders": [
                {"entity_id": "cn_studio", "evidence": {"source_file": "a.csv", "batch_id": "B1"}},
                {"entity_id": "sg_publisher", "evidence": {"source_file": "b.csv", "batch_id": "B2"}},
            ]}},
        )["output"]
        self.assertTrue(result["ready"])
        self.assertEqual(set(result["entity_ids"]), {"cn_studio", "sg_publisher"})


if __name__ == "__main__":
    unittest.main()
