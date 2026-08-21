from __future__ import annotations

import hashlib
import hmac
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.airwallex_webhooks import (
    AirwallexWebhookError,
    AirwallexWebhookStore,
    MAX_BODY_BYTES,
    verify_airwallex_signature,
)


NOW = datetime(2026, 8, 14, 8, 0, tzinfo=timezone.utc)
SECRET = "webhook-signing-secret"
RUNTIME_FINGERPRINT = "a" * 64
BINDINGS = json.dumps({
    "sg_store": {
        "legal_entity_id": "le_sg_demo",
        "account_id": "acct_sg_demo",
        "environment": "sandbox",
    },
    "cn_store": {
        "legal_entity_id": "le_cn_demo",
        "account_id": "acct_cn_demo",
        "environment": "production",
    },
})


def event_body(
    *, event_id: str = "evt_demo_001", expense_id: str = "exp_demo_001",
    event_name: str = "spend.expense.updated", nested: bool = False,
    legal_entity_id: str = "le_sg_demo", account_id: str = "acct_sg_demo",
    merchant: str = "Private Merchant",
) -> bytes:
    expense = {
        "id": expense_id,
        "legal_entity_id": legal_entity_id,
        "account_id": account_id,
        "merchant": merchant,
        "status": "APPROVED",
    }
    return json.dumps({
        "id": event_id,
        "name": event_name,
        "account_id": account_id,
        "data": {"object": expense} if nested else expense,
        "created_at": "2026-08-14T08:00:00Z",
        "version": "2026-07-17",
    }, separators=(",", ":")).encode()


def signed(body: bytes, *, now: datetime = NOW, secret: str = SECRET) -> tuple[str, str]:
    timestamp = str(int(now.timestamp() * 1000))
    signature = hmac.new(secret.encode(), timestamp.encode() + body, hashlib.sha256).hexdigest()
    return timestamp, signature


