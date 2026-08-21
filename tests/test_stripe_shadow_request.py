from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from src.box_runtime import BoxRuntime
from src.stripe_shadow_request import (
    StripeShadowRequestError,
    build_stripe_shadow_request_template,
    read_private_stripe_shadow_request,
    validate_stripe_shadow_request,
    verify_private_stripe_shadow_request,
)


ROOT = Path(__file__).resolve().parents[1]
BOX = ROOT / "examples" / "boxes" / "cn_dtc_stripe_store.json"


class StripeShadowRequestTests(unittest.TestCase):
    def setUp(self):
        self.runtime = BoxRuntime(BOX, ROOT / "packs")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def _request_path(self) -> Path:
        path = Path(self.temp.name) / "private-stripe-request.json"
        summary = build_stripe_shadow_request_template(
            self.runtime,
            entity_id="cn_dtc_company",
            period="2026-08",
            output=path,
        )
        self.assertTrue(summary["template_only"])
        self.assertFalse(summary["ready_for_network_dispatch"])
        self.assertFalse(summary["financial_amounts_included"])
        self.assertEqual(summary["operator_edits_required"], ["bank_transactions"])
        return path

    def _completed_request(self, path: Path) -> dict:
        request = json.loads(path.read_text(encoding="utf-8"))
        request["payload"]["bank_transactions"] = [{
            "bank_transaction_id": "private_bank_txn_8101",
            "entity_id": "cn_dtc_company",
            "amount_minor": 7180,
            "currency": "USD",
            "direction": "inflow",
            "transaction_date": "2026-08-15",
            "reference": "Stripe private payout evidence",
            "evidence": {
                "source_file": "private-export://bank/2026-08",
                "batch_id": "approved-bank-evidence-2026-08",
            },
        }]
        path.write_text(json.dumps(request), encoding="utf-8")
        if os.name != "nt":
            path.chmod(0o600)
        return request

    def test_init_and_verify_bind_exact_month_entity_and_private_bank_evidence(self):
        path = self._request_path()
        request = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(request["pipeline_id"], "stripe.daily_close")
        for field in ("balance_request", "payout_request"):
            connector_request = request["payload"][field]
            self.assertEqual(connector_request["mode"], "fetch")
            self.assertEqual(connector_request["default_entity_id"], "cn_dtc_company")
            self.assertEqual(connector_request["created_gte"], 1785542400)
            self.assertEqual(connector_request["created_lt"], 1788220800)
        self.assertEqual(request["payload"]["bank_transactions"], [])
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

        private_request = self._completed_request(path)
        verified = verify_private_stripe_shadow_request(self.runtime, path)
        self.assertTrue(verified["valid"])
        self.assertEqual(verified["period"], "2026-08")
        self.assertEqual(verified["bank_transaction_count"], 1)
        self.assertEqual(verified["currency_count"], 1)
        serialized = json.dumps(verified, ensure_ascii=False)
        self.assertNotIn("7180", serialized)
        self.assertNotIn("private_bank_txn_8101", serialized)
        self.assertNotIn("Stripe private payout evidence", serialized)
        self.assertFalse(verified["financial_amounts_returned"])
        self.assertFalse(verified["network_access_performed"])
        self.assertEqual(
            validate_stripe_shadow_request(self.runtime, private_request), verified,
        )

    def test_incomplete_fixture_cross_window_and_secret_requests_fail_closed(self):
        path = self._request_path()
        request = json.loads(path.read_text(encoding="utf-8"))
        with self.assertRaisesRegex(StripeShadowRequestError, "bank_transactions"):
            validate_stripe_shadow_request(self.runtime, request)

        request = self._completed_request(path)
        request["payload"]["balance_request"]["mode"] = "fixture"
        with self.assertRaisesRegex(StripeShadowRequestError, "fetch mode"):
            validate_stripe_shadow_request(self.runtime, request)
        request["payload"]["balance_request"]["mode"] = "fetch"
        request["payload"]["payout_request"]["created_lt"] += 1
        with self.assertRaisesRegex(StripeShadowRequestError, "identical bounds"):
            validate_stripe_shadow_request(self.runtime, request)
        request["payload"]["payout_request"]["created_lt"] -= 1
        request["payload"]["bank_transactions"][0]["reference"] = (
            "Bearer sk_live_never_store_this"
        )
        with self.assertRaisesRegex(StripeShadowRequestError, "credentials"):
            validate_stripe_shadow_request(self.runtime, request)

    def test_bank_evidence_entity_date_direction_and_uniqueness_are_strict(self):
        path = self._request_path()
        request = self._completed_request(path)
        row = request["payload"]["bank_transactions"][0]
        row["entity_id"] = "other"
        with self.assertRaisesRegex(StripeShadowRequestError, "outside"):
            validate_stripe_shadow_request(self.runtime, request)
        row["entity_id"] = "cn_dtc_company"
        row["transaction_date"] = "2026-09-05"
        with self.assertRaisesRegex(StripeShadowRequestError, "arrival tolerance"):
            validate_stripe_shadow_request(self.runtime, request)
        row["transaction_date"] = "2026-08-15"
        row["direction"] = "outflow"
        with self.assertRaisesRegex(StripeShadowRequestError, "must be inflow"):
            validate_stripe_shadow_request(self.runtime, request)
        row["direction"] = "inflow"
        request["payload"]["bank_transactions"].append(dict(row))
        with self.assertRaisesRegex(StripeShadowRequestError, "unique"):
            validate_stripe_shadow_request(self.runtime, request)

    def test_request_must_be_mode_0600_and_exclusive(self):
        path = self._request_path()
        with self.assertRaisesRegex(StripeShadowRequestError, "already exists"):
            build_stripe_shadow_request_template(
                self.runtime,
                entity_id="cn_dtc_company",
                period="2026-08",
                output=path,
            )
        if os.name != "nt":
            path.chmod(0o644)
            with self.assertRaisesRegex(StripeShadowRequestError, "0600"):
                read_private_stripe_shadow_request(path)


if __name__ == "__main__":
    unittest.main()
