from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.box_pipeline import dispatch_box_pipeline_request
from src.box_runtime import BoxRuntime
from src.connector_shadow_artifacts import (
    assess_connector_shadow_artifacts,
    review_connector_shadow_artifact,
)
from src.release_promotion import (
    AUTOMATED_PROMOTION_GATES,
    REQUIRED_REHEARSALS,
    ReleasePromotionError,
    ReleasePromotionStore,
    _hash,
    build_stable_promotion_assessment,
    build_stable_promotion_evidence_template,
)
from src.pilot_shadow_series import (
    assemble_pilot_shadow_series,
    review_pilot_shadow_series,
)
from tests import test_pilot_shadow_series as pilot_series_test_helpers


ROOT = Path(__file__).resolve().parents[1]
BOX = ROOT / "examples" / "boxes" / "sg_dtc_shopify_stripe_wise_store.json"
BASELINE = ROOT / "examples" / "shadow" / "sg_shopify_stripe_wise_connector_baseline.json"
REQUEST = ROOT / "examples" / "pipelines" / "shopify_stripe_wise_daily_close_fixture.json"


class ConnectorShadowReleasePromotionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runtime = BoxRuntime(BOX, ROOT / "packs")
        self.clock = datetime.now(timezone.utc).replace(microsecond=0)
        self.periods = ("2026-07", "2026-08")
        request_text = REQUEST.read_text(encoding="utf-8")
        self.result_paths = {}
        for period in self.periods:
            period_text = request_text
            if period == "2026-07":
                period_text = (
                    period_text.replace("2026-08", "__TARGET_PERIOD__")
                    .replace("2026-09", "2026-08")
                    .replace("__TARGET_PERIOD__", "2026-07")
                )
            result = dispatch_box_pipeline_request(
                self.runtime, json.loads(period_text),
            )
            # Represent a live read-only run without performing network access in unit tests.
            result["network_access_performed"] = True
            for batch in result["connector_batches"].values():
                batch["source"]["kind"] = "api"
                batch["source"]["network_access_performed"] = True
            result_path = self.root / f"pipeline-result-{period}.json"
            result_path.write_text(json.dumps(result), encoding="utf-8")
            self.result_paths[period] = result_path
        helper = pilot_series_test_helpers.PilotShadowSeriesTests(methodName="runTest")
        helper.setUp()
        self.series_evidence_root = self.root / "period-evidence"
        self.series_runs_root = self.root / "pilot-runs"
        for index, period in enumerate(self.periods, 1):
            helper.build_period(
                self.root,
                self.series_evidence_root,
                self.series_runs_root,
                period,
                multiplier=index,
                runtime=self.runtime,
            )
        receipt = self.root / "series-receipt.json"
        self.series_reviewed = self.root / "series-reviewed.json"
        assemble_pilot_shadow_series(
            self.runtime,
            self.series_evidence_root,
            self.series_runs_root,
            receipt,
            as_of=self.clock.date().isoformat(),
        )
        review_pilot_shadow_series(
            self.runtime,
            receipt,
            self.series_reviewed,
            decision="approved-for-promotion-evidence",
            actor="connector-continuity-reviewer",
            rationale="Independent continuity review confirms both Connector pilot periods.",
            evidence_references=["audit://connector/pilot-series/review"],
        )

    def _reviewed_connector_shadow(
        self, *, period: str = "2026-08", legacy_demo: bool = False,
        decision: str = "passed",
    ) -> Path:
        baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
        if period != "2026-08":
            baseline = json.loads(
                json.dumps(baseline).replace("2026-08", period)
            )
        baseline_path = BASELINE
        if not legacy_demo or period != "2026-08":
            if not legacy_demo:
                baseline["schema_version"] = 2
                baseline["sample_classification"] = "real_anonymized"
                baseline["source_independence"] = {
                    "prepared_from_independent_source": True,
                    "pipeline_output_used_as_baseline": False,
                    "source_scope_confirmed": True,
                }
                baseline["anonymization"] = {
                    "raw_identifiers_removed_from_baseline": True,
                    "financial_amounts_removed_from_baseline": True,
                    "private_source_evidence_retained": True,
                }
            baseline_path = self.root / f"baseline-{period}-{len(list(self.root.glob('baseline-*.json')))}.json"
            baseline_path.write_text(json.dumps(baseline), encoding="utf-8")
        assessment_path = self.root / f"connector-assessment-{len(list(self.root.glob('connector-assessment-*.json')))}.json"
        reviewed_path = self.root / f"connector-reviewed-{len(list(self.root.glob('connector-reviewed-*.json')))}.json"
        assess_connector_shadow_artifacts(
            self.runtime, baseline_path, self.result_paths[period], assessment_path,
        )
        review_connector_shadow_artifact(
            self.runtime, assessment_path, reviewed_path, decision=decision,
            actor="connector-shadow-reviewer",
            rationale="四个来源计数及跨来源控制已经完成独立复核",
            evidence_references=["review://sg-store/2026-08/connector-shadow"],
        )
        return reviewed_path

    def _evidence(self, connector_artifacts: list[str]) -> dict:
        fingerprint = self.runtime.snapshot()["fingerprint"]
        completed = self.clock.isoformat().replace("+00:00", "Z")
        pack = next(item for item in self.runtime.snapshot()["packs"] if item["id"] == "connector.wise")
        return {
            "schema_version": 1,
            "runtime_fingerprint": fingerprint,
            "pack_id": "connector.wise",
            "pack_version": pack["version"],
            "prepared_by": "promotion-preparer",
            "sample": {
                "description": "Anonymized representative four-source Singapore DTC close sample",
                "anonymized": True, "representative": True,
                "entity_ids": ["sg_store"], "periods": list(self.periods),
                "operator_principals": ["shadow-operator-1", "shadow-operator-2"],
                "evidence_references": ["audit://sample-scope"],
            },
            "thresholds": {
                "minimum_distinct_entities": 1, "minimum_distinct_periods": 2,
                "minimum_comparisons_per_report": 6, "minimum_match_rate": 0.98,
                "maximum_accepted_exceptions": 0,
                "required_domains": ["trial_balance", "statement"],
                "maximum_shadow_age_days": 30, "maximum_gate_age_days": 30,
                "maximum_rehearsal_age_days": 180,
                "approved_by": "finance-threshold-owner",
                "rationale": "Require complete close and source controls with no accepted exceptions",
            },
            "shadow_close_reports": [
                json.loads(
                    (
                        self.series_evidence_root / period / "entity-reports" /
                        "sg_store.json"
                    ).read_text(encoding="utf-8")
                )
                for period in self.periods
            ],
            "multi_entity_shadow_close_portfolios": [],
            "connector_shadow_artifacts": connector_artifacts,
            "pilot_shadow_series": {
                "reviewed_receipt_path": str(self.series_reviewed),
                "period_evidence_root": str(self.series_evidence_root),
                "pipeline_runs_root": str(self.series_runs_root),
            },
            "automated_gates": [{
                "gate": gate, "passed": True, "completed_at": completed,
                "runtime_fingerprint": fingerprint,
                "evidence_references": [f"ci://gate-{index}"],
            } for index, gate in enumerate(AUTOMATED_PROMOTION_GATES, 1)],
            "rehearsals": {
                name: {
                    "passed": True, "completed_at": completed,
                    "runtime_fingerprint": fingerprint,
                    "evidence_references": [f"audit://rehearsal-{index}"],
                }
                for index, name in enumerate(REQUIRED_REHEARSALS, 1)
            },
            "known_limitations": [], "contains_financial_results": True,
            "storage_boundary": "input_only_not_persisted",
        }

    def test_network_connector_pack_requires_complete_reviewed_shadow_scope(self):
        template = build_stable_promotion_evidence_template(self.runtime, "connector.wise")
        self.assertEqual(template["connector_shadow_artifacts"], [])
        assessment = build_stable_promotion_assessment(
            self.runtime, self._evidence([]), now=self.clock,
        )
        self.assertFalse(assessment["candidate_eligible"])
        self.assertIn(
            "connector_shadow_coverage", {item["code"] for item in assessment["blockers"]},
        )

    def test_reviewed_connector_shadow_unlocks_candidate_without_persisting_artifact(self):
        reviewed = [
            self._reviewed_connector_shadow(period=period) for period in self.periods
        ]
        assessment = build_stable_promotion_assessment(
            self.runtime, self._evidence([str(path) for path in reviewed]), now=self.clock,
        )
        self.assertTrue(assessment["candidate_eligible"])
        self.assertEqual(assessment["metrics"]["connector_shadow_count"], 2)
        self.assertFalse(assessment["raw_connector_shadow_artifacts_persisted"])
        summary = assessment["connector_shadow_summaries"][0]
        self.assertIn("connector.wise", summary["covered_pack_ids"])
        self.assertTrue(summary["real_sample_evidence"])
        self.assertEqual(summary["sample_classification"], "real_anonymized")
        serialized = json.dumps(assessment, ensure_ascii=False)
        for path in reviewed:
            self.assertNotIn(str(path), serialized)
        self.assertNotIn("source_results", serialized)
        self.assertNotIn("control_results", serialized)
        stored = ReleasePromotionStore(self.root / "promotion-store").record_assessment(
            assessment, actor="promotion-preparer",
        )
        self.assertTrue(stored["recorded"])

    def test_legacy_demo_stale_review_and_accepted_difference_fail_closed(self):
        legacy = self._reviewed_connector_shadow(legacy_demo=True)
        with self.assertRaisesRegex(ReleasePromotionError, "schema v2 real_anonymized"):
            build_stable_promotion_assessment(
                self.runtime, self._evidence([str(legacy)]), now=self.clock,
            )

        stale = self._reviewed_connector_shadow(period="2026-07")
        current = self._reviewed_connector_shadow(period="2026-08")
        payload = json.loads(stale.read_text(encoding="utf-8"))
        payload["review"]["reviewed_at"] = (
            self.clock - timedelta(days=31)
        ).isoformat().replace("+00:00", "Z")
        review_core = {key: value for key, value in payload["review"].items() if key != "review_id"}
        payload["review"]["review_id"] = _hash(review_core)[:24]
        stale.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(ReleasePromotionError, "older than"):
            build_stable_promotion_assessment(
                self.runtime, self._evidence([str(stale), str(current)]), now=self.clock,
            )

        accepted = self._reviewed_connector_shadow(
            period="2026-07", decision="accepted-differences",
        )
        passed = self._reviewed_connector_shadow(period="2026-08")
        assessment = build_stable_promotion_assessment(
            self.runtime, self._evidence([str(accepted), str(passed)]), now=self.clock,
        )
        self.assertFalse(assessment["candidate_eligible"])
        self.assertIn(
            "connector_shadow_not_passed", {item["code"] for item in assessment["blockers"]},
        )


if __name__ == "__main__":
    unittest.main()
