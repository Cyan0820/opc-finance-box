from __future__ import annotations

import json
import unittest
from pathlib import Path

from src.box_runtime import BoxRuntime
from src.default_connectors import build_box_connector_registry
from src.default_services import build_default_service_registry


ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / "packs" / "connectors" / "stripe"


class StripeServicesTests(unittest.TestCase):
    def setUp(self):
        self.runtime = BoxRuntime(
            ROOT / "examples" / "boxes" / "cn_dtc_stripe_store.json", ROOT / "packs",
        )
        connectors = build_box_connector_registry(self.runtime)
        balance_request = json.loads((PACK / "fixture-balance-transactions.json").read_text(encoding="utf-8"))
        payout_request = json.loads((PACK / "fixture-payouts.json").read_text(encoding="utf-8"))
        self.balance = connectors.dispatch(
            self.runtime, "stripe.balance_transactions", balance_request,
        )["batch"]["datasets"]["payments.stripe_balance_transactions"]
        self.payouts = connectors.dispatch(
            self.runtime, "stripe.payouts", payout_request,
        )["batch"]["datasets"]["payments.stripe_payouts"]
        self.services = build_default_service_registry()

    @staticmethod
    def _bank(bank_id: str = "bank_001", *, reference: str = "Stripe po_demo_001") -> dict:
        return {
            "bank_transaction_id": bank_id,
            "entity_id": "cn_dtc_company",
            "currency": "USD",
            "amount_minor": 7180,
            "direction": "inflow",
            "transaction_date": "2026-08-15",
            "reference": reference,
            "evidence": {"source_file": "bank.csv", "batch_id": "bank-demo"},
        }

    def test_balance_summary_keeps_currency_categories_fees_and_refunds_separate(self):
        output = self.services.dispatch(
            self.runtime,
            "stripe.summarize_balance_activity",
            {"balance_transactions": self.balance},
            entity_id="cn_dtc_company",
        )["output"]
        self.assertTrue(output["ready"])
        summary = output["currency_summary"][0]
        self.assertEqual(summary["currency"], "USD")
        self.assertEqual(summary["transaction_count"], 3)
        self.assertEqual(summary["fee_minor"], 320)
        self.assertEqual(summary["refund_outflow_minor"], 2500)
        self.assertFalse(output["posting_performed"])
        self.assertFalse(output["revenue_recognition_performed"])
        self.assertTrue(output["founder_briefing"]["cross_currency_total_prohibited"])

    def test_exact_payout_reference_produces_high_confidence_candidate_only(self):
        output = self.services.dispatch(
            self.runtime,
            "stripe.reconcile_payouts",
            {
                "payouts": self.payouts,
                "balance_transactions": self.balance,
                "bank_transactions": [self._bank()],
            },
            entity_id="cn_dtc_company",
        )["output"]
        self.assertTrue(output["ready"])
        row = output["reconciliation"][0]
        self.assertEqual(row["balance_check"], "matched")
        self.assertEqual(row["reconciliation_status"], "high_confidence_candidate")
        self.assertEqual(row["bank_transaction_id"], "bank_001")
        self.assertTrue(row["human_confirmation_required"])
        self.assertTrue(output["candidate_only"])
        self.assertFalse(output["bank_reconciliation_completed"])
        self.assertFalse(output["posting_performed"])

    def test_unique_amount_currency_arrival_window_is_review_candidate(self):
        output = self.services.dispatch(
            self.runtime,
            "stripe.reconcile_payouts",
            {
                "payouts": self.payouts,
                "balance_transactions": self.balance,
                "bank_transactions": [self._bank(reference="processor settlement")],
                "arrival_date_tolerance_days": 3,
            },
            entity_id="cn_dtc_company",
        )["output"]
        self.assertEqual(output["reconciliation"][0]["reconciliation_status"], "review_candidate")

    def test_ambiguous_bank_rows_and_balance_mismatch_are_blocking_exceptions(self):
        bad_balances = [dict(row) for row in self.balance]
        payout_balance = next(
            row for row in bad_balances if row["balance_transaction_id"] == "txn_demo_payout_001"
        )
        payout_balance["amount_minor"] = -7000
        output = self.services.dispatch(
            self.runtime,
            "stripe.reconcile_payouts",
            {
                "payouts": self.payouts,
                "balance_transactions": bad_balances,
                "bank_transactions": [self._bank("bank_001"), self._bank("bank_002")],
            },
            entity_id="cn_dtc_company",
        )["output"]
        row = output["reconciliation"][0]
        self.assertEqual(row["balance_check"], "balance_amount_currency_or_category_mismatch")
        self.assertEqual(row["reconciliation_status"], "ambiguous_bank_candidates")
        self.assertFalse(output["ready"])
        self.assertEqual(len(output["exceptions"]), 1)
        self.assertEqual(output["currency_summary"][0]["exception_count"], 1)

    def test_duplicate_business_keys_are_visible_and_never_ready(self):
        output = self.services.dispatch(
            self.runtime,
            "stripe.reconcile_payouts",
            {
                "payouts": [self.payouts[0], self.payouts[0]],
                "balance_transactions": self.balance,
                "bank_transactions": [self._bank()],
            },
            entity_id="cn_dtc_company",
        )["output"]
        self.assertFalse(output["ready"])
        self.assertEqual(output["duplicate_inputs"]["payout_ids"], ["po_demo_001"])

    def test_service_rejects_cross_entity_rows_missing_evidence_and_major_unit_amounts(self):
        foreign = dict(self.payouts[0], entity_id="another_entity")
        with self.assertRaisesRegex(ValueError, "outside statutory entity"):
            self.services.dispatch(
                self.runtime, "stripe.reconcile_payouts",
                {"payouts": [foreign], "balance_transactions": self.balance, "bank_transactions": []},
                entity_id="cn_dtc_company",
            )
        no_evidence = dict(self.payouts[0])
        no_evidence.pop("evidence")
        with self.assertRaisesRegex(ValueError, "requires source_file and batch_id evidence"):
            self.services.dispatch(
                self.runtime, "stripe.reconcile_payouts",
                {"payouts": [no_evidence], "balance_transactions": self.balance, "bank_transactions": []},
                entity_id="cn_dtc_company",
            )
        bank = self._bank()
        bank["amount_minor"] = 71.80
        with self.assertRaisesRegex(ValueError, "smallest unit"):
            self.services.dispatch(
                self.runtime, "stripe.reconcile_payouts",
                {"payouts": self.payouts, "balance_transactions": self.balance, "bank_transactions": [bank]},
                entity_id="cn_dtc_company",
            )


if __name__ == "__main__":
    unittest.main()
