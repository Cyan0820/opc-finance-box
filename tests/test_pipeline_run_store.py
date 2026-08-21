from __future__ import annotations

import copy
import json
import tempfile
import unittest
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from openpyxl import Workbook

from src.box_pipeline import dispatch_box_pipeline_request
from src.box_runtime import BoxRuntime
from src.pipeline_run_store import PipelineRunStore, PipelineRunStoreError


ROOT = Path(__file__).resolve().parents[1]


class PipelineRunStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = PipelineRunStore(Path(self.temp.name) / "runs")
        self.runtime = BoxRuntime(
            ROOT / "examples" / "boxes" / "cn_dtc_shopify_stripe_store.json", ROOT / "packs",
        )
        self.request = json.loads(
            (ROOT / "examples" / "pipelines" / "shopify_stripe_daily_close_fixture.json").read_text()
        )
        self.result = dispatch_box_pipeline_request(self.runtime, self.request)

    def tearDown(self):
        self.temp.cleanup()

    def _record(self, request=None, result=None, actor="测试负责人"):
        return self.store.record(
            self.runtime.snapshot(), request or self.request, result or self.result, actor=actor,
        )

    def test_ready_run_persists_control_summary_without_raw_request_or_result(self):
        request = copy.deepcopy(self.request)
        request["payload"]["operator_note"] = "DO_NOT_PERSIST_RAW_REQUEST_MARKER"
        record = self._record(request=request)
        self.assertEqual(record["status"], "ready")
        self.assertEqual(record["pipeline_id"], "dtc.shopify_stripe_daily_close")
        self.assertEqual(record["entity_id"], "cn_dtc_company")
        self.assertEqual(record["run_id"], self.result["pipeline"]["run_id"])
        self.assertEqual(len(record["connector_batches"]), 3)
        self.assertEqual(len(record["service_stages"]), 4)
        self.assertTrue(record["candidate_only"])
        self.assertEqual(record["review_status"], "pending_review")
        self.assertFalse(record["review_complete"])
        self.assertFalse(record["release_candidate"])
        self.assertEqual(record["required_review_gates"], [
            "shopify_mapping_approval",
            "processor_link_mapping_approval",
            "stripe_mapping_approval",
        ])
        self.assertFalse(record["external_actions_performed"])
        self.assertFalse(record["posting_performed"])
        self.assertFalse(record["full_request_persisted"])
        serialized = self.store.events_file.read_text(encoding="utf-8")
        self.assertNotIn("DO_NOT_PERSIST_RAW_REQUEST_MARKER", serialized)
        self.assertNotIn("Demo Shopify payment", serialized)
        self.assertNotIn("processor-links-demo.csv", serialized)

    def test_month_close_record_persists_only_canonical_period_control(self):
        request = json.loads(
            (ROOT / "examples" / "pipelines" / "month_close_control_fixture.json")
            .read_text(encoding="utf-8")
        )
        result = dispatch_box_pipeline_request(self.runtime, request)
        record = self._record(request=request, result=result)
        self.assertEqual(record["period"], "2026-08")
        self.assertEqual(record["lineage"]["period"], "2026-08")
        metric_summary = record["cfo_metric_operand_assembly"]
        self.assertEqual(metric_summary["source_id"], "finance.month_close_control")
        self.assertEqual(metric_summary["assembly_count"], 1)
        self.assertFalse(metric_summary["operand_values_persisted"])
        self.assertFalse(metric_summary["evaluation_values_persisted"])
        self.assertNotIn(
            "unresolved_authoritative_close_blockers",
            self.store.events_file.read_text(encoding="utf-8"),
        )
        self.assertIsNone(self._record()["period"])
        mismatched = copy.deepcopy(request)
        mismatched["payload"]["period"] = "2026-07"
        with self.assertRaisesRegex(PipelineRunStoreError, "entity-period lineage"):
            self._record(request=mismatched, result=result)

    def test_review_decisions_are_append_only_and_all_required_gates_must_be_approved(self):
        record = self._record()
        attempt_id = record["attempt_id"]
        runtime_fingerprint = self.runtime.snapshot()["fingerprint"]
        first = self.store.review(
            attempt_id, runtime_fingerprint=runtime_fingerprint,
            gate="shopify_mapping_approval", decision="needs_more_evidence",
            actor="复核人甲", rationale="订单映射需要补充抽样证据",
            evidence_references=["evidence://close/2026-07/orders-v1"],
        )
        self.assertEqual(first["review_status"], "needs_more_evidence")
        self.assertFalse(first["review_complete"])
        second = self.store.review(
            attempt_id, runtime_fingerprint=runtime_fingerprint,
            gate="shopify_mapping_approval", decision="approved",
            actor="复核人乙", rationale="抽样证据完整并已复核",
            evidence_references=["evidence://close/2026-07/orders-v2"],
        )
        self.assertEqual(len(second["review_history"]), 2)
        self.assertEqual(second["current_reviews"]["shopify_mapping_approval"]["decision"], "approved")
        self.assertFalse(second["release_candidate"])
        for gate in ("processor_link_mapping_approval", "stripe_mapping_approval"):
            second = self.store.review(
                attempt_id, runtime_fingerprint=runtime_fingerprint,
                gate=gate, decision="approved", actor="复核人乙",
                rationale=f"{gate} 证据完整",
            )
        self.assertTrue(second["review_complete"])
        self.assertEqual(second["review_status"], "approved")
        self.assertTrue(second["release_candidate"])
        self.assertFalse(second["release_candidate_is_external_authorization"])
        self.assertTrue(all(not item["financial_state_changed"] for item in second["review_history"]))
        self.assertTrue(all(not item["external_action_performed"] for item in second["review_history"]))

    def test_rejected_review_and_blocked_run_never_become_release_candidate(self):
        request = copy.deepcopy(self.request)
        request["payload"]["processor_links"] = request["payload"]["processor_links"][:1]
        result = dispatch_box_pipeline_request(self.runtime, request)
        record = self._record(request=request, result=result)
        fingerprint = self.runtime.snapshot()["fingerprint"]
        rejected = self.store.review(
            record["attempt_id"], runtime_fingerprint=fingerprint,
            gate="shopify_mapping_approval", decision="rejected",
            actor="复核人", rationale="关键映射不成立",
        )
        self.assertEqual(rejected["review_status"], "rejected")
        for gate in record["required_review_gates"]:
            rejected = self.store.review(
                record["attempt_id"], runtime_fingerprint=fingerprint,
                gate=gate, decision="approved", actor="复核人",
                rationale="后续证据已补齐",
            )
        self.assertTrue(rejected["review_complete"])
        self.assertFalse(rejected["release_candidate"])

    def test_review_validation_and_box_scope_fail_without_appending(self):
        record = self._record()
        before = len(self.store.events_file.read_text(encoding="utf-8").splitlines())
        kwargs = {
            "runtime_fingerprint": self.runtime.snapshot()["fingerprint"],
            "decision": "approved", "actor": "复核人", "rationale": "证据充分",
        }
        with self.assertRaisesRegex(PipelineRunStoreError, "not required"):
            self.store.review(record["attempt_id"], gate="unknown_gate", **kwargs)
        with self.assertRaisesRegex(PipelineRunStoreError, "decision"):
            self.store.review(
                record["attempt_id"], gate="shopify_mapping_approval",
                **{**kwargs, "decision": "auto_approved"},
            )
        with self.assertRaisesRegex(PipelineRunStoreError, "not found"):
            self.store.review(
                record["attempt_id"], gate="shopify_mapping_approval",
                **{**kwargs, "runtime_fingerprint": "another-box"},
            )
        after = len(self.store.events_file.read_text(encoding="utf-8").splitlines())
        self.assertEqual(after, before)

    def test_review_queue_projects_only_unresolved_latest_gate_states(self):
        record = self._record()
        fingerprint = self.runtime.snapshot()["fingerprint"]
        queue = self.store.review_queue(runtime_fingerprint=fingerprint)
        self.assertEqual(len(queue), 3)
        self.assertEqual({item["current_decision"] for item in queue}, {"pending"})
        self.store.review(
            record["attempt_id"], runtime_fingerprint=fingerprint,
            gate="shopify_mapping_approval", decision="approved",
            actor="复核人", rationale="证据完整",
        )
        self.store.review(
            record["attempt_id"], runtime_fingerprint=fingerprint,
            gate="processor_link_mapping_approval", decision="rejected",
            actor="复核人", rationale="链接证据冲突",
        )
        queue = self.store.review_queue(
            runtime_fingerprint=fingerprint,
            pipeline_id="dtc.shopify_stripe_daily_close", entity_id="cn_dtc_company",
        )
        self.assertEqual({item["gate"] for item in queue}, {
            "processor_link_mapping_approval", "stripe_mapping_approval",
        })
        rejected = next(item for item in queue if item["gate"] == "processor_link_mapping_approval")
        self.assertEqual(rejected["current_decision"], "rejected")
        self.assertFalse(rejected["external_action_performed"])
        self.assertEqual(self.store.review_queue(
            runtime_fingerprint="other-box",
        ), [])

    def test_verify_reports_scoped_counts_and_fails_closed_after_tamper(self):
        record = self._record()
        self.store.review(
            record["attempt_id"], runtime_fingerprint=self.runtime.snapshot()["fingerprint"],
            gate="shopify_mapping_approval", decision="approved",
            actor="复核人", rationale="证据完整",
        )
        integrity = self.store.verify(runtime_fingerprint=self.runtime.snapshot()["fingerprint"])
        self.assertTrue(integrity["valid"])
        self.assertEqual(integrity["attempt_count_for_box"], 1)
        self.assertEqual(integrity["review_event_count_for_box"], 1)
        self.assertRegex(integrity["chain_head"], r"^[0-9a-f]{64}$")
        self.assertFalse(integrity["external_action_performed"])
        lines = self.store.events_file.read_text(encoding="utf-8").splitlines()
        event = json.loads(lines[-1])
        event["review"]["decision"] = "rejected"
        lines[-1] = json.dumps(event)
        self.store.events_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(PipelineRunStoreError, "hash mismatch"):
            self.store.verify(runtime_fingerprint=self.runtime.snapshot()["fingerprint"])

    def test_backup_verify_and_empty_target_restore_preserve_attempts_and_reviews(self):
        record = self._record()
        fingerprint = self.runtime.snapshot()["fingerprint"]
        self.store.review(
            record["attempt_id"], runtime_fingerprint=fingerprint,
            gate="shopify_mapping_approval", decision="approved",
            actor="复核人", rationale="证据完整",
        )
        backup_path = Path(self.temp.name) / "backup-2026-08-13"
        backup = self.store.backup(backup_path, actor="备份操作人")
        self.assertTrue(backup["valid"])
        self.assertEqual(backup["event_count"], 2)
        self.assertEqual(stat.S_IMODE(backup_path.stat().st_mode), 0o700)
        self.assertEqual(
            stat.S_IMODE((backup_path / "pipeline_runs.jsonl").stat().st_mode), 0o600,
        )
        verified = PipelineRunStore.verify_backup(backup_path)
        self.assertEqual(verified["chain_head"], backup["chain_head"])
        restored_store = PipelineRunStore(Path(self.temp.name) / "restored")
        receipt = restored_store.restore_from_backup(backup_path, actor="恢复操作人")
        self.assertTrue(receipt["restored"])
        restored = restored_store.get(record["attempt_id"], runtime_fingerprint=fingerprint)
        self.assertEqual(len(restored["review_history"]), 1)
        self.assertEqual(
            restored["current_reviews"]["shopify_mapping_approval"]["decision"], "approved",
        )
        self.assertTrue((restored_store.root / "pipeline_runs_restore_receipt.json").exists())

    def test_backup_and_restore_never_overwrite_and_tamper_fails_closed(self):
        self._record()
        backup_path = Path(self.temp.name) / "backup"
        self.store.backup(backup_path, actor="备份操作人")
        with self.assertRaisesRegex(PipelineRunStoreError, "already exists"):
            self.store.backup(backup_path, actor="备份操作人")
        restored_store = PipelineRunStore(Path(self.temp.name) / "restored")
        restored_store.restore_from_backup(backup_path, actor="恢复操作人")
        with self.assertRaisesRegex(PipelineRunStoreError, "never overwrites"):
            restored_store.restore_from_backup(backup_path, actor="恢复操作人")
        ledger = backup_path / "pipeline_runs.jsonl"
        ledger.write_bytes(ledger.read_bytes() + b" ")
        with self.assertRaisesRegex(PipelineRunStoreError, "fingerprint mismatch"):
            PipelineRunStore.verify_backup(backup_path)

    def test_ledger_permissions_are_private(self):
        self._record()
        self.assertEqual(stat.S_IMODE(self.store.root.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.store.events_file.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.store.lock_file.stat().st_mode), 0o600)

    def test_identical_rerun_appends_attempt_and_links_duplicate(self):
        first = self._record()
        second = self._record()
        self.assertNotEqual(first["attempt_id"], second["attempt_id"])
        self.assertEqual(first["idempotency_key"], second["idempotency_key"])
        self.assertEqual(first["attempt_number_for_idempotency_key"], 1)
        self.assertEqual(second["attempt_number_for_idempotency_key"], 2)
        self.assertEqual(second["duplicate_of_attempt_id"], first["attempt_id"])
        records = self.store.list(runtime_fingerprint=self.runtime.snapshot()["fingerprint"])
        self.assertEqual([item["attempt_id"] for item in records], [second["attempt_id"], first["attempt_id"]])

    def test_blocked_finance_result_is_recorded_as_blocked_not_as_success(self):
        request = copy.deepcopy(self.request)
        request["payload"]["processor_links"] = request["payload"]["processor_links"][:1]
        result = dispatch_box_pipeline_request(self.runtime, request)
        record = self._record(request=request, result=result)
        self.assertFalse(record["ready"])
        self.assertEqual(record["status"], "blocked")
        self.assertEqual(record["blocked_at"], "shopify_stripe_activity_reconciliation")
        self.assertFalse(record["retryable"])
        self.assertTrue(any(not item["ready"] for item in record["service_stages"]))

    def test_runtime_fingerprint_filters_other_box_and_get_is_scoped(self):
        record = self._record()
        other = BoxRuntime(ROOT / "examples" / "boxes" / "cn_dtc_stripe_store.json", ROOT / "packs")
        other_fingerprint = other.snapshot()["fingerprint"]
        self.assertEqual(self.store.list(runtime_fingerprint=other_fingerprint), [])
        self.assertIsNone(self.store.get(record["attempt_id"], runtime_fingerprint=other_fingerprint))
        self.assertEqual(
            self.store.get(record["attempt_id"], runtime_fingerprint=self.runtime.snapshot()["fingerprint"])["run_id"],
            record["run_id"],
        )

    def test_tampered_hash_chain_fails_closed(self):
        self._record()
        line = json.loads(self.store.events_file.read_text(encoding="utf-8"))
        line["record"]["ready"] = False
        self.store.events_file.write_text(json.dumps(line) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(PipelineRunStoreError, "hash mismatch"):
            self.store.list(runtime_fingerprint=self.runtime.snapshot()["fingerprint"])

    def test_threaded_appends_keep_unique_attempts_and_valid_sequence(self):
        with ThreadPoolExecutor(max_workers=8) as executor:
            records = list(executor.map(lambda _: self._record(), range(20)))
        self.assertEqual(len({item["attempt_id"] for item in records}), 20)
        listed = self.store.list(
            runtime_fingerprint=self.runtime.snapshot()["fingerprint"], limit=20,
        )
        self.assertEqual(len(listed), 20)
        lines = self.store.events_file.read_text(encoding="utf-8").splitlines()
        self.assertEqual([json.loads(line)["sequence"] for line in lines], list(range(1, 21)))

    def test_external_action_or_invalid_identity_is_never_recorded(self):
        unsafe = copy.deepcopy(self.result)
        unsafe["external_actions_performed"] = True
        with self.assertRaisesRegex(PipelineRunStoreError, "no external actions"):
            self._record(result=unsafe)
        with self.assertRaisesRegex(PipelineRunStoreError, "actor"):
            self._record(actor="")
        with self.assertRaisesRegex(PipelineRunStoreError, "attempt_id"):
            self.store.get("../../etc/passwd", runtime_fingerprint=self.runtime.snapshot()["fingerprint"])
        self.assertFalse(self.store.events_file.exists())

    def test_commerce_pipeline_also_has_stable_recordable_run_contract(self):
        runtime = BoxRuntime(ROOT / "examples" / "boxes" / "cn_dtc_api_store.json", ROOT / "packs")
        connector_request = json.loads(
            (ROOT / "examples" / "connectors" / "commerce_api_payload.json").read_text()
        )
        request = {
            "pipeline_id": "commerce.import_analyze",
            "payload": {
                "connector_id": "example.commerce_api_payload",
                "connector_request": connector_request,
            },
        }
        result = dispatch_box_pipeline_request(runtime, request)
        record = self.store.record(runtime.snapshot(), request, result, actor="测试负责人")
        self.assertTrue(record["ready"])
        self.assertEqual(record["review_status"], "not_required")
        self.assertTrue(record["release_candidate"])
        self.assertEqual(record["run_id"], result["pipeline"]["run_id"])
        self.assertFalse(record["network_access_performed"])

    def test_commerce_close_requires_source_cutoff_and_inventory_reviews(self):
        runtime = BoxRuntime(
            ROOT / "examples" / "boxes" / "cn_dtc_api_store.json", ROOT / "packs",
        )
        request = json.loads(
            (ROOT / "examples" / "pipelines" / "commerce_channel_close_fixture.json").read_text()
        )
        result = dispatch_box_pipeline_request(runtime, request)
        record = self.store.record(runtime.snapshot(), request, result, actor="电商月结操作人")
        self.assertTrue(record["ready"])
        self.assertEqual(record["required_review_gates"], [
            "commerce_source_mapping", "revenue_cutoff", "inventory_valuation_policy",
            "return_disposition_review", "import_landed_cost_policy", "sales_tax_nexus_review",
        ])
        self.assertTrue(record["candidate_only"])
        self.assertFalse(record["release_candidate"])
        queue = self.store.review_queue(runtime_fingerprint=runtime.snapshot()["fingerprint"])
        self.assertEqual({task["gate"] for task in queue}, {
            "commerce_source_mapping", "revenue_cutoff", "inventory_valuation_policy",
            "return_disposition_review", "import_landed_cost_policy", "sales_tax_nexus_review",
        })

    def test_marketplace_close_persists_seven_specialist_review_tasks(self):
        runtime = BoxRuntime(
            ROOT / "examples" / "boxes" / "cn_marketplace_store.json", ROOT / "packs",
        )
        request = json.loads(
            (ROOT / "examples" / "pipelines" / "marketplace_channel_close_fixture.json").read_text()
        )
        result = dispatch_box_pipeline_request(runtime, request)
        record = self.store.record(runtime.snapshot(), request, result, actor="平台月结操作人")
        expected = {
            "commerce_source_mapping", "marketplace_contract_mapping",
            "marketplace_inventory_mapping", "revenue_cutoff", "inventory_valuation_policy",
            "return_disposition_review", "import_landed_cost_policy",
        }
        self.assertTrue(record["ready"])
        self.assertEqual(set(record["required_review_gates"]), expected)
        self.assertEqual(record["lineage"]["inventory_mapping_evidence_count"], 2)
        self.assertFalse(record["release_candidate"])
        queue = self.store.review_queue(runtime_fingerprint=runtime.snapshot()["fingerprint"])
        self.assertEqual({task["gate"] for task in queue}, expected)

    def test_amazon_marketplace_close_derives_month_and_persists_only_safe_metric_scope(self):
        runtime = BoxRuntime(
            ROOT / "examples" / "boxes" / "us_marketplace_amazon_seller_c_corp.json",
            ROOT / "packs",
        )
        request = json.loads(
            (ROOT / "examples" / "pipelines" / "amazon_seller_marketplace_close_fixture.json")
            .read_text(encoding="utf-8")
        )
        result = dispatch_box_pipeline_request(runtime, request)
        record = self.store.record(
            runtime.snapshot(), request, result, actor="Amazon 月结操作人",
        )
        self.assertEqual(record["period"], "2026-08")
        self.assertEqual(record["lineage"]["marketplace_id"], "ATVPDKIKX0DER")
        self.assertEqual(record["lineage"]["dataset_counts"], {
            "commerce.amazon_seller_orders": 1,
            "commerce.amazon_seller_inventory": 1,
            "commerce.amazon_seller_transactions": 1,
        })
        metric = record["cfo_metric_operand_assembly"]
        self.assertEqual(metric["source_id"], "amazon_seller.marketplace_close")
        self.assertEqual(metric["assembly_count"], 1)
        self.assertEqual(metric["assemblies"][0]["dimension_scope"], {
            "dimension_type_id": "marketplace",
            "dimension_value_ids": ["ATVPDKIKX0DER"],
        })
        self.assertFalse(metric["operand_values_persisted"])
        serialized = self.store.events_file.read_text(encoding="utf-8")
        self.assertNotIn("eligible_marketplace_orders", serialized)
        self.assertNotIn("orders_matched_across_orders_finances_and_inventory", serialized)

    def test_game_settlement_candidate_records_both_human_review_gates(self):
        runtime = BoxRuntime(
            ROOT / "examples" / "boxes" / "global_game_studio.json", ROOT / "packs",
        )
        with tempfile.TemporaryDirectory() as fixture_dir:
            path = Path(fixture_dir) / "App Store结算.xlsx"
            book = Workbook()
            sheet = book.active
            sheet.title = "商店金流账单-App Store"
            sheet.append([
                "账期月份", "游戏名称", "平台", "渠道", "总流水", "退款流水",
                "结算金额", "预提所得税（结算币种）", "甲方实收金额（结算币种）", "结算币种",
            ])
            sheet.append(["2026-07", "G1", "iOS", "App Store", 1000, 0, 700, 70, 630, "USD"])
            book.save(path)
            request = {
                "pipeline_id": "game.channel_settlement_close",
                "payload": {
                    "entity_id": "sg_publisher",
                    "connector_id": "file.app_store_settlements",
                    "connector_request": {"path": str(path)},
                    "contract_mappings": [{
                        "entity_id": "sg_publisher", "period": "2026-07", "game": "G1",
                        "channel": "App Store", "currency": "USD",
                        "contract_basis": 1000, "contract_rate": 0.7,
                        "evidence": {
                            "source_reference": "contract://app-store/G1/2026",
                            "captured_at": "2026-08-13",
                        },
                    }],
                },
            }
            result = dispatch_box_pipeline_request(runtime, request)
        record = self.store.record(runtime.snapshot(), request, result, actor="游戏财务操作人")
        self.assertTrue(record["ready"])
        self.assertEqual(record["required_review_gates"], [
            "channel_contract_mapping", "game_principal_agent_assessment",
        ])
        self.assertEqual(record["lineage"]["contract_mapping_evidence_count"], 1)
        self.assertFalse(record["release_candidate"])
        queue = self.store.review_queue(runtime_fingerprint=runtime.snapshot()["fingerprint"])
        self.assertEqual({task["gate"] for task in queue}, {
            "channel_contract_mapping", "game_principal_agent_assessment",
        })


if __name__ == "__main__":
    unittest.main()
