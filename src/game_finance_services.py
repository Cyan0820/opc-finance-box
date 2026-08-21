from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

from .pack_services import ServiceContext


MONEY = Decimal("0.01")
RATE = Decimal("0.000001")


def _amount(value: Any, field: str) -> Decimal:
    try:
        number = Decimal(str(value or 0))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field} must be numeric") from exc
    if not number.is_finite():
        raise ValueError(f"{field} must be finite")
    return number.quantize(MONEY, rounding=ROUND_HALF_UP)


def _rate(value: Any, field: str) -> Decimal:
    try:
        number = Decimal(str(value or 0))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field} must be numeric") from exc
    if not number.is_finite():
        raise ValueError(f"{field} must be finite")
    return number.quantize(RATE, rounding=ROUND_HALF_UP)


def _scoped_rows(payload: dict[str, Any], field: str, context: ServiceContext) -> list[dict[str, Any]]:
    rows = payload.get(field) or []
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError(f"{field} must be a list of objects")
    allowed = set(context.entity_ids)
    output = []
    for index, row in enumerate(rows, 1):
        if row.get("entity_id") not in allowed:
            raise ValueError(f"{field}[{index}] is outside management entity scope")
        if not row.get("evidence"):
            raise ValueError(f"{field}[{index}] requires evidence")
        output.append(dict(row))
    return output


def reconcile_game_channel_settlements(payload: dict[str, Any], context: ServiceContext) -> dict[str, Any]:
    rows = _scoped_rows(payload, "settlements", context)
    tolerance = _amount(payload.get("tolerance", 0.01), "tolerance")
    seen = set()
    reconciliations = []
    issues = []
    for index, row in enumerate(rows, 1):
        settlement_id = str(row.get("id") or row.get("settlement_id") or "")
        if not settlement_id:
            raise ValueError(f"settlements[{index}] requires id")
        business_key = (row["entity_id"], settlement_id)
        if business_key in seen:
            issues.append({"id": settlement_id, "reason": "duplicate settlement id"})
            continue
        seen.add(business_key)
        currency = str(row.get("currency") or "").upper()
        if len(currency) != 3:
            raise ValueError(f"settlements[{index}].currency must be a 3-letter code")
        basis = _amount(row.get("contract_basis"), f"settlements[{index}].contract_basis")
        rate = _rate(row.get("contract_rate"), f"settlements[{index}].contract_rate")
        adjustments = _amount(row.get("contract_adjustments", 0), f"settlements[{index}].contract_adjustments")
        reported = _amount(
            row.get("reported_settlement", row.get("settlement_amount")),
            f"settlements[{index}].reported_settlement",
        )
        withholding = _amount(row.get("withholding_tax", 0), f"settlements[{index}].withholding_tax")
        reported_receivable = _amount(
            row.get("net_receivable", reported - withholding),
            f"settlements[{index}].net_receivable",
        )
        expected = (basis * rate + adjustments).quantize(MONEY)
        expected_receivable = (reported - withholding).quantize(MONEY)
        settlement_difference = (reported - expected).quantize(MONEY)
        receivable_difference = (reported_receivable - expected_receivable).quantize(MONEY)
        status = (
            "reconciled"
            if abs(settlement_difference) <= tolerance and abs(receivable_difference) <= tolerance
            else "difference"
        )
        result = {
            "id": settlement_id,
            "entity_id": row["entity_id"],
            "period": row.get("period"),
            "game": row.get("game") or row.get("project_code"),
            "channel": row.get("channel"),
            "currency": currency,
            "contract_basis": float(basis),
            "contract_rate": float(rate),
            "contract_adjustments": float(adjustments),
            "expected_settlement": float(expected),
            "reported_settlement": float(reported),
            "settlement_difference": float(settlement_difference),
            "withholding_tax": float(withholding),
            "expected_net_receivable": float(expected_receivable),
            "reported_net_receivable": float(reported_receivable),
            "receivable_difference": float(receivable_difference),
            "status": status,
            "evidence": row["evidence"],
        }
        reconciliations.append(result)
        if status == "difference":
            issues.append(result)
    return {
        "ready": bool(reconciliations) and not issues,
        "reconciliations": reconciliations,
        "issues": issues,
        "posting_or_collection_performed": False,
        "guardrail": "Contract basis and rates must be explicitly mapped; the service does not infer commercial terms from channel name.",
    }


