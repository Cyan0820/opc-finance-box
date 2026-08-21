import json
import tempfile
import unittest
from pathlib import Path

from src.box_doctor import diagnose_box
from src.box_runtime import BoxRuntime
from src.tax_applicability_artifacts import (
    build_tax_applicability_workpaper,
    import_tax_applicability_review,
    review_tax_applicability_workpaper,
    verify_tax_applicability_review_portfolio,
    write_tax_applicability_registry_receipt,
)
from src.pilot_readiness import (
    build_pilot_readiness_workpaper,
    review_pilot_readiness_workpaper,
)
from src.pilot_data_handoff import (
    build_pilot_data_handoff_workpaper,
    review_pilot_data_handoff_workpaper,
)


ROOT = Path(__file__).resolve().parents[1]


class BoxDoctorTests(unittest.TestCase):
    def setUp(self):
        self.runtime = BoxRuntime(
            ROOT / "examples" / "boxes" / "global_game_studio.json",
            ROOT / "packs",
        )

    def test_supported_environment_has_no_blockers_but_keeps_tax_warnings(self):
        report = diagnose_box(
            self.runtime,
            python_version=(3, 11, 0),
            dependency_probe={"openpyxl": True, "pypdf": True},
            executable_probe={"tesseract": True, "pdftoppm": True},
        )
        self.assertTrue(report["ready"])
        self.assertTrue(report["ready_for_internal_demo"])
        self.assertFalse(report["ready_for_external_filing"])
        self.assertFalse(report["ready_for_tax_calendar_release"])
        self.assertEqual(report["counts"]["blocker"], 0)
        tax_check = next(check for check in report["checks"] if check["check_id"] == "tax.filing_readiness")
        self.assertEqual(tax_check["status"], "warning")
        coverage = next(check for check in report["checks"] if check["check_id"] == "box.capability_coverage")
        self.assertEqual(coverage["status"], "pass")
        self.assertFalse(coverage["details"]["declared_only_capabilities"])
        applicability = next(
            check for check in report["checks"]
            if check["check_id"] == "tax.applicability_reviews"
        )
        self.assertEqual(applicability["status"], "warning")
        self.assertEqual(
            applicability["details"]["missing_entity_ids"],
            ["cn_studio", "sg_publisher"],
        )
        activation = next(
            check for check in report["checks"]
            if check["check_id"] == "tax.applicability_registry_activation"
        )
        self.assertEqual(activation["status"], "warning")
        self.assertFalse(activation["details"]["configured"])
        shadow_registration = next(
            check for check in report["checks"]
            if check["check_id"] == "pilot.first_shadow_run_registration"
        )
        self.assertEqual(shadow_registration["status"], "warning")
        self.assertFalse(report["ready_for_first_shadow_observation"])

    def test_current_independent_reviews_are_checked_per_entity_without_answers(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            reviews = []
            review_dir = Path(temp_dir) / "reviews"
            review_dir.mkdir()
            for entity_id in ("cn_studio", "sg_publisher"):
                workpaper = build_tax_applicability_workpaper(
                    self.runtime, entity_id, prepared_by=f"{entity_id}-operator",
                    facts_as_of="2026-08-14",
                )
                answers = {
                    "legal_form_and_pack_scope": "confirmed_in_scope",
                    "tax_residency_and_permanent_establishment": "confirmed_in_scope",
                    "direct_and_indirect_tax_registrations": "confirmed_complete",
                    "fiscal_year_and_return_periods": "confirmed",
                    "special_cross_border_and_group_regimes": "reviewed_no_additional_scope",
                }
                for question in workpaper["entity"]["questions"]:
                    question["answer"] = answers[question["question_id"]]
                    question["evidence_references"] = [
                        f"evidence://{entity_id}/{question['question_id']}"
                    ]
                workpaper_path = Path(temp_dir) / f"{entity_id}-workpaper.json"
                review_path = Path(temp_dir) / f"{entity_id}-review.json"
                workpaper_path.write_text(
                    json.dumps(workpaper, ensure_ascii=False), encoding="utf-8",
                )
                workpaper_path.chmod(0o600)
                review_tax_applicability_workpaper(
                    self.runtime,
                    workpaper_path,
                    review_path,
                    decision="approved-in-scope",
                    actor=f"{entity_id}-local-reviewer",
                    rationale="已完成主体适用性复核。",
                    evidence_references=[f"advisor://{entity_id}/memo"],
                )
                reviews.append(review_path)
                import_tax_applicability_review(
                    self.runtime, review_path, review_dir, as_of="2026-08-14",
                )
            receipt = Path(temp_dir) / "registry-receipt.json"
            write_tax_applicability_registry_receipt(
                self.runtime, review_dir, receipt,
                actor="registry-controller", as_of="2026-08-14",
            )
            report = diagnose_box(
                self.runtime,
                python_version=(3, 11, 0),
                dependency_probe={"openpyxl": True, "pypdf": True},
                executable_probe={"tesseract": True, "pdftoppm": True},
                as_of="2026-08-14",
                tax_applicability_review_paths=reviews,
                tax_applicability_review_dir=review_dir,
                tax_applicability_registry_receipt=receipt,
            )
            due_report = diagnose_box(
                self.runtime,
                python_version=(3, 11, 0),
                dependency_probe={"openpyxl": True, "pypdf": True},
                executable_probe={"tesseract": True, "pdftoppm": True},
                as_of="2027-07-15",
                tax_applicability_review_paths=reviews,
            )
            portfolio = verify_tax_applicability_review_portfolio(
                self.runtime, reviews, as_of="2026-08-14",
            )
        applicability = next(
            check for check in report["checks"]
            if check["check_id"] == "tax.applicability_reviews"
        )
        self.assertEqual(applicability["status"], "pass")
        self.assertEqual(
            applicability["details"]["approved_entity_ids"],
            ["cn_studio", "sg_publisher"],
        )
        self.assertFalse(applicability["details"]["answers_returned"])
        self.assertFalse(applicability["details"]["evidence_references_returned"])
        activation = next(
            check for check in report["checks"]
            if check["check_id"] == "tax.applicability_registry_activation"
        )
        self.assertEqual(activation["status"], "pass")
        self.assertTrue(activation["details"]["valid"])
        self.assertFalse(activation["details"]["digital_signature_verified"])
        self.assertFalse(report["ready_for_tax_calendar_release"])
        self.assertTrue(report["ready_for_internal_demo"])
        self.assertTrue(portfolio["complete"])
        self.assertEqual(portfolio["entity_count"], 2)
        self.assertFalse(portfolio["answers_returned"])
        due_applicability = next(
            check for check in due_report["checks"]
            if check["check_id"] == "tax.applicability_reviews"
        )
        self.assertEqual(due_applicability["status"], "warning")
        self.assertEqual(
            due_applicability["details"]["review_due_entity_ids"],
            ["cn_studio", "sg_publisher"],
        )

    def test_old_python_and_missing_dependency_are_blockers(self):
        report = diagnose_box(
            self.runtime,
            python_version=(3, 9, 6),
            dependency_probe={"openpyxl": True, "pypdf": False},
            executable_probe={"tesseract": False, "pdftoppm": False},
        )
        self.assertFalse(report["ready"])
        self.assertEqual(report["counts"]["blocker"], 2)
        self.assertEqual(
            {check["check_id"] for check in report["checks"] if check["status"] == "blocker"},
            {"runtime.python", "runtime.dependencies"},
        )

    def test_missing_ocr_is_only_a_warning(self):
        report = diagnose_box(
            self.runtime,
            python_version=(3, 10, 0),
            dependency_probe={"openpyxl": True, "pypdf": True},
            executable_probe={"tesseract": False, "pdftoppm": False},
        )
        ocr = next(check for check in report["checks"] if check["check_id"] == "optional.ocr")
        self.assertEqual(ocr["status"], "warning")
        self.assertTrue(report["ready"])

    def test_pilot_activation_controls_bounded_shadow_without_exposing_private_data(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workpaper = build_pilot_readiness_workpaper(
                self.runtime, period="2026-07", prepared_by="pilot-preparer",
            )
            workpaper["operator_principal"] = "pilot-operator"
            for entity in workpaper["entities"]:
                for domain in entity["data_domains"]:
                    domain.update({
                        "status": "ready", "acquisition_mode": "file_export",
                        "period_coverage": ["2026-07"],
                        "read_only_confirmed": True,
                        "mapping_approved_by": "mapping-reviewer",
                        "evidence_references": [
                            f"evidence://doctor/{entity['entity_id']}/{domain['domain']}"
                        ],
                    })
            workpaper["shadow_close_plan"].update({
                "planned": True, "baseline_owner": "baseline-owner",
                "evidence_references": ["workpaper://doctor/shadow-plan"],
            })
            workpaper_path = root / "pilot-workpaper.json"
            review_path = root / "pilot-reviewed.json"
            workpaper_path.write_text(json.dumps(workpaper), encoding="utf-8")
            workpaper_path.chmod(0o600)
            review_pilot_readiness_workpaper(
                self.runtime, workpaper_path, review_path,
                actor="pilot-reviewer",
                rationale="Independent reviewer confirmed bounded pilot controls.",
                evidence_references=["advisor://doctor/pilot-review"],
            )
            handoff = build_pilot_data_handoff_workpaper(
                self.runtime, review_path,
                prepared_by="handoff-preparer",
                custodian_principal="handoff-custodian",
            )
            for entity in handoff["entities"]:
                for domain in entity["data_domains"]:
                    domain.update({
                        "status": "delivered", "transfer_mode": "local_only",
                        "source_file_count": 1,
                        "source_manifest_sha256": "d" * 64,
                        "period_coverage": ["2026-07"],
                        "contains_personal_data": "no",
                        "privacy_control": "not_required",
                        "source_owner": "handoff-source-owner",
                        "access_approved_by": "handoff-access-approver",
                        "evidence_references": [
                            f"evidence://doctor-handoff/{entity['entity_id']}/{domain['domain']}"
                        ],
                    })
            handoff_path = root / "handoff-workpaper.json"
            handoff_review_path = root / "handoff-reviewed.json"
            handoff_path.write_text(json.dumps(handoff), encoding="utf-8")
            handoff_path.chmod(0o600)
            review_pilot_data_handoff_workpaper(
                self.runtime, handoff_path, review_path, handoff_review_path,
                actor="handoff-independent-reviewer",
                rationale="Independent reviewer confirmed controlled data intake.",
                evidence_references=["advisor://doctor-handoff/review"],
            )
            report = diagnose_box(
                self.runtime,
                python_version=(3, 11, 0),
                dependency_probe={"openpyxl": True, "pypdf": True},
                executable_probe={"tesseract": True, "pdftoppm": True},
                pilot_readiness_review=review_path,
                pilot_data_handoff_review=handoff_review_path,
            )
        check = next(
            item for item in report["checks"]
            if item["check_id"] == "pilot.readiness_activation"
        )
        self.assertEqual(check["status"], "pass")
        self.assertTrue(report["ready_for_bounded_shadow"])
        self.assertTrue(report["ready_for_controlled_data_intake"])
        handoff_check = next(
            item for item in report["checks"]
            if item["check_id"] == "pilot.data_handoff_activation"
        )
        self.assertEqual(handoff_check["status"], "pass")
        serialized = json.dumps(report)
        self.assertNotIn("pilot-reviewer", serialized)
        self.assertNotIn("advisor://", serialized)


if __name__ == "__main__":
    unittest.main()
