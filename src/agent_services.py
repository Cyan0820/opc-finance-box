from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from typing import Any

from .agent_runtime import build_goal_snapshot
from .pack_services import ServiceContext


PERIOD_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
DECISION_ALIASES = {
    "approved": "approved", "同意": "approved",
    "rejected": "rejected", "退回": "rejected",
    "deferred": "deferred", "暂缓": "deferred",
}


def _stable_id(prefix: str, payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"{prefix}-{hashlib.sha256(encoded.encode('utf-8')).hexdigest()[:16].upper()}"


def _validate_period(value: Any) -> str:
    period = str(value or "").strip()
    if not PERIOD_PATTERN.fullmatch(period):
        raise ValueError("period must use YYYY-MM")
    return period


def _assert_entity_scope(value: Any, allowed: set[str], path: str = "payload") -> None:
    if isinstance(value, dict):
        entity_id = value.get("entity_id")
        if entity_id is not None and str(entity_id) not in allowed:
            raise ValueError(f"{path}.entity_id is outside the selected management scope: {entity_id}")
        for key, child in value.items():
            _assert_entity_scope(child, allowed, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _assert_entity_scope(child, allowed, f"{path}[{index}]")


def create_goal_draft(payload: dict[str, Any], context: ServiceContext) -> dict[str, Any]:
    objective = str(payload.get("objective") or "").strip()
    if len(objective) < 4:
        raise ValueError("objective must explain the finance outcome in at least 4 characters")
    period = _validate_period(payload.get("period"))
    actor = str(payload.get("actor") or "Box user").strip()
    if not actor:
        raise ValueError("actor is required")
    entity_ids = list(context.entity_ids)
    identity = {"objective": objective[:500], "period": period, "entity_ids": entity_ids}
    goal = {
        "id": _stable_id("GOAL-DRAFT", identity),
        "type": "monthly_finance_outcome",
        "objective": identity["objective"],
        "period": period,
        "entity_ids": entity_ids,
        "status": "draft",
        "created_by": actor[:80],
        "data_mode": context.runtime.snapshot().get("data_mode"),
        "origin": "pack_service",
        "actions": [],
        "decisions": [],
        "deliverables": [],
    }
    return {
        "goal": goal,
        "output_status": "draft_not_persisted",
        "state_changed": False,
        "next_step": "将目标草稿交给已配置审计存储创建正式目标，再用 agent.build_plan_snapshot 刷新计划。",
    }


def build_plan_snapshot(payload: dict[str, Any], context: ServiceContext) -> dict[str, Any]:
    goal = deepcopy(payload.get("goal"))
    finance = deepcopy(payload.get("finance"))
    onboarding = deepcopy(payload.get("onboarding") or {})
    period_state = deepcopy(payload.get("period_state") or {})
    if not isinstance(goal, dict):
        raise ValueError("goal must be an object")
    if not isinstance(finance, dict) or not isinstance(finance.get("close"), dict):
        raise ValueError("finance.close must be an object built from current finance facts")
    if not isinstance(finance["close"].get("tasks"), list):
        raise ValueError("finance.close.tasks must be a list")
    _validate_period(goal.get("period"))
    allowed = set(context.entity_ids)
    goal_entities = goal.get("entity_ids")
    if not isinstance(goal_entities, list) or not goal_entities:
        raise ValueError("goal.entity_ids must declare at least one legal entity")
    if not set(map(str, goal_entities)).issubset(allowed):
        raise ValueError("goal.entity_ids is outside the selected management scope")
    _assert_entity_scope(finance, allowed, "finance")
    snapshot = build_goal_snapshot(
        goal,
        finance,
        onboarding,
        period_state,
        deepcopy(payload.get("business_flows")),
        deepcopy(payload.get("planning")),
        deepcopy(payload.get("analysis")),
    )
    return {
        "plan": snapshot,
        "output_status": "computed_snapshot_not_persisted",
        "state_changed": False,
        "source_policy": "计划由本次传入的财务事实重新计算，不把旧快照当作真实状态。",
    }


def create_approval_event_draft(payload: dict[str, Any], context: ServiceContext) -> dict[str, Any]:
    gate = str(payload.get("gate") or "").strip()
    configured_gates = set(context.runtime.snapshot().get("manual_review_gates") or [])
    if gate not in configured_gates:
        raise ValueError(f"gate is not configured by this Box: {gate}")
    decision = DECISION_ALIASES.get(str(payload.get("decision") or "").strip())
    if decision is None:
        raise ValueError("decision must be approved, rejected, deferred, 同意, 退回 or 暂缓")
    actor = str(payload.get("actor") or "").strip()
    rationale = str(payload.get("rationale") or "").strip()
    target_id = str(payload.get("target_id") or "").strip()
    if not actor:
        raise ValueError("actor is required")
    if len(rationale) < 4:
        raise ValueError("rationale must contain at least 4 characters")
    if not target_id:
        raise ValueError("target_id is required")
    evidence = payload.get("evidence") or []
    if not isinstance(evidence, list):
        raise ValueError("evidence must be a list")
    event_body = {
        "target_id": target_id[:120],
        "gate": gate,
        "decision": decision,
        "actor": actor[:80],
        "rationale": rationale[:1000],
        "evidence": [str(item)[:500] for item in evidence[:20]],
        "entity_ids": list(context.entity_ids),
    }
    return {
        "approval_event": {"id": _stable_id("APPROVAL-DRAFT", event_body), **event_body},
        "output_status": "approval_event_draft_not_persisted",
        "state_changed": False,
        "control_note": "此草稿不会改变目标、账簿、申报或付款状态；须由审计存储记录并由目标动作重新计算后才生效。",
    }
