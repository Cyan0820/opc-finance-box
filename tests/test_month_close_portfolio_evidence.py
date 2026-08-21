import copy
import tempfile
import unittest
from pathlib import Path

from src.box_runtime import BoxRuntime
from src.month_close_portfolio_evidence import month_close_result_to_portfolio_source
from src.pipeline_run_store import PipelineRunStore, PipelineRunStoreError


ROOT = Path(__file__).resolve().parents[1]


class MonthClosePortfolioEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = PipelineRunStore(Path(self.temp.name) / "runs")
        self.runtime = BoxRuntime(
            ROOT / "examples" / "boxes" / "global_game_studio.json", ROOT / "packs",
        )

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def _result(entity_id, run_id, currency, values):
        return {
            "pipeline": {
                "pipeline_id": "finance.month_close_control",
                "run_id": run_id,
                "executed_at": "2026-08-14T00:00:00+00:00",
                "required_review_gates": ["month_close_control_review"],
            },
            "ready": True,
            "blocked_at": None,
            "services": {},
            "lineage": {"entity_id": entity_id, "period": "2026-07"},
            "founder_briefing": {
                "entity_id": entity_id,
                "period": "2026-07",
                "close_control_ready_for_review": True,
                "candidate_only": True,
                "currency_summaries": [{
                    "currency": currency,
                    "bank_account_count": 1,
                    "statement_cash_total": values[0],
                    "ledger_cash_total": values[0],
                    "assets": values[1],
                    "liabilities": values[2],
                    "revenue": values[3],
                    "expenses": values[4],
                    "profit_before_tax_candidate": values[3] - values[4],
                }],
            },
            "blockers": [],
            "external_actions_performed": False,
            "network_access_performed": False,
            "posting_performed": False,
            "period_close_performed": False,
            "retryable": False,
        }

    def _record_source(self, entity_id, run_id, currency, values):
        request = {
            "pipeline_id": "finance.month_close_control",
            "payload": {"entity_id": entity_id, "period": "2026-07"},
        }
        result = self._result(entity_id, run_id, currency, values)
        record = self.store.record(
            self.runtime.snapshot(), request, result, actor=f"operator-{entity_id}",
        )
        candidate = month_close_result_to_portfolio_source(result)
        candidate["source_attempt_id"] = record["attempt_id"]
        candidate["source_evidence"] = [
            f"pipeline-ledger://attempts/{record['attempt_id']}"
        ]
        return record, candidate

    def _portfolio_request(self):
        cn_record, cn = self._record_source(
            "cn_studio", "a" * 24, "CNY", (500, 800, 200, 1000, 600),
        )
        sg_record, sg = self._record_source(
            "sg_publisher", "b" * 24, "USD", (20, 50, 10, 100, 40),
        )
        return {
            "pipeline_id": "finance.multi_entity_month_close_portfolio",
            "payload": {
                "period": "2026-07",
                "entity_ids": ["cn_studio", "sg_publisher"],
                "entity_close_controls": [cn, sg],
                "fx_rates": {},
            },
        }, [cn_record, sg_record]

    def test_record_persists_only_source_fingerprint_and_requires_completed_reviews(self):
        request, records = self._portfolio_request()
        serialized = self.store.events_file.read_text(encoding="utf-8")
        self.assertNotIn('"statement_cash_total":500', serialized)
        self.assertTrue(all(record["portfolio_source_fingerprint"] for record in records))
        self.assertTrue(all(not record["portfolio_source_artifact_persisted"] for record in records))
        with self.assertRaisesRegex(PipelineRunStoreError, "review gate"):
            self.store.verify_month_close_portfolio_sources(
                request, runtime_fingerprint=self.runtime.snapshot()["fingerprint"],
            )
        for record in records:
            self.store.review(
                record["attempt_id"],
                runtime_fingerprint=self.runtime.snapshot()["fingerprint"],
                gate="month_close_control_review", decision="approved",
                actor="independent-reviewer", rationale="月结控制证据已逐主体复核",
                evidence_references=[f"review://{record['attempt_id']}"],
            )
        verified = self.store.verify_month_close_portfolio_sources(
            request, runtime_fingerprint=self.runtime.snapshot()["fingerprint"],
        )
        self.assertTrue(verified["verified"])
        self.assertEqual(verified["source_count"], 2)
        self.assertFalse(verified["raw_pipeline_results_persisted"])

    def test_tampered_aggregate_or_wrong_evidence_reference_is_rejected(self):
        request, records = self._portfolio_request()
        for record in records:
            self.store.review(
                record["attempt_id"],
                runtime_fingerprint=self.runtime.snapshot()["fingerprint"],
                gate="month_close_control_review", decision="approved",
                actor="independent-reviewer", rationale="月结控制证据已逐主体复核",
                evidence_references=[f"review://{record['attempt_id']}"],
            )
        tampered = copy.deepcopy(request)
        tampered["payload"]["entity_close_controls"][0]["currency_summaries"][0]["cash"] += 1
        with self.assertRaisesRegex(PipelineRunStoreError, "does not match"):
            self.store.verify_month_close_portfolio_sources(
                tampered, runtime_fingerprint=self.runtime.snapshot()["fingerprint"],
            )
        wrong_evidence = copy.deepcopy(request)
        wrong_evidence["payload"]["entity_close_controls"][0]["source_evidence"] = [
            "pipeline-ledger://attempts/ffffffffffffffffffffffff"
        ]
        with self.assertRaisesRegex(PipelineRunStoreError, "evidence must include"):
            self.store.verify_month_close_portfolio_sources(
                wrong_evidence, runtime_fingerprint=self.runtime.snapshot()["fingerprint"],
            )


if __name__ == "__main__":
    unittest.main()
