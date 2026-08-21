import copy
import json
import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook

from src.accounting_import import parse_trial_balance_file, validate_trial_balance_lines
from src.box_pipeline import BoxPipelineError, run_trial_balance_review_pipeline
from src.box_runtime import BoxRuntime
from src.default_connectors import build_default_connector_registry


ROOT = Path(__file__).resolve().parents[1]


class AccountingImportTests(unittest.TestCase):
    def setUp(self):
        self.runtime = BoxRuntime(ROOT / "examples" / "boxes" / "cn_dtc_store.json", ROOT / "packs")

    def test_csv_connector_preserves_scope_and_evidence(self):
        result = build_default_connector_registry().dispatch(
            self.runtime,
            "file.trial_balance",
            {
                "path": str(ROOT / "examples" / "accounting" / "trial_balance.csv"),
                "default_entity_id": "cn_dtc_company",
            },
        )
        self.assertTrue(result["batch"]["quality"]["ready"], result)
        self.assertEqual(result["batch"]["quality"]["record_count"], 4)
        line = result["batch"]["datasets"]["finance.trial_balance_lines"][0]
        self.assertEqual(line["entity_id"], "cn_dtc_company")
        self.assertEqual(line["period"], "2026-08")
        self.assertEqual(line["evidence"]["batch_id"], result["batch"]["batch_id"])

    def test_xlsx_cover_sheet_is_ignored_and_defaults_are_applied(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "tb.xlsx"
            workbook = Workbook()
            workbook.active.title = "说明"
            workbook.active.append(["会计系统导出", "请复核"])
            sheet = workbook.create_sheet("余额表")
            sheet.append(["科目编码", "科目名称", "期末借方", "期末贷方"])
            sheet.append(["1001", "Cash", 10, 0])
            sheet.append(["4001", "Revenue", 0, 10])
            workbook.save(path)
            result = parse_trial_balance_file(
                path, entity_id="cn_dtc_company",
                default_period="2026-08", default_currency="CNY",
            )
        self.assertEqual(result["rejected_rows"], [])
        self.assertEqual(len(result["records"]), 2)
        self.assertEqual(result["records"][0]["currency"], "CNY")

    def test_duplicate_account_scope_fails_connector_quality_gate(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "duplicate.csv"
            path.write_text(
                "期间,币种,科目编码,科目名称,期末借方,期末贷方\n"
                "2026-08,CNY,1001,Cash,10,0\n"
                "2026-08,CNY,1001,Cash duplicate,20,0\n",
                encoding="utf-8",
            )
            result = build_default_connector_registry().dispatch(
                self.runtime, "file.trial_balance",
                {"path": str(path), "default_entity_id": "cn_dtc_company"},
            )
        self.assertFalse(result["batch"]["quality"]["ready"])
        self.assertEqual(len(result["batch"]["quality"]["duplicate_business_keys"]), 1)

    def test_validation_rejects_cross_entity_lines(self):
        lines = parse_trial_balance_file(
            ROOT / "examples" / "accounting" / "trial_balance.csv",
            entity_id="cn_dtc_company",
        )["records"]
        lines[0]["entity_id"] = "other_entity"
        result = validate_trial_balance_lines(lines, entity_id="cn_dtc_company")
        self.assertFalse(result["ready"])
        self.assertIn("cross_entity_trial_balance", {item["type"] for item in result["issues"]})


class TrialBalanceReviewPipelineTests(unittest.TestCase):
    def setUp(self):
        self.runtime = BoxRuntime(ROOT / "examples" / "boxes" / "cn_dtc_store.json", ROOT / "packs")
        self.request = json.loads(
            (ROOT / "examples" / "pipelines" / "trial_balance_review_fixture.json").read_text(
                encoding="utf-8"
            )
        )["payload"]

    def test_balanced_export_is_review_ready_but_never_posts_or_closes(self):
        first = run_trial_balance_review_pipeline(self.runtime, self.request)
        second = run_trial_balance_review_pipeline(self.runtime, self.request)
        self.assertTrue(first["ready"], first)
        self.assertEqual(first["pipeline"]["run_id"], second["pipeline"]["run_id"])
        self.assertEqual(first["pipeline"]["required_review_gates"], [
            "accounting_export_mapping_review", "trial_balance_control_total_review",
        ])
        summary = first["services"]["trial_balance_validation"]["output"]["summaries"][0]
        self.assertEqual(summary["closing_debit"], 1500.0)
        self.assertEqual(summary["closing_credit"], 1500.0)
        self.assertTrue(summary["balanced"])
        self.assertFalse(first["ledger_or_opening_balances_modified"])
        self.assertFalse(first["posting_performed"])
        self.assertFalse(first["period_close_performed"])

    def test_unbalanced_export_stops_at_deterministic_validation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "unbalanced.csv"
            path.write_text(
                "期间,币种,科目编码,科目名称,期末借方,期末贷方\n"
                "2026-08,CNY,1001,Cash,10,0\n"
                "2026-08,CNY,4001,Revenue,0,9\n",
                encoding="utf-8",
            )
            request = copy.deepcopy(self.request)
            request["connector_request"]["path"] = str(path)
            result = run_trial_balance_review_pipeline(self.runtime, request)
        self.assertFalse(result["ready"])
        self.assertEqual(result["blocked_at"], "trial_balance_validation")
        self.assertEqual(result["blockers"][0]["type"], "unbalanced_trial_balance")
        self.assertFalse(result["posting_performed"])

    def test_pipeline_rejects_conflicting_period_default(self):
        request = copy.deepcopy(self.request)
        request["connector_request"]["default_period"] = "2026-07"
        with self.assertRaisesRegex(BoxPipelineError, "does not match"):
            run_trial_balance_review_pipeline(self.runtime, request)


if __name__ == "__main__":
    unittest.main()
