import tempfile
import unittest
import copy
import json
from pathlib import Path

from openpyxl import Workbook

from src.ledger_import import (
    parse_general_ledger_file,
    reconcile_ledger_to_trial_balance,
    validate_general_ledger_lines,
)
from src.box_runtime import BoxRuntime
from src.box_pipeline import BoxPipelineError, run_accounting_close_review_pipeline
from src.default_connectors import build_default_connector_registry
from src.default_services import build_default_service_registry


ROOT = Path(__file__).resolve().parents[1]


GENERAL_LEDGER = (
    "凭证号,分录行号,记账日期,期间,币种,科目编码,科目名称,借方金额,贷方金额,摘要\n"
    "J-001,1,2026-08-05,2026-08,CNY,1122,应收账款,300,0,收入与应付确认\n"
    "J-001,2,2026-08-05,2026-08,CNY,2202,应付账款,0,200,收入与应付确认\n"
    "J-001,3,2026-08-05,2026-08,CNY,4001,主营业务收入,0,100,收入与应付确认\n"
    "J-002,1,2026-08-12,2026-08,CNY,1001,银行存款,200,0,应收回款\n"
    "J-002,2,2026-08-12,2026-08,CNY,1122,应收账款,0,200,应收回款\n"
    "J-003,1,2026-08-20,2026-08,CNY,2202,应付账款,100,0,供应商付款\n"
    "J-003,2,2026-08-20,2026-08,CNY,1001,银行存款,0,100,供应商付款\n"
)

TRIAL_BALANCE = [
    {"entity_id": "cn_dtc_company", "period": "2026-08", "currency": "CNY",
     "account_code": "1001", "account_name": "银行存款", "opening_debit": 900,
     "opening_credit": 0, "period_debit": 200, "period_credit": 100,
     "closing_debit": 1000, "closing_credit": 0},
    {"entity_id": "cn_dtc_company", "period": "2026-08", "currency": "CNY",
     "account_code": "1122", "account_name": "应收账款", "opening_debit": 400,
     "opening_credit": 0, "period_debit": 300, "period_credit": 200,
     "closing_debit": 500, "closing_credit": 0},
    {"entity_id": "cn_dtc_company", "period": "2026-08", "currency": "CNY",
     "account_code": "2202", "account_name": "应付账款", "opening_debit": 0,
     "opening_credit": 200, "period_debit": 100, "period_credit": 200,
     "closing_debit": 0, "closing_credit": 300},
    {"entity_id": "cn_dtc_company", "period": "2026-08", "currency": "CNY",
     "account_code": "3001", "account_name": "实收资本", "opening_debit": 0,
     "opening_credit": 1100, "period_debit": 0, "period_credit": 0,
     "closing_debit": 0, "closing_credit": 1100},
    {"entity_id": "cn_dtc_company", "period": "2026-08", "currency": "CNY",
     "account_code": "4001", "account_name": "主营业务收入", "opening_debit": 0,
     "opening_credit": 0, "period_debit": 0, "period_credit": 100,
     "closing_debit": 0, "closing_credit": 100},
]

MAPPINGS = [
    {"account_code": "1001", "source_account_name": "银行存款",
     "statement_group": "assets", "statement_line_id": "cash",
     "statement_line_name": "现金及现金等价物"},
    {"account_code": "1122", "source_account_name": "应收账款",
     "statement_group": "assets", "statement_line_id": "trade_receivables",
     "statement_line_name": "应收账款"},
    {"account_code": "2202", "source_account_name": "应付账款",
     "statement_group": "liabilities", "statement_line_id": "trade_payables",
     "statement_line_name": "应付账款"},
    {"account_code": "3001", "source_account_name": "实收资本",
     "statement_group": "equity", "statement_line_id": "contributed_capital",
     "statement_line_name": "投入资本"},
    {"account_code": "4001", "source_account_name": "主营业务收入",
     "statement_group": "revenue", "statement_line_id": "operating_revenue",
     "statement_line_name": "营业收入"},
]


