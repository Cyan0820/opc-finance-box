import json
import copy
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from src.box_runtime import BoxRuntime
from src.pipeline_run_store import PipelineRunStore, PipelineRunStoreError
from src.pipeline_scheduler import (
    PipelineScheduleError,
    inspect_pipeline_schedule,
    load_pipeline_schedule,
    pipeline_request_fingerprint,
    run_due_pipeline_schedule,
    schedule_job_approval_fingerprint,
)


ROOT = Path(__file__).resolve().parents[1]


class PipelineSchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.plan_path = self.root / "plan.json"
        self.request_path = self.root / "request.json"
        self.request_path.write_bytes(
            (ROOT / "examples" / "pipelines" / "shopify_stripe_daily_close_fixture.json").read_bytes()
        )
        self.runtime = BoxRuntime(
            ROOT / "examples" / "boxes" / "cn_dtc_shopify_stripe_store.json",
            ROOT / "packs",
        )
        self.store = PipelineRunStore(self.root / "runs")
        self.now = datetime.now(timezone.utc).replace(second=0, microsecond=0)

    def tearDown(self):
        self.temp.cleanup()

    def _plan(self, **overrides):
        job = {
            "job_id": "cn-dtc-daily-close",
            "enabled": True,
            "pipeline_id": "dtc.shopify_stripe_daily_close",
            "entity_id": "cn_dtc_company",
            "request_file": "request.json",
            "request_fingerprint": pipeline_request_fingerprint(
                json.loads(self.request_path.read_text(encoding="utf-8"))
            ),
            "cadence": {"kind": "daily", "local_time": self.now.strftime("%H:%M")},
            "execution_window_minutes": 60,
            "max_attempts": 3,
            "retry_delay_minutes": 15,
            "lease_seconds": 900,
            "operator": "schedule_operator",
            "alert_owner": "finance_owner",
            "approved_by": "schedule_reviewer",
            "approved_at": self.now.isoformat(),
            "approval_fingerprint": None,
        }
        job.update(overrides)
        if job["enabled"] and job["approved_by"] and job["approved_at"] and job["approval_fingerprint"] is None:
            job["approval_fingerprint"] = schedule_job_approval_fingerprint(job)
        payload = {"schema_version": 2, "timezone": "UTC", "jobs": [job]}
        self.plan_path.write_text(json.dumps(payload), encoding="utf-8")
        return payload

    def test_plan_requires_explicit_approval_and_safe_local_request(self):
        self._plan(approved_by=None, approved_at=None)
        with self.assertRaisesRegex(PipelineScheduleError, "without explicit approval"):
            load_pipeline_schedule(self.plan_path)
        self._plan(request_file="../request.json")
        with self.assertRaisesRegex(PipelineScheduleError, "relative in-directory"):
            load_pipeline_schedule(self.plan_path)
        payload = self._plan(enabled=False, approved_by=None, approved_at=None)
        payload["timezone"] = "Not/A_Timezone"
        self.plan_path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(PipelineScheduleError, "IANA"):
            load_pipeline_schedule(self.plan_path)
        self._plan(approval_fingerprint="0" * 64)
        with self.assertRaisesRegex(PipelineScheduleError, "does not match"):
            load_pipeline_schedule(self.plan_path)

    def test_legacy_enabled_plan_and_changed_approved_request_are_blocked(self):
        payload = self._plan()
        payload["schema_version"] = 1
        payload["jobs"][0].pop("request_fingerprint")
        self.plan_path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(PipelineScheduleError, "migrate to version 2"):
            load_pipeline_schedule(self.plan_path)

        self._plan()
        request = json.loads(self.request_path.read_text(encoding="utf-8"))
        request["payload"]["source_batch_id"] = "changed-without-new-approval"
        self.request_path.write_text(json.dumps(request), encoding="utf-8")
        inspected = inspect_pipeline_schedule(
            self.plan_path, self.runtime, self.store, now=self.now,
        )
        self.assertEqual(inspected["jobs"][0]["status"], "blocked_configuration")
        self.assertTrue(any(
            "approved request_fingerprint" in blocker
            for blocker in inspected["jobs"][0]["blockers"]
        ))

    def test_disabled_job_is_visible_without_dispatch_or_request_access(self):
        self._plan(
            enabled=False, approved_by=None, approved_at=None,
            request_file="missing.json",
        )
        inspected = inspect_pipeline_schedule(
            self.plan_path, self.runtime, self.store, now=self.now,
        )
        self.assertEqual(inspected["jobs"][0]["status"], "disabled")
        self.assertFalse(inspected["jobs"][0]["dispatch_performed"])
        self.assertFalse(inspected["external_actions_performed"])
        self.assertFalse(self.store.events_file.exists())

    def test_due_job_is_preflighted_leased_run_and_recorded_once(self):
        self._plan()
        inspected = inspect_pipeline_schedule(
            self.plan_path, self.runtime, self.store, now=self.now,
        )
        self.assertEqual(inspected["jobs"][0]["status"], "due")
        self.assertTrue(inspected["jobs"][0]["preflight"]["ready_to_dispatch"])
        self.assertFalse(inspected["dispatch_performed"])

        executed = run_due_pipeline_schedule(
            self.plan_path, self.runtime, self.store,
            actor="schedule_operator", now=self.now,
        )
        self.assertEqual(executed["counts"], {
            "selected": 1, "dispatched": 1, "ready": 1, "blocked": 0,
        })
        outcome = executed["outcomes"][0]
        record = self.store.get(
            outcome["attempt_id"],
            runtime_fingerprint=self.runtime.snapshot()["fingerprint"],
        )
        self.assertEqual(record["trigger_kind"], "schedule")
        self.assertEqual(record["schedule_job_id"], "cn-dtc-daily-close")
        self.assertEqual(record["schedule_occurrence_id"], outcome["occurrence_id"])
        self.assertFalse(record["external_actions_performed"])
        self.assertFalse(record["secret_values_persisted"])
        repeated = run_due_pipeline_schedule(
            self.plan_path, self.runtime, self.store,
            actor="schedule_operator", now=self.now,
        )
        self.assertEqual(repeated["counts"]["dispatched"], 0)
        self.assertEqual(repeated["outcomes"][0]["status"], "completed")
        integrity = self.store.verify(
            runtime_fingerprint=self.runtime.snapshot()["fingerprint"],
        )
        self.assertEqual(integrity["event_count"], 2)
        self.assertEqual(integrity["schedule_claim_count_for_box"], 1)

    def test_operator_mismatch_never_claims_or_dispatches(self):
        self._plan()
        result = run_due_pipeline_schedule(
            self.plan_path, self.runtime, self.store,
            actor="someone_else", now=self.now,
        )
        self.assertEqual(result["outcomes"][0]["status"], "operator_mismatch")
        self.assertFalse(self.store.events_file.exists())

    def test_plan_or_request_changed_after_inspection_fails_closed(self):
        self._plan()
        first_plan = load_pipeline_schedule(self.plan_path)
        changed_plan = copy.deepcopy(first_plan)
        changed_plan["plan_fingerprint"] = "0" * 64
        with patch(
            "src.pipeline_scheduler.load_pipeline_schedule",
            side_effect=[first_plan, changed_plan],
        ):
            with self.assertRaisesRegex(PipelineScheduleError, "changed after inspection"):
                run_due_pipeline_schedule(
                    self.plan_path, self.runtime, self.store,
                    actor="schedule_operator", now=self.now,
                )
        self.assertFalse(self.store.events_file.exists())

        original_request = json.loads(self.request_path.read_text(encoding="utf-8"))
        changed_request = copy.deepcopy(original_request)
        changed_request["payload"]["source_batch_id"] = "changed-after-inspection"
        with patch(
            "src.pipeline_scheduler._load_request",
            side_effect=[original_request, changed_request],
        ):
            result = run_due_pipeline_schedule(
                self.plan_path, self.runtime, self.store,
                actor="schedule_operator", now=self.now,
            )
        self.assertEqual(result["outcomes"][0]["status"], "request_changed")
        self.assertFalse(result["outcomes"][0]["dispatched"])
        self.assertFalse(self.store.events_file.exists())

    def test_missed_window_and_future_approval_fail_closed(self):
        self._plan(
            cadence={
                "kind": "daily",
                "local_time": (self.now - timedelta(hours=2)).strftime("%H:%M"),
            },
            execution_window_minutes=10,
        )
        missed = inspect_pipeline_schedule(
            self.plan_path, self.runtime, self.store, now=self.now,
        )
        self.assertEqual(missed["jobs"][0]["status"], "missed_window")
        self._plan(approved_at=(self.now + timedelta(days=1)).isoformat())
        future = inspect_pipeline_schedule(
            self.plan_path, self.runtime, self.store, now=self.now,
        )
        self.assertEqual(future["jobs"][0]["status"], "blocked_configuration")
        self.assertIn("future", future["jobs"][0]["blockers"][0])

    def test_retry_wait_then_second_attempt_uses_same_schedule_idempotency(self):
        self._plan()
        blocked = {
            "pipeline": {
                "pipeline_id": "dtc.shopify_stripe_daily_close",
                "run_id": "transient-run",
                "executed_at": self.now.isoformat(),
                "required_review_gates": [],
            },
            "ready": False,
            "blocked_at": "connector_transport",
            "retryable": True,
            "network_access_performed": True,
            "external_actions_performed": False,
        }
        with patch("src.pipeline_scheduler.dispatch_box_pipeline_request", return_value=blocked):
            first = run_due_pipeline_schedule(
                self.plan_path, self.runtime, self.store,
                actor="schedule_operator", now=self.now,
            )
        first_record = self.store.get(
            first["outcomes"][0]["attempt_id"],
            runtime_fingerprint=self.runtime.snapshot()["fingerprint"],
        )
        waiting = inspect_pipeline_schedule(
            self.plan_path, self.runtime, self.store, now=self.now + timedelta(minutes=5),
        )
        self.assertEqual(waiting["jobs"][0]["status"], "retry_wait")
        retry_time = datetime.now(timezone.utc) + timedelta(minutes=16)
        due = inspect_pipeline_schedule(
            self.plan_path, self.runtime, self.store, now=retry_time,
        )
        self.assertEqual(due["jobs"][0]["status"], "retry_due")
        ready = dict(blocked)
        ready.update({"ready": True, "blocked_at": None, "retryable": False})
        with patch("src.pipeline_scheduler.dispatch_box_pipeline_request", return_value=ready):
            second = run_due_pipeline_schedule(
                self.plan_path, self.runtime, self.store,
                actor="schedule_operator", now=retry_time,
            )
        second_record = self.store.get(
            second["outcomes"][0]["attempt_id"],
            runtime_fingerprint=self.runtime.snapshot()["fingerprint"],
        )
        self.assertEqual(second_record["idempotency_key"], first_record["idempotency_key"])
        self.assertEqual(second_record["duplicate_of_attempt_id"], first_record["attempt_id"])
        self.assertEqual(second_record["attempt_number_for_idempotency_key"], 2)

    def test_active_lease_blocks_concurrent_claim_and_expired_lease_can_be_reclaimed(self):
        self._plan()
        inspected = inspect_pipeline_schedule(
            self.plan_path, self.runtime, self.store, now=self.now,
        )["jobs"][0]
        kwargs = {
            "runtime_fingerprint": self.runtime.snapshot()["fingerprint"],
            "job_id": inspected["job_id"],
            "occurrence_id": inspected["occurrence_id"],
            "scheduled_for": inspected["scheduled_for"],
            "actor": "schedule_operator", "max_attempts": 3,
            "retry_delay_minutes": 15, "lease_seconds": 60,
        }
        first = self.store.reserve_schedule_occurrence(**kwargs, now=self.now)
        with self.assertRaisesRegex(PipelineRunStoreError, "active execution lease"):
            self.store.reserve_schedule_occurrence(**kwargs, now=self.now)
        second = self.store.reserve_schedule_occurrence(
            **kwargs, now=self.now + timedelta(seconds=61),
        )
        self.assertNotEqual(first["claim_id"], second["claim_id"])


if __name__ == "__main__":
    unittest.main()
