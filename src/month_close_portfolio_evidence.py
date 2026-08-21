from __future__ import annotations

import hashlib
import json
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any


MONEY = Decimal("0.01")
SUMMARY_METRICS = (
    "cash", "assets", "liabilities", "revenue", "expenses",
    "profit_before_tax_candidate",
)


class MonthClosePortfolioEvidenceError(ValueError):
    """Raised when a month-close result cannot become a portfolio source."""


def _money(value: Any, field: str) -> str:
    if isinstance(value, bool) or value in (None, ""):
        raise MonthClosePortfolioEvidenceError(f"{field} must be a finite decimal")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise MonthClosePortfolioEvidenceError(f"{field} must be a finite decimal") from exc
    if not number.is_finite():
        raise MonthClosePortfolioEvidenceError(f"{field} must be a finite decimal")
    return format(number.quantize(MONEY, rounding=ROUND_HALF_UP), "f")


def normalize_month_close_portfolio_source(candidate: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(candidate, dict):
        raise MonthClosePortfolioEvidenceError("portfolio source candidate must be an object")
    summaries = candidate.get("currency_summaries")
    if not isinstance(summaries, list) or not summaries:
        raise MonthClosePortfolioEvidenceError("portfolio source requires currency_summaries")
    normalized_summaries = []
    seen = set()
    for index, summary in enumerate(summaries):
        if not isinstance(summary, dict):
            raise MonthClosePortfolioEvidenceError(
                f"currency_summaries[{index}] must be an object"
            )
        currency = str(summary.get("currency") or "").upper()
        if len(currency) != 3 or not currency.isalpha() or currency in seen:
            raise MonthClosePortfolioEvidenceError("currency summaries require unique ISO currency codes")
        seen.add(currency)
        bank_count = summary.get("bank_account_count")
        if isinstance(bank_count, bool) or not isinstance(bank_count, int) or bank_count < 0:
            raise MonthClosePortfolioEvidenceError("bank_account_count must be a non-negative integer")
        normalized_summaries.append({
            "currency": currency,
            "bank_account_count": bank_count,
            **{
                metric: _money(summary.get(metric), f"currency_summaries[{index}].{metric}")
                for metric in SUMMARY_METRICS
            },
        })
    return {
        "entity_id": str(candidate.get("entity_id") or ""),
        "period": str(candidate.get("period") or ""),
        "source_pipeline_id": str(candidate.get("source_pipeline_id") or ""),
        "source_run_id": str(candidate.get("source_run_id") or ""),
        "close_control_ready_for_review": candidate.get("close_control_ready_for_review") is True,
        "candidate_only": candidate.get("candidate_only") is True,
        "posting_performed": candidate.get("posting_performed") is True,
        "period_close_performed": candidate.get("period_close_performed") is True,
        "blocker_count": len(candidate.get("blockers") or []),
        "currency_summaries": sorted(normalized_summaries, key=lambda item: item["currency"]),
    }


def month_close_portfolio_source_fingerprint(candidate: dict[str, Any]) -> str:
    canonical = json.dumps(
        normalize_month_close_portfolio_source(candidate),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def month_close_result_to_portfolio_source(result: dict[str, Any]) -> dict[str, Any]:
    pipeline = result.get("pipeline") or {}
    briefing = result.get("founder_briefing") or {}
    if pipeline.get("pipeline_id") != "finance.month_close_control":
        raise MonthClosePortfolioEvidenceError("result is not finance.month_close_control")
    summaries = []
    for index, raw in enumerate(briefing.get("currency_summaries") or []):
        if not isinstance(raw, dict):
            raise MonthClosePortfolioEvidenceError(f"currency_summaries[{index}] must be an object")
        summaries.append({
            "currency": raw.get("currency"),
            "bank_account_count": raw.get("bank_account_count"),
            "cash": raw.get("statement_cash_total"),
            "assets": raw.get("assets"),
            "liabilities": raw.get("liabilities"),
            "revenue": raw.get("revenue"),
            "expenses": raw.get("expenses"),
            "profit_before_tax_candidate": raw.get("profit_before_tax_candidate"),
        })
    return {
        "entity_id": briefing.get("entity_id"),
        "period": briefing.get("period"),
        "source_pipeline_id": pipeline.get("pipeline_id"),
        "source_run_id": pipeline.get("run_id"),
        "close_control_ready_for_review": briefing.get("close_control_ready_for_review") is True,
        "candidate_only": briefing.get("candidate_only") is True,
        "posting_performed": result.get("posting_performed") is True,
        "period_close_performed": result.get("period_close_performed") is True,
        "blockers": list(result.get("blockers") or []),
        "currency_summaries": summaries,
    }


def verify_portfolio_source_record(
    record: dict[str, Any], candidate: dict[str, Any],
) -> None:
    if record.get("pipeline_id") != "finance.month_close_control":
        raise MonthClosePortfolioEvidenceError("source attempt is not a month-close control run")
    if record.get("status") != "ready" or record.get("ready") is not True:
        raise MonthClosePortfolioEvidenceError("source month-close attempt is not ready")
    if record.get("review_complete") is not True or record.get("release_candidate") is not True:
        raise MonthClosePortfolioEvidenceError(
            "source month-close attempt has not completed every required review gate"
        )
    if record.get("entity_id") != candidate.get("entity_id"):
        raise MonthClosePortfolioEvidenceError("source attempt entity does not match candidate")
    if record.get("run_id") != candidate.get("source_run_id"):
        raise MonthClosePortfolioEvidenceError("source attempt run id does not match candidate")
    supplied = month_close_portfolio_source_fingerprint(candidate)
    if record.get("portfolio_source_fingerprint") != supplied:
        raise MonthClosePortfolioEvidenceError(
            "source candidate summary does not match the recorded month-close result fingerprint"
        )
