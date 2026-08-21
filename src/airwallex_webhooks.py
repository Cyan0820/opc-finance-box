from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - supported production targets are POSIX
    fcntl = None


WEBHOOK_SECRET_ENV = "OPC_AIRWALLEX_WEBHOOK_SECRET"
ENTITY_BINDINGS_ENV = "OPC_AIRWALLEX_ENTITY_BINDINGS_JSON"
MAX_BODY_BYTES = 1024 * 1024
MAX_LEDGER_BYTES = 64 * 1024 * 1024
MAX_EVENT_BYTES = 64 * 1024
MAX_EVENTS = 100_000
REPLAY_TOLERANCE_SECONDS = 300
PROCESSING_LEASE_SECONDS = 300
MAX_PROCESS_ATTEMPTS = 3
EXPENSE_EVENT_NAMES = frozenset({
    "spend.expense.draft",
    "spend.expense.awaiting_approval",
    "spend.expense.updated",
    "spend.expense.rejected",
    "spend.expense.approved",
    "spend.expense.archived",
    "spend.expense.deleted",
})
IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,199}$")
HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
VERSION_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{0,40}$")
RECEIPT_PATTERN = re.compile(r"^[0-9a-f]{24}$")
ACTOR_PATTERN = re.compile(r"^[^\x00-\x1f\x7f]{1,80}$")


class AirwallexWebhookError(RuntimeError):
    """Raised when a webhook cannot be authenticated, bound, or durably queued."""

    def __init__(self, message: str, *, error_type: str, http_status: int):
        super().__init__(message)
        self.error_type = error_type
        self.http_status = http_status


def _error(message: str, error_type: str, status: int) -> AirwallexWebhookError:
    return AirwallexWebhookError(message, error_type=error_type, http_status=status)


