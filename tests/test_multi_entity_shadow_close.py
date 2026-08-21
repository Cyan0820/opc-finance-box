import copy
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from datetime import datetime, timezone

from src.box_runtime import BoxRuntime
from src.cli import main as cli_main
from src.multi_entity_shadow_close import (
    MultiEntityShadowCloseError,
    assemble_multi_entity_shadow_close_artifact,
    review_multi_entity_shadow_close_artifact,
    verify_multi_entity_shadow_close_artifact,
)
from src.release_promotion import (
    AUTOMATED_PROMOTION_GATES,
    REQUIRED_REHEARSALS,
    ReleasePromotionError,
    ReleasePromotionStore,
    build_stable_promotion_evidence_template,
    build_stable_promotion_assessment,
)
from src.pilot_shadow_series import (
    assemble_pilot_shadow_series,
    review_pilot_shadow_series,
)
from src.shadow_close import compare_shadow_close, review_shadow_close
from tests import test_pilot_shadow_series as pilot_series_test_helpers


ROOT = Path(__file__).resolve().parents[1]


class MultiEntityShadowCloseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = ROOT / "examples" / "boxes" / "global_game_studio.json"
        self.runtime = BoxRuntime(self.config, ROOT / "packs")
        self.period = "2026-07"
        self.entity_paths = []
        for index, entity_id in enumerate(("cn_studio", "sg_publisher"), 1):
            report = self._reviewed_entity_report(
                entity_id, reviewer=f"entity-reviewer-{index}", multiplier=index,
            )
            path = self.root / f"{entity_id}-reviewed.json"
            path.write_text(json.dumps(report), encoding="utf-8")
            self.entity_paths.append(path)
        self.portfolio_result = self._portfolio_result()
        self.portfolio_path = self.root / "portfolio-result.json"
        self.portfolio_path.write_text(json.dumps(self.portfolio_result), encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def _reviewed_entity_report(self, entity_id, *, reviewer, multiplier):
        values = {
            "BS_ASSETS": 100 * multiplier,
            "BS_LIABILITIES": 20 * multiplier,
            "BS_EQUITY": 80 * multiplier,
            "IS_REVENUE": 50 * multiplier,
            "IS_EXPENSES": 30 * multiplier,
            "IS_PROFIT": 20 * multiplier,
        }
        rows = [{
            "domain": "trial_balance",
            "entity_id": entity_id,
            "period": self.period,
            "key": "1001",
            "name": "Cash",
            "value": 10 * multiplier,
            "absolute_tolerance": 0,
            "percent_tolerance": 0,
        }, *[{
            "domain": "statement",
            "entity_id": entity_id,
            "period": self.period,
            "key": key,
            "name": key,
            "value": value,
            "absolute_tolerance": 0,
            "percent_tolerance": 0,
        } for key, value in values.items()]]
        baseline = {
            "id": f"SHADOW-{entity_id}-{self.period}",
            "entity_id": entity_id,
            "period": self.period,
            "source_fingerprint": str(multiplier) * 64,
            "rows": rows,
        }
        finance = {
            "financial_statements": {
                "detail": [{
                    "account": "1001 Cash",
                    "closing_debit": 10 * multiplier,
                    "closing_credit": 0,
                }],
                "balance_sheet": {
                    "assets": values["BS_ASSETS"],
                    "liabilities": values["BS_LIABILITIES"],
                    "liabilities_and_equity": values["BS_ASSETS"],
                },
                "income_statement": {
                    "revenue": values["IS_REVENUE"],
                    "expenses": values["IS_EXPENSES"],
                    "profit_before_tax": values["IS_PROFIT"],
                },
            },
            "tax_pack": {},
        }
        report = compare_shadow_close(
            baseline,
            finance,
            runtime_fingerprint=self.runtime.snapshot()["fingerprint"],
        )
        review = review_shadow_close(
            report,
            "验证通过",
            reviewer,
            "Independent entity close evidence was checked",
            [f"audit://{entity_id}/shadow-close"],
        )
        report["review"] = review
        report["review_current"] = True
        return report

    def _portfolio_result(self):
        entity_ids = ["cn_studio", "sg_publisher"]
        sources = []
        for index, entity_id in enumerate(entity_ids, 1):
            sources.append({
                "attempt_id": str(index) * 24,
                "entity_id": entity_id,
                "run_id": chr(96 + index) * 24,
                "result_fingerprint": str(index) * 64,
                "portfolio_source_fingerprint": chr(96 + index) * 64,
                "review_complete": True,
            })
        return {
            "pipeline": {
                "pipeline_id": "finance.multi_entity_month_close_portfolio",
                "run_id": "c" * 24,
                "required_review_gates": ["month_close_portfolio_review"],
            },
            "ready": True,
            "blocked_at": None,
            "source_run_ledger_verified": True,
            "source_run_ledger_verification": {
                "verified": True,
                "chain_head": "d" * 64,
                "source_count": 2,
                "sources": sources,
                "raw_pipeline_results_persisted": False,
            },
            "lineage": {
                "period": self.period,
                "entity_ids": entity_ids,
                "source_run_ledger_verified": True,
                "source_attempt_ids": [item["attempt_id"] for item in sources],
            },
            "founder_briefing": {
                "period": self.period,
                "entity_count": 2,
                "ready_entity_count": 2,
                "statutory_readiness": [{
                    "entity_id": entity_id,
                    "ready_for_portfolio_review": True,
                } for entity_id in entity_ids],
                "management_portfolio_totals": {
                    "revenue": 150,
                    "expenses": 90,
                    "profit_before_tax_candidate": 60,
                },
                "candidate_only": True,
                "pre_elimination_view": True,
                "cross_entity_native_currency_netting_performed": False,
                "consolidated_financial_statements_produced": False,
                "posting_or_period_close_performed": False,
            },
            "external_actions_performed": False,
            "statutory_books_modified": False,
            "posting_performed": False,
            "period_close_performed": False,
            "external_filing_performed": False,
        }

    def test_assemble_review_and_verify_store_no_financial_values(self):
        manifest_path = self.root / "portfolio-shadow-manifest.json"
        assembled = assemble_multi_entity_shadow_close_artifact(
            self.runtime, self.entity_paths, self.portfolio_path, manifest_path,
        )
        self.assertEqual(assembled["entity_count"], 2)
        self.assertEqual(assembled["comparison_count"], 14)
        self.assertEqual(assembled["exception_count"], 0)
        self.assertFalse(assembled["raw_financial_values_written_to_output"])
        self.assertEqual(manifest_path.stat().st_mode & 0o777, 0o600)
        persisted = manifest_path.read_text(encoding="utf-8")
        for forbidden in ("manual_value", "agent_value", "management_portfolio_totals"):
            self.assertNotIn(forbidden, persisted)

        with self.assertRaisesRegex(MultiEntityShadowCloseError, "independent"):
            review_multi_entity_shadow_close_artifact(
                self.runtime,
                manifest_path,
                self.root / "not-independent.json",
                decision="passed",
                actor="entity-reviewer-1",
                rationale="The complete portfolio evidence was independently checked",
                evidence_references=["audit://portfolio-review"],
            )
        reviewed_path = self.root / "portfolio-shadow-reviewed.json"
        reviewed = review_multi_entity_shadow_close_artifact(
            self.runtime,
            manifest_path,
            reviewed_path,
            decision="passed",
            actor="portfolio-independent-reviewer",
            rationale="The complete portfolio evidence was independently checked",
            evidence_references=["audit://portfolio-review"],
        )
        self.assertTrue(reviewed["review_current"])
        self.assertFalse(reviewed["raw_financial_values_written_to_output"])
        verified = verify_multi_entity_shadow_close_artifact(self.runtime, reviewed_path)
        self.assertTrue(verified["valid"])
        self.assertEqual(verified["decision"], "passed")
        self.assertFalse(verified["raw_financial_values_returned"])
        tampered_review = json.loads(reviewed_path.read_text(encoding="utf-8"))
        tampered_review["review"]["decision"] = "needs-correction"
        reviewed_path.write_text(json.dumps(tampered_review), encoding="utf-8")
        with self.assertRaisesRegex(MultiEntityShadowCloseError, "fingerprint"):
            verify_multi_entity_shadow_close_artifact(self.runtime, reviewed_path)

    def test_missing_entity_unverified_source_and_tampered_manifest_fail_closed(self):
        with self.assertRaisesRegex(MultiEntityShadowCloseError, "every configured entity"):
            assemble_multi_entity_shadow_close_artifact(
                self.runtime,
                self.entity_paths[:1],
                self.portfolio_path,
                self.root / "missing.json",
            )
        unverified = copy.deepcopy(self.portfolio_result)
        unverified["source_run_ledger_verified"] = False
        unverified_path = self.root / "unverified.json"
        unverified_path.write_text(json.dumps(unverified), encoding="utf-8")
        with self.assertRaisesRegex(MultiEntityShadowCloseError, "not verified"):
            assemble_multi_entity_shadow_close_artifact(
                self.runtime,
                self.entity_paths,
                unverified_path,
                self.root / "unverified-manifest.json",
            )
        manifest_path = self.root / "manifest.json"
        assemble_multi_entity_shadow_close_artifact(
            self.runtime, self.entity_paths, self.portfolio_path, manifest_path,
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["entity_reports"][0]["exception_count"] = 1
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(MultiEntityShadowCloseError, "counts|fingerprint"):
            review_multi_entity_shadow_close_artifact(
                self.runtime,
                manifest_path,
                self.root / "tampered-review.json",
                decision="needs-correction",
                actor="portfolio-independent-reviewer",
                rationale="The portfolio evidence contains a detected inconsistency",
                evidence_references=["audit://tamper-detection"],
            )

    def test_cli_exposes_complete_portfolio_shadow_close_flow(self):
        manifest_path = self.root / "cli-manifest.json"
        reviewed_path = self.root / "cli-reviewed.json"
        code, output, error = self._cli([
            "shadow-close-portfolio-assemble",
            str(self.config),
            "--entity-report", str(self.entity_paths[0]),
            "--entity-report", str(self.entity_paths[1]),
            "--portfolio-result", str(self.portfolio_path),
            "--output", str(manifest_path),
        ])
        self.assertEqual(code, 0, error)
        self.assertFalse(json.loads(output)["result"]["raw_financial_values_returned"])
        code, _, error = self._cli([
            "shadow-close-portfolio-review",
            str(self.config), str(manifest_path),
            "--decision", "passed",
            "--actor", "portfolio-independent-reviewer",
            "--rationale", "The complete portfolio evidence was independently checked",
            "--evidence-reference", "audit://portfolio-review",
            "--output", str(reviewed_path),
        ])
        self.assertEqual(code, 0, error)
        code, output, error = self._cli([
            "shadow-close-portfolio-verify",
            str(self.config), str(reviewed_path),
        ])
        self.assertEqual(code, 0, error)
        self.assertTrue(json.loads(output)["result"]["valid"])
        self.assertNotIn("manual_value", output)

    def test_stable_promotion_requires_and_summarizes_portfolio_shadow_evidence(self):
        clock = datetime.now(timezone.utc).replace(microsecond=0)
        periods = ("2026-07", "2026-08")
        evidence_root = self.root / "promotion-period-evidence"
        runs_root = self.root / "promotion-runs"
        helper = pilot_series_test_helpers.PilotShadowSeriesTests(methodName="runTest")
        helper.setUp()
        for index, period in enumerate(periods, 1):
            helper.build_multi_period(
                self.runtime,
                self.root,
                evidence_root,
                runs_root,
                period,
                period_index=index,
            )
        series_receipt = self.root / "promotion-series-receipt.json"
        series_reviewed = self.root / "promotion-series-reviewed.json"
        assemble_pilot_shadow_series(
            self.runtime,
            evidence_root,
            runs_root,
            series_receipt,
            as_of=clock.date().isoformat(),
        )
        review_pilot_shadow_series(
            self.runtime,
            series_receipt,
            series_reviewed,
            decision="approved-for-promotion-evidence",
            actor="multi-release-continuity-reviewer",
            rationale="Independent continuity review confirms both multi-entity periods.",
            evidence_references=["audit://multi-entity/release-series/review"],
        )
        reports = [
            json.loads(
                (evidence_root / period / "entity-reports" / f"{entity_id}.json")
                .read_text(encoding="utf-8")
            )
            for period in periods
            for entity_id in ("cn_studio", "sg_publisher")
        ]
        reviewed_manifests = [
            json.loads(
                (evidence_root / period / "portfolio-review.json")
                .read_text(encoding="utf-8")
            )
            for period in periods
        ]
        completed = clock.isoformat().replace("+00:00", "Z")
        fingerprint = self.runtime.snapshot()["fingerprint"]
        evidence = {
            "schema_version": 1,
            "runtime_fingerprint": fingerprint,
            "pack_id": "feature.multi_entity",
            "pack_version": "0.2.0",
            "prepared_by": "promotion-preparer",
            "sample": {
                "description": "Anonymized representative multi-entity monthly close sample",
                "anonymized": True,
                "representative": True,
                "entity_ids": ["cn_studio", "sg_publisher"],
                "periods": list(periods),
                "operator_principals": [
                    "multi-operator-1-1", "multi-operator-1-2",
                    "multi-operator-2-1", "multi-operator-2-2",
                ],
                "evidence_references": ["audit://multi-entity-sample"],
            },
            "thresholds": {
                "minimum_distinct_entities": 2,
                "minimum_distinct_periods": 2,
                "minimum_comparisons_per_report": 7,
                "minimum_match_rate": 1,
                "maximum_accepted_exceptions": 0,
                "required_domains": ["trial_balance", "statement"],
                "maximum_shadow_age_days": 30,
                "maximum_gate_age_days": 30,
                "maximum_rehearsal_age_days": 180,
                "approved_by": "finance-threshold-owner",
                "rationale": "Require complete entity reports and a reviewed portfolio with no differences",
            },
            "shadow_close_reports": reports,
            "multi_entity_shadow_close_portfolios": reviewed_manifests,
            "connector_shadow_artifacts": [],
            "pilot_shadow_series": {
                "reviewed_receipt_path": str(series_reviewed),
                "period_evidence_root": str(evidence_root),
                "pipeline_runs_root": str(runs_root),
            },
            "automated_gates": [{
                "gate": gate,
                "passed": True,
                "completed_at": completed,
                "runtime_fingerprint": fingerprint,
                "evidence_references": [f"ci://gate-{index}"],
            } for index, gate in enumerate(AUTOMATED_PROMOTION_GATES, 1)],
            "rehearsals": {
                name: {
                    "passed": True,
                    "completed_at": completed,
                    "runtime_fingerprint": fingerprint,
                    "evidence_references": [f"audit://rehearsal-{index}"],
                }
                for index, name in enumerate(REQUIRED_REHEARSALS, 1)
            },
            "known_limitations": [],
            "contains_financial_results": True,
            "storage_boundary": "input_only_not_persisted",
        }
        assessment = build_stable_promotion_assessment(
            self.runtime, evidence, now=clock,
        )
        self.assertTrue(assessment["candidate_eligible"])
        self.assertEqual(assessment["metrics"]["portfolio_shadow_count"], 2)
        self.assertEqual(len(assessment["portfolio_shadow_summaries"]), 2)
        self.assertFalse(assessment["raw_portfolio_shadow_manifests_persisted"])
        serialized = json.dumps(assessment, ensure_ascii=False)
        self.assertNotIn("management_portfolio_totals", serialized)
        self.assertNotIn("manual_value", serialized)
        template = build_stable_promotion_evidence_template(
            self.runtime, "feature.multi_entity",
        )
        self.assertEqual(template["multi_entity_shadow_close_portfolios"], [])
        self.assertEqual(template["connector_shadow_artifacts"], [])
        self.assertEqual(
            template["sample"]["entity_ids"], ["cn_studio", "sg_publisher"],
        )
        store = ReleasePromotionStore(self.root / "release-promotion")
        stored = store.record_assessment(assessment, actor="promotion-preparer")
        self.assertTrue(stored["recorded"])
        ledger_text = store.events_file.read_text(encoding="utf-8")
        self.assertIn("portfolio_shadow_summaries", ledger_text)
        self.assertNotIn("management_portfolio_totals", ledger_text)
        self.assertNotIn("manual_value", ledger_text)
        with self.assertRaisesRegex(ReleasePromotionError, "separate"):
            store.review(
                assessment["assessment_id"],
                runtime_fingerprint=fingerprint,
                actor="multi-portfolio-reviewer-1",
                decision="approved",
                rationale="Attempted release approval by the portfolio evidence reviewer",
                evidence_references=["audit://invalid-release-review"],
            )

        missing = copy.deepcopy(evidence)
        missing["multi_entity_shadow_close_portfolios"] = []
        with self.assertRaisesRegex(ReleasePromotionError, "exact manifests|requires one portfolio"):
            build_stable_promotion_assessment(self.runtime, missing, now=clock)
        tampered = copy.deepcopy(evidence)
        tampered["multi_entity_shadow_close_portfolios"][0]["entity_reports"][0][
            "report_fingerprint"
        ] = "f" * 64
        with self.assertRaisesRegex(ReleasePromotionError, "fingerprint|does not match"):
            build_stable_promotion_assessment(self.runtime, tampered, now=clock)

    @staticmethod
    def _cli(arguments):
        stdout, stderr = StringIO(), StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = cli_main(["--packs", str(ROOT / "packs"), *arguments])
        return code, stdout.getvalue(), stderr.getvalue()


if __name__ == "__main__":
    unittest.main()
