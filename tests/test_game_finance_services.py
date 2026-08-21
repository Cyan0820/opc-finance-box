import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook

from src.box_runtime import BoxRuntime
from src.default_connectors import build_default_connector_registry
from src.default_services import build_default_service_registry


ROOT = Path(__file__).resolve().parents[1]


class GameFinanceServicesTests(unittest.TestCase):
    def setUp(self):
        self.runtime = BoxRuntime(ROOT / "examples" / "boxes" / "global_game_studio.json", ROOT / "packs")
        self.registry = build_default_service_registry()

    def test_channel_settlement_uses_explicit_contract_formula(self):
        result = self.registry.dispatch(
            self.runtime,
            "game.reconcile_channel_settlements",
            {"settlements": [{
                "id": "S1", "entity_id": "sg_publisher", "period": "2026-07",
                "game": "G1", "channel": "App Store", "currency": "USD",
                "contract_basis": 1000, "contract_rate": 0.7, "contract_adjustments": 0,
                "reported_settlement": 700, "withholding_tax": 70, "net_receivable": 630,
                "evidence": ["channel contract", "settlement report"],
            }]},
        )["output"]
        self.assertTrue(result["ready"])
        self.assertEqual(result["reconciliations"][0]["expected_settlement"], 700)
        self.assertFalse(result["posting_or_collection_performed"])

    def test_project_profitability_never_mixes_entities_or_currencies(self):
        result = self.registry.dispatch(
            self.runtime,
            "game.project_profitability",
            {
                "revenues": [
                    {"id": "R1", "entity_id": "cn_studio", "project_code": "G1", "period": "2026-07", "currency": "CNY", "amount": 1000, "evidence": ["r1"]},
                    {"id": "R2", "entity_id": "sg_publisher", "project_code": "G1", "period": "2026-07", "currency": "USD", "amount": 100, "evidence": ["r2"]},
                ],
                "costs": [
                    {"id": "C1", "entity_id": "cn_studio", "project_code": "G1", "period": "2026-07", "currency": "CNY", "amount": 600, "evidence": ["c1"]},
                ],
            },
        )["output"]
        self.assertTrue(result["ready"])
        self.assertEqual(len(result["rows"]), 2)
        self.assertEqual({row["currency"] for row in result["rows"]}, {"CNY", "USD"})

    def test_ltv_roi_service_stays_inside_finance_decision_boundary(self):
        result = self.registry.dispatch(
            self.runtime,
            "game.ltv_roi_review",
            {"cohorts": [{
                "entity_id": "sg_publisher", "project_code": "G1", "channel": "Meta",
                "region": "US", "cohort": "2026-W20", "currency": "USD",
                "spend": 1000, "acquired_users": 100, "realized_net_revenue": 800,
                "forecast_ltv": 15, "maturity_days": 45, "target_roi": 1.2,
                "evidence": ["finance spend", "cohort model"],
            }]},
        )["output"]
        self.assertTrue(result["ready"])
        self.assertEqual(result["rows"][0]["projected_roi"], 1.5)
        self.assertEqual(result["rows"][0]["recommendation"], "eligible_for_budget_review")
        self.assertIn("bidding", result["boundary"]["not_owned"])
        self.assertFalse(result["budget_change_performed"])

    def test_app_store_file_connector_injects_explicit_entity_and_evidence(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "App Store结算.xlsx"
            book = Workbook()
            sheet = book.active
            sheet.title = "商店金流账单-App Store"
            sheet.append(["账期月份", "游戏名称", "平台", "渠道", "总流水", "退款流水", "结算金额", "甲方实收金额（结算币种）", "结算币种"])
            sheet.append(["2026-07", "G1", "iOS", "App Store", 1000, 0, 700, 700, "USD"])
            book.save(path)
            result = build_default_connector_registry().dispatch(
                self.runtime,
                "file.app_store_settlements",
                {"path": str(path), "default_entity_id": "sg_publisher"},
            )
        self.assertTrue(result["batch"]["quality"]["ready"])
        record = result["batch"]["datasets"]["game.settlements"][0]
        self.assertEqual(record["entity_id"], "sg_publisher")
        self.assertEqual(record["evidence"]["batch_id"], result["batch"]["batch_id"])


if __name__ == "__main__":
    unittest.main()
