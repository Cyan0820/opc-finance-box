import json
import os
import tempfile
import unittest
from pathlib import Path

from src.box_builder import write_box_candidate_bundle
from src.handoff_receipt import (
    BrowserHandoffReceiptError,
    verify_browser_handoff_receipt,
)


ROOT = Path(__file__).resolve().parents[1]
PACKS = ROOT / "packs"


class BrowserHandoffReceiptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        spec = json.loads(
            (ROOT / "examples" / "box_specs" / "dtc_cn.json").read_text(encoding="utf-8")
        )
        self.bundle = self.root / "opc-finance-box-0123456789ab.zip"
        written = write_box_candidate_bundle(spec, PACKS, self.bundle)
        self.receipt = self.root / "opc-finance-box-0123456789ab.browser-receipt.json"
        self.payload = {
            "schema_version": 1,
            "verification_status": "passed",
            "filename": self.bundle.name,
            "size_bytes": written["size_bytes"],
            "sha256": written["sha256"],
            "runtime_fingerprint": written["runtime_fingerprint"],
            "manifest_schema_version": 2,
            "manifest_file_count": written["file_count"],
            "browser_bytes_verified": True,
            "receipt_is_digital_signature": False,
            "archive_members_executed": False,
            "active_runtime_changed": False,
            "external_actions_performed": False,
        }
        self._write_receipt(self.payload)

    def tearDown(self):
        self.temp.cleanup()

    def _write_receipt(self, payload):
        self.receipt.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        if os.name != "nt":
            self.receipt.chmod(0o600)

    def test_receipt_binds_formally_verified_bundle_without_overclaiming_browser_identity(self):
        result = verify_browser_handoff_receipt(self.bundle, self.receipt, PACKS)
        self.assertTrue(result["valid"])
        self.assertTrue(result["bundle_receipt_match"])
        self.assertEqual(result["bundle_sha256"], self.payload["sha256"])
        self.assertEqual(result["bundle_size_bytes"], self.payload["size_bytes"])
        self.assertEqual(
            result["runtime_fingerprint"], self.payload["runtime_fingerprint"]
        )
        self.assertEqual(
            result["manifest_schema_version"], self.payload["manifest_schema_version"]
        )
        self.assertEqual(
            result["manifest_file_count"], self.payload["manifest_file_count"]
        )
        self.assertTrue(result["reproducible_with_installed_packs"])
        self.assertTrue(result["archive_bytes_match_current_builder"])
        self.assertTrue(result["browser_bytes_verified_claimed"])
        self.assertFalse(result["browser_execution_attested"])
        self.assertFalse(result["receipt_is_digital_signature"])
        self.assertFalse(result["receipt_is_identity_attestation"])
        self.assertFalse(result["archive_extracted"])
        self.assertFalse(result["paths_returned"])

    def test_receipt_bundle_binding_and_exact_contract_fail_closed(self):
        mutations = {
            "filename": "opc-finance-box-ffffffffffff.zip",
            "size_bytes": self.payload["size_bytes"] + 1,
            "sha256": "f" * 64,
            "runtime_fingerprint": "e" * 64,
            "manifest_schema_version": self.payload["manifest_schema_version"] + 1,
            "manifest_file_count": self.payload["manifest_file_count"] + 1,
        }
        for field, value in mutations.items():
            with self.subTest(field=field):
                changed = dict(self.payload)
                changed[field] = value
                self._write_receipt(changed)
                with self.assertRaisesRegex(
                    BrowserHandoffReceiptError, "does not match the verified bundle"
                ):
                    verify_browser_handoff_receipt(self.bundle, self.receipt, PACKS)
        changed = dict(self.payload)
        changed["unknown"] = "not allowed"
        self._write_receipt(changed)
        with self.assertRaisesRegex(BrowserHandoffReceiptError, "contract is invalid"):
            verify_browser_handoff_receipt(self.bundle, self.receipt, PACKS)
        changed = dict(self.payload)
        changed["browser_bytes_verified"] = False
        self._write_receipt(changed)
        with self.assertRaisesRegex(BrowserHandoffReceiptError, "safety boundary"):
            verify_browser_handoff_receipt(self.bundle, self.receipt, PACKS)

    @unittest.skipIf(os.name == "nt", "POSIX permission and symlink contract")
    def test_receipt_requires_owner_private_regular_single_link_file(self):
        self.receipt.chmod(0o644)
        with self.assertRaisesRegex(BrowserHandoffReceiptError, "mode 0600"):
            verify_browser_handoff_receipt(self.bundle, self.receipt, PACKS)
        self.receipt.chmod(0o600)
        symlink = self.root / "receipt-link.json"
        os.symlink(self.receipt, symlink)
        with self.assertRaisesRegex(BrowserHandoffReceiptError, "regular file"):
            verify_browser_handoff_receipt(self.bundle, symlink, PACKS)
        hardlink = self.root / "receipt-hardlink.json"
        os.link(self.receipt, hardlink)
        with self.assertRaisesRegex(BrowserHandoffReceiptError, "private regular file"):
            verify_browser_handoff_receipt(self.bundle, self.receipt, PACKS)


if __name__ == "__main__":
    unittest.main()
