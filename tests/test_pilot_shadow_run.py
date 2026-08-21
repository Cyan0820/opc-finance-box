from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest

from src.box_pipeline import dispatch_box_pipeline_request
from src.box_doctor import diagnose_box
from src.box_runtime import BoxRuntime
from src.pilot_data_handoff import (
    review_pilot_data_handoff_workpaper,
    write_pilot_data_handoff_workpaper,
)
from src.pilot_readiness import (
    review_pilot_readiness_workpaper,
    write_pilot_readiness_workpaper,
)
from src.pilot_shadow_run import (
    PilotShadowRunError,
    build_pilot_shadow_run_status,
    build_pilot_shadow_run_workspace,
    register_pilot_shadow_run,
    verify_pilot_shadow_run_registration,
)
from src.pipeline_run_store import PipelineRunStore, PipelineRunStoreError


ROOT = Path(__file__).resolve().parents[1]
PACKS = ROOT / "packs"


class PilotShadowRunTests(unittest.TestCase):
    def runtime(self) -> BoxRuntime:
        return BoxRuntime(
            ROOT / "examples" / "boxes" / "cn_dtc_shopify_stripe_store.json",
            PACKS,
        )

    def build_reviews(self, runtime: BoxRuntime, root: Path) -> tuple[Path, Path]:
        readiness_workpaper = root / "readiness-workpaper.json"
        readiness_review = root / "readiness-review.json"
        write_pilot_readiness_workpaper(
            runtime, readiness_workpaper, period="2026-08",
            prepared_by="readiness-preparer",
        )
        readiness = json.loads(readiness_workpaper.read_text(encoding="utf-8"))
        readiness["operator_principal"] = "readiness-operator"
        for entity in readiness["entities"]:
            for domain in entity["data_domains"]:
                domain.update({
                    "status": "ready",
                    "acquisition_mode": "file_export",
                    "period_coverage": ["2026-08"],
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
                    "period_coverage": ["2026-08"],
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
        *, operator: str = "shadow-operator",
    ) -> dict:
        request = json.loads(
            (ROOT / "examples" / "pipelines" / "month_close_control_fixture.json")
            .read_text(encoding="utf-8")
        )
        result = dispatch_box_pipeline_request(runtime, request)
        record = store.record(runtime.snapshot(), request, result, actor=operator)
        for gate in record["required_review_gates"]:
            record = store.review(
                record["attempt_id"],
                runtime_fingerprint=runtime.snapshot()["fingerprint"],
                gate=gate, decision="approved", actor="shadow-reviewer",
                rationale=f"Independent evidence review approved {gate}.",
                evidence_references=[f"evidence://shadow/2026-08/{gate}"],
            )
        return record

    def test_registration_binds_current_handoff_period_and_reviewed_ledger(self):
        runtime = self.runtime()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            readiness_review, handoff_review = self.build_reviews(runtime, root)
            store = PipelineRunStore(root / "runs")
            record = self.reviewed_month_close(runtime, store)
            self.assertEqual(record["period"], "2026-08")
            registration = root / "shadow-registration.json"
            created = register_pilot_shadow_run(
                runtime, handoff_review, readiness_review, root / "runs",
                {"cn_dtc_company": record["attempt_id"]}, registration,
                actor="shadow-registrar",
                rationale="Registrar binds the independently reviewed first Shadow Close.",
                evidence_references=["workpaper://shadow/2026-08/registration"],
            )
            self.assertTrue(created["ready_for_first_shadow_observation"])
            self.assertFalse(created["attempt_ids_returned"])
            self.assertFalse(created["financial_values_returned"])
            self.assertFalse(created["period_close_authorized"])
            if os.name != "nt":
                self.assertEqual(registration.stat().st_mode & 0o777, 0o600)
            serialized_result = json.dumps(created)
            self.assertNotIn(record["attempt_id"], serialized_result)
            self.assertNotIn(record["result_fingerprint"], serialized_result)
            verified = verify_pilot_shadow_run_registration(
                runtime, registration, handoff_review, readiness_review, root / "runs",
            )
            self.assertTrue(verified["valid"])
            self.assertEqual(verified["entity_count"], 1)
            self.assertFalse(verified["ready_for_statutory_release"])
            self.assertFalse(verified["external_filing_authorized"])
            status = build_pilot_shadow_run_status(
                runtime, registration, handoff_review, readiness_review, root / "runs",
            )
            self.assertEqual(status["status"], "current")
            workspace = build_pilot_shadow_run_workspace(
                runtime, registration, handoff_review, readiness_review, root / "runs",
            )
            self.assertTrue(
                workspace["summary"]["ready_for_first_shadow_observation"]
            )
            self.assertEqual(workspace["summary"]["registered_entity_count"], 1)
            serialized_workspace = json.dumps(workspace)
            self.assertNotIn(record["attempt_id"], serialized_workspace)
            self.assertNotIn(record["result_fingerprint"], serialized_workspace)
            self.assertNotIn("shadow-registrar", serialized_workspace)
            self.assertNotIn("workpaper://shadow", serialized_workspace)
            doctor = diagnose_box(
                runtime,
                python_version=(3, 11, 0),
                dependency_probe={"openpyxl": True, "pypdf": True},
                executable_probe={"tesseract": True, "pdftoppm": True},
                pilot_readiness_review=readiness_review,
                pilot_data_handoff_review=handoff_review,
                pilot_shadow_run_registration=registration,
                pipeline_runs_root=root / "runs",
            )
            doctor_check = next(
                item for item in doctor["checks"]
                if item["check_id"] == "pilot.first_shadow_run_registration"
            )
            self.assertEqual(doctor_check["status"], "pass")
            self.assertTrue(doctor["ready_for_first_shadow_observation"])
            self.assertNotIn(record["attempt_id"], json.dumps(doctor))

            # Later append-only activity does not invalidate the historical chain head.
            daily_request = json.loads(
                (ROOT / "examples" / "pipelines" / "shopify_stripe_daily_close_fixture.json")
                .read_text(encoding="utf-8")
            )
            daily_result = dispatch_box_pipeline_request(runtime, daily_request)
            store.record(runtime.snapshot(), daily_request, daily_result, actor="daily-operator")
            verified_later = verify_pilot_shadow_run_registration(
                runtime, registration, handoff_review, readiness_review, root / "runs",
            )
            self.assertTrue(verified_later["registration_chain_head_is_historical"])

    def test_status_is_safe_when_registration_or_companion_mounts_are_missing(self):
        runtime = self.runtime()
        missing = build_pilot_shadow_run_status(runtime)
        self.assertEqual(missing["status"], "missing")
        self.assertFalse(missing["ready_for_first_shadow_observation"])
        invalid = build_pilot_shadow_run_status(
            runtime, "private-registration.json",
        )
        self.assertEqual(invalid["status"], "invalid")
        self.assertRegex(invalid["error_sha256"], r"^[0-9a-f]{64}$")
        self.assertNotIn("private-registration.json", json.dumps(invalid))

    def test_missing_review_wrong_period_and_role_overlap_fail_closed(self):
        runtime = self.runtime()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            readiness_review, handoff_review = self.build_reviews(runtime, root)
            store = PipelineRunStore(root / "runs")
            request = json.loads(
                (ROOT / "examples" / "pipelines" / "month_close_control_fixture.json")
                .read_text(encoding="utf-8")
            )
            result = dispatch_box_pipeline_request(runtime, request)
            pending = store.record(runtime.snapshot(), request, result, actor="shadow-operator")
            with self.assertRaisesRegex(PilotShadowRunError, "fully reviewed"):
                register_pilot_shadow_run(
                    runtime, handoff_review, readiness_review, root / "runs",
                    {"cn_dtc_company": pending["attempt_id"]}, root / "pending.json",
                    actor="shadow-registrar", rationale="Pending review must be rejected here.",
                    evidence_references=["evidence://shadow/pending"],
                )

            reviewed = self.reviewed_month_close(runtime, store)
            with self.assertRaisesRegex(PilotShadowRunError, "registrar must differ"):
                register_pilot_shadow_run(
                    runtime, handoff_review, readiness_review, root / "runs",
                    {"cn_dtc_company": reviewed["attempt_id"]}, root / "overlap.json",
                    actor="shadow-reviewer", rationale="Role overlap must be rejected here.",
                    evidence_references=["evidence://shadow/role-overlap"],
                )

            mismatched_request = json.loads(json.dumps(request))
            mismatched_request["payload"]["period"] = "2026-07"
            with self.assertRaisesRegex(PipelineRunStoreError, "entity-period lineage do not match"):
                store.record(
                    runtime.snapshot(), mismatched_request, result,
                    actor="wrong-period-operator",
                )

    def test_tampering_or_later_rejection_invalidates_registration(self):
        runtime = self.runtime()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            readiness_review, handoff_review = self.build_reviews(runtime, root)
            store = PipelineRunStore(root / "runs")
            record = self.reviewed_month_close(runtime, store)
            registration = root / "registration.json"
            register_pilot_shadow_run(
                runtime, handoff_review, readiness_review, root / "runs",
                {"cn_dtc_company": record["attempt_id"]}, registration,
                actor="shadow-registrar",
                rationale="Registrar binds the independently reviewed first Shadow Close.",
                evidence_references=["workpaper://shadow/2026-08/registration"],
            )
            value = json.loads(registration.read_text(encoding="utf-8"))
            value["entity_runs"][0]["result_fingerprint"] = "0" * 64
            registration.write_text(json.dumps(value), encoding="utf-8")
            registration.chmod(0o600)
            with self.assertRaisesRegex(PilotShadowRunError, "no longer matches"):
                verify_pilot_shadow_run_registration(
                    runtime, registration, handoff_review, readiness_review, root / "runs",
                )

            value["entity_runs"][0]["result_fingerprint"] = record["result_fingerprint"]
            without_id = {key: item for key, item in value.items() if key != "registration_id"}
            import hashlib
            value["registration_id"] = hashlib.sha256(json.dumps(
                without_id, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            ).encode("utf-8")).hexdigest()[:24]
            registration.write_text(json.dumps(value), encoding="utf-8")
            registration.chmod(0o600)
            store.review(
                record["attempt_id"],
                runtime_fingerprint=runtime.snapshot()["fingerprint"],
                gate=record["required_review_gates"][0], decision="rejected",
                actor="shadow-reviewer", rationale="Later evidence invalidated this gate.",
            )
            with self.assertRaisesRegex(PilotShadowRunError, "fully reviewed"):
                verify_pilot_shadow_run_registration(
                    runtime, registration, handoff_review, readiness_review, root / "runs",
                )


if __name__ == "__main__":
    unittest.main()
