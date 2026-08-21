from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import shlex
import stat
import tempfile
import unittest
from unittest.mock import patch

from src.activation_workspace import (
    initialize_activation_workspace,
    verify_activation_workspace,
)
from src.box_runtime import BoxRuntime
from src.cli import build_parser, main as cli_main
from src.pilot_shadow_next_period import (
    COMMANDS_NAME,
    DIRECTORIES,
    ENV_NAME,
    MANIFEST_NAME,
    PERIOD_WORKSPACE_DIRECTORY,
    PilotShadowNextPeriodError,
    initialize_next_pilot_shadow_period,
    verify_next_pilot_shadow_period,
)
from src.pilot_shadow_period_tasks import project_period_operator_tasks
from src.pilot_shadow_series import (
    archive_pilot_shadow_period,
    inspect_pilot_shadow_period_archive,
)
from tests import test_pilot_shadow_series as series_helpers


ROOT = Path(__file__).resolve().parents[1]
PACKS = ROOT / "packs"
SINGLE_BOX = ROOT / "examples" / "boxes" / "cn_dtc_shopify_stripe_store.json"
MULTI_BOX = ROOT / "examples" / "boxes" / "global_game_studio.json"


class PilotShadowNextPeriodTests(unittest.TestCase):
    def setUp(self) -> None:
        self.series = series_helpers.PilotShadowSeriesTests(methodName="runTest")
        self.series.setUp()

    def initialize_activation(
        self,
        parent: Path,
        runtime: BoxRuntime,
        config: Path,
        *,
        period: str = "2026-08",
    ) -> Path:
        activation = parent / "private-activation"
        initialize_activation_workspace(
            runtime,
            config,
            activation,
            period=period,
            facts_as_of="2026-08-14",
            prepared_by="activation-preparer",
        )
        return activation

    def archive_single(
        self,
        root: Path,
        runtime: BoxRuntime,
        activation: Path,
        *,
        classification: str | None = None,
    ) -> dict:
        source_root = root / "single-source"
        self.series.build_period(
            root,
            source_root,
            activation / "pipeline-runs",
            "2026-08",
            multiplier=1,
            classification=classification,
            runtime=runtime,
        )
        source = source_root / "2026-08"
        return archive_pilot_shadow_period(
            runtime,
            source / "reviewed-observation.json",
            source / "shadow-run-registration.json",
            source / "data-handoff-review.json",
            source / "pilot-readiness-review.json",
            activation / "pipeline-runs",
            [source / "entity-reports" / "cn_dtc_company.json"],
            activation / "pilot" / "series-periods",
        )

    def archive_multi(
        self,
        root: Path,
        runtime: BoxRuntime,
        activation: Path,
    ) -> None:
        source_root = root / "multi-source"
        self.series.build_multi_period(
            runtime,
            root,
            source_root,
            activation / "pipeline-runs",
            "2026-08",
            period_index=1,
        )
        source = source_root / "2026-08"
        archive_pilot_shadow_period(
            runtime,
            source / "reviewed-observation.json",
            source / "shadow-run-registration.json",
            source / "data-handoff-review.json",
            source / "pilot-readiness-review.json",
            activation / "pipeline-runs",
            [
                source / "entity-reports" / "cn_studio.json",
                source / "entity-reports" / "sg_publisher.json",
            ],
            activation / "pilot" / "series-periods",
            portfolio_review_path=source / "portfolio-review.json",
        )

    def assert_private_tree(self, root: Path) -> None:
        if os.name == "nt":
            return
        self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
        for path in root.rglob("*"):
            expected = 0o700 if path.is_dir() else 0o600
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), expected)

    def test_single_entity_next_period_is_exact_private_and_cli_parseable(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            runtime = BoxRuntime(SINGLE_BOX, PACKS)
            activation = self.initialize_activation(root, runtime, SINGLE_BOX)
            archived = self.archive_single(root, runtime, activation)
            self.assertTrue(archived["ready_for_next_shadow_period"])
            archive_status = inspect_pilot_shadow_period_archive(
                runtime,
                activation / "pilot" / "series-periods",
                activation / "pipeline-runs",
                as_of="2026-09-15",
            )
            self.assertEqual(archive_status["period_count"], 1)
            self.assertTrue(archive_status["ready_to_initialize_next_period"])
            self.assertFalse(archive_status["source_artifact_hashes_returned"])

            result = initialize_next_pilot_shadow_period(
                runtime,
                SINGLE_BOX,
                activation,
                prepared_by="period-preparer",
                facts_as_of="2026-09-15",
            )
            self.assertTrue(result["initialized"])
            self.assertEqual(result["previous_period"], "2026-08")
            self.assertEqual(result["period"], "2026-09")
            self.assertEqual(result["entity_count"], 1)
            self.assertFalse(result["multi_entity"])
            self.assertFalse(result["output_path_returned"])
            self.assertFalse(result["previous_archive_hashes_returned"])
            self.assertNotIn(str(root), json.dumps(result))

            period_root = activation / PERIOD_WORKSPACE_DIRECTORY / "2026-09"
            self.assertEqual(
                {item.name for item in period_root.iterdir()},
                {*DIRECTORIES, MANIFEST_NAME, COMMANDS_NAME, ENV_NAME},
            )
            self.assert_private_tree(period_root)
            readiness = json.loads(
                (period_root / "readiness" / "workpaper.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(readiness["period"], "2026-09")
            self.assertTrue(readiness["template_only"])
            commands = json.loads(
                (period_root / COMMANDS_NAME).read_text(encoding="utf-8")
            )
            command_names = []
            parser = build_parser()
            for step in commands["steps"]:
                if step["action"] != "run_cli":
                    continue
                self.assertEqual(step["shell_preview"], shlex.join(step["argv"]))
                parsed = parser.parse_args(step["argv"][1:])
                command_names.append(parsed.command)
            self.assertIn("pilot-shadow-period-archive", command_names)
            self.assertIn("pilot-shadow-next-period-verify", command_names)
            self.assertNotIn("shadow-close-portfolio-review", command_names)

            verified = verify_next_pilot_shadow_period(
                runtime, activation, "2026-09", as_of="2026-09-15",
            )
            self.assertTrue(verified["valid"])
            self.assertTrue(verified["prior_archive_binding_current"])
            self.assertFalse(verified["paths_returned"])
            self.assertFalse(verified["financial_values_returned"])

            source_root = root / "single-source"
            self.series.build_period(
                root,
                source_root,
                activation / "pipeline-runs",
                "2026-09",
                multiplier=2,
                runtime=runtime,
            )
            source = source_root / "2026-09"
            archive_pilot_shadow_period(
                runtime,
                source / "reviewed-observation.json",
                source / "shadow-run-registration.json",
                source / "data-handoff-review.json",
                source / "pilot-readiness-review.json",
                activation / "pipeline-runs",
                [source / "entity-reports" / "cn_dtc_company.json"],
                activation / "pilot" / "series-periods",
            )
            historical = verify_next_pilot_shadow_period(
                runtime, activation, "2026-09", as_of="2026-10-15",
            )
            self.assertTrue(historical["prior_archive_binding_current"])
            self.assertTrue(verify_activation_workspace(runtime, activation)["valid"])

    def test_multi_entity_next_period_has_exact_entity_and_portfolio_chain(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            runtime = BoxRuntime(MULTI_BOX, PACKS)
            activation = self.initialize_activation(root, runtime, MULTI_BOX)
            self.archive_multi(root, runtime, activation)
            result = initialize_next_pilot_shadow_period(
                runtime,
                MULTI_BOX,
                activation,
                prepared_by="multi-period-preparer",
                facts_as_of="2026-09-15",
            )
            self.assertTrue(result["multi_entity"])
            self.assertEqual(result["entity_count"], 2)
            period_root = activation / PERIOD_WORKSPACE_DIRECTORY / "2026-09"
            commands = json.loads(
                (period_root / COMMANDS_NAME).read_text(encoding="utf-8")
            )
            by_id = {item["step_id"]: item for item in commands["steps"]}
            register = by_id["shadow-run-register"]["argv"]
            self.assertEqual(register.count("--entity-attempt"), 2)
            archive = by_id["shadow-period-archive"]["argv"]
            self.assertEqual(archive.count("--entity-report"), 2)
            self.assertEqual(archive.count("--portfolio-review"), 1)
            self.assertIn("shadow-portfolio-assemble", by_id)
            self.assertIn("shadow-portfolio-review", by_id)
            self.assertIn("shadow-portfolio-verify", by_id)
            tasks = project_period_operator_tasks([
                {
                    "step_id": item["step_id"],
                    "action": item["action"],
                    "reported_outcome": "not_reported",
                    "event_count": 0,
                }
                for item in commands["steps"]
            ], entity_ids=runtime.entities.ids())
            self.assertEqual(len(tasks), len(commands["steps"]))
            self.assertEqual(sum(item["actionable_now"] for item in tasks), 1)
            for task in tasks:
                self.assertEqual(task["guidance_version"], 1)
                self.assertTrue(task["work_product_type_id"])
                self.assertTrue(task["operator_checklist_type_ids"])
                self.assertTrue(task["stop_condition_type_ids"])
                self.assertTrue(task["authoritative_verifier_required"])
            portfolio = {
                item["step_id"]: item for item in tasks
                if item["step_id"].startswith("shadow-portfolio-")
            }
            self.assertEqual(
                portfolio["shadow-portfolio-review"]["responsible_role"],
                "portfolio_reviewer",
            )
            self.assertFalse(any(item["command_returned"] for item in tasks))
            verified = verify_next_pilot_shadow_period(
                runtime, activation, "2026-09", as_of="2026-09-15",
            )
            self.assertTrue(verified["multi_entity"])
            self.assertEqual(verified["entity_count"], 2)
            self.assertTrue(verified["prior_archive_reverified"])
            self.assert_private_tree(period_root)

    def test_refuses_missing_blocked_duplicate_tampered_and_failed_writes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            runtime = BoxRuntime(SINGLE_BOX, PACKS)
            activation = self.initialize_activation(root, runtime, SINGLE_BOX)
            with self.assertRaisesRegex(
                Exception, "requires 1-24 archived periods",
            ):
                initialize_next_pilot_shadow_period(
                    runtime,
                    SINGLE_BOX,
                    activation,
                    prepared_by="period-preparer",
                    facts_as_of="2026-09-15",
                )

            blocked_root = root / "blocked-case"
            blocked_root.mkdir(mode=0o700)
            blocked_root.chmod(0o700)
            blocked_activation = self.initialize_activation(
                blocked_root, runtime, SINGLE_BOX,
            )
            blocked = self.archive_single(
                blocked_root,
                runtime,
                blocked_activation,
                classification="system_defect",
            )
            self.assertFalse(blocked["ready_for_next_shadow_period"])
            with self.assertRaisesRegex(
                PilotShadowNextPeriodError, "not ready for the next period",
            ):
                initialize_next_pilot_shadow_period(
                    runtime,
                    SINGLE_BOX,
                    blocked_activation,
                    prepared_by="period-preparer",
                    facts_as_of="2026-09-15",
                )

            self.archive_single(root, runtime, activation)
            initialize_next_pilot_shadow_period(
                runtime,
                SINGLE_BOX,
                activation,
                prepared_by="period-preparer",
                facts_as_of="2026-09-15",
            )
            sentinel = (
                activation / PERIOD_WORKSPACE_DIRECTORY / "2026-09" / MANIFEST_NAME
            ).read_bytes()
            with self.assertRaisesRegex(
                PilotShadowNextPeriodError, "already exists",
            ):
                initialize_next_pilot_shadow_period(
                    runtime,
                    SINGLE_BOX,
                    activation,
                    prepared_by="period-preparer",
                    facts_as_of="2026-09-15",
                )
            self.assertEqual(
                (
                    activation
                    / PERIOD_WORKSPACE_DIRECTORY
                    / "2026-09"
                    / MANIFEST_NAME
                ).read_bytes(),
                sentinel,
            )
            prior = (
                activation
                / "pilot"
                / "series-periods"
                / "2026-08"
                / "reviewed-observation.json"
            )
            value = json.loads(prior.read_text(encoding="utf-8"))
            value["unexpected"] = True
            prior.write_text(json.dumps(value), encoding="utf-8")
            prior.chmod(0o600)
            with self.assertRaises(Exception):
                verify_next_pilot_shadow_period(
                    runtime, activation, "2026-09", as_of="2026-09-15",
                )

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            runtime = BoxRuntime(SINGLE_BOX, PACKS)
            activation = self.initialize_activation(root, runtime, SINGLE_BOX)
            self.archive_single(root, runtime, activation)
            with patch(
                "src.pilot_shadow_next_period._write_exclusive",
                side_effect=OSError("simulated next-period write failure"),
            ):
                with self.assertRaisesRegex(OSError, "simulated"):
                    initialize_next_pilot_shadow_period(
                        runtime,
                        SINGLE_BOX,
                        activation,
                        prepared_by="period-preparer",
                        facts_as_of="2026-09-15",
                    )
            self.assertFalse(
                (activation / PERIOD_WORKSPACE_DIRECTORY / "2026-09").exists()
            )
            self.assertFalse(
                (activation / PERIOD_WORKSPACE_DIRECTORY).exists()
            )

    def test_cli_initializes_and_verifies_without_returning_private_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            runtime = BoxRuntime(SINGLE_BOX, PACKS)
            activation = self.initialize_activation(root, runtime, SINGLE_BOX)
            self.archive_single(root, runtime, activation)
            stdout = StringIO()
            stderr = StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = cli_main([
                    "pilot-shadow-next-period-init",
                    str(SINGLE_BOX),
                    str(activation),
                    "--prepared-by", "period-preparer",
                    "--facts-as-of", "2026-09-15",
                ])
            self.assertEqual(exit_code, 0, stderr.getvalue())
            result = json.loads(stdout.getvalue())["result"]
            self.assertEqual(result["period"], "2026-09")
            self.assertNotIn(str(root), stdout.getvalue())
            stdout = StringIO()
            stderr = StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = cli_main([
                    "pilot-shadow-next-period-verify",
                    str(SINGLE_BOX),
                    str(activation),
                    "2026-09",
                    "--as-of", "2026-09-15",
                ])
            self.assertEqual(exit_code, 0, stderr.getvalue())
            verified = json.loads(stdout.getvalue())["result"]
            self.assertTrue(verified["valid"])
            self.assertFalse(verified["paths_returned"])
            self.assertNotIn(str(root), stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
