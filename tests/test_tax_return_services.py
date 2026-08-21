import unittest
from pathlib import Path

from src.box_runtime import BoxRuntime
from src.default_services import build_default_service_registry


ROOT = Path(__file__).resolve().parents[1]


class TaxReturnServicesTests(unittest.TestCase):
    def setUp(self):
        self.runtime = BoxRuntime(ROOT / "examples" / "boxes" / "global_game_studio.json", ROOT / "packs")
        self.registry = build_default_service_registry()
        self.base = {
            "period": "2026-07",
            "tax_profile": {
                "credit_code": "DEMO-CREDIT-CODE",
                "registered_city": "演示城市",
                "vat_filing_frequency": "月度",
                "cit_filing_frequency": "季度",
            },
            "settlements": [],
            "purchases": [],
            "invoices": [],
            "payroll_rows": [],
        }

    def _dispatch(self, service_id):
        return self.registry.dispatch(
            self.runtime,
            service_id,
            self.base,
            entity_id="cn_studio",
        )["output"]

    def test_four_cn_workpapers_are_candidates_with_pack_sources(self):
        expected = {
            "tax.cn.vat_workpaper": "VAT-RETURN",
            "tax.cn.cit_prepaid_workpaper": "A200000",
            "tax.cn.stamp_tax_workpaper": "A01103",
            "tax.cn.iit_withholding_workpaper": "IIT-WITHHOLD",
        }
        for service_id, form_code in expected.items():
            with self.subTest(service_id=service_id):
                result = self._dispatch(service_id)
                self.assertEqual(result["form"]["form_code"], form_code)
                self.assertEqual(result["entity_id"], "cn_studio")
                self.assertTrue(result["form"]["official_sources"])
                self.assertTrue(result["form"]["official_sources"][0]["url"].startswith("https://"))
                self.assertFalse(result["filing_performed"])
                self.assertFalse(result["external_submission_enabled"])
                self.assertTrue(any(
                    marker in result["form"]["transport"]
                    for marker in ("不可直接", "候选")
                ))

    def test_workpaper_rejects_records_from_another_entity(self):
        payload = {**self.base, "settlements": [{
            "id": "S1", "entity_id": "sg_publisher", "period": "2026-07",
        }]}
        with self.assertRaisesRegex(ValueError, "outside entity cn_studio"):
            self.registry.dispatch(
                self.runtime,
                "tax.cn.vat_workpaper",
                payload,
                entity_id="cn_studio",
            )

    def test_missing_facts_remain_visible_blockers(self):
        result = self._dispatch("tax.cn.vat_workpaper")
        self.assertFalse(result["ready_for_review"])
        self.assertIn("按发票/交易性质拆分销售额及适用税率", result["form"]["blockers"])


if __name__ == "__main__":
    unittest.main()
