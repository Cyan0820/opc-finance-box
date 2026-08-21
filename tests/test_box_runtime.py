import json
import shutil
import tempfile
import unittest
from pathlib import Path

from src.box_runtime import BoxRuntime, BoxRuntimeError


ROOT = Path(__file__).resolve().parents[1]


class BoxRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.config = ROOT / "examples" / "boxes" / "global_game_studio.json"
        self.packs = ROOT / "packs"

    def test_snapshot_exposes_honest_product_readiness(self):
        runtime = BoxRuntime(self.config, self.packs)
        snapshot = runtime.snapshot()
        self.assertEqual(snapshot["name"], "全球小游戏 OPC 样板")
        self.assertEqual(snapshot["tax_readiness"]["cn_studio"], "workpaper")
        self.assertEqual(snapshot["tax_readiness"]["sg_publisher"], "design")
        self.assertFalse(snapshot["production_ready"])
        self.assertTrue(snapshot["fingerprint"])

    def test_capability_and_entity_guards(self):
        runtime = BoxRuntime(self.config, self.packs)
        runtime.require_capability("game.channel_settlement")
        runtime.require_entity("cn_studio")
        with self.assertRaises(BoxRuntimeError):
            runtime.require_capability("commerce.inventory_cost")
        with self.assertRaises(BoxRuntimeError):
            runtime.require_entity("missing")

    def test_reload_changes_snapshot_only_when_sources_change(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "box.json"
            payload = json.loads(self.config.read_text(encoding="utf-8"))
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            runtime = BoxRuntime(path, self.packs)
            first = runtime.snapshot()["fingerprint"]
            self.assertFalse(runtime.reload())
            payload["name"] = "更新后的样板"
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            self.assertTrue(runtime.reload())
            self.assertNotEqual(first, runtime.snapshot()["fingerprint"])
            self.assertEqual(runtime.snapshot()["name"], "更新后的样板")

    def test_tax_rule_source_change_triggers_reload(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            packs = root / "packs"
            shutil.copytree(self.packs, packs)
            config = root / "box.json"
            config.write_text(self.config.read_text(encoding="utf-8"), encoding="utf-8")
            runtime = BoxRuntime(config, packs)
            first = runtime.snapshot()["fingerprint"]
            rules_path = packs / "jurisdictions" / "sg" / "rules.json"
            payload = json.loads(rules_path.read_text(encoding="utf-8"))
            payload["scope_note"] += " 已复核。"
            rules_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            self.assertTrue(runtime.reload())
            self.assertNotEqual(first, runtime.snapshot()["fingerprint"])

    def test_connector_binding_is_exposed_and_enforced_per_entity(self):
        payload = json.loads(
            (ROOT / "examples" / "boxes" / "global_game_studio_xero.json").read_text(
                encoding="utf-8"
            )
        )
        payload["connector_bindings"] = [
            {"connector_pack": "connector.file_import", "entity_ids": ["cn_studio", "sg_publisher"]},
            {"connector_pack": "connector.xero", "entity_ids": ["sg_publisher"]},
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "box.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            runtime = BoxRuntime(path, self.packs)
            self.assertEqual(runtime.snapshot()["connector_binding_mode"], "explicit")
            self.assertEqual(runtime.connector_entity_ids("connector.xero"), {"sg_publisher"})
            runtime.require_connector_entity("connector.xero", "sg_publisher")
            with self.assertRaisesRegex(BoxRuntimeError, "not bound"):
                runtime.require_connector_entity("connector.xero", "cn_studio")


if __name__ == "__main__":
    unittest.main()
