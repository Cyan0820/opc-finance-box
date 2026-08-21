import shutil
import tempfile
import unittest
from pathlib import Path

from src.deployment_assets import DeploymentAssetError, verify_deployment_assets


ROOT = Path(__file__).resolve().parents[1]


class DeploymentAssetTests(unittest.TestCase):
    def test_bundled_templates_keep_non_root_auth_and_systemd_hardening(self):
        result = verify_deployment_assets(ROOT / "deployment")
        self.assertTrue(result["valid"])
        self.assertEqual(result["asset_count"], 7)
        self.assertTrue(result["workbench_runs_as_non_root"])
        self.assertTrue(result["root_auth_init_is_one_shot"])
        self.assertTrue(result["runtime_data_excluded_from_build_context"])
        self.assertTrue(result["role_policy_reference_required"])
        self.assertTrue(result["versioned_runtime_data_initialized_before_start"])
        self.assertFalse(result["raw_secret_values_included"])
        self.assertFalse(result["deployment_performed"])

    def test_missing_non_root_container_control_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            copied = Path(temp_dir) / "deployment"
            shutil.copytree(ROOT / "deployment", copied)
            dockerfile = copied / "Dockerfile"
            dockerfile.write_text(
                dockerfile.read_text(encoding="utf-8").replace("USER 10001:10001", ""),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(DeploymentAssetError, "USER 10001"):
                verify_deployment_assets(copied)


if __name__ == "__main__":
    unittest.main()
