from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any, Iterable


VENDOR_BANK_DECISIONS = {"批准", "退回", "取消"}
INDEPENDENT_VERIFICATION_METHODS = {"回拨主数据联系人", "银行证明核对", "线下当面确认"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean_account(value: Any) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", str(value or "")).upper()


def _fingerprint(account: str) -> str:
    return hashlib.sha256(account.encode("utf-8")).hexdigest()


def _masked(account: str) -> str:
    if len(account) <= 4:
        return "••••"
    return f"•••• {account[-4:]}"


def _change_id(entity_id: str, vendor: str, fingerprint: str) -> str:
    seed = f"{entity_id}|{vendor.casefold()}|{fingerprint}|{_now()}"
    return f"VBANK-{hashlib.sha1(seed.encode('utf-8')).hexdigest()[:14].upper()}"


def approved_vendor_bank_accounts(
    records: Iterable[dict], *, entity_id: str = "", vendor: str = "", currency: str = "",
) -> list[dict]:
    vendor_key = str(vendor or "").strip().casefold()
    currency = str(currency or "").strip().upper()
    return [
        dict(row) for row in records
        if row.get("status") == "已批准"
        and (not entity_id or str(row.get("entity_id") or "") == entity_id)
        and (not vendor_key or str(row.get("vendor") or "").strip().casefold() == vendor_key)
        and (not currency or str(row.get("currency") or "").upper() == currency)
    ]


def public_vendor_bank_record(record: dict) -> dict:
    """Return the payment-safe view. Full account numbers are never persisted."""
    return {
        key: record.get(key) for key in (
            "id", "entity_id", "vendor", "beneficiary_name", "bank_name", "bank_country",
            "currency", "account_masked", "swift_bic", "status",
            "change_type", "requested_by", "requested_at", "review", "blockers", "security_note",
        )
    }


def create_vendor_bank_change(
    *, entity_id: str, vendor: str, beneficiary_name: str, bank_name: str,
    bank_country: str, currency: str, account_number: str, requester: str,
    evidence: Iterable[str], change_type: str = "新增", previous_account_id: str = "",
    existing_records: Iterable[dict] = (),
) -> dict:
    entity_id = str(entity_id or "").strip()
    vendor = str(vendor or "").strip()
    beneficiary_name = str(beneficiary_name or "").strip()
    bank_name = str(bank_name or "").strip()
    bank_country = str(bank_country or "").strip().upper()
    currency = str(currency or "").strip().upper()
    requester = str(requester or "").strip()
    account = _clean_account(account_number)
    if not all((entity_id, vendor, beneficiary_name, bank_name, bank_country, currency, requester)):
        raise ValueError("供应商账户申请必须填写主体、供应商、户名、银行、国家地区、币种和申请人")
    if change_type not in {"新增", "变更"}:
        raise ValueError("供应商账户申请类型只能是新增或变更")
    if len(account) < 6:
        raise ValueError("收款账号格式无效")
    if not re.fullmatch(r"[A-Z]{3}", currency):
        raise ValueError("收款账户币种必须为3位代码")
    records = list(existing_records)
    if change_type == "变更":
        previous = next((row for row in records if row.get("id") == previous_account_id), None)
        if not previous or previous.get("status") != "已批准":
            raise ValueError("变更账户必须引用同主体当前已批准的旧账户")
        if str(previous.get("entity_id") or "") != entity_id or str(previous.get("vendor") or "").casefold() != vendor.casefold():
            raise ValueError("旧账户与本次法律主体或供应商不一致")
    evidence_items = list(dict.fromkeys(str(item).strip()[:500] for item in evidence if str(item).strip()))
    blockers = []
    if len(evidence_items) < 2:
        blockers.append("至少需要供应商盖章/签字资料和独立联系方式或银行证明两类证据")
    fingerprint = _fingerprint(account)
    same_account = [row for row in records if row.get("account_fingerprint") == fingerprint and row.get("status") in {"待批准", "已批准"}]
    same_entity_vendor = any(
        str(row.get("entity_id") or "") == entity_id
        and str(row.get("vendor") or "").casefold() == vendor.casefold()
        for row in same_account
    )
    other_vendor = any(str(row.get("vendor") or "").casefold() != vendor.casefold() for row in same_account)
    if same_entity_vendor:
        blockers.append("该主体和供应商已有相同的待批或有效收款账户")
    elif other_vendor:
        blockers.append("同一收款账户被其他供应商使用，必须调查后处理")
    return {
        "id": _change_id(entity_id, vendor, fingerprint), "entity_id": entity_id,
        "vendor": vendor[:160], "beneficiary_name": beneficiary_name[:160],
        "bank_name": bank_name[:160], "bank_country": bank_country[:40], "currency": currency,
        "account_masked": _masked(account), "account_fingerprint": fingerprint,
        "swift_bic": "", "change_type": change_type, "previous_account_id": str(previous_account_id or "")[:120],
        "evidence": evidence_items, "requested_by": requester[:80], "requested_at": _now(),
        "status": "阻塞" if blockers else "待批准", "blockers": blockers, "review": None,
        "security_note": "完整账号仅用于本次指纹计算，未写入台账；付款时重新在银行端录入并按尾号复核。",
    }


def decide_vendor_bank_change(
    records: Iterable[dict], change_id: str, decision: str, reviewer: str,
    rationale: str, verification_method: str, verification_reference: str,
) -> tuple[list[dict], dict]:
    rows = [dict(row) for row in records]
    index = next((i for i, row in enumerate(rows) if row.get("id") == change_id), None)
    if index is None:
        raise ValueError("找不到供应商账户申请")
    item = rows[index]
    decision, reviewer = str(decision or "").strip(), str(reviewer or "").strip()
    rationale = str(rationale or "").strip()
    verification_method = str(verification_method or "").strip()
    verification_reference = str(verification_reference or "").strip()
    if decision not in VENDOR_BANK_DECISIONS:
        raise ValueError("供应商账户决定无效")
    if not reviewer or reviewer == str(item.get("requested_by") or ""):
        raise ValueError("供应商账户必须由申请人以外的人员独立复核")
    if len(rationale) < 8:
        raise ValueError("请填写至少8个字的复核依据")
    if decision == "批准":
        if item.get("blockers"):
            raise ValueError("供应商账户申请仍有阻塞项，不能批准")
        if verification_method not in INDEPENDENT_VERIFICATION_METHODS:
            raise ValueError("批准前必须选择独立回拨、银行证明或线下核对方式")
        if len(verification_reference) < 6:
            raise ValueError("请记录独立核验的联系人、银行证明或线下记录引用")
        for row in rows:
            if (
                row.get("status") == "已批准"
                and str(row.get("entity_id") or "") == str(item.get("entity_id") or "")
                and str(row.get("vendor") or "").casefold() == str(item.get("vendor") or "").casefold()
                and str(row.get("currency") or "") == str(item.get("currency") or "")
            ):
                row["status"] = "已停用"
                row["superseded_by"] = change_id
    item["status"] = {"批准": "已批准", "退回": "已退回", "取消": "已取消"}[decision]
    item["review"] = {
        "decision": decision, "reviewer": reviewer[:80], "rationale": rationale[:1000],
        "verification_method": verification_method, "verification_reference": verification_reference[:500],
        "timestamp": _now(),
    }
    rows[index] = item
    return rows, item
