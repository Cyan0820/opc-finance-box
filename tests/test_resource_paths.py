import tempfile
import unittest
from pathlib import Path

from src.resource_paths import find_resource_root


class ResourcePathTests(unittest.TestCase):
    def test_prefers_editable_source_tree(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "source"
            (root / "packs").mkdir(parents=True)
            self.assertEqual(find_resource_root(local_root=root, data_prefix=Path(temp_dir) / "prefix"), root)

    def test_falls_back_to_installed_share_tree(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            shared = base / "prefix" / "share" / "opc-finance-box"
            (shared / "packs").mkdir(parents=True)
            result = find_resource_root(local_root=base / "missing", data_prefix=base / "prefix")
            self.assertEqual(result, shared)


if __name__ == "__main__":
    unittest.main()
