from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from src.box_pipeline import BoxPipelineError, dispatch_box_pipeline_request
from src.box_runtime import BoxRuntime
from src.default_services import build_default_service_registry


ROOT = Path(__file__).resolve().parents[1]


class MonthCloseControlTests(unittest.TestCase):
    def setUp(self):
        self.runtime = BoxRuntime(
            ROOT / "examples" / "boxes" / "cn_dtc_store.json", ROOT / "packs",
        )
        self.request = json.loads(
            (ROOT / "examples" / "pipelines" / "month_close_control_fixture.json").read_text(
                encoding="utf-8"
            )
        )

    def test_three_sources_produce_stable_candidate_only_founder_briefing(self):
        first = dispatch_box_pipeline_request(self.runtime, self.request)
        second = dispatch_box_pipeline_request(self.runtime, self.request)
        self.assertTrue(first["ready"], first)
        self.assertEqual(first["pipeline"]["run_id"], second["pipeline"]["run_id"])
        self.assertEqual(first["pipeline"]["required_review_gates"], [
            "bank_statement_mapping_review", "bank_balance_reconciliation",
            "accounting_export_mapping_review", "trial_balance_control_total_review",
            "financial_statement_mapping_review", "accounting_policy_decision",
            "month_close_control_review",
        ])
        control = first["services"]["month_close_control"]["output"]["account_controls"][0]
        self.assertEqual(control["statement_ending_balance"], 5579.5)
        self.assertEqual(control["ledger_ending_balance"], 5579.5)
        self.assertEqual(control["difference"], 0.0)
        self.assertTrue(control["source_review_current"])
        self.assertTrue(control["ready_for_month_close_review"])
        briefing = first["founder_briefing"]
        self.assertEqual(briefing["currency_summaries"][0]["profit_before_tax_candidate"], 579.5)
        self.assertTrue(briefing["candidate_only"])
        self.assertFalse(briefing["transaction_matching_or_cash_allocation_performed"])
        self.assertFalse(first["ledger_modified"])
        self.assertFalse(first["posting_performed"])
        self.assertFalse(first["period_close_performed"])
        self.assertFalse(first["external_filing_performed"])

    def test_source_fingerprint_change_invalidates_transaction_review(self):
        request = copy.deepcopy(self.request)
        request["payload"]["bank_gl_mappings"][0]["bank_source_fingerprint"] = "0" * 64
        result = dispatch_box_pipeline_request(self.runtime, request)
        self.assertFalse(result["ready"])
        self.assertEqual(result["blocked_at"], "month_close_control")
        issue_types = {
            item["type"]
            for item in result["services"]["month_close_control"]["output"]["issues"]
        }
        self.assertIn("stale_bank_source_review", issue_types)

    def test_amount_equality_does_not_match_transactions_post_or_close(self):
        result = dispatch_box_pipeline_request(self.runtime, self.request)
        output = result["services"]["month_close_control"]["output"]
        self.assertTrue(result["ready"])
        self.assertFalse(output["transaction_matching_performed"])
        self.assertFalse(output["cash_allocation_performed"])
        self.assertFalse(output["posting_performed"])
        self.assertFalse(output["period_close_performed"])

    def test_unapproved_reconciling_item_blocks_and_is_not_applied(self):
        request = copy.deepcopy(self.request)
        request["payload"]["bank_gl_mappings"][0]["reconciling_items"] = [{
            "item_id": "OUTSTANDING-1",
            "side": "bank",
            "direction": "decrease",
            "amount": 20,
            "reason": "待复核未达付款",
            "evidence": ["bank-item://OUTSTANDING-1"],
            "review_status": "pending",
        }]
        result = dispatch_box_pipeline_request(self.runtime, request)
        output = result["services"]["month_close_control"]["output"]
        self.assertFalse(result["ready"])
        control = output["account_controls"][0]
        self.assertEqual(control["approved_bank_adjustments"], 0.0)
        self.assertEqual(control["adjusted_bank_balance"], 5579.5)
        self.assertIn("pending_reconciling_items", {item["type"] for item in output["issues"]})

    def test_trial_balance_cash_is_derived_not_accepted_from_mapping(self):
        request = copy.deepcopy(self.request)
        request["payload"]["bank_gl_mappings"][0]["ledger_ending_balance"] = 5579.5
        result = dispatch_box_pipeline_request(self.runtime, request)
        control = result["services"]["month_close_control"]["output"]["account_controls"][0]
        self.assertEqual(control["ledger_ending_balance"], 5579.5)
        self.assertNotIn("ledger_ending_balance", control["transaction_review"])

    def test_service_is_statutory_read_only_and_review_gated(self):
        pipeline = dispatch_box_pipeline_request(self.runtime, self.request)
        bank = pipeline["services"]["bank_reconciliation_candidate"]["output"]
        accounting = pipeline["services"]["accounting_close_reconciliation"]["output"]
        trial_lines = pipeline["batches"]["trial_balance"]["datasets"]["finance.trial_balance_lines"]
        mappings = self.request["payload"]["bank_gl_mappings"]
        response = build_default_service_registry().dispatch(
            self.runtime,
            "core.build_month_close_control",
            {
                "period": "2026-08", "bank_reconciliation": bank,
                "accounting_close": accounting, "trial_balance_lines": trial_lines,
                "bank_gl_mappings": mappings,
            },
            entity_id="cn_dtc_company",
        )
        self.assertTrue(response["output"]["ready"])
        self.assertEqual(response["service"]["action_class"], "read")
        definition = next(
            item for item in build_default_service_registry().catalog(self.runtime)
            if item["service_id"] == "core.build_month_close_control"
        )
        self.assertEqual(definition["review_gate"], "month_close_control_review")

    def test_connector_scope_is_forced_to_pipeline_entity(self):
        request = copy.deepcopy(self.request)
        request["payload"]["bank_connector_request"]["default_entity_id"] = "other"
        with self.assertRaisesRegex(BoxPipelineError, "does not match"):
            dispatch_box_pipeline_request(self.runtime, request)


if __name__ == "__main__":
    unittest.main()
