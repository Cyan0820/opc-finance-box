from __future__ import annotations

import re
from collections import defaultdict
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Iterable

from .finance_ops import build_bank_reconciliation
from .pack_services import ServiceContext


MONEY = Decimal("0.01")


def _money(value: Any, field: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field} must be numeric") from exc
    if not number.is_finite():
        raise ValueError(f"{field} must be finite")
    return number.quantize(MONEY, rounding=ROUND_HALF_UP)


def _period(value: Any, field: str = "period") -> str:
    text = str(value or "")
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", text):
        raise ValueError(f"{field} must use YYYY-MM")
    return text


def _add_month(period: str, offset: int) -> str:
    year, month = map(int, period.split("-"))
    value = year * 12 + month - 1 + offset
    return f"{value // 12:04d}-{value % 12 + 1:02d}"


def _statutory_rows(payload: dict[str, Any], field: str, context: ServiceContext) -> list[dict[str, Any]]:
    rows = payload.get(field) or []
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError(f"{field} must be a list of objects")
    invalid = [
        str(row.get("id") or index + 1)
        for index, row in enumerate(rows)
        if row.get("entity_id") != context.entity_id
    ]
    if invalid:
        raise ValueError(
            f"{field} contains records outside statutory entity {context.entity_id}: {', '.join(invalid)}"
        )
    return [dict(row) for row in rows]


def reconcile_bank_activity(payload: dict[str, Any], context: ServiceContext) -> dict[str, Any]:
    transactions = _statutory_rows(payload, "transactions", context)
    result = build_bank_reconciliation(transactions, _period(payload.get("period")))
    result.update({
        "entity_id": context.entity_id,
        "output_status": "candidate_reconciliation",
        "full_ledger_reconciliation_completed": False,
        "review_required": True,
    })
    return result


def build_cash_forecast(payload: dict[str, Any], context: ServiceContext) -> dict[str, Any]:
    entity = context.runtime.entities.get(str(context.entity_id))
    lines = _statutory_rows(payload, "forecast_lines", context)
    as_of = _period(payload.get("as_of_period"), "as_of_period")
    horizon = payload.get("horizon_months", 3)
    if not isinstance(horizon, int) or not 3 <= horizon <= 24:
        raise ValueError("horizon_months must be an integer from 3 to 24")
    opening_value = payload.get("opening_cash")
    blockers = []
    opening_cash = None
    if opening_value in (None, ""):
        blockers.append("missing reconciled opening_cash")
    else:
        opening_cash = _money(opening_value, "opening_cash")
    minimum_buffer = _money(payload.get("minimum_buffer", 0), "minimum_buffer")
    buckets: dict[str, dict[str, Decimal]] = defaultdict(
        lambda: {"inflows": Decimal("0"), "outflows": Decimal("0")}
    )
    rejected = []
    for index, line in enumerate(lines, 1):
        line_period = _period(line.get("period"), f"forecast_lines[{index}].period")
        currency = str(line.get("currency") or entity.functional_currency).upper()
        if currency != entity.functional_currency:
            rejected.append({
                "row": index,
                "reason": f"currency {currency} requires an explicit FX translation policy",
            })
            continue
        direction = str(line.get("direction") or "")
        if direction not in {"inflow", "outflow", "收入", "支出"}:
            rejected.append({"row": index, "reason": "direction must be inflow or outflow"})
            continue
        amount = _money(line.get("amount"), f"forecast_lines[{index}].amount")
        if amount < 0:
            rejected.append({"row": index, "reason": "amount must not be negative"})
            continue
        key = "inflows" if direction in {"inflow", "收入"} else "outflows"
        buckets[line_period][key] += amount
    if rejected:
        blockers.append("forecast contains rejected lines")
    forecast = []
    cash = opening_cash
    breach_period = None
    for offset in range(1, horizon + 1):
        target = _add_month(as_of, offset)
        inflows = buckets[target]["inflows"].quantize(MONEY)
        outflows = buckets[target]["outflows"].quantize(MONEY)
        ending = (cash + inflows - outflows).quantize(MONEY) if cash is not None else None
        if breach_period is None and ending is not None and ending < minimum_buffer:
            breach_period = target
        forecast.append({
            "period": target,
            "currency": entity.functional_currency,
            "starting_cash": float(cash) if cash is not None else None,
            "inflows": float(inflows),
            "outflows": float(outflows),
            "net_cash_flow": float((inflows - outflows).quantize(MONEY)),
            "ending_cash": float(ending) if ending is not None else None,
        })
        cash = ending
    return {
        "ready": not blockers,
        "entity_id": context.entity_id,
        "currency": entity.functional_currency,
        "as_of_period": as_of,
        "horizon_months": horizon,
        "minimum_buffer": float(minimum_buffer),
        "buffer_breach_period": breach_period,
        "forecast": forecast,
        "rejected_lines": rejected,
        "blockers": blockers,
        "guardrail": "Only the entity functional currency is forecast; foreign-currency lines require an approved FX translation policy.",
    }


