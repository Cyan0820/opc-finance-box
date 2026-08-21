from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

from .pack_services import ServiceContext


MONEY = Decimal("0.01")
FX_RATE = Decimal("0.00000001")
METRICS = ("revenue", "expenses", "cash", "receivables", "payables")
PNL_METRICS = {"revenue", "expenses"}
PORTFOLIO_METRICS = ("revenue", "expenses", "cash", "assets", "liabilities")
PORTFOLIO_PNL_METRICS = {"revenue", "expenses"}


def _amount(value: Any, field: str) -> Decimal:
    try:
        number = Decimal(str(value or 0))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field} must be numeric") from exc
    if not number.is_finite():
        raise ValueError(f"{field} must be finite")
    return number.quantize(MONEY, rounding=ROUND_HALF_UP)


def _period(value: Any) -> str:
    text = str(value or "")
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", text):
        raise ValueError("period must use YYYY-MM")
    return text


def _required_amount(value: Any, field: str) -> Decimal:
    if isinstance(value, bool) or value in (None, ""):
        raise ValueError(f"{field} must be a finite decimal")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field} must be a finite decimal") from exc
    if not number.is_finite():
        raise ValueError(f"{field} must be a finite decimal")
    return number.quantize(MONEY, rounding=ROUND_HALF_UP)


def _required_rate(value: Any, field: str) -> Decimal:
    if isinstance(value, bool) or value in (None, ""):
        raise ValueError(f"{field} must be a finite positive decimal")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field} must be a finite positive decimal") from exc
    if not number.is_finite() or number <= 0:
        raise ValueError(f"{field} must be a finite positive decimal")
    return number.quantize(FX_RATE, rounding=ROUND_HALF_UP)


def _required_text(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field} must be a non-empty string")
    return text


def _required_evidence(value: Any, field: str) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a non-empty list")
    evidence = [str(item).strip() for item in value if str(item).strip()]
    if not evidence:
        raise ValueError(f"{field} must be a non-empty list")
    return evidence


