from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import patch

from src.box_pipeline import BoxPipelineError, run_trial_balance_review_pipeline
from src.box_compiler import compile_box_file
from src.box_runtime import BoxRuntime
from src.connector_http import HttpResponse
from src.connector_sdk import ConnectorError
from src.connector_testkit import run_connector_contract_test
from src.default_connectors import build_box_connector_registry


ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / "packs" / "connectors" / "xero"
BOX = ROOT / "examples" / "boxes" / "global_game_studio_xero.json"


class XeroConnectorTests(unittest.TestCase):
    def setUp(self):
        self.runtime = BoxRuntime(BOX, ROOT / "packs")
        self.registry = build_box_connector_registry(self.runtime)
        self.fixture = json.loads(
            (PACK / "fixture-trial-balance.json").read_text(encoding="utf-8")
        )

    def test_catalog_declares_point_in_time_read_only_connector(self):
        connector = next(
            item for item in self.registry.catalog(self.runtime)
            if item["connector_id"] == "xero.trial_balance"
        )
        self.assertTrue(connector["network_access"])
        self.assertIsNone(connector["sync_window"])
        self.assertEqual(connector["credential_env"], [
            "OPC_XERO_ACCESS_TOKEN", "OPC_XERO_ENTITY_BINDINGS_JSON",
        ])
        contract = json.loads((PACK / "provider-contract.json").read_text(encoding="utf-8"))
        self.assertEqual(contract["oauth_scopes"], [
            "accounting.settings.read", "accounting.reports.trialbalance.read",
        ])
        self.assertEqual(contract["access_probe_contract"]["receipt_schema"], 2)
        self.assertEqual(
            contract["access_probe_contract"]["binding_fingerprint_scope"],
            "selected_entity_slice",
        )
        self.assertFalse(
            contract["access_probe_contract"]["response_values_retained"]
        )
        self.assertTrue(
            contract["access_probe_contract"][
                "current_receipt_required_for_shadow_dispatch"
            ]
        )
        self.assertIn("not_included", contract["journals_boundary"])

    def test_fixture_contract_preserves_closing_and_ytd_without_inventing_movements(self):
        report = run_connector_contract_test(
            self.registry, self.runtime, "xero.trial_balance", self.fixture,
            expected_minimum_counts={"finance.trial_balance_lines": 2},
        )
        self.assertTrue(report["passed"], report)
        batch = self.registry.dispatch(
            self.runtime, "xero.trial_balance", self.fixture,
        )["batch"]
        cash = next(
            row for row in batch["datasets"]["finance.trial_balance_lines"]
            if row["account_code"] == "1001"
        )
        self.assertEqual(cash["closing_debit"], 1000.0)
        self.assertEqual(cash["xero_ytd_debit"], 1200.0)
        self.assertEqual(cash["xero_ytd_credit"], 200.0)
        self.assertEqual(cash["opening_debit"], 0.0)
        self.assertEqual(cash["period_debit"], 0.0)
        self.assertEqual(cash["currency"], "CNY")
        self.assertEqual(cash["evidence"]["as_at"], "2026-07-31")
        serialized = json.dumps(batch, ensure_ascii=False)
        self.assertNotIn("11111111-1111-4111-8111-111111111111", serialized)
        self.assertNotIn("22222222-2222-4222-8222-222222222222", serialized)
        self.assertNotIn("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", serialized)
        self.assertNotIn("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", serialized)
        self.assertRegex(cash["evidence"]["source_object_id_sha256"], r"^[0-9a-f]{64}$")
        self.assertNotIn("source_object_id", cash["evidence"])
        for forbidden in ("posting_performed", "period_close_performed", "revenue"):
            self.assertNotIn(forbidden, cash)

    def test_entity_currency_organisation_and_account_code_are_fail_closed(self):
        wrong_entity = json.loads(json.dumps(self.fixture))
        wrong_entity["default_entity_id"] = "unknown"
        with self.assertRaisesRegex(ConnectorError, "valid default_entity_id"):
            self.registry.dispatch(self.runtime, "xero.trial_balance", wrong_entity)

        wrong_currency = json.loads(json.dumps(self.fixture))
        wrong_currency["organisation"]["Organisations"][0]["BaseCurrency"] = "USD"
        with self.assertRaisesRegex(ConnectorError, "base currency"):
            self.registry.dispatch(self.runtime, "xero.trial_balance", wrong_currency)

        wrong_organisation = json.loads(json.dumps(self.fixture))
        wrong_organisation["organisation"]["Organisations"][0]["OrganisationID"] = (
            "33333333-3333-4333-8333-333333333333"
        )
        with self.assertRaisesRegex(ConnectorError, "bound legal entity"):
            self.registry.dispatch(self.runtime, "xero.trial_balance", wrong_organisation)

        bad_account = json.loads(json.dumps(self.fixture))
        bad_account["report"]["Reports"][0]["Rows"][1]["Rows"][0]["Cells"][0]["Value"] = "Cash"
        batch = self.registry.dispatch(self.runtime, "xero.trial_balance", bad_account)["batch"]
        self.assertFalse(batch["quality"]["ready"])
        self.assertEqual(batch["quality"]["rejected_count"], 1)

    def test_fetch_uses_fixed_get_endpoints_retries_and_env_bindings_only(self):
        definition = self.registry.definition("xero.trial_balance")
        organisation = self.fixture["organisation"]
        report = self.fixture["report"]
        responses = [
            HttpResponse(429, {"Retry-After": "0"}, b"private retry response"),
            HttpResponse(200, {}, json.dumps(organisation).encode()),
            HttpResponse(200, {}, json.dumps(report).encode()),
        ]
        calls, sleeps = [], []

        def transport(request):
            calls.append(request)
            return responses.pop(0)

        definition.handler.__globals__["HTTP_TRANSPORT"] = transport
        definition.handler.__globals__["HTTP_SLEEPER"] = sleeps.append
        bindings = json.dumps({
            "cn_studio": {
                "tenant_id": "11111111-1111-4111-8111-111111111111",
                "organisation_id": "22222222-2222-4222-8222-222222222222",
            }
        })
        with patch.dict("os.environ", {
            "OPC_XERO_ACCESS_TOKEN": "XERO_ENV_PRIVATE",
            "OPC_XERO_ENTITY_BINDINGS_JSON": bindings,
        }, clear=False):
            result = self.registry.dispatch(self.runtime, "xero.trial_balance", {
                "mode": "fetch", "default_entity_id": "cn_studio",
                "default_period": "2026-07", "as_at": "2026-07-31",
                "payments_only": False,
            })
        self.assertTrue(result["batch"]["quality"]["ready"])
        self.assertEqual(result["batch"]["source"]["retry_count"], 1)
        self.assertEqual(result["batch"]["source"]["rate_limit_count"], 1)
        self.assertEqual(sleeps, [0.0])
        self.assertEqual(calls[0].method, "GET")
        self.assertEqual(calls[0].url, "https://api.xero.com/api.xro/2.0/Organisation")
        self.assertEqual(
            calls[-1].url,
            "https://api.xero.com/api.xro/2.0/Reports/TrialBalance?date=2026-07-31&paymentsOnly=false",
        )
        self.assertEqual(calls[-1].headers["xero-tenant-id"], "11111111-1111-4111-8111-111111111111")
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("XERO_ENV_PRIVATE", serialized)
        self.assertNotIn("Authorization", serialized)
        self.assertNotIn("11111111-1111-4111-8111-111111111111", serialized)

    def test_missing_or_inline_live_binding_is_sanitized(self):
        with self.assertRaisesRegex(ConnectorError, "must not be passed"):
            self.registry.dispatch(self.runtime, "xero.trial_balance", {
                "mode": "fetch", "default_entity_id": "cn_studio",
                "as_at": "2026-07-31", "tenant_id": "private",
            })
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(ConnectorError, "binding configuration is missing") as raised:
                self.registry.dispatch(self.runtime, "xero.trial_balance", {
                    "mode": "fetch", "default_entity_id": "cn_studio",
                    "as_at": "2026-07-31",
                })
        self.assertNotIn("OPC_XERO_ENTITY_BINDINGS_JSON", str(raised.exception))

    def test_trial_balance_pipeline_accepts_xero_but_close_workflows_do_not(self):
        request = json.loads(json.dumps(self.fixture))
        result = run_trial_balance_review_pipeline(self.runtime, {
            "entity_id": "cn_studio", "period": "2026-07",
            "connector_id": "xero.trial_balance", "connector_request": request,
        }, connector_registry=self.registry)
        self.assertTrue(result["ready"], result)
        self.assertFalse(result["network_access_performed"])
        self.assertFalse(result["posting_performed"])
        self.assertFalse(result["period_close_performed"])
        self.assertFalse(
            result["services"]["trial_balance_validation"]["output"]["summaries"][0]["roll_forward_checked"]
        )
        from src.box_pipeline import run_accounting_close_review_pipeline
        with self.assertRaisesRegex(BoxPipelineError, "opening and period movements"):
            run_accounting_close_review_pipeline(self.runtime, {
                "entity_id": "cn_studio", "period": "2026-07",
                "general_ledger_connector_id": "file.general_ledger",
                "trial_balance_connector_id": "xero.trial_balance",
                "account_mappings": [{}],
            })

    def test_compiler_selects_secret_free_xero_starter_and_review_gates(self):
        compiled = compile_box_file(BOX, ROOT / "packs")
        pipeline = next(
            item for item in compiled["pipelines"]
            if item["pipeline_id"] == "finance.trial_balance_review"
        )
        self.assertEqual(pipeline["available_connectors"], [
            "file.trial_balance", "xero.trial_balance",
        ])
        self.assertIn("xero_entity_binding_review", pipeline["review_gates"])
        template = next(
            item for item in compiled["pipeline_request_templates"]["templates"]
            if item["pipeline_id"] == "finance.trial_balance_review"
            and item["entity_id"] == "cn_studio"
        )
        self.assertEqual(template["request"]["payload"]["connector_id"], "xero.trial_balance")
        connector_request = template["request"]["payload"]["connector_request"]
        self.assertEqual(set(connector_request), {"mode", "as_at", "payments_only"})
        self.assertNotIn("token", json.dumps(connector_request).lower())


if __name__ == "__main__":
    unittest.main()
