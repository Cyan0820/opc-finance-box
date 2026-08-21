from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from src.box_pipeline import dispatch_box_pipeline_request
from src.box_runtime import BoxRuntime
from src.connector_shadow_artifacts import (
    ConnectorShadowArtifactError,
    assess_connector_shadow_artifacts,
    build_connector_shadow_baseline_workpaper,
    finalize_connector_shadow_baseline_workpaper,
    review_connector_shadow_artifact,
    write_xero_shadow_observation,
)
from src.release_promotion import _validate_connector_shadow_promotion_artifact


ROOT = Path(__file__).resolve().parents[1]
BOX = ROOT / "examples" / "boxes" / "global_game_studio_xero.json"
FIXTURE = ROOT / "packs" / "connectors" / "xero" / "fixture-trial-balance.json"


class XeroConnectorShadowTests(unittest.TestCase):
    def setUp(self):
        self.runtime = BoxRuntime(BOX, ROOT / "packs")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def _real_pipeline_result(self) -> dict:
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        result = dispatch_box_pipeline_request(self.runtime, {
            "pipeline_id": "finance.trial_balance_review",
            "payload": {
                "entity_id": "cn_studio",
                "period": "2026-07",
                "connector_id": "xero.trial_balance",
                "connector_request": fixture,
            },
        })
        source = result["batch"]["source"]
        source.update({"kind": "api", "network_access_performed": True})
        result["connector_batches"]["xero.trial_balance"]["source"] = source
        result["network_access_performed"] = True
        return result

    def _real_baseline(self) -> Path:
        workpaper_path = Path(self.temp.name) / "xero-workpaper.json"
        created = build_connector_shadow_baseline_workpaper(
            self.runtime,
            pipeline_id="finance.trial_balance_review",
            entity_id="cn_studio",
            sample_period="2026-07",
            prepared_by="independent-xero-source-preparer",
            output=workpaper_path,
        )
        self.assertEqual(created["source_count"], 1)
        self.assertEqual(created["control_count"], 14)
        workpaper = json.loads(workpaper_path.read_text(encoding="utf-8"))
        workpaper["source_independence"] = {
            "prepared_from_independent_source": True,
            "pipeline_output_used_as_baseline": False,
            "source_scope_confirmed": True,
        }
        workpaper["anonymization"]["private_source_evidence_retained"] = True
        workpaper["source_expectations"][0]["expected_record_count"] = 2
        workpaper["source_expectations"][0]["evidence_references"] = [
            "private-export://xero/cn-studio/2026-07/trial-balance",
        ]
        expected_controls = {
            "pipeline_ready": True,
            "trial_balance_line_count": 2,
            "scope_count": 1,
            "balanced_scope_count": 1,
            "unbalanced_scope_count": 0,
            "roll_forward_checked_scope_count": 0,
            "network_snapshot_performed": True,
            "as_at_period_end": True,
            "payments_only_disabled": True,
            "entity_currency_binding_matched": True,
            "point_in_time_snapshot": True,
            "opening_and_period_movements_absent": True,
            "ytd_columns_preserved_separately": True,
            "external_actions_disabled": True,
        }
        for item in workpaper["control_expectations"]:
            item["expected_value"] = expected_controls[item["control_id"]]
        workpaper["evidence_references"] = [
            "workpaper://xero/cn-studio/2026-07/count-tie-out",
            "review://xero/cn-studio/2026-07/entity-currency-scope",
        ]
        workpaper["finalization_ready"] = True
        workpaper_path.write_text(json.dumps(workpaper), encoding="utf-8")
        baseline_path = Path(self.temp.name) / "xero-baseline.json"
        finalized = finalize_connector_shadow_baseline_workpaper(
            self.runtime, workpaper_path, baseline_path,
        )
        self.assertTrue(finalized["real_sample_evidence"])
        return baseline_path

    def test_real_xero_observation_is_amount_and_identifier_free_and_assesses(self):
        baseline_path = self._real_baseline()
        result = self._real_pipeline_result()
        observation_path = Path(self.temp.name) / "xero-observation.json"
        summary = write_xero_shadow_observation(
            self.runtime, result, observation_path,
        )
        self.assertEqual(summary["trial_balance_line_count"], 2)
        self.assertEqual(summary["scope_count"], 1)
        self.assertEqual(oct(observation_path.stat().st_mode & 0o777), "0o600")
        observation = json.loads(observation_path.read_text(encoding="utf-8"))
        def without_opaque_hashes(value):
            if isinstance(value, dict):
                return {
                    key: without_opaque_hashes(item)
                    for key, item in value.items()
                    if "hash" not in key and "fingerprint" not in key
                }
            if isinstance(value, list):
                return [without_opaque_hashes(item) for item in value]
            return value

        # Opaque digests can coincidentally contain a short account code or amount
        # substring. Inspect the observable payload after removing digest fields so
        # the privacy assertion remains deterministic and still covers every
        # human-readable/source field.
        serialized = json.dumps(without_opaque_hashes(observation), ensure_ascii=False)
        for forbidden in (
            "1000.0", "1200.0", "cash at bank", "owner capital", "1001", "3001",
            "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            "source_object_id_sha256",
        ):
            self.assertNotIn(forbidden, serialized.lower())
        self.assertFalse(observation["financial_amounts_included"])
        expected_private_hash = hashlib.sha256(json.dumps(
            result, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest()
        self.assertEqual(observation["private_pipeline_result_sha256"], expected_private_hash)

        assessment_path = Path(self.temp.name) / "xero-assessment.json"
        assessed = assess_connector_shadow_artifacts(
            self.runtime, baseline_path, observation_path, assessment_path,
        )
        self.assertTrue(assessed["passed"])
        self.assertEqual(assessed["control_count"], 14)
        stored = json.loads(assessment_path.read_text(encoding="utf-8"))
        self.assertEqual(stored["pipeline_result_sha256"], expected_private_hash)
        reviewed_path = Path(self.temp.name) / "xero-reviewed.json"
        review_connector_shadow_artifact(
            self.runtime, assessment_path, reviewed_path,
            decision="passed", actor="independent-xero-shadow-reviewer",
            rationale="真实匿名 Xero 月末快照的主体、期间、计数及只读控制已独立复核",
            evidence_references=["review://xero/cn-studio/2026-07/final"],
        )
        promotion_summary = _validate_connector_shadow_promotion_artifact(
            self.runtime, reviewed_path, pack_id="connector.xero",
            clock=datetime.now(timezone.utc), maximum_age_days=30,
        )
        self.assertEqual(promotion_summary["covered_pack_ids"], ["connector.xero"])
        self.assertTrue(promotion_summary["passed"])
        self.assertEqual(promotion_summary["control_count"], 14)

    def test_xero_observation_rejects_fixture_non_month_end_cross_currency_and_tampering(self):
        result = self._real_pipeline_result()
        fixture_result = json.loads(json.dumps(result))
        fixture_result["batch"]["source"]["kind"] = "fixture"
        with self.assertRaisesRegex(ConnectorShadowArtifactError, "real read-only network"):
            write_xero_shadow_observation(
                self.runtime, fixture_result, Path(self.temp.name) / "fixture.json",
            )

        wrong_date = json.loads(json.dumps(result))
        wrong_date["batch"]["source"]["as_at"] = "2026-07-30"
        with self.assertRaisesRegex(ConnectorShadowArtifactError, "scope, binding"):
            write_xero_shadow_observation(
                self.runtime, wrong_date, Path(self.temp.name) / "wrong-date.json",
            )

        wrong_currency = json.loads(json.dumps(result))
        wrong_currency["batch"]["source"]["base_currency"] = "USD"
        with self.assertRaisesRegex(ConnectorShadowArtifactError, "scope, binding"):
            write_xero_shadow_observation(
                self.runtime, wrong_currency, Path(self.temp.name) / "wrong-currency.json",
            )

        baseline_path = self._real_baseline()
        observation_path = Path(self.temp.name) / "valid-observation.json"
        write_xero_shadow_observation(self.runtime, result, observation_path)
        observation = json.loads(observation_path.read_text(encoding="utf-8"))
        observation["ready"] = not observation["ready"]
        tampered_path = Path(self.temp.name) / "tampered-observation.json"
        tampered_path.write_text(json.dumps(observation), encoding="utf-8")
        with self.assertRaisesRegex(ConnectorShadowArtifactError, "integrity or privacy"):
            assess_connector_shadow_artifacts(
                self.runtime, baseline_path, tampered_path,
                Path(self.temp.name) / "never.json",
            )

    def test_real_xero_baseline_rejects_cash_basis_or_roll_forward_overclaim(self):
        workpaper_path = Path(self.temp.name) / "invalid-workpaper.json"
        build_connector_shadow_baseline_workpaper(
            self.runtime,
            pipeline_id="finance.trial_balance_review",
            entity_id="cn_studio",
            sample_period="2026-07",
            prepared_by="independent-xero-source-preparer",
            output=workpaper_path,
        )
        workpaper = json.loads(workpaper_path.read_text(encoding="utf-8"))
        workpaper["source_independence"] = {
            "prepared_from_independent_source": True,
            "pipeline_output_used_as_baseline": False,
            "source_scope_confirmed": True,
        }
        workpaper["anonymization"]["private_source_evidence_retained"] = True
        workpaper["source_expectations"][0].update({
            "expected_record_count": 2,
            "evidence_references": ["private-export://xero/cn-studio/2026-07/tb"],
        })
        for item in workpaper["control_expectations"]:
            item["expected_value"] = False if item["control_id"] == "payments_only_disabled" else (
                0 if item["control_id"].endswith("count") else True
            )
        controls = {item["control_id"]: item for item in workpaper["control_expectations"]}
        controls["trial_balance_line_count"]["expected_value"] = 2
        controls["scope_count"]["expected_value"] = 1
        controls["balanced_scope_count"]["expected_value"] = 1
        workpaper["evidence_references"] = ["workpaper://xero/cn-studio/2026-07/tb"]
        workpaper["finalization_ready"] = True
        workpaper_path.write_text(json.dumps(workpaper), encoding="utf-8")
        with self.assertRaisesRegex(ConnectorShadowArtifactError, "month-end, accrual-basis"):
            finalize_connector_shadow_baseline_workpaper(
                self.runtime, workpaper_path, Path(self.temp.name) / "invalid-baseline.json",
            )


if __name__ == "__main__":
    unittest.main()