def _portfolio_rates(
    payload: dict[str, Any], context: ServiceContext, period: str,
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    reporting_currency = str(context.scope["reporting_currency"]).upper()
    raw_rates = payload.get("fx_rates") or {}
    if not isinstance(raw_rates, dict):
        raise ValueError("fx_rates must be an object keyed by source currency")
    rates: dict[str, dict[str, Any]] = {
        reporting_currency: {
            "pnl_rate": Decimal("1"),
            "closing_rate": Decimal("1"),
            "source_reference": "policy://reporting-currency-identity",
            "period": period,
            "review_status": "identity",
            "reviewed_by": "deterministic_identity_rule",
            "evidence": ["source currency equals Box reporting currency"],
        }
    }
    blockers: list[str] = []
    for raw_currency, raw in raw_rates.items():
        currency = str(raw_currency or "").upper()
        field = f"fx_rates.{raw_currency}"
        if not re.fullmatch(r"[A-Z]{3}", currency):
            blockers.append(f"{field} must use a three-letter currency key")
            continue
        if currency == reporting_currency:
            blockers.append(
                f"{currency} equals reporting currency and must use the deterministic identity rate"
            )
            continue
        if not isinstance(raw, dict):
            raise ValueError(f"{field} must be an object")
        rate_period = str(raw.get("period") or "")
        source_reference = str(raw.get("source_reference") or "").strip()
        review_status = str(raw.get("review_status") or "")
        reviewed_by = str(raw.get("reviewed_by") or "").strip()
        evidence = raw.get("evidence")
        reasons = []
        if rate_period != period:
            reasons.append(f"period must equal {period}")
        if not source_reference:
            reasons.append("source_reference is required")
        if review_status != "approved":
            reasons.append("review_status must be approved")
        if not reviewed_by:
            reasons.append("reviewed_by is required")
        if not isinstance(evidence, list) or not any(str(item).strip() for item in evidence):
            reasons.append("evidence must be a non-empty list")
        pnl_rate = _required_rate(raw.get("pnl_rate"), f"{field}.pnl_rate")
        closing_rate = _required_rate(raw.get("closing_rate"), f"{field}.closing_rate")
        if reasons:
            blockers.append(f"{currency} FX rate: {'; '.join(reasons)}")
            continue
        rates[currency] = {
            "pnl_rate": pnl_rate,
            "closing_rate": closing_rate,
            "source_reference": source_reference,
            "period": rate_period,
            "review_status": review_status,
            "reviewed_by": reviewed_by,
            "evidence": [str(item).strip() for item in evidence if str(item).strip()],
        }
    return rates, blockers


def build_month_close_portfolio(payload: dict[str, Any], context: ServiceContext) -> dict[str, Any]:
    """Combine reviewed single-entity close candidates without changing statutory books."""
    period = _period(payload.get("period"))
    controls = payload.get("entity_close_controls")
    if not isinstance(controls, list) or any(not isinstance(item, dict) for item in controls):
        raise ValueError("entity_close_controls must be a list of objects")
    selected_entities = set(context.entity_ids)
    if len(selected_entities) < 2:
        raise ValueError("month-close portfolio requires at least two legal entities")
    rates, blockers = _portfolio_rates(payload, context, period)
    seen_entities: set[str] = set()
    native_entities: list[dict[str, Any]] = []
    translated_entities: list[dict[str, Any]] = []
    statutory_readiness: list[dict[str, Any]] = []

    for index, raw in enumerate(controls):
        field = f"entity_close_controls[{index}]"
        entity_id = _required_text(raw.get("entity_id"), f"{field}.entity_id")
        if entity_id not in selected_entities:
            blockers.append(f"{field} has entity outside management scope: {entity_id}")
            continue
        if entity_id in seen_entities:
            blockers.append(f"duplicate entity close control: {entity_id}")
            continue
        seen_entities.add(entity_id)
        entity = context.runtime.entities.get(entity_id)
        entity_blockers: list[str] = []
        if raw.get("period") != period:
            entity_blockers.append("close-control period does not match portfolio period")
        if raw.get("source_pipeline_id") != "finance.month_close_control":
            entity_blockers.append("source_pipeline_id must be finance.month_close_control")
        source_run_id = str(raw.get("source_run_id") or "")
        if not re.fullmatch(r"[a-f0-9]{24}", source_run_id):
            entity_blockers.append("source_run_id must be a 24-character pipeline run id")
        source_attempt_id = str(raw.get("source_attempt_id") or "")
        if source_attempt_id and not re.fullmatch(r"[a-f0-9]{24}", source_attempt_id):
            entity_blockers.append("source_attempt_id must be a 24-character ledger attempt id")
        try:
            source_evidence = _required_evidence(raw.get("source_evidence"), f"{field}.source_evidence")
        except ValueError as exc:
            entity_blockers.append(str(exc))
            source_evidence = []
        if raw.get("candidate_only") is not True:
            entity_blockers.append("source must remain candidate_only")
        if raw.get("posting_performed") is not False:
            entity_blockers.append("source must explicitly state posting_performed=false")
        if raw.get("period_close_performed") is not False:
            entity_blockers.append("source must explicitly state period_close_performed=false")
        if raw.get("close_control_ready_for_review") is not True:
            entity_blockers.append("single-entity month-close control is not ready for review")
        source_blockers = raw.get("blockers") or []
        if not isinstance(source_blockers, list):
            raise ValueError(f"{field}.blockers must be a list")
        if source_blockers:
            entity_blockers.append("single-entity source reports blockers")

        summaries = raw.get("currency_summaries")
        if not isinstance(summaries, list) or not summaries:
            entity_blockers.append("currency_summaries must be a non-empty list")
            summaries = []
        normalized_summaries: list[dict[str, Any]] = []
        translated_summaries: list[dict[str, Any]] = []
        seen_currencies: set[str] = set()
        for summary_index, summary in enumerate(summaries):
            summary_field = f"{field}.currency_summaries[{summary_index}]"
            if not isinstance(summary, dict):
                raise ValueError(f"{summary_field} must be an object")
            currency = str(summary.get("currency") or "").upper()
            if not re.fullmatch(r"[A-Z]{3}", currency):
                entity_blockers.append(f"{summary_field}.currency must be a three-letter code")
                continue
            if currency in seen_currencies:
                entity_blockers.append(f"duplicate currency summary: {entity_id} {currency}")
                continue
            seen_currencies.add(currency)
            metrics = {
                metric: _required_amount(summary.get(metric), f"{summary_field}.{metric}")
                for metric in PORTFOLIO_METRICS
            }
            profit = _required_amount(
                summary.get("profit_before_tax_candidate"),
                f"{summary_field}.profit_before_tax_candidate",
            )
            derived_profit = (metrics["revenue"] - metrics["expenses"]).quantize(MONEY)
            if abs(profit - derived_profit) >= MONEY:
                entity_blockers.append(
                    f"{entity_id} {currency} profit does not equal revenue minus expenses"
                )
            bank_account_count = summary.get("bank_account_count")
            if isinstance(bank_account_count, bool) or not isinstance(bank_account_count, int) or bank_account_count < 0:
                raise ValueError(f"{summary_field}.bank_account_count must be a non-negative integer")
            native = {
                "currency": currency,
                "bank_account_count": bank_account_count,
                **{metric: float(value) for metric, value in metrics.items()},
                "profit_before_tax_candidate": float(profit),
            }
            normalized_summaries.append(native)
            rate = rates.get(currency)
            if rate is None:
                entity_blockers.append(f"missing approved FX rates for {currency}")
                continue
            translated_metrics = {
                metric: (
                    metrics[metric]
                    * (rate["pnl_rate"] if metric in PORTFOLIO_PNL_METRICS else rate["closing_rate"])
                ).quantize(MONEY)
                for metric in PORTFOLIO_METRICS
            }
            translated_profit = (
                translated_metrics["revenue"] - translated_metrics["expenses"]
            ).quantize(MONEY)
            translated_summaries.append({
                "source_currency": currency,
                "reporting_currency": context.scope["reporting_currency"],
                "source_metrics": {**native},
                "translated_metrics": {
                    **{metric: float(value) for metric, value in translated_metrics.items()},
                    "profit_before_tax_candidate": float(translated_profit),
                },
                "pnl_rate": float(rate["pnl_rate"]),
                "closing_rate": float(rate["closing_rate"]),
                "fx_source_reference": rate["source_reference"],
                "fx_period": rate["period"],
                "fx_review_status": rate["review_status"],
                "fx_reviewed_by": rate["reviewed_by"],
                "fx_evidence": list(rate["evidence"]),
            })

        entity_ready = not entity_blockers
        statutory_readiness.append({
            "entity_id": entity_id,
            "legal_name": entity.legal_name,
            "functional_currency": entity.functional_currency,
            "period": period,
            "source_run_id": source_run_id,
            "source_attempt_id": source_attempt_id or None,
            "ready_for_portfolio_review": entity_ready,
            "blockers": entity_blockers,
        })
        native_entities.append({
            "entity_id": entity_id,
            "legal_name": entity.legal_name,
            "functional_currency": entity.functional_currency,
            "period": period,
            "source_pipeline_id": raw.get("source_pipeline_id"),
            "source_run_id": source_run_id,
            "source_attempt_id": source_attempt_id or None,
            "source_evidence": source_evidence,
            "currency_summaries": normalized_summaries,
            "candidate_only": True,
            "statutory_books_modified": False,
        })
        if entity_ready:
            translated_entities.append({
                "entity_id": entity_id,
                "period": period,
                "source_run_id": source_run_id,
                "currency_summaries": translated_summaries,
                "statutory_books_modified": False,
            })
        blockers.extend(f"{entity_id}: {reason}" for reason in entity_blockers)

    missing_entities = sorted(selected_entities - seen_entities)
    if missing_entities:
        blockers.append(f"missing entity close controls: {', '.join(missing_entities)}")
    complete = not blockers and seen_entities == selected_entities
    totals = None
    if complete:
        decimal_totals = {metric: Decimal("0") for metric in PORTFOLIO_METRICS}
        decimal_profit = Decimal("0")
        for entity in translated_entities:
            for summary in entity["currency_summaries"]:
                translated = summary["translated_metrics"]
                for metric in PORTFOLIO_METRICS:
                    decimal_totals[metric] += Decimal(str(translated[metric]))
                decimal_profit += Decimal(str(translated["profit_before_tax_candidate"]))
        totals = {
            **{metric: float(value.quantize(MONEY)) for metric, value in decimal_totals.items()},
            "profit_before_tax_candidate": float(decimal_profit.quantize(MONEY)),
        }
    readiness_by_id = {item["entity_id"]: item for item in statutory_readiness}
    statutory_readiness = [
        readiness_by_id[entity_id]
        for entity_id in sorted(readiness_by_id)
    ]
    return {
        "ready": complete,
        "period": period,
        "reporting_currency": context.scope["reporting_currency"],
        "selected_entity_ids": sorted(selected_entities),
        "statutory_readiness": statutory_readiness,
        "native_entity_candidates": sorted(native_entities, key=lambda item: item["entity_id"]),
        "translated_entity_candidates": sorted(translated_entities, key=lambda item: item["entity_id"]),
        "management_portfolio_totals": totals,
        "entity_count": len(selected_entities),
        "ready_entity_count": sum(item["ready_for_portfolio_review"] for item in statutory_readiness),
        "blockers": list(dict.fromkeys(blockers)),
        "candidate_only": True,
        "pre_elimination_view": True,
        "consolidated_financial_statements_produced": False,
        "statutory_books_modified": False,
        "posting_performed": False,
        "period_close_performed": False,
        "external_filing_performed": False,
        "review_gate": "month_close_portfolio_review",
        "guardrail": (
            "This combines reviewed close candidates for founder management review only. "
            "It never nets native currencies, performs eliminations, modifies legal-entity books, "
            "posts journals, closes periods or produces statutory consolidated statements."
        ),
    }


def _rates(payload: dict[str, Any], context: ServiceContext) -> tuple[dict[str, dict[str, Any]], list[str]]:
    reporting_currency = context.scope["reporting_currency"]
    raw_rates = payload.get("fx_rates") or {}
    if not isinstance(raw_rates, dict):
        raise ValueError("fx_rates must be an object keyed by source currency")
    rates: dict[str, dict[str, Any]] = {
        reporting_currency: {
            "pnl_rate": Decimal("1"),
            "closing_rate": Decimal("1"),
            "source": "identity",
            "as_of": payload.get("period"),
        }
    }
    blockers = []
    for currency, raw in raw_rates.items():
        if not isinstance(raw, dict):
            raise ValueError(f"fx_rates.{currency} must be an object")
        source = str(raw.get("source") or "").strip()
        as_of = str(raw.get("as_of") or "").strip()
        if not source or not as_of:
            blockers.append(f"{currency} FX rate requires source and as_of")
            continue
        pnl_rate = _amount(raw.get("pnl_rate"), f"fx_rates.{currency}.pnl_rate")
        closing_rate = _amount(raw.get("closing_rate"), f"fx_rates.{currency}.closing_rate")
        if pnl_rate <= 0 or closing_rate <= 0:
            blockers.append(f"{currency} FX rates must be positive")
            continue
        rates[str(currency).upper()] = {
            "pnl_rate": pnl_rate,
            "closing_rate": closing_rate,
            "source": source,
            "as_of": as_of,
        }
    return rates, blockers


def _translate(payload: dict[str, Any], context: ServiceContext) -> dict[str, Any]:
    period = _period(payload.get("period"))
    rows = payload.get("entity_balances") or []
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("entity_balances must be a list of objects")
    selected_entities = set(context.entity_ids)
    rates, blockers = _rates(payload, context)
    translated = []
    seen_entities = set()
    for index, row in enumerate(rows, 1):
        entity_id = str(row.get("entity_id") or "")
        if entity_id not in selected_entities:
            blockers.append(f"entity_balances[{index}] has entity outside management scope: {entity_id}")
            continue
        if entity_id in seen_entities:
            blockers.append(f"duplicate entity balance: {entity_id}")
            continue
        entity = context.runtime.entities.get(entity_id)
        currency = str(row.get("currency") or "").upper()
        if currency != entity.functional_currency:
            blockers.append(
                f"{entity_id} balance currency {currency} does not match functional currency {entity.functional_currency}"
            )
            continue
        rate = rates.get(currency)
        if rate is None:
            blockers.append(f"missing FX rates for {currency}")
            continue
        metrics = {}
        translated_metrics = {}
        for metric in METRICS:
            amount = _amount(row.get(metric, 0), f"entity_balances[{index}].{metric}")
            metric_rate = rate["pnl_rate"] if metric in PNL_METRICS else rate["closing_rate"]
            metrics[metric] = float(amount)
            translated_metrics[metric] = float((amount * metric_rate).quantize(MONEY))
        translated.append({
            "entity_id": entity_id,
            "legal_name": entity.legal_name,
            "period": period,
            "functional_currency": currency,
            "reporting_currency": context.scope["reporting_currency"],
            "source_metrics": metrics,
            "translated_metrics": translated_metrics,
            "pnl_rate": float(rate["pnl_rate"]),
            "closing_rate": float(rate["closing_rate"]),
            "fx_source": rate["source"],
            "fx_as_of": rate["as_of"],
            "statutory_books_modified": False,
        })
        seen_entities.add(entity_id)
    missing_entities = sorted(selected_entities - seen_entities)
    if missing_entities:
        blockers.append(f"missing entity balances: {', '.join(missing_entities)}")
    totals = {
        metric: round(sum(row["translated_metrics"][metric] for row in translated), 2)
        for metric in METRICS
    }
    totals["profit"] = round(totals["revenue"] - totals["expenses"], 2)
    return {
        "ready": not blockers,
        "period": period,
        "reporting_currency": context.scope["reporting_currency"],
        "translated_entities": translated,
        "pre_adjustment_totals": totals,
        "blockers": blockers,
    }


def _adjustments(payload: dict[str, Any], context: ServiceContext) -> dict[str, Any]:
    rows = payload.get("intercompany_adjustments") or []
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("intercompany_adjustments must be a list of objects")
    selected_entities = set(context.entity_ids)
    approved = []
    pending = []
    totals = {metric: Decimal("0") for metric in METRICS}
    duplicate_ids = []
    seen_ids = set()
    for index, row in enumerate(rows, 1):
        adjustment_id = str(row.get("id") or "")
        if not adjustment_id:
            pending.append({"row": index, "reason": "missing adjustment id"})
            continue
        if adjustment_id in seen_ids:
            duplicate_ids.append(adjustment_id)
            continue
        seen_ids.add(adjustment_id)
        from_entity = str(row.get("from_entity_id") or "")
        to_entity = str(row.get("to_entity_id") or "")
        metric = str(row.get("metric") or "")
        reasons = []
        if from_entity not in selected_entities or to_entity not in selected_entities:
            reasons.append("entities must both be inside management scope")
        if from_entity == to_entity:
            reasons.append("from and to entities must differ")
        if metric not in METRICS:
            reasons.append(f"metric must be one of {', '.join(METRICS)}")
        if row.get("approved") is not True:
            reasons.append("consolidation adjustment is not approved")
        if not row.get("evidence"):
            reasons.append("missing intercompany evidence")
        if reasons:
            pending.append({"id": adjustment_id, "reasons": reasons})
            continue
        amount = _amount(row.get("amount_reporting_currency"), f"intercompany_adjustments[{index}].amount")
        totals[metric] += amount
        approved.append({
            "id": adjustment_id,
            "from_entity_id": from_entity,
            "to_entity_id": to_entity,
            "metric": metric,
            "amount_reporting_currency": float(amount),
            "evidence": row["evidence"],
            "approved_by": row.get("approved_by"),
            "approved_at": row.get("approved_at"),
        })
    if duplicate_ids:
        pending.append({"reason": "duplicate adjustment ids", "ids": sorted(set(duplicate_ids))})
    return {
        "ready": not pending,
        "approved_adjustments": approved,
        "pending_adjustments": pending,
        "adjustment_totals": {metric: float(value.quantize(MONEY)) for metric, value in totals.items()},
    }


def translate_management_balances(payload: dict[str, Any], context: ServiceContext) -> dict[str, Any]:
    result = _translate(payload, context)
    return {
        **result,
        "scope": "management_translation_only",
        "statutory_books_modified": False,
    }


def review_intercompany_adjustments(payload: dict[str, Any], context: ServiceContext) -> dict[str, Any]:
    result = _adjustments(payload, context)
    return {
        **result,
        "entity_ids": list(context.entity_ids),
        "reporting_currency": context.scope["reporting_currency"],
        "review_gate": "consolidation_adjustment",
        "statutory_books_modified": False,
    }


def consolidate_management_view(payload: dict[str, Any], context: ServiceContext) -> dict[str, Any]:
    translated = _translate(payload, context)
    adjustments = _adjustments(payload, context)
    final_totals = {
        metric: round(
            translated["pre_adjustment_totals"][metric] + adjustments["adjustment_totals"][metric],
            2,
        )
        for metric in METRICS
    }
    final_totals["profit"] = round(final_totals["revenue"] - final_totals["expenses"], 2)
    blockers = list(translated["blockers"])
    if adjustments["pending_adjustments"]:
        blockers.append("intercompany adjustments require evidence and approval")
    return {
        "ready": translated["ready"] and adjustments["ready"],
        "period": translated["period"],
        "reporting_currency": translated["reporting_currency"],
        "translated_entities": translated["translated_entities"],
        "pre_adjustment_totals": translated["pre_adjustment_totals"],
        "approved_adjustments": adjustments["approved_adjustments"],
        "pending_adjustments": adjustments["pending_adjustments"],
        "adjustment_totals": adjustments["adjustment_totals"],
        "management_totals": final_totals,
        "blockers": blockers,
        "statutory_books_modified": False,
        "guardrail": "This is a management view; legal-entity books, tax, bank and approvals remain separate.",
    }
