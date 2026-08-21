from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from src.box_compiler import compile_box_file, preflight_pipeline_request
from src.box_pipeline import BoxPipelineError, dispatch_box_pipeline_request
from src.box_runtime import BoxRuntime


ROOT = Path(__file__).resolve().parents[1]
BOX = ROOT / "examples" / "boxes" / "sg_dtc_shopify_stripe_wise_store.json"
REQUEST = ROOT / "examples" / "pipelines" / "shopify_stripe_wise_daily_close_fixture.json"


class ShopifyStripeWisePipelineTests(unittest.TestCase):
    def setUp(self):
        self.runtime = BoxRuntime(BOX, ROOT / "packs")
        self.request = json.loads(REQUEST.read_text(encoding="utf-8"))

    def test_four_source_order_to_bank_chain_is_ready_but_candidate_only(self):
        result = dispatch_box_pipeline_request(self.runtime, self.request)
        self.assertTrue(result["ready"], result)
        self.assertEqual(set(result["connector_batches"]), {
            "shopify.orders", "stripe.balance_transactions", "stripe.payouts",
            "wise.balance_statement",
        })
        payout = result["services"]["stripe_payout_bank_reconciliation"]["output"]
        self.assertEqual(
            payout["reconciliation"][0]["reconciliation_status"],
            "high_confidence_candidate",
        )
        self.assertEqual(
            payout["reconciliation"][0]["match_basis"], "exact_payout_reference"
        )
        self.assertEqual(result["lineage"]["bank_connector_id"], "wise.balance_statement")
        self.assertEqual(result["lineage"]["bank_evidence_count"], 1)
        self.assertIn(
            "wise_statement_access_review", result["pipeline"]["required_review_gates"]
        )
        self.assertTrue(result["founder_briefing"]["candidate_only"])
        self.assertFalse(result["external_actions_performed"])
        self.assertFalse(result["network_access_performed"])

    def test_wise_amount_is_converted_to_integer_minor_units_without_rounding(self):
        invalid = copy.deepcopy(self.request)
        invalid["payload"]["currency_minor_units"] = {"SGD": 0}
        result = dispatch_box_pipeline_request(self.runtime, invalid)
        self.assertFalse(result["ready"])
        self.assertEqual(result["blocked_at"], "bank_minor_unit_adapter")
        self.assertIn("more precision", result["blockers"][0])
        self.assertEqual(result["services"], {})

    def test_direct_bank_rows_and_wise_connector_cannot_be_mixed(self):
        invalid = copy.deepcopy(self.request)
        invalid["payload"]["bank_transactions"] = [{"bank_transaction_id": "injected"}]
        with self.assertRaisesRegex(BoxPipelineError, "not both"):
            dispatch_box_pipeline_request(self.runtime, invalid)

    def test_compiler_selects_secret_free_wise_bank_source_and_preflight_accepts_it(self):
        compiled = compile_box_file(BOX, ROOT / "packs")
        pipeline = next(
            item for item in compiled["pipelines"]
            if item["pipeline_id"] == "dtc.shopify_stripe_daily_close"
        )
        self.assertIn("wise.balance_statement", pipeline["optional_connectors"])
        self.assertIn("wise.balance_statement", pipeline["available_connectors"])
        template = next(
            item for item in compiled["pipeline_request_templates"]["templates"]
            if item["pipeline_id"] == "dtc.shopify_stripe_daily_close"
        )
        payload = template["request"]["payload"]
        self.assertEqual(payload["bank_connector_id"], "wise.balance_statement")
        self.assertNotIn("bank_transactions", payload)
        serialized = json.dumps(payload).lower()
        self.assertNotIn("access_token", serialized)
        self.assertNotIn("profile_id", serialized)
        self.assertNotIn("balance_id", serialized)

        runnable = copy.deepcopy(self.request)
        preflight = preflight_pipeline_request(self.runtime, runnable)
        self.assertTrue(preflight["ready_to_dispatch"], preflight)


if __name__ == "__main__":
    unittest.main()