def assess_close_readiness(payload: dict[str, Any], context: ServiceContext) -> dict[str, Any]:
    period = _period(payload.get("period"))
    controls = payload.get("controls") or {}
    if not isinstance(controls, dict):
        raise ValueError("controls must be an object")
    task_specs = (
        ("source_data_complete", "业务资料与截止完整"),
        ("bank_reconciled", "银行流水与账面余额勾稽"),
        ("receivables_reconciled", "应收账龄与回款勾稽"),
        ("payables_reconciled", "采购、发票与付款勾稽"),
        ("vouchers_balanced", "凭证草稿借贷平衡"),
        ("vouchers_reviewed", "有权人完成凭证复核"),
        ("tax_workpaper_reviewed", "主体税务工作底稿完成复核"),
    )
    tasks = [{
        "control": control,
        "name": name,
        "status": "complete" if controls.get(control) is True else "incomplete",
        "evidence": controls.get(f"{control}_evidence") or [],
    } for control, name in task_specs]
    incomplete = [task["control"] for task in tasks if task["status"] != "complete"]
    ready_for_approval = not incomplete
    close_approved = controls.get("period_close_approved") is True
    return {
        "entity_id": context.entity_id,
        "period": period,
        "ready_for_period_close_approval": ready_for_approval,
        "can_close": ready_for_approval and close_approved,
        "period_close_approved": close_approved,
        "tasks": tasks,
        "blockers": incomplete + ([] if close_approved else ["period_close_approval"]),
        "review_gate": "period_close",
        "output_status": "readiness_assessment_only",
    }


def summarize_procure_to_pay(payload: dict[str, Any], context: ServiceContext) -> dict[str, Any]:
    rows = _statutory_rows(payload, "purchases", context)
    buckets: dict[str, dict[str, Decimal | int]] = defaultdict(
        lambda: {
            "purchase_count": 0,
            "ordered": Decimal("0"),
            "accepted": Decimal("0"),
            "invoiced": Decimal("0"),
            "paid": Decimal("0"),
        }
    )
    missing_evidence = []
    for index, row in enumerate(rows, 1):
        currency = str(row.get("currency") or "").upper()
        if not re.fullmatch(r"[A-Z]{3}", currency):
            raise ValueError(f"purchases[{index}].currency must be a 3-letter code")
        bucket = buckets[currency]
        bucket["purchase_count"] = int(bucket["purchase_count"]) + 1
        for source, target in (
            ("ordered_amount", "ordered"),
            ("accepted_amount", "accepted"),
            ("invoice_amount", "invoiced"),
            ("paid_amount", "paid"),
        ):
            bucket[target] = Decimal(bucket[target]) + _money(row.get(source, 0), f"purchases[{index}].{source}")
        if not row.get("evidence"):
            missing_evidence.append(str(row.get("id") or index))
    summaries = []
    for currency, bucket in sorted(buckets.items()):
        ordered = Decimal(bucket["ordered"])
        accepted = Decimal(bucket["accepted"])
        invoiced = Decimal(bucket["invoiced"])
        paid = Decimal(bucket["paid"])
        summaries.append({
            "currency": currency,
            "purchase_count": int(bucket["purchase_count"]),
            "ordered": float(ordered.quantize(MONEY)),
            "accepted": float(accepted.quantize(MONEY)),
            "invoiced": float(invoiced.quantize(MONEY)),
            "paid": float(paid.quantize(MONEY)),
            "unaccepted": float(max(Decimal("0"), ordered - accepted).quantize(MONEY)),
            "accepted_not_invoiced": float(max(Decimal("0"), accepted - invoiced).quantize(MONEY)),
            "invoiced_unpaid": float(max(Decimal("0"), invoiced - paid).quantize(MONEY)),
        })
    return {
        "ready": not missing_evidence,
        "entity_id": context.entity_id,
        "currency_summaries": summaries,
        "missing_evidence_purchase_ids": missing_evidence,
        "guardrail": "Currencies are summarized separately and no payment or acceptance decision is performed.",
    }


def validate_evidence_lineage(payload: dict[str, Any], context: ServiceContext) -> dict[str, Any]:
    datasets = payload.get("datasets") or {}
    if not isinstance(datasets, dict):
        raise ValueError("datasets must be an object")
    allowed_entities = set(context.entity_ids)
    failures = []
    counts = {}
    for dataset, rows in datasets.items():
        if not isinstance(rows, list):
            raise ValueError(f"dataset {dataset} must be a list")
        counts[dataset] = len(rows)
        for index, row in enumerate(rows, 1):
            if not isinstance(row, dict):
                failures.append({"dataset": dataset, "row": index, "reason": "record must be object"})
                continue
            if row.get("entity_id") not in allowed_entities:
                failures.append({"dataset": dataset, "row": index, "reason": "invalid entity scope"})
            evidence = row.get("evidence")
            if not isinstance(evidence, dict) or not evidence.get("source_file") or not evidence.get("batch_id"):
                failures.append({"dataset": dataset, "row": index, "reason": "missing evidence lineage"})
    return {
        "ready": not failures and bool(counts),
        "entity_ids": list(context.entity_ids),
        "dataset_counts": counts,
        "failure_count": len(failures),
        "failures": failures,
    }
