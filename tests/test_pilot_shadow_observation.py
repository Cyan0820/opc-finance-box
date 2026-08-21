from __future__ import annotations

import copy
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import tempfile
import unittest

from src.box_pipeline import dispatch_box_pipeline_request
from src.box_doctor import diagnose_box
from src.box_runtime import BoxRuntime
from src.cli import main as cli_main
from src.multi_entity_shadow_close import (
    assemble_multi_entity_shadow_close_artifact,
    review_multi_entity_shadow_close_artifact,
)
from src.pilot_data_handoff import (
    review_pilot_data_handoff_workpaper,
    write_pilot_data_handoff_workpaper,
)
from src.pilot_readiness import (
    review_pilot_readiness_workpaper,
    write_pilot_readiness_workpaper,
)
from src.pilot_shadow_observation import (
    PilotShadowObservationError,
    assemble_pilot_shadow_observation,
    build_pilot_shadow_observation_status,
    build_pilot_shadow_observation_workspace,
    review_pilot_shadow_observation,
    verify_pilot_shadow_observation,
)
from src.pilot_shadow_run import register_pilot_shadow_run
from src.pipeline_run_store import PipelineRunStore
from src.shadow_close import compare_shadow_close, review_shadow_close


ROOT = Path(__file__).resolve().parents[1]
PACKS = ROOT / "packs"


