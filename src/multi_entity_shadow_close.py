from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .box_runtime import BoxRuntime
from .shadow_close import validate_shadow_close_report
from .shadow_close_artifacts import _read_json, _write_private_json


HEX64_PATTERN = re.compile(r"^[0-9a-f]{64}$")
RUN_ID_PATTERN = re.compile(r"^[0-9a-f]{24}$")
PERIOD_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
REVIEW_DECISIONS = {"passed", "accepted-differences", "needs-correction"}
ACCEPTED_ENTITY_DECISIONS = {"验证通过", "接受差异"}


class MultiEntityShadowCloseError(ValueError):
    """Raised when a portfolio Shadow Close acceptance artifact is not trustworthy."""


def _canonical(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise MultiEntityShadowCloseError(
            "multi-entity Shadow Close input must be JSON-serializable"
        ) from exc


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _text(value: Any, field: str, *, minimum: int = 1, maximum: int = 1000) -> str:
    text = str(value or "").strip()
    if not minimum <= len(text) <= maximum or any(ord(char) < 32 for char in text):
        raise MultiEntityShadowCloseError(
            f"{field} must be {minimum}-{maximum} printable characters"
        )
    return text


def _evidence(value: Iterable[str]) -> list[str]:
    if isinstance(value, (str, bytes)):
        raise MultiEntityShadowCloseError("portfolio review evidence must be a list")
    references = [
        _text(item, "portfolio review evidence reference", maximum=500)
        for item in value
    ]
    if not references:
        raise MultiEntityShadowCloseError(
            "portfolio review requires at least one evidence reference"
        )
    if len(references) != len(set(references)):
        raise MultiEntityShadowCloseError("portfolio review evidence references must be unique")
    return references


def _manifest_fingerprint(manifest: dict[str, Any]) -> str:
    fields = {
        key: manifest.get(key)
        for key in (
            "schema_version", "artifact_type", "runtime_fingerprint", "period",
            "entity_ids", "entity_reports", "portfolio", "status",
            "raw_financial_values_persisted", "source_artifacts_input_only",
            "statutory_books_modified", "posting_performed", "period_close_performed",
            "external_filing_performed", "external_actions_performed", "guardrail",
        )
    }
    return _hash(fields)


def _review_fingerprint(review: dict[str, Any]) -> str:
    return _hash({
        key: review.get(key)
        for key in (
            "id", "manifest_fingerprint", "decision", "actor", "rationale",
            "evidence_references", "reviewed_at", "scope_note",
        )
    })


def _validate_portfolio_result(
    portfolio: dict[str, Any], *, runtime_fingerprint: str, period: str,
    entity_ids: list[str],
) -> dict[str, Any]:
    if not isinstance(portfolio, dict):
        raise MultiEntityShadowCloseError("portfolio result must be a JSON object")
    pipeline = portfolio.get("pipeline")
    if not isinstance(pipeline, dict) or pipeline.get("pipeline_id") != (
        "finance.multi_entity_month_close_portfolio"
    ):
        raise MultiEntityShadowCloseError(
            "portfolio result is not finance.multi_entity_month_close_portfolio"
        )
    run_id = str(pipeline.get("run_id") or "")
    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise MultiEntityShadowCloseError("portfolio result run_id is invalid")
    if pipeline.get("required_review_gates") != ["month_close_portfolio_review"]:
        raise MultiEntityShadowCloseError(
            "portfolio result must retain the month_close_portfolio_review gate"
        )
    if portfolio.get("ready") is not True or portfolio.get("blocked_at") is not None:
        raise MultiEntityShadowCloseError("portfolio result is not ready")
    if portfolio.get("source_run_ledger_verified") is not True:
        raise MultiEntityShadowCloseError(
            "portfolio result sources were not verified against the Pipeline ledger"
        )
    verification = portfolio.get("source_run_ledger_verification")
    if not isinstance(verification, dict) or verification.get("verified") is not True:
        raise MultiEntityShadowCloseError("portfolio source ledger verification is missing")
    if verification.get("raw_pipeline_results_persisted") is not False:
        raise MultiEntityShadowCloseError(
            "portfolio source ledger must not persist raw pipeline results"
        )
    chain_head = str(verification.get("chain_head") or "")
    if not HEX64_PATTERN.fullmatch(chain_head):
        raise MultiEntityShadowCloseError("portfolio source ledger chain head is invalid")
    sources = verification.get("sources")
    if not isinstance(sources, list) or len(sources) != len(entity_ids):
        raise MultiEntityShadowCloseError(
            "portfolio source ledger verification does not cover every entity"
        )
    source_entities: list[str] = []
    source_attempt_ids: list[str] = []
    for source in sources:
        if not isinstance(source, dict):
            raise MultiEntityShadowCloseError("portfolio source verification entry is invalid")
        entity_id = str(source.get("entity_id") or "")
        attempt_id = str(source.get("attempt_id") or "")
        source_run_id = str(source.get("run_id") or "")
        if not RUN_ID_PATTERN.fullmatch(attempt_id) or not RUN_ID_PATTERN.fullmatch(source_run_id):
            raise MultiEntityShadowCloseError("portfolio source attempt or run id is invalid")
        if source.get("review_complete") is not True:
            raise MultiEntityShadowCloseError("portfolio source review is incomplete")
        for field in ("result_fingerprint", "portfolio_source_fingerprint"):
            if not HEX64_PATTERN.fullmatch(str(source.get(field) or "")):
                raise MultiEntityShadowCloseError(
                    f"portfolio source {field} is invalid"
                )
        source_entities.append(entity_id)
        source_attempt_ids.append(attempt_id)
    if sorted(source_entities) != entity_ids or len(set(source_attempt_ids)) != len(entity_ids):
        raise MultiEntityShadowCloseError(
            "portfolio verified source scope does not match configured entities"
        )
    if verification.get("source_count") != len(entity_ids):
        raise MultiEntityShadowCloseError("portfolio verified source count is inconsistent")
    lineage = portfolio.get("lineage")
    if not isinstance(lineage, dict):
        raise MultiEntityShadowCloseError("portfolio result lineage is missing")
    if lineage.get("period") != period or sorted(lineage.get("entity_ids") or []) != entity_ids:
        raise MultiEntityShadowCloseError("portfolio lineage scope does not match the acceptance scope")
    if lineage.get("source_run_ledger_verified") is not True:
        raise MultiEntityShadowCloseError("portfolio lineage is not ledger verified")
    if sorted(lineage.get("source_attempt_ids") or []) != sorted(source_attempt_ids):
        raise MultiEntityShadowCloseError("portfolio lineage source attempts are inconsistent")
    briefing = portfolio.get("founder_briefing")
    if not isinstance(briefing, dict):
        raise MultiEntityShadowCloseError("portfolio founder briefing is missing")
    if briefing.get("period") != period:
        raise MultiEntityShadowCloseError("portfolio period does not match Shadow Close period")
    if briefing.get("entity_count") != len(entity_ids) or briefing.get("ready_entity_count") != len(entity_ids):
        raise MultiEntityShadowCloseError("portfolio is not ready for every configured entity")
    readiness = briefing.get("statutory_readiness")
    if not isinstance(readiness, list) or sorted(
        str(item.get("entity_id") or "") for item in readiness if isinstance(item, dict)
    ) != entity_ids:
        raise MultiEntityShadowCloseError("portfolio statutory readiness scope is incomplete")
    if any(item.get("ready_for_portfolio_review") is not True for item in readiness):
        raise MultiEntityShadowCloseError("one or more entity close candidates are not ready")
    if briefing.get("management_portfolio_totals") is None:
        raise MultiEntityShadowCloseError("portfolio management totals were suppressed by blockers")
    required_false = (
        "external_actions_performed", "statutory_books_modified", "posting_performed",
        "period_close_performed", "external_filing_performed",
    )
    if any(portfolio.get(field) is not False for field in required_false):
        raise MultiEntityShadowCloseError(
            "portfolio result crossed a read-only finance boundary"
        )
    if (
        briefing.get("candidate_only") is not True
        or briefing.get("pre_elimination_view") is not True
        or briefing.get("cross_entity_native_currency_netting_performed") is not False
        or briefing.get("consolidated_financial_statements_produced") is not False
        or briefing.get("posting_or_period_close_performed") is not False
    ):
        raise MultiEntityShadowCloseError("portfolio management-view guardrails are invalid")
    return {
        "pipeline_id": pipeline["pipeline_id"],
        "run_id": run_id,
        "result_fingerprint": _hash(portfolio),
        "source_ledger_chain_head": chain_head,
        "source_attempt_ids": sorted(source_attempt_ids),
        "source_count": len(source_attempt_ids),
        "source_run_ledger_verified": True,
        "ready": True,
        "candidate_only": True,
        "pre_elimination_view": True,
        "raw_pipeline_results_persisted": False,
    }


def validate_multi_entity_shadow_close_manifest(
    runtime: BoxRuntime, manifest: dict[str, Any], *, require_review: bool = False,
) -> dict[str, Any]:
    """Validate one secret-safe manifest and its exact-fingerprint independent review."""
    if not isinstance(manifest, dict):
        raise MultiEntityShadowCloseError("multi-entity Shadow Close manifest must be an object")
    snapshot = runtime.snapshot()
    configured_entity_ids = sorted(item["id"] for item in snapshot["entities"])
    if len(configured_entity_ids) < 2:
        raise MultiEntityShadowCloseError("multi-entity Shadow Close requires at least two entities")
    if manifest.get("schema_version") != 1 or manifest.get("artifact_type") != (
        "multi_entity_shadow_close_acceptance"
    ):
        raise MultiEntityShadowCloseError("multi-entity Shadow Close manifest contract is invalid")
    if manifest.get("runtime_fingerprint") != snapshot["fingerprint"]:
        raise MultiEntityShadowCloseError(
            "multi-entity Shadow Close manifest belongs to a different Box runtime fingerprint"
        )
    period = str(manifest.get("period") or "")
    if not PERIOD_PATTERN.fullmatch(period):
        raise MultiEntityShadowCloseError("multi-entity Shadow Close period must use YYYY-MM")
    if manifest.get("entity_ids") != configured_entity_ids:
        raise MultiEntityShadowCloseError(
            "multi-entity Shadow Close must cover every configured entity exactly once"
        )
    summaries = manifest.get("entity_reports")
    if not isinstance(summaries, list) or len(summaries) != len(configured_entity_ids):
        raise MultiEntityShadowCloseError("entity report summaries are incomplete")
    summary_entities = [str(item.get("entity_id") or "") for item in summaries if isinstance(item, dict)]
    if summary_entities != configured_entity_ids:
        raise MultiEntityShadowCloseError("entity report summaries must be uniquely sorted by entity")
    exception_count = 0
    entity_reviewers: list[str] = []
    for summary in summaries:
        expected_fields = {
            "entity_id", "baseline_id", "report_fingerprint", "review_id", "decision",
            "review_actor", "comparison_count", "matched_count", "exception_count",
            "domain_summary",
        }
        if set(summary) != expected_fields:
            raise MultiEntityShadowCloseError("entity report summary fields are invalid")
        if summary.get("decision") not in ACCEPTED_ENTITY_DECISIONS:
            raise MultiEntityShadowCloseError("entity Shadow Close report is not accepted")
        if not HEX64_PATTERN.fullmatch(str(summary.get("report_fingerprint") or "")):
            raise MultiEntityShadowCloseError("entity report fingerprint is invalid")
        if not str(summary.get("review_id") or "").startswith("SHADOW-REVIEW-"):
            raise MultiEntityShadowCloseError("entity report review id is invalid")
        counts = [summary.get(field) for field in ("comparison_count", "matched_count", "exception_count")]
        if any(isinstance(item, bool) or not isinstance(item, int) or item < 0 for item in counts):
            raise MultiEntityShadowCloseError("entity Shadow Close counts are invalid")
        if counts[0] != counts[1] + counts[2] or counts[0] <= 0:
            raise MultiEntityShadowCloseError("entity Shadow Close counts are inconsistent")
        if summary.get("decision") == "验证通过" and counts[2] != 0:
            raise MultiEntityShadowCloseError(
                "a clean entity Shadow Close review cannot contain exceptions"
            )
        if summary.get("decision") == "接受差异" and counts[2] == 0:
            raise MultiEntityShadowCloseError(
                "an accepted-differences entity review requires exceptions"
            )
        domain_summary = summary.get("domain_summary")
        if not isinstance(domain_summary, list) or not domain_summary:
            raise MultiEntityShadowCloseError("entity Shadow Close domain summary is missing")
        domain_count = domain_matched = domain_exceptions = 0
        seen_domains: set[str] = set()
        for domain in domain_summary:
            if not isinstance(domain, dict) or set(domain) != {
                "domain", "label", "count", "matched", "exceptions",
            }:
                raise MultiEntityShadowCloseError("entity Shadow Close domain summary is invalid")
            domain_name = str(domain.get("domain") or "")
            if domain_name not in {"trial_balance", "statement", "tax"} or domain_name in seen_domains:
                raise MultiEntityShadowCloseError("entity Shadow Close domains are invalid or duplicated")
            seen_domains.add(domain_name)
            domain_counts = [domain.get(field) for field in ("count", "matched", "exceptions")]
            if any(
                isinstance(item, bool) or not isinstance(item, int) or item < 0
                for item in domain_counts
            ) or domain_counts[0] != domain_counts[1] + domain_counts[2]:
                raise MultiEntityShadowCloseError("entity Shadow Close domain counts are inconsistent")
            domain_count += domain_counts[0]
            domain_matched += domain_counts[1]
            domain_exceptions += domain_counts[2]
        if [domain_count, domain_matched, domain_exceptions] != counts:
            raise MultiEntityShadowCloseError(
                "entity Shadow Close domain totals do not match report counts"
            )
        actor = _text(summary.get("review_actor"), "entity Shadow Close reviewer", maximum=80)
        entity_reviewers.append(actor)
        exception_count += counts[2]
    portfolio = manifest.get("portfolio")
    expected_portfolio_fields = {
        "pipeline_id", "run_id", "result_fingerprint", "source_ledger_chain_head",
        "source_attempt_ids", "source_count", "source_run_ledger_verified", "ready",
        "candidate_only", "pre_elimination_view", "raw_pipeline_results_persisted",
    }
    if not isinstance(portfolio, dict) or set(portfolio) != expected_portfolio_fields:
        raise MultiEntityShadowCloseError("portfolio summary fields are invalid")
    if portfolio.get("pipeline_id") != "finance.multi_entity_month_close_portfolio":
        raise MultiEntityShadowCloseError("portfolio summary pipeline is invalid")
    if not RUN_ID_PATTERN.fullmatch(str(portfolio.get("run_id") or "")):
        raise MultiEntityShadowCloseError("portfolio summary run id is invalid")
    for field in ("result_fingerprint", "source_ledger_chain_head"):
        if not HEX64_PATTERN.fullmatch(str(portfolio.get(field) or "")):
            raise MultiEntityShadowCloseError(f"portfolio summary {field} is invalid")
    attempts = portfolio.get("source_attempt_ids")
    if (
        not isinstance(attempts, list)
        or len(attempts) != len(configured_entity_ids)
        or len(set(attempts)) != len(attempts)
        or any(not RUN_ID_PATTERN.fullmatch(str(item or "")) for item in attempts)
        or attempts != sorted(attempts)
    ):
        raise MultiEntityShadowCloseError("portfolio source attempt ids are invalid")
    if portfolio.get("source_count") != len(attempts):
        raise MultiEntityShadowCloseError("portfolio source count is inconsistent")
    for field in (
        "source_run_ledger_verified", "ready", "candidate_only", "pre_elimination_view",
    ):
        if portfolio.get(field) is not True:
            raise MultiEntityShadowCloseError(f"portfolio summary {field} must be true")
    if portfolio.get("raw_pipeline_results_persisted") is not False:
        raise MultiEntityShadowCloseError("portfolio raw pipeline results must not be persisted")
    for field in (
        "raw_financial_values_persisted", "statutory_books_modified", "posting_performed",
        "period_close_performed", "external_filing_performed", "external_actions_performed",
    ):
        if manifest.get(field) is not False:
            raise MultiEntityShadowCloseError(f"manifest {field} must be false")
    if manifest.get("source_artifacts_input_only") is not True:
        raise MultiEntityShadowCloseError("source artifacts must remain input-only")
    if manifest.get("status") != "ready_for_independent_review":
        raise MultiEntityShadowCloseError("multi-entity Shadow Close manifest status is invalid")
    expected_fingerprint = _manifest_fingerprint(manifest)
    if manifest.get("manifest_fingerprint") != expected_fingerprint:
        raise MultiEntityShadowCloseError("multi-entity Shadow Close manifest fingerprint mismatch")
    review = manifest.get("review")
    review_current = manifest.get("review_current") is True
    if require_review and not review_current:
        raise MultiEntityShadowCloseError("multi-entity Shadow Close manifest is not independently reviewed")
    if review_current:
        if not isinstance(review, dict):
            raise MultiEntityShadowCloseError("multi-entity Shadow Close review is missing")
        expected_review_fields = {
            "id", "manifest_fingerprint", "decision", "actor", "rationale",
            "evidence_references", "reviewed_at", "scope_note", "review_fingerprint",
        }
        if set(review) != expected_review_fields:
            raise MultiEntityShadowCloseError("multi-entity Shadow Close review fields are invalid")
        if review.get("manifest_fingerprint") != expected_fingerprint:
            raise MultiEntityShadowCloseError("portfolio review does not bind the current manifest")
        decision = review.get("decision")
        if decision not in REVIEW_DECISIONS:
            raise MultiEntityShadowCloseError("portfolio review decision is invalid")
        actor = _text(review.get("actor"), "portfolio reviewer", maximum=80)
        if actor in set(entity_reviewers):
            raise MultiEntityShadowCloseError(
                "portfolio reviewer must be independent from every entity Shadow Close reviewer"
            )
        _text(review.get("rationale"), "portfolio review rationale", minimum=12, maximum=1000)
        _evidence(review.get("evidence_references") or [])
        reviewed_at = str(review.get("reviewed_at") or "")
        try:
            parsed_reviewed_at = datetime.fromisoformat(reviewed_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise MultiEntityShadowCloseError(
                "portfolio review reviewed_at must be an ISO-8601 timestamp"
            ) from exc
        if parsed_reviewed_at.tzinfo is None:
            raise MultiEntityShadowCloseError(
                "portfolio review reviewed_at must include a timezone"
            )
        expected_review_id = "PORTFOLIO-SHADOW-REVIEW-" + hashlib.sha1(
            f"{expected_fingerprint}|{actor}|{reviewed_at}".encode("utf-8")
        ).hexdigest()[:16]
        if review.get("id") != expected_review_id:
            raise MultiEntityShadowCloseError("portfolio review id does not match its signed scope")
        if review.get("review_fingerprint") != _review_fingerprint(review):
            raise MultiEntityShadowCloseError("portfolio review fingerprint mismatch")
        if decision == "passed" and exception_count:
            raise MultiEntityShadowCloseError(
                "portfolio with accepted entity differences cannot be reviewed as passed"
            )
        if decision == "passed" and any(
            item.get("decision") != "验证通过" for item in summaries
        ):
            raise MultiEntityShadowCloseError(
                "portfolio passed requires every entity report to be verified cleanly"
            )
        if decision == "accepted-differences" and not exception_count:
            raise MultiEntityShadowCloseError(
                "accepted-differences requires at least one accepted entity exception"
            )
    elif review is not None:
        raise MultiEntityShadowCloseError("non-current portfolio review cannot be attached")
    return {
        "valid": True,
        "manifest_fingerprint": expected_fingerprint,
        "entity_count": len(configured_entity_ids),
        "comparison_count": sum(item["comparison_count"] for item in summaries),
        "exception_count": exception_count,
        "entity_reviewers": entity_reviewers,
        "review_current": review_current,
    }


def assemble_multi_entity_shadow_close_artifact(
    runtime: BoxRuntime,
    entity_report_paths: Iterable[str | Path],
    portfolio_result_path: str | Path,
    output: str | Path,
) -> dict[str, Any]:
    """Build a no-values acceptance manifest from reviewed entity reports and a verified portfolio."""
    runtime.reload()
    snapshot = runtime.snapshot()
    configured_entity_ids = sorted(item["id"] for item in snapshot["entities"])
    if len(configured_entity_ids) < 2:
        raise MultiEntityShadowCloseError("multi-entity Shadow Close requires at least two entities")
    reports: list[dict[str, Any]] = []
    periods: set[str] = set()
    seen_entities: set[str] = set()
    for path in entity_report_paths:
        report = _read_json(path)
        if not isinstance(report, dict):
            raise MultiEntityShadowCloseError("entity Shadow Close report must be a JSON object")
        try:
            integrity = validate_shadow_close_report(report)
        except ValueError as exc:
            raise MultiEntityShadowCloseError(str(exc)) from exc
        entity_id = str(report.get("entity_id") or "")
        if entity_id not in configured_entity_ids or entity_id in seen_entities:
            raise MultiEntityShadowCloseError(
                "entity Shadow Close reports must cover configured entities exactly once"
            )
        if report.get("runtime_fingerprint") != snapshot["fingerprint"]:
            raise MultiEntityShadowCloseError(
                "entity Shadow Close report belongs to a different Box runtime fingerprint"
            )
        review = report.get("review") if report.get("review_current") is True else None
        if not isinstance(review, dict) or review.get("decision") not in ACCEPTED_ENTITY_DECISIONS:
            raise MultiEntityShadowCloseError(
                "every entity Shadow Close report must have a current accepted review"
            )
        seen_entities.add(entity_id)
        periods.add(str(report.get("period") or ""))
        reports.append({
            "entity_id": entity_id,
            "baseline_id": report["baseline_id"],
            "report_fingerprint": integrity["report_fingerprint"],
            "review_id": review["id"],
            "decision": review["decision"],
            "review_actor": review["actor"],
            "comparison_count": integrity["comparison_count"],
            "matched_count": integrity["matched_count"],
            "exception_count": integrity["exception_count"],
            "domain_summary": report["domain_summary"],
        })
    if seen_entities != set(configured_entity_ids):
        missing = sorted(set(configured_entity_ids) - seen_entities)
        raise MultiEntityShadowCloseError(
            "entity Shadow Close reports do not cover every configured entity: " + ", ".join(missing)
        )
    if len(periods) != 1:
        raise MultiEntityShadowCloseError("entity Shadow Close reports must use one common period")
    period = next(iter(periods))
    portfolio_result = _read_json(portfolio_result_path)
    if (
        isinstance(portfolio_result, dict)
        and portfolio_result.get("ok") is True
        and isinstance(portfolio_result.get("result"), dict)
    ):
        portfolio_result = portfolio_result["result"]
    portfolio_summary = _validate_portfolio_result(
        portfolio_result,
        runtime_fingerprint=snapshot["fingerprint"],
        period=period,
        entity_ids=configured_entity_ids,
    )
    manifest = {
        "schema_version": 1,
        "artifact_type": "multi_entity_shadow_close_acceptance",
        "runtime_fingerprint": snapshot["fingerprint"],
        "period": period,
        "entity_ids": configured_entity_ids,
        "entity_reports": sorted(reports, key=lambda item: item["entity_id"]),
        "portfolio": portfolio_summary,
        "status": "ready_for_independent_review",
        "raw_financial_values_persisted": False,
        "source_artifacts_input_only": True,
        "statutory_books_modified": False,
        "posting_performed": False,
        "period_close_performed": False,
        "external_filing_performed": False,
        "external_actions_performed": False,
        "guardrail": (
            "This private manifest binds reviewed entity Shadow Close reports to one ledger-verified "
            "management portfolio by fingerprint only. It stores no comparison or portfolio amounts, "
            "does not consolidate statutory books and cannot promote a Pack automatically."
        ),
        "review": None,
        "review_current": False,
    }
    manifest["manifest_fingerprint"] = _manifest_fingerprint(manifest)
    integrity = validate_multi_entity_shadow_close_manifest(runtime, manifest)
    destination = _write_private_json(output, manifest)
    return {
        "output": str(destination),
        "runtime_fingerprint": snapshot["fingerprint"],
        "period": period,
        "entity_count": integrity["entity_count"],
        "comparison_count": integrity["comparison_count"],
        "exception_count": integrity["exception_count"],
        "manifest_fingerprint": integrity["manifest_fingerprint"],
        "review_current": False,
        "raw_financial_values_written_to_output": False,
        "raw_financial_values_returned": False,
        "external_actions_performed": False,
    }


def review_multi_entity_shadow_close_artifact(
    runtime: BoxRuntime,
    manifest_path: str | Path,
    output: str | Path,
    *,
    decision: str,
    actor: str,
    rationale: str,
    evidence_references: Iterable[str],
) -> dict[str, Any]:
    """Independently review one exact no-values portfolio acceptance manifest."""
    manifest = _read_json(manifest_path)
    integrity = validate_multi_entity_shadow_close_manifest(runtime, manifest)
    if manifest.get("review_current") is True:
        raise MultiEntityShadowCloseError(
            "multi-entity Shadow Close manifest is already reviewed; assemble a new manifest"
        )
    if decision not in REVIEW_DECISIONS:
        raise MultiEntityShadowCloseError(
            "decision must be passed, accepted-differences or needs-correction"
        )
    actor = _text(actor, "portfolio reviewer", maximum=80)
    if actor in set(integrity["entity_reviewers"]):
        raise MultiEntityShadowCloseError(
            "portfolio reviewer must be independent from every entity Shadow Close reviewer"
        )
    rationale = _text(rationale, "portfolio review rationale", minimum=12, maximum=1000)
    evidence = _evidence(evidence_references)
    if decision == "passed" and integrity["exception_count"]:
        raise MultiEntityShadowCloseError(
            "portfolio with accepted entity differences cannot be reviewed as passed"
        )
    if decision == "passed" and any(
        item["decision"] != "验证通过" for item in manifest["entity_reports"]
    ):
        raise MultiEntityShadowCloseError(
            "portfolio passed requires every entity report to be verified cleanly"
        )
    if decision == "accepted-differences" and not integrity["exception_count"]:
        raise MultiEntityShadowCloseError(
            "accepted-differences requires at least one accepted entity exception"
        )
    reviewed_at = datetime.now(timezone.utc).isoformat()
    review_id = "PORTFOLIO-SHADOW-REVIEW-" + hashlib.sha1(
        f"{integrity['manifest_fingerprint']}|{actor}|{reviewed_at}".encode("utf-8")
    ).hexdigest()[:16]
    reviewed = dict(manifest)
    review = {
        "id": review_id,
        "manifest_fingerprint": integrity["manifest_fingerprint"],
        "decision": decision,
        "actor": actor,
        "rationale": rationale,
        "evidence_references": evidence,
        "reviewed_at": reviewed_at,
        "scope_note": (
            "This decision applies only to the exact entity reports, portfolio result and source "
            "ledger chain bound by the manifest fingerprint. Any input change requires reassembly."
        ),
    }
    review["review_fingerprint"] = _review_fingerprint(review)
    reviewed["review"] = review
    reviewed["review_current"] = True
    validate_multi_entity_shadow_close_manifest(runtime, reviewed, require_review=True)
    destination = _write_private_json(output, reviewed)
    return {
        "output": str(destination),
        "runtime_fingerprint": reviewed["runtime_fingerprint"],
        "period": reviewed["period"],
        "entity_count": integrity["entity_count"],
        "comparison_count": integrity["comparison_count"],
        "exception_count": integrity["exception_count"],
        "manifest_fingerprint": integrity["manifest_fingerprint"],
        "review_id": review_id,
        "decision": decision,
        "review_actor": actor,
        "review_current": True,
        "raw_financial_values_written_to_output": False,
        "raw_financial_values_returned": False,
        "external_actions_performed": False,
    }


def verify_multi_entity_shadow_close_artifact(
    runtime: BoxRuntime, manifest_path: str | Path,
) -> dict[str, Any]:
    """Verify a reviewed portfolio acceptance package without returning finance values."""
    manifest = _read_json(manifest_path)
    integrity = validate_multi_entity_shadow_close_manifest(
        runtime, manifest, require_review=True,
    )
    review = manifest["review"]
    return {
        "valid": True,
        "runtime_fingerprint": manifest["runtime_fingerprint"],
        "period": manifest["period"],
        "entity_count": integrity["entity_count"],
        "comparison_count": integrity["comparison_count"],
        "exception_count": integrity["exception_count"],
        "manifest_fingerprint": integrity["manifest_fingerprint"],
        "portfolio_run_id": manifest["portfolio"]["run_id"],
        "portfolio_result_fingerprint": manifest["portfolio"]["result_fingerprint"],
        "source_ledger_chain_head": manifest["portfolio"]["source_ledger_chain_head"],
        "review_id": review["id"],
        "decision": review["decision"],
        "review_current": True,
        "raw_financial_values_returned": False,
        "external_actions_performed": False,
    }
