import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook

from src.commerce import build_commerce_analysis
from src.commerce_import import CommerceDataError, parse_commerce_csv, parse_commerce_file


ORDER_CSV = """订单ID,法律主体ID,期间,渠道,目的地国家,币种,商品原价不含税,折扣不含税,运费收入不含税,已收税额,退款不含税,退回税额,商品成本,履约成本,物流成本
DTC-1,cn_dtc_company,2026-07,DTC Store,US,USD,100,10,5,9.5,0,0,35,6,8
DTC-2,cn_dtc_company,2026-07,DTC Store,DE,USD,200,0,0,20,50,5,75,9,12
"""

SETTLEMENT_CSV = """结算ID,法律主体ID,期间,渠道,币种,渠道报告订单净流入,渠道及支付费用,渠道代扣代缴税额,其他调整,实际打款
PAY-1,cn_dtc_company,2026-07,DTC Store,USD,269.5,8,25,0,236.5
"""

RETURN_CSV = """退货单号,订单号,法律主体ID,期间,渠道,商品SKU,币种,授权退货数量,已退款数量,退款金额不含税,退回税额
RET-1,DTC-1,cn_dtc_company,2026-07,DTC Store,SKU-1,USD,2,2,20,2
"""

RETURN_RECEIPT_CSV = """退货入库单号,退货单号,法律主体ID,期间,商品SKU,仓库,实收数量,处置状态
RCPT-1,RET-1,cn_dtc_company,2026-07,SKU-1,WH-A,1,restockable
RCPT-2,RET-1,cn_dtc_company,2026-07,SKU-1,WH-B,1,damaged
"""

IMPORT_COST_CSV = """进口明细行ID,进口批次号,法律主体ID,期间,商品SKU,仓库,原产国,目的地国家,币种,进口数量,申报货值,进口运费,保险费,关税金额,进口税,报关服务费
IMPORT-LINE-1,IMPORT-ENTRY-1,cn_dtc_company,2026-07,SKU-1,WH-A,CN,US,USD,10,100,20,2,8,10,5
"""


class CommerceImportTests(unittest.TestCase):
    def _write(self, directory: str, name: str, text: str) -> Path:
        path = Path(directory) / name
        path.write_text(text, encoding="utf-8-sig")
        return path

    def test_chinese_order_headers_map_to_standard_schema(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = self._write(temp_dir, "orders.csv", ORDER_CSV)
            batch = parse_commerce_csv(path)
        self.assertTrue(batch["quality"]["ready"])
        self.assertEqual(batch["quality"]["order_count"], 2)
        self.assertEqual(batch["orders"][0]["destination_country"], "US")
        self.assertEqual(batch["orders"][0]["evidence"]["source_row"], 2)
        self.assertTrue(batch["batch_id"])

    def test_order_and_settlement_csvs_feed_deterministic_engine(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            orders_path = self._write(temp_dir, "orders.csv", ORDER_CSV)
            settlement_path = self._write(temp_dir, "settlements.csv", SETTLEMENT_CSV)
            orders = parse_commerce_csv(orders_path)["orders"]
            settlements = parse_commerce_csv(settlement_path)["settlements"]
        analysis = build_commerce_analysis(orders, settlements, allowed_entity_ids={"cn_dtc_company"})
        self.assertTrue(analysis["ready"])
        self.assertEqual(analysis["reconciliations"][0]["reported_payout"], 236.5)

    def test_default_entity_and_channel_are_explicit_connector_inputs(self):
        csv_text = "订单ID,期间,目的地国家,币种,商品原价不含税\nDTC-1,2026-07,US,USD,100\n"
        with tempfile.TemporaryDirectory() as temp_dir:
            path = self._write(temp_dir, "orders.csv", csv_text)
            batch = parse_commerce_csv(
                path, record_type="orders", default_entity_id="cn_dtc_company", default_channel="DTC Store"
            )
        self.assertTrue(batch["quality"]["ready"])
        self.assertEqual(batch["orders"][0]["entity_id"], "cn_dtc_company")

    def test_invalid_row_is_rejected_without_poisoning_valid_rows(self):
        csv_text = ORDER_CSV + "DTC-3,cn_dtc_company,2026-07,DTC Store,GB,USD,100,80,0,0,30,0,0,0,0\n"
        with tempfile.TemporaryDirectory() as temp_dir:
            path = self._write(temp_dir, "orders.csv", csv_text)
            batch = parse_commerce_csv(path)
        self.assertFalse(batch["quality"]["ready"])
        self.assertEqual(batch["quality"]["order_count"], 2)
        self.assertEqual(batch["quality"]["rejected_count"], 1)

    def test_same_file_has_stable_batch_id(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = self._write(temp_dir, "orders.csv", ORDER_CSV)
            first = parse_commerce_csv(path)["batch_id"]
            second = parse_commerce_csv(path)["batch_id"]
        self.assertEqual(first, second)

    def test_return_and_receipt_csvs_map_to_separate_standard_datasets(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            returns_path = self._write(temp_dir, "returns.csv", RETURN_CSV)
            receipts_path = self._write(temp_dir, "return_receipts.csv", RETURN_RECEIPT_CSV)
            returns = parse_commerce_csv(returns_path, record_type="returns")
            receipts = parse_commerce_csv(receipts_path, record_type="return_receipts")
        self.assertTrue(returns["quality"]["ready"])
        self.assertEqual(returns["quality"]["return_count"], 1)
        self.assertEqual(returns["returns"][0]["sku"], "SKU-1")
        self.assertTrue(receipts["quality"]["ready"])
        self.assertEqual(receipts["quality"]["return_receipt_count"], 2)
        self.assertEqual({row["warehouse"] for row in receipts["return_receipts"]}, {"WH-A", "WH-B"})

    def test_workbook_sheet_names_route_returns_before_generic_refunds(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "退货闭环.xlsx"
            book = Workbook()
            returns_sheet = book.active
            returns_sheet.title = "退货授权与退款"
            for row in [line.split(",") for line in RETURN_CSV.strip().splitlines()]:
                returns_sheet.append(row)
            receipts_sheet = book.create_sheet("退货入库")
            for row in [line.split(",") for line in RETURN_RECEIPT_CSV.strip().splitlines()]:
                receipts_sheet.append(row)
            book.save(path)
            batch = parse_commerce_file(path)
        self.assertTrue(batch["quality"]["ready"], batch)
        self.assertEqual(batch["quality"]["return_count"], 1)
        self.assertEqual(batch["quality"]["return_receipt_count"], 2)

    def test_invalid_return_disposition_fails_batch_quality(self):
        text = RETURN_RECEIPT_CSV.replace("restockable", "sellable")
        with tempfile.TemporaryDirectory() as temp_dir:
            path = self._write(temp_dir, "return_receipts.csv", text)
            batch = parse_commerce_csv(path, record_type="return_receipts")
        self.assertFalse(batch["quality"]["ready"])
        self.assertEqual(batch["quality"]["rejected_count"], 1)

    def test_import_cost_csv_maps_customs_evidence_without_tax_conclusion(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = self._write(temp_dir, "import_costs.csv", IMPORT_COST_CSV)
            batch = parse_commerce_csv(path, record_type="import_costs")
        self.assertTrue(batch["quality"]["ready"], batch)
        self.assertEqual(batch["quality"]["import_cost_count"], 1)
        self.assertEqual(batch["import_costs"][0]["customs_duty"], "8")

    def test_unsupported_file_is_rejected(self):
        with self.assertRaises(CommerceDataError):
            parse_commerce_file("orders.pdf")


if __name__ == "__main__":
    unittest.main()
