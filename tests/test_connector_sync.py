from __future__ import annotations

import json
import hashlib
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from src.box_runtime import BoxRuntime
from src.connector_sdk import ConnectorError
from src.connector_sync import (
    ConnectorSyncError,
    ConnectorSyncStore,
    build_sync_plan,
    execute_sync_plan,
    validate_sync_plan,
)
from src.default_connectors import build_box_connector_registry


ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / "packs" / "connectors" / "shopify"


class ConnectorSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runtime = BoxRuntime(
            ROOT / "examples" / "boxes" / "cn_dtc_shopify_stripe_store.json",
            ROOT / "packs",
        )
        self.registry = build_box_connector_registry(self.runtime)
        self.store = ConnectorSyncStore(Path(self.temp.name) / "connector-sync")
        self.clock = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)

    def _plan(self, **overrides):
        values = {
            "entity_id": "cn_dtc_company",
            "stream_id": "primary-orders",
            "sync_mode": "incremental",
            "window_start": "2026-08-01T00:00:00Z",
            "window_end": "2026-08-02T00:00:00Z",
            "request_base": {"shop_domain": "opc-demo.myshopify.com"},
            "now": self.clock,
        }
        values.update(overrides)
        return build_sync_plan(
            self.runtime,
            self.registry.definition("shopify.orders"),
            self.store,
            **values,
        )

    @staticmethod
    def _successful_result(plan: dict) -> dict:
        return {
            "connector": {"connector_id": plan["connector_id"]},
            "batch": {
                "batch_id": "batch-success-001",
                "source": {
                    "kind": "api", "name": "shopify.orders",
                    "network_access_performed": True, "page_count": 2,
                    "retry_count": 1, "rate_limit_count": 1,
                    "retry_delay_seconds_total": 2.0, "retry_after_honored": True,
                },
                "quality": {
                    "ready": True, "record_count": 1,
                    "dataset_counts": {"commerce.shopify_orders": 1},
                    "rejected_count": 0, "duplicate_business_keys": [],
                },
            },
        }

    def test_plan_is_checkpoint_bound_secret_free_and_strict(self):
        plan = self._plan()
        self.assertEqual(plan["request"]["mode"], "fetch")
        self.assertEqual(plan["request"]["created_at_gte"], "2026-08-01T00:00:00Z")
        self.assertEqual(plan["schema_version"], 2)
        self.assertEqual(plan["capture_policy"]["source_window_strategy"], "initial_window")
        self.assertFalse(plan["capture_policy"]["complete_update_capture_claimed"])
        self.assertEqual(plan["expected_checkpoint_event_hash"], "GENESIS")
        self.assertTrue(plan["checkpoint_promotion_allowed"])
        self.assertFalse(plan["secret_values_included"])
        tampered = json.loads(json.dumps(plan))
        tampered["request"]["access_token"] = "private"
        with self.assertRaisesRegex(ConnectorSyncError, "credential-like"):
            validate_sync_plan(tampered)
        with self.assertRaisesRegex(ConnectorSyncError, "sync-controlled fields"):
            self._plan(request_base={"created_at_gte": "2026-01-01T00:00:00Z"})
        tampered_capture = json.loads(json.dumps(plan))
        tampered_capture["capture_policy"]["complete_update_capture_claimed"] = True
        with self.assertRaisesRegex(ConnectorSyncError, "capture policy"):
            validate_sync_plan(tampered_capture)

    def test_success_needs_manual_commit_and_next_plan_starts_at_checkpoint(self):
        plan = self._plan()
        attempt = self.store.record_success(
            plan, self._successful_result(plan), actor="同步执行人",
        )
        self.assertTrue(attempt["checkpoint_candidate"])
        self.assertIsNone(self.store.checkpoint(plan["stream_key"]))
        checkpoint = self.store.commit_checkpoint(
            attempt["attempt_id"],
            runtime_fingerprint=self.runtime.snapshot()["fingerprint"],
            actor="同步复核人",
            rationale="来源计数和质量门均已复核",
            evidence_references=["shadow://shopify/2026-08-02"],
        )
        self.assertEqual(checkpoint["window_end"], "2026-08-02T00:00:00Z")
        next_plan = self._plan(
            window_start=None, window_end="2026-08-03T00:00:00Z",
        )
        self.assertEqual(next_plan["window"]["start"], "2026-08-02T00:00:00Z")
        self.assertEqual(next_plan["expected_checkpoint_event_hash"], checkpoint["event_hash"])
        status = self.store.status(runtime_fingerprint=self.runtime.snapshot()["fingerprint"])
        self.assertEqual(status["counts"]["checkpoints"], 1)
        self.assertEqual(status["counts"]["checkpoint_candidates"], 0)

    def test_schema_v1_plan_remains_runnable_and_is_recorded_with_safe_legacy_capture(self):
        plan = self._plan()
        legacy = json.loads(json.dumps(plan))
        legacy["schema_version"] = 1
        legacy.pop("capture_policy")
        legacy.pop("plan_id")
        legacy["plan_id"] = hashlib.sha256(json.dumps(
            legacy, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()[:24]
        validate_sync_plan(legacy)
        attempt = self.store.record_success(
            legacy, self._successful_result(legacy), actor="旧计划执行人",
        )
        self.assertEqual(
            attempt["capture_policy"]["source_window_strategy"],
            "legacy_contiguous_window",
        )
        self.assertFalse(attempt["capture_policy"]["complete_update_capture_claimed"])

    def test_backfill_never_advances_checkpoint(self):
        plan = self._plan(sync_mode="backfill", stream_id="historical-orders")
        attempt = self.store.record_success(plan, self._successful_result(plan), actor="回溯执行人")
        self.assertFalse(attempt["checkpoint_candidate"])
        with self.assertRaisesRegex(ConnectorSyncError, "not eligible"):
            self.store.commit_checkpoint(
                attempt["attempt_id"],
                runtime_fingerprint=self.runtime.snapshot()["fingerprint"],
                actor="回溯复核人", rationale="不应提交", evidence_references=["shadow://backfill"],
            )

    def test_quality_failure_is_quarantined_and_can_be_resolved_with_replacement(self):
        plan = self._plan()
        failed_result = self._successful_result(plan)
        failed_result["batch"]["quality"].update({"ready": False, "rejected_count": 1})
        failed = self.store.record_success(plan, failed_result, actor="同步执行人")
        self.assertTrue(failed["quarantined"])
        self.assertFalse(failed["checkpoint_candidate"])
        replacement = self.store.record_success(plan, self._successful_result(plan), actor="同步执行人")
        resolution = self.store.resolve_quarantine(
            failed["attempt_id"],
            runtime_fingerprint=self.runtime.snapshot()["fingerprint"],
            actor="异常复核人",
            resolution="replaced",
            rationale="修复映射后完整窗口重新执行",
            replacement_attempt_id=replacement["attempt_id"],
        )
        self.assertTrue(resolution["resolved"])
        status = self.store.status(runtime_fingerprint=self.runtime.snapshot()["fingerprint"])
        self.assertEqual(status["counts"]["quarantine"], 0)
        self.assertEqual(status["counts"]["checkpoint_candidates"], 1)

    def test_dispatch_failure_is_sanitized_recorded_and_ledger_tamper_fails(self):
        plan = self._plan()
        definition = self.registry.definition("shopify.orders")
        original = definition.handler.__globals__["HTTP_TRANSPORT"]
        definition.handler.__globals__["HTTP_TRANSPORT"] = lambda request: (_ for _ in ()).throw(
            ConnectorError("Authorization Bearer shpat_private https://private.example")
        )
        try:
            with self.assertRaisesRegex(ConnectorSyncError, "quarantined as attempt") as raised:
                execute_sync_plan(
                    self.runtime, self.registry, self.store, plan, actor="同步执行人",
                )
        finally:
            definition.handler.__globals__["HTTP_TRANSPORT"] = original
        self.assertNotIn("shpat_private", str(raised.exception))
        status = self.store.status(runtime_fingerprint=self.runtime.snapshot()["fingerprint"])
        failure = status["quarantine"][0]
        self.assertNotIn("shpat_private", json.dumps(failure))
        serialized = self.store.events_file.read_text(encoding="utf-8")
        self.assertNotIn("shop_domain", serialized)
        self.assertNotIn("shpat_private", serialized)
        self.assertTrue(self.store.verify()["valid"])
        self.store.events_file.write_text(serialized.replace('"sequence":1', '"sequence":2'), encoding="utf-8")
        with self.assertRaisesRegex(ConnectorSyncError, "sequence or chain"):
            self.store.verify()

    def test_stale_plan_cannot_record_or_commit_after_checkpoint_changes(self):
        first = self._plan()
        stale = self._plan()
        attempt = self.store.record_success(first, self._successful_result(first), actor="同步执行人")
        self.store.commit_checkpoint(
            attempt["attempt_id"],
            runtime_fingerprint=self.runtime.snapshot()["fingerprint"],
            actor="同步复核人", rationale="完成", evidence_references=["shadow://done"],
        )
        with self.assertRaisesRegex(ConnectorSyncError, "stale"):
            self.store.record_success(stale, self._successful_result(stale), actor="同步执行人")
