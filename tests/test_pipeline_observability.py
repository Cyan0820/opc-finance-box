import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.box_runtime import BoxRuntime
from src.pipeline_observability import build_pipeline_observability, render_pipeline_prometheus
from src.pipeline_run_store import PipelineRunStore
from src.pipeline_scheduler import pipeline_request_fingerprint, schedule_job_approval_fingerprint


ROOT = Path(__file__).resolve().parents[1]


class PipelineObservabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.runtime = BoxRuntime(
            ROOT / "examples" / "boxes" / "cn_dtc_shopify_stripe_store.json",
            ROOT / "packs",
        )
        self.store = PipelineRunStore(self.root / "runs")
        self.now = datetime.now(timezone.utc).replace(second=0, microsecond=0)

    def tearDown(self):
        self.temp.cleanup()

    def _missed_schedule(self):
        request_path = self.root / "request.json"
        request_path.write_bytes(
            (ROOT / "examples" / "pipelines" / "shopify_stripe_daily_close_fixture.json").read_bytes()
        )
        request = json.loads(request_path.read_text(encoding="utf-8"))
        job = {
            "job_id": "observability-close", "enabled": True,
            "pipeline_id": "dtc.shopify_stripe_daily_close",
            "entity_id": "cn_dtc_company", "request_file": "request.json",
            "request_fingerprint": pipeline_request_fingerprint(request),
            "cadence": {
                "kind": "daily",
                "local_time": (self.now - timedelta(hours=2)).strftime("%H:%M"),
            },
            "execution_window_minutes": 10, "max_attempts": 2,
            "retry_delay_minutes": 15, "lease_seconds": 900,
            "operator": "scheduler", "alert_owner": "finance_on_call",
            "approved_by": "reviewer", "approved_at": self.now.isoformat(),
            "approval_fingerprint": None,
        }
        job["approval_fingerprint"] = schedule_job_approval_fingerprint(job)
        path = self.root / "schedule.json"
        path.write_text(json.dumps({
            "schema_version": 2, "timezone": "UTC", "jobs": [job],
        }), encoding="utf-8")
        return path

    def test_unconfigured_export_is_read_only_and_secret_free(self):
        result = build_pipeline_observability(
            self.runtime, self.store, now=self.now,
        )
        self.assertFalse(result["schedule_configured"])
        self.assertTrue(result["ledger"]["integrity_valid"])
        self.assertEqual(result["alert_counts"]["total"], 0)
        self.assertFalse(result["raw_financial_data_included"])
        self.assertFalse(result["external_actions_performed"])
        self.assertFalse(self.store.events_file.exists())

    def test_missed_window_becomes_owned_alert_and_low_cardinality_metric(self):
        result = build_pipeline_observability(
            self.runtime, self.store,
            schedule_path=self._missed_schedule(), now=self.now,
        )
        self.assertEqual(result["schedule"]["counts_by_status"], {"missed_window": 1})
        self.assertEqual(result["alerts"][0]["owner"], "finance_on_call")
        self.assertEqual(result["alerts"][0]["kind"], "missed_window")
        self.assertFalse(result["alerts"][0]["notification_sent"])
        metrics = render_pipeline_prometheus(result)
        self.assertIn('opc_finance_pipeline_schedule_jobs{status="missed_window"} 1', metrics)
        self.assertIn('opc_finance_pipeline_alerts{severity="warning"} 1', metrics)
        self.assertNotIn("finance_on_call", metrics)
        self.assertNotIn("cn_dtc_company", metrics)
        self.assertNotIn(str(self.root), metrics)


if __name__ == "__main__":
    unittest.main()
