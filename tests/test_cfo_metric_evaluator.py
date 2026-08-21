import unittest
from pathlib import Path

from src.box_runtime import BoxRuntime
from src.cfo_metric_evaluator import (
    CfoMetricEvaluationError,
    evaluate_cfo_metrics,
)
from src.default_services import build_default_service_registry


ROOT = Path(__file__).resolve().parents[1]


class CfoMetricEvaluatorTests(unittest.TestCase):
    def _runtime(self, box_name: str) -> BoxRuntime:
        return BoxRuntime(ROOT / "examples" / "boxes" / box_name, ROOT / "packs")

    @staticmethod
    def _request(runtime: BoxRuntime, **overrides):
        request = {
            "runtime_fingerprint": runtime.snapshot()["fingerprint"],
            "period": "2026-07",
            "currency": "USD",
            "metric_type_ids": ["dtc_net_sales"],
            "operand_values": {
                "gross_order_sales_ex_tax_including_shipping": "1250.25",
                "discounts_and_refunds_ex_tax": "125.20",
            },
            "confirmed_control_type_ids": [
                "order_and_refund_period_scope_aligned",
                "tax_inclusive_policy_confirmed",
            ],
        }
        request.update(overrides)
        return request

    def test_dtc_metrics_use_decimal_strings_and_allowlisted_formulas(self):
        runtime = self._runtime("us_dtc_shopify_stripe_c_corp.json")
        request = self._request(
            runtime,
            metric_type_ids=[
                "dtc_net_sales",
                "dtc_refund_return_rate",
                "dtc_inventory_days_on_hand",
                "dtc_processor_payout_gap",
                "unreconciled_cash_item_count",
            ],
            operand_values={
                "gross_order_sales_ex_tax_including_shipping": "1250.25",
                "discounts_and_refunds_ex_tax": "125.20",
                "refunds_ex_tax": "100.02",
                "gross_merchandise_sales_ex_tax": "1250.25",
                "average_inventory_cost": "600",
                "trailing_twelve_month_cost_of_goods_sold": "2400",
                "expected_processor_payout": "1000.10",
                "observed_bank_payout": "998.85",
                "unresolved_bank_reconciliation_items": 3,
            },
            confirmed_control_type_ids=[
                "order_and_refund_period_scope_aligned",
                "tax_inclusive_policy_confirmed",
                "return_authorization_and_receipt_scope_aligned",
                "period_end_inventory_evidence_confirmed",
                "inventory_cost_basis_approved",
                "processor_fee_refund_and_timing_bridge_confirmed",
                "duplicate_and_timing_items_deduplicated",
                "entity_scope_confirmed",
            ],
        )
        result = evaluate_cfo_metrics(runtime, "us_store", request)
        metrics = {item["metric_type_id"]: item for item in result["metric_results"]}

        self.assertEqual(metrics["dtc_net_sales"]["value"], "1125.05")
        self.assertEqual(metrics["dtc_refund_return_rate"]["value"], "0.08")
        self.assertEqual(metrics["dtc_inventory_days_on_hand"]["value"], "91.25")
        self.assertEqual(metrics["dtc_processor_payout_gap"]["value"], "1.25")
        self.assertEqual(metrics["unreconciled_cash_item_count"]["value"], 3)
        self.assertTrue(result["all_metrics_available"])
        self.assertEqual(result["status_counts"], {"available": 5})
        self.assertEqual(result["currency_basis_type_id"], "legal_entity_functional_currency")
        self.assertTrue(result["formula_allowlist_enforced"])
        self.assertFalse(result["implicit_currency_conversion_performed"])
        self.assertFalse(result["authoritative_accounting_or_statutory_truth_inferred"])

    def test_missing_inputs_controls_and_nonpositive_burn_fail_closed(self):
        runtime = self._runtime("us_dtc_shopify_stripe_c_corp.json")
        request = self._request(
            runtime,
            metric_type_ids=[
                "cash_runway_months",
                "overdue_receivable_ratio",
                "dtc_net_sales",
            ],
            operand_values={
                "unrestricted_cash_balance": "120000",
                "trailing_three_month_average_net_cash_burn": "-1000",
                "overdue_receivable_balance": "25",
                "gross_receivable_balance": "0",
                "gross_order_sales_ex_tax_including_shipping": "100",
            },
            confirmed_control_type_ids=[
                "cash_accounts_reconciled_to_ledger",
                "three_month_period_continuity_confirmed",
                "nonpositive_burn_returns_not_available",
                "receivable_aging_reconciled_to_ledger",
                "credit_and_disputed_balances_reviewed",
            ],
        )
        result = evaluate_cfo_metrics(runtime, "us_store", request)
        metrics = {item["metric_type_id"]: item for item in result["metric_results"]}

        self.assertEqual(
            metrics["cash_runway_months"]["status"],
            "not_available_nonpositive_denominator",
        )
        self.assertEqual(
            metrics["overdue_receivable_ratio"]["status"],
            "not_available_zero_denominator",
        )
        self.assertEqual(metrics["dtc_net_sales"]["status"], "blocked_missing_controls")
        self.assertEqual(
            metrics["dtc_net_sales"]["missing_operand_type_ids"],
            ["discounts_and_refunds_ex_tax"],
        )
        self.assertNotIn("value", metrics["dtc_net_sales"])
        self.assertFalse(result["all_metrics_available"])
        self.assertFalse(result["missing_inputs_inferred_or_filled_with_zero"])

    def test_game_rollforward_and_concentration_validate_vector_total(self):
        runtime = self._runtime("global_game_studio.json")
        base = {
            "runtime_fingerprint": runtime.snapshot()["fingerprint"],
            "period": "2026-07",
            "currency": "CNY",
            "metric_type_ids": [
                "game_deferred_revenue_closing_balance",
                "game_platform_revenue_concentration_ratio",
            ],
            "operand_values": {
                "opening_deferred_revenue": "1000",
                "current_period_deferrals": "400",
                "current_period_releases": "250",
                "total_platform_net_revenue": "1000",
            },
            "vector_operand_values": {
                "platform_net_revenue_by_platform": ["550", "300", "150"],
            },
            "confirmed_control_type_ids": [
                "approved_revenue_policy_bound",
                "release_evidence_period_matched",
                "complete_platform_population_confirmed",
            ],
        }
        result = evaluate_cfo_metrics(runtime, "cn_studio", base)
        metrics = {item["metric_type_id"]: item for item in result["metric_results"]}
        self.assertEqual(metrics["game_deferred_revenue_closing_balance"]["value"], "1150")
        self.assertEqual(metrics["game_platform_revenue_concentration_ratio"]["value"], "0.55")
        self.assertNotIn(
            "platform_net_revenue_by_platform",
            metrics["game_platform_revenue_concentration_ratio"]["operand_snapshot"][
                "scalar_operand_values"
            ],
        )
        self.assertEqual(
            metrics["game_platform_revenue_concentration_ratio"]["operand_snapshot"][
                "vector_operand_summaries"
            ]["platform_net_revenue_by_platform"]["item_count"],
            3,
        )

        base["vector_operand_values"] = {
            "platform_net_revenue_by_platform": ["600", "300", "150"],
        }
        blocked = evaluate_cfo_metrics(runtime, "cn_studio", base)
        concentration = blocked["metric_results"][1]
        self.assertEqual(concentration["status"], "blocked_inconsistent_operands")
        self.assertEqual(concentration["block_reason_type_id"], "vector_total_mismatch")

    def test_request_rejects_stale_scope_implicit_fx_and_unselected_inputs(self):
        runtime = self._runtime("us_dtc_shopify_stripe_c_corp.json")
        invalid_requests = [
            self._request(runtime, runtime_fingerprint="a" * 64),
            self._request(runtime, currency="EUR"),
            self._request(runtime, period="2026-13"),
            self._request(runtime, operand_values={"marketplace_fees": 10}),
            self._request(runtime, confirmed_control_type_ids=["made_up_control"]),
            self._request(runtime, extra="typo"),
        ]
        for request in invalid_requests:
            with self.subTest(request=request), self.assertRaises(CfoMetricEvaluationError):
                evaluate_cfo_metrics(runtime, "us_store", request)

    def test_registry_exposes_and_dispatches_entity_scoped_metric_service(self):
        runtime = self._runtime("us_dtc_shopify_stripe_c_corp.json")
        registry = build_default_service_registry()
        service = next(
            item for item in registry.catalog(runtime)
            if item["service_id"] == "core.evaluate_cfo_metrics"
        )
        self.assertTrue(service["deterministic"])
        self.assertEqual(service["action_class"], "read")
        self.assertEqual(service["entity_scope"], "statutory")
        result = registry.dispatch(
            runtime,
            "core.evaluate_cfo_metrics",
            self._request(runtime),
            entity_id="us_store",
        )
        self.assertEqual(result["service"]["entity_ids"], ["us_store"])
        self.assertEqual(result["output"]["metric_results"][0]["value"], "1125.05")


if __name__ == "__main__":
    unittest.main()
