import unittest
from pathlib import Path

from src.box_api import build_box_context, load_default_box_runtime
from src.box_runtime import BoxRuntimeError


ROOT = Path(__file__).resolve().parents[1]


class BoxApiTests(unittest.TestCase):
    def test_default_runtime_uses_global_game_sample(self):
        runtime = load_default_box_runtime(ROOT, {})
        context = build_box_context(runtime)
        self.assertEqual(context["product"]["name"], "全球小游戏 OPC 样板")
        self.assertEqual(context["scope"]["scope"], "management")
        self.assertIn("game", context["capability_groups"])
        self.assertFalse(context["product"]["production_ready"])
        self.assertEqual(context["product"]["workbench"]["profile"], "game_studio")
        self.assertEqual(context["product"]["workbench"]["demo_dataset"], "game_global")

    def test_relative_environment_paths_are_resolved_from_project_root(self):
        runtime = load_default_box_runtime(ROOT, {
            "OPC_FINANCE_BOX_CONFIG": "examples/boxes/cn_dtc_store.json",
            "OPC_FINANCE_PACKS_ROOT": "packs",
        })
        context = build_box_context(runtime, scope="statutory", entity_id="cn_dtc_company")
        self.assertIn("commerce", context["capability_groups"])
        self.assertEqual(context["scope"]["entity"]["entity_id"], "cn_dtc_company")
        self.assertEqual(context["product"]["workbench"]["profile"], "commerce_dtc")
        self.assertIsNone(context["product"]["workbench"]["demo_dataset"])

    def test_statutory_context_requires_one_entity(self):
        runtime = load_default_box_runtime(ROOT, {})
        with self.assertRaises(BoxRuntimeError):
            build_box_context(runtime, scope="statutory")

    def test_marketplace_box_selects_marketplace_control_workbench(self):
        runtime = load_default_box_runtime(ROOT, {
            "OPC_FINANCE_BOX_CONFIG": "examples/boxes/cn_marketplace_store.json",
            "OPC_FINANCE_PACKS_ROOT": "packs",
        })
        context = build_box_context(runtime)
        workbench = context["product"]["workbench"]
        self.assertEqual(workbench["profile"], "commerce_marketplace")
        self.assertEqual(workbench["reference_workbench"], "box_control")
        self.assertIn("channel.marketplace_commerce", workbench["channel_pack_ids"])

    def test_unsupported_scope_is_rejected(self):
        runtime = load_default_box_runtime(ROOT, {})
        with self.assertRaises(BoxRuntimeError):
            build_box_context(runtime, scope="combined_books")


if __name__ == "__main__":
    unittest.main()
