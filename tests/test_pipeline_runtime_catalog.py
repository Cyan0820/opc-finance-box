import json
import tempfile
import unittest
from pathlib import Path

from src.box_compiler import (
    build_pipeline_runtime_catalog,
    compile_box,
    preflight_pipeline_request,
)
from src.box_runtime import BoxRuntime


ROOT = Path(__file__).resolve().parents[1]


class PipelineRuntimeCatalogTests(unittest.TestCase):
    def runtime(self, config: str) -> BoxRuntime:
        return BoxRuntime(ROOT / "examples" / "boxes" / config, ROOT / "packs")

    def test_dtc_catalog_contains_only_enabled_executable_pipeline_starters(self):
        catalog = build_pipeline_runtime_catalog(self.runtime("cn_dtc_api_store.json"))
        pipelines = {item["pipeline_id"]: item for item in catalog["pipelines"]}
        self.assertEqual(set(pipelines), {
            "finance.bank_statement_close", "finance.trial_balance_review",
            "finance.accounting_close_review", "finance.first_close_discovery",
            "finance.month_close_control",
            "commerce.import_analyze", "commerce.channel_close",
        })
        self.assertTrue(all(item["implementation_status"] == "executable" for item in pipelines.values()))
        self.assertFalse(catalog["request_templates"]["secret_values_included"])
        self.assertEqual(
            {item["pipeline_id"] for item in catalog["request_templates"]["templates"]},
            set(pipelines),
        )
        self.assertFalse(catalog["control_boundary"]["external_authorization_inferred"])

    def test_marketplace_and_game_catalogs_do_not_cross_expose_channel_pipelines(self):
        marketplace = build_pipeline_runtime_catalog(self.runtime("cn_marketplace_store.json"))
        game = build_pipeline_runtime_catalog(self.runtime("global_game_studio.json"))
        self.assertEqual(
            {item["pipeline_id"] for item in marketplace["pipelines"]},
            {
                "finance.bank_statement_close", "finance.trial_balance_review",
                "finance.accounting_close_review", "finance.first_close_discovery",
                "finance.month_close_control",
                "marketplace.channel_close",
            },
        )
        self.assertEqual(
            {item["pipeline_id"] for item in game["pipelines"]},
            {
                "finance.bank_statement_close", "finance.trial_balance_review",
                "finance.accounting_close_review", "finance.first_close_discovery",
                "finance.month_close_control",
                "finance.multi_entity_month_close_portfolio",
                "game.channel_settlement_close",
            },
        )

    def test_runtime_catalog_matches_compiled_distribution_contract(self):
        runtime = self.runtime("cn_dtc_api_store.json")
        catalog = build_pipeline_runtime_catalog(runtime)
        compiled = compile_box(runtime)
        self.assertEqual(catalog["runtime_fingerprint"], compiled["lock"]["runtime_fingerprint"])
        self.assertEqual(catalog["pipelines"], compiled["pipelines"])
        self.assertEqual(
            catalog["request_templates"], compiled["pipeline_request_templates"],
        )

    def test_preflight_is_read_only_and_fails_closed_on_template_placeholders(self):
        runtime = self.runtime("cn_dtc_api_store.json")
        catalog = build_pipeline_runtime_catalog(runtime)
        template = next(
            item for item in catalog["request_templates"]["templates"]
            if item["pipeline_id"] == "commerce.channel_close"
        )
        result = preflight_pipeline_request(runtime, template["request"])
        self.assertFalse(result["ready_to_dispatch"])
        self.assertTrue(result["placeholder_paths"])
        self.assertIn("request still contains fail-closed placeholders", result["blockers"])
        self.assertFalse(result["dispatch_performed"])
        self.assertFalse(result["source_access_performed"])
        self.assertFalse(result["state_changed"])
        self.assertFalse(result["external_actions_performed"])

    def test_preflight_accepts_configured_offline_fixture_without_dispatching(self):
        runtime = self.runtime("cn_dtc_api_store.json")
        request = json.loads(
            (ROOT / "examples" / "pipelines" / "commerce_channel_close_fixture.json").read_text(
                encoding="utf-8"
            )
        )
        result = preflight_pipeline_request(runtime, request)
        self.assertTrue(result["ready_to_dispatch"], result)
        self.assertEqual(result["required_review_gates"], [
            "commerce_source_mapping", "revenue_cutoff", "inventory_valuation_policy",
            "return_disposition_review", "import_landed_cost_policy", "sales_tax_nexus_review",
        ])
        self.assertFalse(result["dispatch_performed"])

    def test_preflight_rejects_secret_fields_unknown_entity_and_wrong_connector(self):
        runtime = self.runtime("cn_dtc_api_store.json")
        result = preflight_pipeline_request(runtime, {
            "pipeline_id": "commerce.channel_close",
            "payload": {
                "entity_id": "other_company",
                "connector_id": "example.marketplace_api_payload",
                "connector_request": {
                    "api_key": "must-not-be-sent",
                    "apiToken": "also-must-not-be-sent",
                    "Authorization": "Bearer must-not-be-sent",
                    "client-secret": "must-not-be-sent",
                },
            },
        })
        self.assertFalse(result["ready_to_dispatch"])
        self.assertIn("request.payload.connector_request.api_key", result["forbidden_secret_paths"])
        self.assertIn("request.payload.connector_request.apiToken", result["forbidden_secret_paths"])
        self.assertIn("request.payload.connector_request.Authorization", result["forbidden_secret_paths"])
        self.assertIn("request.payload.connector_request.client-secret", result["forbidden_secret_paths"])
        self.assertIn("payload.entity_id is not configured in the current Box", result["blockers"])
        self.assertIn("payload.connector_id is not allowed by this Pipeline", result["blockers"])

    def test_templates_and_preflight_honor_per_entity_connector_binding(self):
        payload = json.loads(
            (ROOT / "examples" / "boxes" / "global_game_studio_xero.json").read_text(
                encoding="utf-8"
            )
        )
        payload["connector_bindings"] = [
            {"connector_pack": "connector.file_import", "entity_ids": ["cn_studio", "sg_publisher"]},
            {"connector_pack": "connector.xero", "entity_ids": ["sg_publisher"]},
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "box.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            runtime = BoxRuntime(path, ROOT / "packs")
            catalog = build_pipeline_runtime_catalog(runtime)
            pipeline = next(
                item for item in catalog["pipelines"]
                if item["pipeline_id"] == "finance.trial_balance_review"
            )
            self.assertIn("xero.trial_balance", pipeline["available_connectors_by_entity"]["sg_publisher"])
            self.assertNotIn("xero.trial_balance", pipeline["available_connectors_by_entity"]["cn_studio"])
            templates = {
                item["entity_id"]: item["request"]
                for item in catalog["request_templates"]["templates"]
                if item["pipeline_id"] == "finance.trial_balance_review"
            }
            self.assertEqual(
                templates["cn_studio"]["payload"]["connector_id"], "file.trial_balance",
            )
            self.assertEqual(
                templates["sg_publisher"]["payload"]["connector_id"], "xero.trial_balance",
            )
            request = {
                "pipeline_id": "finance.trial_balance_review",
                "payload": {
                    "entity_id": "cn_studio",
                    "period": "2026-07",
                    "connector_id": "xero.trial_balance",
                    "connector_request": {},
                },
            }
            result = preflight_pipeline_request(runtime, request)
            self.assertFalse(result["ready_to_dispatch"])
            self.assertIn(
                "payload.connector_id Connector is not bound to payload.entity_id",
                result["blockers"],
            )


if __name__ == "__main__":
    unittest.main()
