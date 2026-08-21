from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path

from src.source_kit import (
    GUIDE_NAME,
    MANIFEST_NAME,
    SourceKitError,
    _add_tree,
    _canonical_bytes,
    build_source_kit_bundle,
    verify_source_kit_bundle,
    write_source_kit_bundle,
)


ROOT = Path(__file__).resolve().parents[1]


class SourceKitTests(unittest.TestCase):
    @staticmethod
    def _rewrite(files: dict[str, bytes]) -> bytes:
        output = io.BytesIO()
        with zipfile.ZipFile(
            output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9,
        ) as archive:
            for name, body in sorted(files.items()):
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                archive.writestr(info, body)
        return output.getvalue()

    def test_source_kit_is_deterministic_complete_and_excludes_workspace_state(self):
        first, filename, manifest = build_source_kit_bundle(ROOT)
        second, repeated_filename, repeated_manifest = build_source_kit_bundle(ROOT)
        self.assertEqual(first, second)
        self.assertEqual(filename, repeated_filename)
        self.assertEqual(manifest, repeated_manifest)
        self.assertRegex(filename, r"^opc-finance-box-source-kit-[0-9a-f]{12}\.zip$")
        self.assertTrue(manifest["fork_ready_source_tree"])
        self.assertTrue(manifest["editable_source_included"])
        self.assertTrue(manifest["tests_included"])
        self.assertFalse(manifest["dependency_lock_included"])
        self.assertFalse(manifest["vendored_dependencies_included"])
        self.assertFalse(manifest["git_history_included"])
        self.assertFalse(manifest["runtime_data_included"])
        self.assertFalse(manifest["private_evidence_included"])
        paths = {item["path"] for item in manifest["files"]}
        self.assertTrue({
            "pyproject.toml",
            "src/cli.py",
            "src/source_kit_unpack.py",
            "src/starter_workspace.py",
            "src/trial_workspace.py",
            "tests/test_box_builder.py",
            "tests/test_source_kit_unpack.py",
            "tests/test_starter_workspace.py",
            "tests/test_trial_workspace.py",
            "tests/test_jp_tax_pack.py",
            "tests/test_kr_tax_pack.py",
            "tests/test_ae_tax_pack.py",
            "packs/core/finance/manifest.json",
            "packs/jurisdictions/jp_domestic_corporation/manifest.json",
            "src/jp_tax_services.py",
            "examples/boxes/jp_dtc_shopify_stripe_kk.json",
            "docs/日本株式会社合同会社税务包.md",
            "packs/jurisdictions/kr_domestic_corporation/manifest.json",
            "src/kr_tax_services.py",
            "examples/boxes/kr_dtc_shopify_stripe_jusik_hoesa.json",
            "docs/韩国境内营利法人税务包.md",
            "packs/jurisdictions/ae_domestic_juridical_person/manifest.json",
            "src/ae_tax_services.py",
            "examples/boxes/ae_dtc_shopify_stripe_free_zone_company.json",
            "docs/阿联酋境内法人税务包.md",
            "examples/boxes/global_game_studio.json",
            "examples/boxes/cn_dtc_shopify_stripe_store.json",
            "examples/boxes/cn_marketplace_store.json",
            "public/index.html",
            "docs/可Fork源码安全初始化.md",
            "docs/五分钟本地试用.md",
            ".github/workflows/tests.yml",
            GUIDE_NAME,
        }.issubset(paths))
        self.assertEqual(
            {path for path in paths if path.startswith("data/")},
            {"data/commerce_demo.json", "data/demo_scenarios.json"},
        )
        forbidden_parts = {
            ".git", "dist", "build", "outputs", ".tmp", "__pycache__",
            "node_modules", "agent_runtime", "ledger", "pipeline_runs",
        }
        self.assertFalse(any(forbidden_parts.intersection(Path(path).parts) for path in paths))
        with zipfile.ZipFile(io.BytesIO(first)) as archive:
            self.assertEqual(set(archive.namelist()), paths | {MANIFEST_NAME})
            embedded = json.loads(archive.read(MANIFEST_NAME))
            self.assertEqual(embedded, manifest)
            self.assertIn("starter-compose", archive.read(GUIDE_NAME).decode("utf-8"))

    def test_writer_and_verifier_are_private_exclusive_and_reproducible(self):
        with tempfile.TemporaryDirectory() as temp_name:
            output = Path(temp_name).resolve() / "source-kit.zip"
            written = write_source_kit_bundle(output, project_root=ROOT)
            self.assertTrue(written["written"])
            self.assertFalse(written["output_path_returned"])
            self.assertFalse(written["runtime_data_included"])
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
            verified = verify_source_kit_bundle(output, project_root=ROOT)
            self.assertTrue(verified["valid"])
            self.assertEqual(verified["sha256"], written["sha256"])
            self.assertTrue(verified["reproducible_from_installed_source"])
            self.assertTrue(verified["archive_bytes_match_current_builder"])
            self.assertFalse(verified["archive_extracted"])
            self.assertFalse(verified["paths_returned"])
            with self.assertRaisesRegex(SourceKitError, "refusing to overwrite"):
                write_source_kit_bundle(output, project_root=ROOT)

    def test_member_manifest_path_and_polyglot_tampering_fail_closed(self):
        body, _, _ = build_source_kit_bundle(ROOT)
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            files = {name: archive.read(name) for name in archive.namelist()}
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name).resolve()
            changed = dict(files)
            changed["README.md"] += b"\nchanged\n"
            changed_path = root / "changed.zip"
            changed_path.write_bytes(self._rewrite(changed))
            with self.assertRaisesRegex(SourceKitError, "does not match"):
                verify_source_kit_bundle(changed_path, project_root=ROOT)

            consistent = dict(changed)
            manifest = json.loads(consistent[MANIFEST_NAME])
            record = next(item for item in manifest["files"] if item["path"] == "README.md")
            record["size_bytes"] = len(consistent["README.md"])
            record["sha256"] = hashlib.sha256(consistent["README.md"]).hexdigest()
            manifest["content_fingerprint"] = hashlib.sha256(
                _canonical_bytes(manifest["files"]),
            ).hexdigest()
            consistent[MANIFEST_NAME] = (
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
            ).encode("utf-8")
            consistent_path = root / "consistent.zip"
            consistent_path.write_bytes(self._rewrite(consistent))
            with self.assertRaisesRegex(SourceKitError, "does not reproduce"):
                verify_source_kit_bundle(consistent_path, project_root=ROOT)

            unsafe = dict(files)
            unsafe["../escape"] = b"bad"
            unsafe_path = root / "unsafe.zip"
            unsafe_path.write_bytes(self._rewrite(unsafe))
            with self.assertRaisesRegex(SourceKitError, "unsafe"):
                verify_source_kit_bundle(unsafe_path, project_root=ROOT)

            polyglot = root / "polyglot.zip"
            polyglot.write_bytes(body + b"untrusted-tail")
            with self.assertRaisesRegex(SourceKitError, "canonical"):
                verify_source_kit_bundle(polyglot, project_root=ROOT)

    def test_tree_allowlist_rejects_unexpected_types_and_symbolic_links(self):
        with tempfile.TemporaryDirectory() as temp_name:
            source = Path(temp_name).resolve() / "source"
            source.mkdir()
            (source / "ok.py").write_text("value = 1\n", encoding="utf-8")
            unexpected = source / "secret.env"
            unexpected.write_text("not-allowed", encoding="utf-8")
            with self.assertRaisesRegex(SourceKitError, "unexpected file type"):
                _add_tree(
                    {}, source=source, output_prefix="src",
                    allowed_suffixes=frozenset({".py"}),
                )
            unexpected.unlink()
            if os.name != "nt":
                os.symlink(source / "ok.py", source / "linked.py")
                with self.assertRaisesRegex(SourceKitError, "symbolic link"):
                    _add_tree(
                        {}, source=source, output_prefix="src",
                        allowed_suffixes=frozenset({".py"}),
                    )


if __name__ == "__main__":
    unittest.main()