class LedgerImportTests(unittest.TestCase):
    def setUp(self):
        self.runtime = BoxRuntime(ROOT / "examples" / "boxes" / "cn_dtc_store.json", ROOT / "packs")

    def _csv(self, folder: str, content: str = GENERAL_LEDGER) -> Path:
        path = Path(folder) / "general-ledger.csv"
        path.write_text(content, encoding="utf-8")
        return path

    def test_csv_import_has_stable_entity_scoped_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            first = parse_general_ledger_file(
                self._csv(folder), entity_id="cn_dtc_company",
            )
            second = parse_general_ledger_file(
                Path(folder) / "general-ledger.csv", entity_id="cn_dtc_company",
            )
        self.assertEqual(len(first["records"]), 7)
        self.assertEqual(first["records"][0]["journal_line_id"], second["records"][0]["journal_line_id"])
        self.assertEqual(first["records"][0]["entity_id"], "cn_dtc_company")
        self.assertEqual(first["records"][0]["evidence"]["batch_id"], first["batch_id"])
        self.assertFalse(first["source"]["raw_source_rows_retained"])

    def test_xlsx_ignores_cover_sheet_and_applies_explicit_defaults(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "ledger.xlsx"
            workbook = Workbook()
            workbook.active.title = "说明"
            workbook.active.append(["General Ledger Export", "Read only"])
            sheet = workbook.create_sheet("明细账")
            sheet.append(["凭证号", "分录行号", "记账日期", "科目编码", "科目名称", "借方金额", "贷方金额"])
            sheet.append(["J-1", 1, "2026-08-01", "1001", "Cash", 10, 0])
            sheet.append(["J-1", 2, "2026-08-01", "3001", "Capital", 0, 10])
            workbook.save(path)
            parsed = parse_general_ledger_file(
                path, entity_id="cn_dtc_company",
                default_period="2026-08", default_currency="CNY",
            )
        self.assertEqual(parsed["rejected_rows"], [])
        self.assertEqual(len(parsed["records"]), 2)
        self.assertEqual(parsed["records"][0]["currency"], "CNY")

    def test_row_requires_one_debit_or_credit_and_matching_period(self):
        content = (
            "凭证号,分录行号,记账日期,期间,币种,科目编码,科目名称,借方金额,贷方金额\n"
            "J-1,1,2026-08-01,2026-07,CNY,1001,Cash,10,10\n"
        )
        with tempfile.TemporaryDirectory() as folder:
            parsed = parse_general_ledger_file(
                self._csv(folder, content), entity_id="cn_dtc_company",
            )
        self.assertEqual(parsed["records"], [])
        self.assertIn("period must match", parsed["rejected_rows"][0]["reason"])

    def test_each_journal_must_balance_without_currency_netting(self):
        with tempfile.TemporaryDirectory() as folder:
            rows = parse_general_ledger_file(
                self._csv(folder), entity_id="cn_dtc_company",
            )["records"]
        rows[0]["debit"] = 301
        validation = validate_general_ledger_lines(rows, entity_id="cn_dtc_company")
        self.assertFalse(validation["ready"])
        self.assertIn("unbalanced_journal", {item["type"] for item in validation["issues"]})

    def test_connector_and_service_are_statutory_read_only_providers(self):
        with tempfile.TemporaryDirectory() as folder:
            imported = build_default_connector_registry().dispatch(
                self.runtime,
                "file.general_ledger",
                {"path": str(self._csv(folder)), "default_entity_id": "cn_dtc_company"},
            )
        self.assertTrue(imported["batch"]["quality"]["ready"], imported)
        lines = imported["batch"]["datasets"]["finance.general_ledger_lines"]
        service = build_default_service_registry().dispatch(
            self.runtime,
            "core.reconcile_accounting_close_exports",
            {
                "period": "2026-08", "general_ledger_lines": lines,
                "trial_balance_lines": TRIAL_BALANCE, "account_mappings": MAPPINGS,
            },
            entity_id="cn_dtc_company",
        )
        self.assertEqual(service["service"]["action_class"], "read")
        self.assertEqual(service["service"]["entity_ids"], ["cn_dtc_company"])
        self.assertTrue(service["output"]["ready"], service)

    def test_gl_trial_balance_and_explicit_mapping_produce_candidate_only_statements(self):
        with tempfile.TemporaryDirectory() as folder:
            rows = parse_general_ledger_file(
                self._csv(folder), entity_id="cn_dtc_company",
            )["records"]
        result = reconcile_ledger_to_trial_balance(
            rows, TRIAL_BALANCE, MAPPINGS,
            entity_id="cn_dtc_company", period="2026-08",
        )
        self.assertTrue(result["ready"], result)
        self.assertEqual(result["mapping_coverage"]["coverage_percent"], 100.0)
        self.assertTrue(all(item["matched"] for item in result["account_reconciliation"]))
        statement = result["financial_statement_candidates"][0]
        self.assertEqual(statement["balance_sheet"]["assets"], 1500.0)
        self.assertEqual(statement["balance_sheet"]["liabilities"], 300.0)
        self.assertEqual(statement["balance_sheet"]["equity_before_current_profit"], 1100.0)
        self.assertEqual(statement["income_statement"]["profit_before_tax_candidate"], 100.0)
        self.assertTrue(statement["balance_sheet"]["balanced"])
        self.assertTrue(result["candidate_only"])
        self.assertFalse(result["ledger_modified"])
        self.assertFalse(result["posting_performed"])
        self.assertFalse(result["period_close_performed"])

    def test_missing_mapping_and_gl_tb_difference_fail_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            rows = parse_general_ledger_file(
                self._csv(folder), entity_id="cn_dtc_company",
            )["records"]
        trial = [dict(row) for row in TRIAL_BALANCE]
        trial[0]["period_debit"] = 201
        result = reconcile_ledger_to_trial_balance(
            rows, trial, MAPPINGS[:-1], entity_id="cn_dtc_company", period="2026-08",
        )
        self.assertFalse(result["ready"])
        issue_types = {item["type"] for item in result["issues"]}
        self.assertIn("ledger_trial_balance_mismatch", issue_types)
        self.assertIn("unmapped_trial_balance_account", issue_types)

    def test_accounting_close_pipeline_is_stable_and_never_posts_or_closes(self):
        request = json.loads(
            (ROOT / "examples" / "pipelines" / "accounting_close_review_fixture.json").read_text(
                encoding="utf-8"
            )
        )["payload"]
        first = run_accounting_close_review_pipeline(self.runtime, request)
        second = run_accounting_close_review_pipeline(self.runtime, request)
        self.assertTrue(first["ready"], first)
        self.assertEqual(first["pipeline"]["run_id"], second["pipeline"]["run_id"])
        self.assertEqual(first["pipeline"]["required_review_gates"], [
            "accounting_export_mapping_review", "trial_balance_control_total_review",
            "financial_statement_mapping_review", "accounting_policy_decision",
        ])
        self.assertEqual(first["lineage"]["general_ledger_line_count"], 7)
        self.assertEqual(first["lineage"]["trial_balance_line_count"], 5)
        self.assertEqual(first["founder_briefing"]["mapping_coverage"]["coverage_percent"], 100.0)
        self.assertFalse(first["ledger_modified"])
        self.assertFalse(first["opening_balances_modified"])
        self.assertFalse(first["posting_performed"])
        self.assertFalse(first["period_close_performed"])
        self.assertFalse(first["external_filing_performed"])

    def test_accounting_close_pipeline_stops_on_duplicate_gl_line(self):
        request = json.loads(
            (ROOT / "examples" / "pipelines" / "accounting_close_review_fixture.json").read_text(
                encoding="utf-8"
            )
        )["payload"]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "duplicate.csv"
            path.write_text(GENERAL_LEDGER + GENERAL_LEDGER.splitlines()[1] + "\n", encoding="utf-8")
            request = copy.deepcopy(request)
            request["general_ledger_connector_request"]["path"] = str(path)
            result = run_accounting_close_review_pipeline(self.runtime, request)
        self.assertFalse(result["ready"])
        self.assertEqual(result["blocked_at"], "quality_gate")
        self.assertEqual(result["services"], {})

    def test_accounting_close_pipeline_forces_both_sources_to_requested_period(self):
        request = json.loads(
            (ROOT / "examples" / "pipelines" / "accounting_close_review_fixture.json").read_text(
                encoding="utf-8"
            )
        )["payload"]
        request = copy.deepcopy(request)
        request["trial_balance_connector_request"]["default_period"] = "2026-07"
        with self.assertRaisesRegex(BoxPipelineError, "does not match"):
            run_accounting_close_review_pipeline(self.runtime, request)


if __name__ == "__main__":
    unittest.main()
