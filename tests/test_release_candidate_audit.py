from __future__ import annotations

import unittest
from pathlib import Path

from src.cli import build_parser
from src.release_candidate_audit import audit_release_candidate


ROOT = Path(__file__).resolve().parents[1]
PACKS = ROOT / "packs"


class ReleaseCandidateAuditTests(unittest.TestCase):
    def test_full_installed_product_matrix_is_reproducible_and_boundary_safe(self):
        result = audit_release_candidate(PACKS, project_root=ROOT)
        self.assertTrue(result["passed"])
        self.assertTrue(result["source_tree_release_candidate"])
        self.assertFalse(result["release_artifacts_provided"])
        self.assertFalse(result["release_artifacts_verified"])
        self.assertEqual(
            result["pack_contracts"],
            {
                "pack_count": 36,
                "capability_count": 114,
                "executable_count": 114,
                "declared_only_count": 0,
                "complete_implementation": True,
            },
        )
        starter = result["starter_matrix"]
        self.assertEqual(starter["profile_count"], 3)
        self.assertEqual(starter["jurisdiction_count"], 15)
        self.assertEqual(starter["eligible_combination_count"], 45)
        self.assertEqual(starter["verified_handoff_count"], 45)
        self.assertEqual(starter["unavailable_combination_count"], 0)
        self.assertEqual(
            {item["profile_id"] for item in starter["entries"]},
            {"game", "dtc", "marketplace"},
        )
        self.assertTrue(all(item["passed"] for item in starter["entries"]))
        self.assertEqual(result["integration_matrix"]["verified_variant_count"], 19)
        self.assertTrue(
            all(item["enabled_pack_ids"] for item in result["integration_matrix"]["entries"])
        )
        self.assertEqual(result["multi_entity_matrix"]["verified_variant_count"], 3)
        self.assertTrue(
            all(item["entity_count"] == 2 for item in result["multi_entity_matrix"]["entries"])
        )
        self.assertEqual(
            result["finance_boundary_eval"], {"total": 48, "passed": 48, "failed": 0},
        )
        self.assertEqual(result["deployment_assets"], {"verified": True, "asset_count": 7})
        self.assertTrue(result["maturity_boundary"]["technical_release_candidate_only"])
        self.assertFalse(result["maturity_boundary"]["stable_release_ready"])
        self.assertFalse(result["maturity_boundary"]["tax_filing_ready"])
        self.assertFalse(result["control_boundary"]["persistent_workspace_written"])
        self.assertFalse(result["control_boundary"]["external_actions_performed"])
        self.assertEqual(len(result["matrix_fingerprint"]), 64)

    def test_cli_exposes_optional_distribution_artifact_contract(self):
        args = build_parser().parse_args([
            "--packs", str(PACKS), "release-candidate-audit",
            "--project-root", str(ROOT), "--wheel", "candidate.whl",
            "--source-kit", "candidate.zip",
        ])
        self.assertEqual(args.command, "release-candidate-audit")
        self.assertEqual(args.project_root, ROOT)
        self.assertEqual(args.wheel, Path("candidate.whl"))
        self.assertEqual(args.source_kit, Path("candidate.zip"))


if __name__ == "__main__":
    unittest.main()