def _canonical(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise _error("webhook ledger metadata is not JSON serializable", "webhook_store_invalid", 503) from exc


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _raw_hash(value: str | bytes) -> str:
    body = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(body).hexdigest()


def _actor(value: Any) -> str:
    text = str(value or "").strip()
    if not ACTOR_PATTERN.fullmatch(text):
        raise _error("webhook actor must be 1-80 printable characters", "invalid_webhook_actor", 400)
    return text


def _utc(value: datetime | None = None) -> datetime:
    return (value or datetime.now(timezone.utc)).astimezone(timezone.utc)


def _render_time(value: datetime) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")


def _parse_time(value: Any, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError as exc:
        raise _error(f"Airwallex {field} must be ISO-8601", "invalid_webhook_payload", 400) from exc
    if parsed.tzinfo is None:
        raise _error(f"Airwallex {field} must include timezone", "invalid_webhook_payload", 400)
    return parsed.astimezone(timezone.utc)


def _identifier(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not IDENTIFIER_PATTERN.fullmatch(text):
        raise _error(f"Airwallex {field} is invalid", "invalid_webhook_payload", 400)
    return text


def verify_airwallex_signature(
    raw_body: bytes,
    *,
    timestamp: str,
    signature: str,
    secret: str,
    now: datetime | None = None,
) -> int:
    """Verify the exact raw-body signature before any JSON parsing."""
    if not secret:
        raise _error("Airwallex webhook secret is not configured", "webhook_authentication_misconfigured", 503)
    if len(raw_body) > MAX_BODY_BYTES:
        raise _error("Airwallex webhook body exceeds 1 MiB", "webhook_payload_too_large", 413)
    if not re.fullmatch(r"[0-9]{10,16}", str(timestamp or "")):
        raise _error("Airwallex webhook timestamp is invalid", "invalid_webhook_signature", 401)
    if not re.fullmatch(r"[0-9a-fA-F]{64}", str(signature or "")):
        raise _error("Airwallex webhook signature is invalid", "invalid_webhook_signature", 401)
    expected = hmac.new(
        secret.encode("utf-8"), str(timestamp).encode("ascii") + raw_body, hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, str(signature).lower()):
        raise _error("Airwallex webhook signature is invalid", "invalid_webhook_signature", 401)
    timestamp_ms = int(timestamp)
    clock_ms = int(_utc(now).timestamp() * 1000)
    if abs(clock_ms - timestamp_ms) > REPLAY_TOLERANCE_SECONDS * 1000:
        raise _error("Airwallex webhook timestamp is outside the replay window", "stale_webhook", 400)
    return timestamp_ms


def _binding_map(raw: str, allowed_entity_ids: set[str]) -> dict[str, dict[str, str]]:
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise _error("Airwallex entity bindings are invalid", "webhook_binding_misconfigured", 503) from exc
    if not isinstance(payload, dict):
        raise _error("Airwallex entity bindings are invalid", "webhook_binding_misconfigured", 503)
    result: dict[str, dict[str, str]] = {}
    for entity_id, binding in payload.items():
        if entity_id not in allowed_entity_ids or not isinstance(binding, dict):
            continue
        try:
            legal_entity_id = _identifier(binding.get("legal_entity_id"), "legal_entity_id")
            account_id = _identifier(binding.get("account_id"), "account_id")
        except AirwallexWebhookError as exc:
            raise _error("Airwallex entity bindings are invalid", "webhook_binding_misconfigured", 503) from exc
        result[entity_id] = {
            "legal_entity_id": legal_entity_id,
            "account_id": account_id,
        }
    return result


def parse_airwallex_expense_event(
    raw_body: bytes,
    *,
    entity_bindings_json: str,
    allowed_entity_ids: set[str],
) -> dict[str, Any]:
    try:
        event = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _error("Airwallex webhook body is invalid JSON", "invalid_webhook_payload", 400) from exc
    if not isinstance(event, dict):
        raise _error("Airwallex webhook event must be an object", "invalid_webhook_payload", 400)
    event_id = _identifier(event.get("id"), "event id")
    event_name = str(event.get("name") or "")
    if event_name not in EXPENSE_EVENT_NAMES:
        raise _error("Airwallex webhook event is not a supported Spend expense event", "unsupported_webhook_event", 400)
    data = event.get("data")
    if isinstance(data, dict) and isinstance(data.get("object"), dict):
        expense = data["object"]
    elif isinstance(data, dict):
        expense = data
    else:
        raise _error("Airwallex webhook expense data is missing", "invalid_webhook_payload", 400)
    expense_id = _identifier(expense.get("id"), "expense id")
    legal_entity_id = _identifier(expense.get("legal_entity_id"), "legal_entity_id")
    account_id = _identifier(expense.get("account_id") or event.get("account_id"), "account_id")
    matches = [
        entity_id for entity_id, binding in _binding_map(
            entity_bindings_json, allowed_entity_ids,
        ).items()
        if binding["legal_entity_id"] == legal_entity_id and binding["account_id"] == account_id
    ]
    if len(matches) != 1:
        raise _error(
            "Airwallex webhook does not match exactly one configured Box entity",
            "webhook_entity_binding_conflict",
            409,
        )
    created_at = _render_time(_parse_time(event.get("created_at"), "event created_at"))
    version = str(event.get("version") or "")
    if not VERSION_PATTERN.fullmatch(version):
        raise _error("Airwallex event version is invalid", "invalid_webhook_payload", 400)
    return {
        "event_id_sha256": _raw_hash(event_id),
        "expense_id_sha256": _raw_hash(expense_id),
        "raw_expense_id": expense_id,
        "entity_id": matches[0],
        "event_name": event_name,
        "event_created_at": created_at,
        "event_version": version,
        "legal_entity_binding_sha256": _raw_hash(legal_entity_id),
        "account_binding_sha256": _raw_hash(account_id),
    }


class AirwallexWebhookStore:
    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()
        self.events_file = self.root / "airwallex_webhook_events.jsonl"
        self.lock_file = self.root / ".airwallex_webhook.lock"
        self._lock = threading.RLock()

    def _locked(self):
        if fcntl is None:
            raise _error("webhook store requires POSIX file locking", "webhook_store_unavailable", 503)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.root.is_symlink() or not self.root.is_dir():
            raise _error("webhook store root must be a real directory", "webhook_store_unavailable", 503)
        os.chmod(self.root, 0o700)
        handle = self.lock_file.open("a+b")
        os.chmod(self.lock_file, 0o600)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        return handle

    def _events_unlocked(self) -> list[dict[str, Any]]:
        if not self.events_file.exists():
            return []
        if self.events_file.is_symlink() or not self.events_file.is_file():
            raise _error("webhook ledger must be a regular file", "webhook_ledger_invalid", 503)
        os.chmod(self.events_file, 0o600)
        if self.events_file.stat().st_size > MAX_LEDGER_BYTES:
            raise _error("webhook ledger exceeds 64 MiB", "webhook_ledger_invalid", 503)
        events: list[dict[str, Any]] = []
        previous = "GENESIS"
        with self.events_file.open("rb") as handle:
            for sequence, raw in enumerate(handle, 1):
                if sequence > MAX_EVENTS or len(raw) > MAX_EVENT_BYTES:
                    raise _error("webhook ledger limits are exceeded", "webhook_ledger_invalid", 503)
                try:
                    event = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise _error("webhook ledger contains invalid JSON", "webhook_ledger_invalid", 503) from exc
                if not isinstance(event, dict) or event.get("schema_version") != 1:
                    raise _error("webhook ledger contains an unsupported event", "webhook_ledger_invalid", 503)
                supplied = event.get("event_hash")
                unsigned = {key: value for key, value in event.items() if key != "event_hash"}
                if (
                    event.get("sequence") != sequence
                    or event.get("previous_event_hash") != previous
                    or not HASH_PATTERN.fullmatch(str(supplied or ""))
                    or supplied != _hash(unsigned)
                ):
                    raise _error("webhook ledger hash chain is invalid", "webhook_ledger_invalid", 503)
                previous = supplied
                events.append(event)
        return events

    def _append_unlocked(self, event_type: str, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        events = self._events_unlocked()
        event = {
            "schema_version": 1,
            "sequence": len(events) + 1,
            "event_type": event_type,
            "recorded_at": _render_time(_utc()),
            "actor": _actor(actor),
            "payload": payload,
            "previous_event_hash": events[-1]["event_hash"] if events else "GENESIS",
        }
        event["event_hash"] = _hash(event)
        encoded = (_canonical(event) + "\n").encode("utf-8")
        if len(encoded) > MAX_EVENT_BYTES:
            raise _error("webhook ledger event exceeds 64 KiB", "webhook_store_invalid", 503)
        with self.events_file.open("ab") as handle:
            os.chmod(self.events_file, 0o600)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        directory = os.open(self.root, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return event

    @staticmethod
    def _project(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        receipts: dict[str, dict[str, Any]] = {}
        for event in events:
            payload = event.get("payload") or {}
            event_type = event.get("event_type")
            if event_type == "AIRWALLEX_WEBHOOK_RECEIVED":
                receipt = payload.get("receipt")
                receipt_id = str((receipt or {}).get("receipt_id") or "")
                if (
                    not isinstance(receipt, dict) or not RECEIPT_PATTERN.fullmatch(receipt_id)
                    or receipt_id in receipts
                ):
                    raise _error("webhook receipt event is invalid", "webhook_ledger_invalid", 503)
                receipts[receipt_id] = {
                    **receipt,
                    "received_at": event["recorded_at"],
                    "status": "pending",
                    "attempt_count": 0,
                    "claim": None,
                    "failure_summary": None,
                }
            elif event_type == "AIRWALLEX_WEBHOOK_PROCESSING_CLAIMED":
                receipt_id = str(payload.get("receipt_id") or "")
                receipt = receipts.get(receipt_id)
                if receipt is None or receipt["status"] in {"succeeded", "quarantined"}:
                    raise _error("webhook processing claim is invalid", "webhook_ledger_invalid", 503)
                receipt["status"] = "processing"
                receipt["attempt_count"] += 1
                receipt["claim"] = {
                    "claim_id": payload.get("claim_id"),
                    "lease_expires_at": payload.get("lease_expires_at"),
                }
            elif event_type in {"AIRWALLEX_WEBHOOK_PROCESSING_SUCCEEDED", "AIRWALLEX_WEBHOOK_PROCESSING_FAILED"}:
                receipt_id = str(payload.get("receipt_id") or "")
                receipt = receipts.get(receipt_id)
                claim = (receipt or {}).get("claim")
                if (
                    receipt is None or receipt.get("status") != "processing" or not claim
                    or payload.get("claim_id") != claim.get("claim_id")
                ):
                    raise _error("webhook processing outcome is invalid", "webhook_ledger_invalid", 503)
                receipt["claim"] = None
                if event_type.endswith("SUCCEEDED"):
                    receipt["status"] = "succeeded"
                    receipt["result_summary"] = payload.get("result_summary") or {}
                    receipt["failure_summary"] = None
                else:
                    receipt["status"] = "quarantined" if payload.get("quarantined") else "pending"
                    receipt["failure_summary"] = str(payload.get("failure_summary") or "processing failed")
            elif event_type == "AIRWALLEX_WEBHOOK_QUARANTINE_RESOLVED":
                receipt_id = str(payload.get("receipt_id") or "")
                receipt = receipts.get(receipt_id)
                resolution = payload.get("resolution")
                if receipt is None or receipt.get("status") != "quarantined" or resolution not in {"retry", "dismissed"}:
                    raise _error("webhook quarantine resolution is invalid", "webhook_ledger_invalid", 503)
                receipt["status"] = "pending" if resolution == "retry" else "dismissed"
                receipt["attempt_count"] = 0 if resolution == "retry" else receipt["attempt_count"]
                receipt["failure_summary"] = None if resolution == "retry" else receipt["failure_summary"]
                receipt["resolution"] = {
                    "resolution": resolution,
                    "rationale": payload.get("rationale"),
                    "evidence_references": payload.get("evidence_references") or [],
                    "resolved_at": event["recorded_at"],
                }
            else:
                raise _error("webhook ledger contains an unknown event type", "webhook_ledger_invalid", 503)
        return receipts

    def receive(
        self,
        raw_body: bytes,
        *,
        timestamp: str,
        signature: str,
        secret: str,
        entity_bindings_json: str,
        allowed_entity_ids: set[str],
        runtime_fingerprint: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        timestamp_ms = verify_airwallex_signature(
            raw_body, timestamp=timestamp, signature=signature, secret=secret, now=now,
        )
        parsed = parse_airwallex_expense_event(
            raw_body,
            entity_bindings_json=entity_bindings_json,
            allowed_entity_ids=allowed_entity_ids,
        )
        if not HASH_PATTERN.fullmatch(str(runtime_fingerprint or "")):
            raise _error("Box runtime fingerprint is invalid", "webhook_runtime_invalid", 503)
        body_sha256 = _raw_hash(raw_body)
        receipt_id = _hash({
            "event_id_sha256": parsed["event_id_sha256"],
            "body_sha256": body_sha256,
        })[:24]
        receipt = {
            "receipt_id": receipt_id,
            **parsed,
            "body_sha256": body_sha256,
            "signature_timestamp_ms": timestamp_ms,
            "runtime_fingerprint": str(runtime_fingerprint),
            "raw_body_stored": False,
            "candidate_only": True,
            "external_actions_performed": False,
        }
        with self._lock:
            handle = self._locked()
            try:
                events = self._events_unlocked()
                receipts = self._project(events)
                same_event = [
                    item for item in receipts.values()
                    if item["event_id_sha256"] == parsed["event_id_sha256"]
                ]
                if same_event:
                    existing = same_event[0]
                    if existing["body_sha256"] != body_sha256:
                        raise _error(
                            "Airwallex event id was reused with a different body",
                            "webhook_event_conflict",
                            409,
                        )
                    return self._public_receipt(existing, duplicate=True)
                self._append_unlocked("AIRWALLEX_WEBHOOK_RECEIVED", {"receipt": receipt}, "airwallex-webhook")
                stored = self._project(self._events_unlocked())[receipt_id]
                return self._public_receipt(stored, duplicate=False)
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                handle.close()

    @staticmethod
    def _public_receipt(receipt: dict[str, Any], *, duplicate: bool | None = None) -> dict[str, Any]:
        result = {
            key: receipt.get(key) for key in (
                "receipt_id", "entity_id", "event_name", "event_created_at",
                "event_version", "event_id_sha256", "expense_id_sha256", "body_sha256",
                "runtime_fingerprint", "received_at", "status", "attempt_count",
                "failure_summary", "result_summary",
                "resolution",
            ) if key in receipt
        }
        result.update({
            "raw_event_id_included": False,
            "raw_expense_id_included": False,
            "raw_body_included": False,
            "secret_values_included": False,
            "external_actions_performed": False,
        })
        if duplicate is not None:
            result["duplicate"] = duplicate
            result["durably_received"] = True
        return result

    def claim_next(
        self,
        *,
        runtime_fingerprint: str,
        actor: str,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        clock = _utc(now)
        with self._lock:
            handle = self._locked()
            try:
                receipts = self._project(self._events_unlocked())
                eligible = []
                for receipt in receipts.values():
                    if receipt["runtime_fingerprint"] != runtime_fingerprint:
                        continue
                    if receipt["status"] == "pending":
                        eligible.append(receipt)
                    elif receipt["status"] == "processing" and receipt.get("claim"):
                        if _parse_time(receipt["claim"]["lease_expires_at"], "lease_expires_at") <= clock:
                            eligible.append(receipt)
                if not eligible:
                    return None
                selected = sorted(eligible, key=lambda item: (item["received_at"], item["receipt_id"]))[0]
                if selected["attempt_count"] >= MAX_PROCESS_ATTEMPTS:
                    raise _error("webhook retry state is invalid", "webhook_ledger_invalid", 503)
                claim = {
                    "receipt_id": selected["receipt_id"],
                    "claim_id": uuid.uuid4().hex[:24],
                    "lease_expires_at": _render_time(clock + timedelta(seconds=PROCESSING_LEASE_SECONDS)),
                }
                self._append_unlocked("AIRWALLEX_WEBHOOK_PROCESSING_CLAIMED", claim, actor)
                return {
                    **self._public_receipt(selected),
                    **claim,
                    "raw_expense_id": selected["raw_expense_id"],
                    "private_processing_claim": True,
                }
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                handle.close()

    def record_success(self, claim: dict[str, Any], *, actor: str, result_summary: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "ready", "blocked_at", "record_count", "state_change_count",
            "network_access_performed", "external_actions_performed",
        }
        summary = {key: result_summary[key] for key in allowed if key in result_summary}
        if summary.get("external_actions_performed") is not False:
            raise _error("webhook worker result must prove no external actions", "unsafe_webhook_result", 400)
        return self._record_outcome(claim, actor=actor, succeeded=True, detail=summary)

    def record_failure(self, claim: dict[str, Any], error: Any, *, actor: str) -> dict[str, Any]:
        text = " ".join(str(error or "processing failed").split())[:240]
        lowered = text.lower()
        if any(marker in lowered for marker in ("bearer ", "api_key", "secret", "token", "http://", "https://")):
            text = "webhook refetch failed; sensitive error detail was suppressed"
        return self._record_outcome(claim, actor=actor, succeeded=False, detail=text)

    def _record_outcome(
        self, claim: dict[str, Any], *, actor: str, succeeded: bool, detail: Any,
    ) -> dict[str, Any]:
        receipt_id = str(claim.get("receipt_id") or "")
        claim_id = str(claim.get("claim_id") or "")
        with self._lock:
            handle = self._locked()
            try:
                receipts = self._project(self._events_unlocked())
                receipt = receipts.get(receipt_id)
                active = (receipt or {}).get("claim")
                if receipt is None or receipt.get("status") != "processing" or not active or active.get("claim_id") != claim_id:
                    raise _error("webhook processing claim is no longer active", "stale_webhook_claim", 409)
                payload: dict[str, Any] = {"receipt_id": receipt_id, "claim_id": claim_id}
                if succeeded:
                    payload["result_summary"] = detail
                    event_type = "AIRWALLEX_WEBHOOK_PROCESSING_SUCCEEDED"
                else:
                    payload["failure_summary"] = detail
                    payload["quarantined"] = receipt["attempt_count"] >= MAX_PROCESS_ATTEMPTS
                    event_type = "AIRWALLEX_WEBHOOK_PROCESSING_FAILED"
                self._append_unlocked(event_type, payload, actor)
                updated = self._project(self._events_unlocked())[receipt_id]
                return self._public_receipt(updated)
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                handle.close()

    def status(self, *, runtime_fingerprint: str | None = None, limit: int = 100) -> dict[str, Any]:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 500:
            raise _error("webhook status limit must be 1-500", "invalid_webhook_status_query", 400)
        with self._lock:
            handle = self._locked()
            try:
                receipts = list(self._project(self._events_unlocked()).values())
                if runtime_fingerprint is not None:
                    receipts = [item for item in receipts if item["runtime_fingerprint"] == runtime_fingerprint]
                public = [self._public_receipt(item) for item in receipts]
                public.sort(key=lambda item: (str(item.get("received_at")), str(item.get("receipt_id"))), reverse=True)
                counts = {
                    state: sum(item["status"] == state for item in receipts)
                    for state in ("pending", "processing", "succeeded", "quarantined", "dismissed")
                }
                return {
                    "schema_version": 1,
                    "receipts": public[:limit],
                    "counts": {"total": len(receipts), **counts},
                    "list_limit": limit,
                    "counts_may_be_truncated": len(receipts) > limit,
                    "raw_event_ids_included": False,
                    "raw_expense_ids_included": False,
                    "raw_bodies_included": False,
                    "secret_values_included": False,
                    "external_actions_performed": False,
                }
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                handle.close()

    def resolve_quarantine(
        self,
        receipt_id: str,
        *,
        runtime_fingerprint: str,
        actor: str,
        resolution: str,
        rationale: str,
        evidence_references: list[str],
    ) -> dict[str, Any]:
        if not RECEIPT_PATTERN.fullmatch(str(receipt_id or "")):
            raise _error("webhook receipt_id is invalid", "invalid_webhook_resolution", 400)
        if resolution not in {"retry", "dismissed"}:
            raise _error("webhook quarantine resolution must be retry or dismissed", "invalid_webhook_resolution", 400)
        rationale = str(rationale or "").strip()
        evidence = [str(item or "").strip() for item in evidence_references]
        if not rationale or len(rationale) > 500:
            raise _error("webhook resolution rationale must be 1-500 characters", "invalid_webhook_resolution", 400)
        if not evidence or any(not item or len(item) > 240 for item in evidence):
            raise _error("webhook resolution requires bounded evidence references", "invalid_webhook_resolution", 400)
        with self._lock:
            handle = self._locked()
            try:
                receipts = self._project(self._events_unlocked())
                receipt = receipts.get(receipt_id)
                if receipt is None or receipt.get("status") != "quarantined":
                    raise _error("webhook receipt is not quarantined", "invalid_webhook_resolution", 409)
                if receipt.get("runtime_fingerprint") != runtime_fingerprint:
                    raise _error("webhook receipt belongs to a different Box fingerprint", "invalid_webhook_resolution", 409)
                payload = {
                    "receipt_id": receipt_id,
                    "resolution": resolution,
                    "rationale": rationale,
                    "evidence_references": evidence,
                }
                self._append_unlocked("AIRWALLEX_WEBHOOK_QUARANTINE_RESOLVED", payload, actor)
                return self._public_receipt(self._project(self._events_unlocked())[receipt_id])
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                handle.close()

    def verify(self) -> dict[str, Any]:
        with self._lock:
            handle = self._locked()
            try:
                events = self._events_unlocked()
                receipts = self._project(events)
                return {
                    "valid": True,
                    "integrity": "sha256_hash_chain",
                    "integrity_limit": "tamper_evident_not_immutable",
                    "event_count": len(events),
                    "receipt_count": len(receipts),
                    "chain_head": events[-1]["event_hash"] if events else "GENESIS",
                    "raw_body_stored": False,
                    "private_expense_ids_stored": bool(receipts),
                    "private_file_mode": "0600",
                    "external_actions_performed": False,
                }
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                handle.close()
