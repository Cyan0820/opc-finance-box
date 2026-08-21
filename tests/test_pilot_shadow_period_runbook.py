from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import unittest

from src.activation_runbook import ActivationRunbookError
from src.activation_workspace import build_initialized_activation_status
from src.box_runtime import BoxRuntime
from src.cli import main as cli_main
from src.default_services import build_default_service_registry
from src.pilot_shadow_next_period import (
    PERIOD_WORKSPACE_DIRECTORY,
    initialize_next_pilot_shadow_period,
    verify_next_pilot_shadow_period,
)
from src.pilot_shadow_period_runbook import PilotShadowPeriodRunbookStore
from src.pilot_shadow_series import archive_pilot_shadow_period
from tests import test_pilot_shadow_next_period as next_period_helpers


ROOT = Path(__file__).resolve().parents[1]
PACKS = ROOT / "packs"
CONFIG = ROOT / "examples" / "boxes" / "cn_dtc_shopify_stripe_store.json"


class PilotShadowPeriodRunbookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.runtime = BoxRuntime(CONFIG, PACKS)
        self.helper = next_period_helpers.PilotShadowNextPeriodTests(
            methodName="runTest"
        )
        self.helper.setUp()
        self.activation = self.helper.initialize_activation(
            self.root, self.runtime, CONFIG,
        )
        self.helper.archive_single(self.root, self.runtime, self.activation)
        initialize_next_pilot_shadow_period(
            self.runtime,
            CONFIG,
            self.activation,
            prepared_by="period-preparer",
            facts_as_of="2026-09-15",
        )
        self.store = PilotShadowPeriodRunbookStore(
            self.activation, "2026-09",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def append_and_initialize_october(self) -> PilotShadowPeriodRunbookStore:
        source_root = self.root / "single-source"
        self.helper.series.build_period(
            self.root,
            source_root,
            self.activation / "pipeline-runs",
            "2026-09",
            multiplier=2,
            runtime=self.runtime,
        )
        source = source_root / "2026-09"
        archive_pilot_shadow_period(
            self.runtime,
            source / "reviewed-observation.json",
            source / "shadow-run-registration.json",
            source / "data-handoff-review.json",
            source / "pilot-readiness-review.json",
            self.activation / "pipeline-runs",
            [source / "entity-reports" / "cn_dtc_company.json"],
            self.activation / "pilot" / "series-periods",
        )
        initialize_next_pilot_shadow_period(
            self.runtime,
            CONFIG,
            self.activation,
            prepared_by="october-period-preparer",
            facts_as_of="2026-10-15",
        )
        return PilotShadowPeriodRunbookStore(self.activation, "2026-10")

    def test_empty_manual_and_cli_progress_are_resumable_but_not_authoritative(self):
        initial = self.store.status(self.runtime)
        self.assertEqual(initial["artifact_type"], "pilot_shadow_period_runbook_status")
        self.assertEqual(initial["period"], "2026-09")
        self.assertEqual(initial["event_count"], 0)
        self.assertEqual(initial["chain_head"], "GENESIS")
        self.assertEqual(
            initial["next_reported_progress_step_id"],
            "pilot-readiness-complete",
        )
        self.assertFalse(initial["authoritative_completion_inferred"])
        self.assertFalse(initial["authoritative_period_completion_inferred"])
        self.assertFalse(initial["evidence_gates_unlocked"])

        first = self.store.record(
            self.runtime,
            step_id="pilot-readiness-complete",
            outcome="reported_complete",
            actor="period-preparer",
            rationale="Current-period readiness was filled; verifier remains authoritative.",
            evidence_references=["private://period/2026-09/readiness-checkpoint"],
        )
        self.assertEqual(first["period"], "2026-09")
        self.assertFalse(first["authoritative_completion"])
        self.assertFalse(first["actor_returned"])
        second = self.store.record(
            self.runtime,
            step_id="pilot-readiness-review",
            outcome="reported_complete",
            observed_exit_code=0,
            actor="period-review-runner",
            rationale="CLI returned zero; the reviewed artifact still requires verification.",
        )
        self.assertEqual(second["sequence"], 2)
        status = self.store.status(self.runtime)
        self.assertEqual(status["reported_complete_count"], 2)
        self.assertEqual(
            status["next_reported_progress_step_id"],
            "pilot-readiness-verify",
        )
        self.assertFalse(status["actors_returned"])
        self.assertFalse(status["evidence_references_returned"])
        self.assertFalse(status["private_paths_returned"])
        verified = self.store.verify(self.runtime)
        self.assertTrue(verified["valid"])
        self.assertEqual(verified["event_count"], 2)
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(self.store.root.stat().st_mode), 0o700)
            self.assertEqual(
                stat.S_IMODE(self.store.events_file.stat().st_mode), 0o600,
            )
            self.assertEqual(
                stat.S_IMODE(self.store.lock_file.stat().st_mode), 0o600,
            )

    def test_exit_codes_unknown_steps_and_authoritative_state_fail_closed(self):
        with self.assertRaisesRegex(ActivationRunbookError, "exit code 0"):
            self.store.record(
                self.runtime,
                step_id="pilot-readiness-review",
                outcome="reported_complete",
                observed_exit_code=2,
                actor="runner",
                rationale="A nonzero command cannot be reported as complete.",
            )
        with self.assertRaisesRegex(ActivationRunbookError, "exit code 1-255"):
            self.store.record(
                self.runtime,
                step_id="pilot-readiness-review",
                outcome="reported_failed",
                observed_exit_code=0,
                actor="runner",
                rationale="A zero command cannot be reported as failed.",
            )
        with self.assertRaisesRegex(ActivationRunbookError, "manual.*exit code"):
            self.store.record(
                self.runtime,
                step_id="pilot-readiness-complete",
                outcome="reported_complete",
                observed_exit_code=0,
                actor="runner",
                rationale="A manual edit cannot claim a command exit code.",
            )
        with self.assertRaisesRegex(ActivationRunbookError, "step_id"):
            self.store.record(
                self.runtime,
                step_id="unknown-monthly-step",
                outcome="blocked",
                actor="runner",
                rationale="An unknown step must not enter the monthly ledger.",
            )
        self.assertFalse(self.store.events_file.exists())

        services = build_default_service_registry()
        before = build_initialized_activation_status(
            self.runtime, services, self.activation, as_of="2026-09-15",
        )["activation"]["summary"]
        self.store.record(
            self.runtime,
            step_id="pilot-readiness-complete",
            outcome="reported_complete",
            actor="period-preparer",
            rationale="Operator progress only; no authoritative readiness is asserted.",
        )
        after = build_initialized_activation_status(
            self.runtime, services, self.activation, as_of="2026-09-15",
        )["activation"]["summary"]
        self.assertEqual(after["current_wave_stage_ids"], before["current_wave_stage_ids"])
        self.assertEqual(after["completed_stage_count"], before["completed_stage_count"])
        self.assertTrue(
            verify_next_pilot_shadow_period(
                self.runtime, self.activation, "2026-09",
            )["valid"]
        )

    def test_concurrent_append_keeps_one_period_bound_hash_chain(self):
        def record(index: int):
            return self.store.record(
                self.runtime,
                step_id="pilot-readiness-complete",
                outcome="blocked" if index % 2 == 0 else "deferred",
                actor=f"period-operator-{index}",
                rationale=f"Concurrent monthly progress event {index} remains non-authoritative.",
            )

        with ThreadPoolExecutor(max_workers=8) as executor:
            events = list(executor.map(record, range(24)))
        self.assertEqual(
            {item["sequence"] for item in events}, set(range(1, 25)),
        )
        verified = self.store.verify(self.runtime)
        self.assertEqual(verified["event_count"], 24)
        lines = self.store.events_file.read_text(encoding="utf-8").splitlines()
        self.assertEqual(
            [json.loads(line)["sequence"] for line in lines],
            list(range(1, 25)),
        )
        self.assertTrue(all(
            json.loads(line)["event_type"]
            == "PILOT_SHADOW_PERIOD_STEP_PROGRESS_REPORTED"
            for line in lines
        ))

    def test_tamper_other_box_and_cross_period_copy_are_rejected(self):
        self.store.record(
            self.runtime,
            step_id="pilot-readiness-complete",
            outcome="blocked",
            actor="period-operator",
            rationale="Waiting for current-period source mapping evidence.",
        )
        other_runtime = BoxRuntime(
            ROOT / "examples" / "boxes" / "cn_marketplace_store.json", PACKS,
        )
        with self.assertRaises(Exception):
            self.store.status(other_runtime)

        october = self.append_and_initialize_october()
        october.events_file.write_bytes(self.store.events_file.read_bytes())
        october.events_file.chmod(0o600)
        with self.assertRaisesRegex(
            ActivationRunbookError, "command contract",
        ):
            october.status(self.runtime)
        october.events_file.unlink()
        self.assertEqual(october.status(self.runtime)["event_count"], 0)
        self.assertTrue(self.store.verify(self.runtime)["valid"])

        event = json.loads(self.store.events_file.read_text(encoding="utf-8"))
        event["outcome"] = "reported_complete"
        self.store.events_file.write_text(json.dumps(event) + "\n", encoding="utf-8")
        self.store.events_file.chmod(0o600)
        with self.assertRaisesRegex(ActivationRunbookError, "hash mismatch"):
            self.store.verify(self.runtime)

    def test_cli_record_status_verify_do_not_return_actor_evidence_or_paths(self):
        stdout = StringIO()
        stderr = StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = cli_main([
                "pilot-shadow-period-runbook-record",
                str(CONFIG),
                str(self.activation),
                "2026-09",
                "pilot-readiness-complete",
                "--outcome", "reported-complete",
                "--actor", "secret-period-actor",
                "--rationale", "Private readiness edit was reported complete only.",
                "--evidence-reference", "private://secret/monthly/checkpoint",
            ])
        self.assertEqual(exit_code, 0, stderr.getvalue())
        self.assertNotIn("secret-period-actor", stdout.getvalue())
        self.assertNotIn("private://secret", stdout.getvalue())
        self.assertNotIn(str(self.activation), stdout.getvalue())
        for command in (
            "pilot-shadow-period-runbook-status",
            "pilot-shadow-period-runbook-verify",
        ):
            stdout = StringIO()
            stderr = StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = cli_main([
                    command,
                    str(CONFIG),
                    str(self.activation),
                    "2026-09",
                ])
            self.assertEqual(exit_code, 0, stderr.getvalue())
            result = json.loads(stdout.getvalue())["result"]
            self.assertEqual(result["period"], "2026-09")
            self.assertNotIn("secret-period-actor", stdout.getvalue())
            self.assertNotIn("private://secret", stdout.getvalue())
            self.assertNotIn(str(self.activation), stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
