from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from src.box_compiler import compile_box_file, preflight_pipeline_request
from src.box_pipeline import BoxPipelineError, dispatch_box_pipeline_request
from src.box_runtime import BoxRuntime


ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / "packs" / "connectors" / "woocommerce"
BOX = ROOT / "examples" / "boxes" / "us_dtc_woocommerce_c_corp.json"


class WooCommercePipelineTests(unittest.TestCase):
    def setUp(self):
        self.runtime = BoxRuntime(BOX, ROOT / "packs")
        fixture = json.loads((PACK / "fixture-order-refunds.json").read_text(encoding="utf-8"))
        self.request = {
            "pipeline_id": "woocommerce.order_refund_close",
            "payload": {"entity_id": "us_dtc_company", "woocommerce_request": fixture},
        }

    def test_fixture_pipeline_is_recordable_candidate_only_and_currency_safe(self):
        result = dispatch_box_pipeline_request(self.runtime, self.request)
        self.assertTrue(result["ready"], result)
        self.assertEqual(result["pipeline"]["pipeline_id"], "woocommerce.order_refund_close")
        self.assertRegex(result["pipeline"]["run_id"], r"^[0-9a-f]{24}$")
        self.assertEqual(result["lineage"]["entity_id"], "us_dtc_company")
        self.assertEqual(result["lineage"]["accepted_record_count"], 3)
        self.assertEqual(result["founder_briefing"]["order_count"], 2)
        self.assertEqual(result["founder_briefing"]["refund_event_count"], 1)
        self.assertEqual(len(result["founder_briefing"]["currency_summary"]), 2)
        self.assertTrue(result["founder_briefing"]["candidate_only"])
        self.assertTrue(result["founder_briefing"]["revenue_claim_prohibited"])
        self.assertTrue(result["founder_briefing"]["tax_liability_claim_prohibited"])
        self.assertFalse(result["external_actions_performed"])
        self.assertFalse(result["network_access_performed"])

    def test_connector_quality_entity_and_capability_fail_closed(self):
        request = copy.deepcopy(self.request)
        request["payload"]["woocommerce_request"]["consumer_secret"] = "inline-private"
        result = dispatch_box_pipeline_request(self.runtime, request)
        self.assertFalse(result["ready"])
        self.assertEqual(result["blocked_at"], "woocommerce_order_refund_connector")
        self.assertTrue(result["retryable"])
        self.assertEqual(result["services"], {})

        request = copy.deepcopy(self.request)
        request["payload"]["woocommerce_request"]["order_pages"][0][0][
            "date_modified_gmt"
        ] = "2026-07-31T23:59:59"
        result = dispatch_box_pipeline_request(self.runtime, request)
        self.assertFalse(result["ready"])
        self.assertEqual(result["blocked_at"], "quality_gate")
        self.assertFalse(result["retryable"])
        self.assertEqual(result["services"], {})

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
        request["payload"]["woocommerce_request"]["default_entity_id"] = "us_store"
        with self.assertRaisesRegex(BoxPipelineError, "Capability"):
            dispatch_box_pipeline_request(runtime_without_pack, request)

    def test_compiler_template_schedule_and_preflight_are_secret_free(self):
        compiled = compile_box_file(BOX, ROOT / "packs")
        pipeline = next(
            item for item in compiled["pipelines"]
            if item["pipeline_id"] == "woocommerce.order_refund_close"
        )
        self.assertEqual(pipeline["implementation_status"], "executable")
        self.assertEqual(pipeline["eligible_entity_ids"], ["us_dtc_company"])
        self.assertEqual(pipeline["required_connectors"], ["woocommerce.order_refund_activity"])
        template = next(
            item for item in compiled["pipeline_request_templates"]["templates"]
            if item["pipeline_id"] == "woocommerce.order_refund_close"
        )
        self.assertEqual(template["entity_id"], "us_dtc_company")
        serialized = json.dumps(template["request"]).lower()
        self.assertNotIn("consumer_key", serialized)
        self.assertNotIn("consumer_secret", serialized)
        self.assertNotIn("site_origin", serialized)
        configuration = " ".join(template["required_configuration"]).lower()
        self.assertIn("opc_woocommerce_site_origin", configuration)
        self.assertIn("opc_woocommerce_consumer_key", configuration)
        self.assertIn("opc_woocommerce_consumer_secret", configuration)
        job = next(
            item for item in compiled["pipeline_schedule_template"]["jobs"]
            if item["pipeline_id"] == "woocommerce.order_refund_close"
        )
        self.assertEqual(job["entity_id"], "us_dtc_company")
        self.assertFalse(job["enabled"])
        preflight = preflight_pipeline_request(self.runtime, self.request)
        self.assertTrue(preflight["ready_to_dispatch"], preflight)


if __name__ == "__main__":
    unittest.main()
