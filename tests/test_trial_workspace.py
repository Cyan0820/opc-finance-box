from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.runtime_storage import inspect_runtime_data
from src.trial_workspace import (
    GUIDE_NAME,
    MANIFEST_NAME,
    TrialWorkspaceError,
    build_trial_onboarding_plan,
    initialize_trial_workspace,
    run_trial_workbench,
    verify_trial_workspace,
)


ROOT = Path(__file__).resolve().parents[1]
PACKS = ROOT / "packs"


class TrialWorkspaceTests(unittest.TestCase):
    def test_three_profiles_initialize_as_private_verified_runnable_demo_workspaces(self):
        cases = (
            ("game", "CN", "xero"),
            ("dtc", "NL", "shopify_stripe"),
            ("marketplace", "US", "amazon_seller"),
        )
        with tempfile.TemporaryDirectory() as temp_name:
            base = Path(temp_name).resolve()
            for profile, country, integration in cases:
                destination = base / f"{profile}-{country.lower()}"
                result = initialize_trial_workspace(
                    profile=profile,
                    country=country,
                    packs_root=PACKS,
                    destination_root=destination,
                    actor="trial-founder",
                    integrations=[integration],
                )
                self.assertTrue(result["initialized"])
                self.assertTrue(result["workspace_verified"])
                self.assertTrue(result["ready_to_run_locally"])
                self.assertTrue(result["box_workspace_immutable"])
                self.assertTrue(result["runtime_data_separate"])
                self.assertFalse(result["credentials_persisted"])
                self.assertFalse(result["destination_path_returned"])
                self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o700)
                self.assertEqual(
                    {item.name for item in destination.iterdir()},
                    {"box", "runtime-data", GUIDE_NAME, MANIFEST_NAME},
                )
                self.assertEqual(
                    stat.S_IMODE((destination / MANIFEST_NAME).stat().st_mode), 0o600,
                )
                self.assertEqual(inspect_runtime_data(destination / "runtime-data")["state"], "ready")
                config = json.loads((destination / "box" / "box.json").read_text())
                self.assertEqual(config["data_mode"], "demo")
                verified = verify_trial_workspace(destination, PACKS)
                self.assertTrue(verified["valid"])
                self.assertEqual(verified["profile_id"], profile)

    def test_existing_root_tamper_extra_entry_and_unsafe_mode_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp_name:
            base = Path(temp_name).resolve()
            destination = base / "trial"
            initialize_trial_workspace(
                profile="dtc",
                country="NL",
                packs_root=PACKS,
                destination_root=destination,
                actor="trial-negative-auditor",
            )
            with self.assertRaisesRegex(TrialWorkspaceError, "already exists"):
                initialize_trial_workspace(
                    profile="dtc", country="NL", packs_root=PACKS,
                    destination_root=destination, actor="trial-negative-auditor",
                )
            extra = destination / "unexpected.txt"
            extra.write_text("unexpected", encoding="utf-8")
            with self.assertRaisesRegex(TrialWorkspaceError, "unexpected"):
                verify_trial_workspace(destination, PACKS)
            extra.unlink()
            manifest_path = destination / MANIFEST_NAME
            manifest = json.loads(manifest_path.read_text())
            manifest["country_code"] = "US"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            manifest_path.chmod(0o600)
            with self.assertRaisesRegex(TrialWorkspaceError, "fingerprint"):
                verify_trial_workspace(destination, PACKS)

            second = base / "unsafe"
            initialize_trial_workspace(
                profile="game", country="CN", packs_root=PACKS,
                destination_root=second, actor="trial-negative-auditor",
            )
            second.chmod(0o755)
            with self.assertRaisesRegex(TrialWorkspaceError, "0700"):
                verify_trial_workspace(second, PACKS)

    def test_runner_verifies_then_uses_separate_data_and_requires_auth_off_loopback(self):
        with tempfile.TemporaryDirectory() as temp_name:
            destination = Path(temp_name).resolve() / "trial"
            initialize_trial_workspace(
                profile="marketplace",
                country="US",
                packs_root=PACKS,
                destination_root=destination,
                actor="trial-runner-auditor",
                integrations=["amazon_seller"],
            )
            with self.assertRaisesRegex(TrialWorkspaceError, "requires authentication"):
                run_trial_workbench(
                    destination, PACKS, host="0.0.0.0", environ={"PATH": os.environ["PATH"]},
                )
            observed: dict[str, object] = {}

            class FakeProcess:
                def wait(self, timeout=None):
                    observed["wait_timeout"] = timeout
                    return 0

            def fake_popen(argv, *, cwd, env):
                observed.update({"argv": argv, "cwd": cwd, "env": env})
                return FakeProcess()

            with patch("src.trial_workspace.subprocess.Popen", side_effect=fake_popen):
                result = run_trial_workbench(
                    destination,
                    PACKS,
                    port=8877,
                    environ={"PATH": os.environ["PATH"]},
                )
            self.assertTrue(result["stopped"])
            self.assertEqual(result["port"], 8877)
            self.assertTrue(result["post_stop_workspace_verified"])
            self.assertEqual(observed["cwd"], destination / "runtime-data")
            environment = observed["env"]
            self.assertEqual(environment["OPC_FINANCE_BOX_CONFIG"], str(destination / "box" / "box.json"))
            self.assertEqual(environment["OPC_FINANCE_DATA_DIR"], str(destination / "runtime-data"))
            self.assertEqual(environment["OPC_FINANCE_TRIAL_ROOT"], str(destination))
            self.assertEqual(environment["OPC_FINANCE_PORT"], "8877")
            self.assertNotIn("OPC_FINANCE_API_TOKEN", environment)
            self.assertTrue(verify_trial_workspace(destination, PACKS)["valid"])

    def test_runner_turns_keyboard_interrupt_into_a_clean_operator_stop(self):
        with tempfile.TemporaryDirectory() as temp_name:
            destination = Path(temp_name).resolve() / "trial"
            initialize_trial_workspace(
                profile="game", country="CN", packs_root=PACKS,
                destination_root=destination, actor="trial-stop-auditor",
            )

            class InterruptThenStop:
                calls = 0

                def wait(self, timeout=None):
                    self.calls += 1
                    if self.calls == 1:
                        raise KeyboardInterrupt
                    self.timeout = timeout
                    return 0

            process = InterruptThenStop()
            with patch("src.trial_workspace.subprocess.Popen", return_value=process):
                result = run_trial_workbench(
                    destination, PACKS, environ={"PATH": os.environ["PATH"]},
                )
            self.assertTrue(result["stopped"])
            self.assertTrue(result["stopped_by_operator"])
            self.assertTrue(result["post_stop_workspace_verified"])
            self.assertEqual(process.timeout, 10)

    def test_onboarding_plan_compresses_verified_trial_into_safe_founder_journey(self):
        with tempfile.TemporaryDirectory() as temp_name:
            destination = Path(temp_name).resolve() / "uae-dtc-trial"
            initialize_trial_workspace(
                profile="dtc",
                country="AE",
                packs_root=PACKS,
                destination_root=destination,
                actor="trial-onboarding-founder",
                integrations=["shopify_stripe"],
            )
            plan = build_trial_onboarding_plan(destination, PACKS)
            self.assertTrue(plan["valid"])
            self.assertEqual(
                plan["artifact_type"], "opc_finance_box_trial_onboarding_plan",
            )
            self.assertEqual(plan["current_stage_id"], "explore_local_demo")
            self.assertEqual(plan["next_action_id"], "run_local_demo")
            self.assertEqual(plan["starter"]["starter_id"], "dtc.ae")
            self.assertEqual(plan["starter"]["selected_integrations"], ["shopify_stripe"])
            self.assertEqual(len(plan["journey"]), 5)
            self.assertEqual(
                [item["status"] for item in plan["journey"]],
                ["ready", "available", "blocked", "locked", "locked"],
            )
            fork_command = plan["journey"][1]["command_templates"][0]
            self.assertIn("--profile dtc", fork_command)
            self.assertIn("--country AE", fork_command)
            self.assertIn("--integration shopify_stripe", fork_command)
            self.assertIn("--data-mode live", fork_command)
            self.assertGreater(plan["summary"]["setup_task_count"], 0)
            self.assertGreater(plan["summary"]["blocking_setup_task_count"], 0)
            self.assertEqual(
                sum(item["task_count"] for item in plan["setup_phases"]),
                plan["summary"]["setup_task_count"],
            )
            self.assertTrue(plan["priority_setup_tasks"])
            self.assertFalse(plan["summary"]["production_ready"])
            self.assertTrue(plan["control_boundary"]["commands_are_templates_only"])
            self.assertFalse(plan["control_boundary"]["commands_executed"])
            self.assertFalse(plan["control_boundary"]["paths_returned"])
            serialized = json.dumps(plan)
            self.assertNotIn(str(destination), serialized)
            self.assertNotIn("trial-onboarding-founder", serialized)


if __name__ == "__main__":
    unittest.main()
