from __future__ import annotations

import json
import hashlib
import os
import unittest
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from src.box_runtime import BoxRuntime
from src.box_pipeline import dispatch_box_pipeline_request, run_expense_evidence_review_pipeline
from src.connector_http import HttpResponse
from src.connector_sdk import ConnectorError
from src.connector_sync import ConnectorSyncStore, build_sync_plan
from src.default_connectors import build_box_connector_registry


ROOT = Path(__file__).resolve().parents[1]
BOX = ROOT / "examples" / "boxes" / "sg_dtc_shopify_stripe_wise_airwallex_store.json"
FIXTURE = ROOT / "packs" / "connectors" / "airwallex" / "fixture-approved-expenses.json"


class AirwallexConnectorTests(unittest.TestCase):
    def setUp(self):
        self.runtime = BoxRuntime(BOX, ROOT / "packs")
        self.registry = build_box_connector_registry(self.runtime)
        self.request = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def _webhook_context(
        self,
        expense_id: str,
        *,
        event_name: str = "spend.expense.updated",
        receipt_id: str = "a" * 24,
    ):
        return {
            "receipt_id": receipt_id,
            "event_name": event_name,
            "event_created_at": "2026-08-14T08:00:00Z",
            "expense_id_sha256": hashlib.sha256(expense_id.encode()).hexdigest(),
            "body_sha256": "b" * 64,
            "runtime_fingerprint": self.runtime.snapshot()["fingerprint"],
        }

    def test_fixture_maps_approved_expenses_without_private_fields(self):
        batch = self.registry.dispatch(
            self.runtime, "airwallex.approved_expenses", self.request,
        )["batch"]
        self.assertEqual(batch["quality"]["record_count"], 2)
        rows = batch["datasets"]["finance.expense_evidence"]
        self.assertEqual(rows[0]["billing_amount_minor"], 12840)
        self.assertEqual(rows[0]["transaction_amount_minor"], 9800)
        self.assertTrue(rows[0]["ready_for_accounting_review"])
        self.assertRegex(rows[0]["evidence"]["source_record_version_sha256"], r"^[0-9a-f]{64}$")
        serialized = json.dumps(batch, ensure_ascii=False)
        for forbidden in (
            "exp_demo_001", "card_private", "finance@example", "files.example",
            "private-dept", "approved\"",
        ):
            self.assertNotIn(forbidden, serialized)
        self.assertFalse(batch["source"]["network_access_performed"])

    def test_live_authenticates_then_pages_with_exact_entity_binding(self):
        live = {
            key: value for key, value in self.request.items()
            if key not in {"fixture_binding", "objects"}
        }
        live["mode"] = "fetch"
        source_objects = self.request["objects"]
        calls = []

        def transport(request):
            calls.append(request)
            if request.method == "POST":
                return HttpResponse(201, {}, json.dumps({"token": "short-lived"}).encode())
            page = urllib_page(request.url)
            payload = (
                {"items": [source_objects[0]], "page_after": "next-bookmark"}
                if page is None
                else {"items": [source_objects[1]], "page_after": None}
            )
            return HttpResponse(200, {}, json.dumps(payload).encode())

        handler_globals = self.registry.definition(
            "airwallex.approved_expenses"
        ).handler.__globals__
        bindings = json.dumps({"sg_store": self.request["fixture_binding"]})
        with patch.dict(os.environ, {
            "OPC_AIRWALLEX_CLIENT_ID": "client",
            "OPC_AIRWALLEX_API_KEY": "api-key",
            "OPC_AIRWALLEX_ENTITY_BINDINGS_JSON": bindings,
        }, clear=False), patch.dict(handler_globals, {"HTTP_TRANSPORT": transport}):
            batch = self.registry.dispatch(
                self.runtime, "airwallex.approved_expenses", live,
            )["batch"]
        self.assertEqual(batch["quality"]["record_count"], 2)
        self.assertTrue(batch["source"]["network_access_performed"])
        self.assertEqual(batch["source"]["page_count"], 2)
        self.assertEqual(calls[0].method, "POST")
        self.assertEqual(calls[0].headers["x-login-as"], "acct_sg_demo")
        self.assertTrue(all(call.headers["x-api-version"] == "2026-07-17" for call in calls))
        self.assertIn("legal_entity_id=le_sg_demo", calls[1].url)
        self.assertIn("status=APPROVED", calls[1].url)
        self.assertNotIn("/sync", " ".join(call.url for call in calls))

    def test_webhook_refetch_reads_exact_expenses_and_emits_state_change_candidates(self):
        approved = json.loads(json.dumps(self.request["objects"][0]))
        rejected = json.loads(json.dumps(self.request["objects"][1]))
        rejected["status"] = "MANUAL_REVIEW_REQUIRED"
        calls = []

        def transport(request):
            calls.append(request)
            if request.method == "POST":
                return HttpResponse(201, {}, json.dumps({"token": "short-lived"}).encode())
            payload = approved if request.url.endswith("/exp_demo_001") else rejected
            return HttpResponse(200, {}, json.dumps(payload).encode())

        handler_globals = self.registry.definition(
            "airwallex.approved_expenses"
        ).handler.__globals__
        bindings = json.dumps({"sg_store": self.request["fixture_binding"]})
        request = {
            "mode": "refetch",
            "default_entity_id": "sg_store",
            "expense_ids": ["exp_demo_001", "exp_demo_002"],
            "webhook_contexts": [
                self._webhook_context("exp_demo_001", receipt_id="a" * 24),
                self._webhook_context("exp_demo_002", receipt_id="c" * 24),
            ],
            "currency_minor_units": {"SGD": 2, "USD": 2},
        }
        with patch.dict(os.environ, {
            "OPC_AIRWALLEX_CLIENT_ID": "client",
            "OPC_AIRWALLEX_API_KEY": "api-key",
            "OPC_AIRWALLEX_ENTITY_BINDINGS_JSON": bindings,
        }, clear=False), patch.dict(handler_globals, {"HTTP_TRANSPORT": transport}):
            result = self.registry.dispatch(
                self.runtime, "airwallex.approved_expenses", request,
            )
        batch = result["batch"]
        self.assertTrue(batch["quality"]["ready"])
        self.assertEqual(batch["quality"]["dataset_counts"], {
            "finance.expense_evidence": 1,
            "finance.expense_evidence_state_changes": 1,
        })
        change = batch["datasets"]["finance.expense_evidence_state_changes"][0]
        self.assertEqual(change["current_status"], "MANUAL_REVIEW_REQUIRED")
        self.assertTrue(change["invalidates_approved_evidence"])
        self.assertTrue(batch["source"]["network_access_performed"])
        self.assertTrue(batch["source"]["webhook_context_validated"])
        self.assertEqual(batch["source"]["webhook_context_count"], 2)
        self.assertEqual(batch["source"]["provider_absence_count"], 0)
        self.assertEqual(calls[1].method, "GET")
        self.assertTrue(calls[1].url.endswith("/api/v1/spend/expenses/exp_demo_001"))
        self.assertTrue(calls[2].url.endswith("/api/v1/spend/expenses/exp_demo_002"))
        self.assertNotIn("/sync", " ".join(call.url for call in calls))
        serialized = json.dumps(batch)
        self.assertNotIn("exp_demo_001", serialized)
        self.assertNotIn("exp_demo_002", serialized)

    def test_refetch_pipeline_blocks_non_approved_state_for_human_review(self):
        changed = json.loads(json.dumps(self.request["objects"][0]))
        changed["status"] = "ARCHIVED"

        def transport(request):
            if request.method == "POST":
                return HttpResponse(201, {}, json.dumps({"token": "short-lived"}).encode())
            return HttpResponse(200, {}, json.dumps(changed).encode())

        handler_globals = self.registry.definition(
            "airwallex.approved_expenses"
        ).handler.__globals__
        bindings = json.dumps({"sg_store": self.request["fixture_binding"]})
        with patch.dict(os.environ, {
            "OPC_AIRWALLEX_CLIENT_ID": "client",
            "OPC_AIRWALLEX_API_KEY": "api-key",
            "OPC_AIRWALLEX_ENTITY_BINDINGS_JSON": bindings,
        }, clear=False), patch.dict(handler_globals, {"HTTP_TRANSPORT": transport}):
            result = run_expense_evidence_review_pipeline(self.runtime, {
                "entity_id": "sg_store",
                "connector_request": {
                    "mode": "refetch", "default_entity_id": "sg_store",
                    "expense_ids": ["exp_demo_001"],
                    "webhook_contexts": [self._webhook_context("exp_demo_001")],
                    "currency_minor_units": {"SGD": 2, "USD": 2},
                },
            }, connector_registry=self.registry)
        self.assertFalse(result["ready"])
        self.assertEqual(result["founder_briefing"]["state_change_count"], 1)
        self.assertEqual(result["founder_briefing"]["record_count"], 0)
        self.assertIn("non-approved state", result["blockers"][0])
        self.assertFalse(result["expense_claims_created"])
        self.assertFalse(result["posting_performed"])
        self.assertFalse(result["payment_performed"])

    def test_deleted_webhook_plus_get_404_emits_review_only_tombstone(self):
        def transport(request):
            if request.method == "POST":
                return HttpResponse(201, {}, json.dumps({"token": "short-lived"}).encode())
            return HttpResponse(404, {}, b'{"code":"not_found"}')

        handler_globals = self.registry.definition(
            "airwallex.approved_expenses"
        ).handler.__globals__
        bindings = json.dumps({"sg_store": self.request["fixture_binding"]})
        request = {
            "mode": "refetch",
            "default_entity_id": "sg_store",
            "expense_ids": ["exp_demo_001"],
            "webhook_contexts": [self._webhook_context(
                "exp_demo_001", event_name="spend.expense.deleted",
            )],
            "currency_minor_units": {"SGD": 2, "USD": 2},
        }
        with patch.dict(os.environ, {
            "OPC_AIRWALLEX_CLIENT_ID": "client",
            "OPC_AIRWALLEX_API_KEY": "api-key",
            "OPC_AIRWALLEX_ENTITY_BINDINGS_JSON": bindings,
        }, clear=False), patch.dict(handler_globals, {"HTTP_TRANSPORT": transport}):
            result = run_expense_evidence_review_pipeline(
                self.runtime,
                {"entity_id": "sg_store", "connector_request": request},
                connector_registry=self.registry,
            )
        self.assertFalse(result["ready"])
        self.assertEqual(result["founder_briefing"]["record_count"], 0)
        self.assertEqual(result["founder_briefing"]["state_change_count"], 1)
        change = result["batch"]["datasets"][
            "finance.expense_evidence_state_changes"
        ][0]
        self.assertEqual(change["current_status"], "DELETED")
        self.assertTrue(change["provider_absence_confirmed"])
        self.assertEqual(change["deletion_signal"], "signed_webhook_and_get_404")
        self.assertTrue(change["candidate_only"])
        self.assertEqual(result["batch"]["source"]["provider_absence_count"], 1)
        self.assertFalse(result["external_actions_performed"])
        self.assertNotIn("exp_demo_001", json.dumps(result))

    def test_refetch_context_and_non_deleted_404_fail_closed(self):
        handler_globals = self.registry.definition(
            "airwallex.approved_expenses"
        ).handler.__globals__
        bindings = json.dumps({"sg_store": self.request["fixture_binding"]})
        base = {
            "mode": "refetch",
            "default_entity_id": "sg_store",
            "expense_ids": ["exp_demo_001"],
            "currency_minor_units": {"SGD": 2, "USD": 2},
        }
        with patch.dict(os.environ, {
            "OPC_AIRWALLEX_CLIENT_ID": "client",
            "OPC_AIRWALLEX_API_KEY": "api-key",
            "OPC_AIRWALLEX_ENTITY_BINDINGS_JSON": bindings,
        }, clear=False):
            with self.assertRaisesRegex(ConnectorError, "signed webhook context"):
                self.registry.dispatch(
                    self.runtime, "airwallex.approved_expenses", base,
                )

            def transport(request):
                if request.method == "POST":
                    return HttpResponse(
                        201, {}, json.dumps({"token": "short-lived"}).encode(),
                    )
                return HttpResponse(404, {}, b'{"code":"not_found"}')

            request = {
                **base,
                "webhook_contexts": [self._webhook_context("exp_demo_001")],
            }
            with patch.dict(handler_globals, {"HTTP_TRANSPORT": transport}):
                with self.assertRaisesRegex(ConnectorError, "without a signed deleted event"):
                    self.registry.dispatch(
                        self.runtime, "airwallex.approved_expenses", request,
                    )

    def test_scope_rounding_and_inline_binding_fail_closed(self):
        wrong = json.loads(json.dumps(self.request))
        wrong["objects"][0]["legal_entity_id"] = "le_other"
        batch = self.registry.dispatch(
            self.runtime, "airwallex.approved_expenses", wrong,
        )["batch"]
        self.assertEqual(batch["quality"]["rejected_count"], 1)
        rounding = json.loads(json.dumps(self.request))
        rounding["objects"][0]["billing_amount"] = "1.001"
        batch = self.registry.dispatch(
            self.runtime, "airwallex.approved_expenses", rounding,
        )["batch"]
        self.assertEqual(batch["quality"]["rejected_count"], 1)
        malformed = json.loads(json.dumps(self.request))
        malformed["objects"][0]["attachments"] = {"url": "private"}
        batch = self.registry.dispatch(
            self.runtime, "airwallex.approved_expenses", malformed,
        )["batch"]
        self.assertEqual(batch["quality"]["rejected_count"], 1)
        inline = json.loads(json.dumps(self.request))
        inline["api_key"] = "forbidden"
        with self.assertRaisesRegex(ConnectorError, "inline"):
            self.registry.dispatch(
                self.runtime, "airwallex.approved_expenses", inline,
            )

    def test_incremental_plan_is_checkpoint_controlled_and_secret_free(self):
        with tempfile.TemporaryDirectory() as temp:
            plan = build_sync_plan(
                self.runtime,
                self.registry.definition("airwallex.approved_expenses"),
                ConnectorSyncStore(Path(temp) / "sync"),
                entity_id="sg_store", stream_id="primary-approved-expenses",
                sync_mode="incremental",
                window_start="2026-08-01T00:00:00Z",
                window_end="2026-08-14T00:00:00Z",
                request_base={"currency_minor_units": {"SGD": 2, "USD": 2}, "max_pages": 50},
                now=datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc),
            )
        self.assertEqual(plan["request"]["mode"], "fetch")
        self.assertEqual(plan["request"]["from_created_at"], "2026-08-01T00:00:00Z")
        self.assertEqual(plan["request"]["to_created_at"], "2026-08-14T00:00:00Z")
        self.assertEqual(plan["expected_checkpoint_event_hash"], "GENESIS")
        self.assertEqual(plan["capture_policy"]["configured_overlap_seconds"], 7 * 86400)
        self.assertEqual(plan["capture_policy"]["applied_overlap_seconds"], 0)
        self.assertFalse(plan["capture_policy"]["complete_update_capture_claimed"])
        self.assertFalse(plan["secret_values_included"])

    def test_next_incremental_plan_refetches_seven_days_without_rewinding_checkpoint(self):
        with tempfile.TemporaryDirectory() as temp:
            store = ConnectorSyncStore(Path(temp) / "sync")
            connector = self.registry.definition("airwallex.approved_expenses")
            first = build_sync_plan(
                self.runtime, connector, store,
                entity_id="sg_store", stream_id="primary-approved-expenses",
                sync_mode="incremental", window_start="2026-08-01T00:00:00Z",
                window_end="2026-08-14T00:00:00Z",
                request_base={"currency_minor_units": {"SGD": 2, "USD": 2}},
                now=datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc),
            )
            result = self.registry.dispatch(
                self.runtime, "airwallex.approved_expenses", self.request,
            )
            result["batch"]["source"]["network_access_performed"] = True
            attempt = store.record_success(first, result, actor="Airwallex 同步执行人")
            checkpoint = store.commit_checkpoint(
                attempt["attempt_id"], runtime_fingerprint=self.runtime.snapshot()["fingerprint"],
                actor="Airwallex 同步复核人", rationale="完整窗口及来源计数已复核",
                evidence_references=["shadow://airwallex/sg_store/2026-08-14"],
            )
            second = build_sync_plan(
                self.runtime, connector, store,
                entity_id="sg_store", stream_id="primary-approved-expenses",
                sync_mode="incremental", window_end="2026-08-20T00:00:00Z",
                request_base={"currency_minor_units": {"SGD": 2, "USD": 2}},
                now=datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc),
            )
        self.assertEqual(second["window"]["start"], checkpoint["window_end"])
        self.assertEqual(second["request"]["from_created_at"], "2026-08-07T00:00:00Z")
        self.assertEqual(second["capture_policy"]["source_window_strategy"], "bounded_overlap_refetch")
        self.assertEqual(second["capture_policy"]["applied_overlap_seconds"], 7 * 86400)
        self.assertFalse(second["capture_policy"]["complete_update_capture_claimed"])

    def test_expense_pipeline_exposes_review_gaps_without_creating_claim_or_posting(self):
        result = dispatch_box_pipeline_request(self.runtime, {
            "pipeline_id": "finance.expense_evidence_review",
            "payload": {"entity_id": "sg_store", "connector_request": self.request},
        })
        self.assertTrue(result["ready"])
        self.assertEqual(result["founder_briefing"]["record_count"], 2)
        self.assertEqual(result["founder_briefing"]["receipt_missing_count"], 1)
        self.assertEqual(result["founder_briefing"]["accounting_mapping_missing_count"], 1)
        self.assertFalse(result["expense_claims_created"])
        self.assertFalse(result["posting_performed"])
        self.assertFalse(result["payment_performed"])
        self.assertFalse(result["external_actions_performed"])


def urllib_page(url: str) -> str | None:
    from urllib.parse import parse_qs, urlsplit

    return parse_qs(urlsplit(url).query).get("page", [None])[0]


if __name__ == "__main__":
    unittest.main()
