from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import src.handoff_unpack as handoff_unpack
from src.box_builder import write_box_candidate_bundle
from src.handoff_unpack import (
    BoxHandoffUnpackError,
    RECEIPT_NAME,
    unpack_box_candidate_bundle,
    verify_unpacked_box_candidate,
)


ROOT = Path(__file__).resolve().parents[1]
PACKS = ROOT / "packs"


class HandoffUnpackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.bundle = self.root / "customer-handoff.zip"
        self.spec = json.loads(
            (ROOT / "examples" / "box_specs" / "dtc_cn.json").read_text(
                encoding="utf-8",
            )
        )
        self.written = write_box_candidate_bundle(self.spec, PACKS, self.bundle)

    def tearDown(self):
        self.temp.cleanup()

    def _unpack(self, name: str = "fork-root") -> tuple[Path, dict]:
        destination = self.root / name
        result = unpack_box_candidate_bundle(
            self.bundle, PACKS, destination, actor="handoff-recipient",
        )
        return destination, result

    def test_verified_bundle_materializes_private_tree_and_non_signing_receipt(self):
        destination, result = self._unpack()
        self.assertTrue(result["unpacked"])
        self.assertTrue(result["installed_tree_verified"])
        self.assertTrue(result["receipt_written_last"])
        self.assertEqual(result["extracted_member_count"], 55)
        self.assertEqual(result["installed_file_count"], 56)
        self.assertEqual(result["bundle_sha256"], self.written["sha256"])
        self.assertFalse(result["destination_path_returned"])
        self.assertFalse(result["actor_returned"])
        self.assertFalse(result["archive_members_executed"])
        self.assertFalse(result["authoritative_financial_evidence"])
        self.assertTrue(self.bundle.is_file())
        receipt_path = destination / RECEIPT_NAME
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        self.assertEqual(receipt["actor"], "handoff-recipient")
        self.assertFalse(receipt["receipt_is_digital_signature"])
        self.assertFalse(receipt["authoritative_financial_evidence"])
        self.assertFalse(receipt["archive_members_executed"])
        if os.name != "nt":
            for path in [destination, *(item for item in destination.rglob("*") if item.is_dir())]:
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
            for path in (item for item in destination.rglob("*") if item.is_file()):
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                self.assertEqual(path.stat().st_nlink, 1)
        verified = verify_unpacked_box_candidate(destination, PACKS)
        self.assertTrue(verified["valid"])
        self.assertEqual(verified["installed_file_count"], 56)
        self.assertTrue(verified["installed_pack_reproducible"])
        self.assertFalse(verified["source_bundle_required"])
        self.assertFalse(verified["receipt_is_digital_signature"])
        self.assertFalse(verified["paths_returned"])
        self.assertFalse(verified["actor_returned"])
        self.bundle.unlink()
        self.assertTrue(verify_unpacked_box_candidate(destination, PACKS)["valid"])

    def test_existing_and_concurrent_destinations_never_overwrite(self):
        existing = self.root / "existing"
        existing.mkdir()
        sentinel = existing / "sentinel.txt"
        sentinel.write_text("preserve", encoding="utf-8")
        with self.assertRaisesRegex(BoxHandoffUnpackError, "refusing to overwrite"):
            unpack_box_candidate_bundle(
                self.bundle, PACKS, existing, actor="handoff-recipient",
            )
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve")

        destination = self.root / "concurrent"

        def run(index: int):
            try:
                return unpack_box_candidate_bundle(
                    self.bundle, PACKS, destination, actor=f"recipient-{index}",
                )
            except BoxHandoffUnpackError as exc:
                return exc

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(run, range(2)))
        self.assertEqual(sum(isinstance(item, dict) for item in outcomes), 1)
        self.assertEqual(sum(isinstance(item, BoxHandoffUnpackError) for item in outcomes), 1)
        self.assertTrue(verify_unpacked_box_candidate(destination, PACKS)["valid"])

    def test_untrusted_archive_is_rejected_before_destination_creation(self):
        tampered = self.root / "polyglot.zip"
        tampered.write_bytes(self.bundle.read_bytes() + b"appended-untrusted-data")
        tampered.chmod(0o600)
        destination = self.root / "must-not-exist"
        with self.assertRaisesRegex(BoxHandoffUnpackError, "canonical"):
            unpack_box_candidate_bundle(
                tampered, PACKS, destination, actor="handoff-recipient",
            )
        self.assertFalse(destination.exists())

    def test_interrupted_materialization_leaves_no_completion_receipt(self):
        destination = self.root / "interrupted"
        original = handoff_unpack._write_private_file
        calls = 0

        def fail_during_write(path: Path, body: bytes):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise OSError("simulated disk failure")
            return original(path, body)

        with patch("src.handoff_unpack._write_private_file", side_effect=fail_during_write):
            with self.assertRaisesRegex(BoxHandoffUnpackError, "no valid receipt"):
                unpack_box_candidate_bundle(
                    self.bundle, PACKS, destination, actor="handoff-recipient",
                )
        self.assertTrue(destination.is_dir())
        self.assertFalse((destination / RECEIPT_NAME).exists())
        with self.assertRaisesRegex(BoxHandoffUnpackError, "receipt is missing"):
            verify_unpacked_box_candidate(destination, PACKS)

    def test_tree_file_receipt_permission_and_symlink_tampering_fail_closed(self):
        destination, _ = self._unpack("file-tamper")
        handoff = destination / "HANDOFF.md"
        handoff.write_bytes(handoff.read_bytes() + b"\nchanged\n")
        handoff.chmod(0o600)
        with self.assertRaisesRegex(BoxHandoffUnpackError, "does not match"):
            verify_unpacked_box_candidate(destination, PACKS)

        destination, _ = self._unpack("receipt-tamper")
        receipt_path = destination / RECEIPT_NAME
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt["actor"] = "different-recipient"
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        receipt_path.chmod(0o600)
        with self.assertRaisesRegex(BoxHandoffUnpackError, "payload fingerprint"):
            verify_unpacked_box_candidate(destination, PACKS)

        destination, _ = self._unpack("unexpected-file")
        extra = destination / "unexpected.txt"
        extra.write_text("unexpected", encoding="utf-8")
        extra.chmod(0o600)
        with self.assertRaisesRegex(BoxHandoffUnpackError, "does not match its receipt"):
            verify_unpacked_box_candidate(destination, PACKS)

        if os.name != "nt":
            destination, _ = self._unpack("wide-permission")
            (destination / "box.json").chmod(0o644)
            with self.assertRaisesRegex(BoxHandoffUnpackError, "mode 0600"):
                verify_unpacked_box_candidate(destination, PACKS)

            destination, _ = self._unpack("symlink-tamper")
            handoff = destination / "HANDOFF.md"
            handoff.unlink()
            os.symlink(self.bundle, handoff)
            with self.assertRaisesRegex(BoxHandoffUnpackError, "symbolic link"):
                verify_unpacked_box_candidate(destination, PACKS)

    def test_invalid_actor_and_relative_root_fail_before_writing(self):
        with self.assertRaisesRegex(BoxHandoffUnpackError, "actor"):
            unpack_box_candidate_bundle(
                self.bundle, PACKS, self.root / "bad-actor", actor=" padded ",
            )
        self.assertFalse((self.root / "bad-actor").exists())
        with self.assertRaisesRegex(BoxHandoffUnpackError, "absolute"):
            unpack_box_candidate_bundle(
                self.bundle, PACKS, Path("relative-fork"), actor="recipient",
            )


if __name__ == "__main__":
    unittest.main()
