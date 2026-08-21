import json
import os
import tempfile
import unittest
from pathlib import Path

from src.box_runtime import BoxRuntime
from src.pilot_readiness import (
    PilotReadinessError,
    build_pilot_readiness_alerts,
    build_pilot_readiness_plan,
    build_pilot_readiness_status,
    review_pilot_readiness_workpaper,
    verify_pilot_readiness_review,
    write_pilot_readiness_workpaper,
)


ROOT = Path(__file__).resolve().parents[1]
PACKS = ROOT / "packs"


class PilotReadinessTests(unittest.TestCase):
    def runtime(self, config="global_game_studio.json"):
        return BoxRuntime(ROOT / "examples" / "boxes" / config, PACKS)

    def complete(self, path: Path):
        value = json.loads(path.read_text(encoding="utf-8"))
        value["operator_principal"] = "pilot-operator"
        for entity in value["entities"]:
            for domain in entity["data_domains"]:
                domain["mapped_entity_id"] = entity["entity_id"]
                if not domain["required"] and domain["not_applicable_allowed"]:
                    domain["status"] = "not_applicable"
                    domain["acquisition_mode"] = "not_applicable"
                else:
                    domain["status"] = "ready"
                    domain["acquisition_mode"] = "file_export"
                domain["period_coverage"] = [value["period"]]
                domain["read_only_confirmed"] = True
                domain["mapping_approved_by"] = "mapping-reviewer"
                domain["evidence_references"] = [
                    f"evidence://pilot/{entity['entity_id']}/{domain['domain']}"
                ]
        entity_ids = [item["entity_id"] for item in value["entities"]]
        for connector in value["network_connectors"]:
            connector.update({
                "status": "approved_file_fallback",
                "entity_ids": entity_ids,
                "credential_reference_configured": False,
                "provider_contract_passed": False,
                "bounded_read_window_confirmed": False,
                "checkpoint_owner": "checkpoint-owner",
                "mapping_approved_by": "connector-reviewer",
                "evidence_references": [
                    f"evidence://pilot/connector/{connector['connector_id']}"
                ],
            })
        value["shadow_close_plan"].update({
            "planned": True,
            "baseline_owner": "baseline-owner",
            "evidence_references": ["workpaper://pilot/shadow-close-plan"],
        })
        path.write_text(json.dumps(value), encoding="utf-8")
        path.chmod(0o600)
        return value

    def test_plan_is_industry_and_connector_driven(self):
        game = build_pilot_readiness_plan(self.runtime())
        game_domains = {item["domain"] for item in game["data_domain_requirements"]}
        self.assertIn("channel_settlements", game_domains)
        self.assertIn("intercompany", game_domains)
        self.assertNotIn("orders", game_domains)
        self.assertEqual(game["entity_ids"], ["cn_studio", "sg_publisher"])

        dtc = build_pilot_readiness_plan(
            self.runtime("sg_dtc_shopify_stripe_wise_store.json")
        )
        dtc_domains = {item["domain"] for item in dtc["data_domain_requirements"]}
        self.assertTrue({"orders", "payments_and_settlements", "inventory"} <= dtc_domains)
        connector_ids = {
            item["connector_id"] for item in dtc["network_connector_requirements"]
        }
        self.assertTrue({
            "shopify.orders", "stripe.balance_transactions",
            "stripe.payouts", "wise.balance_statement",
        } <= connector_ids)
        self.assertFalse(dtc["control_boundary"]["credential_values_requested"])

    def test_private_workpaper_review_and_safe_verification(self):
        runtime = self.runtime()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workpaper = root / "pilot-workpaper.json"
            reviewed = root / "pilot-reviewed.json"
            created = write_pilot_readiness_workpaper(
                runtime, workpaper, period="2026-07", prepared_by="pilot-preparer",
            )
            self.assertTrue(created["output_written"])
            if os.name != "nt":
                self.assertEqual(workpaper.stat().st_mode & 0o777, 0o600)
            self.complete(workpaper)
            review = review_pilot_readiness_workpaper(
                runtime, workpaper, reviewed, actor="pilot-reviewer",
                rationale="Independent evidence confirms a bounded read-only pilot.",
                evidence_references=["advisor://pilot/readiness-review"],
            )
            self.assertTrue(review["ready_for_bounded_shadow"])
            self.assertFalse(review["ready_for_statutory_release"])
            verified = verify_pilot_readiness_review(runtime, reviewed)
            self.assertTrue(verified["valid"])
            self.assertTrue(verified["ready_for_bounded_shadow"])
            self.assertFalse(verified["ready_for_tax_calendar_release"])
            self.assertFalse(verified["ready_for_external_filing"])
            self.assertFalse(verified["actors_returned"])
            self.assertFalse(verified["financial_values_returned"])
            self.assertFalse(verified["tax_registry_activation"]["configured"])

    def test_role_separation_scope_and_review_binding_fail_closed(self):
        runtime = self.runtime()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workpaper = root / "pilot-workpaper.json"
            write_pilot_readiness_workpaper(
                runtime, workpaper, period="2026-07", prepared_by="pilot-preparer",
            )
            self.complete(workpaper)
            with self.assertRaisesRegex(PilotReadinessError, "reviewer must differ"):
                review_pilot_readiness_workpaper(
                    runtime, workpaper, root / "bad-review.json",
                    actor="pilot-operator", rationale="This must be rejected for role overlap.",
                    evidence_references=["advisor://pilot/rejected-review"],
                )
            value = json.loads(workpaper.read_text(encoding="utf-8"))
            value["entities"][0]["data_domains"][0]["mapped_entity_id"] = "sg_publisher"
            workpaper.write_text(json.dumps(value), encoding="utf-8")
            workpaper.chmod(0o600)
            with self.assertRaisesRegex(PilotReadinessError, "cross-entity"):
                review_pilot_readiness_workpaper(
                    runtime, workpaper, root / "scope-review.json",
                    actor="pilot-reviewer", rationale="Cross-entity scope must be rejected safely.",
                    evidence_references=["advisor://pilot/scope-review"],
                )

            self.complete(workpaper)
            reviewed = root / "pilot-reviewed.json"
            review_pilot_readiness_workpaper(
                runtime, workpaper, reviewed, actor="pilot-reviewer",
                rationale="Independent evidence confirms the corrected pilot scope.",
                evidence_references=["advisor://pilot/corrected-review"],
            )
            tampered = json.loads(reviewed.read_text(encoding="utf-8"))
            tampered["shadow_close_plan"]["baseline_owner"] = "different-owner"
            reviewed.write_text(json.dumps(tampered), encoding="utf-8")
            reviewed.chmod(0o600)
            with self.assertRaisesRegex(PilotReadinessError, "not bound"):
                verify_pilot_readiness_review(runtime, reviewed)

    def test_private_file_controls_and_paired_tax_activation_arguments(self):
        runtime = self.runtime()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workpaper = root / "pilot-workpaper.json"
            write_pilot_readiness_workpaper(
                runtime, workpaper, period="2026-07", prepared_by="pilot-preparer",
            )
            with self.assertRaisesRegex(PilotReadinessError, "refusing to overwrite"):
                write_pilot_readiness_workpaper(
                    runtime, workpaper, period="2026-07", prepared_by="pilot-preparer",
                )
            self.complete(workpaper)
            reviewed = root / "pilot-reviewed.json"
            review_pilot_readiness_workpaper(
                runtime, workpaper, reviewed, actor="pilot-reviewer",
                rationale="Independent evidence confirms the bounded pilot controls.",
                evidence_references=["advisor://pilot/control-review"],
            )
            with self.assertRaisesRegex(PilotReadinessError, "configured together"):
                verify_pilot_readiness_review(
                    runtime, reviewed, tax_review_dir=root / "reviews",
                )
            if os.name != "nt":
                reviewed.chmod(0o644)
                with self.assertRaisesRegex(PilotReadinessError, "group or other"):
                    verify_pilot_readiness_review(runtime, reviewed)

    def test_runtime_status_and_alerts_follow_review_lifecycle(self):
        runtime = self.runtime()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workpaper = root / "pilot-workpaper.json"
            reviewed = root / "pilot-reviewed.json"
            write_pilot_readiness_workpaper(
                runtime, workpaper, period="2026-07", prepared_by="pilot-preparer",
            )
            self.complete(workpaper)
            review_pilot_readiness_workpaper(
                runtime, workpaper, reviewed, actor="pilot-reviewer",
                rationale="Independent evidence confirms lifecycle alert controls.",
                evidence_references=["advisor://pilot/lifecycle-review"],
            )
            artifact = json.loads(reviewed.read_text(encoding="utf-8"))
            due_at = artifact["review"]["review_due_at"]
            expires_at = artifact["review"]["expires_at"]
            current = build_pilot_readiness_status(runtime, reviewed)
            self.assertTrue(current["valid"])
            self.assertEqual(current["status"], "current")
            due = build_pilot_readiness_alerts(runtime, reviewed, as_of=due_at)
            self.assertEqual(due["warning_count"], 1)
            self.assertEqual(due["alerts"][0]["alert_id"], "pilot-readiness:review:review_due")
            from datetime import date, timedelta
            expired_clock = (date.fromisoformat(expires_at) + timedelta(days=1)).isoformat()
            expired = build_pilot_readiness_alerts(
                runtime, reviewed, as_of=expired_clock,
            )
            self.assertEqual(expired["critical_count"], 1)
            self.assertFalse(expired["ready_for_bounded_shadow"])
            self.assertFalse(expired["notifications_sent"])
            missing = build_pilot_readiness_status(runtime)
            self.assertEqual(missing["status"], "missing")

            artifact["review"]["expires_at"] = "2099-01-01"
            reviewed.write_text(json.dumps(artifact), encoding="utf-8")
            reviewed.chmod(0o600)
            invalid = build_pilot_readiness_alerts(runtime, reviewed)
            self.assertEqual(invalid["alerts"][0]["alert_id"], "pilot-readiness:review:invalid")
            self.assertNotIn(str(root), json.dumps(invalid))


if __name__ == "__main__":
    unittest.main()
