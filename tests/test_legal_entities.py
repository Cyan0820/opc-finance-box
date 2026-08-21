import unittest
from pathlib import Path

from src.box_config import resolve_box_file
from src.legal_entities import (
    EntityRegistry,
    EntityScopeError,
    build_legacy_entity,
    entity_scope_quality,
)


ROOT = Path(__file__).resolve().parents[1]


class LegalEntityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.resolved = resolve_box_file(
            ROOT / "examples" / "boxes" / "global_game_studio.json",
            ROOT / "packs",
        )
        cls.registry = EntityRegistry.from_resolved_box(cls.resolved)

    def test_statutory_books_are_scoped_to_one_entity(self):
        scope = self.registry.statutory_scope("cn_studio")
        self.assertEqual(scope["entity"]["jurisdiction"], "CN")
        self.assertEqual(scope["currency"], "CNY")
        self.assertTrue(scope["books_must_remain_separate"])

    def test_management_scope_requires_translation_and_elimination(self):
        scope = self.registry.management_scope(["cn_studio", "sg_publisher"])
        self.assertEqual(scope["reporting_currency"], "CNY")
        self.assertTrue(scope["requires_fx_translation"])
        self.assertTrue(scope["requires_intercompany_elimination"])

    def test_unknown_entity_is_rejected(self):
        with self.assertRaises(EntityScopeError):
            self.registry.statutory_scope("missing")

    def test_scope_quality_blocks_unassigned_and_unknown_records(self):
        quality = entity_scope_quality({
            "settlements": [
                {"id": "S1", "entity_id": "cn_studio"},
                {"id": "S2"},
                {"id": "S3", "entity_id": "wrong"},
            ]
        }, self.registry)
        self.assertFalse(quality["ready"])
        self.assertEqual(quality["unassigned_count"], 1)
        self.assertEqual(quality["unknown_count"], 1)

    def test_legacy_profile_is_explicitly_adapted_as_cn(self):
        entity = build_legacy_entity({
            "company_name": "旧版游戏公司",
            "base_currency": "CNY",
            "accounting_standard": "小企业会计准则",
            "vat_taxpayer_type": "一般纳税人",
            "payroll_enabled": True,
        })
        self.assertEqual(entity.jurisdiction, "CN")
        self.assertEqual(entity.tax_pack, "jurisdiction.cn_mainland")
        self.assertIn("vat:一般纳税人", entity.tax_registrations)


if __name__ == "__main__":
    unittest.main()
