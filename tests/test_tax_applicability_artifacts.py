from __future__ import annotations

import json
import stat
import tempfile
import unittest
from pathlib import Path

from src.box_runtime import BoxRuntime
from src.tax_applicability_artifacts import (
    TaxApplicabilityArtifactError,
    build_tax_applicability_workpaper,
    build_tax_applicability_registry_alerts,
    import_tax_applicability_review,
    inspect_tax_applicability_review_directory,
    review_tax_applicability_workpaper,
    validate_tax_applicability_review,
    validate_tax_applicability_workpaper,
    verify_tax_applicability_review,
    verify_tax_applicability_review_portfolio,
    verify_tax_applicability_registry_receipt,
    write_tax_applicability_registry_receipt,
    write_tax_applicability_workpaper,
)


ROOT = Path(__file__).resolve().parents[1]
PACKS = ROOT / "packs"
GAME_BOX = ROOT / "examples" / "boxes" / "global_game_studio.json"


class TaxApplicabilityArtifactTests(unittest.TestCase):
    def setUp(self):
        self.runtime = BoxRuntime(GAME_BOX, PACKS)

    @staticmethod
    def _answer_in_scope(workpaper):
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
                f"evidence://tax-case/{question['question_id']}"
            ]
        return workpaper

    @staticmethod
    def _write(path, value):
        path.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        path.chmod(0o600)

    def test_private_entity_workpaper_is_box_and_pack_bound(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "cn-workpaper.json"
            result = write_tax_applicability_workpaper(
                self.runtime, "cn_studio", prepared_by="tax-operator",
                facts_as_of="2026-08-14", output=output,
            )
            value = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
            self.assertEqual(result["entity_id"], "cn_studio")
            self.assertEqual(value["entity"]["pack_id"], "jurisdiction.cn_mainland")
            self.assertEqual(value["entity"]["question_count"], 5)
            self.assertFalse(value["raw_tax_identifiers_included"])
            self.assertTrue(all(
                item["answer"] is None and item["evidence_references"] == []
                for item in value["entity"]["questions"]
            ))
            with self.assertRaisesRegex(
                TaxApplicabilityArtifactError, "refusing to overwrite",
            ):
                write_tax_applicability_workpaper(
                    self.runtime, "cn_studio", prepared_by="tax-operator",
                    facts_as_of="2026-08-14", output=output,
                )

    def test_independent_approved_review_verifies_without_returning_answers(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workpaper_path = Path(temp_dir) / "workpaper.json"
            review_path = Path(temp_dir) / "review.json"
            workpaper = self._answer_in_scope(build_tax_applicability_workpaper(
                self.runtime, "sg_publisher", prepared_by="tax-operator",
                facts_as_of="2026-08-14",
            ))
            self._write(workpaper_path, workpaper)
            result = review_tax_applicability_workpaper(
                self.runtime,
                workpaper_path,
                review_path,
                decision="approved-in-scope",
                actor="sg-local-tax-reviewer",
                rationale="主体、居民身份、登记、期间和特殊制度均已依据私有材料复核。",
                evidence_references=["advisor://sg-review/memo-2026"],
            )
            verified = verify_tax_applicability_review(self.runtime, review_path)
            self.assertTrue(result["applicability_gate_passed"])
            self.assertTrue(verified["valid"])
            self.assertEqual(verified["entity_id"], "sg_publisher")
            self.assertEqual(verified["decision"], "approved-in-scope")
            self.assertEqual(verified["unanswered_count"], 0)
            self.assertFalse(verified["answers_returned"])
            self.assertFalse(verified["review_rationale_returned"])
            self.assertFalse(verified["evidence_references_returned"])
            self.assertNotIn("entity", verified)
            with self.assertRaisesRegex(
                TaxApplicabilityArtifactError, "coverage is incomplete",
            ):
                verify_tax_applicability_review_portfolio(
                    self.runtime, [review_path],
                )
            with self.assertRaisesRegex(
                TaxApplicabilityArtifactError, "duplicate review",
            ):
                verify_tax_applicability_review_portfolio(
                    self.runtime, [review_path, review_path],
                )

    def test_review_lifecycle_is_bound_to_facts_date_and_pack_policy(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workpaper_path = Path(temp_dir) / "workpaper.json"
            review_path = Path(temp_dir) / "review.json"
            workpaper = self._answer_in_scope(build_tax_applicability_workpaper(
                self.runtime, "sg_publisher", prepared_by="tax-operator",
                facts_as_of="2026-08-14",
            ))
            self._write(workpaper_path, workpaper)
            review_tax_applicability_workpaper(
                self.runtime, workpaper_path, review_path,
                decision="approved-in-scope",
                actor="sg-local-tax-reviewer",
                rationale="依据截至事实日的主体与登记材料完成复核。",
                evidence_references=["advisor://sg-review/lifecycle-memo"],
            )
            current = verify_tax_applicability_review(
                self.runtime, review_path, as_of="2026-08-14",
            )
            due = verify_tax_applicability_review(
                self.runtime, review_path, as_of="2027-07-15",
            )
            expired = verify_tax_applicability_review(
                self.runtime, review_path, as_of="2027-08-15",
            )
            self.assertEqual(current["lifecycle_status"], "current")
            self.assertEqual(current["review_due_at"], "2027-07-15")
            self.assertEqual(current["expires_at"], "2027-08-14")
            self.assertEqual(due["lifecycle_status"], "review_due")
            self.assertTrue(due["applicability_gate_passed"])
            self.assertEqual(expired["lifecycle_status"], "expired")
            self.assertFalse(expired["applicability_gate_passed"])
            self.assertTrue(expired["decision_gate_passed"])

    def test_facts_and_verification_dates_fail_closed(self):
        with self.assertRaisesRegex(
            TaxApplicabilityArtifactError, "cannot predate.*rules_verified_at",
        ):
            build_tax_applicability_workpaper(
                self.runtime, "cn_studio", prepared_by="tax-operator",
                facts_as_of="2026-08-12",
            )
        with self.assertRaisesRegex(TaxApplicabilityArtifactError, "future"):
            build_tax_applicability_workpaper(
                self.runtime, "cn_studio", prepared_by="tax-operator",
                facts_as_of="9999-12-31",
            )
        with tempfile.TemporaryDirectory() as temp_dir:
            workpaper_path = Path(temp_dir) / "workpaper.json"
            review_path = Path(temp_dir) / "review.json"
            workpaper = self._answer_in_scope(build_tax_applicability_workpaper(
                self.runtime, "cn_studio", prepared_by="tax-operator",
                facts_as_of="2026-08-14",
            ))
            self._write(workpaper_path, workpaper)
            review_tax_applicability_workpaper(
                self.runtime, workpaper_path, review_path,
                decision="approved-in-scope", actor="local-tax-reviewer",
                rationale="完成独立复核。",
                evidence_references=["advisor://cn-review/date-boundary"],
            )
            with self.assertRaisesRegex(
                TaxApplicabilityArtifactError, "as_of cannot predate facts_as_of",
            ):
                verify_tax_applicability_review(
                    self.runtime, review_path, as_of="2026-08-13",
                )

    def test_review_directory_status_is_entity_exact_private_and_safe(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            review_dir = root / "reviews"
            review_dir.mkdir()
            workpaper_path = root / "workpaper.json"
            review_path = review_dir / "cn_studio.json"
            workpaper = self._answer_in_scope(build_tax_applicability_workpaper(
                self.runtime, "cn_studio", prepared_by="tax-operator",
                facts_as_of="2026-08-14",
            ))
            self._write(workpaper_path, workpaper)
            review_tax_applicability_workpaper(
                self.runtime, workpaper_path, review_path,
                decision="approved-in-scope", actor="independent-reviewer",
                rationale="完成轮换目录检查。",
                evidence_references=["advisor://registry/review"],
            )
            status = inspect_tax_applicability_review_directory(
                self.runtime, review_dir, as_of="2026-08-14",
            )
            self.assertEqual(status["counts"]["current"], 1)
            self.assertEqual(status["counts"]["missing"], 1)
            self.assertFalse(status["ready_for_calendar_release"])
            self.assertFalse(status["paths_returned"])
            self.assertNotIn("完成轮换目录检查", json.dumps(status, ensure_ascii=False))

            review_path.chmod(0o644)
            invalid = inspect_tax_applicability_review_directory(
                self.runtime, review_dir, as_of="2026-08-14",
            )
            self.assertEqual(invalid["counts"]["invalid"], 1)
            self.assertEqual(invalid["counts"]["missing"], 1)

    def test_review_import_is_entity_exact_private_safe_and_non_overwriting(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            review_dir = root / "reviews"
            review_dir.mkdir()
            workpaper_path = root / "workpaper.json"
            source_review = root / "incoming-review.json"
            workpaper = self._answer_in_scope(build_tax_applicability_workpaper(
                self.runtime, "cn_studio", prepared_by="tax-operator",
                facts_as_of="2026-08-14",
            ))
            self._write(workpaper_path, workpaper)
            review_tax_applicability_workpaper(
                self.runtime, workpaper_path, source_review,
                decision="approved-in-scope", actor="independent-reviewer",
                rationale="只应保留在私有源工件。",
                evidence_references=["advisor://registry/import-review"],
            )
            result = import_tax_applicability_review(
                self.runtime, source_review, review_dir, as_of="2026-08-14",
            )
            installed = review_dir / "cn_studio.json"
            self.assertTrue(installed.is_file())
            self.assertEqual(stat.S_IMODE(installed.stat().st_mode), 0o600)
            self.assertEqual(result["entity_id"], "cn_studio")
            self.assertEqual(result["registry_counts"]["current"], 1)
            self.assertFalse(result["overwrite_performed"])
            self.assertFalse(result["paths_returned"])
            self.assertNotIn("只应保留", json.dumps(result, ensure_ascii=False))
            with self.assertRaisesRegex(
                TaxApplicabilityArtifactError, "refusing to overwrite",
            ):
                import_tax_applicability_review(
                    self.runtime, source_review, review_dir, as_of="2026-08-14",
                )

            dirty_dir = root / "dirty"
            dirty_dir.mkdir()
            (dirty_dir / ".DS_Store").touch()
            with self.assertRaisesRegex(
                TaxApplicabilityArtifactError, "unexpected entries",
            ):
                import_tax_applicability_review(
                    self.runtime, source_review, dirty_dir, as_of="2026-08-14",
                )

    def test_registry_receipt_binds_exact_content_and_separates_controller(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            review_dir = root / "reviews"
            review_dir.mkdir()
            source_by_entity = {}
            for entity_id in ("cn_studio", "sg_publisher"):
                workpaper = self._answer_in_scope(build_tax_applicability_workpaper(
                    self.runtime, entity_id,
                    prepared_by=f"{entity_id}-preparer", facts_as_of="2026-08-14",
                ))
                workpaper_path = root / f"{entity_id}-workpaper.json"
                source_review = root / f"{entity_id}-source.json"
                self._write(workpaper_path, workpaper)
                review_tax_applicability_workpaper(
                    self.runtime, workpaper_path, source_review,
                    decision="approved-in-scope",
                    actor=f"{entity_id}-reviewer",
                    rationale=f"PRIVATE-{entity_id}-RATIONALE",
                    evidence_references=[f"advisor://{entity_id}/review"],
                )
                import_tax_applicability_review(
                    self.runtime, source_review, review_dir, as_of="2026-08-14",
                )
                source_by_entity[entity_id] = source_review

            receipt_path = root / "registry-receipt.json"
            with self.assertRaisesRegex(
                TaxApplicabilityArtifactError, "differ from every preparer and reviewer",
            ):
                write_tax_applicability_registry_receipt(
                    self.runtime, review_dir, receipt_path,
                    actor="cn_studio-reviewer", as_of="2026-08-14",
                )
            created = write_tax_applicability_registry_receipt(
                self.runtime, review_dir, receipt_path,
                actor="registry-controller", as_of="2026-08-14",
            )
            self.assertEqual(stat.S_IMODE(receipt_path.stat().st_mode), 0o600)
            self.assertTrue(created["controller_role_separation_verified"])
            self.assertFalse(created["digital_signature_performed"])
            self.assertFalse(created["filing_authorization_granted"])
            self.assertNotIn("PRIVATE-", json.dumps(created))

            current = verify_tax_applicability_registry_receipt(
                self.runtime, review_dir, receipt_path, as_of="2026-08-14",
            )
            due = verify_tax_applicability_registry_receipt(
                self.runtime, review_dir, receipt_path, as_of="2027-07-15",
            )
            expired = verify_tax_applicability_registry_receipt(
                self.runtime, review_dir, receipt_path, as_of="2027-08-15",
            )
            self.assertTrue(current["valid"])
            self.assertTrue(current["registry_unchanged"])
            self.assertEqual(due["counts"]["review_due"], 2)
            self.assertTrue(due["ready_for_calendar_release"])
            self.assertEqual(expired["counts"]["expired"], 2)
            self.assertFalse(expired["ready_for_calendar_release"])
            self.assertFalse(expired["digital_signature_verified"])
            current_alerts = build_tax_applicability_registry_alerts(
                self.runtime, review_dir,
                receipt_json=receipt_path, as_of="2026-08-14",
            )
            due_alerts = build_tax_applicability_registry_alerts(
                self.runtime, review_dir,
                receipt_json=receipt_path, as_of="2027-07-15",
            )
            expired_alerts = build_tax_applicability_registry_alerts(
                self.runtime, review_dir,
                receipt_json=receipt_path, as_of="2027-08-15",
            )
            missing_receipt_alerts = build_tax_applicability_registry_alerts(
                self.runtime, review_dir, as_of="2026-08-14",
            )
            self.assertEqual(current_alerts["alert_count"], 0)
            self.assertTrue(current_alerts["ready_for_calendar_release"])
            self.assertEqual(due_alerts["warning_count"], 2)
            self.assertEqual(expired_alerts["critical_count"], 2)
            self.assertFalse(expired_alerts["notifications_sent"])
            self.assertEqual(missing_receipt_alerts["critical_count"], 1)
            self.assertEqual(
                missing_receipt_alerts["alerts"][0]["status"], "missing"
            )

            alternate_workpaper = self._answer_in_scope(
                build_tax_applicability_workpaper(
                    self.runtime, "sg_publisher", prepared_by="alternate-preparer",
                    facts_as_of="2026-08-14",
                )
            )
            alternate_workpaper_path = root / "alternate-workpaper.json"
            alternate_review_path = root / "alternate-review.json"
            self._write(alternate_workpaper_path, alternate_workpaper)
            review_tax_applicability_workpaper(
                self.runtime, alternate_workpaper_path, alternate_review_path,
                decision="approved-in-scope", actor="alternate-reviewer",
                rationale="A distinct but valid replacement review.",
                evidence_references=["advisor://alternate/review"],
            )
            alternate_review_path.replace(review_dir / "sg_publisher.json")
            with self.assertRaisesRegex(
                TaxApplicabilityArtifactError, "no longer matches its receipt",
            ):
                verify_tax_applicability_registry_receipt(
                    self.runtime, review_dir, receipt_path, as_of="2026-08-14",
                )

    def test_approval_requires_complete_supported_answers_and_independent_actor(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workpaper_path = Path(temp_dir) / "workpaper.json"
            workpaper = build_tax_applicability_workpaper(
                self.runtime, "cn_studio", prepared_by="same-actor",
                facts_as_of="2026-08-14",
            )
            self._write(workpaper_path, workpaper)
            with self.assertRaisesRegex(
                TaxApplicabilityArtifactError, "complete evidence-backed",
            ):
                review_tax_applicability_workpaper(
                    self.runtime,
                    workpaper_path,
                    Path(temp_dir) / "incomplete.json",
                    decision="approved-in-scope",
                    actor="local-reviewer",
                    rationale="Incomplete workpaper must not pass.",
                    evidence_references=["advisor://review/memo"],
                )
            self._answer_in_scope(workpaper)
            self._write(Path(temp_dir) / "complete.json", workpaper)
            with self.assertRaisesRegex(
                TaxApplicabilityArtifactError, "must differ",
            ):
                review_tax_applicability_workpaper(
                    self.runtime,
                    Path(temp_dir) / "complete.json",
                    Path(temp_dir) / "same-actor.json",
                    decision="approved-in-scope",
                    actor="same-actor",
                    rationale="Self-review is not allowed.",
                    evidence_references=["advisor://review/memo"],
                )

    def test_out_of_scope_and_needs_correction_are_not_release_approvals(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            out_workpaper = self._answer_in_scope(build_tax_applicability_workpaper(
                self.runtime, "cn_studio", prepared_by="tax-operator",
                facts_as_of="2026-08-14",
            ))
            out_workpaper["entity"]["questions"][0]["answer"] = "confirmed_out_of_scope"
            out_path = Path(temp_dir) / "out-workpaper.json"
            out_review = Path(temp_dir) / "out-review.json"
            self._write(out_path, out_workpaper)
            review_tax_applicability_workpaper(
                self.runtime,
                out_path,
                out_review,
                decision="confirmed-out-of-scope",
                actor="local-tax-reviewer",
                rationale="法律形式不在当前 Pack 授权范围内。",
                evidence_references=["registry://entity/legal-form"],
            )
            self.assertFalse(
                verify_tax_applicability_review(self.runtime, out_review)[
                    "applicability_gate_passed"
                ]
            )

            incomplete = build_tax_applicability_workpaper(
                self.runtime, "sg_publisher", prepared_by="tax-operator",
                facts_as_of="2026-08-14",
            )
            incomplete_path = Path(temp_dir) / "incomplete.json"
            correction_path = Path(temp_dir) / "correction.json"
            self._write(incomplete_path, incomplete)
            review_tax_applicability_workpaper(
                self.runtime,
                incomplete_path,
                correction_path,
                decision="needs-correction",
                actor="local-tax-reviewer",
                rationale="需要补齐居民身份及登记证据。",
                evidence_references=["advisor://review/open-items"],
            )
            correction = verify_tax_applicability_review(
                self.runtime, correction_path,
            )
            self.assertEqual(correction["unanswered_count"], 5)
            self.assertFalse(correction["applicability_gate_passed"])

    def test_unknown_fields_raw_reference_and_tampering_fail_closed(self):
        workpaper = build_tax_applicability_workpaper(
            self.runtime, "cn_studio", prepared_by="tax-operator",
            facts_as_of="2026-08-14",
        )
        workpaper["entity"]["tax_number"] = "sensitive"
        with self.assertRaisesRegex(
            TaxApplicabilityArtifactError, "entity fields",
        ):
            validate_tax_applicability_workpaper(self.runtime, workpaper)

        workpaper = self._answer_in_scope(build_tax_applicability_workpaper(
            self.runtime, "cn_studio", prepared_by="tax-operator",
            facts_as_of="2026-08-14",
        ))
        workpaper["entity"]["questions"][0]["evidence_references"] = ["raw-tax-number"]
        with self.assertRaisesRegex(
            TaxApplicabilityArtifactError, "opaque evidence",
        ):
            validate_tax_applicability_workpaper(self.runtime, workpaper)

        with tempfile.TemporaryDirectory() as temp_dir:
            workpaper = self._answer_in_scope(build_tax_applicability_workpaper(
                self.runtime, "cn_studio", prepared_by="tax-operator",
                facts_as_of="2026-08-14",
            ))
            workpaper_path = Path(temp_dir) / "workpaper.json"
            review_path = Path(temp_dir) / "review.json"
            self._write(workpaper_path, workpaper)
            review_tax_applicability_workpaper(
                self.runtime,
                workpaper_path,
                review_path,
                decision="approved-in-scope",
                actor="local-tax-reviewer",
                rationale="完整复核。",
                evidence_references=["advisor://review/memo"],
            )
            reviewed = json.loads(review_path.read_text(encoding="utf-8"))
            reviewed["entity"]["questions"][0]["evidence_references"] = [
                "evidence://different/file"
            ]
            with self.assertRaisesRegex(
                TaxApplicabilityArtifactError, "fingerprint mismatch",
            ):
                validate_tax_applicability_review(self.runtime, reviewed)


if __name__ == "__main__":
    unittest.main()
