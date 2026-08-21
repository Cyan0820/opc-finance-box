from __future__ import annotations

import copy
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.box_runtime import BoxRuntime
from src.cli import main as cli_main
from src.multi_entity_shadow_close import (
    assemble_multi_entity_shadow_close_artifact,
    review_multi_entity_shadow_close_artifact,
)
from src.pilot_shadow_observation import (
    assemble_pilot_shadow_observation,
    review_pilot_shadow_observation,
)
from src.pilot_shadow_run import register_pilot_shadow_run
from src.pilot_shadow_series import (
    PilotShadowSeriesError,
    archive_pilot_shadow_period,
    assemble_pilot_shadow_series,
    build_pilot_shadow_series_status,
    build_pilot_shadow_series_workspace,
    review_pilot_shadow_series,
    verify_pilot_shadow_series,
)
from src.pipeline_run_store import PipelineRunStore
from tests import test_pilot_shadow_observation as observation_test_helpers


ROOT = Path(__file__).resolve().parents[1]
PACKS = ROOT / "packs"


class PilotShadowSeriesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.helper = observation_test_helpers.PilotShadowObservationTests(
            methodName="runTest"
        )
        self.runtime = BoxRuntime(
            ROOT / "examples" / "boxes" / "cn_dtc_shopify_stripe_store.json",
            PACKS,
        )

    @staticmethod
    def write_private(path: Path, value: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")
        path.chmod(0o600)

    def build_period(
        self,
        root: Path,
        evidence_root: Path,
        runs_root: Path,
        period: str,
        *,
        multiplier: int,
        classification: str | None = None,
        runtime: BoxRuntime | None = None,
    ) -> dict:
        selected_runtime = runtime or self.runtime
        entity_ids = sorted(selected_runtime.entities.ids())
        if len(entity_ids) != 1:
            raise AssertionError("build_period is a single-entity test fixture")
        entity_id = entity_ids[0]
        currency = selected_runtime.entities.get(entity_id).functional_currency
        prep = root / f"prep-{period}"
        prep.mkdir()
        readiness, handoff = self.helper.build_reviews(
            selected_runtime, prep, period=period,
        )
        request = {
            "pipeline_id": "finance.month_close_control",
            "payload": {"entity_id": entity_id, "period": period},
        }
        result = self.helper.multi_month_close_result(
            entity_id,
            run_id=(str(multiplier) * 24),
            currency=currency,
            multiplier=multiplier,
        )
        result["lineage"]["period"] = period
        result["founder_briefing"]["period"] = period
        store = PipelineRunStore(runs_root)
        record = store.record(
            selected_runtime.snapshot(), request, result,
            actor=f"shadow-operator-{multiplier}",
        )
        record = store.review(
            record["attempt_id"],
            runtime_fingerprint=selected_runtime.snapshot()["fingerprint"],
            gate="month_close_control_review",
            decision="approved",
            actor=f"pipeline-reviewer-{multiplier}",
            rationale="Independent month-close source evidence was approved.",
            evidence_references=[f"review://shadow/{period}/month-close"],
        )
        period_root = evidence_root / period
        period_root.mkdir(parents=True)
        self.write_private(
            period_root / "pilot-readiness-review.json",
            json.loads(readiness.read_text(encoding="utf-8")),
        )
        self.write_private(
            period_root / "data-handoff-review.json",
            json.loads(handoff.read_text(encoding="utf-8")),
        )
        registration = period_root / "shadow-run-registration.json"
        register_pilot_shadow_run(
            selected_runtime,
            period_root / "data-handoff-review.json",
            period_root / "pilot-readiness-review.json",
            runs_root,
            {entity_id: record["attempt_id"]},
            registration,
            actor=f"shadow-registrar-{multiplier}",
            rationale="Registrar binds the independently reviewed period run evidence.",
            evidence_references=[f"workpaper://shadow/{period}/registration"],
        )
        report = self.helper.reviewed_entity_report(
            selected_runtime,
            entity_id=entity_id,
            classification=classification,
            period=period,
            multiplier=multiplier,
            reviewer=f"entity-reviewer-{multiplier}",
        )
        report_path = period_root / "entity-reports" / f"{entity_id}.json"
        self.write_private(report_path, report)
        receipt = prep / "observation-receipt.json"
        assemble_pilot_shadow_observation(
            selected_runtime,
            registration,
            period_root / "data-handoff-review.json",
            period_root / "pilot-readiness-review.json",
            runs_root,
            [report_path],
            receipt,
        )
        reviewed = period_root / "reviewed-observation.json"
        decision = "needs-correction" if classification == "system_defect" else (
            "accepted-differences" if classification else "passed"
        )
        review_pilot_shadow_observation(
            selected_runtime,
            receipt,
            reviewed,
            decision=decision,
            actor=f"observation-reviewer-{multiplier}",
            rationale="Independent review confirms this exact period observation evidence.",
            evidence_references=[f"audit://shadow/{period}/observation"],
        )
        return {"period_root": period_root, "record": record, "report": report}

    def prepare_clean_series(self, root: Path) -> tuple[Path, Path]:
        evidence_root = root / "period-evidence"
        runs_root = root / "runs"
        self.build_period(
            root, evidence_root, runs_root, "2026-08", multiplier=1,
        )
        self.build_period(
            root, evidence_root, runs_root, "2026-09", multiplier=2,
        )
        return evidence_root, runs_root

    def build_multi_period(
        self,
        runtime: BoxRuntime,
        root: Path,
        evidence_root: Path,
        runs_root: Path,
        period: str,
        *,
        period_index: int,
    ) -> None:
        prep = root / f"multi-prep-{period}"
        prep.mkdir()
        readiness, handoff = self.helper.build_reviews(
            runtime, prep, period=period,
        )
        store = PipelineRunStore(runs_root)
        records = []
        for entity_index, (entity_id, currency) in enumerate(
            (("cn_studio", "CNY"), ("sg_publisher", "USD")), 1,
        ):
            marker = str(period_index * 2 + entity_index)
            request = {
                "pipeline_id": "finance.month_close_control",
                "payload": {"entity_id": entity_id, "period": period},
            }
            result = self.helper.multi_month_close_result(
                entity_id,
                run_id=marker * 24,
                currency=currency,
                multiplier=entity_index + period_index,
            )
            result["lineage"]["period"] = period
            result["founder_briefing"]["period"] = period
            record = store.record(
                runtime.snapshot(), request, result,
                actor=f"multi-operator-{period_index}-{entity_index}",
            )
            record = store.review(
                record["attempt_id"],
                runtime_fingerprint=runtime.snapshot()["fingerprint"],
                gate="month_close_control_review",
                decision="approved",
                actor=f"multi-pipeline-reviewer-{period_index}-{entity_index}",
                rationale="Independent legal-entity month-close evidence was approved.",
                evidence_references=[
                    f"review://{entity_id}/{period}/month-close"
                ],
            )
            records.append(record)
        period_root = evidence_root / period
        period_root.mkdir(parents=True)
        self.write_private(
            period_root / "pilot-readiness-review.json",
            json.loads(readiness.read_text(encoding="utf-8")),
        )
        self.write_private(
            period_root / "data-handoff-review.json",
            json.loads(handoff.read_text(encoding="utf-8")),
        )
        registration = period_root / "shadow-run-registration.json"
        register_pilot_shadow_run(
            runtime,
            period_root / "data-handoff-review.json",
            period_root / "pilot-readiness-review.json",
            runs_root,
            {record["entity_id"]: record["attempt_id"] for record in records},
            registration,
            actor=f"multi-registrar-{period_index}",
            rationale="Registrar binds both independently reviewed legal-entity runs.",
            evidence_references=[f"workpaper://shadow/{period}/multi-registration"],
        )
        report_paths = []
        for entity_index, entity_id in enumerate(("cn_studio", "sg_publisher"), 1):
            report_path = period_root / "entity-reports" / f"{entity_id}.json"
            self.write_private(
                report_path,
                self.helper.reviewed_entity_report(
                    runtime,
                    entity_id=entity_id,
                    period=period,
                    multiplier=entity_index + period_index,
                    reviewer=f"multi-entity-reviewer-{period_index}-{entity_index}",
                ),
            )
            report_paths.append(report_path)
        portfolio_result = self.helper.portfolio_result(records)
        portfolio_result["lineage"]["period"] = period
        portfolio_result["founder_briefing"]["period"] = period
        portfolio_path = prep / "portfolio-result.json"
        self.write_private(portfolio_path, portfolio_result)
        manifest = prep / "portfolio-manifest.json"
        assemble_multi_entity_shadow_close_artifact(
            runtime, report_paths, portfolio_path, manifest,
        )
        portfolio_review = period_root / "portfolio-review.json"
        review_multi_entity_shadow_close_artifact(
            runtime,
            manifest,
            portfolio_review,
            decision="passed",
            actor=f"multi-portfolio-reviewer-{period_index}",
            rationale="Independent review confirms the exact portfolio and source scope.",
            evidence_references=[f"audit://portfolio/{period}/independent-review"],
        )
        observation = prep / "observation.json"
        assemble_pilot_shadow_observation(
            runtime,
            registration,
            period_root / "data-handoff-review.json",
            period_root / "pilot-readiness-review.json",
            runs_root,
            report_paths,
            observation,
            portfolio_review_path=portfolio_review,
        )
        review_pilot_shadow_observation(
            runtime,
            observation,
            period_root / "reviewed-observation.json",
            decision="passed",
            actor=f"multi-observation-reviewer-{period_index}",
            rationale="Independent observation review confirms exact portfolio lineage.",
            evidence_references=[f"audit://observation/{period}/multi-review"],
        )

    def test_clean_consecutive_series_reviews_and_verifies_without_values(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            evidence_root, runs_root = self.prepare_clean_series(root)
            receipt = root / "series-receipt.json"
            assembled = assemble_pilot_shadow_series(
                self.runtime, evidence_root, runs_root, receipt,
            )
            self.assertEqual(assembled["period_count"], 2)
            self.assertEqual(
                assembled["series_result_candidate"],
                "ready_for_promotion_evidence",
            )
            self.assertFalse(assembled["raw_financial_values_returned"])
            if os.name != "nt":
                self.assertEqual(receipt.stat().st_mode & 0o777, 0o600)
            persisted = receipt.read_text(encoding="utf-8")
            for forbidden in ("manual_value", "agent_value", "difference"):
                self.assertNotIn(forbidden, persisted)
            reviewed = root / "series-reviewed.json"
            result = review_pilot_shadow_series(
                self.runtime,
                receipt,
                reviewed,
                decision="approved-for-promotion-evidence",
                actor="continuity-independent-reviewer",
                rationale="Independent review confirms two consecutive current observations.",
                evidence_references=["audit://shadow/series/independent-review"],
            )
            self.assertTrue(result["eligible_to_prepare_stable_promotion_evidence"])
            self.assertFalse(result["ready_for_stable_promotion"])
            verified = verify_pilot_shadow_series(
                self.runtime, reviewed, evidence_root, runs_root,
            )
            self.assertTrue(verified["consecutive_periods_verified"])
            self.assertTrue(verified["eligible_to_prepare_stable_promotion_evidence"])
            self.assertFalse(verified["posting_authorized"])
            safe = json.dumps(verified)
            self.assertNotIn("shadow-registrar", safe)
            self.assertNotIn("source_bundle_fingerprint", safe)
            status = build_pilot_shadow_series_status(
                self.runtime, reviewed, evidence_root, runs_root,
            )
            self.assertEqual(status["status"], "current")
            workspace = build_pilot_shadow_series_workspace(
                self.runtime, reviewed, evidence_root, runs_root,
            )
            self.assertEqual(workspace["summary"]["period_count"], 2)
            self.assertTrue(
                workspace["summary"][
                    "eligible_to_prepare_stable_promotion_evidence"
                ]
            )
            self.assertFalse(workspace["summary"]["ready_for_stable_promotion"])

    def test_multi_entity_series_requires_and_reverifies_each_portfolio(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime = BoxRuntime(
                ROOT / "examples" / "boxes" / "global_game_studio.json", PACKS,
            )
            evidence_root = root / "period-evidence"
            runs_root = root / "runs"
            self.build_multi_period(
                runtime, root, evidence_root, runs_root, "2026-07", period_index=1,
            )
            self.build_multi_period(
                runtime, root, evidence_root, runs_root, "2026-08", period_index=2,
            )
            receipt = root / "multi-series.json"
            assembled = assemble_pilot_shadow_series(
                runtime, evidence_root, runs_root, receipt,
            )
            self.assertEqual(assembled["period_count"], 2)
            reviewed = root / "multi-series-reviewed.json"
            review_pilot_shadow_series(
                runtime,
                receipt,
                reviewed,
                decision="approved-for-promotion-evidence",
                actor="multi-continuity-independent-reviewer",
                rationale="Independent continuity review confirms both entity portfolios.",
                evidence_references=["audit://shadow/multi-series/review"],
            )
            verified = verify_pilot_shadow_series(
                runtime, reviewed, evidence_root, runs_root,
            )
            self.assertTrue(verified["eligible_to_prepare_stable_promotion_evidence"])
            missing_portfolio = evidence_root / "2026-08" / "portfolio-review.json"
            missing_portfolio.rename(root / "detached-portfolio.json")
            with self.assertRaisesRegex(PilotShadowSeriesError, "exact evidence layout"):
                verify_pilot_shadow_series(
                    runtime, reviewed, evidence_root, runs_root,
                )

    def test_multi_entity_period_archive_builds_exact_series_layout(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            runtime = BoxRuntime(
                ROOT / "examples" / "boxes" / "global_game_studio.json", PACKS,
            )
            source_root = root / "source-periods"
            archive_root = root / "archived-periods"
            runs_root = root / "runs"
            archive_root.mkdir(mode=0o700)
            archive_root.chmod(0o700)
            for index, period in enumerate(("2026-07", "2026-08"), 1):
                self.build_multi_period(
                    runtime,
                    root,
                    source_root,
                    runs_root,
                    period,
                    period_index=index,
                )
                source = source_root / period
                result = archive_pilot_shadow_period(
                    runtime,
                    source / "reviewed-observation.json",
                    source / "shadow-run-registration.json",
                    source / "data-handoff-review.json",
                    source / "pilot-readiness-review.json",
                    runs_root,
                    [
                        source / "entity-reports" / "cn_studio.json",
                        source / "entity-reports" / "sg_publisher.json",
                    ],
                    archive_root,
                    portfolio_review_path=source / "portfolio-review.json",
                )
                self.assertTrue(result["archive_verified"])
                self.assertTrue(result["portfolio_archived"])
                self.assertEqual(result["private_file_count"], 7)
                self.assertFalse(result["source_paths_returned"])
                self.assertFalse(result["raw_financial_values_returned"])
                serialized = json.dumps(result)
                self.assertNotIn(str(root), serialized)

            expected = {
                "reviewed-observation.json", "shadow-run-registration.json",
                "data-handoff-review.json", "pilot-readiness-review.json",
                "portfolio-review.json", "entity-reports",
            }
            for period in ("2026-07", "2026-08"):
                period_root = archive_root / period
                self.assertEqual({item.name for item in period_root.iterdir()}, expected)
                self.assertEqual(
                    {item.name for item in (period_root / "entity-reports").iterdir()},
                    {"cn_studio.json", "sg_publisher.json"},
                )
                if os.name != "nt":
                    self.assertEqual(period_root.stat().st_mode & 0o777, 0o700)
                    for path in period_root.rglob("*.json"):
                        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            receipt = root / "archive-series-receipt.json"
            assembled = assemble_pilot_shadow_series(
                runtime, archive_root, runs_root, receipt,
            )
            self.assertEqual(assembled["period_count"], 2)

    def test_period_archive_is_private_non_overwriting_and_transactional(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            source_root = root / "source-periods"
            runs_root = root / "runs"
            self.build_period(
                root, source_root, runs_root, "2026-08", multiplier=1,
            )
            source = source_root / "2026-08"
            report = source / "entity-reports" / "cn_dtc_company.json"
            archive_root = root / "archive"
            archive_root.mkdir(mode=0o700)
            archive_root.chmod(0o700)

            result = archive_pilot_shadow_period(
                self.runtime,
                source / "reviewed-observation.json",
                source / "shadow-run-registration.json",
                source / "data-handoff-review.json",
                source / "pilot-readiness-review.json",
                runs_root,
                [report],
                archive_root,
            )
            self.assertTrue(result["archived"])
            with self.assertRaisesRegex(PilotShadowSeriesError, "already exists"):
                archive_pilot_shadow_period(
                    self.runtime,
                    source / "reviewed-observation.json",
                    source / "shadow-run-registration.json",
                    source / "data-handoff-review.json",
                    source / "pilot-readiness-review.json",
                    runs_root,
                    [report],
                    archive_root,
                )

            empty_root = root / "transactional"
            empty_root.mkdir(mode=0o700)
            empty_root.chmod(0o700)
            with patch(
                "src.pilot_shadow_series._write_private_bytes",
                side_effect=OSError("simulated private write failure"),
            ):
                with self.assertRaisesRegex(OSError, "simulated"):
                    archive_pilot_shadow_period(
                        self.runtime,
                        source / "reviewed-observation.json",
                        source / "shadow-run-registration.json",
                        source / "data-handoff-review.json",
                        source / "pilot-readiness-review.json",
                        runs_root,
                        [report],
                        empty_root,
                    )
            self.assertEqual(list(empty_root.iterdir()), [])

            non_private = root / "non-private"
            non_private.mkdir(mode=0o755)
            non_private.chmod(0o755)
            with self.assertRaisesRegex(PilotShadowSeriesError, "mode 0700"):
                archive_pilot_shadow_period(
                    self.runtime,
                    source / "reviewed-observation.json",
                    source / "shadow-run-registration.json",
                    source / "data-handoff-review.json",
                    source / "pilot-readiness-review.json",
                    runs_root,
                    [report],
                    non_private,
                )

    def test_gap_extra_file_and_period_role_overlap_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            evidence_root, runs_root = self.prepare_clean_series(root)
            (evidence_root / "2026-09").rename(evidence_root / "2026-10")
            with self.assertRaisesRegex(PilotShadowSeriesError, "consecutive"):
                assemble_pilot_shadow_series(
                    self.runtime, evidence_root, runs_root, root / "gap.json",
                )
            (evidence_root / "2026-10").rename(evidence_root / "2026-09")
            extra = evidence_root / "2026-09" / "notes.txt"
            extra.write_text("not allowed", encoding="utf-8")
            with self.assertRaisesRegex(PilotShadowSeriesError, "exact evidence layout"):
                assemble_pilot_shadow_series(
                    self.runtime, evidence_root, runs_root, root / "extra.json",
                )
            extra.unlink()
            receipt = root / "receipt.json"
            assemble_pilot_shadow_series(
                self.runtime, evidence_root, runs_root, receipt,
            )
            with self.assertRaisesRegex(PilotShadowSeriesError, "every period principal"):
                review_pilot_shadow_series(
                    self.runtime,
                    receipt,
                    root / "overlap.json",
                    decision="approved-for-promotion-evidence",
                    actor="observation-reviewer-1",
                    rationale="This deliberately overlaps a prior observation reviewer.",
                    evidence_references=["audit://shadow/series/role-overlap"],
                )

    def test_source_tamper_after_review_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            evidence_root, runs_root = self.prepare_clean_series(root)
            receipt = root / "receipt.json"
            reviewed = root / "reviewed.json"
            assemble_pilot_shadow_series(
                self.runtime, evidence_root, runs_root, receipt,
            )
            review_pilot_shadow_series(
                self.runtime,
                receipt,
                reviewed,
                decision="approved-for-promotion-evidence",
                actor="continuity-independent-reviewer",
                rationale="Independent review confirms exact current period evidence.",
                evidence_references=["audit://shadow/series/current-evidence"],
            )
            report_path = (
                evidence_root / "2026-09" / "entity-reports" / "cn_dtc_company.json"
            )
            original = json.loads(report_path.read_text(encoding="utf-8"))
            tampered = copy.deepcopy(original)
            tampered["review"]["rationale"] += " tampered"
            self.write_private(report_path, tampered)
            with self.assertRaisesRegex(PilotShadowSeriesError, "verification failed"):
                verify_pilot_shadow_series(
                    self.runtime, reviewed, evidence_root, runs_root,
                )

    def test_latest_system_defect_records_blocked_series(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            evidence_root = root / "period-evidence"
            runs_root = root / "runs"
            self.build_period(
                root, evidence_root, runs_root, "2026-08", multiplier=1,
            )
            self.build_period(
                root, evidence_root, runs_root, "2026-09", multiplier=2,
                classification="system_defect",
            )
            receipt = root / "receipt.json"
            assembled = assemble_pilot_shadow_series(
                self.runtime, evidence_root, runs_root, receipt,
            )
            self.assertEqual(assembled["series_result_candidate"], "needs_correction")
            with self.assertRaisesRegex(PilotShadowSeriesError, "complete consecutive"):
                review_pilot_shadow_series(
                    self.runtime,
                    receipt,
                    root / "wrong.json",
                    decision="approved-for-promotion-evidence",
                    actor="continuity-independent-reviewer",
                    rationale="A system defect cannot enter promotion evidence preparation.",
                    evidence_references=["audit://shadow/series/system-defect"],
                )
            reviewed = root / "reviewed.json"
            result = review_pilot_shadow_series(
                self.runtime,
                receipt,
                reviewed,
                decision="needs-correction",
                actor="continuity-independent-reviewer",
                rationale="The latest system defect must be corrected before continuing.",
                evidence_references=["audit://shadow/series/correction-required"],
            )
            self.assertFalse(result["eligible_to_prepare_stable_promotion_evidence"])
            verified = verify_pilot_shadow_series(
                self.runtime, reviewed, evidence_root, runs_root,
            )
            self.assertFalse(verified["ready_for_next_shadow_period"])

    def test_cli_complete_series_chain(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            source_root, runs_root = self.prepare_clean_series(root)
            evidence_root = root / "cli-archived-periods"
            evidence_root.mkdir(mode=0o700)
            evidence_root.chmod(0o700)
            config = ROOT / "examples" / "boxes" / "cn_dtc_shopify_stripe_store.json"
            receipt = root / "cli-receipt.json"
            reviewed = root / "cli-reviewed.json"
            output = StringIO()
            with redirect_stdout(output), redirect_stderr(output):
                for period in ("2026-08", "2026-09"):
                    source = source_root / period
                    self.assertEqual(cli_main([
                        "pilot-shadow-period-archive", str(config),
                        str(source / "reviewed-observation.json"),
                        str(source / "shadow-run-registration.json"),
                        str(source / "data-handoff-review.json"),
                        str(source / "pilot-readiness-review.json"),
                        "--entity-report",
                        str(source / "entity-reports" / "cn_dtc_company.json"),
                        "--evidence-root", str(evidence_root),
                        "--runs-root", str(runs_root),
                    ]), 0)
                self.assertEqual(cli_main([
                    "pilot-shadow-series-assemble", str(config),
                    str(evidence_root), "--runs-root", str(runs_root),
                    "--output", str(receipt),
                ]), 0)
                self.assertEqual(cli_main([
                    "pilot-shadow-series-review", str(config), str(receipt),
                    "--decision", "approved-for-promotion-evidence",
                    "--actor", "continuity-cli-reviewer",
                    "--rationale",
                    "Independent CLI review confirms the consecutive period evidence.",
                    "--evidence-reference", "audit://shadow/series/cli-review",
                    "--output", str(reviewed),
                ]), 0)
                self.assertEqual(cli_main([
                    "pilot-shadow-series-verify", str(config), str(reviewed),
                    str(evidence_root), "--runs-root", str(runs_root),
                ]), 0)
            payload = output.getvalue()
            self.assertIn('"consecutive_periods_verified": true', payload)
            self.assertNotIn("manual_value", payload)


if __name__ == "__main__":
    unittest.main()
