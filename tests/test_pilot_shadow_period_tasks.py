from __future__ import annotations

import unittest
from unittest.mock import patch

import src.pilot_shadow_period_tasks as task_module
from src.pilot_shadow_period_tasks import (
    PilotShadowPeriodTaskError,
    project_period_operator_tasks,
)


class PilotShadowPeriodTaskTests(unittest.TestCase):
    def test_projects_role_evidence_and_dependency_without_commands(self):
        tasks = project_period_operator_tasks([
            {
                "step_id": "pilot-readiness-complete",
                "action": "edit_private_json",
                "reported_outcome": "reported_complete",
                "event_count": 1,
            },
            {
                "step_id": "pilot-readiness-review",
                "action": "run_cli",
                "reported_outcome": "blocked",
                "event_count": 2,
            },
            {
                "step_id": "pilot-readiness-verify",
                "action": "run_cli",
                "reported_outcome": "not_reported",
                "event_count": 0,
            },
        ], entity_ids=["entity_a"])
        self.assertEqual(tasks[0]["work_status"], "reported_complete")
        self.assertEqual(tasks[1]["work_status"], "reported_blocked")
        self.assertTrue(tasks[1]["actionable_now"])
        self.assertEqual(tasks[1]["responsible_role"], "pilot_control_reviewer")
        self.assertEqual(
            tasks[1]["must_be_separate_from_role_ids"],
            ["pilot_finance_preparer"],
        )
        self.assertIn(
            "completed_readiness_workpaper",
            tasks[1]["required_evidence_type_ids"],
        )
        self.assertEqual(tasks[1]["guidance_version"], 1)
        self.assertEqual(
            tasks[1]["work_product_type_id"],
            "reviewed_readiness_workpaper",
        )
        self.assertIn(
            "confirm_required_roles_are_separate",
            tasks[1]["operator_checklist_type_ids"],
        )
        self.assertIn(
            "stop_on_role_overlap",
            tasks[1]["stop_condition_type_ids"],
        )
        self.assertTrue(tasks[1]["completion_is_operator_report_only"])
        self.assertTrue(tasks[1]["authoritative_verifier_required"])
        self.assertEqual(tasks[2]["work_status"], "waiting_on_prior_task")
        for task in tasks:
            self.assertTrue(task["work_product_type_id"])
            self.assertTrue(task["operator_checklist_type_ids"])
            self.assertTrue(task["stop_condition_type_ids"])
            self.assertFalse(task["browser_action_available"])
            self.assertFalse(task["command_returned"])
            self.assertNotIn("argv", task)
            self.assertNotIn("shell_preview", task)

    def test_entity_task_is_bound_to_an_installed_legal_entity(self):
        task = project_period_operator_tasks([{
            "step_id": "shadow-close-review:entity_a",
            "action": "run_cli",
            "reported_outcome": "not_reported",
            "event_count": 0,
        }], entity_ids=["entity_a"])[0]
        self.assertEqual(task["entity_id"], "entity_a")
        self.assertEqual(task["task_type"], "entity_shadow_review")
        self.assertEqual(task["responsible_role"], "entity_shadow_reviewer")
        self.assertEqual(task["completion_channel"], "cli")
        self.assertEqual(
            task["work_product_type_id"], "reviewed_entity_report",
        )
        self.assertIn(
            "resolve_each_difference_with_evidence",
            task["operator_checklist_type_ids"],
        )
        self.assertIn(
            "stop_on_unresolved_difference",
            task["stop_condition_type_ids"],
        )
        self.assertFalse(task["authoritative_completion"])

        with self.assertRaisesRegex(PilotShadowPeriodTaskError, "not covered"):
            project_period_operator_tasks([{
                "step_id": "shadow-close-review:other_entity",
                "action": "run_cli",
                "reported_outcome": "not_reported",
                "event_count": 0,
            }], entity_ids=["entity_a"])

    def test_unknown_steps_and_action_drift_fail_closed(self):
        with self.assertRaisesRegex(PilotShadowPeriodTaskError, "not covered"):
            project_period_operator_tasks([{
                "step_id": "new-unsafe-step",
                "action": "run_cli",
                "reported_outcome": "not_reported",
                "event_count": 0,
            }], entity_ids=[])

        with patch.dict(task_module._PLAYBOOKS, {}, clear=True):
            with self.assertRaisesRegex(
                PilotShadowPeriodTaskError, "no safe method playbook",
            ):
                project_period_operator_tasks([{
                    "step_id": "pilot-readiness-complete",
                    "action": "edit_private_json",
                    "reported_outcome": "not_reported",
                    "event_count": 0,
                }], entity_ids=[])
        with self.assertRaisesRegex(PilotShadowPeriodTaskError, "action"):
            project_period_operator_tasks([{
                "step_id": "pilot-readiness-complete",
                "action": "run_cli",
                "reported_outcome": "not_reported",
                "event_count": 0,
            }], entity_ids=[])

    def test_multi_entity_tasks_keep_each_entity_and_portfolio_roles_separate(self):
        tasks = project_period_operator_tasks([
            {
                "step_id": "shadow-close-review:entity_a",
                "action": "run_cli",
                "reported_outcome": "reported_complete",
                "event_count": 1,
            },
            {
                "step_id": "shadow-close-review:entity_b",
                "action": "run_cli",
                "reported_outcome": "reported_complete",
                "event_count": 1,
            },
            {
                "step_id": "shadow-portfolio-assemble",
                "action": "run_cli",
                "reported_outcome": "not_reported",
                "event_count": 0,
            },
            {
                "step_id": "shadow-portfolio-review",
                "action": "run_cli",
                "reported_outcome": "not_reported",
                "event_count": 0,
            },
        ], entity_ids=["entity_a", "entity_b"])
        self.assertEqual(
            [item["entity_id"] for item in tasks[:2]],
            ["entity_a", "entity_b"],
        )
        self.assertTrue(tasks[2]["actionable_now"])
        self.assertEqual(tasks[2]["responsible_role"], "portfolio_preparer")
        self.assertEqual(
            tasks[2]["independent_review_role"], "portfolio_reviewer",
        )
        self.assertIn(
            "entity_shadow_reviewer",
            tasks[3]["must_be_separate_from_role_ids"],
        )
        self.assertEqual(
            tasks[2]["work_product_type_id"], "assembled_portfolio",
        )
        self.assertIn(
            "confirm_all_entity_reports_present",
            tasks[2]["operator_checklist_type_ids"],
        )
        self.assertIn(
            "stop_on_incomplete_entity_coverage",
            tasks[2]["stop_condition_type_ids"],
        )


if __name__ == "__main__":
    unittest.main()
