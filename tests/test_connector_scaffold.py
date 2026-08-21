import json
import shutil
import tempfile
import unittest
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from src.box_runtime import BoxRuntime
from src.connector_scaffold import ConnectorScaffoldError, scaffold_connector_pack
from src.connector_sdk import ConnectorError
from src.connector_testkit import run_connector_contract_test
from src.connector_http import HttpResponse
from src.default_connectors import build_box_connector_registry
from src.box_compiler import compile_box


ROOT = Path(__file__).resolve().parents[1]


class ConnectorScaffoldTests(unittest.TestCase):
    def _box(self, root: Path) -> tuple[BoxRuntime, dict]:
        packs = root / "packs"
        shutil.copytree(ROOT / "packs", packs)
        result = scaffold_connector_pack(
            packs / "connectors", slug="sample_store", display_name="Sample Store API",
            secret_env="OPC_SAMPLE_STORE_TOKEN", base_url="https://api.example.test/v1/finance",
        )
        config = json.loads((ROOT / "examples" / "boxes" / "cn_dtc_store.json").read_text(encoding="utf-8"))
        config["connectors"].append("connector.sample_store")
        config["entities"][0]["id"] = "demo_entity"
        path = root / "box.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        return BoxRuntime(path, packs), result

    def test_generated_provider_is_discovered_and_passes_fixture_contract(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime, result = self._box(Path(temp_dir))
            registry = build_box_connector_registry(runtime)
            fixture = json.loads(
                (Path(result["destination"]) / "fixture-request.json").read_text(encoding="utf-8")
            )
            report = run_connector_contract_test(
                registry, runtime, result["connector_id"], fixture,
                expected_minimum_counts={"commerce.orders": 1, "commerce.settlements": 1},
            )
        self.assertTrue(report["passed"], report)

    def test_generated_pack_carries_a_standalone_contract_runner(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            runtime, result = self._box(Path(temp_dir))
            process = subprocess.run(
                [sys.executable, str(Path(result["destination"]) / "provider_contract_test.py"),
                 str(runtime.config_path), "--packs", str(runtime.packs_root)],
                cwd=ROOT, text=True, capture_output=True, timeout=20,
            )
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertTrue(json.loads(process.stdout)["passed"])

    def test_generated_provider_rejects_inline_credentials(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime, result = self._box(Path(temp_dir))
            with self.assertRaisesRegex(ConnectorError, "credentials must not be passed"):
                build_box_connector_registry(runtime).dispatch(
                    runtime, result["connector_id"], {"token": "secret", "payload": {}},
                )

    def test_generated_fetch_uses_env_secret_bounded_retry_and_pagination(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime, result = self._box(Path(temp_dir))
            registry = build_box_connector_registry(runtime)
            definition = next(item for item in registry.definitions() if item.connector_id == result["connector_id"])
            self.assertTrue(definition.network_access)
            self.assertEqual(definition.credential_env, ("OPC_SAMPLE_STORE_TOKEN",))
            fixture = json.loads(
                (Path(result["destination"]) / "fixture-request.json").read_text(encoding="utf-8")
            )["payload"]
            responses = [
                HttpResponse(503, {}, b"do not expose this body"),
                HttpResponse(200, {}, json.dumps({
                    "orders": fixture["orders"], "payouts": [], "next_cursor": "PAGE-2",
                }).encode()),
                HttpResponse(200, {}, json.dumps({
                    "orders": [], "payouts": fixture["payouts"], "next_cursor": None,
                }).encode()),
            ]
            calls, sleeps = [], []
            def transport(request):
                calls.append(request)
                return responses.pop(0)
            definition.handler.__globals__["HTTP_TRANSPORT"] = transport
            definition.handler.__globals__["HTTP_SLEEPER"] = sleeps.append
            with patch.dict("os.environ", {"OPC_SAMPLE_STORE_TOKEN": "ENV-SECRET"}, clear=False):
                output = registry.dispatch(runtime, result["connector_id"], {"mode": "fetch"})
        self.assertTrue(output["batch"]["quality"]["ready"])
        self.assertEqual(output["batch"]["source"]["page_count"], 2)
        self.assertEqual(output["batch"]["source"]["retry_count"], 1)
        self.assertEqual(sleeps, [1])
        self.assertIn("cursor=PAGE-2", calls[-1].url)
        serialized = json.dumps(output, ensure_ascii=False)
        self.assertNotIn("ENV-SECRET", serialized)
        self.assertNotIn("Authorization", serialized)

    def test_compiled_box_lists_secret_reference_name_never_value(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime, _ = self._box(Path(temp_dir))
            with patch.dict("os.environ", {"OPC_SAMPLE_STORE_TOKEN": "MUST-NOT-APPEAR"}, clear=False):
                compiled = compile_box(runtime)
        task = next(item for item in compiled["setup_tasks"] if item["category"] == "connector_runtime")
        self.assertEqual(task["credential_env"], ["OPC_SAMPLE_STORE_TOKEN"])
        self.assertFalse(task["secret_values_included"])
        self.assertNotIn("MUST-NOT-APPEAR", json.dumps(compiled))

    def test_scaffold_refuses_bad_secret_names_and_overwrite(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(ConnectorScaffoldError):
                scaffold_connector_pack(temp_dir, slug="store", display_name="Store", secret_env="TOKEN", base_url="https://api.example.test")
            scaffold_connector_pack(temp_dir, slug="store", display_name="Store", secret_env="OPC_STORE_TOKEN", base_url="https://api.example.test")
            with self.assertRaisesRegex(ConnectorScaffoldError, "already exists"):
                scaffold_connector_pack(temp_dir, slug="store", display_name="Store", secret_env="OPC_STORE_TOKEN", base_url="https://api.example.test")


if __name__ == "__main__":
    unittest.main()
