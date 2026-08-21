from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from src.activation_runbook import ActivationRunbookError, ActivationRunbookStore
from src.activation_workspace import (
    MANIFEST_NAME,
    V3_DIRECTORIES,
    build_initialized_activation_status,
    initialize_activation_workspace,
    verify_activation_workspace,
)
from src.box_runtime import BoxRuntime
from src.default_services import build_default_service_registry


ROOT = Path(__file__).resolve().parents[1]


class ActivationRunbookTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.parent = Path(self.temp.name).resolve()
        self.config = ROOT / "examples" / "boxes" / "cn_marketplace_store.json"
        self.runtime = BoxRuntime(self.config, ROOT / "packs")
        self.workspace = self.parent / "private activation"
        initialize_activation_workspace(
            self.runtime,
            self.config,
            self.workspace,
            period="2026-08",
            facts_as_of="2026-08-14",
            prepared_by="activation-preparer",
        )
        self.store = ActivationRunbookStore(self.workspace)

    def tearDown(self):
        self.temp.cleanup()

    def test_empty_status_and_reported_progress_are_resumable_but_not_authoritative(self):
        initial = self.store.status(self.runtime)
        self.assertEqual(initial["event_count"], 0)
        self.assertEqual(initial["chain_head"], "GENESIS")
        self.assertEqual(
            initial["next_reported_progress_step_id"],
            "tax-workpaper-complete:cn_marketplace_company",
        )
        first = self.store.record(
            self.runtime,
            step_id="tax-workpaper-complete:cn_marketplace_company",
            outcome="reported_complete",
            actor="本地准备人",
            rationale="已在私有目录填写底稿，等待权威校验和独立复核",
            evidence_references=["private://tax/workpaper/checkpoint"],
        )
        self.assertFalse(first["authoritative_completion"])
        self.assertFalse(first["actor_returned"])
        second = self.store.record(
            self.runtime,
            step_id="tax-review:cn_marketplace_company",
            outcome="reported_complete",
            observed_exit_code=0,
            actor="本地复核执行人",
            rationale="命令退出码为零，但仍以正式税务轮换验证为准",
        )
        self.assertEqual(second["sequence"], 2)
        status = self.store.status(self.runtime)
        self.assertEqual(status["reported_complete_count"], 2)
        self.assertEqual(
            status["next_reported_progress_step_id"],
            "tax-import:cn_marketplace_company",
        )
        self.assertFalse(status["authoritative_completion_inferred"])
        self.assertFalse(status["evidence_gates_unlocked"])
        self.assertFalse(status["actors_returned"])
        self.assertFalse(status["evidence_references_returned"])
        verified = self.store.verify(self.runtime)
        self.assertTrue(verified["valid"])
        self.assertEqual(verified["event_count"], 2)

    def test_progress_never_changes_authoritative_activation_wave(self):
        services = build_default_service_registry()
        before = build_initialized_activation_status(
            self.runtime, services, self.workspace, as_of="2026-08-14",
        )["activation"]["summary"]
        self.store.record(
            self.runtime,
            step_id="tax-workpaper-complete:cn_marketplace_company",
            outcome="reported_complete",
            actor="准备人",
            rationale="仅记录操作者认为已经填写，不作为税务适用性通过证据",
        )
        after = build_initialized_activation_status(
            self.runtime, services, self.workspace, as_of="2026-08-14",
        )["activation"]["summary"]
        self.assertEqual(after["current_wave_stage_ids"], before["current_wave_stage_ids"])
        self.assertEqual(after["completed_stage_count"], before["completed_stage_count"])

    def test_exit_code_and_step_contract_fail_closed_without_appending(self):
        with self.assertRaisesRegex(ActivationRunbookError, "exit code 0"):
            self.store.record(
                self.runtime,
                step_id="tax-review:cn_marketplace_company",
                outcome="reported_complete",
                observed_exit_code=1,
                actor="执行人",
                rationale="故意使用不一致退出码验证失败关闭",
            )
        with self.assertRaisesRegex(ActivationRunbookError, "exit code 1-255"):
            self.store.record(
                self.runtime,
                step_id="tax-review:cn_marketplace_company",
                outcome="reported_failed",
                observed_exit_code=0,
                actor="执行人",
                rationale="故意使用不一致退出码验证失败关闭",
            )
        with self.assertRaisesRegex(ActivationRunbookError, "manual.*exit code"):
            self.store.record(
                self.runtime,
                step_id="tax-workpaper-complete:cn_marketplace_company",
                outcome="reported_complete",
                observed_exit_code=0,
                actor="执行人",
                rationale="手工作业不能伪装成命令退出码",
            )
        with self.assertRaisesRegex(ActivationRunbookError, "step_id"):
            self.store.record(
                self.runtime,
                step_id="unknown-step",
                outcome="blocked",
                actor="执行人",
                rationale="不存在的步骤不能写入当前命令合同",
            )
        self.assertFalse(self.store.events_file.exists())

    def test_hash_chain_permissions_tamper_and_other_box_binding(self):
        self.store.record(
            self.runtime,
            step_id="tax-workpaper-complete:cn_marketplace_company",
            outcome="blocked",
            actor="执行人",
            rationale="缺少当地税务复核人，暂时阻塞",
        )
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(self.store.root.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(self.store.events_file.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(self.store.lock_file.stat().st_mode), 0o600)
        other = BoxRuntime(
            ROOT / "examples" / "boxes" / "cn_dtc_store.json", ROOT / "packs",
        )
        with self.assertRaisesRegex(Exception, "current Box contract"):
            self.store.status(other)
        line = json.loads(self.store.events_file.read_text(encoding="utf-8"))
        line["outcome"] = "reported_complete"
        self.store.events_file.write_text(json.dumps(line) + "\n", encoding="utf-8")
        self.store.events_file.chmod(0o600)
        with self.assertRaisesRegex(ActivationRunbookError, "hash mismatch"):
            self.store.verify(self.runtime)

    def test_concurrent_append_keeps_one_valid_chain(self):
        def record(index: int):
            return self.store.record(
                self.runtime,
                step_id="tax-workpaper-complete:cn_marketplace_company",
                outcome="deferred" if index % 2 else "blocked",
                actor=f"执行人-{index}",
                rationale=f"并发写入测试事件编号 {index}，不表示权威完成",
            )

        with ThreadPoolExecutor(max_workers=8) as executor:
            records = list(executor.map(record, range(20)))
        self.assertEqual({item["sequence"] for item in records}, set(range(1, 21)))
        verified = self.store.verify(self.runtime)
        self.assertEqual(verified["event_count"], 20)
        lines = self.store.events_file.read_text(encoding="utf-8").splitlines()
        self.assertEqual(
            [json.loads(line)["sequence"] for line in lines], list(range(1, 21)),
        )

    def test_schema_v3_workspace_stays_verifiable_but_cannot_gain_runbook_claims(self):
        manifest_path = self.workspace / MANIFEST_NAME
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["schema_version"] = 3
        manifest["directory_contract"] = list(V3_DIRECTORIES)
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        manifest_path.chmod(0o600)
        (self.workspace / "runbook").rmdir()
        self.assertTrue(verify_activation_workspace(self.runtime, self.workspace)["valid"])
        with self.assertRaisesRegex(ActivationRunbookError, "schema v4 or v5"):
            self.store.status(self.runtime)


if __name__ == "__main__":
    unittest.main()
