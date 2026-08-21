import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ProductSkillTests(unittest.TestCase):
    def test_product_skills_have_valid_metadata_and_resolved_references(self):
        expected = {
            "build-opc-finance-box",
            "review-opc-month-close",
            "add-opc-tax-pack",
        }
        actual = {path.name for path in (ROOT / "skills").iterdir() if path.is_dir()}
        self.assertEqual(actual, expected)
        for name in expected:
            with self.subTest(skill=name):
                root = ROOT / "skills" / name
                content = (root / "SKILL.md").read_text(encoding="utf-8")
                self.assertNotIn("TODO", content)
                frontmatter = re.match(r"^---\n(.*?)\n---", content, re.S)
                self.assertIsNotNone(frontmatter)
                self.assertIn(f"name: {name}", frontmatter.group(1))
                self.assertIn("description:", frontmatter.group(1))
                for reference in re.findall(r"\]\((references/[^)]+)\)", content):
                    self.assertTrue((root / reference).is_file(), reference)
                metadata = (root / "agents" / "openai.yaml").read_text(encoding="utf-8")
                self.assertIn(f"${name}", metadata)
                self.assertIn("short_description:", metadata)


if __name__ == "__main__":
    unittest.main()
