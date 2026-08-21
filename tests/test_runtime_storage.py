from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from src.runtime_storage import (
    BACKUP_MANIFEST_NAME,
    MANIFEST_NAME,
    RESTORE_RECEIPT_NAME,
    V2_STORE_CONTRACT,
    V1_STORE_CONTRACT,
    RuntimeStorageError,
    backup_runtime_data,
    initialize_runtime_data,
    inspect_runtime_data,
    migrate_runtime_data,
    restore_runtime_backup,
    runtime_upgrade_preflight,
    verify_runtime_backup,
)


ROOT = Path(__file__).resolve().parents[1]


class RuntimeStorageTests(unittest.TestCase):
    def test_initialize_empty_layout_is_private_versioned_and_idempotent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "runtime"
            initialized = initialize_runtime_data(root, actor="部署负责人")
            self.assertTrue(initialized["initialized"])
            self.assertEqual(initialized["current_layout_version"], 3)
            self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE((root / MANIFEST_NAME).stat().st_mode), 0o600)
            for directory in (
                "ledger", "agent_runtime", "finance_inbox", "pipeline_runs", "connector_sync",
                "release_promotion",
            ):
                self.assertTrue((root / directory).is_dir())
                self.assertEqual(stat.S_IMODE((root / directory).stat().st_mode), 0o700)
            repeated = initialize_runtime_data(root, actor="部署负责人")
            self.assertFalse(repeated["initialized"])
            self.assertTrue(repeated["already_initialized"])

    def test_existing_data_requires_explicit_adoption_and_preflight_calls_it_out(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "legacy"
            (root / "ledger" / "datasets").mkdir(parents=True)
            (root / "ledger" / "datasets" / "settlements.json").write_text(
                '{"records": []}\n', encoding="utf-8"
            )
            preflight = runtime_upgrade_preflight(root)
            self.assertEqual(preflight["decision"], "adopt_legacy_layout")
            self.assertTrue(preflight["backup_required_before_change"])
            with self.assertRaisesRegex(RuntimeStorageError, "explicit legacy adoption"):
                initialize_runtime_data(root, actor="迁移负责人")
            adopted = initialize_runtime_data(root, actor="迁移负责人", adopt_existing=True)
            self.assertTrue(adopted["adopted_existing_data"])
            manifest = json.loads((root / MANIFEST_NAME).read_text(encoding="utf-8"))
            self.assertEqual(manifest["adoption_file_count"], 1)
            self.assertEqual(len(manifest["adoption_inventory_sha256"]), 64)

    def test_offline_backup_verify_and_new_target_restore(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            root = base / "runtime"
            initialize_runtime_data(root, actor="部署负责人")
            evidence = root / "ledger" / "datasets" / "settlements.json"
            evidence.parent.mkdir(parents=True)
            evidence.write_text('{"records": [{"id": "s1"}]}\n', encoding="utf-8")
            backup = base / "backup"
            with self.assertRaisesRegex(RuntimeStorageError, "service is stopped"):
                backup_runtime_data(
                    root, backup, actor="备份负责人", service_stopped_confirmed=False,
                )
            result = backup_runtime_data(
                root, backup, actor="备份负责人", service_stopped_confirmed=True,
            )
            self.assertTrue(result["valid"])
            self.assertTrue(result["contains_sensitive_runtime_data"])
            self.assertFalse(result["encrypted_by_tool"])
            self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o700)
            verified = verify_runtime_backup(backup)
            self.assertEqual(verified["inventory_sha256"], result["inventory_sha256"])
            target = base / "restored"
            restored = restore_runtime_backup(backup, target, actor="恢复负责人")
            self.assertTrue(restored["restored"])
            self.assertEqual(evidence.read_bytes(), (target / evidence.relative_to(root)).read_bytes())
            self.assertTrue((target / RESTORE_RECEIPT_NAME).is_file())
            self.assertEqual(inspect_runtime_data(target)["state"], "ready")
            with self.assertRaisesRegex(RuntimeStorageError, "must not exist"):
                restore_runtime_backup(backup, target, actor="恢复负责人")

    def test_backup_tamper_and_symlink_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            root = base / "runtime"
            initialize_runtime_data(root, actor="部署负责人")
            backup = base / "backup"
            backup_runtime_data(root, backup, actor="备份负责人", service_stopped_confirmed=True)
            (backup / "payload" / MANIFEST_NAME).write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeStorageError, "inventory"):
                verify_runtime_backup(backup)
            symlink_root = base / "symlink-runtime"
            symlink_root.mkdir()
            os.symlink(base / "outside", symlink_root / "unsafe")
            with self.assertRaisesRegex(RuntimeStorageError, "symlinks"):
                inspect_runtime_data(symlink_root)

    def test_future_layout_is_blocked_without_in_place_migration(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "runtime"
            initialize_runtime_data(root, actor="部署负责人")
            manifest_path = root / MANIFEST_NAME
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["layout_version"] = 4
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            preflight = runtime_upgrade_preflight(root)
            self.assertEqual(preflight["decision"], "blocked_by_newer_layout")
            self.assertFalse(preflight["automatic_in_place_migration_available"])
            with self.assertRaisesRegex(RuntimeStorageError, "supported initialized"):
                backup_runtime_data(
                    root, Path(temp_dir) / "backup", actor="备份负责人",
                    service_stopped_confirmed=True,
                )

    def test_v1_to_current_migration_requires_matching_stopped_service_backup(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            root = base / "runtime"
            initialize_runtime_data(root, actor="初始部署人")
            (root / "connector_sync").rmdir()
            (root / "release_promotion").rmdir()
            manifest_path = root / MANIFEST_NAME
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["layout_version"] = 1
            manifest["stores"] = V1_STORE_CONTRACT
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            manifest_path.chmod(0o600)
            preflight = runtime_upgrade_preflight(root)
            self.assertEqual(preflight["decision"], "offline_migration_required")
            backup = base / "v1-backup"
            backup_runtime_data(
                root, backup, actor="备份负责人", service_stopped_confirmed=True,
            )
            with self.assertRaisesRegex(RuntimeStorageError, "workbench and scheduler are stopped"):
                migrate_runtime_data(
                    root, backup, actor="迁移负责人", service_stopped_confirmed=False,
                )
            migrated = migrate_runtime_data(
                root, backup, actor="迁移负责人", service_stopped_confirmed=True,
            )
            self.assertTrue(migrated["migrated"])
            self.assertEqual(migrated["from_layout_version"], 1)
            self.assertEqual(migrated["to_layout_version"], 3)
            self.assertEqual(
                migrated["new_stores_initialized"], ["connector_sync", "release_promotion"],
            )
            self.assertTrue((root / "connector_sync").is_dir())
            self.assertTrue((root / "release_promotion").is_dir())
            self.assertEqual(inspect_runtime_data(root)["state"], "ready")

    def test_v2_to_v3_migration_adds_only_release_promotion_store(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            root = base / "runtime"
            initialize_runtime_data(root, actor="初始部署人")
            (root / "release_promotion").rmdir()
            manifest_path = root / MANIFEST_NAME
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["layout_version"] = 2
            manifest["stores"] = V2_STORE_CONTRACT
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            manifest_path.chmod(0o600)
            backup = base / "v2-backup"
            backup_runtime_data(
                root, backup, actor="备份负责人", service_stopped_confirmed=True,
            )
            migrated = migrate_runtime_data(
                root, backup, actor="迁移负责人", service_stopped_confirmed=True,
            )
            self.assertEqual(migrated["from_layout_version"], 2)
            self.assertEqual(migrated["new_stores_initialized"], ["release_promotion"])
            self.assertTrue((root / "connector_sync").is_dir())
            self.assertTrue((root / "release_promotion").is_dir())

    def test_insecure_root_permissions_and_workbench_legacy_start_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            runtime = base / "runtime"
            initialize_runtime_data(runtime, actor="部署负责人")
            runtime.chmod(0o755)
            inspection = inspect_runtime_data(runtime)
            self.assertEqual(inspection["state"], "unsafe_permissions")
            self.assertEqual(
                runtime_upgrade_preflight(runtime)["decision"],
                "fix_root_permissions_before_start",
            )

            legacy = base / "legacy"
            legacy.mkdir()
            (legacy / "business-data.json").write_text("{}\n", encoding="utf-8")
            environment = {
                "PATH": os.environ.get("PATH", ""),
                "PYTHONPATH": str(ROOT),
                "OPC_FINANCE_DATA_DIR": str(legacy),
                "OPC_FINANCE_BOX_CONFIG": str(
                    ROOT / "examples" / "boxes" / "global_game_studio.json"
                ),
                "OPC_FINANCE_PACKS_ROOT": str(ROOT / "packs"),
            }
            completed = subprocess.run(
                [sys.executable, "-c", "import src.server"],
                cwd=ROOT,
                env=environment,
                text=True,
                capture_output=True,
                timeout=15,
                check=False,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("runtime data layout is not ready", completed.stderr)
