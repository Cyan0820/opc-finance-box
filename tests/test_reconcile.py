import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook

from src.reconcile import dashboard_payload, discover_workbook, parse_workbook, parse_workbook_configured


HEADERS = ["账期月份", "游戏名称", "平台", "渠道", "总流水", "退款流水", "结算金额", "甲方实收金额（结算币种）", "结算币种"]


def make_settlement_book(path: Path, sheet_name: str, rows: list[list], *, preface: bool = False) -> Path:
    book = Workbook()
    sheet = book.active
    sheet.title = sheet_name
    if preface:
        sheet.append(["虚构示例结算单"])
        sheet.append(["仅用于自动化测试"])
        sheet.append([])
    sheet.append(HEADERS)
    for row in rows:
        sheet.append(row)
    book.save(path)
    return path


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.domestic = make_settlement_book(root / "国服结算.xlsx", "对外账单-发行", [
            ["2025-12", "游戏甲", "联运", "渠道一", 2_000_000, 5_000, 1_500_000, 1_500_000, "CNY"],
            ["2026-01", "游戏甲", "联运", "渠道一", 2_400_000, 6_000, 1_800_000, 1_800_000, "CNY"],
            ["2026-02", "游戏甲", "联运", "渠道一", 2_100_000, 6_350.99, 1_572_535.88, 1_572_535.88, "CNY"],
        ], preface=True)
        overseas_rows = [
            [f"2025-{month:02d}", "游戏乙", "海外发行", "渠道二", 30_000 + month, 0, 20_000 + month, 20_000 + month, "USD"]
            for month in range(1, 13)
        ]
        self.overseas = make_settlement_book(root / "海外结算.xlsx", "商店金流账单-海外", overseas_rows)
        self.google = make_settlement_book(root / "Google渠道.xlsx", "汇总", [
            ["2026-01", "游戏丙", "Android", "Google Play", 2_000_000, 0, 1_399_607.95, 1_399_607.95, "HKD"],
            ["2026-02", "游戏丙", "Android", "Google Play", 2_300_000, 0, 1_600_000, 1_600_000, "HKD"],
        ])

    def tearDown(self):
        self.temp.cleanup()

    def test_domestic_settlement_rows(self):
        records = parse_workbook(self.domestic)
        self.assertEqual(len(records), 3)
        self.assertEqual(round(sum(r.settlement_amount or 0 for r in records), 2), 4_872_535.88)
        self.assertEqual(round(sum(r.refunds for r in records), 2), 17_350.99)
        self.assertEqual({r.period for r in records}, {"2025-12", "2026-01", "2026-02"})

    def test_foreign_settlement_rows(self):
        records = parse_workbook(self.overseas)
        self.assertEqual(len(records), 12)
        self.assertEqual({r.currency for r in records}, {"USD"})
        self.assertFalse(any(r.anomalies for r in records))

    def test_summary_never_combines_currencies(self):
        payload = dashboard_payload(parse_workbook(self.domestic) + parse_workbook(self.overseas))
        self.assertEqual(set(payload["summary"]["currencies"]), {"CNY", "USD"})

    def test_google_summary_is_not_double_counted(self):
        records = parse_workbook(self.google)
        self.assertEqual(len(records), 2)
        self.assertEqual(round(sum(r.settlement_amount or 0 for r in records), 2), 2_999_607.95)

    def test_discovery_proposes_a_reusable_semantic_mapping(self):
        selected = discover_workbook(self.domestic)["selected"]
        self.assertEqual(selected["sheet"], "对外账单-发行")
        self.assertEqual(selected["header_row"], 4)
        self.assertEqual(selected["mapping"]["period"], 0)
        self.assertEqual(selected["mapping"]["settlement"], 6)
        self.assertEqual(len(selected["fingerprint"]), 16)

    def test_confirmed_mapping_matches_automatic_import(self):
        selected = discover_workbook(self.domestic)["selected"]
        records = parse_workbook_configured(
            self.domestic, {**selected, "defaults": {}, "formula_mode": "declared"},
        )
        self.assertEqual(len(records), 3)
        self.assertEqual(round(sum(r.settlement_amount or 0 for r in records), 2), 4_872_535.88)

    def test_discovery_prefers_summary_sheet(self):
        selected = discover_workbook(self.google)["selected"]
        self.assertEqual(selected["sheet"], "汇总")
        records = parse_workbook_configured(
            self.google, {**selected, "defaults": {"currency": "HKD"}, "formula_mode": "declared"},
        )
        self.assertEqual(len(records), 2)


if __name__ == "__main__":
    unittest.main()
