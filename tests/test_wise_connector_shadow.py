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
    write_wise_shadow_observation,
)
from src.release_promotion import _validate_connector_shadow_promotion_artifact


ROOT = Path(__file__).resolve().parents[1]
BOX = ROOT / "examples" / "boxes" / "sg_dtc_wise_store.json"
FIXTURE = ROOT / "packs" / "connectors" / "wise" / "fixture-balance-statement.json"


class WiseConnectorShadowTests(unittest.TestCase):
    def setUp(self):
        self.runtime = BoxRuntime(BOX, ROOT / "packs")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def _pipeline_result(self) -> dict:
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        result = dispatch_box_pipeline_request(self.runtime, {
            "pipeline_id": "finance.bank_statement_close",
            "payload": {
                "entity_id": "sg_store",
                "period": "2026-07",
                "connector_id": "wise.balance_statement",
                "connector_request": fixture,
            },
        })
        source = result["batch"]["source"]
        source.update({"kind": "api", "network_access_performed": True})
        result["connector_batches"]["wise.balance_statement"]["source"] = source
        result["network_access_performed"] = True
        return result

    def _baseline(self) -> Path:
        workpaper_path = Path(self.temp.name) / "wise-workpaper.json"
        created = build_connector_shadow_baseline_workpaper(
            self.runtime,
            pipeline_id="finance.bank_statement_close",
            entity_id="sg_store",
            sample_period="2026-07",
            prepared_by="independent-wise-source-preparer",
            output=workpaper_path,
        )
        self.assertEqual(created["control_count"], 13)
        workpaper = json.loads(workpaper_path.read_text(encoding="utf-8"))
        workpaper["source_independence"] = {
            "prepared_from_independent_source": True,
            "pipeline_output_used_as_baseline": False,
            "source_scope_confirmed": True,
        }
        workpaper["anonymization"]["private_source_evidence_retained"] = True
        workpaper["source_expectations"][0].update({
            "expected_record_count": 2,
            "evidence_references": [
                "private-export://wise/sg-store/2026-07/balance-statement",
            ],
        })
        expected = {
            "pipeline_ready": True,
            "bank_transaction_count": 2,
            "account_scope_count": 1,
            "pending_transaction_count": 2,
            "network_statement_performed": True,
            "monthly_half_open_window": True,
            "entity_currency_binding_matched": True,
            "business_profile_verified": True,
            "compact_english_statement": True,
            "opening_closing_balance_controls_present": True,
            "reconciliation_candidate_only": True,
            "bank_balance_unconfirmed_without_review": True,
            "external_actions_disabled": True,
        }
        for item in workpaper["control_expectations"]:
            item["expected_value"] = expected[item["control_id"]]
        workpaper["evidence_references"] = [
            "workpaper://wise/sg-store/2026-07/count-and-balance-tie-out",
        ]
        workpaper["finalization_ready"] = True
        workpaper_path.write_text(json.dumps(workpaper), encoding="utf-8")
        baseline_path = Path(self.temp.name) / "wise-baseline.json"
        finalize_connector_shadow_baseline_workpaper(
            self.runtime, workpaper_path, baseline_path,
        )
        return baseline_path

    def test_real_observation_is_amount_account_and_counterparty_free_and_assesses(self):
        result = self._pipeline_result()
        observation_path = Path(self.temp.name) / "wise-observation.json"
        summary = write_wise_shadow_observation(self.runtime, result, observation_path)
        self.assertEqual(summary["bank_transaction_count"], 2)
        self.assertEqual(summary["account_scope_count"], 1)
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

        serialized = json.dumps(without_opaque_hashes(observation), ensure_ascii=False).lower()
        for forbidden in (
            "1000.0", "1650.0", "850.0", "200.0", "demo customer",
            "demo supplier", "wise sgd", "7654", "123456", "987654",
            "wise-ref-credit-001", "account_reference_masked",
        ):
            self.assertNotIn(forbidden, serialized)
        def keys(value):
            if isinstance(value, dict):
                return set(value).union(*(keys(item) for item in value.values()))
            if isinstance(value, list):
                return set().union(*(keys(item) for item in value)) if value else set()
            return set()

        self.assertTrue({
            "opening_balance", "closing_balance", "amount", "balance", "fee_amount",
            "counterparty", "summary", "account_masked", "bank_transaction_id",
            "transaction_id", "source_object_fingerprint",
        }.isdisjoint(keys(observation)))
        self.assertFalse(observation["financial_amounts_included"])
        expected_hash = hashlib.sha256(json.dumps(
            result, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest()
        self.assertEqual(observation["private_pipeline_result_sha256"], expected_hash)

        assessment_path = Path(self.temp.name) / "wise-assessment.json"
        assessed = assess_connector_shadow_artifacts(
            self.runtime, self._baseline(), observation_path, assessment_path,
        )
        self.assertTrue(assessed["passed"])
        self.assertEqual(assessed["control_count"], 13)
        reviewed_path = Path(self.temp.name) / "wise-reviewed.json"
        review_connector_shadow_artifact(
            self.runtime, assessment_path, reviewed_path,
            decision="passed", actor="independent-wise-shadow-reviewer",
            rationale="真实匿名 Wise 月度流水的范围、余额连续性和只读调节控制已复核",
            evidence_references=["review://wise/sg-store/2026-07/final"],
        )
        promotion = _validate_connector_shadow_promotion_artifact(
            self.runtime, reviewed_path, pack_id="connector.wise",
            clock=datetime.now(timezone.utc), maximum_age_days=30,
        )
        self.assertEqual(promotion["covered_pack_ids"], ["connector.wise"])
        self.assertTrue(promotion["passed"])
        self.assertEqual(promotion["control_count"], 13)

    def test_observation_rejects_fixture_wrong_window_and_tampering(self):
        result = self._pipeline_result()
        fixture = json.loads(json.dumps(result))
        fixture["batch"]["source"]["kind"] = "fixture"
        with self.assertRaisesRegex(ConnectorShadowArtifactError, "real read-only network"):
            write_wise_shadow_observation(
                self.runtime, fixture, Path(self.temp.name) / "fixture.json",
            )
        wrong_window = json.loads(json.dumps(result))
        wrong_window["batch"]["source"]["interval_end"] = "2026-07-31T00:00:00Z"
        with self.assertRaisesRegex(ConnectorShadowArtifactError, "scope, binding"):
            write_wise_shadow_observation(
                self.runtime, wrong_window, Path(self.temp.name) / "wrong-window.json",
            )
        observation_path = Path(self.temp.name) / "valid.json"
        write_wise_shadow_observation(self.runtime, result, observation_path)
        observation = json.loads(observation_path.read_text(encoding="utf-8"))
        observation["services"]["bank_reconciliation_candidate"]["output"]["complete"] = True
        tampered = Path(self.temp.name) / "tampered.json"
        tampered.write_text(json.dumps(observation), encoding="utf-8")
        with self.assertRaisesRegex(ConnectorShadowArtifactError, "integrity or privacy"):
            assess_connector_shadow_artifacts(
                self.runtime, self._baseline(), tampered,
                Path(self.temp.name) / "never.json",
            )

    def test_provider_fails_closed_when_running_balance_does_not_reconcile(self):
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        fixture["statement"]["transactions"][0]["runningBalance"]["value"] += 1
        with self.assertRaisesRegex(Exception, "running balance does not reconcile"):
            dispatch_box_pipeline_request(self.runtime, {
                "pipeline_id": "finance.bank_statement_close",
                "payload": {
                    "entity_id": "sg_store", "period": "2026-07",
                    "connector_id": "wise.balance_statement",
                    "connector_request": fixture,
                },
            })