class AirwallexWebhookTests(unittest.TestCase):
    def _receive(self, store: AirwallexWebhookStore, body: bytes, *, now: datetime = NOW):
        timestamp, signature = signed(body, now=now)
        return store.receive(
            body,
            timestamp=timestamp,
            signature=signature,
            secret=SECRET,
            entity_bindings_json=BINDINGS,
            allowed_entity_ids={"sg_store", "cn_store"},
            runtime_fingerprint=RUNTIME_FINGERPRINT,
            now=now,
        )

    def test_signature_is_raw_body_bound_and_replay_limited(self):
        body = event_body()
        timestamp, signature = signed(body)
        self.assertEqual(
            verify_airwallex_signature(
                body, timestamp=timestamp, signature=signature, secret=SECRET, now=NOW,
            ),
            int(timestamp),
        )
        with self.assertRaisesRegex(AirwallexWebhookError, "signature") as raised:
            verify_airwallex_signature(
                body + b" ", timestamp=timestamp, signature=signature, secret=SECRET, now=NOW,
            )
        self.assertEqual(raised.exception.error_type, "invalid_webhook_signature")
        with self.assertRaisesRegex(AirwallexWebhookError, "replay") as raised:
            verify_airwallex_signature(
                body, timestamp=timestamp, signature=signature, secret=SECRET,
                now=NOW + timedelta(seconds=301),
            )
        self.assertEqual(raised.exception.error_type, "stale_webhook")
        with self.assertRaises(AirwallexWebhookError) as raised:
            verify_airwallex_signature(
                b"x" * (MAX_BODY_BYTES + 1), timestamp=timestamp,
                signature=signature, secret=SECRET, now=NOW,
            )
        self.assertEqual(raised.exception.http_status, 413)

    def test_receive_supports_direct_and_nested_data_and_exact_entity_binding(self):
        with tempfile.TemporaryDirectory() as temp:
            store = AirwallexWebhookStore(Path(temp) / "webhooks")
            direct = self._receive(store, event_body())
            nested = self._receive(store, event_body(
                event_id="evt_demo_002", expense_id="exp_demo_002", nested=True,
            ))
            self.assertEqual(direct["entity_id"], "sg_store")
            self.assertEqual(nested["entity_id"], "sg_store")
            self.assertFalse(direct["duplicate"])
            self.assertFalse(direct["raw_expense_id_included"])
            with self.assertRaises(AirwallexWebhookError) as raised:
                self._receive(store, event_body(
                    event_id="evt_wrong", expense_id="exp_wrong",
                    legal_entity_id="le_other", account_id="acct_other",
                ))
            self.assertEqual(raised.exception.error_type, "webhook_entity_binding_conflict")

    def test_duplicate_is_idempotent_and_event_id_body_conflict_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            store = AirwallexWebhookStore(Path(temp) / "webhooks")
            body = event_body()
            first = self._receive(store, body)
            duplicate = self._receive(store, body)
            self.assertEqual(first["receipt_id"], duplicate["receipt_id"])
            self.assertTrue(duplicate["duplicate"])
            self.assertEqual(store.verify()["event_count"], 1)
            conflicting = event_body(merchant="Changed Merchant")
            with self.assertRaises(AirwallexWebhookError) as raised:
                self._receive(store, conflicting)
            self.assertEqual(raised.exception.error_type, "webhook_event_conflict")

    def test_public_status_is_redacted_and_private_ledger_is_tamper_evident(self):
        with tempfile.TemporaryDirectory() as temp:
            store = AirwallexWebhookStore(Path(temp) / "webhooks")
            self._receive(store, event_body())
            public = store.status(runtime_fingerprint=RUNTIME_FINGERPRINT)
            serialized = json.dumps(public)
            self.assertNotIn("exp_demo_001", serialized)
            self.assertNotIn("Private Merchant", serialized)
            self.assertFalse(public["raw_expense_ids_included"])
            self.assertEqual(oct(store.root.stat().st_mode & 0o777), "0o700")
            self.assertEqual(oct(store.events_file.stat().st_mode & 0o777), "0o600")
            self.assertTrue(store.verify()["valid"])
            body = store.events_file.read_text(encoding="utf-8")
            store.events_file.write_text(body.replace("spend.expense.updated", "spend.expense.approved"), encoding="utf-8")
            with self.assertRaises(AirwallexWebhookError) as raised:
                store.verify()
            self.assertEqual(raised.exception.error_type, "webhook_ledger_invalid")

    def test_claim_outcomes_are_durable_and_three_failures_quarantine(self):
        with tempfile.TemporaryDirectory() as temp:
            store = AirwallexWebhookStore(Path(temp) / "webhooks")
            self._receive(store, event_body())
            claim = store.claim_next(runtime_fingerprint=RUNTIME_FINGERPRINT, actor="worker", now=NOW)
            self.assertEqual(claim["raw_expense_id"], "exp_demo_001")
            success = store.record_success(claim, actor="worker", result_summary={
                "ready": True, "record_count": 1, "state_change_count": 0,
                "network_access_performed": True, "external_actions_performed": False,
            })
            self.assertEqual(success["status"], "succeeded")
            self.assertIsNone(store.claim_next(runtime_fingerprint=RUNTIME_FINGERPRINT, actor="worker"))

            self._receive(store, event_body(event_id="evt_demo_002", expense_id="exp_demo_002"))
            outcome = None
            for _ in range(3):
                claim = store.claim_next(runtime_fingerprint=RUNTIME_FINGERPRINT, actor="worker")
                outcome = store.record_failure(claim, "provider failed", actor="worker")
            self.assertEqual(outcome["status"], "quarantined")
            self.assertEqual(store.status(runtime_fingerprint=RUNTIME_FINGERPRINT)["counts"]["quarantined"], 1)
            resolved = store.resolve_quarantine(
                outcome["receipt_id"], runtime_fingerprint=RUNTIME_FINGERPRINT,
                actor="reviewer", resolution="retry",
                rationale="Read-only binding was corrected and reviewed",
                evidence_references=["review://airwallex/evt_demo_002"],
            )
            self.assertEqual(resolved["status"], "pending")
            self.assertEqual(resolved["attempt_count"], 0)
            retried = store.claim_next(runtime_fingerprint=RUNTIME_FINGERPRINT, actor="worker")
            succeeded = store.record_success(retried, actor="worker", result_summary={
                "ready": False, "record_count": 0, "state_change_count": 1,
                "network_access_performed": True, "external_actions_performed": False,
            })
            self.assertEqual(succeeded["status"], "succeeded")

    def test_invalid_signature_precedes_json_parsing(self):
        with tempfile.TemporaryDirectory() as temp:
            store = AirwallexWebhookStore(Path(temp) / "webhooks")
            with self.assertRaises(AirwallexWebhookError) as raised:
                store.receive(
                    b"not-json", timestamp=str(int(NOW.timestamp() * 1000)), signature="0" * 64,
                    secret=SECRET, entity_bindings_json=BINDINGS,
                    allowed_entity_ids={"sg_store"}, runtime_fingerprint=RUNTIME_FINGERPRINT, now=NOW,
                )
            self.assertEqual(raised.exception.error_type, "invalid_webhook_signature")


if __name__ == "__main__":
    unittest.main()
