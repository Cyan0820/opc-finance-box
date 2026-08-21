import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openpyxl import Workbook

from src.box_pipeline import (
    BoxPipelineError,
    dispatch_box_pipeline_request,
    run_bank_statement_close_pipeline,
    run_commerce_import_analysis_pipeline,
)
from src.box_runtime import BoxRuntime
from src.default_connectors import build_box_connector_registry


ROOT = Path(__file__).resolve().parents[1]


class BankStatementClosePipelineTests(unittest.TestCase):
    def setUp(self):
        self.runtime = BoxRuntime(ROOT / "examples" / "boxes" / "cn_dtc_store.json", ROOT / "packs")
        self.request = json.loads(
            (ROOT / "examples" / "pipelines" / "bank_statement_close_fixture.json").read_text(
                encoding="utf-8"
            )
        )["payload"]

    def test_bank_statement_runs_to_candidate_without_posting(self):
        first = run_bank_statement_close_pipeline(self.runtime, self.request)
        second = run_bank_statement_close_pipeline(self.runtime, self.request)
        self.assertTrue(first["ready"], first)
        self.assertEqual(first["pipeline"]["run_id"], second["pipeline"]["run_id"])
        self.assertEqual(first["pipeline"]["required_review_gates"], [
            "bank_statement_mapping_review", "bank_balance_reconciliation",
        ])
        self.assertEqual(first["batch"]["quality"]["record_count"], 2)
        account = first["services"]["bank_reconciliation_candidate"]["output"]["accounts"][0]
        self.assertEqual(account["receipts"], 700.0)
        self.assertEqual(account["payments"], 120.5)
        self.assertEqual(account["statement_ending_balance"], 5579.5)
        self.assertTrue(first["founder_briefing"]["candidate_only"])
        self.assertFalse(first["founder_briefing"]["posting_or_cash_allocation_performed"])
        self.assertFalse(first["external_actions_performed"])

    def test_pipeline_forces_connector_to_requested_entity(self):
        request = copy.deepcopy(self.request)
        request["connector_request"]["default_entity_id"] = "other_entity"
        with self.assertRaisesRegex(BoxPipelineError, "does not match"):
            run_bank_statement_close_pipeline(self.runtime, request)

    def test_bad_rows_stop_before_reconciliation_service(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "bad.csv"
            path.write_text(
                "交易日期,交易流水号,本方账号,交易金额,币种\n"
                "2026-08-01,BAD-1,MAIN,10,CNY\n",
                encoding="utf-8",
            )
            request = copy.deepcopy(self.request)
            request["connector_request"]["path"] = str(path)
            result = run_bank_statement_close_pipeline(self.runtime, request)
        self.assertFalse(result["ready"])
        self.assertEqual(result["blocked_at"], "quality_gate")
        self.assertFalse(result["lineage"]["service_executed"])
        self.assertEqual(result["services"], {})


class BoxPipelineTests(unittest.TestCase):
    def setUp(self):
        self.runtime = BoxRuntime(ROOT / "examples" / "boxes" / "cn_dtc_api_store.json", ROOT / "packs")
        self.request = json.loads(
            (ROOT / "examples" / "connectors" / "commerce_api_payload.json").read_text(encoding="utf-8")
        )

    def test_api_connector_runs_end_to_end_deterministic_analysis(self):
        result = run_commerce_import_analysis_pipeline(
            self.runtime,
            "example.commerce_api_payload",
            self.request,
        )
        self.assertTrue(result["ready"])
        self.assertIsNone(result["blocked_at"])
        self.assertTrue(result["lineage"]["service_executed"])
        self.assertEqual(result["lineage"]["entity_ids"], ["cn_dtc_company"])
        analysis = result["analysis"]["output"]
        self.assertEqual(analysis["reconciliations"][0]["reported_payout"], 92)
        self.assertEqual(analysis["reconciliations"][0]["calculated_payout"], 92)

    def test_generic_dispatch_contract_runs_commerce_pipeline(self):
        result = dispatch_box_pipeline_request(self.runtime, {
            "pipeline_id": "commerce.import_analyze",
            "payload": {
                "connector_id": "example.commerce_api_payload",
                "connector_request": self.request,
            },
        })
        self.assertTrue(result["ready"], result)
        self.assertEqual(result["pipeline"]["pipeline_id"], "commerce.import_analyze")
        self.assertEqual(result["analysis"]["service"]["action_class"], "read")

    def test_quality_failure_stops_before_financial_service(self):
        request = copy.deepcopy(self.request)
        request["payload"]["orders"].append(copy.deepcopy(request["payload"]["orders"][0]))
        result = run_commerce_import_analysis_pipeline(
            self.runtime,
            "example.commerce_api_payload",
            request,
        )
        self.assertFalse(result["ready"])
        self.assertEqual(result["blocked_at"], "quality_gate")
        self.assertFalse(result["lineage"]["service_executed"])
        self.assertIsNone(result["analysis"])

    def test_reconciliation_difference_is_visible_after_service_execution(self):
        request = copy.deepcopy(self.request)
        request["payload"]["payouts"][0]["payout"] = 91
        result = run_commerce_import_analysis_pipeline(
            self.runtime,
            "example.commerce_api_payload",
            request,
        )
        self.assertFalse(result["ready"])
        self.assertEqual(result["blocked_at"], "deterministic_analysis")
        self.assertTrue(result["lineage"]["service_executed"])
        self.assertTrue(result["blockers"])


class GameChannelSettlementClosePipelineTests(unittest.TestCase):
    def setUp(self):
        self.runtime = BoxRuntime(
            ROOT / "examples" / "boxes" / "global_game_studio.json", ROOT / "packs",
        )

    @staticmethod
    def _workbook(path: Path, rows: list[list[object]]) -> None:
        book = Workbook()
        sheet = book.active
        sheet.title = "商店金流账单-App Store"
        sheet.append([
            "账期月份", "游戏名称", "平台", "渠道", "总流水", "退款流水",
            "结算金额", "预提所得税（结算币种）", "甲方实收金额（结算币种）", "结算币种",
        ])
        for row in rows:
            sheet.append(row)
        book.save(path)

    @staticmethod
    def _mapping(*, basis: float = 1000, rate: float = 0.7) -> dict:
        return {
            "entity_id": "sg_publisher",
            "period": "2026-07",
            "game": "G1",
            "channel": "App Store",
            "currency": "USD",
            "contract_basis": basis,
            "contract_rate": rate,
            "contract_adjustments": 0,
            "evidence": {
                "source_reference": "contract://app-store/G1/2026",
                "captured_at": "2026-08-13",
            },
        }

    def _request(self, path: Path, mappings: list[dict]) -> dict:
        return {
            "pipeline_id": "game.channel_settlement_close",
            "payload": {
                "entity_id": "sg_publisher",
                "connector_id": "file.app_store_settlements",
                "connector_request": {"path": str(path)},
                "contract_mappings": mappings,
                "tolerance": 0.01,
            },
        }

    def test_explicit_contract_evidence_runs_complete_candidate_pipeline(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "App Store结算.xlsx"
            self._workbook(path, [[
                "2026-07", "G1", "iOS", "App Store", 1000, 0, 700, 70, 630, "USD",
            ]])
            request = self._request(path, [self._mapping()])
            first = dispatch_box_pipeline_request(self.runtime, request)
            second = dispatch_box_pipeline_request(self.runtime, request)
        self.assertTrue(first["ready"], first)
        self.assertEqual(first["pipeline"]["run_id"], second["pipeline"]["run_id"])
        self.assertEqual(first["pipeline"]["required_review_gates"], [
            "channel_contract_mapping", "game_principal_agent_assessment",
        ])
        output = first["services"]["settlement_reconciliation"]["output"]
        self.assertEqual(output["reconciliations"][0]["expected_settlement"], 700)
        self.assertIn("contract_mapping", output["reconciliations"][0]["evidence"])
        self.assertTrue(first["founder_briefing"]["candidate_only"])
        self.assertTrue(first["founder_briefing"]["cross_currency_total_prohibited"])
        self.assertFalse(first["external_actions_performed"])
        self.assertFalse(first["network_access_performed"])

    def test_every_imported_row_requires_exactly_one_mapping(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "App Store结算.xlsx"
            self._workbook(path, [
                ["2026-07", "G1", "iOS", "App Store", 1000, 0, 700, 70, 630, "USD"],
                ["2026-07", "G2", "iOS", "App Store", 500, 0, 350, 0, 350, "USD"],
            ])
            result = dispatch_box_pipeline_request(
                self.runtime, self._request(path, [self._mapping()]),
            )
        self.assertFalse(result["ready"])
        self.assertEqual(result["blocked_at"], "contract_mapping")
        self.assertFalse(result["lineage"]["service_executed"])
        self.assertIn("do not cover every imported settlement", result["blockers"][0])

    def test_ambiguous_business_key_requires_settlement_id(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "App Store结算.xlsx"
            row = ["2026-07", "G1", "iOS", "App Store", 1000, 0, 700, 70, 630, "USD"]
            self._workbook(path, [row, row])
            result = dispatch_box_pipeline_request(
                self.runtime, self._request(path, [self._mapping(), self._mapping()]),
            )
            imported = build_box_connector_registry(self.runtime).dispatch(
                self.runtime, "file.app_store_settlements",
                {"path": str(path), "default_entity_id": "sg_publisher"},
            )
            mappings = []
            for settlement in imported["batch"]["datasets"]["game.settlements"]:
                mapping = self._mapping()
                mapping["settlement_id"] = settlement["id"]
                mappings.append(mapping)
            explicit = dispatch_box_pipeline_request(
                self.runtime, self._request(path, list(reversed(mappings))),
            )
            explicit_reordered = dispatch_box_pipeline_request(
                self.runtime, self._request(path, mappings),
            )
        self.assertFalse(result["ready"])
        self.assertEqual(result["blocked_at"], "contract_mapping")
        self.assertIn("settlement_id", result["blockers"][0])
        self.assertTrue(explicit["ready"], explicit)
        self.assertEqual(explicit["pipeline"]["run_id"], explicit_reordered["pipeline"]["run_id"])
        self.assertEqual(
            explicit["services"]["settlement_reconciliation"]["output"],
            explicit_reordered["services"]["settlement_reconciliation"]["output"],
        )

    def test_cross_entity_or_inferred_rate_is_rejected_before_service(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "App Store结算.xlsx"
            self._workbook(path, [[
                "2026-07", "G1", "iOS", "App Store", 1000, 0, 700, 70, 630, "USD",
            ]])
            wrong_entity = self._mapping()
            wrong_entity["entity_id"] = "cn_studio"
            entity_result = dispatch_box_pipeline_request(
                self.runtime, self._request(path, [wrong_entity]),
            )
            invalid_rate = dispatch_box_pipeline_request(
                self.runtime, self._request(path, [self._mapping(rate=1.2)]),
            )
        self.assertEqual(entity_result["blocked_at"], "contract_mapping")
        self.assertIn("does not match", entity_result["blockers"][0])
        self.assertEqual(invalid_rate["blocked_at"], "contract_mapping")
        self.assertIn("at most 1", invalid_rate["blockers"][0])


class CommerceChannelClosePipelineTests(unittest.TestCase):
    def setUp(self):
        self.runtime = BoxRuntime(
            ROOT / "examples" / "boxes" / "cn_dtc_api_store.json", ROOT / "packs",
        )
        self.request = json.loads(
            (ROOT / "examples" / "pipelines" / "commerce_channel_close_fixture.json").read_text(
                encoding="utf-8"
            )
        )

    def test_generic_commerce_close_runs_return_aware_deterministic_review_outputs(self):
        first = dispatch_box_pipeline_request(self.runtime, self.request)
        second = dispatch_box_pipeline_request(self.runtime, self.request)
        self.assertTrue(first["ready"], first)
        self.assertEqual(first["pipeline"]["run_id"], second["pipeline"]["run_id"])
        self.assertEqual(first["pipeline"]["required_review_gates"], [
            "commerce_source_mapping", "revenue_cutoff", "inventory_valuation_policy",
            "return_disposition_review", "import_landed_cost_policy", "sales_tax_nexus_review",
        ])
        self.assertEqual(set(first["services"]), {
            "order_settlement_reconciliation", "refund_summary",
            "return_inventory_reconciliation", "import_landed_cost_candidates",
            "fulfillment_cost_summary",
            "destination_evidence",
        })
        reconciliation = first["services"]["order_settlement_reconciliation"]["output"]
        self.assertEqual(reconciliation["reconciliations"][0]["status"], "已核对")
        self.assertEqual(first["founder_briefing"]["destination_tax_evidence"][0]["destination_country"], "US")
        self.assertEqual(first["founder_briefing"]["returns_by_authorization"][0]["status"], "reconciled")
        self.assertEqual(first["founder_briefing"]["restock_candidates"][0]["warehouse"], "US-3PL-WH-1")
        self.assertEqual(
            first["founder_briefing"]["import_landed_cost_candidates"][0]["inventory_landed_cost_candidate"],
            135,
        )
        self.assertTrue(first["founder_briefing"]["customs_or_import_tax_conclusion_prohibited"])
        self.assertTrue(first["founder_briefing"]["revenue_claim_prohibited"])
        self.assertTrue(first["founder_briefing"]["tax_due_claim_prohibited"])
        self.assertTrue(first["founder_briefing"]["margin_requires_inventory_policy_review"])
        self.assertFalse(first["external_actions_performed"])

    def test_reconciliation_difference_blocks_at_first_finance_stage(self):
        request = copy.deepcopy(self.request)
        request["payload"]["connector_request"]["payload"]["payouts"][0]["payout"] = 91
        result = dispatch_box_pipeline_request(self.runtime, request)
        self.assertFalse(result["ready"])
        self.assertEqual(result["blocked_at"], "order_settlement_reconciliation")
        self.assertTrue(result["founder_briefing"]["risk_signals"])
        self.assertFalse(result["external_actions_performed"])

    def test_multi_entity_api_batch_is_rejected_before_finance_service(self):
        request = copy.deepcopy(self.request)
        foreign = copy.deepcopy(request["payload"]["connector_request"]["payload"]["orders"][0])
        foreign["id"] = "gid://fictional/Order/foreign"
        foreign["orderNumber"] = "API-FOREIGN"
        foreign["entityId"] = "unknown_entity"
        request["payload"]["connector_request"]["payload"]["orders"].append(foreign)
        result = dispatch_box_pipeline_request(self.runtime, request)
        self.assertFalse(result["ready"])
        self.assertEqual(result["blocked_at"], "quality_gate")
        self.assertEqual(result["services"], {})

    def test_connector_entity_override_is_rejected(self):
        request = copy.deepcopy(self.request)
        request["payload"]["connector_request"]["default_entity_id"] = "another_entity"
        with self.assertRaisesRegex(BoxPipelineError, "does not match"):
            dispatch_box_pipeline_request(self.runtime, request)

    def test_refunded_return_without_receipt_blocks_before_cost_and_tax_stages(self):
        request = copy.deepcopy(self.request)
        request["payload"]["connector_request"]["payload"]["returnReceipts"] = []
        result = dispatch_box_pipeline_request(self.runtime, request)
        self.assertFalse(result["ready"])
        self.assertEqual(result["blocked_at"], "return_inventory_reconciliation")
        output = result["services"]["return_inventory_reconciliation"]["output"]
        self.assertEqual(output["reconciliations"][0]["status"], "refunded_without_receipt")
        self.assertFalse(output["inventory_adjustment_performed"])


class MarketplaceChannelClosePipelineTests(unittest.TestCase):
    def setUp(self):
        self.runtime = BoxRuntime(
            ROOT / "examples" / "boxes" / "cn_marketplace_store.json", ROOT / "packs",
        )
        self.request = json.loads(
            (ROOT / "examples" / "pipelines" / "marketplace_channel_close_fixture.json").read_text(
                encoding="utf-8"
            )
        )

    def test_marketplace_close_reconciles_distinct_fee_receivable_and_inventory_outputs(self):
        first = dispatch_box_pipeline_request(self.runtime, self.request)
        second = dispatch_box_pipeline_request(self.runtime, self.request)
        self.assertTrue(first["ready"], first)
        self.assertEqual(first["pipeline"]["run_id"], second["pipeline"]["run_id"])
        self.assertEqual(first["pipeline"]["required_review_gates"], [
            "commerce_source_mapping", "marketplace_contract_mapping",
            "marketplace_inventory_mapping", "revenue_cutoff", "inventory_valuation_policy",
            "return_disposition_review", "import_landed_cost_policy",
        ])
        self.assertEqual(set(first["services"]), {
            "marketplace_fee_reconciliation", "marketplace_receivable_reconciliation",
            "return_inventory_reconciliation", "import_landed_cost_candidates",
            "marketplace_inventory_reconciliation",
        })
        self.assertEqual(first["founder_briefing"]["inventory_by_sku_warehouse"][0]["status"], "reconciled")
        self.assertEqual(
            {row["warehouse"] for row in first["founder_briefing"]["return_receipts_by_warehouse_disposition"]},
            {"PLATFORM-WH-1", "PLATFORM-WH-2"},
        )
        self.assertEqual(
            first["founder_briefing"]["import_landed_cost_candidates"][0]["unit_landed_cost_candidate"],
            13.5,
        )
        self.assertTrue(first["founder_briefing"]["inventory_adjustment_prohibited"])
        self.assertFalse(first["external_actions_performed"])

    def test_inventory_difference_blocks_without_adjustment(self):
        request = copy.deepcopy(self.request)
        request["payload"]["ledger_inventory"][0]["quantity"] = 9
        result = dispatch_box_pipeline_request(self.runtime, request)
        self.assertFalse(result["ready"])
        self.assertEqual(result["blocked_at"], "marketplace_inventory_reconciliation")
        inventory = result["services"]["marketplace_inventory_reconciliation"]["output"]
        self.assertEqual(inventory["rows"][0]["difference"], 1)
        self.assertFalse(inventory["posting_or_inventory_adjustment_performed"])

    def test_financial_difference_is_earliest_blocker(self):
        request = copy.deepcopy(self.request)
        request["payload"]["connector_request"]["payload"]["settlements"][0]["payout"] = 89
        result = dispatch_box_pipeline_request(self.runtime, request)
        self.assertFalse(result["ready"])
        self.assertEqual(result["blocked_at"], "marketplace_fee_reconciliation")
        self.assertTrue(result["founder_briefing"]["risk_signals"]["financial"])

    def test_cross_entity_inventory_is_rejected_before_services(self):
        request = copy.deepcopy(self.request)
        request["payload"]["ledger_inventory"][0]["entity_id"] = "another_entity"
        result = dispatch_box_pipeline_request(self.runtime, request)
        self.assertFalse(result["ready"])
        self.assertEqual(result["blocked_at"], "entity_scope")
        self.assertEqual(result["services"], {})


class StripeDailyClosePipelineTests(unittest.TestCase):
    def setUp(self):
        self.runtime = BoxRuntime(
            ROOT / "examples" / "boxes" / "cn_dtc_stripe_store.json", ROOT / "packs",
        )
        self.request = json.loads(
            (ROOT / "examples" / "pipelines" / "stripe_daily_close_fixture.json").read_text(
                encoding="utf-8"
            )
        )

    def test_pipeline_runs_connectors_quality_services_and_briefing(self):
        result = dispatch_box_pipeline_request(self.runtime, self.request)
        self.assertTrue(result["ready"], result)
        self.assertIsNone(result["blocked_at"])
        self.assertEqual(result["pipeline"]["pipeline_id"], "stripe.daily_close")
        self.assertEqual(result["lineage"]["service_ids"], [
            "stripe.summarize_balance_activity", "stripe.reconcile_payouts",
        ])
        reconciliation = result["services"]["payout_bank_reconciliation"]["output"]
        self.assertEqual(
            reconciliation["reconciliation"][0]["reconciliation_status"],
            "high_confidence_candidate",
        )
        self.assertTrue(result["founder_briefing"]["candidate_only"])
        self.assertFalse(result["external_actions_performed"])
        self.assertFalse(result["network_access_performed"])

    def test_identical_offline_rerun_has_same_run_id_and_financial_outputs(self):
        first = dispatch_box_pipeline_request(self.runtime, self.request)
        second = dispatch_box_pipeline_request(self.runtime, self.request)
        self.assertEqual(first["pipeline"]["run_id"], second["pipeline"]["run_id"])
        self.assertEqual(first["connector_batches"], second["connector_batches"])
        self.assertEqual(first["founder_briefing"], second["founder_briefing"])
        self.assertEqual(first["lineage"], second["lineage"])

    def test_connector_quality_failure_stops_both_finance_services(self):
        request = copy.deepcopy(self.request)
        objects = request["payload"]["balance_request"]["objects"]
        objects.append(copy.deepcopy(objects[0]))
        result = dispatch_box_pipeline_request(self.runtime, request)
        self.assertFalse(result["ready"])
        self.assertEqual(result["blocked_at"], "quality_gate")
        self.assertEqual(result["services"], {})
        self.assertTrue(any("stripe.balance_transactions" in item for item in result["blockers"]))

    def test_second_connector_failure_preserves_first_batch_lineage_without_secret(self):
        request = copy.deepcopy(self.request)
        request["payload"]["payout_request"] = {"mode": "fetch"}
        with patch.dict(os.environ, {}, clear=True):
            result = dispatch_box_pipeline_request(self.runtime, request)
        self.assertFalse(result["ready"])
        self.assertEqual(result["blocked_at"], "stripe_payout_connector")
        self.assertTrue(result["retryable"])
        self.assertIn("stripe.balance_transactions", result["connector_batches"])
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("OPC_STRIPE_RESTRICTED_KEY", serialized)
        self.assertNotIn("Authorization", serialized)

    def test_missing_bank_receipt_is_finance_blocker_not_connector_failure(self):
        request = copy.deepcopy(self.request)
        request["payload"]["bank_transactions"] = []
        result = dispatch_box_pipeline_request(self.runtime, request)
        self.assertFalse(result["ready"])
        self.assertEqual(result["blocked_at"], "payout_bank_reconciliation")
        self.assertFalse(result["retryable"])
        risks = result["founder_briefing"]["payout_reconciliation"]["risk_signals"]
        self.assertEqual(risks[0]["code"], "bank_receipt_missing")
        self.assertIn("rerun the full request", result["resume_contract"])

    def test_pipeline_rejects_entity_override_and_unknown_pipeline(self):
        request = copy.deepcopy(self.request)
        request["payload"]["balance_request"]["default_entity_id"] = "another_entity"
        with self.assertRaisesRegex(BoxPipelineError, "does not match"):
            dispatch_box_pipeline_request(self.runtime, request)
        with self.assertRaisesRegex(BoxPipelineError, "Unknown pipeline"):
            dispatch_box_pipeline_request(
                self.runtime, {"pipeline_id": "unknown.pipeline", "payload": {}},
            )


class ShopifyStripeDailyClosePipelineTests(unittest.TestCase):
    def setUp(self):
        self.runtime = BoxRuntime(
            ROOT / "examples" / "boxes" / "cn_dtc_shopify_stripe_store.json", ROOT / "packs",
        )
        self.request = json.loads(
            (ROOT / "examples" / "pipelines" / "shopify_stripe_daily_close_fixture.json").read_text(
                encoding="utf-8"
            )
        )

    def test_complete_evidence_chain_is_ready_but_candidate_only(self):
        result = dispatch_box_pipeline_request(self.runtime, self.request)
        self.assertTrue(result["ready"], result)
        self.assertIsNone(result["blocked_at"])
        self.assertEqual(result["pipeline"]["pipeline_id"], "dtc.shopify_stripe_daily_close")
        self.assertEqual(set(result["lineage"]["service_ids"]), {
            "shopify.summarize_order_activity",
            "stripe.summarize_balance_activity",
            "dtc.reconcile_shopify_stripe_activity",
            "stripe.reconcile_payouts",
        })
        processor = result["services"]["shopify_stripe_activity_reconciliation"]["output"]
        self.assertEqual(processor["currency_summary"][0]["stripe_net_minor"], 8027)
        payout = result["services"]["stripe_payout_bank_reconciliation"]["output"]
        self.assertEqual(payout["reconciliation"][0]["reconciliation_status"], "high_confidence_candidate")
        self.assertTrue(result["founder_briefing"]["candidate_only"])
        self.assertTrue(result["founder_briefing"]["margin_claim_prohibited"])
        self.assertTrue(result["founder_briefing"]["revenue_claim_prohibited"])
        self.assertFalse(result["external_actions_performed"])
        self.assertFalse(result["network_access_performed"])

    def test_identical_evidence_reproduces_run_id_and_lineage(self):
        first = dispatch_box_pipeline_request(self.runtime, self.request)
        second = dispatch_box_pipeline_request(self.runtime, self.request)
        self.assertEqual(first["pipeline"]["run_id"], second["pipeline"]["run_id"])
        self.assertEqual(first["connector_batches"], second["connector_batches"])
        self.assertEqual(first["founder_briefing"], second["founder_briefing"])
        self.assertEqual(first["lineage"], second["lineage"])
        reordered = copy.deepcopy(self.request)
        reordered["payload"]["processor_links"].reverse()
        third = dispatch_box_pipeline_request(self.runtime, reordered)
        self.assertEqual(first["pipeline"]["run_id"], third["pipeline"]["run_id"])

    def test_shopify_quality_failure_stops_all_finance_services(self):
        request = copy.deepcopy(self.request)
        orders = request["payload"]["shopify_request"]["objects"]
        orders.append(copy.deepcopy(orders[0]))
        result = dispatch_box_pipeline_request(self.runtime, request)
        self.assertFalse(result["ready"])
        self.assertEqual(result["blocked_at"], "quality_gate")
        self.assertEqual(result["services"], {})
        self.assertTrue(any("shopify.orders" in item for item in result["blockers"]))

    def test_missing_explicit_processor_link_is_earliest_finance_blocker(self):
        request = copy.deepcopy(self.request)
        request["payload"]["processor_links"] = request["payload"]["processor_links"][:1]
        result = dispatch_box_pipeline_request(self.runtime, request)
        self.assertFalse(result["ready"])
        self.assertEqual(result["blocked_at"], "shopify_stripe_activity_reconciliation")
        activity = result["services"]["shopify_stripe_activity_reconciliation"]["output"]
        self.assertEqual(activity["reconciliation"][1]["status"], "missing_processor_link")
        self.assertFalse(result["external_actions_performed"])

    def test_later_connector_failure_preserves_prior_lineage_and_sanitizes_secrets(self):
        request = copy.deepcopy(self.request)
        request["payload"]["stripe_payout_request"] = {"mode": "fetch"}
        with patch.dict(os.environ, {}, clear=True):
            result = dispatch_box_pipeline_request(self.runtime, request)
        self.assertFalse(result["ready"])
        self.assertEqual(result["blocked_at"], "stripe_payout_connector")
        self.assertEqual(set(result["connector_batches"]), {
            "shopify.orders", "stripe.balance_transactions",
        })
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("OPC_STRIPE_RESTRICTED_KEY", serialized)
        self.assertNotIn("OPC_SHOPIFY_ADMIN_TOKEN", serialized)
        self.assertNotIn("Authorization", serialized)

    def test_pipeline_requires_integration_pack(self):
        runtime = BoxRuntime(
            ROOT / "examples" / "boxes" / "cn_dtc_stripe_store.json", ROOT / "packs",
        )
        with self.assertRaisesRegex(BoxPipelineError, "integration.shopify_stripe_order_to_cash"):
            dispatch_box_pipeline_request(runtime, self.request)


if __name__ == "__main__":
    unittest.main()
