from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, InvalidOperation
from typing import Any

from .commerce import build_commerce_analysis
from .pack_services import ServiceContext


def _quantity(value: Any, field: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field} must be numeric") from exc
    if not number.is_finite() or number < 0:
        raise ValueError(f"{field} must be a finite non-negative number")
    return number


def _inventory_rows(payload: dict[str, Any], field: str, context: ServiceContext) -> list[dict[str, Any]]:
    rows = payload.get(field) or []
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError(f"{field} must be a list of objects")
    allowed = set(context.entity_ids)
    output = []
    for index, row in enumerate(rows, 1):
        if row.get("entity_id") not in allowed:
            raise ValueError(f"{field}[{index}] is outside management entity scope")
        if not row.get("sku") or not row.get("warehouse"):
            raise ValueError(f"{field}[{index}] requires sku and warehouse")
        if not row.get("evidence"):
            raise ValueError(f"{field}[{index}] requires evidence")
        output.append(dict(row))
    return output


def reconcile_marketplace_inventory(payload: dict[str, Any], context: ServiceContext) -> dict[str, Any]:
    platform_rows = _inventory_rows(payload, "platform_inventory", context)
    ledger_rows = _inventory_rows(payload, "ledger_inventory", context)
    platform: dict[tuple[str, str, str], Decimal] = defaultdict(Decimal)
    ledger: dict[tuple[str, str, str], Decimal] = defaultdict(Decimal)
    for index, row in enumerate(platform_rows, 1):
        platform[(row["entity_id"], row["sku"], row["warehouse"])] += _quantity(
            row.get("quantity"), f"platform_inventory[{index}].quantity"
        )
    for index, row in enumerate(ledger_rows, 1):
        ledger[(row["entity_id"], row["sku"], row["warehouse"])] += _quantity(
            row.get("quantity"), f"ledger_inventory[{index}].quantity"
        )
    rows = []
    issues = []
    for key in sorted(set(platform) | set(ledger)):
        difference = platform[key] - ledger[key]
        status = "reconciled" if difference == 0 else "difference"
        row = {
            "entity_id": key[0],
            "sku": key[1],
            "warehouse": key[2],
            "platform_quantity": float(platform[key]),
            "ledger_quantity": float(ledger[key]),
            "difference": float(difference),
            "status": status,
        }
        rows.append(row)
        if difference:
            issues.append(row)
    return {
        "ready": bool(rows) and not issues,
        "rows": rows,
        "issues": issues,
        "posting_or_inventory_adjustment_performed": False,
        "review_gate": "marketplace_contract_mapping",
        "guardrail": "Differences require source investigation and approved inventory adjustment; the service does not alter stock.",
    }


def _marketplace_analysis(payload: dict[str, Any], context: ServiceContext) -> dict[str, Any]:
    return build_commerce_analysis(
        payload.get("orders") or [],
        payload.get("settlements") or [],
        tolerance=payload.get("tolerance", 0.01),
        allowed_entity_ids=set(context.entity_ids),
    )


def reconcile_marketplace_fees(payload: dict[str, Any], context: ServiceContext) -> dict[str, Any]:
    analysis = _marketplace_analysis(payload, context)
    rows = [{
        "entity_id": row["entity_id"],
        "period": row["period"],
        "channel": row["channel"],
        "currency": row["currency"],
        "gross_merchandise_sales_ex_tax": row["gross_merchandise_sales_ex_tax"],
        "net_revenue_ex_tax": row["net_revenue_ex_tax"],
        "reported_order_inflow": row["reported_order_inflow"],
        "channel_and_payment_fees": row["channel_and_payment_fees"],
        "tax_withheld_or_remitted": row["tax_withheld_or_remitted"],
        "calculated_payout": row["calculated_payout"],
        "reported_payout": row["reported_payout"],
        "payout_difference": row["payout_difference"],
        "status": row["status"],
    } for row in analysis["reconciliations"]]
    return {
        "ready": bool(rows) and analysis["ready"],
        "fee_reconciliation": rows,
        "issues": analysis["issues"],
        "candidate_only": True,
        "contract_interpretation_performed": False,
        "posting_performed": False,
        "guardrail": (
            "Imported marketplace fees and payout equations are checked, but contract terms are not "
            "inferred and fee classification remains subject to approved contract mapping."
        ),
    }


def reconcile_marketplace_receivable(payload: dict[str, Any], context: ServiceContext) -> dict[str, Any]:
    analysis = _marketplace_analysis(payload, context)
    rows = [{
        "entity_id": row["entity_id"],
        "period": row["period"],
        "channel": row["channel"],
        "currency": row["currency"],
        "net_processor_inflow": row["net_processor_inflow"],
        "reported_order_inflow": row["reported_order_inflow"],
        "order_to_reported_difference": row["order_to_reported_difference"],
        "reported_payout": row["reported_payout"],
        "status": row["status"],
    } for row in analysis["reconciliations"]]
    return {
        "ready": bool(rows) and analysis["ready"],
        "receivable_reconciliation": rows,
        "issues": analysis["issues"],
        "candidate_only": True,
        "collection_or_writeoff_performed": False,
        "posting_performed": False,
        "guardrail": (
            "Order-to-platform inflow and payout evidence form a receivable candidate only; no collection, "
            "write-off, bank clearing or posting is performed."
        ),
    }
