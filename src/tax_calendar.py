from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta
from typing import Any, Iterable

from .box_runtime import BoxRuntime


class TaxCalendarError(ValueError):
    """Raised when tax-calendar anchors are invalid or inconsistent."""


def _parse_date(value: date | str, field: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise TaxCalendarError(f"{field} must use YYYY-MM-DD") from exc


def add_months(value: date, months: int) -> date:
    """Add calendar months, preserving month-end semantics for filing deadlines."""
    if months < 0:
        raise TaxCalendarError("months must not be negative")
    month_index = value.year * 12 + value.month - 1 + months
    target_year, zero_based_month = divmod(month_index, 12)
    target_month = zero_based_month + 1
    source_last_day = calendar.monthrange(value.year, value.month)[1]
    target_last_day = calendar.monthrange(target_year, target_month)[1]
    target_day = target_last_day if value.day == source_last_day else min(value.day, target_last_day)
    return date(target_year, target_month, target_day)


def _normalize_anchor_values(value: Any, field: str) -> list[date]:
    if value is None:
        return []
    raw_values: Iterable[Any] = value if isinstance(value, (list, tuple)) else [value]
    parsed = [_parse_date(item, field) for item in raw_values]
    if len(set(parsed)) != len(parsed):
        raise TaxCalendarError(f"{field} contains duplicate dates")
    return sorted(parsed)


def _candidate_status(due_date: date, as_of: date) -> str:
    if due_date < as_of:
        return "overdue_candidate"
    if due_date <= as_of + timedelta(days=60):
        return "upcoming_candidate"
    return "future_candidate"


def _registration_state(registrations: tuple[str, ...], accepted: list[str]) -> str:
    normalized = {item.strip().lower() for item in registrations}
    expected = {item.strip().lower() for item in accepted}
    if normalized & expected:
        return "confirmed"
    keywords = {item.split("_")[0] for item in expected}
    if any(any(keyword and keyword in item for keyword in keywords) for item in normalized):
        return "needs_confirmation"
    return "not_registered"


def _sources_for_rule(rule: dict[str, Any], source_index: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    return [dict(source_index[source_id]) for source_id in rule["source_ids"]]


def _base_task(
    *,
    entity: Any,
    bundle: dict[str, Any],
    rule: dict[str, Any],
    sources: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "entity_id": entity.entity_id,
        "legal_name": entity.legal_name,
        "jurisdiction": entity.jurisdiction,
        "tax_pack": bundle["pack_id"],
        "tax_pack_version": bundle["pack_version"],
        "tax_readiness": (bundle.get("jurisdiction") or {}).get("tax_readiness"),
        "rules_verified_at": bundle["rules"]["verified_at"],
        "rule_id": rule["id"],
        "rule_effective_from": rule.get("effective_from"),
        "summary": rule.get("summary"),
        "review_gate": rule["review_gate"],
        "human_review_required": True,
        "candidate_only": True,
        "filing_completed": False,
        "source_ids": list(rule["source_ids"]),
        "official_sources": sources,
    }


def build_tax_calendar(
    runtime: BoxRuntime,
    entity_id: str,
    *,
    period_year: int | None = None,
    anchors: dict[str, Any] | None = None,
    as_of: date | str | None = None,
) -> dict[str, Any]:
    """Build explainable tax deadline candidates without representing them as filed advice."""
    runtime.reload()
    entity = runtime.entities.get(entity_id)
    bundle = runtime.tax_rules(entity_id)
    rules_payload = bundle["rules"]
    calendar_rules = [
        rule for rule in rules_payload["rules"] if rule.get("automation_level") == "calendar"
    ]
    effective_as_of = _parse_date(as_of or date.today(), "as_of")
    target_year = period_year if period_year is not None else effective_as_of.year
    if not isinstance(target_year, int) or not 1900 <= target_year <= 9999:
        raise TaxCalendarError("period_year must be a four-digit year")

    configured_anchors = dict(anchors or {})
    if "financial_year_end" not in configured_anchors:
        month, day = (int(item) for item in entity.fiscal_year_end.split("-"))
        try:
            configured_anchors["financial_year_end"] = date(target_year, month, day)
        except ValueError as exc:
            raise TaxCalendarError(
                f"fiscal_year_end {entity.fiscal_year_end} is invalid for {target_year}"
            ) from exc

    source_index = {source["id"]: source for source in rules_payload["sources"]}
    configured_gates = set(bundle["manual_review_gates"])
    tasks: list[dict[str, Any]] = []
    warnings: list[str] = []

    for rule in calendar_rules:
        if rule["review_gate"] not in configured_gates:
            raise TaxCalendarError(
                f"Rule {rule['id']} requires unconfigured review gate {rule['review_gate']}"
            )
        schedule = rule["schedule"]
        sources = _sources_for_rule(rule, source_index)
        base = _base_task(entity=entity, bundle=bundle, rule=rule, sources=sources)

        registration_any = schedule.get("registration_any")
        if registration_any:
            registration_state = _registration_state(entity.tax_registrations, registration_any)
            if registration_state != "confirmed":
                status = (
                    "needs_registration_confirmation"
                    if registration_state == "needs_confirmation"
                    else "not_applicable_unless_registered"
                )
                tasks.append({
                    **base,
                    "task_id": f"{entity_id}:{rule['id']}:registration",
                    "status": status,
                    "applicability": registration_state,
                    "anchor": schedule.get("anchor"),
                    "anchor_date": None,
                    "candidate_due_date": None,
                    "missing_configuration": ["confirmed_tax_registration"],
                })
                if registration_state == "needs_confirmation":
                    warnings.append(f"{rule['id']}: tax registration must be confirmed")
                continue

        kind = schedule["kind"]
        if kind == "manual_configuration":
            tasks.append({
                **base,
                "task_id": f"{entity_id}:{rule['id']}:configuration",
                "status": "needs_configuration",
                "applicability": "requires_local_configuration",
                "anchor": None,
                "anchor_date": None,
                "candidate_due_date": None,
                "missing_configuration": list(schedule["required_fields"]),
            })
            warnings.append(f"{rule['id']}: local filing calendar configuration is required")
            continue

        anchor_name = schedule["anchor"]
        anchor_dates = _normalize_anchor_values(configured_anchors.get(anchor_name), anchor_name)
        if not anchor_dates:
            tasks.append({
                **base,
                "task_id": f"{entity_id}:{rule['id']}:missing-anchor",
                "status": "needs_configuration",
                "applicability": "applicable",
                "anchor": anchor_name,
                "anchor_date": None,
                "candidate_due_date": None,
                "missing_configuration": [anchor_name],
            })
            warnings.append(f"{rule['id']}: {anchor_name} is required")
            continue

        for anchor_date in anchor_dates:
            if kind == "days_after_date":
                due_date = anchor_date + timedelta(days=schedule["days"])
            elif kind == "months_after_date":
                due_date = add_months(anchor_date, schedule["months"])
            elif kind == "annual_fixed_after_date":
                try:
                    due_date = date(
                        anchor_date.year + schedule["year_offset"],
                        schedule["month"],
                        schedule["day"],
                    )
                except ValueError as exc:
                    raise TaxCalendarError(f"Rule {rule['id']} produces an invalid due date") from exc
            else:  # guarded by jurisdiction-rule validation
                raise TaxCalendarError(f"Unsupported schedule kind: {kind}")
            tasks.append({
                **base,
                "task_id": f"{entity_id}:{rule['id']}:{anchor_date.isoformat()}",
                "status": _candidate_status(due_date, effective_as_of),
                "applicability": "applicable",
                "anchor": anchor_name,
                "anchor_date": anchor_date.isoformat(),
                "candidate_due_date": due_date.isoformat(),
                "missing_configuration": [],
            })

    blocking_statuses = {"needs_configuration", "needs_registration_confirmation"}
    return {
        "ready": bool(calendar_rules) and not any(task["status"] in blocking_statuses for task in tasks),
        "entity": entity.to_dict(),
        "period_year": target_year,
        "as_of": effective_as_of.isoformat(),
        "scope_note": rules_payload.get("scope_note"),
        "rules_verified_at": rules_payload["verified_at"],
        "task_count": len(tasks),
        "tasks": tasks,
        "warnings": sorted(set(warnings)),
        "guardrails": [
            "所有日期都是候选日期，提交前必须由配置的复核人确认适用性、节假日顺延和属地要求。",
            "生成日历不会完成申报、付款或对外提交。",
            "规则来源、版本和主体范围必须随任务保留。",
        ],
    }
