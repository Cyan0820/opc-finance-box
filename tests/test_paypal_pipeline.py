from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from src.box_compiler import compile_box_file, preflight_pipeline_request
from src.box_pipeline import BoxPipelineError, dispatch_box_pipeline_request
from src.box_runtime import BoxRuntime


ROOT = Path(__file__).resolve().parents[1]
BOX = ROOT / "examples" / "boxes" / "us_dtc_paypal_c_corp.json"
REQUEST = ROOT / "examples" / "pipelines" / "paypal_transaction_close_fixture.json"


class PayPalPipelineTests(unittest.TestCase):
    def setUp(self):
        self.runtime = BoxRuntime(BOX, ROOT / "packs")
        self.request = json.loads(REQUEST.read_text(encoding="utf-8"))

    def test_fixture_pipeline_is_recordable_candidate_only_and_currency_safe(self):
        result = dispatch_box_pipeline_request(self.runtime, self.request)
        self.assertTrue(result["ready"], result)
        self.assertEqual(result["pipeline"]["pipeline_id"], "paypal.transaction_close")
        self.assertRegex(result["pipeline"]["run_id"], r"^[0-9a-f]{24}$")
        self.assertEqual(result["lineage"]["entity_id"], "us_dtc_company")
        self.assertEqual(result["lineage"]["accepted_record_count"], 3)
        self.assertEqual(result["founder_briefing"]["refund_candidate_count"], 1)
        self.assertEqual(len(result["founder_briefing"]["currency_summary"]), 2)
        self.assertTrue(result["founder_briefing"]["candidate_only"])
        self.assertFalse(result["external_actions_performed"])
        self.assertFalse(result["network_access_performed"])

    def test_connector_and_quality_failures_stop_before_service(self):
        request = copy.deepcopy(self.request)
        request["payload"]["paypal_request"]["client_secret"] = "inline-private"
        result = dispatch_box_pipeline_request(self.runtime, request)
        self.assertFalse(result["ready"])
        self.assertEqual(result["blocked_at"], "paypal_transaction_connector")
        self.assertTrue(result["retryable"])
        self.assertEqual(result["services"], {})

        request = copy.deepcopy(self.request)
        request["payload"]["paypal_request"]["transaction_pages"][0][
            "transaction_details"
        ][0]["transaction_info"]["transaction_initiation_date"] = "2026-07-31T23:59:59Z"
        result = dispatch_box_pipeline_request(self.runtime, request)
        self.assertFalse(result["ready"])
        self.assertEqual(result["blocked_at"], "quality_gate")
        self.assertFalse(result["retryable"])
        self.assertEqual(result["services"], {})

    def test_entity_scope_and_capability_fail_closed(self):
        request = copy.deepcopy(self.request)
        request["payload"]["entity_id"] = "other"
        with self.assertRaisesRegex(BoxPipelineError, "Unknown legal entity"):
            dispatch_box_pipeline_request(self.runtime, request)

        runtime_without_pack = BoxRuntime(
            ROOT / "examples" / "boxes" / "us_dtc_shopify_stripe_c_corp.json",
            ROOT / "packs",
        )
        request = copy.deepcopy(self.request)
        request["payload"]["entity_id"] = "us_store"
        request["payload"]["paypal_request"]["default_entity_id"] = "us_store"
        with self.assertRaisesRegex(BoxPipelineError, "Capability"):
            dispatch_box_pipeline_request(runtime_without_pack, request)

    def test_compiler_template_schedule_and_preflight_are_secret_free(self):
        compiled = compile_box_file(BOX, ROOT / "packs")
        pipeline = next(
            item for item in compiled["pipelines"]
            if item["pipeline_id"] == "paypal.transaction_close"
        )
        self.assertEqual(pipeline["implementation_status"], "executable")
        self.assertEqual(pipeline["eligible_entity_ids"], ["us_dtc_company"])
        self.assertEqual(pipeline["required_connectors"], ["paypal.transaction_activity"])
        template = next(
            item for item in compiled["pipeline_request_templates"]["templates"]
            if item["pipeline_id"] == "paypal.transaction_close"
        )
        self.assertEqual(template["entity_id"], "us_dtc_company")
        serialized = json.dumps(template["request"]).lower()
        self.assertNotIn("client_secret", serialized)
        self.assertNotIn("access_token", serialized)
        job = next(
            item for item in compiled["pipeline_schedule_template"]["jobs"]
            if item["pipeline_id"] == "paypal.transaction_close"
        )
        self.assertEqual(job["entity_id"], "us_dtc_company")
        self.assertFalse(job["enabled"])
        preflight = preflight_pipeline_request(self.runtime, self.request)
        self.assertTrue(preflight["ready_to_dispatch"], preflight)


if __name__ == "__main__":
    unittest.main()
