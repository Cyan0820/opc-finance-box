from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from src.box_pipeline import dispatch_box_pipeline_request
from src.box_runtime import BoxRuntime
from src.connector_shadow_artifacts import (
    ConnectorShadowArtifactError,
    assess_connector_shadow_artifacts,
    build_connector_shadow_baseline_workpaper,
    finalize_connector_shadow_baseline_workpaper,
    review_connector_shadow_artifact,
    verify_connector_shadow_artifact,
    write_stripe_shadow_observation,
)


ROOT = Path(__file__).resolve().parents[1]
BOX = ROOT / "examples" / "boxes" / "cn_dtc_stripe_store.json"
REQUEST = ROOT / "examples" / "pipelines" / "stripe_daily_close_fixture.json"


class StripeShadowObservationTests(unittest.TestCase):
    def setUp(self):
        self.runtime = BoxRuntime(BOX, ROOT / "packs")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def _live_result(self, *, start: int = 1785542400, end: int = 1788220800) -> dict:
        request = json.loads(REQUEST.read_text(encoding="utf-8"))
        result = dispatch_box_pipeline_request(self.runtime, request)
        result["network_access_performed"] = True
        for batch in result["connector_batches"].values():
            source = batch["source"]
            source.update({
                "kind": "api",
                "network_access_performed": True,
                "rate_limit_count": 0,
                "retry_delay_seconds_total": 0.0,
                "retry_after_honored": True,
                "created_window": {
                    "gte": start,
                    "lt": end,
                    "semantics": "half_open_unix_seconds",
                    "complete_bounds_declared": True,
                },
            })
        return result

    def _baseline(self) -> Path:
        workpaper_path = Path(self.temp.name) / "stripe-workpaper.json"
        build_connector_shadow_baseline_workpaper(
            self.runtime,
            pipeline_id="stripe.daily_close",
            entity_id="cn_dtc_company",
            sample_period="2026-08",
            prepared_by="independent-stripe-source-preparer",
            output=workpaper_path,
        )
        workpaper = json.loads(workpaper_path.read_text(encoding="utf-8"))
        workpaper["source_independence"] = {
            "prepared_from_independent_source": True,
            "pipeline_output_used_as_baseline": False,
            "source_scope_confirmed": True,
        }
        workpaper["anonymization"]["private_source_evidence_retained"] = True
        source_counts = {
            "stripe.balance_transactions": 3,
            "stripe.payouts": 1,
        }
        for item in workpaper["source_expectations"]:
            item["expected_record_count"] = source_counts[item["connector_id"]]
            item["evidence_references"] = [
                f"private-export://stripe/2026-08/{item['connector_id']}"
            ]
        controls = {
            "pipeline_ready": True,
            "balance_transaction_count": 3,
            "payout_count": 1,
            "payout_bank_candidate_count": 1,
            "payout_bank_exception_count": 0,
        }
        for item in workpaper["control_expectations"]:
            item["expected_value"] = controls[item["control_id"]]
        workpaper["evidence_references"] = [
            "workpaper://stripe/2026-08/independent-source-scope",
        ]
        workpaper["finalization_ready"] = True
        workpaper_path.write_text(json.dumps(workpaper), encoding="utf-8")
        baseline = Path(self.temp.name) / "stripe-baseline.json"
        finalize_connector_shadow_baseline_workpaper(
            self.runtime, workpaper_path, baseline,
        )
        return baseline

    @staticmethod
    def _rehash(observation: dict) -> None:
        core = {
            key: value for key, value in observation.items()
            if key != "observation_fingerprint"
        }
        observation["observation_fingerprint"] = hashlib.sha256(
            json.dumps(
                core, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            ).encode()
        ).hexdigest()

    def test_safe_observation_assesses_reviews_and_excludes_private_values(self):
        baseline = self._baseline()
        result = self._live_result()
        observation_path = Path(self.temp.name) / "stripe-observation.json"
        summary = write_stripe_shadow_observation(
            self.runtime, result, observation_path,
        )
        self.assertEqual(summary["balance_transaction_count"], 3)
        self.assertEqual(summary["payout_count"], 1)
        self.assertEqual(summary["payout_bank_candidate_count"], 1)
        self.assertFalse(summary["financial_amounts_returned"])
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(observation_path.stat().st_mode), 0o600)
        serialized = observation_path.read_text(encoding="utf-8")
        for private_value in (
            "7180", "10000", "po_demo_001", "txn_demo_payout_001",
            "bank_demo_001", "Stripe po_demo_001", "bank-demo.csv",
        ):
            self.assertNotIn(private_value, serialized)
        observation = json.loads(serialized)
        self.assertFalse(observation["financial_amounts_included"])
        self.assertFalse(observation["bank_references_included"])
        self.assertFalse(observation["raw_source_ids_included"])

        assessment_path = Path(self.temp.name) / "stripe-assessment.json"
        assessed = assess_connector_shadow_artifacts(
            self.runtime, baseline, observation_path, assessment_path,
        )
        self.assertTrue(assessed["passed"])
        review_path = Path(self.temp.name) / "stripe-reviewed.json"
        reviewed = review_connector_shadow_artifact(
            self.runtime,
            assessment_path,
            review_path,
            decision="passed",
            actor="independent-stripe-shadow-reviewer",
            rationale="已独立核对 Stripe 同窗 Balance、Payout 与银行候选控制",
            evidence_references=["review://stripe/2026-08/independent-review"],
        )
        self.assertEqual(reviewed["decision"], "passed")
        self.assertTrue(reviewed["review_current"])
        self.assertTrue(verify_connector_shadow_artifact(self.runtime, review_path)["valid"])

    def test_fixture_incomplete_window_wrong_month_and_tampering_fail_closed(self):
        fixture = dispatch_box_pipeline_request(
            self.runtime, json.loads(REQUEST.read_text(encoding="utf-8")),
        )
        with self.assertRaisesRegex(ConnectorShadowArtifactError, "complete real"):
            write_stripe_shadow_observation(
                self.runtime, fixture, Path(self.temp.name) / "fixture-observation.json",
            )

        baseline = self._baseline()
        wrong_observation = Path(self.temp.name) / "wrong-window-observation.json"
        write_stripe_shadow_observation(
            self.runtime,
            self._live_result(start=1782864000, end=1785542400),
            wrong_observation,
        )
        wrong_assessment = assess_connector_shadow_artifacts(
            self.runtime,
            baseline,
            wrong_observation,
            Path(self.temp.name) / "wrong-window-assessment.json",
        )
        self.assertFalse(wrong_assessment["passed"])

        valid_observation = Path(self.temp.name) / "valid-observation.json"
        write_stripe_shadow_observation(
            self.runtime, self._live_result(), valid_observation,
        )
        tampered = json.loads(valid_observation.read_text(encoding="utf-8"))
        tampered["connector_batches"]["stripe.payouts"]["source"][
            "raw_payout_id"
        ] = "po_private_987"
        self._rehash(tampered)
        tampered_path = Path(self.temp.name) / "tampered-observation.json"
        tampered_path.write_text(json.dumps(tampered), encoding="utf-8")
        with self.assertRaisesRegex(ConnectorShadowArtifactError, "privacy contract"):
            assess_connector_shadow_artifacts(
                self.runtime,
                baseline,
                tampered_path,
                Path(self.temp.name) / "tampered-assessment.json",
            )


if __name__ == "__main__":
    unittest.main()
