from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from src.box_compiler import compile_box_file, preflight_pipeline_request
from src.box_pipeline import BoxPipelineError, dispatch_box_pipeline_request
from src.box_runtime import BoxRuntime


ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / "packs" / "connectors" / "shipbob"
BOX = ROOT / "examples" / "boxes" / "us_dtc_shopify_stripe_shipbob_c_corp.json"


class ShipBobPipelineTests(unittest.TestCase):
    def setUp(self):
        self.runtime = BoxRuntime(BOX, ROOT / "packs")
        fixture = json.loads((PACK / "fixture-fulfillment.json").read_text(encoding="utf-8"))
        self.request = {
            "pipeline_id": "commerce.shipbob_fulfillment_close",
            "payload": {
                "entity_id": "us_dtc_company",
                "shipbob_request": fixture,
            },
        }

    def test_fixture_pipeline_is_ready_recordable_and_candidate_only(self):
        result = dispatch_box_pipeline_request(self.runtime, self.request)
        self.assertTrue(result["ready"], result)
        self.assertEqual(result["pipeline"]["pipeline_id"], "commerce.shipbob_fulfillment_close")
        self.assertRegex(result["pipeline"]["run_id"], r"^[0-9a-f]{24}$")
        self.assertEqual(result["lineage"]["entity_id"], "us_dtc_company")
        self.assertEqual(result["lineage"]["accepted_record_count"], 4)
        self.assertEqual(result["founder_briefing"]["counts"]["shipments"], 1)
        self.assertEqual(result["founder_briefing"]["fulfillment_invoice_by_currency"], [
            {"currency": "USD", "amount": "7.77"},
        ])
        self.assertTrue(result["founder_briefing"]["candidate_only"])
        self.assertFalse(result["external_actions_performed"])
        self.assertFalse(result["network_access_performed"])
        output = result["services"]["fulfillment_and_return_evidence_summary"]["output"]
        self.assertFalse(output["posting_performed"])
        self.assertFalse(output["inventory_adjustment_performed"])

    def test_connector_failure_and_quality_failure_stop_before_service(self):
        request = copy.deepcopy(self.request)
        request["payload"]["shipbob_request"]["access_token"] = "inline-private"
        result = dispatch_box_pipeline_request(self.runtime, request)
        self.assertFalse(result["ready"])
        self.assertEqual(result["blocked_at"], "shipbob_fulfillment_connector")
        self.assertTrue(result["retryable"])
        self.assertEqual(result["services"], {})

        request = copy.deepcopy(self.request)
        request["payload"]["shipbob_request"]["orders"][0]["created_date"] = (
            "2026-07-31T23:59:59Z"
        )
        result = dispatch_box_pipeline_request(self.runtime, request)
        self.assertFalse(result["ready"])
        self.assertEqual(result["blocked_at"], "quality_gate")
        self.assertFalse(result["retryable"])
        self.assertEqual(result["services"], {})

    def test_entity_scope_and_capability_are_fail_closed(self):
        request = copy.deepcopy(self.request)
        request["payload"]["entity_id"] = "other"
        with self.assertRaisesRegex(BoxPipelineError, "Unknown legal entity"):
            dispatch_box_pipeline_request(self.runtime, request)

        request = copy.deepcopy(self.request)
        request["payload"]["shipbob_request"]["default_entity_id"] = "other"
        with self.assertRaisesRegex(BoxPipelineError, "does not match"):
            dispatch_box_pipeline_request(self.runtime, request)

        runtime_without_pack = BoxRuntime(
            ROOT / "examples" / "boxes" / "us_dtc_shopify_stripe_c_corp.json",
            ROOT / "packs",
        )
        request = copy.deepcopy(self.request)
        request["payload"]["entity_id"] = "us_store"
        request["payload"]["shipbob_request"]["default_entity_id"] = "us_store"
        with self.assertRaisesRegex(BoxPipelineError, "Capability"):
            dispatch_box_pipeline_request(runtime_without_pack, request)

    def test_compiler_exposes_statutory_pipeline_template_schedule_and_preflight(self):
        compiled = compile_box_file(BOX, ROOT / "packs")
        pipeline = next(
            item for item in compiled["pipelines"]
            if item["pipeline_id"] == "commerce.shipbob_fulfillment_close"
        )
        self.assertEqual(pipeline["implementation_status"], "executable")
        self.assertEqual(pipeline["eligible_entity_ids"], ["us_dtc_company"])
        self.assertEqual(pipeline["required_connectors"], ["shipbob.fulfillment"])
        template = next(
            item for item in compiled["pipeline_request_templates"]["templates"]
            if item["pipeline_id"] == "commerce.shipbob_fulfillment_close"
        )
        self.assertEqual(template["entity_id"], "us_dtc_company")
        serialized = json.dumps(template["request"]).lower()
        self.assertNotIn("access_token", serialized)
        self.assertNotIn("secret", serialized)
        job = next(
            item for item in compiled["pipeline_schedule_template"]["jobs"]
            if item["pipeline_id"] == "commerce.shipbob_fulfillment_close"
        )
        self.assertEqual(job["entity_id"], "us_dtc_company")
        self.assertFalse(job["enabled"])
        preflight = preflight_pipeline_request(self.runtime, self.request)
        self.assertTrue(preflight["ready_to_dispatch"], preflight)


if __name__ == "__main__":
    unittest.main()
