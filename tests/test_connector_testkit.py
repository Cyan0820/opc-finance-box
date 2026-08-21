import json
import unittest
from pathlib import Path

from src.box_runtime import BoxRuntime
from src.connector_testkit import run_connector_contract_test
from src.default_connectors import build_default_connector_registry


ROOT = Path(__file__).resolve().parents[1]


class ConnectorTestkitTests(unittest.TestCase):
    def test_editable_api_example_passes_full_contract(self):
        runtime = BoxRuntime(ROOT / "examples" / "boxes" / "cn_dtc_api_store.json", ROOT / "packs")
        request = json.loads(
            (ROOT / "examples" / "connectors" / "commerce_api_payload.json").read_text(encoding="utf-8")
        )
        result = run_connector_contract_test(
            build_default_connector_registry(),
            runtime,
            "example.commerce_api_payload",
            request,
            expected_minimum_counts={"commerce.orders": 1, "commerce.settlements": 1},
        )
        self.assertTrue(result["passed"])
        self.assertTrue(all(result["checks"].values()))

    def test_count_expectation_failure_is_explainable(self):
        runtime = BoxRuntime(ROOT / "examples" / "boxes" / "cn_dtc_api_store.json", ROOT / "packs")
        request = json.loads(
            (ROOT / "examples" / "connectors" / "commerce_api_payload.json").read_text(encoding="utf-8")
        )
        result = run_connector_contract_test(
            build_default_connector_registry(),
            runtime,
            "example.commerce_api_payload",
            request,
            expected_minimum_counts={"commerce.orders": 2},
        )
        self.assertFalse(result["passed"])
        self.assertEqual(result["failures"]["minimum_counts"]["commerce.orders"]["actual"], 1)


if __name__ == "__main__":
    unittest.main()
