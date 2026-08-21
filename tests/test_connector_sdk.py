import json
import tempfile
import unittest
from pathlib import Path

from src.box_runtime import BoxRuntime
from src.connector_sdk import ConnectorDefinition, ConnectorError, ConnectorRegistry
from src.default_connectors import build_default_connector_registry


ROOT = Path(__file__).resolve().parents[1]
ORDER_CSV = """订单ID,法律主体ID,期间,渠道,目的地国家,币种,商品原价不含税
DTC-1,cn_dtc_company,2026-07,DTC Store,US,USD,100
"""


class ConnectorSdkTests(unittest.TestCase):
    def setUp(self):
        self.runtime = BoxRuntime(ROOT / "examples" / "boxes" / "cn_dtc_store.json", ROOT / "packs")

    def test_default_catalog_is_filtered_by_box_capability(self):
        catalog = build_default_connector_registry().catalog(self.runtime)
        self.assertEqual(
            {item["connector_id"] for item in catalog},
            {
                "file.bank_statement", "file.general_ledger", "file.trial_balance", "file.commerce",
                "file.csv_commerce", "file.xlsx_commerce",
            },
        )

    def test_file_connector_returns_standard_evidence_backed_batch(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "orders.csv"
            path.write_text(ORDER_CSV, encoding="utf-8")
            result = build_default_connector_registry().dispatch(
                self.runtime,
                "file.commerce",
                {"path": str(path)},
            )
        self.assertTrue(result["batch"]["quality"]["ready"])
        self.assertEqual(result["batch"]["quality"]["dataset_counts"]["commerce.orders"], 1)
        record = result["batch"]["datasets"]["commerce.orders"][0]
        self.assertEqual(record["entity_id"], "cn_dtc_company")
        self.assertEqual(record["evidence"]["batch_id"], result["batch"]["batch_id"])

    def test_unknown_entity_and_missing_evidence_are_rejected(self):
        registry = ConnectorRegistry()
        registry.register(ConnectorDefinition(
            connector_id="test.bad_records",
            pack_id="connector.file_import",
            capability="connector.csv",
            display_name="Test",
            dataset_types=("test.rows",),
            handler=lambda request, context: {
                "batch_id": "B1",
                "datasets": {"test.rows": [
                    {"id": "1", "entity_id": "missing", "evidence": {"source_file": "x", "batch_id": "B1"}},
                    {"id": "2", "entity_id": "cn_dtc_company"},
                ]},
            },
            business_keys={"test.rows": ("id",)},
        ))
        result = registry.dispatch(self.runtime, "test.bad_records", {})
        self.assertFalse(result["batch"]["quality"]["ready"])
        self.assertEqual(result["batch"]["quality"]["rejected_count"], 2)
        self.assertEqual(result["batch"]["quality"]["record_count"], 0)

    def test_duplicate_business_keys_never_enter_accepted_dataset_twice(self):
        registry = ConnectorRegistry()
        row = {"id": "1", "entity_id": "cn_dtc_company", "evidence": {"source_file": "x", "batch_id": "B1"}}
        registry.register(ConnectorDefinition(
            connector_id="test.duplicates",
            pack_id="connector.file_import",
            capability="connector.csv",
            display_name="Test",
            dataset_types=("test.rows",),
            handler=lambda request, context: {"batch_id": "B1", "datasets": {"test.rows": [row, row]}},
            business_keys={"test.rows": ("id",)},
        ))
        result = registry.dispatch(self.runtime, "test.duplicates", {})
        self.assertEqual(result["batch"]["quality"]["record_count"], 1)
        self.assertEqual(len(result["batch"]["quality"]["duplicate_business_keys"]), 1)
        self.assertFalse(result["batch"]["quality"]["ready"])

    def test_registration_requires_business_keys_for_every_dataset(self):
        with self.assertRaises(ConnectorError):
            ConnectorRegistry().register(ConnectorDefinition(
                connector_id="bad",
                pack_id="connector.file_import",
                capability="connector.csv",
                display_name="Bad",
                dataset_types=("a",),
                handler=lambda request, context: {},
                business_keys={},
            ))

    def test_network_connector_must_declare_safe_environment_names(self):
        base = dict(
            connector_id="network.test", pack_id="connector.file_import", capability="connector.csv",
            display_name="Network", dataset_types=("x",), handler=lambda request, context: {},
            business_keys={"x": ("id",)}, network_access=True,
        )
        with self.assertRaisesRegex(ConnectorError, "must declare credential_env"):
            ConnectorRegistry().register(ConnectorDefinition(**base))
        with self.assertRaisesRegex(ConnectorError, "OPC_ environment names"):
            ConnectorRegistry().register(ConnectorDefinition(**base, credential_env=("TOKEN",)))

    def test_explicit_binding_limits_catalog_and_rejects_before_handler(self):
        config = json.loads(
            (ROOT / "examples" / "boxes" / "global_game_studio_xero.json").read_text(
                encoding="utf-8"
            )
        )
        config["connector_bindings"] = [
            {"connector_pack": "connector.file_import", "entity_ids": ["cn_studio", "sg_publisher"]},
            {"connector_pack": "connector.xero", "entity_ids": ["cn_studio"]},
        ]
        calls: list[str] = []
        registry = ConnectorRegistry()
        registry.register(ConnectorDefinition(
            connector_id="test.bound_xero",
            pack_id="connector.xero",
            capability="connector.xero_trial_balance",
            display_name="Bound Xero",
            dataset_types=("test.rows",),
            handler=lambda request, context: calls.append("called") or {
                "batch_id": "B1", "datasets": {"test.rows": []},
            },
            business_keys={"test.rows": ("id",)},
        ))
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "box.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            runtime = BoxRuntime(path, ROOT / "packs")
            self.assertEqual(registry.catalog(runtime)[0]["entity_ids"], ["cn_studio"])
            with self.assertRaisesRegex(ConnectorError, "not bound"):
                registry.dispatch(
                    runtime, "test.bound_xero", {"default_entity_id": "sg_publisher"},
                )
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
