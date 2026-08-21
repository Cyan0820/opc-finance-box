from __future__ import annotations

from typing import Any

from .game_accounting import build_revenue_recognition, create_revenue_policy
from .game_kpis import enrich_kpis, kpi_quality
from .pack_services import ServiceContext


def _scope_quality(rows: list[dict[str, Any]], context: ServiceContext, dataset: str) -> dict[str, Any]:
    allowed = set(context.entity_ids)
    unassigned = [str(row.get("id") or index + 1) for index, row in enumerate(rows) if not row.get("entity_id")]
    outside = [{
        "id": str(row.get("id") or index + 1),
        "entity_id": row.get("entity_id"),
    } for index, row in enumerate(rows) if row.get("entity_id") and row.get("entity_id") not in allowed]
    return {
        "dataset": dataset,
        "ready": not unassigned and not outside,
        "record_count": len(rows),
        "unassigned_ids": unassigned,
        "outside_scope": outside,
        "blocker": (
            "游戏记录必须明确归属当前 Box 范围内的法律主体。"
            if unassigned or outside else ""
        ),
    }


def analyze_game_kpis(payload: dict[str, Any], context: ServiceContext) -> dict[str, Any]:
    rows = list(payload.get("rows") or [])
    scope_quality = _scope_quality(rows, context, "game_kpis")
    if not scope_quality["ready"]:
        return {"ready": False, "rows": [], "scope_quality": scope_quality, "kpi_quality": None}
    enriched = enrich_kpis(rows)
    quality = kpi_quality(enriched, set(payload.get("game_codes") or []))
    return {
        "ready": not quality["issue_count"],
        "rows": enriched,
        "scope_quality": scope_quality,
        "kpi_quality": quality,
        "guardrail": "投放指标用于财务资源效率与预测，不替代投手的实时媒体优化。",
    }


def draft_game_revenue_policy(payload: dict[str, Any], context: ServiceContext) -> dict[str, Any]:
    policy = create_revenue_policy(
        payload.get("game"),
        payload.get("channel"),
        payload.get("revenue_stream"),
        payload.get("presentation"),
        payload.get("recognition_method"),
        payload.get("effective_from"),
        payload.get("actor"),
        payload.get("evidence") or [],
        service_months=payload.get("service_months"),
        role_facts=payload.get("role_facts"),
    )
    policy["entity_id"] = context.entity_id
    return {
        "ready": policy["status"] != "阻塞",
        "policy": policy,
        "output_status": "draft_pending_accountant_review",
    }


def calculate_game_revenue_recognition(payload: dict[str, Any], context: ServiceContext) -> dict[str, Any]:
    settlements = list(payload.get("settlements") or [])
    policies = list(payload.get("policies") or [])
    settlement_quality = _scope_quality(settlements, context, "settlements")
    policy_quality = _scope_quality(policies, context, "revenue_policies")
    if not settlement_quality["ready"] or not policy_quality["ready"]:
        return {
            "ready": False,
            "period": payload.get("target_period"),
            "rows": [],
            "blockers": [
                quality["blocker"] for quality in (settlement_quality, policy_quality) if quality["blocker"]
            ],
            "scope_quality": {"settlements": settlement_quality, "policies": policy_quality},
        }
    recognition = build_revenue_recognition(
        settlements,
        policies,
        payload.get("target_period"),
    )
    return {
        **recognition,
        "ready": not recognition["blockers"],
        "entity_id": context.entity_id,
        "scope_quality": {"settlements": settlement_quality, "policies": policy_quality},
        "output_status": "draft_pending_voucher_review",
    }
