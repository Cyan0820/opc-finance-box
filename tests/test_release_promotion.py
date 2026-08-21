from __future__ import annotations

import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from src.box_runtime import BoxRuntime
from src.release_promotion import (
    AUTOMATED_PROMOTION_GATES,
    REQUIRED_REHEARSALS,
    ReleasePromotionError,
    ReleasePromotionStore,
    _hash,
    _report_fingerprint,
    build_stable_promotion_evidence_template,
    build_stable_promotion_assessment,
)
from src.pilot_shadow_series import (
    assemble_pilot_shadow_series,
    review_pilot_shadow_series,
)
from src.production_readiness import _stable_promotion_stage
from tests import test_pilot_shadow_series as pilot_series_test_helpers


ROOT = Path(__file__).resolve().parents[1]


class ReleasePromotionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runtime = BoxRuntime(
            ROOT / "examples" / "boxes" / "cn_dtc_shopify_stripe_store.json",
            ROOT / "packs",
        )
        self.store = ReleasePromotionStore(Path(self.temp.name) / "release-promotion")
        self.clock = datetime.now(timezone.utc).replace(microsecond=0)
        self._evidence_cache: dict[str, dict] = {}

    def _evidence(self, *, classification: str | None = None) -> dict:
        cache_key = classification or "clean"
        if cache_key in self._evidence_cache:
            return copy.deepcopy(self._evidence_cache[cache_key])
        series_root = Path(self.temp.name) / f"pilot-series-{cache_key}"
        series_root.mkdir()
        evidence_root = series_root / "period-evidence"
        runs_root = series_root / "runs"
        helper = pilot_series_test_helpers.PilotShadowSeriesTests(methodName="runTest")
        helper.setUp()
        helper.build_period(
            series_root, evidence_root, runs_root, "2026-07", multiplier=1,
        )
        helper.build_period(
            series_root,
            evidence_root,
            runs_root,
            "2026-08",
            multiplier=2,
            classification=classification,
        )
        receipt = series_root / "series-receipt.json"
        reviewed = series_root / "series-reviewed.json"
        assemble_pilot_shadow_series(
            self.runtime, evidence_root, runs_root, receipt,
            as_of=self.clock.date().isoformat(),
        )
        decision = (
            "needs-correction" if classification == "system_defect"
            else "approved-for-promotion-evidence"
        )
        review_pilot_shadow_series(
            self.runtime,
            receipt,
            reviewed,
            decision=decision,
            actor=f"continuity-reviewer-{cache_key}",
            rationale="Independent continuity review confirms the exact two-period evidence.",
            evidence_references=[f"audit://release/series/{cache_key}"],
        )
        reports = [
            json.loads(
                (evidence_root / period / "entity-reports" / "cn_dtc_company.json")
                .read_text(encoding="utf-8")
            )
            for period in ("2026-07", "2026-08")
        ]
        fingerprint = self.runtime.snapshot()["fingerprint"]
        completed = self.clock.isoformat().replace("+00:00", "Z")
        evidence = {
            "schema_version": 1,
            "runtime_fingerprint": fingerprint,
            "pack_id": "core.finance",
            "pack_version": "0.8.0",
            "prepared_by": "promotion-preparer",
            "sample": {
                "description": "Anonymized representative DTC monthly close sample",
                "anonymized": True,
                "representative": True,
                "entity_ids": ["cn_dtc_company"],
                "periods": ["2026-07", "2026-08"],
                "operator_principals": ["shadow-operator-1", "shadow-operator-2"],
                "evidence_references": ["audit://sample-scope"],
            },
            "thresholds": {
                "minimum_distinct_entities": 1,
                "minimum_distinct_periods": 2,
                "minimum_comparisons_per_report": 6,
                "minimum_match_rate": 0.98,
                "maximum_accepted_exceptions": 0,
                "required_domains": ["trial_balance", "statement"],
                "maximum_shadow_age_days": 30,
                "maximum_gate_age_days": 30,
                "maximum_rehearsal_age_days": 180,
                "approved_by": "finance-threshold-owner",
                "rationale": "Thresholds require complete ledgers and statements with no accepted exceptions",
            },
            "shadow_close_reports": reports,
            "multi_entity_shadow_close_portfolios": [],
            "connector_shadow_artifacts": [],
            "pilot_shadow_series": {
                "reviewed_receipt_path": str(reviewed),
                "period_evidence_root": str(evidence_root),
                "pipeline_runs_root": str(runs_root),
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
            "known_limitations": [],
            "contains_financial_results": True,
            "storage_boundary": "input_only_not_persisted",
        }
        self._evidence_cache[cache_key] = evidence
        return copy.deepcopy(evidence)

    def test_complete_evidence_builds_candidate_without_changing_pack(self):
        evidence = self._evidence()
        assessment = build_stable_promotion_assessment(
            self.runtime, evidence, now=self.clock,
        )
        self.assertTrue(assessment["candidate_eligible"])
        self.assertEqual(assessment["target_status"], "stable_candidate")
        self.assertEqual(assessment["metrics"]["comparison_count"], 14)
        self.assertEqual(assessment["metrics"]["match_rate"], 1)
        self.assertEqual(assessment["pilot_shadow_series_summary"]["period_count"], 2)
        self.assertTrue(
            assessment["pilot_shadow_series_summary"]["consecutive_periods_verified"]
        )
        self.assertFalse(assessment["pack_manifest_changed"])
        serialized = json.dumps(assessment, ensure_ascii=False)
        self.assertNotIn("manual_value", serialized)
        self.assertNotIn("agent_value", serialized)
        for private_path in evidence["pilot_shadow_series"].values():
            self.assertNotIn(private_path, serialized)
        self.assertFalse(assessment["raw_pilot_shadow_series_artifacts_persisted"])
        self.assertFalse(assessment["raw_financial_values_persisted"])

    def test_consecutive_series_gate_rejects_single_period_swapped_and_changed_evidence(self):
        evidence = self._evidence()
        evidence["thresholds"]["minimum_distinct_periods"] = 1
        with self.assertRaisesRegex(ReleasePromotionError, "from 2 to 24"):
            build_stable_promotion_assessment(self.runtime, evidence, now=self.clock)

        evidence = self._evidence()
        evidence.pop("pilot_shadow_series")
        with self.assertRaisesRegex(ReleasePromotionError, "fields"):
            build_stable_promotion_assessment(self.runtime, evidence, now=self.clock)

        evidence = self._evidence()
        unrelated = self._evidence(classification="accepted_scope")
        evidence["shadow_close_reports"] = unrelated["shadow_close_reports"]
        with self.assertRaisesRegex(ReleasePromotionError, "exact entity reports"):
            build_stable_promotion_assessment(self.runtime, evidence, now=self.clock)

        evidence = self._evidence()
        evidence["prepared_by"] = "continuity-reviewer-clean"
        with self.assertRaisesRegex(ReleasePromotionError, "series reviewer must be separate"):
            build_stable_promotion_assessment(self.runtime, evidence, now=self.clock)

        evidence = self._evidence()
        report_path = (
            Path(evidence["pilot_shadow_series"]["period_evidence_root"])
            / "2026-08" / "entity-reports" / "cn_dtc_company.json"
        )
        changed = json.loads(report_path.read_text(encoding="utf-8"))
        changed["review"]["rationale"] += " changed-after-review"
        report_path.write_text(json.dumps(changed), encoding="utf-8")
        report_path.chmod(0o600)
        with self.assertRaisesRegex(ReleasePromotionError, "source verification failed|no longer matches"):
            build_stable_promotion_assessment(self.runtime, evidence, now=self.clock)

    def test_template_is_box_pack_bound_and_deliberately_not_assessment_ready(self):
        template = build_stable_promotion_evidence_template(
            self.runtime, "core.finance",
        )
        self.assertEqual(
            template["runtime_fingerprint"], self.runtime.snapshot()["fingerprint"],
        )
        self.assertEqual(template["pack_version"], "0.8.0")
        self.assertEqual(template["sample"]["entity_ids"], ["cn_dtc_company"])
        self.assertFalse(template["sample"]["anonymized"])
        self.assertEqual(template["thresholds"]["minimum_distinct_periods"], 2)
        self.assertEqual(len(template["sample"]["periods"]), 2)
        self.assertEqual(
            set(template["pilot_shadow_series"]),
            {"reviewed_receipt_path", "period_evidence_root", "pipeline_runs_root"},
        )
        self.assertEqual(template["multi_entity_shadow_close_portfolios"], [])
        self.assertEqual(template["connector_shadow_artifacts"], [])
        self.assertEqual(
            {item["gate"] for item in template["automated_gates"]},
            set(AUTOMATED_PROMOTION_GATES),
        )
        with self.assertRaisesRegex(ReleasePromotionError, "anonymized"):
            build_stable_promotion_assessment(self.runtime, template, now=self.clock)
        with self.assertRaisesRegex(ReleasePromotionError, "not selected"):
            build_stable_promotion_evidence_template(
                self.runtime, "jurisdiction.not_installed",
            )
        schema = json.loads(
            (ROOT / "box" / "stable-promotion-evidence.schema.json").read_text()
        )
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["properties"]["schema_version"]["const"], 1)
        shadow_schema = schema["$defs"]["shadow_report"]
        self.assertIn("runtime_fingerprint", shadow_schema["required"])
        self.assertEqual(
            shadow_schema["properties"]["domain_summary"]["type"], "array",
        )

    def test_record_review_separation_status_and_tamper_evidence(self):
        assessment = build_stable_promotion_assessment(
            self.runtime, self._evidence(), now=self.clock,
        )
        recorded = self.store.record_assessment(assessment, actor="promotion-preparer")
        self.assertTrue(recorded["recorded"])
        for principal in (
            "promotion-preparer", "finance-threshold-owner",
            "shadow-operator-1", "entity-reviewer-1",
            "continuity-reviewer-clean",
        ):
            with self.assertRaisesRegex(ReleasePromotionError, "separate"):
                self.store.review(
                    assessment["assessment_id"],
                    runtime_fingerprint=self.runtime.snapshot()["fingerprint"],
                    actor=principal, decision="approved",
                    rationale="Independent release approval rationale",
                    evidence_references=["audit://release-review"],
                )
        approved = self.store.review(
            assessment["assessment_id"],
            runtime_fingerprint=self.runtime.snapshot()["fingerprint"],
            actor="release-reviewer", decision="approved",
            rationale="Independent release approval after reviewing all evidence",
            evidence_references=["audit://release-review"],
        )
        self.assertEqual(approved["release_status"], "stable_candidate_approved")
        status = self.store.status(
            runtime_fingerprint=self.runtime.snapshot()["fingerprint"], limit=25,
        )
        self.assertEqual(status["counts"]["approved_candidates"], 1)
        ledger_before = self.store.events_file.read_bytes()
        snapshot = self.store.readiness_snapshot(
            runtime_fingerprint=self.runtime.snapshot()["fingerprint"],
        )
        self.assertTrue(snapshot["ledger_integrity_valid"])
        self.assertEqual(snapshot["counts"]["approved_candidates"], 1)
        self.assertEqual(snapshot["candidates"], [{
            "pack_id": "core.finance",
            "pack_version": "0.8.0",
            "candidate_eligible": True,
            "release_status": "stable_candidate_approved",
        }])
        self.assertEqual(self.store.events_file.read_bytes(), ledger_before)
        safe_projection = json.dumps(snapshot, ensure_ascii=False)
        self.assertNotIn(assessment["assessment_id"], safe_projection)
        self.assertNotIn("release-reviewer", safe_projection)
        self.assertNotIn("audit://release-review", safe_projection)
        self.assertFalse(snapshot["pack_manifest_changed"])
        external_link = Path(self.temp.name) / "promotion-ledger-hardlink"
        external_link.hardlink_to(self.store.events_file)
        with self.assertRaisesRegex(ReleasePromotionError, "hard-linked"):
            self.store.readiness_snapshot(
                runtime_fingerprint=self.runtime.snapshot()["fingerprint"],
            )
        external_link.unlink()
        stage = _stable_promotion_stage(
            self.runtime,
            self.store.root,
            [{
                "pack_id": "core.finance", "version": "0.8.0",
                "status": "preview",
            }],
        )
        self.assertEqual(stage["status"], "all_selected_pack_candidates_approved")
        self.assertTrue(stage["gate_passed"])
        self.assertEqual(stage["facts"]["approved_target_pack_count"], 1)
        partial = _stable_promotion_stage(
            self.runtime,
            self.store.root,
            [
                {
                    "pack_id": "core.finance", "version": "0.8.0",
                    "status": "preview",
                },
                {
                    "pack_id": "industry.commerce", "version": "0.2.0",
                    "status": "experimental",
                },
            ],
        )
        self.assertEqual(partial["status"], "partial_or_pending_pack_promotion")
        self.assertFalse(partial["gate_passed"])
        later_evidence = self._evidence()
        later_evidence["thresholds"]["rationale"] += " Reassessed for the next source change."
        later = build_stable_promotion_assessment(
            self.runtime,
            later_evidence,
            now=self.clock + timedelta(seconds=1),
        )
        self.store.record_assessment(later, actor="promotion-preparer")
        superseded_by_latest = _stable_promotion_stage(
            self.runtime,
            self.store.root,
            [{
                "pack_id": "core.finance", "version": "0.8.0",
                "status": "preview",
            }],
        )
        self.assertEqual(
            superseded_by_latest["status"], "partial_or_pending_pack_promotion",
        )
        self.assertFalse(superseded_by_latest["gate_passed"])
        self.assertEqual(
            superseded_by_latest["facts"]["approved_target_pack_count"], 0,
        )
        serialized = self.store.events_file.read_text(encoding="utf-8")
        self.assertNotIn("manual_value", serialized)
        self.assertNotIn("agent_value", serialized)
        self.assertNotIn(str(Path(self.temp.name) / "pilot-series-clean"), serialized)
        self.assertTrue(self.store.verify()["valid"])
        self.store.events_file.write_text(
            serialized.replace('"sequence":1', '"sequence":2'), encoding="utf-8",
        )
        with self.assertRaisesRegex(ReleasePromotionError, "sequence or chain"):
            self.store.verify()

    def test_readiness_snapshot_is_read_only_and_rejects_unsafe_roots(self):
        empty = Path(self.temp.name) / "empty-promotion"
        empty.mkdir(mode=0o700)
        snapshot = ReleasePromotionStore(empty).readiness_snapshot(
            runtime_fingerprint=self.runtime.snapshot()["fingerprint"],
        )
        self.assertEqual(snapshot["event_count"], 0)
        self.assertEqual(list(empty.iterdir()), [])

        extra = empty / "unexpected.json"
        extra.write_text("{}", encoding="utf-8")
        extra.chmod(0o600)
        with self.assertRaisesRegex(ReleasePromotionError, "unexpected entries"):
            ReleasePromotionStore(empty).readiness_snapshot(
                runtime_fingerprint=self.runtime.snapshot()["fingerprint"],
            )
        extra.unlink()
        empty.chmod(0o755)
        with self.assertRaisesRegex(ReleasePromotionError, "mode 0700"):
            ReleasePromotionStore(empty).readiness_snapshot(
                runtime_fingerprint=self.runtime.snapshot()["fingerprint"],
            )

    def test_assessment_expires_before_late_release_review(self):
        assessment = build_stable_promotion_assessment(
            self.runtime, self._evidence(), now=self.clock,
        )
        self.store.record_assessment(assessment, actor="promotion-preparer")
        future = self.clock + timedelta(days=8)

        class FutureDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return future if tz is None else future.astimezone(tz)

        with patch("src.release_promotion.datetime", FutureDateTime):
            status = self.store.status(
                runtime_fingerprint=self.runtime.snapshot()["fingerprint"],
            )
            self.assertEqual(status["counts"]["review_expired"], 1)
            self.assertEqual(
                status["assessments"][0]["release_status"], "assessment_expired",
            )
            with self.assertRaisesRegex(ReleasePromotionError, "review window expired"):
                self.store.review(
                    assessment["assessment_id"],
                    runtime_fingerprint=self.runtime.snapshot()["fingerprint"],
                    actor="release-reviewer", decision="approved",
                    rationale="Independent release approval after reviewing all evidence",
                    evidence_references=["audit://late-release-review"],
                )

    def test_accepted_difference_and_system_defect_remain_blocked(self):
        evidence = self._evidence(classification="accepted_scope")
        evidence["thresholds"]["maximum_accepted_exceptions"] = 1
        assessment = build_stable_promotion_assessment(self.runtime, evidence, now=self.clock)
        self.assertFalse(assessment["candidate_eligible"])
        self.assertIn("shadow_match_rate", {item["code"] for item in assessment["blockers"]})
        with self.assertRaisesRegex(ReleasePromotionError, "blocked"):
            self.store.review(
                self.store.record_assessment(assessment, actor="promotion-preparer")["assessment_id"],
                runtime_fingerprint=self.runtime.snapshot()["fingerprint"],
                actor="release-reviewer", decision="approved",
                rationale="Attempt to approve a deliberately blocked assessment",
                evidence_references=["audit://blocked-review"],
            )
        defect = self._evidence(classification="system_defect")
        defect["thresholds"]["maximum_accepted_exceptions"] = 1
        with self.assertRaisesRegex(ReleasePromotionError, "approved defect-free"):
            build_stable_promotion_assessment(
                self.runtime, defect, now=self.clock,
            )

    def test_runtime_report_and_assessment_tampering_fail_closed(self):
        evidence = self._evidence()
        evidence["runtime_fingerprint"] = "0" * 64
        with self.assertRaisesRegex(ReleasePromotionError, "different Box"):
            build_stable_promotion_assessment(self.runtime, evidence, now=self.clock)
        evidence = self._evidence()
        evidence["shadow_close_reports"][0]["runtime_fingerprint"] = "0" * 64
        with self.assertRaisesRegex(ReleasePromotionError, "different Box runtime"):
            build_stable_promotion_assessment(self.runtime, evidence, now=self.clock)
        evidence = self._evidence()
        evidence["shadow_close_reports"][0]["comparisons"][0]["manual_value"] = 999
        with self.assertRaisesRegex(ReleasePromotionError, "difference|fingerprint"):
            build_stable_promotion_assessment(self.runtime, evidence, now=self.clock)
        evidence = self._evidence()
        evidence["shadow_close_reports"][0]["period"] = "2026-06"
        with self.assertRaisesRegex(ReleasePromotionError, "fingerprint"):
            build_stable_promotion_assessment(self.runtime, evidence, now=self.clock)
        evidence = self._evidence()
        evidence["shadow_close_reports"][0]["review"]["evidence"] = []
        with self.assertRaisesRegex(ReleasePromotionError, "evidence"):
            build_stable_promotion_assessment(self.runtime, evidence, now=self.clock)
        evidence = self._evidence()
        report = evidence["shadow_close_reports"][0]
        report["comparisons"][0]["status"] = "需解释"
        report["matched_count"] -= 1
        report["exception_count"] += 1
        report["report_fingerprint"] = _report_fingerprint(report)
        report["review"]["report_fingerprint"] = report["report_fingerprint"]
        report["review"]["decision"] = "接受差异"
        report["review"]["exception_resolutions"] = [{
            "domain": "trial_balance", "key": "1001",
            "classification": "accepted_scope",
            "rationale": "人为更改状态但差额仍在明确容差范围之内",
            "evidence_references": ["audit://forged-status"],
        }]
        with self.assertRaisesRegex(ReleasePromotionError, "explicit tolerance"):
            build_stable_promotion_assessment(self.runtime, evidence, now=self.clock)
        assessment = build_stable_promotion_assessment(
            self.runtime, self._evidence(), now=self.clock,
        )
        assessment["metrics"]["match_rate"] = 0
        with self.assertRaisesRegex(ReleasePromotionError, "assessment_id"):
            self.store.record_assessment(assessment, actor="promotion-preparer")
        assessment = build_stable_promotion_assessment(
            self.runtime, self._evidence(), now=self.clock,
        )
        assessment["evaluated_at"] = "2025-01-01T00:00:00Z"
        with self.assertRaisesRegex(ReleasePromotionError, "assessment_id"):
            self.store.record_assessment(assessment, actor="promotion-preparer")
        assessment = build_stable_promotion_assessment(
            self.runtime, self._evidence(), now=self.clock,
        )
        assessment["unexpected_control"] = True
        assessment["assessment_id"] = _hash({
            key: value for key, value in assessment.items()
            if key not in {"assessment_id", "control_note"}
        })[:24]
        with self.assertRaisesRegex(ReleasePromotionError, "fields"):
            self.store.record_assessment(assessment, actor="promotion-preparer")


if __name__ == "__main__":
    unittest.main()
