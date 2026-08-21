from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from src.box_pipeline import dispatch_box_pipeline_request
from src.box_runtime import BoxRuntime
from src.connector_shadow_artifacts import (
    assess_connector_shadow_artifacts,
    review_connector_shadow_artifact,
)
from src.connector_shadow_registry import build_connector_shadow_registry_workspace
from src.default_services import build_default_service_registry
from src.production_readiness import build_production_readiness_workspace


ROOT = Path(__file__).resolve().parents[1]
BOX = ROOT / "examples" / "boxes" / "sg_dtc_shopify_stripe_wise_store.json"
GAME_BOX = ROOT / "examples" / "boxes" / "global_game_studio.json"
BASELINE = ROOT / "examples" / "shadow" / "sg_shopify_stripe_wise_connector_baseline.json"
REQUEST = ROOT / "examples" / "pipelines" / "shopify_stripe_wise_daily_close_fixture.json"


def _hash(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class ConnectorShadowRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.registry = self.root / "registry"
        self.registry.mkdir(mode=0o700)
        self.runtime = BoxRuntime(BOX, ROOT / "packs")

    def _reviewed_artifact(self, name: str = "reviewed.json") -> Path:
        baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
        baseline.update({
            "schema_version": 2,
            "sample_classification": "real_anonymized",
            "source_independence": {
                "prepared_from_independent_source": True,
                "pipeline_output_used_as_baseline": False,
                "source_scope_confirmed": True,
            },
            "anonymization": {
                "raw_identifiers_removed_from_baseline": True,
                "financial_amounts_removed_from_baseline": True,
                "private_source_evidence_retained": True,
            },
        })
        baseline_path = self.root / "baseline.json"
        baseline_path.write_text(json.dumps(baseline), encoding="utf-8")
        result = dispatch_box_pipeline_request(
            self.runtime, json.loads(REQUEST.read_text(encoding="utf-8")),
        )
        # Represent a live read-only run without performing network access in unit tests.
        result["network_access_performed"] = True
        for batch in result["connector_batches"].values():
            batch["source"]["kind"] = "api"
            batch["source"]["network_access_performed"] = True
        result_path = self.root / "pipeline-result.json"
        result_path.write_text(json.dumps(result), encoding="utf-8")
        assessment_path = self.root / "assessment.json"
        assess_connector_shadow_artifacts(
            self.runtime, baseline_path, result_path, assessment_path,
        )
        reviewed_path = self.registry / name
        review_connector_shadow_artifact(
            self.runtime,
            assessment_path,
            reviewed_path,
            decision="passed",
            actor="independent-registry-reviewer",
            rationale="真实匿名来源计数与跨来源控制已完成独立复核",
            evidence_references=["review://sg-store/2026-08/connector-shadow"],
        )
        return reviewed_path

    def test_file_only_box_needs_no_connector_shadow_registry(self):
        result = build_connector_shadow_registry_workspace(
            BoxRuntime(GAME_BOX, ROOT / "packs"), None,
        )
        self.assertEqual(result["summary"]["activation_status"], "not_required")
        self.assertTrue(result["summary"]["ready_for_connector_shadow_evidence"])
        self.assertEqual(result["summary"]["required_network_pack_count"], 0)

    def test_network_box_without_registry_fails_closed(self):
        result = build_connector_shadow_registry_workspace(self.runtime, None)
        self.assertEqual(result["summary"]["activation_status"], "missing")
        self.assertFalse(result["summary"]["ready_for_connector_shadow_evidence"])
        self.assertEqual(
            {item["pack_id"] for item in result["pack_coverage"]},
            {"connector.shopify", "connector.stripe", "connector.wise"},
        )
        self.assertTrue(all(
            item["status"] == "missing_current_evidence"
            for item in result["pack_coverage"]
        ))

    def test_current_real_review_covers_three_packs_without_private_echo(self):
        reviewed_path = self._reviewed_artifact()
        result = build_connector_shadow_registry_workspace(
            self.runtime,
            self.registry,
            as_of=datetime.now(timezone.utc).date().isoformat(),
        )
        self.assertEqual(result["summary"]["activation_status"], "current")
        self.assertTrue(result["summary"]["pack_coverage_complete"])
        self.assertTrue(result["summary"]["ready_for_connector_shadow_evidence"])
        self.assertEqual(result["summary"]["covered_network_pack_count"], 3)
        self.assertEqual(result["summary"]["current_artifact_count"], 1)
        self.assertEqual(result["current_artifacts"][0]["entity_id"], "sg_store")
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn(str(self.registry), serialized)
        self.assertNotIn(reviewed_path.name, serialized)
        self.assertNotIn("independent-registry-reviewer", serialized)
        self.assertNotIn("review://", serialized)
        self.assertFalse(result["control_boundary"]["paths_returned"])
        self.assertFalse(result["control_boundary"]["financial_values_returned"])

    def test_production_readiness_uses_mounted_registry_as_separate_gate(self):
        self._reviewed_artifact()
        result = build_production_readiness_workspace(
            self.runtime,
            build_default_service_registry(),
            runs_root=self.root / "runs",
            environ={
                "OPC_CONNECTOR_SHADOW_REVIEW_DIR": str(self.registry),
            },
            as_of=datetime.now(timezone.utc).date().isoformat(),
        )
        stage = next(
            item for item in result["stages"]
            if item["stage_id"] == "connector_shadow_evidence"
        )
        self.assertEqual(stage["status"], "current")
        self.assertTrue(stage["gate_passed"])
        self.assertEqual(stage["facts"]["covered_network_pack_count"], 3)
        self.assertTrue(result["summary"]["connector_shadow_pack_coverage_complete"])
        self.assertFalse(result["summary"]["ready_for_bounded_shadow"])
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn(str(self.registry), serialized)
        self.assertNotIn("independent-registry-reviewer", serialized)

    def test_stale_duplicate_and_nonprivate_artifacts_fail_closed(self):
        reviewed_path = self._reviewed_artifact()
        artifact = json.loads(reviewed_path.read_text(encoding="utf-8"))
        artifact["review"]["reviewed_at"] = (
            datetime.now(timezone.utc) - timedelta(days=31)
        ).isoformat().replace("+00:00", "Z")
        review_core = {
            key: value for key, value in artifact["review"].items()
            if key != "review_id"
        }
        artifact["review"]["review_id"] = _hash(review_core)[:24]
        reviewed_path.write_text(json.dumps(artifact), encoding="utf-8")
        stale = build_connector_shadow_registry_workspace(
            self.runtime,
            self.registry,
            as_of=datetime.now(timezone.utc).date().isoformat(),
        )
        self.assertEqual(stale["counts"]["stale"], 1)
        self.assertFalse(stale["summary"]["ready_for_connector_shadow_evidence"])

        artifact["review"]["reviewed_at"] = datetime.now(timezone.utc).isoformat().replace(
            "+00:00", "Z",
        )
        review_core = {
            key: value for key, value in artifact["review"].items()
            if key != "review_id"
        }
        artifact["review"]["review_id"] = _hash(review_core)[:24]
        reviewed_path.write_text(json.dumps(artifact), encoding="utf-8")
        duplicate_path = self.registry / "duplicate.json"
        duplicate_path.write_text(reviewed_path.read_text(encoding="utf-8"), encoding="utf-8")
        duplicate_path.chmod(0o600)
        duplicate = build_connector_shadow_registry_workspace(self.runtime, self.registry)
        self.assertEqual(duplicate["counts"]["duplicate_scope"], 2)
        self.assertFalse(duplicate["summary"]["ready_for_connector_shadow_evidence"])

        duplicate_path.unlink()
        if os.name != "nt":
            reviewed_path.chmod(0o644)
            unsafe = build_connector_shadow_registry_workspace(self.runtime, self.registry)
            self.assertEqual(unsafe["summary"]["unexpected_entry_count"], 1)
            self.assertFalse(unsafe["summary"]["registry_clean"])
            self.assertFalse(unsafe["summary"]["ready_for_connector_shadow_evidence"])


if __name__ == "__main__":
    unittest.main()
