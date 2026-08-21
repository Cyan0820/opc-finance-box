import tempfile
import unittest
from pathlib import Path

from src.box_config import load_pack_manifest
from src.jurisdiction_scaffold import JurisdictionScaffoldError, scaffold_jurisdiction_pack


class JurisdictionScaffoldTests(unittest.TestCase):
    def _create(self, root: str):
        return scaffold_jurisdiction_pack(
            root,
            slug="us_federal",
            country_code="US",
            display_name="US Federal Tax Design Pack",
            source_authority="Internal Revenue Service",
            source_title="Official business tax guide",
            source_url="https://www.irs.gov/businesses",
            verified_at="2026-08-13",
            rules_effective_at="2026-01-01",
        )

    def test_scaffold_is_valid_but_remains_design_only(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            result = self._create(temp_dir)
            pack = load_pack_manifest(Path(result["manifest"]))
            self.assertEqual(pack.pack_id, "jurisdiction.us_federal")
            self.assertEqual(pack.status, "experimental")
            self.assertEqual(pack.jurisdiction["tax_readiness"], "design")
            self.assertTrue(pack.rules["rules"][0]["human_review_required"])
            self.assertEqual(pack.rules["rules"][0]["automation_level"], "evidence")

    def test_scaffold_refuses_to_overwrite_existing_pack(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            self._create(temp_dir)
            with self.assertRaisesRegex(JurisdictionScaffoldError, "already exists"):
                self._create(temp_dir)

    def test_scaffold_requires_https_official_source(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(JurisdictionScaffoldError, "HTTPS"):
                scaffold_jurisdiction_pack(
                    temp_dir,
                    slug="us_federal",
                    country_code="US",
                    display_name="US",
                    source_authority="IRS",
                    source_title="Guide",
                    source_url="http://example.test",
                    verified_at="2026-08-13",
                    rules_effective_at="2026-01-01",
                )


if __name__ == "__main__":
    unittest.main()
