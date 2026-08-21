from __future__ import annotations

import json
from pathlib import Path
import unittest
from unittest.mock import patch

from src.activation_runbook import ActivationRunbookStore
from src.box_runtime import BoxRuntime
from src.pilot_shadow_period_index import (
    PilotShadowPeriodIndexError,
    build_pilot_shadow_period_workspace_index,
)
from tests.test_pilot_shadow_period_runbook import (
    PilotShadowPeriodRunbookTests,
)


ROOT = Path(__file__).resolve().parents[1]
PACKS = ROOT / "packs"


class PilotShadowPeriodIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = PilotShadowPeriodRunbookTests(methodName="runTest")
        self.fixture.setUp()
        self.runtime = self.fixture.runtime
        self.activation = self.fixture.activation
        self.store = self.fixture.store

    def tearDown(self) -> None:
        self.fixture.tearDown()

    def test_missing_mount_is_safe_and_does_not_claim_completion(self):
        result = build_pilot_shadow_period_workspace_index(self.runtime, None)
        self.assertEqual(result["summary"]["activation_status"], "missing")
        self.assertFalse(result["summary"]["configured"])
        self.assertEqual(result["periods"], [])
        overlay = result["business_control_overlay"]
        self.assertEqual(overlay["business_model_type_ids"], ["dtc_store"])
        self.assertIn(
            "order_payment_refund_reconciliation",
            overlay["monthly_control_objective_type_ids"],
        )
        self.assertFalse(overlay["financial_values_returned"])
        metrics = result["business_metric_catalog"]
        self.assertEqual(metrics["business_model_type_ids"], ["dtc_store"])
        metric_ids = {
            item["metric_type_id"] for item in metrics["metric_definitions"]
        }
        self.assertIn("cash_runway_months", metric_ids)
        self.assertIn("dtc_net_sales", metric_ids)
        self.assertNotIn("game_platform_net_revenue", metric_ids)
        self.assertFalse(metrics["metric_values_returned"])
        self.assertFalse(metrics["formula_evaluated"])
        self.assertTrue(
            result["control_boundary"]["business_control_types_only"]
        )
        self.assertTrue(
            result["control_boundary"]["business_metric_catalog_returned"]
        )
        self.assertTrue(
            result["control_boundary"]["metric_definitions_only"]
        )
        self.assertFalse(result["control_boundary"]["metric_values_returned"])
        self.assertFalse(
            result["summary"]["authoritative_period_completion_inferred"]
        )
        self.assertFalse(result["control_boundary"]["external_action_performed"])

    def test_empty_month_is_projected_without_creating_lock_or_ledger(self):
        self.assertFalse(self.store.lock_file.exists())
        self.assertFalse(self.store.events_file.exists())
        with patch.object(
            ActivationRunbookStore, "_locked",
            side_effect=AssertionError("read-only projection used a writable lock"),
        ):
            result = build_pilot_shadow_period_workspace_index(
                self.runtime, self.activation,
            )
        self.assertEqual(result["summary"]["activation_status"], "current")
        self.assertEqual(result["summary"]["period_count"], 1)
        self.assertEqual(result["summary"]["resumable_period"], "2026-09")
        self.assertEqual(
            result["summary"]["resumable_step_id"],
            "pilot-readiness-complete",
        )
        self.assertEqual(result["periods"][0]["event_count"], 0)
        self.assertFalse(result["periods"][0]["runbook_started"])
        self.assertFalse(result["periods"][0]["authoritative_period_completion"])
        self.assertEqual(result["summary"]["operator_task_count"], 23)
        self.assertEqual(
            result["summary"]["current_task"]["responsible_role"],
            "pilot_finance_preparer",
        )
        self.assertEqual(
            result["summary"]["current_task"]["completion_channel"],
            "private_workspace",
        )
        self.assertEqual(
            result["periods"][0]["actionable_task_count"], 1,
        )
        self.assertFalse(result["control_boundary"]["commands_returned"])
        self.assertFalse(
            result["control_boundary"]["browser_actions_available"]
        )
        self.assertTrue(
            result["control_boundary"]["safe_method_guidance_returned"]
        )
        self.assertTrue(
            result["control_boundary"]["business_control_overlay_returned"]
        )
        self.assertTrue(
            result["control_boundary"]["business_metric_catalog_returned"]
        )
        self.assertTrue(
            result["control_boundary"]["authoritative_verifier_required"]
        )
        self.assertEqual(
            result["summary"]["current_task"]["work_product_type_id"],
            "completed_readiness_workpaper",
        )
        self.assertIn(
            "confirm_current_box_and_period",
            result["summary"]["current_task"][
                "operator_checklist_type_ids"
            ],
        )
        self.assertFalse(self.store.lock_file.exists())
        self.assertFalse(self.store.events_file.exists())
        serialized = json.dumps(result)
        self.assertNotIn(str(self.activation), serialized)
        self.assertNotIn("sha256", serialized.lower())

    def test_private_progress_is_safely_aggregated_without_private_values(self):
        self.store.record(
            self.runtime,
            step_id="pilot-readiness-complete",
            outcome="reported_complete",
            actor="private-monthly-actor",
            rationale="PRIVATE-MONTHLY-RATIONALE",
            evidence_references=["private://monthly/evidence-reference"],
        )
        self.store.record(
            self.runtime,
            step_id="pilot-readiness-review",
            outcome="blocked",
            actor="private-review-actor",
            rationale="PRIVATE-BLOCKER",
        )
        with patch.object(
            ActivationRunbookStore, "_locked",
            side_effect=AssertionError("read-only projection used a writable lock"),
        ):
            result = build_pilot_shadow_period_workspace_index(
                self.runtime, self.activation,
            )
        period = result["periods"][0]
        self.assertTrue(period["runbook_started"])
        self.assertEqual(period["event_count"], 2)
        self.assertEqual(period["reported_complete_count"], 1)
        self.assertEqual(period["reported_blocked_count"], 1)
        self.assertEqual(
            period["next_reported_progress_step_id"],
            "pilot-readiness-review",
        )
        self.assertEqual(result["summary"]["reported_event_count"], 2)
        current = result["summary"]["current_task"]
        self.assertEqual(current["work_status"], "reported_blocked")
        self.assertEqual(current["responsible_role"], "pilot_control_reviewer")
        self.assertIn(
            "completed_readiness_workpaper",
            current["required_evidence_type_ids"],
        )
        self.assertFalse(current["browser_action_available"])
        self.assertEqual(
            current["work_product_type_id"],
            "reviewed_readiness_workpaper",
        )
        self.assertIn(
            "stop_on_role_overlap", current["stop_condition_type_ids"],
        )
        serialized = json.dumps(result)
        for forbidden in (
            "private-monthly-actor", "private-review-actor",
            "PRIVATE-MONTHLY-RATIONALE", "PRIVATE-BLOCKER",
            "private://monthly", str(self.activation), "chain_head",
            "command_contract", "runtime_fingerprint",
            "shell_preview", '"argv"',
        ):
            self.assertNotIn(forbidden, serialized)

    def test_multiple_periods_are_sorted_and_resume_the_latest_open_month(self):
        october = self.fixture.append_and_initialize_october()
        october.record(
            self.runtime,
            step_id="pilot-readiness-complete",
            outcome="reported_complete",
            actor="october-operator",
            rationale="October readiness edit was reported complete only.",
        )
        result = build_pilot_shadow_period_workspace_index(
            self.runtime, self.activation,
        )
        self.assertEqual(
            [item["period"] for item in result["periods"]],
            ["2026-09", "2026-10"],
        )
        self.assertEqual(result["summary"]["period_count"], 2)
        self.assertEqual(result["summary"]["latest_period"], "2026-10")
        self.assertEqual(result["summary"]["resumable_period"], "2026-10")
        self.assertEqual(
            result["summary"]["resumable_step_id"],
            "pilot-readiness-review",
        )
        self.assertFalse(
            result["summary"]["authoritative_period_completion_inferred"]
        )

    def test_invalid_entry_tamper_and_other_box_fail_closed(self):
        period_parent = self.activation / "pilot" / "period-workspaces"
        unexpected = period_parent / "notes.txt"
        unexpected.write_text("private path must not be listed", encoding="utf-8")
        unexpected.chmod(0o600)
        with self.assertRaisesRegex(
            PilotShadowPeriodIndexError, "invalid entry",
        ):
            build_pilot_shadow_period_workspace_index(
                self.runtime, self.activation,
            )
        unexpected.unlink()

        other_runtime = BoxRuntime(
            ROOT / "examples" / "boxes" / "cn_marketplace_store.json", PACKS,
        )
        with self.assertRaisesRegex(
            PilotShadowPeriodIndexError, "failed validation",
        ) as context:
            build_pilot_shadow_period_workspace_index(
                other_runtime, self.activation,
            )
        self.assertNotIn(str(self.activation), str(context.exception))

        self.store.record(
            self.runtime,
            step_id="pilot-readiness-complete",
            outcome="blocked",
            actor="operator",
            rationale="Waiting for evidence.",
        )
        self.store.lock_file.unlink()
        with self.assertRaisesRegex(
            PilotShadowPeriodIndexError, "failed validation",
        ):
            build_pilot_shadow_period_workspace_index(
                self.runtime, self.activation,
            )


if __name__ == "__main__":
    unittest.main()
