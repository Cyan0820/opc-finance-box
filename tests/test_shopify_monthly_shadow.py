from __future__ import annotations

import json
import hashlib
import tempfile
import unittest
from pathlib import Path

from src.box_pipeline import dispatch_box_pipeline_request
from src.box_runtime import BoxRuntime
from src.connector_shadow_artifacts import (
    ConnectorShadowArtifactError,
    assess_connector_shadow_artifacts,
    build_connector_shadow_baseline_workpaper,
    finalize_connector_shadow_baseline_workpaper,
    review_connector_shadow_artifact,
    verify_connector_shadow_artifact,
    write_shopify_stripe_monthly_shadow_observation,
    write_stripe_shadow_observation,
)
from src.connector_shadow_registry import build_connector_shadow_registry_workspace


ROOT = Path(__file__).resolve().parents[1]
BOX = ROOT / "examples" / "boxes" / "cn_dtc_shopify_stripe_store.json"
REQUEST = ROOT / "examples" / "pipelines" / "shopify_stripe_month_close_fixture.json"


class ShopifyMonthlyShadowTests(unittest.TestCase):
    def setUp(self):
        self.runtime = BoxRuntime(BOX, ROOT / "packs")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def _live_result(self) -> dict:
        request = json.loads(REQUEST.read_text(encoding="utf-8"))
        result = dispatch_box_pipeline_request(self.runtime, request)
        result["network_access_performed"] = True
        for batch in result["connector_batches"].values():
            batch["source"]["kind"] = "api"
            batch["source"]["network_access_performed"] = True
        return result

    def _rehash_observation(self, observation: dict) -> None:
        core = {
            key: value for key, value in observation.items()
            if key != "observation_fingerprint"
        }
        observation["observation_fingerprint"] = hashlib.sha256(
            json.dumps(
                core, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            ).encode()
        ).hexdigest()

    def _baseline(self) -> Path:
        workpaper_path = Path(self.temp.name) / "workpaper.json"
        created = build_connector_shadow_baseline_workpaper(
            self.runtime,
            pipeline_id="dtc.shopify_stripe_month_close",
            entity_id="cn_dtc_company",
            sample_period="2026-07",
            prepared_by="independent-monthly-source-preparer",
            output=workpaper_path,
        )
        self.assertEqual(created["source_count"], 2)
        self.assertEqual(created["control_count"], 21)
        workpaper = json.loads(workpaper_path.read_text(encoding="utf-8"))
        self.assertEqual(workpaper["covered_pack_ids"], [
            "connector.shopify", "feature.shopify_stripe_order_to_cash",
        ])
        self.assertEqual(
            [item["connector_id"] for item in workpaper["source_expectations"]],
            ["shopify.monthly_order_evidence", "stripe.balance_transactions"],
        )
        workpaper["source_independence"] = {
            "prepared_from_independent_source": True,
            "pipeline_output_used_as_baseline": False,
            "source_scope_confirmed": True,
        }
        workpaper["anonymization"]["private_source_evidence_retained"] = True
        expected_source_counts = {
            "shopify.monthly_order_evidence": 5,
            "stripe.balance_transactions": 2,
        }
        for item in workpaper["source_expectations"]:
            item["expected_record_count"] = expected_source_counts[item["connector_id"]]
            item["evidence_references"] = [
                f"private-export://cn-dtc/2026-07/{item['connector_id']}"
            ]
        expected_controls = {
            "pipeline_ready": True,
            "shopify_order_count": 2,
            "shopify_transaction_count": 2,
            "shopify_refund_count": 1,
            "stripe_balance_transaction_count": 2,
            "created_population_count": 1,
            "updated_population_count": 1,
            "deduplicated_order_count": 2,
            "monthly_created_order_count": 1,
            "monthly_refund_event_count": 1,
            "processor_link_matched_count": 2,
            "processor_link_exception_count": 0,
            "shopify_network_read_performed": True,
            "stripe_network_read_performed": True,
            "canonical_month_half_open_window": True,
            "close_capture_within_72_hours": True,
            "created_and_updated_population_contract": True,
            "refund_processed_at_membership": True,
            "refund_component_and_transaction_reconciled": True,
            "entity_scope_matched": True,
            "candidate_only_no_external_actions": True,
        }
        for item in workpaper["control_expectations"]:
            item["expected_value"] = expected_controls[item["control_id"]]
        workpaper["evidence_references"] = [
            "workpaper://cn-dtc/2026-07/monthly-close-source-scope",
        ]
        workpaper["finalization_ready"] = True
        workpaper_path.write_text(json.dumps(workpaper), encoding="utf-8")
        baseline_path = Path(self.temp.name) / "baseline.json"
        finalized = finalize_connector_shadow_baseline_workpaper(
            self.runtime, workpaper_path, baseline_path,
        )
        self.assertTrue(finalized["real_sample_evidence"])
        return baseline_path

    def test_real_monthly_observation_assesses_reviews_and_verifies_without_values(self):
        baseline_path = self._baseline()
        observation_path = Path(self.temp.name) / "observation.json"
        observed = write_shopify_stripe_monthly_shadow_observation(
            self.runtime, self._live_result(), observation_path,
        )
        self.assertEqual(observed["sample_period"], "2026-07")
        self.assertEqual(observed["shopify_record_count"], 5)
        observation_text = observation_path.read_text(encoding="utf-8")
        for forbidden in (
            "104.50", "shop_domain", "opc-demo.myshopify.com", "gid://",
            "txn_month", "re_month", "amount_minor",
        ):
            self.assertNotIn(forbidden, observation_text)

        assessment_path = Path(self.temp.name) / "assessment.json"
        assessed = assess_connector_shadow_artifacts(
            self.runtime, baseline_path, observation_path, assessment_path,
        )
        self.assertTrue(assessed["passed"])
        self.assertEqual(assessed["source_count"], 2)
        self.assertEqual(assessed["control_count"], 21)
        assessment_text = assessment_path.read_text(encoding="utf-8")
        self.assertNotIn("monthly_commerce_scope", assessment_text)
        self.assertNotIn("financial_amounts", assessment_text.lower().replace(
            '"financial_amounts_included": false', ""
        ))

        reviewed_path = Path(self.temp.name) / "reviewed.json"
        review_connector_shadow_artifact(
            self.runtime,
            assessment_path,
            reviewed_path,
            decision="passed",
            actor="independent-monthly-shadow-reviewer",
            rationale="月结双人口、同窗处理器活动、退款闭合与只读边界均已独立复核",
            evidence_references=["review://cn-dtc/2026-07/monthly-connector-shadow"],
        )
        verified = verify_connector_shadow_artifact(self.runtime, reviewed_path)
        self.assertTrue(verified["valid"])
        self.assertEqual(verified["pipeline_id"], "dtc.shopify_stripe_month_close")
        self.assertTrue(verified["real_sample_evidence"])
        self.assertEqual(verified["decision"], "passed")

    def test_fixture_or_late_capture_cannot_be_promoted_to_real_observation(self):
        request = json.loads(REQUEST.read_text(encoding="utf-8"))
        fixture_result = dispatch_box_pipeline_request(self.runtime, request)
        with self.assertRaisesRegex(ConnectorShadowArtifactError, "network sources"):
            write_shopify_stripe_monthly_shadow_observation(
                self.runtime,
                fixture_result,
                Path(self.temp.name) / "fixture-observation.json",
            )

        late = self._live_result()
        late["connector_batches"]["shopify.monthly_order_evidence"]["source"][
            "source_observed_at"
        ] = "2026-08-04T00:00:01Z"
        with self.assertRaisesRegex(ConnectorShadowArtifactError, "clean, reconciled"):
            write_shopify_stripe_monthly_shadow_observation(
                self.runtime,
                late,
                Path(self.temp.name) / "late-observation.json",
            )

    def test_observation_tampering_fails_before_assessment(self):
        baseline_path = self._baseline()
        observation_path = Path(self.temp.name) / "observation.json"
        write_shopify_stripe_monthly_shadow_observation(
            self.runtime, self._live_result(), observation_path,
        )
        observation = json.loads(observation_path.read_text(encoding="utf-8"))
        observation["connector_batches"]["shopify.monthly_order_evidence"]["source"][
            "created_population_count"
        ] = 99
        observation_path.write_text(json.dumps(observation), encoding="utf-8")
        with self.assertRaisesRegex(ConnectorShadowArtifactError, "integrity or privacy"):
            assess_connector_shadow_artifacts(
                self.runtime,
                baseline_path,
                observation_path,
                Path(self.temp.name) / "assessment-never.json",
            )

        injected_path = Path(self.temp.name) / "injected-observation.json"
        write_shopify_stripe_monthly_shadow_observation(
            self.runtime, self._live_result(), injected_path,
        )
        injected = json.loads(injected_path.read_text(encoding="utf-8"))
        injected["connector_batches"]["shopify.monthly_order_evidence"]["source"][
            "api_version"
        ] = "private-store.myshopify.com"
        self._rehash_observation(injected)
        injected_path.write_text(json.dumps(injected), encoding="utf-8")
        with self.assertRaisesRegex(ConnectorShadowArtifactError, "integrity or privacy"):
            assess_connector_shadow_artifacts(
                self.runtime,
                baseline_path,
                injected_path,
                Path(self.temp.name) / "assessment-injected-never.json",
            )

        inconsistent_path = Path(self.temp.name) / "inconsistent-observation.json"
        write_shopify_stripe_monthly_shadow_observation(
            self.runtime, self._live_result(), inconsistent_path,
        )
        inconsistent = json.loads(inconsistent_path.read_text(encoding="utf-8"))
        inconsistent["connector_batches"]["shopify.monthly_order_evidence"]["quality"][
            "record_count"
        ] += 1
        self._rehash_observation(inconsistent)
        inconsistent_path.write_text(json.dumps(inconsistent), encoding="utf-8")
        with self.assertRaisesRegex(ConnectorShadowArtifactError, "integrity or privacy"):
            assess_connector_shadow_artifacts(
                self.runtime,
                baseline_path,
                inconsistent_path,
                Path(self.temp.name) / "assessment-inconsistent-never.json",
            )

    def test_monthly_shopify_and_stripe_profiles_complete_registry_coverage(self):
        registry = Path(self.temp.name) / "registry"
        registry.mkdir(mode=0o700)

        monthly_baseline = self._baseline()
        monthly_observation = Path(self.temp.name) / "monthly-observation.json"
        write_shopify_stripe_monthly_shadow_observation(
            self.runtime, self._live_result(), monthly_observation,
        )
        monthly_assessment = Path(self.temp.name) / "monthly-assessment.json"
        assess_connector_shadow_artifacts(
            self.runtime, monthly_baseline, monthly_observation, monthly_assessment,
        )
        review_connector_shadow_artifact(
            self.runtime,
            monthly_assessment,
            registry / "monthly-reviewed.json",
            decision="passed",
            actor="monthly-registry-reviewer",
            rationale="已独立复核 Shopify 月结双人口与同窗 Stripe 证据",
            evidence_references=["review://cn-dtc/2026-07/monthly-registry"],
        )

        stripe_workpaper_path = Path(self.temp.name) / "stripe-workpaper.json"
        build_connector_shadow_baseline_workpaper(
            self.runtime,
            pipeline_id="stripe.daily_close",
            entity_id="cn_dtc_company",
            sample_period="2026-07",
            prepared_by="independent-stripe-source-preparer",
            output=stripe_workpaper_path,
        )
        stripe_workpaper = json.loads(stripe_workpaper_path.read_text(encoding="utf-8"))
        stripe_workpaper["source_independence"] = {
            "prepared_from_independent_source": True,
            "pipeline_output_used_as_baseline": False,
            "source_scope_confirmed": True,
        }
        stripe_workpaper["anonymization"]["private_source_evidence_retained"] = True
        stripe_source_counts = {
            "stripe.balance_transactions": 3,
            "stripe.payouts": 1,
        }
        for item in stripe_workpaper["source_expectations"]:
            item["expected_record_count"] = stripe_source_counts[item["connector_id"]]
            item["evidence_references"] = [
                f"private-export://cn-dtc/2026-07/{item['connector_id']}"
            ]
        stripe_controls = {
            "pipeline_ready": True,
            "balance_transaction_count": 3,
            "payout_count": 1,
            "payout_bank_candidate_count": 1,
            "payout_bank_exception_count": 0,
        }
        for item in stripe_workpaper["control_expectations"]:
            item["expected_value"] = stripe_controls[item["control_id"]]
        stripe_workpaper["evidence_references"] = [
            "workpaper://cn-dtc/2026-07/stripe-source-scope",
        ]
        stripe_workpaper["finalization_ready"] = True
        stripe_workpaper_path.write_text(json.dumps(stripe_workpaper), encoding="utf-8")
        stripe_baseline = Path(self.temp.name) / "stripe-baseline.json"
        finalize_connector_shadow_baseline_workpaper(
            self.runtime, stripe_workpaper_path, stripe_baseline,
        )
        stripe_request = json.loads((
            ROOT / "examples" / "pipelines" / "stripe_daily_close_fixture.json"
        ).read_text(encoding="utf-8"))
        stripe_result = dispatch_box_pipeline_request(self.runtime, stripe_request)
        stripe_result["network_access_performed"] = True
        for batch in stripe_result["connector_batches"].values():
            batch["source"].update({
                "kind": "api",
                "network_access_performed": True,
                "rate_limit_count": 0,
                "retry_delay_seconds_total": 0.0,
                "retry_after_honored": True,
                "created_window": {
                    "gte": 1782864000,
                    "lt": 1785542400,
                    "semantics": "half_open_unix_seconds",
                    "complete_bounds_declared": True,
                },
            })
        stripe_result_path = Path(self.temp.name) / "stripe-observation.json"
        write_stripe_shadow_observation(
            self.runtime, stripe_result, stripe_result_path,
        )
        stripe_assessment = Path(self.temp.name) / "stripe-assessment.json"
        assessed = assess_connector_shadow_artifacts(
            self.runtime, stripe_baseline, stripe_result_path, stripe_assessment,
        )
        self.assertTrue(assessed["passed"])
        review_connector_shadow_artifact(
            self.runtime,
            stripe_assessment,
            registry / "stripe-reviewed.json",
            decision="passed",
            actor="stripe-registry-reviewer",
            rationale="已独立复核 Stripe Balance、Payout 与银行候选控制",
            evidence_references=["review://cn-dtc/2026-07/stripe-registry"],
        )

        workspace = build_connector_shadow_registry_workspace(
            self.runtime, registry,
        )
        self.assertTrue(workspace["summary"]["ready_for_connector_shadow_evidence"])
        self.assertEqual(workspace["summary"]["covered_network_pack_count"], 2)
        self.assertEqual(workspace["summary"]["current_artifact_count"], 2)
        self.assertEqual(
            {item["pipeline_id"] for item in workspace["current_artifacts"]},
            {"dtc.shopify_stripe_month_close", "stripe.daily_close"},
        )


if __name__ == "__main__":
    unittest.main()
