from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.source_kit import write_source_kit_bundle
from src.source_kit_unpack import (
    RECEIPT_NAME,
    SourceKitUnpackError,
    unpack_source_kit_bundle,
    verify_unpacked_source_kit,
)


ROOT = Path(__file__).resolve().parents[1]


class SourceKitUnpackTests(unittest.TestCase):
    def _bundle(self, root: Path) -> Path:
        bundle = root / "source-kit.zip"
        write_source_kit_bundle(bundle, project_root=ROOT)
        return bundle

    def test_unpack_is_private_receipted_and_verifiable_without_source_zip(self):
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name).resolve()
            bundle = self._bundle(root)
            destination = root / "fork-workspace"
            result = unpack_source_kit_bundle(
                bundle, destination, actor="source-kit-recipient", project_root=ROOT,
            )
            self.assertTrue(result["unpacked"])
            self.assertTrue(result["receipt_written_last"])
            self.assertTrue(result["installed_tree_verified"])
            self.assertEqual(
                result["installed_file_count"], result["extracted_member_count"] + 1,
            )
            self.assertTrue(result["source_archive_retained"])
            self.assertFalse(result["destination_path_returned"])
            self.assertFalse(result["actor_returned"])
            self.assertFalse(result["archive_members_executed"])
            self.assertFalse(result["git_repository_initialized"])
            self.assertFalse(result["dependencies_installed"])
            self.assertFalse(result["commands_executed"])
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o700)
                self.assertEqual(
                    stat.S_IMODE((destination / "README.md").stat().st_mode), 0o600,
                )
                self.assertEqual(
                    stat.S_IMODE((destination / RECEIPT_NAME).stat().st_mode), 0o600,
                )
            bundle.unlink()
            verified = verify_unpacked_source_kit(destination, project_root=ROOT)
            self.assertTrue(verified["valid"])
            self.assertTrue(verified["installed_source_reproducible"])
            self.assertFalse(verified["source_archive_required"])
            self.assertTrue(verified["source_archive_sha_matches_current_builder"])
            self.assertTrue(verified["pristine_workspace_required"])
            self.assertFalse(verified["receipt_is_digital_signature"])

    def test_invalid_input_destination_and_actor_fail_before_overwrite(self):
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name).resolve()
            bundle = self._bundle(root)
            existing = root / "existing"
            existing.mkdir()
            marker = existing / "keep.txt"
            marker.write_text("keep", encoding="utf-8")
            with self.assertRaisesRegex(SourceKitUnpackError, "already exists"):
                unpack_source_kit_bundle(
                    bundle, existing, actor="recipient", project_root=ROOT,
                )
            self.assertEqual(marker.read_text(encoding="utf-8"), "keep")
            with self.assertRaisesRegex(SourceKitUnpackError, "visible characters"):
                unpack_source_kit_bundle(
                    bundle, root / "bad-actor", actor=" padded ", project_root=ROOT,
                )
            self.assertFalse((root / "bad-actor").exists())
            invalid = root / "invalid.zip"
            invalid.write_bytes(b"not-a-zip")
            destination = root / "invalid-destination"
            with self.assertRaises(SourceKitUnpackError):
                unpack_source_kit_bundle(
                    invalid, destination, actor="recipient", project_root=ROOT,
                )
            self.assertFalse(destination.exists())

    def test_file_tree_receipt_and_permission_tampering_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name).resolve()
            bundle = self._bundle(root)

            changed = root / "changed"
            unpack_source_kit_bundle(
                bundle, changed, actor="recipient", project_root=ROOT,
            )
            with (changed / "README.md").open("ab") as stream:
                stream.write(b"\nchanged\n")
            with self.assertRaisesRegex(SourceKitUnpackError, "size or type"):
                verify_unpacked_source_kit(changed, project_root=ROOT)

            extra = root / "extra"
            unpack_source_kit_bundle(
                bundle, extra, actor="recipient", project_root=ROOT,
            )
            extra_file = extra / "unexpected.txt"
            extra_file.write_text("unexpected", encoding="utf-8")
            if os.name != "nt":
                extra_file.chmod(0o600)
            with self.assertRaisesRegex(SourceKitUnpackError, "does not match"):
                verify_unpacked_source_kit(extra, project_root=ROOT)

            receipt = root / "receipt"
            unpack_source_kit_bundle(
                bundle, receipt, actor="recipient", project_root=ROOT,
            )
            (receipt / RECEIPT_NAME).unlink()
            with self.assertRaises(SourceKitUnpackError):
                verify_unpacked_source_kit(receipt, project_root=ROOT)

            if os.name != "nt":
                permissions = root / "permissions"
                unpack_source_kit_bundle(
                    bundle, permissions, actor="recipient", project_root=ROOT,
                )
                (permissions / "README.md").chmod(0o644)
                with self.assertRaisesRegex(SourceKitUnpackError, "0600"):
                    verify_unpacked_source_kit(permissions, project_root=ROOT)

    def test_mid_write_failure_leaves_no_receipt_and_cannot_be_reused(self):
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name).resolve()
            bundle = self._bundle(root)
            destination = root / "partial"
            from src import source_kit_unpack as module

            original = module._write_private_file
            calls = 0

            def fail_during_write(path: Path, body: bytes) -> None:
                nonlocal calls
                calls += 1
                if calls == 4:
                    raise OSError("simulated write failure")
                original(path, body)

            with patch.object(module, "_write_private_file", side_effect=fail_during_write):
                with self.assertRaisesRegex(SourceKitUnpackError, "did not complete"):
                    unpack_source_kit_bundle(
                        bundle, destination, actor="recipient", project_root=ROOT,
                    )
            self.assertTrue(destination.is_dir())
            self.assertFalse((destination / RECEIPT_NAME).exists())
            with self.assertRaisesRegex(SourceKitUnpackError, "already exists"):
                unpack_source_kit_bundle(
                    bundle, destination, actor="recipient", project_root=ROOT,
                )


if __name__ == "__main__":
    unittest.main()