class PilotShadowObservationTests(unittest.TestCase):
    def runtime(self) -> BoxRuntime:
        return BoxRuntime(
            ROOT / "examples" / "boxes" / "cn_dtc_shopify_stripe_store.json",
            PACKS,
        )

    def build_reviews(
        self, runtime: BoxRuntime, root: Path, *, period: str = "2026-08",
    ) -> tuple[Path, Path]:
        readiness_workpaper = root / "readiness-workpaper.json"
        readiness_review = root / "readiness-review.json"
        write_pilot_readiness_workpaper(
            runtime, readiness_workpaper, period=period,
            prepared_by="readiness-preparer",
        )
        readiness = json.loads(readiness_workpaper.read_text(encoding="utf-8"))
        readiness["operator_principal"] = "readiness-operator"
        for entity in readiness["entities"]:
            for domain in entity["data_domains"]:
                domain.update({
                    "status": "ready",
                    "acquisition_mode": "file_export",
                    "period_coverage": [period],
                    "read_only_confirmed": True,
                    "mapping_approved_by": "readiness-mapping-reviewer",
                    "evidence_references": [
                        f"evidence://readiness/{entity['entity_id']}/{domain['domain']}"
                    ],
                })
        entity_ids = [item["entity_id"] for item in readiness["entities"]]
        for connector in readiness["network_connectors"]:
            connector.update({
                "status": "approved_file_fallback",
                "entity_ids": entity_ids,
                "credential_reference_configured": False,
                "provider_contract_passed": False,
                "bounded_read_window_confirmed": False,
                "checkpoint_owner": "readiness-checkpoint-owner",
                "mapping_approved_by": "readiness-connector-reviewer",
                "evidence_references": ["evidence://readiness/connector/fallback"],
            })
        readiness["shadow_close_plan"].update({
            "planned": True,
            "baseline_owner": "readiness-baseline-owner",
            "evidence_references": ["workpaper://readiness/shadow-plan"],
        })
        readiness_workpaper.write_text(json.dumps(readiness), encoding="utf-8")
        readiness_workpaper.chmod(0o600)
        review_pilot_readiness_workpaper(
            runtime, readiness_workpaper, readiness_review,
            actor="readiness-independent-reviewer",
            rationale="Independent review confirms bounded readiness controls.",
            evidence_references=["advisor://readiness/independent-review"],
        )

        handoff_workpaper = root / "handoff-workpaper.json"
        handoff_review = root / "handoff-review.json"
        write_pilot_data_handoff_workpaper(
            runtime, readiness_review, handoff_workpaper,
            prepared_by="handoff-preparer", custodian_principal="handoff-custodian",
        )
        handoff = json.loads(handoff_workpaper.read_text(encoding="utf-8"))
        for entity in handoff["entities"]:
            for domain in entity["data_domains"]:
                domain.update({
                    "mapped_entity_id": entity["entity_id"],
                    "status": "delivered",
                    "transfer_mode": "local_only",
                    "source_file_count": 1,
                    "source_manifest_sha256": "a" * 64,
                    "period_coverage": [period],
                    "contains_personal_data": "no",
                    "privacy_control": "not_required",
                    "source_owner": "handoff-source-owner",
                    "access_approved_by": "handoff-access-approver",
                    "evidence_references": [
                        f"evidence://handoff/{entity['entity_id']}/{domain['domain']}"
                    ],
                })
        handoff_workpaper.write_text(json.dumps(handoff), encoding="utf-8")
        handoff_workpaper.chmod(0o600)
        review_pilot_data_handoff_workpaper(
            runtime, handoff_workpaper, readiness_review, handoff_review,
            actor="handoff-independent-reviewer",
            rationale="Independent access review confirms controlled source delivery.",
            evidence_references=["advisor://handoff/independent-review"],
        )
        return readiness_review, handoff_review

    def reviewed_month_close(
        self, runtime: BoxRuntime, store: PipelineRunStore,
    ) -> dict:
        request = json.loads(
            (ROOT / "examples" / "pipelines" / "month_close_control_fixture.json")
            .read_text(encoding="utf-8")
        )
        result = dispatch_box_pipeline_request(runtime, request)
        record = store.record(
            runtime.snapshot(), request, result, actor="shadow-operator",
        )
        for gate in record["required_review_gates"]:
            record = store.review(
                record["attempt_id"],
                runtime_fingerprint=runtime.snapshot()["fingerprint"],
                gate=gate, decision="approved", actor="pipeline-independent-reviewer",
                rationale=f"Independent evidence review approved {gate}.",
                evidence_references=[f"evidence://shadow/2026-08/{gate}"],
            )
        return record

    def reviewed_entity_report(
        self, runtime: BoxRuntime, *, classification: str | None = None,
        entity_id: str = "cn_dtc_company", period: str = "2026-08",
        multiplier: int = 1, reviewer: str = "entity-independent-reviewer",
    ) -> dict:
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
            "period": period,
            "key": "1001",
            "name": "Cash",
            "value": 10 * multiplier,
            "absolute_tolerance": 0,
            "percent_tolerance": 0,
        }, *[{
            "domain": "statement",
            "entity_id": entity_id,
            "period": period,
            "key": key,
            "name": key,
            "value": value,
            "absolute_tolerance": 0,
            "percent_tolerance": 0,
        } for key, value in values.items()]]
        baseline = {
            "id": f"SHADOW-{entity_id}-{period}",
            "entity_id": entity_id,
            "period": period,
            "source_fingerprint": str(multiplier) * 64,
            "rows": rows,
        }
        finance_values = dict(values)
        if classification:
            finance_values["IS_REVENUE"] += 1
        finance = {
            "financial_statements": {
                "detail": [{
                    "account": "1001 Cash", "closing_debit": 10 * multiplier,
                    "closing_credit": 0,
                }],
                "balance_sheet": {
                    "assets": finance_values["BS_ASSETS"],
                    "liabilities": finance_values["BS_LIABILITIES"],
                    "liabilities_and_equity": finance_values["BS_ASSETS"],
                },
                "income_statement": {
                    "revenue": finance_values["IS_REVENUE"],
                    "expenses": finance_values["IS_EXPENSES"],
                    "profit_before_tax": finance_values["IS_PROFIT"],
                },
            },
            "tax_pack": {},
        }
        report = compare_shadow_close(
            baseline, finance,
            runtime_fingerprint=runtime.snapshot()["fingerprint"],
        )
        resolutions = []
        decision = "验证通过"
        if classification:
            decision = "接受差异"
            resolutions = [{
                "domain": "statement",
                "key": "IS_REVENUE",
                "classification": classification,
                "rationale": "The revenue difference was traced to bounded source evidence.",
                "evidence_references": [f"audit://{entity_id}/revenue-difference"],
            }]
        review = review_shadow_close(
            report, decision, reviewer,
            "Independent entity evidence was checked against the manual baseline.",
            [f"audit://{entity_id}/shadow-close"], resolutions,
        )
        report["review"] = review
        report["review_current"] = True
        return report

    def prepare_chain(self, root: Path) -> tuple[BoxRuntime, Path, Path, Path, dict]:
        runtime = self.runtime()
        readiness_review, handoff_review = self.build_reviews(runtime, root)
        store = PipelineRunStore(root / "runs")
        record = self.reviewed_month_close(runtime, store)
        registration = root / "shadow-registration.json"
        register_pilot_shadow_run(
            runtime, handoff_review, readiness_review, root / "runs",
            {"cn_dtc_company": record["attempt_id"]}, registration,
            actor="shadow-registrar",
            rationale="Registrar binds the independently reviewed first Shadow Close.",
            evidence_references=["workpaper://shadow/2026-08/registration"],
        )
        return runtime, readiness_review, handoff_review, registration, record

    @staticmethod
    def multi_month_close_result(
        entity_id: str, *, run_id: str, currency: str, multiplier: int,
    ) -> dict:
        revenue = 100 * multiplier
        expenses = 40 * multiplier
        return {
            "pipeline": {
                "pipeline_id": "finance.month_close_control",
                "run_id": run_id,
                "executed_at": "2026-08-14T00:00:00+00:00",
                "required_review_gates": ["month_close_control_review"],
            },
            "ready": True,
            "blocked_at": None,
            "services": {},
            "lineage": {"entity_id": entity_id, "period": "2026-07"},
            "founder_briefing": {
                "entity_id": entity_id,
                "period": "2026-07",
                "close_control_ready_for_review": True,
                "candidate_only": True,
                "currency_summaries": [{
                    "currency": currency,
                    "bank_account_count": 1,
                    "statement_cash_total": 20 * multiplier,
                    "ledger_cash_total": 20 * multiplier,
                    "assets": 50 * multiplier,
                    "liabilities": 10 * multiplier,
                    "revenue": revenue,
                    "expenses": expenses,
                    "profit_before_tax_candidate": revenue - expenses,
                }],
            },
            "blockers": [],
            "external_actions_performed": False,
            "network_access_performed": False,
            "posting_performed": False,
            "period_close_performed": False,
            "retryable": False,
        }

    def prepare_multi_chain(
        self, root: Path,
    ) -> tuple[BoxRuntime, Path, Path, Path, list[dict]]:
        runtime = BoxRuntime(
            ROOT / "examples" / "boxes" / "global_game_studio.json", PACKS,
        )
        readiness, handoff = self.build_reviews(runtime, root, period="2026-07")
        store = PipelineRunStore(root / "runs")
        records = []
        for index, (entity_id, currency) in enumerate(
            (("cn_studio", "CNY"), ("sg_publisher", "USD")), 1,
        ):
            request = {
                "pipeline_id": "finance.month_close_control",
                "payload": {"entity_id": entity_id, "period": "2026-07"},
            }
            result = self.multi_month_close_result(
                entity_id, run_id=("a" if index == 1 else "b") * 24,
                currency=currency, multiplier=index,
            )
            record = store.record(
                runtime.snapshot(), request, result, actor=f"operator-{entity_id}",
            )
            record = store.review(
                record["attempt_id"],
                runtime_fingerprint=runtime.snapshot()["fingerprint"],
                gate="month_close_control_review", decision="approved",
                actor=f"pipeline-reviewer-{entity_id}",
                rationale="Independent entity month-close evidence was approved.",
                evidence_references=[f"review://{entity_id}/2026-07/month-close"],
            )
            records.append(record)
        registration = root / "multi-registration.json"
        register_pilot_shadow_run(
            runtime, handoff, readiness, root / "runs",
            {record["entity_id"]: record["attempt_id"] for record in records},
            registration, actor="multi-shadow-registrar",
            rationale="Registrar binds both reviewed legal-entity Shadow Close runs.",
            evidence_references=["workpaper://shadow/2026-07/multi-registration"],
        )
        return runtime, readiness, handoff, registration, records

    @staticmethod
    def portfolio_result(records: list[dict]) -> dict:
        sources = [{
            "attempt_id": record["attempt_id"],
            "entity_id": record["entity_id"],
            "run_id": record["run_id"],
            "result_fingerprint": record["result_fingerprint"],
            "portfolio_source_fingerprint": record["portfolio_source_fingerprint"],
            "review_complete": True,
        } for record in records]
        entity_ids = sorted(record["entity_id"] for record in records)
        attempts = sorted(record["attempt_id"] for record in records)
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
                "period": "2026-07",
                "entity_ids": entity_ids,
                "source_run_ledger_verified": True,
                "source_attempt_ids": attempts,
            },
            "founder_briefing": {
                "period": "2026-07",
                "entity_count": 2,
                "ready_entity_count": 2,
                "statutory_readiness": [{
                    "entity_id": entity_id,
                    "ready_for_portfolio_review": True,
                } for entity_id in entity_ids],
                "management_portfolio_totals": {
                    "revenue": 300, "expenses": 120,
                    "profit_before_tax_candidate": 180,
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

    @staticmethod
    def write_private(path: Path, value: dict) -> None:
        path.write_text(json.dumps(value), encoding="utf-8")
        path.chmod(0o600)

    def test_clean_observation_assembles_reviews_and_verifies_without_values(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime, readiness, handoff, registration, record = self.prepare_chain(root)
            report_path = root / "entity-reviewed.json"
            self.write_private(report_path, self.reviewed_entity_report(runtime))
            receipt_path = root / "observation.json"
            assembled = assemble_pilot_shadow_observation(
                runtime, registration, handoff, readiness, root / "runs",
                [report_path], receipt_path,
            )
            self.assertEqual(assembled["observation_result_candidate"], "passed")
            self.assertFalse(assembled["raw_financial_values_written_to_output"])
            if os.name != "nt":
                self.assertEqual(receipt_path.stat().st_mode & 0o777, 0o600)
            persisted = receipt_path.read_text(encoding="utf-8")
            for forbidden in ("manual_value", "agent_value", "difference"):
                self.assertNotIn(forbidden, persisted)
            self.assertIn(record["attempt_id"], persisted)

            with self.assertRaisesRegex(PilotShadowObservationError, "registration"):
                review_pilot_shadow_observation(
                    runtime, receipt_path, root / "overlap.json", decision="passed",
                    actor="shadow-registrar",
                    rationale="Registration role overlap must be rejected by this control.",
                    evidence_references=["audit://observation/role-separation"],
                )
            reviewed_path = root / "observation-reviewed.json"
            reviewed = review_pilot_shadow_observation(
                runtime, receipt_path, reviewed_path, decision="passed",
                actor="observation-independent-reviewer",
                rationale="Independent review confirms the exact first observation evidence.",
                evidence_references=["audit://observation/independent-review"],
            )
            self.assertTrue(reviewed["ready_for_next_shadow_period"])
            verified = verify_pilot_shadow_observation(
                runtime, reviewed_path, registration, handoff, readiness,
                root / "runs", [report_path],
            )
            self.assertTrue(verified["valid"])
            self.assertTrue(verified["ready_for_next_shadow_period"])
            self.assertFalse(verified["ready_for_stable_promotion"])
            self.assertFalse(verified["period_close_authorized"])
            safe = json.dumps(verified)
            self.assertNotIn(record["attempt_id"], safe)
            self.assertNotIn("shadow-registrar", safe)
            self.assertNotIn("manual_value", safe)
            status = build_pilot_shadow_observation_status(
                runtime, reviewed_path, registration, handoff, readiness,
                root / "runs", [report_path],
            )
            self.assertEqual(status["status"], "current")
            self.assertTrue(status["ready_for_next_shadow_period"])
            self.assertNotIn(record["attempt_id"], json.dumps(status))
            report_dir = root / "entity-reports"
            report_dir.mkdir()
            self.write_private(
                report_dir / "cn_dtc_company.json",
                json.loads(report_path.read_text(encoding="utf-8")),
            )
            workspace = build_pilot_shadow_observation_workspace(
                runtime, reviewed_path, registration, handoff, readiness,
                root / "runs", report_dir,
            )
            self.assertEqual(workspace["summary"]["activation_status"], "current")
            self.assertTrue(workspace["summary"]["ready_for_next_shadow_period"])
            self.assertEqual(workspace["summary"]["reviewed_entity_count"], 1)
            self.assertNotIn(record["attempt_id"], json.dumps(workspace))
            doctor = diagnose_box(
                runtime,
                python_version=(3, 11, 0),
                dependency_probe={"openpyxl": True, "pypdf": True},
                executable_probe={"tesseract": True, "pdftoppm": True},
                pilot_readiness_review=readiness,
                pilot_data_handoff_review=handoff,
                pilot_shadow_run_registration=registration,
                pilot_shadow_observation_review=reviewed_path,
                pilot_shadow_entity_reports=[report_path],
                pipeline_runs_root=root / "runs",
            )
            observation_check = next(
                item for item in doctor["checks"]
                if item["check_id"] == "pilot.first_shadow_observation_review"
            )
            self.assertEqual(observation_check["status"], "pass")
            self.assertTrue(doctor["ready_for_next_shadow_period"])
            self.assertNotIn(record["attempt_id"], json.dumps(doctor))

    def test_source_tamper_and_later_pipeline_rejection_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime, readiness, handoff, registration, record = self.prepare_chain(root)
            report_path = root / "entity-reviewed.json"
            report = self.reviewed_entity_report(runtime)
            self.write_private(report_path, report)
            receipt = root / "receipt.json"
            reviewed = root / "reviewed.json"
            assemble_pilot_shadow_observation(
                runtime, registration, handoff, readiness, root / "runs",
                [report_path], receipt,
            )
            review_pilot_shadow_observation(
                runtime, receipt, reviewed, decision="passed",
                actor="observation-independent-reviewer",
                rationale="Independent review confirms the exact first observation evidence.",
                evidence_references=["audit://observation/independent-review"],
            )
            tampered = copy.deepcopy(report)
            tampered["review"]["rationale"] += " tampered"
            self.write_private(report_path, tampered)
            with self.assertRaisesRegex(PilotShadowObservationError, "no longer matches"):
                verify_pilot_shadow_observation(
                    runtime, reviewed, registration, handoff, readiness,
                    root / "runs", [report_path],
                )
            self.write_private(report_path, report)
            PipelineRunStore(root / "runs").review(
                record["attempt_id"],
                runtime_fingerprint=runtime.snapshot()["fingerprint"],
                gate=record["required_review_gates"][0], decision="rejected",
                actor="pipeline-independent-reviewer",
                rationale="Later source evidence invalidated the original approval.",
            )
            with self.assertRaisesRegex(PilotShadowObservationError, "fully reviewed"):
                verify_pilot_shadow_observation(
                    runtime, reviewed, registration, handoff, readiness,
                    root / "runs", [report_path],
                )

    def test_system_defect_requires_correction_and_blocks_next_period(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime, readiness, handoff, registration, _ = self.prepare_chain(root)
            report_path = root / "entity-reviewed.json"
            self.write_private(
                report_path,
                self.reviewed_entity_report(runtime, classification="system_defect"),
            )
            receipt = root / "receipt.json"
            assembled = assemble_pilot_shadow_observation(
                runtime, registration, handoff, readiness, root / "runs",
                [report_path], receipt,
            )
            self.assertEqual(assembled["observation_result_candidate"], "needs_correction")
            with self.assertRaisesRegex(PilotShadowObservationError, "accepted"):
                review_pilot_shadow_observation(
                    runtime, receipt, root / "wrong-review.json",
                    decision="accepted-differences",
                    actor="observation-independent-reviewer",
                    rationale="A system defect cannot be accepted for the next period.",
                    evidence_references=["audit://observation/system-defect"],
                )
            reviewed = root / "reviewed.json"
            result = review_pilot_shadow_observation(
                runtime, receipt, reviewed, decision="needs-correction",
                actor="observation-independent-reviewer",
                rationale="The identified system defect must be corrected and rerun.",
                evidence_references=["audit://observation/system-defect"],
            )
            self.assertFalse(result["ready_for_next_shadow_period"])
            verified = verify_pilot_shadow_observation(
                runtime, reviewed, registration, handoff, readiness,
                root / "runs", [report_path],
            )
            self.assertEqual(verified["system_defect_count"], 1)
            self.assertFalse(verified["ready_for_next_shadow_period"])

    def test_multi_entity_observation_binds_portfolio_to_registered_attempts(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime, readiness, handoff, registration, records = (
                self.prepare_multi_chain(root)
            )
            entity_paths = []
            for index, entity_id in enumerate(("cn_studio", "sg_publisher"), 1):
                path = root / f"{entity_id}-reviewed.json"
                self.write_private(path, self.reviewed_entity_report(
                    runtime, entity_id=entity_id, period="2026-07", multiplier=index,
                    reviewer=f"entity-reviewer-{index}",
                ))
                entity_paths.append(path)
            portfolio_result = root / "portfolio-result.json"
            self.write_private(portfolio_result, self.portfolio_result(records))
            portfolio_manifest = root / "portfolio-manifest.json"
            assemble_multi_entity_shadow_close_artifact(
                runtime, entity_paths, portfolio_result, portfolio_manifest,
            )
            portfolio_review = root / "portfolio-reviewed.json"
            review_multi_entity_shadow_close_artifact(
                runtime, portfolio_manifest, portfolio_review,
                decision="passed", actor="portfolio-independent-reviewer",
                rationale="Independent review confirms the exact management portfolio scope.",
                evidence_references=["audit://portfolio/independent-review"],
            )
            receipt = root / "observation.json"
            assembled = assemble_pilot_shadow_observation(
                runtime, registration, handoff, readiness, root / "runs",
                entity_paths, receipt, portfolio_review_path=portfolio_review,
            )
            self.assertEqual(assembled["entity_count"], 2)
            self.assertEqual(assembled["observation_result_candidate"], "passed")
            with self.assertRaisesRegex(PilotShadowObservationError, "portfolio"):
                review_pilot_shadow_observation(
                    runtime, receipt, root / "overlap.json", decision="passed",
                    actor="portfolio-independent-reviewer",
                    rationale="Portfolio role overlap must be rejected by this control.",
                    evidence_references=["audit://observation/portfolio-overlap"],
                )
            reviewed = root / "observation-reviewed.json"
            review_pilot_shadow_observation(
                runtime, receipt, reviewed, decision="passed",
                actor="observation-independent-reviewer",
                rationale="Fourth-role review confirms registration and portfolio lineage.",
                evidence_references=["audit://observation/multi-entity-review"],
            )
            verified = verify_pilot_shadow_observation(
                runtime, reviewed, registration, handoff, readiness, root / "runs",
                entity_paths, portfolio_review_path=portfolio_review,
            )
            self.assertTrue(verified["valid"])
            self.assertEqual(verified["entity_count"], 2)
            self.assertFalse(verified["source_attempt_ids_returned"])

            manifest = json.loads(portfolio_review.read_text(encoding="utf-8"))
            manifest["portfolio"]["source_attempt_ids"][0] = "f" * 24
            self.write_private(portfolio_review, manifest)
            with self.assertRaisesRegex(PilotShadowObservationError, "fingerprint|attempt"):
                verify_pilot_shadow_observation(
                    runtime, reviewed, registration, handoff, readiness,
                    root / "runs", entity_paths,
                    portfolio_review_path=portfolio_review,
                )

    def test_cli_runs_complete_safe_observation_chain(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime, readiness, handoff, registration, record = self.prepare_chain(root)
            config = ROOT / "examples" / "boxes" / "cn_dtc_shopify_stripe_store.json"
            report = root / "entity-reviewed.json"
            self.write_private(report, self.reviewed_entity_report(runtime))
            receipt = root / "receipt.json"
            stdout, stderr = StringIO(), StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = cli_main([
                    "pilot-shadow-observation-assemble", str(config),
                    str(registration), str(handoff), str(readiness),
                    "--runs-root", str(root / "runs"),
                    "--entity-report", str(report), "--output", str(receipt),
                ])
            self.assertEqual(code, 0, stderr.getvalue())
            assembled_output = json.loads(stdout.getvalue())
            self.assertTrue(assembled_output["result"]["output_written"])
            self.assertNotIn(record["attempt_id"], stdout.getvalue())

            reviewed = root / "reviewed.json"
            stdout, stderr = StringIO(), StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = cli_main([
                    "pilot-shadow-observation-review", str(config), str(receipt),
                    "--decision", "passed", "--actor", "cli-observation-reviewer",
                    "--rationale", "CLI reviewer confirms the exact observation evidence.",
                    "--evidence-reference", "audit://observation/cli-review",
                    "--output", str(reviewed),
                ])
            self.assertEqual(code, 0, stderr.getvalue())
            self.assertTrue(json.loads(stdout.getvalue())["result"]["review_current"])

            stdout, stderr = StringIO(), StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = cli_main([
                    "pilot-shadow-observation-verify", str(config), str(reviewed),
                    str(registration), str(handoff), str(readiness),
                    "--runs-root", str(root / "runs"),
                    "--entity-report", str(report),
                ])
            self.assertEqual(code, 0, stderr.getvalue())
            verified_output = json.loads(stdout.getvalue())
            self.assertTrue(verified_output["result"]["valid"])
            self.assertFalse(verified_output["result"]["actors_returned"])
            self.assertNotIn(record["attempt_id"], stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
