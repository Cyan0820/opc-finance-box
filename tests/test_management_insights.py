import unittest

from src.management_insights import build_change_attribution, build_proactive_insights


class ManagementInsightsTests(unittest.TestCase):
    def test_change_is_decomposed_without_losing_residual(self):
        data = {
            "settlements": [
                {"period": "2026-01", "game": "G", "channel": "Apple", "settlement_amount": 100},
                {"period": "2026-02", "game": "G", "channel": "Apple", "settlement_amount": 130},
            ],
            "game_kpis": [
                {"period": "2026-01", "project_code": "G", "mau": 100, "payers": 10, "gross_bookings": 1000, "marketing_spend": 100, "retention_d7": .3, "status": "可用"},
                {"period": "2026-02", "project_code": "G", "mau": 110, "payers": 11, "gross_bookings": 1320, "marketing_spend": 120, "retention_d7": .28, "status": "可用"},
            ],
        }
        result = build_change_attribution(data, "2026-02")
        self.assertEqual(result["settlement_change"], 30)
        driver = result["operating_drivers"][0]
        self.assertAlmostEqual(driver["explained"] + driver["residual"], driver["gross_change"], places=2)

    def test_missing_kpis_is_reported_not_invented(self):
        result = build_change_attribution({"settlements": [], "game_kpis": []}, "2026-02")
        self.assertFalse(result["operating_drivers"])
        self.assertTrue(result["limitations"])

    def test_foreign_currencies_are_not_added_without_period_rates(self):
        data = {"settlements": [
            {"id": "S1", "period": "2026-01", "game": "G", "channel": "Apple", "currency": "USD", "settlement_amount": 100},
            {"id": "S2", "period": "2026-02", "game": "G", "channel": "Apple", "currency": "USD", "settlement_amount": 110},
        ], "game_kpis": []}
        blocked = build_change_attribution(data, "2026-02")
        self.assertEqual(blocked["settlement_change"], 0)
        self.assertEqual(len(blocked["unconverted_settlements"]), 2)
        converted = build_change_attribution(data, "2026-02", {
            "fx_policy": {"month_end_rates": {"2026-01": {"USD": 7}, "2026-02": {"USD": 7.2}}}
        })
        self.assertEqual(converted["settlement_change"], 92)

    def test_proactive_insight_flags_concentration_and_overdue(self):
        bp = {"totals": {"revenue": 100}, "projects": [{"project_name": "G", "revenue": 90}]}
        flows = {"receivables": {"overdue_count": 2}, "alerts": []}
        signals = build_proactive_insights(bp, flows, {"operating_drivers": []})
        self.assertTrue(any(item["type"] == "收入集中" for item in signals))
        self.assertTrue(any(item["type"] == "回款" for item in signals))


if __name__ == "__main__":
    unittest.main()
