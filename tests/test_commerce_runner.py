import tempfile
import unittest
from pathlib import Path

from src.box_runtime import BoxRuntime, BoxRuntimeError
from src.commerce_runner import run_commerce_box


ROOT = Path(__file__).resolve().parents[1]
ORDER_CSV = """订单ID,法律主体ID,期间,渠道,目的地国家,币种,商品原价不含税,折扣不含税,运费收入不含税,已收税额,退款不含税,退回税额,商品成本,履约成本,物流成本
DTC-1,cn_dtc_company,2026-07,DTC Store,US,USD,100,10,5,9.5,0,0,35,6,8
"""
SETTLEMENT_CSV = """结算ID,法律主体ID,期间,渠道,币种,渠道报告订单净流入,渠道及支付费用,渠道代扣代缴税额,其他调整,实际打款
PAY-1,cn_dtc_company,2026-07,DTC Store,USD,104.5,3,9.5,0,92
"""


class CommerceRunnerTests(unittest.TestCase):
    def _runtime(self, name: str) -> BoxRuntime:
        return BoxRuntime(ROOT / "examples" / "boxes" / name, ROOT / "packs")

    def test_dtc_box_runs_end_to_end(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            order_path = Path(temp_dir) / "orders.csv"
            settlement_path = Path(temp_dir) / "settlements.csv"
            order_path.write_text(ORDER_CSV, encoding="utf-8-sig")
            settlement_path.write_text(SETTLEMENT_CSV, encoding="utf-8-sig")
            result = run_commerce_box(self._runtime("cn_dtc_store.json"), [order_path, settlement_path])
        self.assertTrue(result["ready"])
        self.assertEqual(result["counts"], {
            "input_files": 2, "orders": 1, "settlements": 1,
            "returns": 0, "return_receipts": 0,
            "import_costs": 0,
        })
        self.assertTrue(result["return_inventory"]["no_return_activity"])
        self.assertEqual(result["analysis"]["reconciliations"][0]["reported_payout"], 92)
        self.assertIn("commerce", result["box"]["capability_groups"])

    def test_game_box_cannot_run_commerce_engine(self):
        with self.assertRaises(BoxRuntimeError):
            run_commerce_box(self._runtime("global_game_studio.json"), [])

    def test_reconciliation_difference_keeps_result_not_ready(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            order_path = Path(temp_dir) / "orders.csv"
            settlement_path = Path(temp_dir) / "settlements.csv"
            order_path.write_text(ORDER_CSV, encoding="utf-8-sig")
            settlement_path.write_text(SETTLEMENT_CSV.replace(",92\n", ",91\n"), encoding="utf-8-sig")
            result = run_commerce_box(self._runtime("cn_dtc_store.json"), [order_path, settlement_path])
        self.assertFalse(result["ready"])
        self.assertEqual(result["analysis"]["reconciliations"][0]["status"], "存在差异")


if __name__ == "__main__":
    unittest.main()
