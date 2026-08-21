from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable

from .business_flows import build_receivables_register


def _text(value: Any) -> str:
    return str(value or "").strip()


def _ratio(received: float, receivable: float) -> float | None:
    return round(received / receivable, 4) if receivable else None


def _game_aliases(master_records: Iterable[dict]) -> tuple[dict[tuple[str, str], tuple[str, str]], dict[str, str]]:
    aliases: dict[tuple[str, str], tuple[str, str]] = {}
    names: dict[str, str] = {}
    for record in master_records:
        if record.get("record_type") != "game" or record.get("active") is False:
            continue
        entity_id = _text(record.get("entity_id"))
        code = _text(record.get("code"))
        name = _text(record.get("name"))
        management_id = _text(record.get("management_game_id") or record.get("group_game_id") or code or name)
        management_name = _text(record.get("management_game_name") or record.get("group_game_name") or name or code)
        if not management_id:
            continue
        names.setdefault(management_id, management_name or management_id)
        for value in (code, name):
            if value:
                aliases[(entity_id, value.casefold())] = (management_id, management_name or management_id)
    return aliases, names


def build_game_collection_portfolio(
    datasets: dict[str, list[dict]], *, as_of: str | None = None,
) -> dict:
    """Build a read-only same-game collection view without merging statutory records."""
    settlements = datasets.get("settlements") or []
    receivables = build_receivables_register(
        settlements, datasets.get("cash_allocations") or [], as_of,
        datasets.get("master_records") or [], datasets.get("collection_actions") or [],
    )
    aliases, management_names = _game_aliases(datasets.get("master_records") or [])
    sources = {
        (_text(item.get("entity_id")), _text(item.get("id"))): item
        for item in settlements
    }
    buckets: dict[tuple[str, str, str], dict] = defaultdict(lambda: {
        "receivable": 0.0, "received": 0.0, "outstanding": 0.0,
        "effective_promised_amount": 0.0, "promise_scenario_outstanding": 0.0,
        "overdue_count": 0, "due_soon_count": 0, "missed_promise_count": 0,
        "disputed_count": 0, "receivable_count": 0, "local_games": set(),
        "priority_items": [],
    })
    ownership_gap_count = 0
    for row in receivables["rows"]:
        entity_id = _text(row.get("entity_id"))
        source = sources.get((entity_id, _text(row.get("id")))) or {}
        local_game = _text(row.get("game")) or "未命名游戏"
        explicit_id = _text(source.get("management_game_id") or source.get("group_game_id"))
        explicit_name = _text(source.get("management_game_name") or source.get("group_game_name"))
        alias_id, alias_name = aliases.get((entity_id, local_game.casefold()), ("", ""))
        management_id = explicit_id or alias_id or local_game.casefold()
        management_name = explicit_name or alias_name or management_names.get(management_id) or local_game
        management_names.setdefault(management_id, management_name)
        if not entity_id:
            ownership_gap_count += 1
            entity_id = "legacy_unassigned"
        currency = _text(row.get("currency")).upper() or "CNY"
        bucket = buckets[(management_id, entity_id, currency)]
        bucket["receivable"] += float(row.get("expected_receivable") or 0)
        bucket["received"] += float(row.get("allocated_receipts") or 0)
        bucket["outstanding"] += float(row.get("outstanding") or 0)
        bucket["effective_promised_amount"] += float(row.get("effective_promised_amount") or 0)
        bucket["promise_scenario_outstanding"] += float(row.get("promise_scenario_outstanding") or 0)
        bucket["overdue_count"] += int(bool((row.get("days_overdue") or 0) > 0 and row.get("outstanding", 0) > 0))
        bucket["due_soon_count"] += int(row.get("collection_priority") == "P2")
        bucket["missed_promise_count"] += int(bool(row.get("promise_missed")))
        bucket["disputed_count"] += int(bool(row.get("disputed")))
        bucket["receivable_count"] += 1
        bucket["local_games"].add(local_game)
        if row.get("outstanding", 0) > 0:
            bucket["priority_items"].append(row)

    rows = []
    entities_by_game: dict[str, set[str]] = defaultdict(set)
    currencies_by_game: dict[str, set[str]] = defaultdict(set)
    for (management_id, entity_id, currency), values in sorted(buckets.items()):
        receivable = round(values["receivable"], 2)
        received = round(values["received"], 2)
        outstanding = round(values["outstanding"], 2)
        priority_items = sorted(values["priority_items"], key=lambda item: (
            -int(item.get("collection_priority_score") or 0),
            -(item.get("days_overdue") or 0), -float(item.get("outstanding") or 0),
        ))
        top_priority = priority_items[0] if priority_items else {}
        entities_by_game[management_id].add(entity_id)
        currencies_by_game[management_id].add(currency)
        rows.append({
            "management_game_id": management_id,
            "management_game_name": management_names.get(management_id) or management_id,
            "entity_id": entity_id,
            "currency": currency,
            "local_games": sorted(values["local_games"]),
            "receivable": receivable,
            "received": received,
            "outstanding": outstanding,
            "effective_promised_amount": round(values["effective_promised_amount"], 2),
            "promise_scenario_outstanding": round(values["promise_scenario_outstanding"], 2),
            "collection_rate": _ratio(received, receivable),
            "overdue_count": values["overdue_count"],
            "due_soon_count": values["due_soon_count"],
            "missed_promise_count": values["missed_promise_count"],
            "disputed_count": values["disputed_count"],
            "receivable_count": values["receivable_count"],
            "collection_priority": top_priority.get("collection_priority") or "CLOSED",
            "collection_priority_label": top_priority.get("collection_priority_label") or "已结清",
            "collection_priority_reason": top_priority.get("collection_priority_reason") or "当前范围已无未结清应收",
            "recommended_collection_action": top_priority.get("recommended_collection_action") or "保留核销依据",
            "priority_items": [{
                "id": item.get("id"), "channel": item.get("channel"),
                "due_date": item.get("due_date"), "days_overdue": item.get("days_overdue"),
                "outstanding": item.get("outstanding"),
                "collection_priority": item.get("collection_priority"),
                "collection_priority_label": item.get("collection_priority_label"),
            } for item in priority_items[:3]],
            "action_scope": "entity_currency_only",
        })
    rows.sort(key=lambda item: (
        -len(entities_by_game[item["management_game_id"]]),
        item["management_game_name"], item["entity_id"], item["currency"],
    ))
    game_ids = sorted(entities_by_game)
    return {
        "rows": rows,
        "summary": {
            "game_count": len(game_ids),
            "multi_entity_game_count": sum(len(entities_by_game[key]) > 1 for key in game_ids),
            "entity_count": len({row["entity_id"] for row in rows if row["entity_id"] != "legacy_unassigned"}),
            "ownership_gap_count": ownership_gap_count,
            "currencies": sorted({row["currency"] for row in rows}),
            "priority_entity_currency_scope_count": sum(bool(row.get("priority_items")) for row in rows),
            "valid_promise_count": sum(row.get("effective_promised_amount", 0) > 0 for row in rows),
            "missed_promise_count": sum(row.get("missed_promise_count", 0) for row in rows),
        },
        "game_scopes": [{
            "management_game_id": key,
            "management_game_name": management_names.get(key) or key,
            "entity_count": len(entities_by_game[key]),
            "currencies": sorted(currencies_by_game[key]),
        } for key in game_ids],
        "guardrail": "同一游戏可跨主体打包查看，但不同币种不直接相加；承诺回款只是情景、不冲减应收；银行核销、凭证、税务和审批仍须进入具体法律主体逐笔处理。",
        "promise_scenario_overwrites_baseline": False,
        "books_merged": False,
        "statutory_actions_require_entity": True,
    }
