from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DATASETS = {
    "settlements", "purchases", "procurement_requests", "purchase_deliveries", "vendor_bank_changes", "bank_transactions", "invoices", "payroll_rows", "plan_lines",
    "opening_balances", "master_records", "game_kpis", "cash_allocations",
    "payment_requests", "expense_claims", "collection_actions",
    "asset_cards", "accruals", "posted_vouchers",
    "game_revenue_policies",
    "settlement_candidates",
    "ledger_adapter_reviews",
    "bank_reconciliation_reviews",
    "tax_filing_reviews",
    "shadow_close_baselines", "shadow_close_reviews",
    "onboarding_declarations",
}
PERIOD_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
ENTITY_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,79}$")


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _atomic_write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(payload, ensure_ascii=False, indent=2)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(serialized)
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


class LedgerStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.datasets = self.root / "datasets"
        self.periods = self.root / "periods"
        self.audit_file = self.root / "audit_log.jsonl"
        self._lock = threading.RLock()

    @staticmethod
    def _validate_period(period: str) -> str:
        period = str(period or "").strip()
        if not PERIOD_PATTERN.fullmatch(period):
            raise ValueError("账期必须为 YYYY-MM")
        return period

    @staticmethod
    def _validate_entity_id(entity_id: str | None) -> str:
        value = str(entity_id or "").strip()
        if value and not ENTITY_PATTERN.fullmatch(value):
            raise ValueError("法律主体编号无效")
        return value

    def _period_path(self, period: str, entity_id: str | None = None) -> Path:
        entity_id = self._validate_entity_id(entity_id)
        return (
            self.periods / entity_id / f"{period}.json"
            if entity_id else self.periods / f"{period}.json"
        )

    def load_dataset(self, name: str) -> list[dict]:
        if name not in DATASETS:
            raise ValueError(f"未知数据集：{name}")
        payload = _read_json(self.datasets / f"{name}.json", {"records": []})
        return payload.get("records") or []

    def save_dataset(self, name: str, records: list[dict], actor: str = "Agent", source: str = "界面导入") -> dict:
        if name not in DATASETS:
            raise ValueError(f"未知数据集：{name}")
        if not isinstance(records, list) or any(not isinstance(record, dict) for record in records):
            raise ValueError("数据集必须是标准记录列表")
        now = datetime.now(timezone.utc).isoformat()
        payload = {
            "dataset": name, "records": records, "record_count": len(records),
            "updated_at": now, "source": source,
        }
        entity_ids = sorted({str(record.get("entity_id")) for record in records if record.get("entity_id")})
        with self._lock:
            _atomic_write(self.datasets / f"{name}.json", payload)
            self.append_audit(actor, "SAVE_DATASET", name, {
                "record_count": len(records), "source": source, "entity_ids": entity_ids,
            })
        return payload

    def upsert_dataset(self, name: str, records: list[dict], actor: str = "Agent", source: str = "界面导入") -> dict:
        """按稳定 id 合并导入，避免分批上传时覆盖既有台账。

        没有 id 的记录使用内容指纹；同一条业务记录再次导入时以新版替换。
        """
        if name not in DATASETS:
            raise ValueError(f"未知数据集：{name}")
        if not isinstance(records, list) or any(not isinstance(record, dict) for record in records):
            raise ValueError("数据集必须是标准记录列表")

        def identity(record: dict) -> str:
            if record.get("id"):
                return f"{record.get('entity_id') or ''}:{record['id']}"
            canonical = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            return hashlib.sha1(canonical.encode("utf-8")).hexdigest()

        with self._lock:
            merged = {identity(record): record for record in self.load_dataset(name)}
            for record in records:
                merged[identity(record)] = record
            return self.save_dataset(name, list(merged.values()), actor, source)

    def load_all(self) -> dict[str, list[dict]]:
        return {name: self.load_dataset(name) for name in sorted(DATASETS)}

    def load_period(self, period: str, entity_id: str | None = None) -> dict:
        period = self._validate_period(period)
        entity_id = self._validate_entity_id(entity_id)
        return _read_json(self._period_path(period, entity_id), {
            "period": period, "entity_id": entity_id, "status": "开放",
            "decisions": [], "voucher_reviews": {}, "close_events": [],
        })

    def save_period(
        self, period: str, payload: dict, actor: str = "Agent", entity_id: str | None = None,
    ) -> dict:
        period = self._validate_period(period)
        if not isinstance(payload, dict):
            raise ValueError("期间状态必须是对象")
        entity_id = self._validate_entity_id(entity_id or payload.get("entity_id"))
        with self._lock:
            current = self.load_period(period, entity_id)
            current.update(payload)
            current["period"] = period
            current["entity_id"] = entity_id
            current["updated_at"] = datetime.now(timezone.utc).isoformat()
            _atomic_write(self._period_path(period, entity_id), current)
            self.append_audit(actor, "SAVE_PERIOD", f"{entity_id}:{period}" if entity_id else period, {
                "entity_id": entity_id, "period": period, "status": current.get("status"),
            })
        return current

    def record_review(
        self, period: str, voucher_id: str, decision: str, actor: str,
        rationale: str = "", evidence: list[str] | None = None, entity_id: str | None = None,
    ) -> dict:
        if decision not in {"接受", "退回", "忽略", "冲销"}:
            raise ValueError("复核决定无效")
        period = self._validate_period(period)
        entity_id = self._validate_entity_id(entity_id)
        state = self.load_period(period, entity_id)
        review = {
            "voucher_id": voucher_id, "entity_id": entity_id, "period": period,
            "decision": decision, "actor": actor,
            "rationale": rationale, "evidence": evidence or [],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        state.setdefault("voucher_reviews", {})[voucher_id] = review
        self.save_period(period, state, actor, entity_id)
        self.append_audit(actor, "VOUCHER_REVIEW", voucher_id, review)
        return review

    def append_audit(self, actor: str, action: str, target: str, detail: dict | None = None) -> dict:
        event = {
            "id": hashlib.sha1(
                f"{datetime.now(timezone.utc).isoformat()}|{actor}|{action}|{target}".encode("utf-8")
            ).hexdigest()[:16],
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "actor": actor, "action": action, "target": target, "detail": detail or {},
        }
        self.root.mkdir(parents=True, exist_ok=True)
        with self._lock:
            with self.audit_file.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        return event

    def audit_events(self, limit: int = 200) -> list[dict]:
        if not self.audit_file.exists():
            return []
        lines = self.audit_file.read_text(encoding="utf-8").splitlines()
        events = []
        for line in lines[-max(1, min(limit, 1000)):]:
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return list(reversed(events))
