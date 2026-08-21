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
    build_connector_shadow_baseline_plan,
    build_connector_shadow_baseline_workpaper,
    finalize_connector_shadow_baseline_workpaper,
    review_connector_shadow_artifact,
    verify_connector_shadow_artifact,
    write_airwallex_shadow_observation,
)


ROOT = Path(__file__).resolve().parents[1]
BOX = ROOT / "examples" / "boxes" / "sg_dtc_shopify_stripe_wise_store.json"
BASELINE = ROOT / "examples" / "shadow" / "sg_shopify_stripe_wise_connector_baseline.json"
REQUEST = ROOT / "examples" / "pipelines" / "shopify_stripe_wise_daily_close_fixture.json"
AIRWALLEX_BOX = ROOT / "examples" / "boxes" / "sg_dtc_shopify_stripe_wise_airwallex_store.json"
AIRWALLEX_BASELINE = ROOT / "examples" / "shadow" / "sg_airwallex_expense_connector_baseline.json"
AIRWALLEX_FIXTURE = ROOT / "packs" / "connectors" / "airwallex" / "fixture-approved-expenses.json"
SHOPIFY_STRIPE_BOX = ROOT / "examples" / "boxes" / "cn_dtc_shopify_stripe_store.json"


class ConnectorShadowArtifactTests(unittest.TestCase):
    def setUp(self):
        self.runtime = BoxRuntime(BOX, ROOT / "packs")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        request = json.loads(REQUEST.read_text(encoding="utf-8"))
        self.result = dispatch_box_pipeline_request(self.runtime, request)
        self.result_path = Path(self.temp.name) / "pipeline-result.json"
        self.result_path.write_text(json.dumps({"ok": True, "result": self.result}), encoding="utf-8")

    def test_assess_review_verify_bind_counts_without_financial_values(self):
        assessment_path = Path(self.temp.name) / "assessment.json"
        summary = assess_connector_shadow_artifacts(
            self.runtime, BASELINE, self.result_path, assessment_path,
        )
        self.assertTrue(summary["passed"])
        self.assertEqual(summary["source_count"], 4)
        self.assertEqual(summary["control_count"], 5)
        artifact = json.loads(assessment_path.read_text())
        serialized = json.dumps(artifact)
        for forbidden in ("amount_minor", "80.27", "8027", "customer", "bank_transactions"):
            self.assertNotIn(forbidden, serialized.lower())
        reviewed_path = Path(self.temp.name) / "reviewed.json"
        reviewed = review_connector_shadow_artifact(
            self.runtime, assessment_path, reviewed_path, decision="passed",
            actor="independent-shadow-reviewer", rationale="四个来源计数与五项控制已独立核对",
            evidence_references=["review://sg-store/2026-08/connector-shadow"],
        )
        self.assertTrue(reviewed["review_current"])
        verified = verify_connector_shadow_artifact(self.runtime, reviewed_path)
        self.assertTrue(verified["valid"])
        self.assertEqual(verified["decision"], "passed")
        self.assertEqual(
            verified["covered_pack_ids"],
            [
                "connector.shopify", "connector.stripe", "connector.wise",
                "feature.shopify_stripe_order_to_cash",
            ],
        )
        self.assertEqual(verified["review_actor"], "independent-shadow-reviewer")
        self.assertRegex(verified["reviewed_at"], r"Z$")
        self.assertFalse(verified["raw_source_values_returned"])

    def test_mismatch_is_visible_and_preparer_cannot_self_review(self):
        baseline = json.loads(BASELINE.read_text())
        baseline["source_expectations"][0]["expected_record_count"] = 999
        baseline_path = Path(self.temp.name) / "mismatch-baseline.json"
        baseline_path.write_text(json.dumps(baseline), encoding="utf-8")
        assessment_path = Path(self.temp.name) / "assessment.json"
        summary = assess_connector_shadow_artifacts(
            self.runtime, baseline_path, self.result_path, assessment_path,
        )
        self.assertFalse(summary["passed"])
        with self.assertRaisesRegex(ConnectorShadowArtifactError, "differ"):
            review_connector_shadow_artifact(
                self.runtime, assessment_path, Path(self.temp.name) / "reviewed.json",
                decision="accepted-differences", actor="independent-source-preparer",
                rationale="attempted self review", evidence_references=["review://attempt"],
            )

    def test_shopify_stripe_real_baseline_matches_optional_wise_scope_and_rejects_fixture_run(self):
        runtime = BoxRuntime(SHOPIFY_STRIPE_BOX, ROOT / "packs")
        workpaper_path = Path(self.temp.name) / "shopify-stripe-real-workpaper.json"
        created = build_connector_shadow_baseline_workpaper(
            runtime,
            pipeline_id="dtc.shopify_stripe_daily_close",
            entity_id="cn_dtc_company",
            sample_period="2026-08",
            prepared_by="independent-real-source-preparer",
            output=workpaper_path,
        )
        self.assertEqual(created["source_count"], 3)
        workpaper = json.loads(workpaper_path.read_text(encoding="utf-8"))
        self.assertEqual(
            workpaper["covered_pack_ids"],
            [
                "connector.shopify", "connector.stripe",
                "feature.shopify_stripe_order_to_cash",
            ],
        )
        self.assertNotIn(
            "wise.balance_statement",
            {item["connector_id"] for item in workpaper["source_expectations"]},
        )
        workpaper["source_independence"] = {
            "prepared_from_independent_source": True,
            "pipeline_output_used_as_baseline": False,
            "source_scope_confirmed": True,
        }
        workpaper["anonymization"]["private_source_evidence_retained"] = True
        source_counts = {
            "shopify.orders": 4,
            "stripe.balance_transactions": 3,
            "stripe.payouts": 1,
        }
        for item in workpaper["source_expectations"]:
            item["expected_record_count"] = source_counts[item["connector_id"]]
            item["evidence_references"] = [
                f"private-export://cn-store/2026-08/{item['connector_id']}"
            ]
        expected_controls = {
            "pipeline_ready": True,
            "processor_link_matched_count": 2,
            "processor_link_exception_count": 0,
            "payout_bank_candidate_count": 1,
            "payout_bank_exception_count": 0,
        }
        for item in workpaper["control_expectations"]:
            item["expected_value"] = expected_controls[item["control_id"]]
        workpaper["evidence_references"] = [
            "workpaper://cn-store/2026-08/shopify-stripe-source-scope",
        ]
        workpaper["finalization_ready"] = True
        workpaper_path.write_text(json.dumps(workpaper), encoding="utf-8")
        baseline_path = Path(self.temp.name) / "shopify-stripe-real-baseline.json"
        finalized = finalize_connector_shadow_baseline_workpaper(
            runtime, workpaper_path, baseline_path,
        )
        self.assertTrue(finalized["real_sample_evidence"])

        request = json.loads(
            (ROOT / "examples" / "pipelines" / "shopify_stripe_daily_close_fixture.json").read_text(
                encoding="utf-8"
            )
        )
        fixture_result = dispatch_box_pipeline_request(runtime, request)
        fixture_result_path = Path(self.temp.name) / "shopify-stripe-fixture-result.json"
        fixture_result_path.write_text(json.dumps(fixture_result), encoding="utf-8")
        assessment_path = Path(self.temp.name) / "shopify-stripe-assessment.json"
        summary = assess_connector_shadow_artifacts(
            runtime, baseline_path, fixture_result_path, assessment_path,
        )
        self.assertFalse(summary["passed"])
        assessment = json.loads(assessment_path.read_text(encoding="utf-8"))
        self.assertTrue(assessment["source_results"])
        self.assertTrue(all(
            item["actual_record_count"] == item["expected_record_count"]
            and item["matched"] is False
            for item in assessment["source_results"]
        ))

        wise_workpaper_path = Path(self.temp.name) / "shopify-stripe-wise-workpaper.json"
        wise_created = build_connector_shadow_baseline_workpaper(
            self.runtime,
            pipeline_id="dtc.shopify_stripe_daily_close",
            entity_id="sg_store",
            sample_period="2026-08",
            prepared_by="independent-real-source-preparer",
            output=wise_workpaper_path,
        )
        self.assertEqual(wise_created["source_count"], 4)
        wise_workpaper = json.loads(wise_workpaper_path.read_text(encoding="utf-8"))
        self.assertIn("connector.wise", wise_workpaper["covered_pack_ids"])
        self.assertIn(
            "wise.balance_statement",
            {item["connector_id"] for item in wise_workpaper["source_expectations"]},
        )

    def test_baseline_plan_and_stripe_only_real_shadow_cover_selected_network_packs(self):
        cases = {
            "cn_dtc_shopify_stripe_store.json": [
                ("dtc.shopify_stripe_month_close", ["connector.shopify"]),
                ("stripe.daily_close", ["connector.stripe"]),
            ],
            "sg_dtc_shopify_stripe_wise_store.json": [
                ("dtc.shopify_stripe_month_close", ["connector.shopify"]),
                ("finance.bank_statement_close", ["connector.wise"]),
                ("stripe.daily_close", ["connector.stripe"]),
            ],
            "cn_dtc_stripe_store.json": [
                ("stripe.daily_close", ["connector.stripe"]),
            ],
            "sg_dtc_wise_store.json": [
                ("finance.bank_statement_close", ["connector.wise"]),
            ],
            "global_game_studio_xero.json": [
                ("finance.trial_balance_review", ["connector.xero"]),
            ],
            "us_dtc_woocommerce_c_corp.json": [
                ("woocommerce.order_refund_close", ["connector.woocommerce"]),
            ],
            "sg_dtc_shopify_stripe_wise_airwallex_store.json": [
                ("dtc.shopify_stripe_month_close", ["connector.shopify"]),
                ("finance.bank_statement_close", ["connector.wise"]),
                ("finance.expense_evidence_review", ["connector.airwallex"]),
                ("stripe.daily_close", ["connector.stripe"]),
            ],
        }
        for box_name, expected in cases.items():
            with self.subTest(box=box_name):
                runtime = BoxRuntime(ROOT / "examples" / "boxes" / box_name, ROOT / "packs")
                plan = build_connector_shadow_baseline_plan(runtime)
                actual = [
                    (
                        item["pipeline_id"],
                        [
                            pack_id for pack_id in item["covered_pack_ids"]
                            if pack_id.startswith("connector.")
                        ],
                    )
                    for item in plan["profiles"]
                ]
                self.assertEqual(actual, expected)
                self.assertFalse(plan["baselines_created"])
                self.assertFalse(plan["external_actions_performed"])

    def test_woocommerce_real_shadow_contract_binds_counts_controls_and_review(self):
        runtime = BoxRuntime(
            ROOT / "examples" / "boxes" / "us_dtc_woocommerce_c_corp.json",
            ROOT / "packs",
        )
        request = json.loads((
            ROOT / "examples" / "pipelines" / "woocommerce_order_refund_close_fixture.json"
        ).read_text(encoding="utf-8"))
        result = dispatch_box_pipeline_request(runtime, request)
        source = result["connector_batches"]["woocommerce.order_refund_activity"]["source"]
        # Unit-test the real-read artifact contract without performing an external call.
        source["kind"] = "api"
        source["network_access_performed"] = True
        source["basic_auth_header_used"] = True
        result["network_access_performed"] = True

        workpaper_path = Path(self.temp.name) / "woocommerce-workpaper.json"
        created = build_connector_shadow_baseline_workpaper(
            runtime,
            pipeline_id="woocommerce.order_refund_close",
            entity_id="us_dtc_company",
            sample_period="2026-08",
            prepared_by="independent-woocommerce-source-preparer",
            output=workpaper_path,
        )
        self.assertEqual(created["source_count"], 1)
        workpaper = json.loads(workpaper_path.read_text(encoding="utf-8"))
        workpaper["source_independence"] = {
            "prepared_from_independent_source": True,
            "pipeline_output_used_as_baseline": False,
            "source_scope_confirmed": True,
        }
        workpaper["anonymization"]["private_source_evidence_retained"] = True
        workpaper["source_expectations"][0]["expected_record_count"] = 2
        workpaper["source_expectations"][0]["evidence_references"] = [
            "private-export://us-dtc/2026-08/woocommerce-counts",
        ]
        expected_controls = {
            "pipeline_ready": True,
            "order_count": 1,
            "refund_event_count": 1,
            "orphan_refund_count": 0,
            "arithmetic_exception_count": 0,
            "destination_review_required_count": 0,
            "unpaid_or_unconfirmed_order_count": 0,
            "network_order_refund_read_performed": True,
            "monthly_half_open_window": True,
            "entity_site_scope_matched": True,
            "customer_pii_excluded": True,
            "product_detail_excluded": True,
            "raw_source_ids_excluded": True,
            "fixed_read_only_transport_controls": True,
            "business_write_posting_revenue_tax_actions_disabled": True,
        }
        for item in workpaper["control_expectations"]:
            item["expected_value"] = expected_controls[item["control_id"]]
        workpaper["evidence_references"] = [
            "workpaper://us-dtc/2026-08/woocommerce-independent-scope",
        ]
        workpaper["finalization_ready"] = True
        workpaper_path.write_text(json.dumps(workpaper), encoding="utf-8")

        baseline_path = Path(self.temp.name) / "woocommerce-baseline.json"
        finalized = finalize_connector_shadow_baseline_workpaper(
            runtime, workpaper_path, baseline_path,
        )
        self.assertTrue(finalized["real_sample_evidence"])
        result_path = Path(self.temp.name) / "woocommerce-result.json"
        result_path.write_text(
            json.dumps({"ok": True, "result": result}), encoding="utf-8",
        )
        assessment_path = Path(self.temp.name) / "woocommerce-assessment.json"
        assessed = assess_connector_shadow_artifacts(
            runtime, baseline_path, result_path, assessment_path,
        )
        self.assertTrue(assessed["passed"])
        self.assertEqual(assessed["control_count"], len(expected_controls))
        reviewed_path = Path(self.temp.name) / "woocommerce-reviewed.json"
        review_connector_shadow_artifact(
            runtime,
            assessment_path,
            reviewed_path,
            decision="passed",
            actor="independent-woocommerce-shadow-reviewer",
            rationale="WooCommerce 来源计数、主体站点绑定与只读控制均已独立复核",
            evidence_references=["review://us-dtc/2026-08/woocommerce-final"],
        )
        verified = verify_connector_shadow_artifact(runtime, reviewed_path)
        self.assertTrue(verified["valid"])
        self.assertEqual(verified["covered_pack_ids"], ["connector.woocommerce"])

        runtime = BoxRuntime(SHOPIFY_STRIPE_BOX.parent / "cn_dtc_stripe_store.json", ROOT / "packs")
        workpaper_path = Path(self.temp.name) / "stripe-real-workpaper.json"
        created = build_connector_shadow_baseline_workpaper(
            runtime,
            pipeline_id="stripe.daily_close",
            entity_id="cn_dtc_company",
            sample_period="2026-08",
            prepared_by="independent-real-source-preparer",
            output=workpaper_path,
        )
        self.assertEqual(created["source_count"], 2)
        workpaper = json.loads(workpaper_path.read_text(encoding="utf-8"))
        workpaper["source_independence"] = {
            "prepared_from_independent_source": True,
            "pipeline_output_used_as_baseline": False,
            "source_scope_confirmed": True,
        }
        workpaper["anonymization"]["private_source_evidence_retained"] = True
        source_counts = {
            "stripe.balance_transactions": 3,
            "stripe.payouts": 1,
        }
        for item in workpaper["source_expectations"]:
            item["expected_record_count"] = source_counts[item["connector_id"]]
            item["evidence_references"] = [
                f"private-export://cn-stripe/2026-08/{item['connector_id']}"
            ]
        expected_controls = {
            "pipeline_ready": True,
            "balance_transaction_count": 3,
            "payout_count": 1,
            "payout_bank_candidate_count": 1,
            "payout_bank_exception_count": 0,
        }
        for item in workpaper["control_expectations"]:
            item["expected_value"] = expected_controls[item["control_id"]]
        workpaper["evidence_references"] = [
            "workpaper://cn-stripe/2026-08/source-scope",
        ]
        workpaper["finalization_ready"] = True
        workpaper_path.write_text(json.dumps(workpaper), encoding="utf-8")
        baseline_path = Path(self.temp.name) / "stripe-real-baseline.json"
        finalize_connector_shadow_baseline_workpaper(
            runtime, workpaper_path, baseline_path,
        )

        request = json.loads(
            (ROOT / "examples" / "pipelines" / "stripe_daily_close_fixture.json").read_text(
                encoding="utf-8"
            )
        )
        fixture_result = dispatch_box_pipeline_request(runtime, request)
        fixture_path = Path(self.temp.name) / "stripe-fixture-result.json"
        fixture_path.write_text(json.dumps(fixture_result), encoding="utf-8")
        fixture_summary = assess_connector_shadow_artifacts(
            runtime, baseline_path, fixture_path,
            Path(self.temp.name) / "stripe-fixture-assessment.json",
        )
        self.assertFalse(fixture_summary["passed"])

        live_result = json.loads(json.dumps(fixture_result))
        live_result["network_access_performed"] = True
        for batch in live_result["connector_batches"].values():
            batch["source"].update({
                "kind": "api",
                "network_access_performed": True,
                "rate_limit_count": 0,
                "retry_delay_seconds_total": 0.0,
                "retry_after_honored": True,
                "created_window": {
                    "gte": 1785542400,
                    "lt": 1788220800,
                    "semantics": "half_open_unix_seconds",
                    "complete_bounds_declared": True,
                },
            })
        live_path = Path(self.temp.name) / "stripe-live-result.json"
        live_path.write_text(json.dumps(live_result), encoding="utf-8")
        live_summary = assess_connector_shadow_artifacts(
            runtime, baseline_path, live_path,
            Path(self.temp.name) / "stripe-live-assessment.json",
        )
        self.assertTrue(live_summary["passed"])

    def test_tampering_and_cross_entity_result_fail_closed(self):
        assessment_path = Path(self.temp.name) / "assessment.json"
        assess_connector_shadow_artifacts(self.runtime, BASELINE, self.result_path, assessment_path)
        artifact = json.loads(assessment_path.read_text())
        artifact["source_results"][0]["actual_record_count"] = 999
        assessment_path.write_text(json.dumps(artifact), encoding="utf-8")
        with self.assertRaisesRegex(ConnectorShadowArtifactError, "fingerprint"):
            verify_connector_shadow_artifact(self.runtime, assessment_path)
        wrong = json.loads(self.result_path.read_text())
        wrong["result"]["lineage"]["entity_id"] = "other"
        wrong_path = Path(self.temp.name) / "wrong.json"
        wrong_path.write_text(json.dumps(wrong), encoding="utf-8")
        with self.assertRaisesRegex(ConnectorShadowArtifactError, "baseline entity"):
            assess_connector_shadow_artifacts(
                self.runtime, BASELINE, wrong_path, Path(self.temp.name) / "wrong-assessment.json",
            )

    def test_wise_statement_window_must_equal_baseline_month(self):
        wrong = json.loads(self.result_path.read_text())
        wrong["result"]["connector_batches"]["wise.balance_statement"]["source"][
            "interval_start"
        ] = "2026-07-01T00:00:00Z"
        wrong_path = Path(self.temp.name) / "wrong-window.json"
        wrong_path.write_text(json.dumps(wrong), encoding="utf-8")
        assessment_path = Path(self.temp.name) / "assessment.json"
        summary = assess_connector_shadow_artifacts(
            self.runtime, BASELINE, wrong_path, assessment_path,
        )
        self.assertFalse(summary["passed"])
        artifact = json.loads(assessment_path.read_text())
        wise = next(
            item for item in artifact["source_results"]
            if item["connector_id"] == "wise.balance_statement"
        )
        self.assertFalse(wise["matched"])

    def test_airwallex_expense_shadow_binds_source_count_review_gaps_and_no_actions(self):
        runtime = BoxRuntime(AIRWALLEX_BOX, ROOT / "packs")
        request = {
            "pipeline_id": "finance.expense_evidence_review",
            "payload": {
                "entity_id": "sg_store",
                "connector_request": json.loads(AIRWALLEX_FIXTURE.read_text(encoding="utf-8")),
            },
        }
        result = dispatch_box_pipeline_request(runtime, request)
        result_path = Path(self.temp.name) / "airwallex-result.json"
        result_path.write_text(json.dumps(result), encoding="utf-8")
        assessment_path = Path(self.temp.name) / "airwallex-assessment.json"
        summary = assess_connector_shadow_artifacts(
            runtime, AIRWALLEX_BASELINE, result_path, assessment_path,
        )
        self.assertTrue(summary["passed"])
        self.assertEqual(summary["source_count"], 1)
        self.assertEqual(summary["control_count"], 7)
        artifact = json.loads(assessment_path.read_text(encoding="utf-8"))
        serialized = json.dumps(artifact, ensure_ascii=False)
        for forbidden in ("12840", "4200", "exp_demo_001", "billing_amount_minor"):
            self.assertNotIn(forbidden, serialized)
        reviewed_path = Path(self.temp.name) / "airwallex-reviewed.json"
        review_connector_shadow_artifact(
            runtime, assessment_path, reviewed_path, decision="passed",
            actor="independent-expense-shadow-reviewer",
            rationale="已独立核对批准费用数量、缺口指标和只读边界",
            evidence_references=["demo-review://sg-store/2026-08/airwallex-shadow"],
        )
        verified = verify_connector_shadow_artifact(runtime, reviewed_path)
        self.assertEqual(verified["pipeline_id"], "finance.expense_evidence_review")
        self.assertEqual(verified["covered_pack_ids"], ["connector.airwallex"])
        self.assertEqual(verified["decision"], "passed")
        self.assertFalse(verified["real_sample_evidence"])
        self.assertEqual(verified["sample_classification"], "demonstration")

    def test_real_airwallex_baseline_workpaper_requires_independent_evidence_before_sealing(self):
        runtime = BoxRuntime(AIRWALLEX_BOX, ROOT / "packs")
        workpaper_path = Path(self.temp.name) / "airwallex-real-workpaper.json"
        created = build_connector_shadow_baseline_workpaper(
            runtime,
            pipeline_id="finance.expense_evidence_review",
            entity_id="sg_store",
            sample_period="2026-08",
            prepared_by="independent-real-source-preparer",
            output=workpaper_path,
        )
        self.assertTrue(created["template_only"])
        self.assertFalse(created["finalization_ready"])
        self.assertEqual(oct(workpaper_path.stat().st_mode & 0o777), "0o600")
        with self.assertRaisesRegex(ConnectorShadowArtifactError, "finalization_ready"):
            finalize_connector_shadow_baseline_workpaper(
                runtime, workpaper_path, Path(self.temp.name) / "premature.json",
            )

        workpaper = json.loads(workpaper_path.read_text(encoding="utf-8"))
        workpaper["source_independence"] = {
            "prepared_from_independent_source": True,
            "pipeline_output_used_as_baseline": False,
            "source_scope_confirmed": True,
        }
        workpaper["anonymization"]["private_source_evidence_retained"] = True
        workpaper["source_expectations"][0]["expected_record_count"] = 2
        workpaper["source_expectations"][0]["evidence_references"] = [
            "private-export://airwallex/sg-store/2026-08/approved-expenses",
        ]
        expected_controls = {
            "pipeline_ready": True,
            "expense_record_count": 2,
            "receipt_missing_count": 1,
            "business_purpose_missing_count": 0,
            "uncleared_count": 0,
            "accounting_mapping_missing_count": 1,
            "state_change_candidate_count": 0,
            "network_refetch_performed": True,
            "webhook_refetch_basis": True,
            "external_actions_disabled": True,
        }
        for item in workpaper["control_expectations"]:
            item["expected_value"] = expected_controls[item["control_id"]]
        workpaper["evidence_references"] = [
            "workpaper://airwallex/sg-store/2026-08/count-tie-out",
            "review://airwallex/sg-store/2026-08/source-scope",
        ]
        workpaper["finalization_ready"] = True
        workpaper_path.write_text(json.dumps(workpaper), encoding="utf-8")
        baseline_path = Path(self.temp.name) / "airwallex-real-baseline.json"
        finalized = finalize_connector_shadow_baseline_workpaper(
            runtime, workpaper_path, baseline_path,
        )
        self.assertTrue(finalized["real_sample_evidence"])
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        self.assertEqual(baseline["schema_version"], 2)
        self.assertNotIn("instructions", baseline)
        self.assertNotIn("runtime_fingerprint", baseline)

        request = {
            "pipeline_id": "finance.expense_evidence_review",
            "payload": {
                "entity_id": "sg_store",
                "connector_request": json.loads(AIRWALLEX_FIXTURE.read_text(encoding="utf-8")),
            },
        }
        result_path = Path(self.temp.name) / "airwallex-real-result.json"
        result = dispatch_box_pipeline_request(runtime, request)
        fixture_result_path = Path(self.temp.name) / "airwallex-fixture-not-real-result.json"
        fixture_result_path.write_text(json.dumps(result), encoding="utf-8")
        fixture_assessment_path = Path(self.temp.name) / "airwallex-fixture-not-real-assessment.json"
        fixture_summary = assess_connector_shadow_artifacts(
            runtime, baseline_path, fixture_result_path, fixture_assessment_path,
        )
        self.assertFalse(fixture_summary["passed"])
        fixture_assessment = json.loads(fixture_assessment_path.read_text(encoding="utf-8"))
        fixture_controls = {
            item["control_id"]: item for item in fixture_assessment["control_results"]
        }
        self.assertFalse(fixture_controls["network_refetch_performed"]["matched"])
        self.assertFalse(fixture_controls["webhook_refetch_basis"]["matched"])
        result["network_access_performed"] = True
        result["batch"]["source"].update({
            "kind": "api",
            "name": "airwallex.expense_refetch",
            "network_access_performed": True,
            "update_capture_basis": "signed_webhook_then_read_only_refetch",
            "webhook_context_validated": True,
            "webhook_context_count": 1,
        })
        observation_path = Path(self.temp.name) / "airwallex-shadow-observation.json"
        observation_summary = write_airwallex_shadow_observation(
            runtime, result, observation_path,
        )
        self.assertEqual(oct(observation_path.stat().st_mode & 0o777), "0o600")
        self.assertEqual(observation_summary["expense_record_count"], 2)
        observation = json.loads(observation_path.read_text(encoding="utf-8"))
        serialized_observation = json.dumps(observation)
        self.assertNotIn("billing_amount_minor", serialized_observation)
        self.assertNotIn("transaction_amount_minor", serialized_observation)
        self.assertNotIn("expense_evidence_id", serialized_observation)
        self.assertFalse(observation["financial_amounts_included"])
        expected_private_result_hash = hashlib.sha256(json.dumps(
            result, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest()
        self.assertEqual(
            observation["private_pipeline_result_sha256"],
            expected_private_result_hash,
        )
        observation_assessment_path = (
            Path(self.temp.name) / "airwallex-observation-assessment.json"
        )
        observation_assessment = assess_connector_shadow_artifacts(
            runtime, baseline_path, observation_path, observation_assessment_path,
        )
        self.assertTrue(observation_assessment["passed"])
        stored_observation_assessment = json.loads(
            observation_assessment_path.read_text(encoding="utf-8")
        )
        self.assertEqual(
            stored_observation_assessment["pipeline_result_sha256"],
            expected_private_result_hash,
        )
        tampered_observation = dict(observation)
        tampered_observation["ready"] = not observation["ready"]
        tampered_path = Path(self.temp.name) / "tampered-airwallex-observation.json"
        tampered_path.write_text(json.dumps(tampered_observation), encoding="utf-8")
        with self.assertRaisesRegex(
            ConnectorShadowArtifactError, "integrity or privacy",
        ):
            assess_connector_shadow_artifacts(
                runtime, baseline_path, tampered_path,
                Path(self.temp.name) / "tampered-assessment.json",
            )
        result_path.write_text(json.dumps(result), encoding="utf-8")
        assessment_path = Path(self.temp.name) / "airwallex-real-assessment.json"
        assess_connector_shadow_artifacts(
            runtime, baseline_path, result_path, assessment_path,
        )
        with self.assertRaisesRegex(ConnectorShadowArtifactError, "not demo"):
            review_connector_shadow_artifact(
                runtime, assessment_path, Path(self.temp.name) / "demo-review-never.json",
                decision="passed", actor="independent-real-reviewer",
                rationale="attempted demo evidence on a real assessment",
                evidence_references=["demo-review://airwallex/sg-store/2026-08"],
            )
        reviewed_path = Path(self.temp.name) / "airwallex-real-reviewed.json"
        review_connector_shadow_artifact(
            runtime, assessment_path, reviewed_path, decision="passed",
            actor="independent-real-reviewer",
            rationale="真实匿名来源计数、缺口与只读边界均已独立复核",
            evidence_references=["review://airwallex/sg-store/2026-08/final"],
        )
        verified = verify_connector_shadow_artifact(runtime, reviewed_path)
        self.assertTrue(verified["real_sample_evidence"])
        self.assertEqual(verified["sample_classification"], "real_anonymized")
        self.assertRegex(verified["source_independence_sha256"], r"^[0-9a-f]{64}$")

    def test_real_baseline_refuses_demo_fixture_and_pipeline_output_references(self):
        runtime = BoxRuntime(AIRWALLEX_BOX, ROOT / "packs")
        baseline = json.loads(AIRWALLEX_BASELINE.read_text(encoding="utf-8"))
        baseline["schema_version"] = 2
        baseline["sample_classification"] = "real_anonymized"
        baseline["source_independence"] = {
            "prepared_from_independent_source": True,
            "pipeline_output_used_as_baseline": False,
            "source_scope_confirmed": True,
        }
        baseline["anonymization"] = {
            "raw_identifiers_removed_from_baseline": True,
            "financial_amounts_removed_from_baseline": True,
            "private_source_evidence_retained": True,
        }
        baseline["control_expectations"].extend([
            {"control_id": "state_change_candidate_count", "expected_value": 0},
            {"control_id": "network_refetch_performed", "expected_value": True},
            {"control_id": "webhook_refetch_basis", "expected_value": True},
        ])
        path = Path(self.temp.name) / "fake-real-baseline.json"
        path.write_text(json.dumps(baseline), encoding="utf-8")
        result_path = Path(self.temp.name) / "unused-result.json"
        result_path.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(ConnectorShadowArtifactError, "not demo"):
            assess_connector_shadow_artifacts(
                runtime, path, result_path, Path(self.temp.name) / "never.json",
            )

    def test_airwallex_expense_shadow_rejects_record_outside_baseline_period(self):
        runtime = BoxRuntime(AIRWALLEX_BOX, ROOT / "packs")
        fixture = json.loads(AIRWALLEX_FIXTURE.read_text(encoding="utf-8"))
        fixture["objects"][0]["created_at"] = "2026-07-31T23:59:59Z"
        fixture["from_created_at"] = "2026-07-01T00:00:00Z"
        result = dispatch_box_pipeline_request(runtime, {
            "pipeline_id": "finance.expense_evidence_review",
            "payload": {"entity_id": "sg_store", "connector_request": fixture},
        })
        result_path = Path(self.temp.name) / "airwallex-wrong-period.json"
        result_path.write_text(json.dumps(result), encoding="utf-8")
        assessment_path = Path(self.temp.name) / "airwallex-wrong-period-assessment.json"
        summary = assess_connector_shadow_artifacts(
            runtime, AIRWALLEX_BASELINE, result_path, assessment_path,
        )
        self.assertFalse(summary["passed"])


if __name__ == "__main__":
    unittest.main()
