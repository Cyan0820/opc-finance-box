import json
import os
import tempfile
import unittest
from pathlib import Path

from src.box_eval import BoxEvalError, run_box_eval_suite


ROOT = Path(__file__).resolve().parents[1]


class BoxEvalTests(unittest.TestCase):
    def test_bundled_core_pack_suite_passes_without_external_actions(self):
        result = run_box_eval_suite(ROOT / "evals" / "core_packs.json", ROOT / "packs", project_root=ROOT)
        self.assertTrue(result["passed"], result)
        self.assertEqual(result["counts"], {"total": 48, "passed": 48, "failed": 0})
        self.assertFalse(result["external_actions_performed"])

    def test_failed_assertion_is_reported_without_raising(self):
        suite = json.loads((ROOT / "evals" / "core_packs.json").read_text(encoding="utf-8"))
        suite["cases"] = suite["cases"][:1]
        suite["cases"][0]["assertions"][0]["value"] = False
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            path = Path(temp_dir) / "suite.json"
            path.write_text(json.dumps(suite), encoding="utf-8")
            result = run_box_eval_suite(path, ROOT / "packs", project_root=ROOT)
        self.assertFalse(result["passed"])
        self.assertEqual(result["counts"]["failed"], 1)

    def test_pipeline_fixture_paths_resolve_from_project_root_not_process_cwd(self):
        suite = json.loads((ROOT / "evals" / "core_packs.json").read_text(encoding="utf-8"))
        suite["cases"] = [next(
            item for item in suite["cases"]
            if item["id"] == "bank-statement-close-masks-accounts-and-never-posts"
        )]
        with tempfile.TemporaryDirectory() as temp_dir:
            suite_path = Path(temp_dir) / "suite.json"
            suite_path.write_text(json.dumps(suite), encoding="utf-8")
            previous = Path.cwd()
            try:
                os.chdir(temp_dir)
                result = run_box_eval_suite(suite_path, ROOT / "packs", project_root=ROOT)
            finally:
                os.chdir(previous)
        self.assertTrue(result["passed"], result)
        self.assertEqual(result["counts"], {"total": 1, "passed": 1, "failed": 0})

    def test_suite_paths_cannot_escape_project_root(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            path = Path(temp_dir) / "suite.json"
            path.write_text(json.dumps({
                "schema_version": 1, "suite_id": "bad", "cases": [{
                    "id": "escape", "box_config": "../../../../etc/passwd",
                    "request": "examples/service_requests/dtc_margin.json",
                    "assertions": [{"path": "output.ready", "value": True}],
                }],
            }), encoding="utf-8")
            result = run_box_eval_suite(path, ROOT / "packs", project_root=ROOT)
        self.assertFalse(result["passed"])
        self.assertIn("escapes project_root", result["cases"][0]["error"])

    def test_pipeline_eval_refuses_network_connector_mode(self):
        request = json.loads(
            (ROOT / "examples" / "pipelines" / "shopify_stripe_daily_close_fixture.json").read_text()
        )
        request["payload"]["shopify_request"] = {"mode": "fetch"}
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            temp_path = Path(temp_dir)
            request_path = temp_path / "request.json"
            suite_path = temp_path / "suite.json"
            request_path.write_text(json.dumps(request), encoding="utf-8")
            suite_path.write_text(json.dumps({
                "schema_version": 1,
                "suite_id": "offline-only",
                "cases": [{
                    "id": "pipeline-fetch",
                    "type": "pipeline",
                    "box_config": "examples/boxes/cn_dtc_shopify_stripe_store.json",
                    "request": str(request_path.relative_to(ROOT)),
                    "assertions": [{"path": "ready", "value": True}],
                }],
            }), encoding="utf-8")
            result = run_box_eval_suite(suite_path, ROOT / "packs", project_root=ROOT)
        self.assertFalse(result["passed"])
        self.assertIn("shopify_request.mode=fixture", result["cases"][0]["error"])


if __name__ == "__main__":
    unittest.main()
