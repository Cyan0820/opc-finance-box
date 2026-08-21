import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook

from src.game_kpis import enrich_kpis, parse_kpi_workbook


class GameKpiTests(unittest.TestCase):
    def test_kpi_import_derives_business_drivers(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "运营.xlsx"
            wb = Workbook()
            ws = wb.active
            ws.title = "经营KPI"
            ws.append(["月份", "游戏项目编码", "渠道", "MAU", "付费用户", "流水", "投放金额", "安装数", "次留"])
            ws.append(["2026-07", "G001", "App Store", 1000, 100, 50000, 10000, 2000, "40%"])
            wb.save(path)
            rows = parse_kpi_workbook(path)
        enriched = enrich_kpis(rows)
        self.assertEqual(len(enriched), 1)
        self.assertEqual(enriched[0]["arppu"], 500)
        self.assertEqual(enriched[0]["gross_roas"], 5)
        self.assertEqual(enriched[0]["retention_d1"], 0.4)


if __name__ == "__main__":
    unittest.main()
