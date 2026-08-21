import json
import os
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from src.box_runtime import BoxRuntime
from src.pilot_data_handoff import (
    PilotDataHandoffError,
    build_pilot_data_handoff_plan,
    build_pilot_data_handoff_status,
    build_pilot_data_handoff_workspace,
    review_pilot_data_handoff_workpaper,
    verify_pilot_data_handoff_review,
    write_pilot_data_handoff_workpaper,
)
from src.pilot_readiness import (
    review_pilot_readiness_workpaper,
    write_pilot_readiness_workpaper,
)


ROOT = Path(__file__).resolve().parents[1]
PACKS = ROOT / "packs"


class PilotDataHandoffTests(unittest.TestCase):
    def runtime(self):
        return BoxRuntime(
            ROOT / "examples" / "boxes" / "global_game_studio.json", PACKS,
        )

    def build_readiness_review(self, runtime, root: Path) -> Path:
        workpaper = root / "readiness-workpaper.json"
        reviewed = root / "readiness-review.json"
        write_pilot_readiness_workpaper(
            runtime, workpaper, period="2026-07", prepared_by="readiness-preparer",
        )
        value = json.loads(workpaper.read_text(encoding="utf-8"))
        value["operator_principal"] = "readiness-operator"
        for entity in value["entities"]:
            for domain in entity["data_domains"]:
                domain.update({
                    "status": "ready", "acquisition_mode": "file_export",
                    "period_coverage": ["2026-07"], "read_only_confirmed": True,
                    "mapping_approved_by": "readiness-mapping-reviewer",
                    "evidence_references": [
                        f"evidence://readiness/{entity['entity_id']}/{domain['domain']}"
                    ],
                })
        entity_ids = [item["entity_id"] for item in value["entities"]]
        for connector in value["network_connectors"]:
            connector.update({
                "status": "approved_file_fallback", "entity_ids": entity_ids,
                "credential_reference_configured": False,
                "provider_contract_passed": False,
                "bounded_read_window_confirmed": False,
                "checkpoint_owner": "readiness-checkpoint-owner",
                "mapping_approved_by": "readiness-connector-reviewer",
                "evidence_references": ["evidence://readiness/connector/fallback"],
            })
        value["shadow_close_plan"].update({
            "planned": True, "baseline_owner": "readiness-baseline-owner",
            "evidence_references": ["workpaper://readiness/shadow-plan"],
        })
        workpaper.write_text(json.dumps(value), encoding="utf-8")
        workpaper.chmod(0o600)
        review_pilot_readiness_workpaper(
            runtime, workpaper, reviewed, actor="readiness-independent-reviewer",
            rationale="Independent review confirms bounded readiness controls.",
            evidence_references=["advisor://readiness/independent-review"],
        )
        return reviewed

    def complete_handoff(self, path: Path):
        value = json.loads(path.read_text(encoding="utf-8"))
        for entity in value["entities"]:
            for domain in entity["data_domains"]:
                domain.update({
                    "mapped_entity_id": entity["entity_id"],
                    "status": "delivered",
                    "transfer_mode": "local_only",
                    "source_file_count": 1,
                    "source_manifest_sha256": "a" * 64,
                    "period_coverage": [value["period"]],
                    "contains_personal_data": "no",
                    "privacy_control": "not_required",
                    "source_owner": "handoff-source-owner",
                    "access_approved_by": "handoff-access-approver",
                    "evidence_references": [
                        f"evidence://handoff/{entity['entity_id']}/{domain['domain']}"
                    ],
                })
        path.write_text(json.dumps(value), encoding="utf-8")
        path.chmod(0o600)
        return value

    def test_plan_reuses_exact_entity_and_industry_domain_scope(self):
        plan = build_pilot_data_handoff_plan(self.runtime())
        self.assertEqual(plan["entity_ids"], ["cn_studio", "sg_publisher"])
        domains = {item["domain"] for item in plan["data_domain_requirements"]}
        self.assertIn("channel_settlements", domains)
        self.assertIn("intercompany", domains)
        self.assertFalse(plan["control_boundary"]["raw_files_copied_by_manifest"])
        self.assertFalse(plan["control_boundary"]["financial_values_requested"])

    def test_private_handoff_review_verifies_without_returning_source_details(self):
        runtime = self.runtime()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            readiness = self.build_readiness_review(runtime, root)
            workpaper = root / "handoff-workpaper.json"
            reviewed = root / "handoff-review.json"
            created = write_pilot_data_handoff_workpaper(
                runtime, readiness, workpaper,
                prepared_by="handoff-preparer",
                custodian_principal="handoff-custodian",
            )
            self.assertTrue(created["output_written"])
            if os.name != "nt":
                self.assertEqual(workpaper.stat().st_mode & 0o777, 0o600)
            self.complete_handoff(workpaper)
            review_pilot_data_handoff_workpaper(
                runtime, workpaper, readiness, reviewed,
                actor="handoff-independent-reviewer",
                rationale="Independent access review confirms the controlled handoff manifest.",
                evidence_references=["advisor://handoff/access-review"],
            )
            verified = verify_pilot_data_handoff_review(
                runtime, reviewed, readiness,
            )
            self.assertTrue(verified["valid"])
            self.assertTrue(verified["ready_for_controlled_data_intake"])
            self.assertEqual(verified["entity_count"], 2)
            self.assertEqual(verified["data_domain_count"], 18)
            self.assertFalse(verified["source_file_names_or_paths_returned"])
            self.assertFalse(verified["source_manifest_hash_values_returned"])
            self.assertFalse(verified["financial_values_returned"])
            self.assertFalse(verified["data_import_performed"])
            status = build_pilot_data_handoff_status(
                runtime, reviewed, readiness,
            )
            self.assertEqual(status["status"], "current")
            workspace = build_pilot_data_handoff_workspace(
                runtime, reviewed, readiness,
            )
            self.assertEqual(workspace["summary"]["total_data_domain_count"], 18)
            self.assertTrue(
                workspace["summary"]["ready_for_controlled_data_intake"]
            )
            serialized = json.dumps(workspace)
            self.assertNotIn("handoff-independent-reviewer", serialized)
            self.assertNotIn("a" * 64, serialized)

    def test_scope_roles_tampering_and_readiness_expiry_fail_closed(self):
        runtime = self.runtime()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            readiness = self.build_readiness_review(runtime, root)
            workpaper = root / "handoff-workpaper.json"
            write_pilot_data_handoff_workpaper(
                runtime, readiness, workpaper,
                prepared_by="handoff-preparer",
                custodian_principal="handoff-custodian",
            )
            self.complete_handoff(workpaper)
            value = json.loads(workpaper.read_text(encoding="utf-8"))
            value["entities"][0]["data_domains"][0]["mapped_entity_id"] = "sg_publisher"
            workpaper.write_text(json.dumps(value), encoding="utf-8")
            workpaper.chmod(0o600)
            with self.assertRaisesRegex(PilotDataHandoffError, "cross-entity"):
                review_pilot_data_handoff_workpaper(
                    runtime, workpaper, readiness, root / "bad-scope.json",
                    actor="handoff-independent-reviewer",
                    rationale="Cross-entity data handoff scope must be rejected.",
                    evidence_references=["advisor://handoff/bad-scope"],
                )

            self.complete_handoff(workpaper)
            reviewed = root / "handoff-review.json"
            with self.assertRaisesRegex(PilotDataHandoffError, "reviewer must differ"):
                review_pilot_data_handoff_workpaper(
                    runtime, workpaper, readiness, root / "bad-role.json",
                    actor="handoff-custodian",
                    rationale="Role overlap must be rejected by the handoff control.",
                    evidence_references=["advisor://handoff/bad-role"],
                )
            review_pilot_data_handoff_workpaper(
                runtime, workpaper, readiness, reviewed,
                actor="handoff-independent-reviewer",
                rationale="Independent access review confirms the corrected handoff.",
                evidence_references=["advisor://handoff/corrected-review"],
            )
            readiness_value = json.loads(readiness.read_text(encoding="utf-8"))
            expired_as_of = (
                date.fromisoformat(readiness_value["review"]["expires_at"])
                + timedelta(days=1)
            ).isoformat()
            expired = verify_pilot_data_handoff_review(
                runtime, reviewed, readiness, as_of=expired_as_of,
            )
            self.assertFalse(expired["ready_for_controlled_data_intake"])

            tampered = json.loads(reviewed.read_text(encoding="utf-8"))
            tampered["entities"][0]["data_domains"][0]["source_file_count"] = 2
            reviewed.write_text(json.dumps(tampered), encoding="utf-8")
            reviewed.chmod(0o600)
            with self.assertRaisesRegex(PilotDataHandoffError, "not bound"):
                verify_pilot_data_handoff_review(runtime, reviewed, readiness)


if __name__ == "__main__":
    unittest.main()
