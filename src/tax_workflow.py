from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Iterable

from .tax_filing_assist import form_fingerprint, select_form_contract


REVIEW_DECISIONS = {"同意草稿", "退回修改", "确认不适用"}
SUBMISSION_STATUSES = {"未提交", "已进入申报端", "已提交待回执", "申报成功", "申报失败", "已缴款", "无需缴款"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _id(period: str, form_code: str, entity_id: str = "") -> str:
    return f"TAXREV-{hashlib.sha1(f'{entity_id}|{period}|{form_code}'.encode()).hexdigest()[:14].upper()}"


def review_tax_form(
    workspace: dict, existing_reviews: Iterable[dict], form_code: str,
    decision: str, actor: str, rationale: str, evidence: Iterable[str] = (),
) -> dict:
    if decision not in REVIEW_DECISIONS:
        raise ValueError("税务复核决定无效")
    actor, rationale = str(actor or "").strip(), str(rationale or "").strip()
    if not actor or len(rationale) < 6:
        raise ValueError("请填写复核人和至少6个字的税务判断依据")
    form = next((item for item in workspace.get("returns") or [] if item.get("form_code") == form_code), None)
    if not form:
        raise ValueError("当前税务工作底稿不存在该申报表")
    contract_id, _, contract_blockers = select_form_contract(form, workspace)
    if decision == "同意草稿" and (form.get("blockers") or contract_blockers):
        raise ValueError("该申报表仍有资料或口径阻塞项，不能同意草稿")
    if decision == "确认不适用" and form.get("status") != "不适用":
        raise ValueError("系统尚未判断该表不适用；请说明并退回修改适用性配置")
    evidence_items = [str(item).strip()[:500] for item in evidence if str(item).strip()]
    entity_id = str(workspace.get("entity_id") or "")
    fingerprint = form_fingerprint(form, contract_id)
    review_id = _id(workspace["period"], form_code, entity_id)
    previous = next((item for item in existing_reviews if item.get("id") == review_id), None)
    history = list((previous or {}).get("history") or [])
    event = {
        "decision": decision, "actor": actor[:80], "rationale": rationale[:1200],
        "evidence": evidence_items, "timestamp": _now(),
        "form_version": form.get("version"), "official_source": form.get("official_source"),
        "entity_id": entity_id, "form_fingerprint": fingerprint, "contract_id": contract_id,
    }
    history.append(event)
    return {
        "id": review_id, "entity_id": entity_id, "period": workspace["period"], "form_code": form_code,
        "form_name": form.get("name"), "form_version": form.get("version"),
        "form_fingerprint": fingerprint, "contract_id": contract_id,
        "status": "已复核" if decision in {"同意草稿", "确认不适用"} else "已退回",
        "latest_review": event, "history": history,
        "submission": (previous or {}).get("submission") or {"status": "未提交", "history": []},
    }


def record_tax_submission(
    review: dict, status: str, actor: str, reference: str, evidence: Iterable[str] = (),
    note: str = "",
) -> dict:
    if status not in SUBMISSION_STATUSES:
        raise ValueError("申报状态无效")
    actor, reference = str(actor or "").strip(), str(reference or "").strip()
    evidence = [str(item).strip()[:500] for item in evidence if str(item).strip()]
    if not actor:
        raise ValueError("请填写申报操作人")
    if status in {"已提交待回执", "申报成功", "申报失败", "已缴款"} and len(reference) < 4:
        raise ValueError("该状态必须填写申报流水号、回执号或缴款凭证号")
    if review.get("status") != "已复核" and status not in {"未提交", "申报失败"}:
        raise ValueError("申报表尚未经有权人复核，不能记录为已提交或成功")
    if status in {"申报成功", "已缴款"} and not evidence:
        raise ValueError("记录申报成功或已缴款必须附回执或缴款凭证证据")
    event = {
        "status": status, "actor": actor[:80], "reference": reference[:240],
        "evidence": evidence,
        "note": str(note or "")[:1000], "timestamp": _now(),
    }
    updated = dict(review)
    submission = dict(updated.get("submission") or {})
    submission["status"] = status
    submission["latest"] = event
    submission["history"] = [*(submission.get("history") or []), event]
    updated["submission"] = submission
    return updated


def build_tax_delivery(workspace: dict, reviews: Iterable[dict]) -> dict:
    reviews = list(reviews)
    entity_id = str(workspace.get("entity_id") or "")
    forms = []
    for form in workspace.get("returns") or []:
        review = next((item for item in reviews if
                       str(item.get("entity_id") or "") == entity_id
                       and item.get("period") == workspace.get("period")
                       and item.get("form_code") == form.get("form_code")), None)
        forms.append({
            "form_code": form.get("form_code"), "name": form.get("name"), "version": form.get("version"),
            "workpaper_status": form.get("status"), "blocker_count": len(form.get("blockers") or []),
            "review_status": (review or {}).get("status") or "未复核",
            "submission_status": ((review or {}).get("submission") or {}).get("status") or "未提交",
            "agent_position": form.get("agent_position"), "review_role": form.get("review_role"),
        })
    return {
        "entity_id": entity_id, "entity_name": workspace.get("company_name"),
        "period": workspace.get("period"), "forms": forms,
        "reviewed_count": sum(item["review_status"] == "已复核" for item in forms),
        "submitted_count": sum(item["submission_status"] in {"申报成功", "已缴款", "无需缴款"} for item in forms),
        "failed_count": sum(item["submission_status"] == "申报失败" for item in forms),
        "complete": bool(forms) and all(
            item["review_status"] == "已复核"
            and item["submission_status"] in {"申报成功", "已缴款", "无需缴款"}
            for item in forms
        ),
        "guardrail": "只有申报端回执或缴款凭证才能把状态推进为成功/已缴款；导出工作底稿不等于已经申报。",
    }