def calculate_game_project_profitability(payload: dict[str, Any], context: ServiceContext) -> dict[str, Any]:
    revenues = _scoped_rows(payload, "revenues", context)
    costs = _scoped_rows(payload, "costs", context)
    buckets: dict[tuple[str, str, str, str], dict[str, Decimal]] = defaultdict(
        lambda: defaultdict(Decimal)
    )
    for index, row in enumerate(revenues, 1):
        project = str(row.get("project_code") or row.get("game") or "")
        if not project:
            raise ValueError(f"revenues[{index}] requires project_code")
        currency = str(row.get("currency") or "").upper()
        key = (row["entity_id"], project, str(row.get("period") or ""), currency)
        buckets[key]["revenue"] += _amount(row.get("amount"), f"revenues[{index}].amount")
    for index, row in enumerate(costs, 1):
        project = str(row.get("project_code") or row.get("game") or "")
        if not project:
            raise ValueError(f"costs[{index}] requires project_code")
        currency = str(row.get("currency") or "").upper()
        key = (row["entity_id"], project, str(row.get("period") or ""), currency)
        buckets[key]["cost"] += _amount(row.get("amount"), f"costs[{index}].amount")
    rows = []
    for key, values in sorted(buckets.items()):
        revenue = values["revenue"]
        cost = values["cost"]
        contribution = revenue - cost
        rows.append({
            "entity_id": key[0],
            "project_code": key[1],
            "period": key[2],
            "currency": key[3],
            "revenue": float(revenue),
            "direct_cost": float(cost),
            "contribution": float(contribution),
            "contribution_margin": round(float(contribution / revenue), 4) if revenue else None,
        })
    return {
        "ready": bool(rows),
        "rows": rows,
        "guardrail": "Project profitability remains separated by entity and currency; corporate allocations are not invented.",
    }


def review_game_ltv_roi(payload: dict[str, Any], context: ServiceContext) -> dict[str, Any]:
    cohorts = _scoped_rows(payload, "cohorts", context)
    rows = []
    blockers = []
    for index, row in enumerate(cohorts, 1):
        spend = _amount(row.get("spend"), f"cohorts[{index}].spend")
        acquired_users = _amount(row.get("acquired_users"), f"cohorts[{index}].acquired_users")
        realized_revenue = _amount(row.get("realized_net_revenue"), f"cohorts[{index}].realized_net_revenue")
        forecast_ltv = row.get("forecast_ltv")
        forecast_ltv_value = (
            _amount(forecast_ltv, f"cohorts[{index}].forecast_ltv")
            if forecast_ltv not in (None, "") else None
        )
        maturity_days = int(row.get("maturity_days") or 0)
        target_roi = _rate(row.get("target_roi", 1), f"cohorts[{index}].target_roi")
        row_blockers = []
        if spend <= 0:
            row_blockers.append("missing positive finance-reconciled spend")
        if acquired_users <= 0:
            row_blockers.append("missing acquired user denominator")
        if forecast_ltv_value is None:
            row_blockers.append("missing cohort LTV forecast")
        cac = (spend / acquired_users).quantize(MONEY) if acquired_users else None
        projected_revenue = (
            (forecast_ltv_value * acquired_users).quantize(MONEY)
            if forecast_ltv_value is not None else None
        )
        projected_roi = (
            (projected_revenue / spend).quantize(Decimal("0.0001"))
            if projected_revenue is not None and spend else None
        )
        if row_blockers:
            recommendation = "hold_for_evidence"
        elif maturity_days < int(row.get("minimum_maturity_days") or 30):
            recommendation = "hold_until_cohort_matures"
        elif projected_roi is not None and projected_roi >= target_roi:
            recommendation = "eligible_for_budget_review"
        else:
            recommendation = "review_reduction_or_pause"
        result = {
            "entity_id": row["entity_id"],
            "project_code": row.get("project_code"),
            "channel": row.get("channel"),
            "region": row.get("region"),
            "cohort": row.get("cohort"),
            "currency": row.get("currency"),
            "spend": float(spend),
            "acquired_users": float(acquired_users),
            "realized_net_revenue": float(realized_revenue),
            "realized_roas": round(float(realized_revenue / spend), 4) if spend else None,
            "cac": float(cac) if cac is not None else None,
            "forecast_ltv": float(forecast_ltv_value) if forecast_ltv_value is not None else None,
            "projected_roi": float(projected_roi) if projected_roi is not None else None,
            "target_roi": float(target_roi),
            "maturity_days": maturity_days,
            "recommendation": recommendation,
            "blockers": row_blockers,
        }
        rows.append(result)
        blockers.extend({"cohort": row.get("cohort"), "reason": reason} for reason in row_blockers)
    return {
        "ready": bool(rows) and not blockers,
        "rows": rows,
        "blockers": blockers,
        "review_gate": "user_acquisition_budget_change",
        "boundary": {
            "owned": ["spend reconciliation", "unit economics", "budget gate", "profit and cash impact"],
            "not_owned": ["bidding", "creative optimization", "audience targeting", "media account operation"],
        },
        "budget_change_performed": False,
    }
