import json
import unittest
from pathlib import Path

from src.box_runtime import BoxRuntime
from src.box_service_api import dispatch_box_service_request
from src.default_services import build_default_service_registry


ROOT = Path(__file__).resolve().parents[1]


class ExampleServiceRequestTests(unittest.TestCase):
    def test_all_documented_service_requests_execute_at_their_honest_boundary(self):
        cases = {
            "dtc_margin.json": ("cn_dtc_store.json", lambda output: output["ready"]),
            "marketplace_inventory.json": (
                "cn_marketplace_store.json", lambda output: not output["ready"]
                and not output["posting_or_inventory_adjustment_performed"]
            ),
            "game_goal_draft.json": (
                "global_game_studio.json", lambda output: not output["state_changed"]
            ),
            "game_management_consolidation.json": (
                "global_game_studio.json", lambda output: output["ready"]
                and not output["statutory_books_modified"]
            ),
            "sg_tax_calendar.json": (
                "global_game_studio.json", lambda output: output["tasks"]
                and all(task["candidate_only"] and not task["filing_completed"] for task in output["tasks"])
            ),
            "cn_vat_workpaper.json": (
                "global_game_studio.json", lambda output: not output["filing_performed"]
                and not output["external_submission_enabled"]
            ),
            "stripe_payout_reconciliation.json": (
                "cn_dtc_stripe_store.json", lambda output: output["ready"]
                and output["candidate_only"]
                and output["reconciliation"][0]["reconciliation_status"] == "high_confidence_candidate"
                and not output["posting_performed"]
            ),
            "ca_registration_profile.json": (
                "ca_dtc_shopify_stripe_federal_corporation.json",
                lambda output: output["ready"]
                and not output["ccpc_status_determined"]
                and not output["tax_calculation_performed"]
                and not output["external_submission_enabled"],
            ),
            "nz_registration_profile.json": (
                "nz_dtc_shopify_stripe_limited_company.json",
                lambda output: output["ready"]
                and not output["new_zealand_tax_residency_determined"]
                and not output["tax_calculation_performed"]
                and not output["external_submission_enabled"],
            ),
            "ie_registration_profile.json": (
                "ie_dtc_shopify_stripe_ltd.json",
                lambda output: output["ready"]
                and not output["irish_tax_residency_determined"]
                and not output["tax_calculation_performed"]
                and not output["external_submission_enabled"],
            ),
            "nl_registration_profile.json": (
                "nl_dtc_shopify_stripe_bv.json",
                lambda output: output["ready"]
                and not output["dutch_tax_residency_determined"]
                and not output["fiscal_unity_determined"]
                and not output["tax_calculation_performed"]
                and not output["external_submission_enabled"],
            ),
            "de_registration_profile.json": (
                "de_dtc_shopify_stripe_gmbh.json",
                lambda output: output["ready"]
                and not output["german_tax_residency_determined"]
                and not output["municipal_trade_tax_rate_determined"]
                and not output["tax_calculation_performed"]
                and not output["external_submission_enabled"],
            ),
            "fr_registration_profile.json": (
                "fr_dtc_shopify_stripe_sasu.json",
                lambda output: output["ready"]
                and not output["french_tax_residency_determined"]
                and not output["profit_tax_regime_determined"]
                and not output["tax_calculation_performed"]
                and not output["external_submission_enabled"],
            ),
            "jp_registration_profile.json": (
                "jp_dtc_shopify_stripe_kk.json",
                lambda output: output["ready"]
                and not output["japanese_tax_residency_determined"]
                and not output["consumption_taxable_person_status_determined"]
                and not output["tax_calculation_performed"]
                and not output["external_submission_enabled"],
            ),
            "kr_registration_profile.json": (
                "kr_dtc_shopify_stripe_jusik_hoesa.json",
                lambda output: output["ready"]
                and not output["korean_tax_residency_determined"]
                and not output["vat_taxpayer_status_determined"]
                and not output["tax_calculation_performed"]
                and not output["external_submission_enabled"],
            ),
            "ae_registration_profile.json": (
                "ae_dtc_shopify_stripe_free_zone_company.json",
                lambda output: output["ready"]
                and not output["uae_tax_residency_determined"]
                and not output["qualifying_free_zone_person_status_determined"]
                and not output["tax_calculation_performed"]
                and not output["external_submission_enabled"],
            ),
        }
        registry = build_default_service_registry()
        for request_name, (box_name, assertion) in cases.items():
            with self.subTest(request=request_name):
                runtime = BoxRuntime(ROOT / "examples" / "boxes" / box_name, ROOT / "packs")
                request = json.loads(
                    (ROOT / "examples" / "service_requests" / request_name).read_text(encoding="utf-8")
                )
                output = dispatch_box_service_request(runtime, registry, request)["output"]
                self.assertTrue(assertion(output), output)


if __name__ == "__main__":
    unittest.main()
