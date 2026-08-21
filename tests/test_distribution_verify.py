from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from src.distribution_verify import (
    REQUIRED_MEMBERS,
    DistributionVerifyError,
    verify_wheel,
)


class DistributionVerifyTests(unittest.TestCase):
    @staticmethod
    def _wheel(path: Path, *, name: str = "opc-finance-box", version: str = "0.1.0", omit: str | None = None):
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                "opc_finance_box-0.1.0.dist-info/METADATA",
                f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n",
            )
            archive.writestr(
                "opc_finance_box-0.1.0.dist-info/entry_points.txt",
                "[console_scripts]\n"
                "opc-finance-box = src.cli:main\n"
                "opc-finance-workbench = src.server:main\n",
            )
            for member in REQUIRED_MEMBERS:
                if member != omit:
                    archive.writestr(f"opc_finance_box-0.1.0.data/data/{member}", "fixture")

    def test_valid_product_wheel_requires_metadata_entries_and_assets(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "opc_finance_box-0.1.0-py3-none-any.whl"
            self._wheel(path)
            result = verify_wheel(path)
        self.assertTrue(result["valid"])
        self.assertEqual(result["project_name"], "opc-finance-box")
        self.assertEqual(result["version"], "0.1.0")
        self.assertEqual(result["required_member_count"], len(REQUIRED_MEMBERS))

    def test_unknown_distribution_metadata_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "UNKNOWN-0.0.0-py3-none-any.whl"
            self._wheel(path, name="UNKNOWN", version="0.0.0")
            with self.assertRaisesRegex(DistributionVerifyError, "project name"):
                verify_wheel(path)

    def test_missing_control_asset_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "opc_finance_box-0.1.0-py3-none-any.whl"
            self._wheel(path, omit="share/opc-finance-box/docs/Pipeline运行与恢复.md")
            with self.assertRaisesRegex(DistributionVerifyError, "missing required"):
                verify_wheel(path)

    def test_missing_browser_receipt_verifier_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "opc_finance_box-0.1.0-py3-none-any.whl"
            self._wheel(path, omit="src/handoff_receipt.py")
            with self.assertRaisesRegex(DistributionVerifyError, "handoff_receipt"):
                verify_wheel(path)

    def test_missing_connector_access_renewal_runtime_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "opc_finance_box-0.1.0-py3-none-any.whl"
            self._wheel(path, omit="src/connector_access_probe.py")
            with self.assertRaisesRegex(DistributionVerifyError, "connector_access_probe"):
                verify_wheel(path)

    def test_unsafe_member_path_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "opc_finance_box-0.1.0-py3-none-any.whl"
            self._wheel(path)
            with zipfile.ZipFile(path, "a") as archive:
                archive.writestr("../escape", "bad")
            with self.assertRaisesRegex(DistributionVerifyError, "unsafe member"):
                verify_wheel(path)


if __name__ == "__main__":
    unittest.main()
