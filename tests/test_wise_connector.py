from __future__ import annotations

import json
import tempfile
import unittest
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from src.box_compiler import compile_box_file, preflight_pipeline_request
from src.box_pipeline import (
    run_bank_statement_close_pipeline,
    run_first_close_discovery_pipeline,
    run_month_close_control_pipeline,
)
from src.box_runtime import BoxRuntime
from src.connector_http import HttpResponse
from src.connector_sdk import ConnectorError
from src.connector_sync import ConnectorSyncStore, build_sync_plan
from src.connector_testkit import run_connector_contract_test
from src.default_connectors import build_box_connector_registry


ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / "packs" / "connectors" / "wise"
BOX = ROOT / "examples" / "boxes" / "sg_dtc_wise_store.json"


class WiseConnectorTests(unittest.TestCase):
    def setUp(self):
        self.runtime = BoxRuntime(BOX, ROOT / "packs")
        self.registry = build_box_connector_registry(self.runtime)
        self.fixture = json.loads(
            (PACK / "fixture-balance-statement.json").read_text(encoding="utf-8")
        )

    def _august_close_fixture(self):
        request = json.loads(json.dumps(self.fixture))
        request["interval_start"] = "2026-08-01T00:00:00Z"
        request["interval_end"] = "2026-09-01T00:00:00Z"
        request["statement"]["query"]["intervalStart"] = request["interval_start"]
        request["statement"]["query"]["intervalEnd"] = request["interval_end"]
        request["statement"]["startOfStatementBalance"]["value"] = 5000.0
        request["statement"]["endOfStatementBalance"]["value"] = 5579.5
        credit, debit = request["statement"]["transactions"]
        credit["date"] = "2026-08-05T05:10:00Z"
        credit["amount"]["value"] = 700.0
        credit["runningBalance"]["value"] = 5700.0
        debit["date"] = "2026-08-20T09:30:00Z"
        debit["amount"]["value"] = 120.5
        debit["totalFees"]["value"] = 0.0
        debit["runningBalance"]["value"] = 5579.5
        return request

    def test_catalog_and_contract_declare_bounded_incremental_read_only_pull(self):
        connector = self.registry.definition("wise.balance_statement")
        catalog = next(
            item for item in self.registry.catalog(self.runtime)
            if item["connector_id"] == connector.connector_id
        )
        self.assertTrue(catalog["network_access"])
        self.assertEqual(catalog["credential_env"], [
            "OPC_WISE_ACCESS_TOKEN", "OPC_WISE_ENTITY_BINDINGS_JSON",
        ])
        self.assertEqual(catalog["sync_window"], {
            "start_field": "interval_start", "end_field": "interval_end",
            "value_format": "iso8601", "max_incremental_days": 31,
            "max_backfill_days": 366,
            "incremental_overlap_seconds": 0,
        })
        contract = json.loads((PACK / "provider-contract.json").read_text(encoding="utf-8"))
        self.assertEqual(contract["api_version"], "2026Q3")
        self.assertIn("fails_closed", contract["sca_boundary"])
        self.assertEqual(contract["access_probe_contract"]["receipt_schema"], 2)
        self.assertEqual(
            contract["access_probe_contract"]["binding_fingerprint_scope"],
            "selected_entity_slice",
        )
        self.assertFalse(
            contract["access_probe_contract"]["financial_statement_requested"]
        )
        self.assertTrue(
            contract["access_probe_contract"][
                "current_receipt_required_for_shadow_dispatch"
            ]
        )
        self.assertFalse(contract["pipeline_external_actions"])

    def test_fixture_is_idempotent_masks_details_and_never_exposes_bound_ids(self):
        report = run_connector_contract_test(
            self.registry, self.runtime, "wise.balance_statement", self.fixture,
            expected_minimum_counts={"finance.bank_transactions": 2},
        )
        self.assertTrue(report["passed"], report)
        batch = self.registry.dispatch(
            self.runtime, "wise.balance_statement", self.fixture,
        )["batch"]
        credit, debit = batch["datasets"]["finance.bank_transactions"]
        self.assertEqual(credit["direction_code"], "inflow")
        self.assertEqual(debit["direction_code"], "outflow")
        self.assertEqual(debit["evidence"]["fee_amount"], 2.0)
        self.assertIn("1234****9012", credit["summary"])
        self.assertEqual(credit["account_masked"], "Wise SGD ••7654")
        serialized = json.dumps(batch, ensure_ascii=False)
        self.assertNotIn("WISE-REF-CREDIT-001", serialized)
        self.assertNotIn('"profile_id": 123456', serialized)
        self.assertNotIn('"balance_id": 987654', serialized)
        self.assertNotIn("123456789012", serialized)

    def test_binding_currency_window_and_request_injection_fail_closed(self):
        injected = json.loads(json.dumps(self.fixture))
        injected["profile_id"] = 123456
        with self.assertRaisesRegex(ConnectorError, "must not be passed"):
            self.registry.dispatch(self.runtime, "wise.balance_statement", injected)
        wrong_currency = json.loads(json.dumps(self.fixture))
        wrong_currency["currency"] = "USD"
        with self.assertRaisesRegex(ConnectorError, "functional currency"):
            self.registry.dispatch(self.runtime, "wise.balance_statement", wrong_currency)
        wrong_profile = json.loads(json.dumps(self.fixture))
        wrong_profile["profile"]["id"] = 111111
        with self.assertRaisesRegex(ConnectorError, "bound legal entity"):
            self.registry.dispatch(self.runtime, "wise.balance_statement", wrong_profile)
        outside = json.loads(json.dumps(self.fixture))
        outside["statement"]["transactions"][0]["date"] = "2026-08-01T00:00:00Z"
        batch = self.registry.dispatch(self.runtime, "wise.balance_statement", outside)["batch"]
        self.assertFalse(batch["quality"]["ready"])
        self.assertEqual(batch["quality"]["rejected_count"], 1)

    def test_personal_token_contract_rejects_an_ineligible_entity_jurisdiction(self):
        config = json.loads(
            (ROOT / "examples" / "boxes" / "cn_dtc_store.json").read_text(encoding="utf-8")
        )
        config["connectors"].append("connector.wise")
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "box.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            runtime = BoxRuntime(path, ROOT / "packs")
            registry = build_box_connector_registry(runtime)
            request = json.loads(json.dumps(self.fixture))
            request["default_entity_id"] = "cn_dtc_company"
            request["currency"] = "CNY"
            request["fixture_binding"]["access_contract"] = "personal_token_eligible"
            request["balance"]["currency"] = "CNY"
            for field in ("startOfStatementBalance", "endOfStatementBalance"):
                request["statement"][field]["currency"] = "CNY"
            request["statement"]["query"]["currency"] = "CNY"
            for transaction in request["statement"]["transactions"]:
                for field in ("amount", "totalFees", "runningBalance"):
                    transaction[field]["currency"] = "CNY"
            with self.assertRaisesRegex(ConnectorError, "not eligible"):
                registry.dispatch(runtime, "wise.balance_statement", request)

    def test_fetch_uses_three_fixed_get_endpoints_retries_and_env_bindings(self):
        definition = self.registry.definition("wise.balance_statement")
        responses = [
            HttpResponse(429, {"Retry-After": "0"}, b"private retry response"),
            HttpResponse(200, {}, json.dumps(self.fixture["profile"]).encode()),
            HttpResponse(200, {}, json.dumps(self.fixture["balance"]).encode()),
            HttpResponse(200, {}, json.dumps(self.fixture["statement"]).encode()),
        ]
        calls, sleeps = [], []

        def transport(request):
            calls.append(request)
            return responses.pop(0)

        definition.handler.__globals__["HTTP_TRANSPORT"] = transport
        definition.handler.__globals__["HTTP_SLEEPER"] = sleeps.append
        bindings = json.dumps({
            "sg_store": {
                "profile_id": 123456,
                "business_name": "OPC Wise Demo Pte Ltd",
                "access_contract": "personal_token_eligible",
                "balances": {"SGD": {
                    "balance_id": 987654,
                    "account_reference_masked": "Wise SGD ••7654",
                }},
            }
        })
        with patch.dict("os.environ", {
            "OPC_WISE_ACCESS_TOKEN": "WISE_ENV_PRIVATE",
            "OPC_WISE_ENTITY_BINDINGS_JSON": bindings,
        }, clear=False):
            result = self.registry.dispatch(self.runtime, "wise.balance_statement", {
                "mode": "fetch", "default_entity_id": "sg_store", "currency": "SGD",
                "interval_start": "2026-07-01T00:00:00Z",
                "interval_end": "2026-08-01T00:00:00Z",
            })
        self.assertTrue(result["batch"]["quality"]["ready"])
        self.assertEqual(result["batch"]["source"]["retry_count"], 1)
        self.assertEqual(result["batch"]["source"]["rate_limit_count"], 1)
        self.assertEqual(sleeps, [0.0])
        self.assertEqual(calls[0].url, "https://api.wise.com/2026Q3/profiles/123456")
        self.assertEqual(
            calls[2].url,
            "https://api.wise.com/2026Q3/profiles/123456/balances/987654",
        )
        statement_url = urllib.parse.urlsplit(calls[-1].url)
        self.assertEqual(
            f"{statement_url.scheme}://{statement_url.netloc}{statement_url.path}",
            "https://api.wise.com/2026Q3/profiles/123456/balance-statements/987654/statement.json",
        )
        self.assertEqual(dict(urllib.parse.parse_qsl(statement_url.query)), {
            "currency": "SGD", "intervalStart": "2026-07-01T00:00:00Z",
            "intervalEnd": "2026-08-01T00:00:00Z", "type": "COMPACT",
            "statementLocale": "en",
        })
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("WISE_ENV_PRIVATE", serialized)
        self.assertNotIn("Authorization", serialized)
        self.assertNotIn("WISE-REF-CREDIT-001", serialized)

    def test_sca_403_is_actionable_sanitized_and_never_retried(self):
        definition = self.registry.definition("wise.balance_statement")
        calls = []

        def transport(request):
            calls.append(request)
            return HttpResponse(403, {}, b'{"secret":"private challenge"}')

        definition.handler.__globals__["HTTP_TRANSPORT"] = transport
        bindings = json.dumps({
            "sg_store": {
                "profile_id": 123456, "business_name": "OPC Wise Demo Pte Ltd",
                "access_contract": "personal_token_eligible",
                "balances": {"SGD": {
                    "balance_id": 987654,
                    "account_reference_masked": "Wise SGD ••7654",
                }},
            }
        })
        with patch.dict("os.environ", {
            "OPC_WISE_ACCESS_TOKEN": "WISE_ENV_PRIVATE",
            "OPC_WISE_ENTITY_BINDINGS_JSON": bindings,
        }, clear=False):
            with self.assertRaisesRegex(ConnectorError, "complete required SCA") as raised:
                self.registry.dispatch(self.runtime, "wise.balance_statement", {
                    "mode": "fetch", "default_entity_id": "sg_store", "currency": "SGD",
                    "interval_start": "2026-07-01T00:00:00Z",
                    "interval_end": "2026-08-01T00:00:00Z",
                })
        self.assertEqual(len(calls), 1)
        self.assertNotIn("private challenge", str(raised.exception))
        self.assertNotIn("WISE_ENV_PRIVATE", str(raised.exception))

    def test_pipeline_compiler_preflight_and_sync_plan_use_wise_without_secrets(self):
        result = run_bank_statement_close_pipeline(self.runtime, {
            "entity_id": "sg_store", "period": "2026-07",
            "connector_id": "wise.balance_statement",
            "connector_request": self.fixture,
        }, connector_registry=self.registry)
        self.assertTrue(result["ready"], result)
        self.assertFalse(result["network_access_performed"])
        self.assertIn(
            "wise_entity_profile_binding_review",
            result["pipeline"]["required_review_gates"],
        )
        compiled = compile_box_file(BOX, ROOT / "packs")
        template = next(
            item for item in compiled["pipeline_request_templates"]["templates"]
            if item["pipeline_id"] == "finance.bank_statement_close"
        )
        payload = template["request"]["payload"]
        self.assertEqual(payload["connector_id"], "wise.balance_statement")
        self.assertNotIn("token", json.dumps(payload).lower())
        preflight = preflight_pipeline_request(self.runtime, {
            "pipeline_id": "finance.bank_statement_close",
            "payload": {
                "entity_id": "sg_store", "period": "2026-07",
                "connector_id": "wise.balance_statement",
                "connector_request": {
                    "mode": "fetch", "currency": "SGD",
                    "interval_start": "2026-07-01T00:00:00Z",
                    "interval_end": "2026-08-01T00:00:00Z",
                },
            },
        })
        self.assertTrue(preflight["ready_to_dispatch"], preflight)

        with tempfile.TemporaryDirectory() as temp_dir:
            store = ConnectorSyncStore(Path(temp_dir) / "sync")
            plan = build_sync_plan(
                self.runtime, self.registry.definition("wise.balance_statement"), store,
                entity_id="sg_store", stream_id="sgd-operating-balance",
                sync_mode="incremental", window_start="2026-07-01T00:00:00Z",
                window_end="2026-08-01T00:00:00Z",
                request_base={"currency": "SGD"},
                now=datetime(2026, 8, 14, tzinfo=timezone.utc),
            )
        self.assertEqual(plan["request"]["interval_start"], "2026-07-01T00:00:00Z")
        self.assertEqual(plan["request"]["interval_end"], "2026-08-01T00:00:00Z")
        self.assertNotIn("profile", json.dumps(plan).lower())

    def test_wise_replaces_only_bank_source_in_first_close_and_month_close(self):
        wise = self._august_close_fixture()
        first_request = json.loads(
            (ROOT / "examples" / "pipelines" / "first_close_discovery_fixture.json").read_text()
        )["payload"]
        first_request.update({
            "entity_id": "sg_store",
            "bank_connector_id": "wise.balance_statement",
            "bank_connector_request": wise,
        })
        first = run_first_close_discovery_pipeline(self.runtime, first_request)
        self.assertTrue(first["ready"], first)
        self.assertEqual(
            first["connectors"]["bank_statement"]["connector_id"],
            "wise.balance_statement",
        )
        self.assertEqual(
            first["connectors"]["general_ledger"]["connector_id"],
            "file.general_ledger",
        )
        self.assertIn(
            "wise_statement_access_review", first["pipeline"]["required_review_gates"]
        )

        bank_only = run_bank_statement_close_pipeline(self.runtime, {
            "entity_id": "sg_store", "period": "2026-08",
            "connector_id": "wise.balance_statement", "connector_request": wise,
        })
        source_fingerprint = bank_only[
            "services"
        ]["bank_reconciliation_candidate"]["output"]["accounts"][0]["source_fingerprint"]
        month_request = json.loads(
            (ROOT / "examples" / "pipelines" / "month_close_control_fixture.json").read_text()
        )["payload"]
        month_request.update({
            "entity_id": "sg_store",
            "bank_connector_id": "wise.balance_statement",
            "bank_connector_request": wise,
        })
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = Path(temp_dir) / "ledger.csv"
            trial = Path(temp_dir) / "trial.csv"
            ledger.write_text(
                (ROOT / "examples" / "accounting" / "month_close_general_ledger.csv")
                .read_text(encoding="utf-8").replace(",CNY,", ",SGD,"),
                encoding="utf-8",
            )
            trial.write_text(
                (ROOT / "examples" / "accounting" / "month_close_trial_balance.csv")
                .read_text(encoding="utf-8").replace(",CNY,", ",SGD,"),
                encoding="utf-8",
            )
            month_request["general_ledger_connector_request"]["path"] = str(ledger)
            month_request["trial_balance_connector_request"]["path"] = str(trial)
            mapping = month_request["bank_gl_mappings"][0]
            mapping.update({
                "entity_id": "sg_store", "account_masked": "Wise SGD ••7654",
                "currency": "SGD", "bank_source_fingerprint": source_fingerprint,
            })
            month = run_month_close_control_pipeline(self.runtime, month_request)
        self.assertTrue(month["ready"], month)
        self.assertEqual(
            month["connectors"]["bank_statement"]["connector_id"],
            "wise.balance_statement",
        )
        self.assertIn(
            "wise_balance_account_mapping_review",
            month["pipeline"]["required_review_gates"],
        )
        self.assertFalse(month["network_access_performed"])
        self.assertFalse(month["posting_performed"])


if __name__ == "__main__":
    unittest.main()
