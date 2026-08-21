from __future__ import annotations

import unittest

from src.cfo_control_overlay import (
    CfoControlOverlayError,
    build_cfo_control_overlay,
)


class CfoControlOverlayTests(unittest.TestCase):
    def test_game_overlay_focuses_settlement_projects_and_prepaids(self):
        result = build_cfo_control_overlay({
            "core.finance", "industry.game_studio", "channel.app_store",
            "connector.file_import", "connector.xero",
        }, runtime_fingerprint="a" * 64)
        self.assertEqual(result["business_model_type_ids"], ["game_studio"])
        self.assertIn(
            "platform_settlement_completeness",
            result["monthly_control_objective_type_ids"],
        )
        self.assertIn(
            "license_cloud_and_prepaid_period_release",
            result["monthly_control_objective_type_ids"],
        )
        self.assertIn(
            "xero_trial_balance_is_control_total_not_period_activity",
            result["source_boundary_type_ids"],
        )
        self.assertFalse(result["method_extension_required"])
        self.assertFalse(result["financial_values_returned"])
        self.assertEqual(result["runtime_fingerprint"], "a" * 64)

    def test_dtc_and_marketplace_have_distinct_control_questions(self):
        dtc = build_cfo_control_overlay({
            "core.finance", "industry.commerce", "channel.dtc_storefront",
            "connector.shopify", "connector.stripe",
        })
        marketplace = build_cfo_control_overlay({
            "core.finance", "industry.commerce",
            "channel.marketplace_commerce", "connector.amazon_seller",
        })
        self.assertEqual(dtc["business_model_type_ids"], ["dtc_store"])
        self.assertEqual(
            marketplace["business_model_type_ids"], ["marketplace_seller"],
        )
        self.assertIn(
            "do_orders_payments_refunds_and_payouts_share_scope",
            dtc["founder_review_question_type_ids"],
        )
        self.assertIn(
            "do_orders_finances_and_inventory_share_scope",
            marketplace["founder_review_question_type_ids"],
        )
        self.assertIn(
            "amazon_current_inventory_is_not_historical_period_end",
            marketplace["source_boundary_type_ids"],
        )
        self.assertNotEqual(
            dtc["monthly_control_objective_type_ids"],
            marketplace["monthly_control_objective_type_ids"],
        )

    def test_combined_commerce_channels_are_composable_and_deduplicated(self):
        result = build_cfo_control_overlay({
            "core.finance", "industry.commerce", "channel.dtc_storefront",
            "channel.marketplace_commerce", "connector.file_import",
        })
        self.assertEqual(
            result["business_model_type_ids"],
            ["dtc_store", "marketplace_seller"],
        )
        self.assertEqual(
            len(result["monthly_control_objective_type_ids"]),
            len(set(result["monthly_control_objective_type_ids"])),
        )

    def test_unknown_connector_requires_method_extension_without_leaking_id(self):
        result = build_cfo_control_overlay({
            "core.finance", "industry.commerce", "channel.dtc_storefront",
            "connector.forked_provider",
        })
        self.assertTrue(result["method_extension_required"])
        self.assertEqual(result["uncovered_connector_pack_count"], 1)
        self.assertIn(
            "unmapped_connector_requires_method_extension_before_shadow_use",
            result["source_boundary_type_ids"],
        )
        self.assertNotIn("connector.forked_provider", str(result))

    def test_invalid_industry_and_fingerprint_fail_closed(self):
        with self.assertRaisesRegex(CfoControlOverlayError, "exactly one"):
            build_cfo_control_overlay({"core.finance"})
        with self.assertRaisesRegex(CfoControlOverlayError, "exactly one"):
            build_cfo_control_overlay({
                "industry.game_studio", "industry.commerce",
                "channel.dtc_storefront",
            })
        with self.assertRaisesRegex(CfoControlOverlayError, "DTC or Marketplace"):
            build_cfo_control_overlay({"industry.commerce"})
        with self.assertRaisesRegex(CfoControlOverlayError, "fingerprint"):
            build_cfo_control_overlay(
                {"industry.game_studio"}, runtime_fingerprint="bad",
            )


if __name__ == "__main__":
    unittest.main()
