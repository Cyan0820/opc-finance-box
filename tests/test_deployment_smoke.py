import os
import unittest
from pathlib import Path
from unittest.mock import patch

from src.deployment_smoke import DeploymentSmokeError, run_deployment_smoke
from src.server import _server_port_from_environment


ROOT = Path(__file__).resolve().parents[1]


class DeploymentSmokeTests(unittest.TestCase):
    def test_real_workbench_starts_with_isolated_data_and_is_always_stopped(self):
        with patch.dict(os.environ, {
            "OPC_SHOPIFY_ADMIN_TOKEN": "parent-secret-must-not-be-inherited",
            "HTTPS_PROXY": "http://proxy.invalid:9999",
        }):
            result = run_deployment_smoke(
                ROOT / "examples" / "boxes" / "cn_dtc_shopify_stripe_store.json",
                ROOT / "packs", timeout_seconds=15,
            )
        self.assertTrue(result["passed"])
        self.assertEqual(result["counts"], {"total": 9, "passed": 9, "failed": 0})
        self.assertTrue(result["server_process_terminated"])
        self.assertEqual(result["server_exit_code"], 0)
        self.assertTrue(result["isolated_runtime_data_removed"])
        self.assertFalse(result["secret_values_inherited"])
        self.assertFalse(result["raw_smoke_tokens_returned"])
        self.assertEqual(result["authentication_mode"], "temporary_role_policy_on_loopback")
        self.assertEqual(result["network_access"], "loopback_only")
        self.assertFalse(result["connector_dispatch_performed"])
        self.assertFalse(result["external_actions_performed"])

    def test_invalid_timeout_fails_before_starting_a_process(self):
        with self.assertRaisesRegex(DeploymentSmokeError, "3 to 60"):
            run_deployment_smoke(
                ROOT / "examples" / "boxes" / "cn_dtc_store.json",
                ROOT / "packs", timeout_seconds=2,
            )

    def test_production_port_precedes_legacy_alias_and_is_bounded(self):
        with patch.dict(os.environ, {
            "OPC_FINANCE_PORT": "8877", "SETTLEMENT_MVP_PORT": "9999",
        }, clear=False):
            self.assertEqual(_server_port_from_environment(), 8877)
        with patch.dict(os.environ, {"OPC_FINANCE_PORT": "0"}, clear=False):
            with self.assertRaisesRegex(RuntimeError, "1 to 65535"):
                _server_port_from_environment()


if __name__ == "__main__":
    unittest.main()
