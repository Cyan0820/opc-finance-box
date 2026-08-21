from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

try:
    import fcntl
except ImportError:  # pragma: no cover - supported production targets are POSIX
    fcntl = None

from .box_runtime import BoxRuntime
from .connector_shadow_artifacts import (
    ConnectorShadowArtifactError,
    verify_connector_shadow_artifact,
)
from .default_connectors import build_box_connector_registry
from .multi_entity_shadow_close import (
    MultiEntityShadowCloseError,
    validate_multi_entity_shadow_close_manifest,
)
from .pilot_shadow_series import (
    PilotShadowSeriesError,
    verify_pilot_shadow_series_for_promotion,
)


MAX_LEDGER_BYTES = 32 * 1024 * 1024
MAX_EVENT_BYTES = 256 * 1024
MAX_EVENTS = 50_000
MAX_ASSESSMENT_REVIEW_AGE_DAYS = 7
ACTOR_PATTERN = re.compile(r"^[^\x00-\x1f\x7f]{1,80}$")
PACK_PATTERN = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")
HEX64_PATTERN = re.compile(r"^[0-9a-f]{64}$")
ASSESSMENT_PATTERN = re.compile(r"^[0-9a-f]{24}$")
PERIOD_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
SECRET_PATTERN = re.compile(
    r"(?:secret|token|password|authorization|api[_-]?key|credential|bearer\s|"
    r"sk_|rk_|shpat_)",
    re.I,
)

AUTOMATED_PROMOTION_GATES = (
    "unit_and_contract_tests",
    "pack_provider_audit",
    "finance_boundary_eval",
    "box_doctor",
    "installable_distribution",
    "upgrade_compatibility",
    "runtime_data_layout_preflight",
    "pipeline_ledger_integrity_and_review_state",
    "connector_sync_checkpoint_and_quarantine_controls",
    "isolated_workbench_smoke",
    "deployment_starter_controls",
)
REQUIRED_REHEARSALS = (
    "backup_restore",
    "upgrade_rollback",
    "authorization_separation",
    "incident_recovery",
)
REPORT_DOMAINS = {"trial_balance", "statement", "tax"}
EXCEPTION_CLASSIFICATIONS = {
    "mapping", "cutoff", "accounting_policy", "source_evidence",
    "timing", "foreign_exchange", "accepted_scope", "system_defect",
}
REVIEW_DECISIONS = {"approved", "rejected", "needs_more_evidence"}
CONNECTOR_SHADOW_DECISIONS = {"passed", "accepted-differences", "needs-correction"}
PERSISTED_ASSESSMENT_FIELDS = {
    "schema_version", "evaluated_at", "runtime_fingerprint", "pack_id",
    "pack_version", "current_pack_status", "target_status", "prepared_by",
    "threshold_approved_by", "evidence_fingerprint", "sample_fingerprint",
    "pilot_shadow_series_summary", "report_summaries",
    "portfolio_shadow_summaries", "connector_shadow_summaries",
    "gate_evidence_fingerprints",
    "rehearsal_evidence_fingerprints", "known_limitation_count",
    "known_limitation_fingerprint", "metrics", "thresholds", "blockers",
    "warnings", "candidate_eligible", "separation_principals",
    "raw_shadow_reports_persisted", "raw_portfolio_shadow_manifests_persisted",
    "raw_connector_shadow_artifacts_persisted",
    "raw_pilot_shadow_series_artifacts_persisted",
    "raw_financial_values_persisted",
    "pack_manifest_changed", "external_actions_performed", "assessment_id",
}


class ReleasePromotionError(RuntimeError):
    """Raised when stable-promotion evidence or its review ledger is not trustworthy."""


def build_stable_promotion_evidence_template(
    runtime: BoxRuntime,
    pack_id: str,
) -> dict[str, Any]:
    """Return an editable, deliberately incomplete evidence input for one selected Pack."""
    if not isinstance(pack_id, str) or not PACK_PATTERN.fullmatch(pack_id):
        raise ReleasePromotionError("stable promotion template pack_id is invalid")
    runtime.reload()
    snapshot = runtime.snapshot()
    packs = {item["id"]: item for item in snapshot["packs"]}
    pack = packs.get(pack_id)
    if pack is None:
        raise ReleasePromotionError(
            "stable promotion template target Pack is not selected by this Box"
        )
    if pack.get("status") == "stable":
        raise ReleasePromotionError(
            "a Pack already marked stable does not require a stable promotion template"
        )
    entity_ids = [item["id"] for item in snapshot["entities"]]
    if str(pack.get("kind") or "") == "jurisdiction":
        entity_ids = [
            item["id"] for item in snapshot["entities"]
            if item.get("tax_pack") == pack_id
        ]
    if not entity_ids:
        raise ReleasePromotionError(
            "stable promotion template target Pack has no eligible legal entity in this Box"
        )
    fingerprint = snapshot["fingerprint"]
    return {
        "schema_version": 1,
        "runtime_fingerprint": fingerprint,
        "pack_id": pack_id,
        "pack_version": pack["version"],
        "prepared_by": "REQUIRED_EVIDENCE_PREPARER",
        "sample": {
            "description": "REQUIRED_ANONYMIZED_REPRESENTATIVE_SAMPLE_DESCRIPTION",
            "anonymized": False,
            "representative": False,
            "entity_ids": entity_ids,
            "periods": ["REQUIRED_YYYY_MM_1", "REQUIRED_YYYY_MM_2"],
            "operator_principals": ["REQUIRED_SHADOW_OPERATOR"],
            "evidence_references": [],
        },
        "thresholds": {
            "minimum_distinct_entities": 1,
            "minimum_distinct_periods": 2,
            "minimum_comparisons_per_report": 6,
            "minimum_match_rate": 0.98,
            "maximum_accepted_exceptions": 0,
            "required_domains": ["trial_balance", "statement"],
            "maximum_shadow_age_days": 30,
            "maximum_gate_age_days": 30,
            "maximum_rehearsal_age_days": 180,
            "approved_by": "REQUIRED_THRESHOLD_APPROVER",
            "rationale": "REQUIRED_APPROVED_THRESHOLD_RATIONALE",
        },
        "shadow_close_reports": [],
        "multi_entity_shadow_close_portfolios": [],
        "connector_shadow_artifacts": [],
        "pilot_shadow_series": {
            "reviewed_receipt_path": "REQUIRED_PRIVATE_REVIEWED_SHADOW_SERIES_PATH",
            "period_evidence_root": "REQUIRED_PRIVATE_CONSECUTIVE_PERIOD_EVIDENCE_ROOT",
            "pipeline_runs_root": "REQUIRED_PRIVATE_PIPELINE_RUNS_ROOT",
        },
        "automated_gates": [{
            "gate": gate,
            "passed": False,
            "completed_at": "REQUIRED_ISO_8601_WITH_TIMEZONE",
            "runtime_fingerprint": fingerprint,
            "evidence_references": [],
        } for gate in AUTOMATED_PROMOTION_GATES],
        "rehearsals": {
            name: {
                "passed": False,
                "completed_at": "REQUIRED_ISO_8601_WITH_TIMEZONE",
                "runtime_fingerprint": fingerprint,
                "evidence_references": [],
            }
            for name in REQUIRED_REHEARSALS
        },
        "known_limitations": [],
        "contains_financial_results": True,
        "storage_boundary": "input_only_not_persisted",
    }


def stable_promotion_evidence_template_catalog(runtime: BoxRuntime) -> dict[str, Any]:
    """Compile one editable evidence starter per non-stable Pack selected by the Box."""
    runtime.reload()
    snapshot = runtime.snapshot()
    templates = []
    for pack in snapshot["packs"]:
        if pack.get("status") == "stable":
            continue
        evidence = build_stable_promotion_evidence_template(runtime, pack["id"])
        templates.append({
            "pack_id": pack["id"],
            "pack_version": pack["version"],
            "current_status": pack["status"],
            "eligible_entity_ids": list(evidence["sample"]["entity_ids"]),
            "evidence": evidence,
        })
    return {
        "schema_version": 1,
        "runtime_fingerprint": snapshot["fingerprint"],
        "template_only": True,
        "assessment_ready": False,
        "evidence_schema": "stable-promotion-evidence.schema.json",
        "templates": templates,
        "control_note": (
            "Copy exactly one evidence object, replace every REQUIRED value, attach current "
            "Shadow reports, the exact reviewed consecutive Pilot Shadow series and its private "
            "period/ledger roots, one reviewed portfolio manifest per period when multiple entities "
            "are in scope, reviewed Connector Shadow artifacts for network Connector Packs, "
            "and real gate/rehearsal evidence, then run promotion-assess."
        ),
    }


def _canonical(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ReleasePromotionError("stable promotion metadata must be JSON-serializable") from exc


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _actor(value: Any, *, field: str = "actor") -> str:
    text = str(value or "").strip()
    if not ACTOR_PATTERN.fullmatch(text):
        raise ReleasePromotionError(f"{field} must be 1-80 printable characters")
    return text


def _timestamp(value: Any, field: str) -> datetime:
    text = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReleasePromotionError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ReleasePromotionError(f"{field} must include a timezone offset")
    return parsed.astimezone(timezone.utc)


def _evidence_references(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ReleasePromotionError(f"{field} requires at least one evidence reference")
    references = [str(item or "").strip() for item in value]
    if any(
        not item or len(item) > 240 or "\n" in item or "\r" in item
        or SECRET_PATTERN.search(item) or "?" in item or "#" in item
        for item in references
    ):
        raise ReleasePromotionError(
            f"{field} references must be bounded, secret-free labels or paths without query strings"
        )
    if len(references) != len(set(references)):
        raise ReleasePromotionError(f"{field} contains duplicate evidence references")
    return references


def _bounded_int(value: Any, field: str, minimum: int, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise ReleasePromotionError(f"{field} must be an integer from {minimum} to {maximum}")
    return value


def _validate_persisted_assessment_shape(assessment: Any) -> None:
    if not isinstance(assessment, dict) or set(assessment) != PERSISTED_ASSESSMENT_FIELDS:
        raise ReleasePromotionError("stable promotion assessment fields do not match the strict contract")
    if assessment.get("schema_version") != 1:
        raise ReleasePromotionError("stable promotion assessment schema_version is invalid")
    for field in (
        "runtime_fingerprint", "evidence_fingerprint", "sample_fingerprint",
        "known_limitation_fingerprint",
    ):
        if not HEX64_PATTERN.fullmatch(str(assessment.get(field) or "")):
            raise ReleasePromotionError(f"stable promotion assessment {field} is invalid")
    if not PACK_PATTERN.fullmatch(str(assessment.get("pack_id") or "")):
        raise ReleasePromotionError("stable promotion assessment pack_id is invalid")
    if not isinstance(assessment.get("pack_version"), str) or not assessment["pack_version"]:
        raise ReleasePromotionError("stable promotion assessment pack_version is invalid")
    if not isinstance(assessment.get("current_pack_status"), str) or not assessment["current_pack_status"]:
        raise ReleasePromotionError("stable promotion assessment Pack status is invalid")
    _actor(assessment.get("prepared_by"), field="promotion evidence preparer")
    _actor(assessment.get("threshold_approved_by"), field="threshold approver")
    if assessment.get("target_status") != "stable_candidate":
        raise ReleasePromotionError("stable promotion assessment target is invalid")
    series_summary = assessment.get("pilot_shadow_series_summary")
    expected_series_fields = {
        "period_count", "first_period", "latest_period", "comparison_count",
        "matched_count", "exception_count", "system_defect_count", "decision",
        "consecutive_periods_verified",
        "eligible_to_prepare_stable_promotion_evidence",
        "raw_financial_values_persisted",
    }
    if not isinstance(series_summary, dict) or set(series_summary) != expected_series_fields:
        raise ReleasePromotionError(
            "stable promotion Pilot Shadow series summary fields are invalid"
        )
    series_counts = [
        series_summary.get(field) for field in (
            "period_count", "comparison_count", "matched_count",
            "exception_count", "system_defect_count",
        )
    ]
    if (
        any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in series_counts
        )
        or series_counts[0] < 2
        or series_counts[1] != series_counts[2] + series_counts[3]
        or series_counts[4] != 0
        or not PERIOD_PATTERN.fullmatch(str(series_summary.get("first_period") or ""))
        or not PERIOD_PATTERN.fullmatch(str(series_summary.get("latest_period") or ""))
        or series_summary.get("decision") != "approved-for-promotion-evidence"
        or series_summary.get("consecutive_periods_verified") is not True
        or series_summary.get("eligible_to_prepare_stable_promotion_evidence") is not True
        or series_summary.get("raw_financial_values_persisted") is not False
    ):
        raise ReleasePromotionError(
            "stable promotion Pilot Shadow series summary is inconsistent"
        )
    report_summaries = assessment.get("report_summaries")
    if not isinstance(report_summaries, list) or not report_summaries:
        raise ReleasePromotionError("stable promotion assessment requires report summaries")
    expected_report_fields = {
        "baseline_id_sha256", "baseline_source_fingerprint", "report_fingerprint",
        "entity_id", "period", "comparison_count", "matched_count",
        "exception_count", "missing_count", "domains", "review_actor",
        "review_id", "review_rationale_sha256", "review_evidence_references",
        "reviewed_at", "review_decision", "exception_classifications",
        "resolution_fingerprint",
    }
    for report in report_summaries:
        if not isinstance(report, dict) or set(report) != expected_report_fields:
            raise ReleasePromotionError("stable promotion report summary fields are invalid")
        for field in (
            "baseline_id_sha256", "baseline_source_fingerprint", "report_fingerprint",
            "review_rationale_sha256", "resolution_fingerprint",
        ):
            if not HEX64_PATTERN.fullmatch(str(report.get(field) or "")):
                raise ReleasePromotionError("stable promotion report summary fingerprint is invalid")
        if not PERIOD_PATTERN.fullmatch(str(report.get("period") or "")):
            raise ReleasePromotionError("stable promotion report summary period is invalid")
        if not isinstance(report.get("entity_id"), str) or not report["entity_id"]:
            raise ReleasePromotionError("stable promotion report summary entity is invalid")
        comparison_count = report.get("comparison_count")
        matched_count = report.get("matched_count")
        exception_count = report.get("exception_count")
        missing_count = report.get("missing_count")
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in (comparison_count, matched_count, exception_count, missing_count)
        ) or matched_count + exception_count != comparison_count or missing_count > exception_count:
            raise ReleasePromotionError("stable promotion report summary counts are inconsistent")
        if (
            not isinstance(report.get("domains"), list)
            or not set(report["domains"]).issubset(REPORT_DOMAINS)
            or report["domains"] != sorted(set(report["domains"]))
        ):
            raise ReleasePromotionError("stable promotion report summary domains are invalid")
        _actor(report.get("review_actor"), field="shadow close reviewer")
        if not re.fullmatch(r"SHADOW-REVIEW-[0-9a-f]{16}", str(report.get("review_id") or "")):
            raise ReleasePromotionError("stable promotion report summary review id is invalid")
        _timestamp(report.get("reviewed_at"), "stable promotion report summary reviewed_at")
        _evidence_references(
            report.get("review_evidence_references"),
            "stable promotion report summary review evidence",
        )
    portfolio_summaries = assessment.get("portfolio_shadow_summaries")
    if not isinstance(portfolio_summaries, list) or len(portfolio_summaries) > 24:
        raise ReleasePromotionError(
            "stable promotion portfolio Shadow summaries are invalid"
        )
    expected_portfolio_fields = {
        "manifest_fingerprint", "period", "entity_ids", "entity_report_fingerprints",
        "entity_review_ids", "portfolio_run_id", "portfolio_result_fingerprint",
        "source_ledger_chain_head", "source_count", "review_id", "review_fingerprint",
        "review_actor", "review_decision", "review_rationale_sha256",
        "review_evidence_references", "reviewed_at", "raw_financial_values_persisted",
    }
    report_by_scope = {
        (item["entity_id"], item["period"]): item for item in report_summaries
    }
    seen_portfolio_periods: set[str] = set()
    for portfolio in portfolio_summaries:
        if not isinstance(portfolio, dict) or set(portfolio) != expected_portfolio_fields:
            raise ReleasePromotionError(
                "stable promotion portfolio Shadow summary fields are invalid"
            )
        for field in (
            "manifest_fingerprint", "portfolio_result_fingerprint",
            "source_ledger_chain_head", "review_fingerprint", "review_rationale_sha256",
        ):
            if not HEX64_PATTERN.fullmatch(str(portfolio.get(field) or "")):
                raise ReleasePromotionError(
                    "stable promotion portfolio Shadow fingerprint is invalid"
                )
        period = str(portfolio.get("period") or "")
        if not PERIOD_PATTERN.fullmatch(period) or period in seen_portfolio_periods:
            raise ReleasePromotionError(
                "stable promotion portfolio Shadow periods are invalid or duplicated"
            )
        seen_portfolio_periods.add(period)
        entity_ids = portfolio.get("entity_ids")
        report_fingerprints = portfolio.get("entity_report_fingerprints")
        review_ids = portfolio.get("entity_review_ids")
        if (
            not isinstance(entity_ids, list) or len(entity_ids) < 2
            or entity_ids != sorted(set(entity_ids))
            or not isinstance(report_fingerprints, list)
            or len(report_fingerprints) != len(entity_ids)
            or any(not HEX64_PATTERN.fullmatch(str(item or "")) for item in report_fingerprints)
            or not isinstance(review_ids, list) or len(review_ids) != len(entity_ids)
            or any(not re.fullmatch(r"SHADOW-REVIEW-[0-9a-f]{16}", str(item or "")) for item in review_ids)
        ):
            raise ReleasePromotionError(
                "stable promotion portfolio Shadow entity evidence is invalid"
            )
        scoped_reports = [report_by_scope.get((entity_id, period)) for entity_id in entity_ids]
        if any(item is None for item in scoped_reports):
            raise ReleasePromotionError(
                "stable promotion portfolio Shadow summary lacks entity report evidence"
            )
        if report_fingerprints != [item["report_fingerprint"] for item in scoped_reports]:
            raise ReleasePromotionError(
                "stable promotion portfolio Shadow report fingerprints are inconsistent"
            )
        if review_ids != [item["review_id"] for item in scoped_reports]:
            raise ReleasePromotionError(
                "stable promotion portfolio Shadow entity review ids are inconsistent"
            )
        if not re.fullmatch(r"[0-9a-f]{24}", str(portfolio.get("portfolio_run_id") or "")):
            raise ReleasePromotionError("stable promotion portfolio run id is invalid")
        source_count = portfolio.get("source_count")
        if (
            not isinstance(source_count, int) or isinstance(source_count, bool)
            or source_count != len(entity_ids)
        ):
            raise ReleasePromotionError("stable promotion portfolio source count is invalid")
        if not re.fullmatch(
            r"PORTFOLIO-SHADOW-REVIEW-[0-9a-f]{16}", str(portfolio.get("review_id") or "")
        ):
            raise ReleasePromotionError("stable promotion portfolio review id is invalid")
        _actor(portfolio.get("review_actor"), field="portfolio Shadow reviewer")
        if portfolio.get("review_decision") not in {"passed", "accepted-differences"}:
            raise ReleasePromotionError("stable promotion portfolio review decision is invalid")
        _timestamp(portfolio.get("reviewed_at"), "stable promotion portfolio reviewed_at")
        _evidence_references(
            portfolio.get("review_evidence_references"),
            "stable promotion portfolio review evidence",
        )
        if portfolio.get("raw_financial_values_persisted") is not False:
            raise ReleasePromotionError(
                "stable promotion portfolio summary violates persistence boundaries"
            )
    connector_summaries = assessment.get("connector_shadow_summaries")
    if not isinstance(connector_summaries, list) or len(connector_summaries) > 2400:
        raise ReleasePromotionError("stable promotion Connector Shadow summaries are invalid")
    legacy_connector_fields = {
        "assessment_fingerprint", "baseline_id_sha256", "baseline_sha256",
        "pipeline_result_sha256", "pipeline_id", "entity_id", "sample_period",
        "covered_pack_ids", "source_count", "control_count", "passed",
        "baseline_prepared_by", "review_id", "review_actor", "review_decision",
        "reviewed_at", "review_rationale_sha256", "review_evidence_references",
        "raw_source_values_persisted", "financial_amounts_persisted",
    }
    real_connector_fields = legacy_connector_fields | {
        "sample_classification", "real_sample_evidence",
        "source_independence_sha256", "anonymization_sha256",
    }
    seen_connector_scopes: set[tuple[str, str]] = set()
    for item in connector_summaries:
        if not isinstance(item, dict) or set(item) not in (
            legacy_connector_fields, real_connector_fields,
        ):
            raise ReleasePromotionError("stable promotion Connector Shadow summary fields are invalid")
        for field in (
            "assessment_fingerprint", "baseline_id_sha256", "baseline_sha256",
            "pipeline_result_sha256", "review_rationale_sha256",
        ):
            if not HEX64_PATTERN.fullmatch(str(item.get(field) or "")):
                raise ReleasePromotionError("stable promotion Connector Shadow fingerprint is invalid")
        if set(item) == real_connector_fields and (
            item.get("sample_classification") != "real_anonymized"
            or item.get("real_sample_evidence") is not True
            or any(
                not HEX64_PATTERN.fullmatch(str(item.get(field) or ""))
                for field in ("source_independence_sha256", "anonymization_sha256")
            )
        ):
            raise ReleasePromotionError(
                "stable promotion Connector Shadow real-sample classification is invalid"
            )
        scope = (str(item.get("entity_id") or ""), str(item.get("sample_period") or ""))
        if (
            not scope[0] or not PERIOD_PATTERN.fullmatch(scope[1])
            or scope in seen_connector_scopes
        ):
            raise ReleasePromotionError("stable promotion Connector Shadow scope is invalid or duplicated")
        seen_connector_scopes.add(scope)
        covered_pack_ids = item.get("covered_pack_ids")
        if (
            not isinstance(covered_pack_ids, list) or not covered_pack_ids
            or covered_pack_ids != sorted(set(covered_pack_ids))
            or any(not PACK_PATTERN.fullmatch(str(pack_id or "")) for pack_id in covered_pack_ids)
        ):
            raise ReleasePromotionError("stable promotion Connector Shadow Pack coverage is invalid")
        if not isinstance(item.get("pipeline_id"), str) or not item["pipeline_id"]:
            raise ReleasePromotionError("stable promotion Connector Shadow pipeline is invalid")
        for field in ("source_count", "control_count"):
            if not isinstance(item.get(field), int) or isinstance(item.get(field), bool) or item[field] < 1:
                raise ReleasePromotionError("stable promotion Connector Shadow counts are invalid")
        _actor(item.get("baseline_prepared_by"), field="Connector Shadow baseline preparer")
        _actor(item.get("review_actor"), field="Connector Shadow reviewer")
        if not re.fullmatch(r"[0-9a-f]{24}", str(item.get("review_id") or "")):
            raise ReleasePromotionError("stable promotion Connector Shadow review id is invalid")
        if item.get("review_decision") not in CONNECTOR_SHADOW_DECISIONS:
            raise ReleasePromotionError("stable promotion Connector Shadow decision is invalid")
        _timestamp(item.get("reviewed_at"), "stable promotion Connector Shadow reviewed_at")
        _evidence_references(
            item.get("review_evidence_references"),
            "stable promotion Connector Shadow review evidence",
        )
        if (
            not isinstance(item.get("passed"), bool)
            or item.get("raw_source_values_persisted") is not False
            or item.get("financial_amounts_persisted") is not False
        ):
            raise ReleasePromotionError("stable promotion Connector Shadow summary violates persistence boundaries")
    if (
        not isinstance(assessment.get("gate_evidence_fingerprints"), dict)
        or set(assessment["gate_evidence_fingerprints"]) != set(AUTOMATED_PROMOTION_GATES)
        or any(
            not HEX64_PATTERN.fullmatch(str(value or ""))
            for value in assessment["gate_evidence_fingerprints"].values()
        )
    ):
        raise ReleasePromotionError("stable promotion assessment gate fingerprints are invalid")
    if (
        not isinstance(assessment.get("rehearsal_evidence_fingerprints"), dict)
        or set(assessment["rehearsal_evidence_fingerprints"]) != set(REQUIRED_REHEARSALS)
        or any(
            not HEX64_PATTERN.fullmatch(str(value or ""))
            for value in assessment["rehearsal_evidence_fingerprints"].values()
        )
    ):
        raise ReleasePromotionError("stable promotion assessment rehearsal fingerprints are invalid")
    metrics = assessment.get("metrics")
    thresholds = assessment.get("thresholds")
    if not isinstance(metrics, dict) or not isinstance(thresholds, dict):
        raise ReleasePromotionError("stable promotion assessment metrics or thresholds are invalid")
    expected_metrics = {
        "distinct_entities", "distinct_periods", "report_count", "comparison_count",
        "portfolio_shadow_count", "connector_shadow_count", "matched_count",
        "exception_count", "missing_count", "match_rate",
        "automated_gates_passed", "automated_gates_required", "rehearsals_passed",
        "rehearsals_required",
    }
    if set(metrics) != expected_metrics:
        raise ReleasePromotionError("stable promotion assessment metric fields are invalid")
    aggregate_comparisons = sum(item["comparison_count"] for item in report_summaries)
    aggregate_matched = sum(item["matched_count"] for item in report_summaries)
    aggregate_exceptions = sum(item["exception_count"] for item in report_summaries)
    aggregate_missing = sum(item["missing_count"] for item in report_summaries)
    if (
        metrics.get("report_count") != len(report_summaries)
        or metrics.get("portfolio_shadow_count") != len(portfolio_summaries)
        or metrics.get("connector_shadow_count") != len(connector_summaries)
        or metrics.get("comparison_count") != aggregate_comparisons
        or metrics.get("matched_count") != aggregate_matched
        or metrics.get("exception_count") != aggregate_exceptions
        or metrics.get("missing_count") != aggregate_missing
        or metrics.get("automated_gates_required") != len(AUTOMATED_PROMOTION_GATES)
        or metrics.get("rehearsals_required") != len(REQUIRED_REHEARSALS)
    ):
        raise ReleasePromotionError("stable promotion assessment metrics are inconsistent")
    expected_match_rate = round(
        aggregate_matched / aggregate_comparisons if aggregate_comparisons else 0.0,
        6,
    )
    if metrics.get("match_rate") != expected_match_rate:
        raise ReleasePromotionError("stable promotion assessment match rate is inconsistent")
    distinct_report_entities = {item["entity_id"] for item in report_summaries}
    distinct_report_periods = {item["period"] for item in report_summaries}
    if metrics.get("distinct_entities") != len(distinct_report_entities) or metrics.get(
        "distinct_periods"
    ) != len(distinct_report_periods):
        raise ReleasePromotionError(
            "stable promotion assessment distinct scope metrics are inconsistent"
        )
    if (
        series_summary["period_count"] != len(distinct_report_periods)
        or series_summary["first_period"] != min(distinct_report_periods)
        or series_summary["latest_period"] != max(distinct_report_periods)
    ):
        raise ReleasePromotionError(
            "stable promotion Pilot Shadow series scope is inconsistent with report summaries"
        )
    if len(distinct_report_entities) > 1:
        if (
            len(portfolio_summaries) != len(distinct_report_periods)
            or {item["period"] for item in portfolio_summaries} != distinct_report_periods
            or any(item["entity_ids"] != sorted(distinct_report_entities) for item in portfolio_summaries)
        ):
            raise ReleasePromotionError(
                "stable promotion assessment lacks complete portfolio Shadow coverage"
            )
    elif portfolio_summaries:
        raise ReleasePromotionError(
            "single-entity stable promotion assessment must not persist portfolio Shadow summaries"
        )
    if (
        not isinstance(metrics.get("automated_gates_passed"), int)
        or isinstance(metrics.get("automated_gates_passed"), bool)
        or not 0 <= metrics["automated_gates_passed"] <= len(AUTOMATED_PROMOTION_GATES)
        or not isinstance(metrics.get("rehearsals_passed"), int)
        or isinstance(metrics.get("rehearsals_passed"), bool)
        or not 0 <= metrics["rehearsals_passed"] <= len(REQUIRED_REHEARSALS)
    ):
        raise ReleasePromotionError("stable promotion assessment gate counts are invalid")
    expected_threshold_fields = {
        "minimum_distinct_entities", "minimum_distinct_periods",
        "minimum_comparisons_per_report", "minimum_match_rate",
        "maximum_accepted_exceptions", "required_domains",
        "maximum_shadow_age_days", "maximum_gate_age_days",
        "maximum_rehearsal_age_days", "approved_by", "rationale_sha256",
    }
    if set(thresholds) != expected_threshold_fields or not HEX64_PATTERN.fullmatch(
        str(thresholds.get("rationale_sha256") or "")
    ):
        raise ReleasePromotionError("stable promotion assessment thresholds are invalid")
    if (
        not isinstance(assessment.get("known_limitation_count"), int)
        or isinstance(assessment.get("known_limitation_count"), bool)
        or not 0 <= assessment["known_limitation_count"] <= 20
    ):
        raise ReleasePromotionError("stable promotion known limitation count is invalid")
    blockers, warnings = assessment.get("blockers"), assessment.get("warnings")
    if not isinstance(blockers, list) or not isinstance(warnings, list):
        raise ReleasePromotionError("stable promotion assessment blockers or warnings are invalid")
    if not isinstance(assessment.get("candidate_eligible"), bool):
        raise ReleasePromotionError("stable promotion assessment eligibility is invalid")
    if assessment["candidate_eligible"] != (not blockers):
        raise ReleasePromotionError("stable promotion assessment eligibility is inconsistent")
    principals = assessment.get("separation_principals")
    if (
        not isinstance(principals, list) or not principals
        or principals != sorted(set(principals))
        or any(not isinstance(item, str) for item in principals)
    ):
        raise ReleasePromotionError("stable promotion separation principals are invalid")
    for item in principals:
        _actor(item, field="stable promotion separation principal")
    if assessment.get("prepared_by") not in principals or assessment.get("threshold_approved_by") not in principals:
        raise ReleasePromotionError("stable promotion separation principals are incomplete")
    if any(report["review_actor"] not in principals for report in report_summaries):
        raise ReleasePromotionError("stable promotion separation principals omit a Shadow reviewer")
    if any(portfolio["review_actor"] not in principals for portfolio in portfolio_summaries):
        raise ReleasePromotionError(
            "stable promotion separation principals omit a portfolio Shadow reviewer"
        )
    if any(
        item["review_actor"] not in principals or item["baseline_prepared_by"] not in principals
        for item in connector_summaries
    ):
        raise ReleasePromotionError(
            "stable promotion separation principals omit a Connector Shadow principal"
        )
    report_reviewer_principals = {item["review_actor"] for item in report_summaries}
    if any(
        portfolio["review_actor"] in report_reviewer_principals
        for portfolio in portfolio_summaries
    ):
        raise ReleasePromotionError(
            "stable promotion portfolio reviewer is not separate from entity Shadow reviewers"
        )
    if (
        assessment.get("raw_shadow_reports_persisted") is not False
        or assessment.get("raw_portfolio_shadow_manifests_persisted") is not False
        or assessment.get("raw_connector_shadow_artifacts_persisted") is not False
        or assessment.get("raw_pilot_shadow_series_artifacts_persisted") is not False
        or assessment.get("raw_financial_values_persisted") is not False
        or assessment.get("pack_manifest_changed") is not False
        or assessment.get("external_actions_performed") is not False
    ):
        raise ReleasePromotionError("stable promotion assessment violates persistence boundaries")


def _report_fingerprint(report: dict[str, Any]) -> str:
    rows = [{
        key: row.get(key)
        for key in (
            "domain", "key", "manual_value", "agent_value", "difference",
            "allowed_tolerance", "status",
        )
    } for row in report.get("comparisons") or []]
    payload = {
        "baseline_id": report.get("baseline_id"),
        "entity_id": report.get("entity_id"),
        "period": report.get("period"),
        "baseline": report.get("baseline_source_fingerprint"),
        "rows": rows,
    }
    if report.get("runtime_fingerprint"):
        payload["runtime_fingerprint"] = report.get("runtime_fingerprint")
    return _hash(payload)


def _validate_resolution(value: Any, *, expected: tuple[str, str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ReleasePromotionError("shadow close exception resolutions must be objects")
    required = {"domain", "key", "classification", "rationale", "evidence_references"}
    if set(value) != required:
        raise ReleasePromotionError("shadow close exception resolution fields do not match the strict contract")
    identity = (str(value.get("domain") or ""), str(value.get("key") or ""))
    if identity != expected:
        raise ReleasePromotionError("shadow close exception resolution identity does not match its comparison")
    classification = str(value.get("classification") or "")
    if classification not in EXCEPTION_CLASSIFICATIONS:
        raise ReleasePromotionError("shadow close exception classification is invalid")
    rationale = str(value.get("rationale") or "").strip()
    if len(rationale) < 12 or len(rationale) > 1000:
        raise ReleasePromotionError("shadow close exception rationale must be 12-1000 characters")
    return {
        "domain": identity[0],
        "key": identity[1],
        "classification": classification,
        "rationale_sha256": hashlib.sha256(rationale.encode("utf-8")).hexdigest(),
        "evidence_references": _evidence_references(
            value.get("evidence_references"), "shadow close exception resolution",
        ),
    }


def _validate_shadow_report(
    value: Any,
    *,
    runtime_fingerprint: str,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ReleasePromotionError("shadow_close_reports must contain report objects")
    for field in (
        "baseline_id", "baseline_source_fingerprint", "runtime_fingerprint",
        "entity_id", "period",
        "report_fingerprint", "comparisons", "comparison_count", "matched_count",
        "exception_count", "domain_summary", "review", "review_current",
    ):
        if field not in value:
            raise ReleasePromotionError(f"shadow close report is missing {field}")
    if not HEX64_PATTERN.fullmatch(str(value.get("baseline_source_fingerprint") or "")):
        raise ReleasePromotionError("shadow close baseline fingerprint is invalid")
    if value.get("runtime_fingerprint") != runtime_fingerprint:
        raise ReleasePromotionError(
            "shadow close report belongs to a different Box runtime fingerprint"
        )
    baseline_id = str(value.get("baseline_id") or "").strip()
    if not baseline_id or len(baseline_id) > 160 or not ACTOR_PATTERN.fullmatch(baseline_id):
        raise ReleasePromotionError("shadow close baseline_id must be 1-160 printable characters")
    if not PERIOD_PATTERN.fullmatch(str(value.get("period") or "")):
        raise ReleasePromotionError("shadow close report period must use YYYY-MM")
    entity_id = str(value.get("entity_id") or "").strip()
    if not entity_id:
        raise ReleasePromotionError("shadow close report requires entity_id")
    comparisons = value.get("comparisons")
    if not isinstance(comparisons, list) or not comparisons:
        raise ReleasePromotionError("shadow close report requires comparison rows")
    identities: set[tuple[str, str]] = set()
    exceptions: list[tuple[str, str]] = []
    missing = 0
    for row in comparisons:
        if not isinstance(row, dict):
            raise ReleasePromotionError("shadow close comparisons must be objects")
        domain, key = str(row.get("domain") or ""), str(row.get("key") or "")
        if domain not in REPORT_DOMAINS or not key:
            raise ReleasePromotionError("shadow close comparison domain or key is invalid")
        identity = (domain, key)
        if identity in identities:
            raise ReleasePromotionError("shadow close report contains a duplicate comparison identity")
        identities.add(identity)
        status = str(row.get("status") or "")
        if status not in {"一致", "需解释", "Agent 缺项", "人工基准缺项"}:
            raise ReleasePromotionError("shadow close comparison status is invalid")
        if status != "一致":
            exceptions.append(identity)
        if status in {"Agent 缺项", "人工基准缺项"}:
            missing += 1
        for field in ("manual_value", "agent_value"):
            amount = row.get(field)
            if amount is not None and (
                not isinstance(amount, (int, float))
                or isinstance(amount, bool)
                or not math.isfinite(float(amount))
            ):
                raise ReleasePromotionError(
                    f"shadow close comparison {field} must be a finite number or null"
                )
        tolerance = row.get("allowed_tolerance")
        if (
            not isinstance(tolerance, (int, float))
            or isinstance(tolerance, bool)
            or not math.isfinite(float(tolerance))
            or float(tolerance) < 0
        ):
            raise ReleasePromotionError(
                "shadow close comparison allowed_tolerance must be a finite non-negative number"
            )
        difference = row.get("difference")
        if difference is not None:
            if (
                not isinstance(difference, (int, float))
                or isinstance(difference, bool)
                or not math.isfinite(float(difference))
            ):
                raise ReleasePromotionError(
                    "shadow close comparison difference must be a finite number or null"
                )
        manual_value = row.get("manual_value")
        agent_value = row.get("agent_value")
        if manual_value is not None and agent_value is not None:
            expected_difference = round(float(agent_value) - float(manual_value), 2)
            if difference is None or round(float(difference), 2) != expected_difference:
                raise ReleasePromotionError(
                    "shadow close comparison difference does not match manual and Agent values"
                )
            expected_status = (
                "一致" if abs(expected_difference) <= float(tolerance) else "需解释"
            )
            if status != expected_status:
                raise ReleasePromotionError(
                    "shadow close comparison status does not match its explicit tolerance"
                )
        elif difference is not None or status == "一致":
            raise ReleasePromotionError(
                "shadow close missing-value comparison has an invalid difference or status"
            )
    if value.get("comparison_count") != len(comparisons):
        raise ReleasePromotionError("shadow close comparison_count is inconsistent")
    if value.get("exception_count") != len(exceptions):
        raise ReleasePromotionError("shadow close exception_count is inconsistent")
    if value.get("matched_count") != len(comparisons) - len(exceptions):
        raise ReleasePromotionError("shadow close matched_count is inconsistent")
    supplied_fingerprint = str(value.get("report_fingerprint") or "")
    if not HEX64_PATTERN.fullmatch(supplied_fingerprint) or supplied_fingerprint != _report_fingerprint(value):
        raise ReleasePromotionError("shadow close report fingerprint does not match its comparisons")
    review = value.get("review")
    if value.get("review_current") is not True or not isinstance(review, dict):
        raise ReleasePromotionError("shadow close report requires a current independent review")
    required_review_fields = {
        "id", "baseline_id", "entity_id", "period", "report_fingerprint",
        "decision", "actor", "rationale", "evidence", "exception_resolutions",
        "reviewed_at", "scope_note",
    }
    if set(review) != required_review_fields:
        raise ReleasePromotionError("shadow close review fields do not match the strict contract")
    if (
        review.get("baseline_id") != baseline_id
        or review.get("entity_id") != entity_id
        or review.get("period") != value.get("period")
    ):
        raise ReleasePromotionError("shadow close review scope does not match the current report")
    review_id = str(review.get("id") or "")
    if not re.fullmatch(r"SHADOW-REVIEW-[0-9a-f]{16}", review_id):
        raise ReleasePromotionError("shadow close review id is invalid")
    if review.get("report_fingerprint") != supplied_fingerprint:
        raise ReleasePromotionError("shadow close review is not bound to the current report fingerprint")
    decision = str(review.get("decision") or "")
    if decision not in {"验证通过", "接受差异"}:
        raise ReleasePromotionError("shadow close report is not signed off for promotion evidence")
    if decision == "验证通过" and exceptions:
        raise ReleasePromotionError("shadow close cannot be marked verified while exceptions remain")
    reviewer = _actor(review.get("actor"), field="shadow close reviewer")
    review_rationale = str(review.get("rationale") or "").strip()
    if len(review_rationale) < 6 or len(review_rationale) > 1000:
        raise ReleasePromotionError("shadow close review rationale must be 6-1000 characters")
    review_evidence = _evidence_references(
        review.get("evidence"), "shadow close review evidence",
    )
    reviewed_at = _timestamp(review.get("reviewed_at"), "shadow close review.reviewed_at")
    resolutions = review.get("exception_resolutions") or []
    if not isinstance(resolutions, list):
        raise ReleasePromotionError("shadow close exception_resolutions must be a list")
    by_identity = {
        (str(item.get("domain") or ""), str(item.get("key") or "")): item
        for item in resolutions if isinstance(item, dict)
    }
    if len(by_identity) != len(resolutions):
        raise ReleasePromotionError("shadow close exception resolutions contain invalid or duplicate identities")
    if decision == "接受差异" and set(by_identity) != set(exceptions):
        raise ReleasePromotionError("every accepted shadow close exception requires one exact resolution")
    if decision == "验证通过" and resolutions:
        raise ReleasePromotionError("a zero-exception shadow close review must not contain resolutions")
    validated_resolutions = [
        _validate_resolution(by_identity[identity], expected=identity)
        for identity in sorted(exceptions)
    ]
    domains = sorted({domain for domain, _ in identities})
    return {
        "baseline_id_sha256": hashlib.sha256(baseline_id.encode("utf-8")).hexdigest(),
        "baseline_source_fingerprint": value["baseline_source_fingerprint"],
        "report_fingerprint": supplied_fingerprint,
        "entity_id": entity_id,
        "period": value["period"],
        "comparison_count": len(comparisons),
        "matched_count": len(comparisons) - len(exceptions),
        "exception_count": len(exceptions),
        "missing_count": missing,
        "domains": domains,
        "review_actor": reviewer,
        "review_id": review_id,
        "review_rationale_sha256": hashlib.sha256(
            review_rationale.encode("utf-8")
        ).hexdigest(),
        "review_evidence_references": review_evidence,
        "reviewed_at": reviewed_at.isoformat().replace("+00:00", "Z"),
        "review_decision": decision,
        "exception_classifications": [item["classification"] for item in validated_resolutions],
        "resolution_fingerprint": _hash(validated_resolutions),
    }


def _validate_portfolio_shadow_manifest(
    runtime: BoxRuntime,
    value: Any,
    *,
    reports: list[dict[str, Any]],
    clock: datetime,
    maximum_age_days: int,
) -> dict[str, Any]:
    """Validate one no-values portfolio acceptance artifact against raw report evidence."""
    try:
        integrity = validate_multi_entity_shadow_close_manifest(
            runtime, value, require_review=True,
        )
    except (MultiEntityShadowCloseError, ValueError) as exc:
        raise ReleasePromotionError(
            f"multi-entity Shadow Close portfolio is invalid: {exc}"
        ) from exc
    review = value["review"]
    if review.get("decision") not in {"passed", "accepted-differences"}:
        raise ReleasePromotionError(
            "multi-entity Shadow Close portfolio is not signed off for promotion evidence"
        )
    reviewed_at = _timestamp(
        review.get("reviewed_at"), "multi-entity Shadow Close portfolio review.reviewed_at",
    )
    if reviewed_at > clock + timedelta(minutes=5):
        raise ReleasePromotionError(
            "multi-entity Shadow Close portfolio review is dated in the future"
        )
    if reviewed_at < clock - timedelta(days=maximum_age_days):
        raise ReleasePromotionError(
            f"multi-entity Shadow Close portfolio review is older than {maximum_age_days} days"
        )
    report_index = {
        (item["entity_id"], item["period"]): item
        for item in reports
    }
    entity_summaries = value["entity_reports"]
    manifest_scope = {
        (str(item.get("entity_id") or ""), str(value.get("period") or ""))
        for item in entity_summaries
    }
    if manifest_scope != {
        scope for scope in report_index if scope[1] == value.get("period")
    }:
        raise ReleasePromotionError(
            "multi-entity Shadow Close portfolio scope does not match raw report evidence"
        )
    for item in entity_summaries:
        report = report_index[(item["entity_id"], value["period"])]
        if (
            item.get("report_fingerprint") != report["report_fingerprint"]
            or item.get("review_id") != report["review_id"]
            or item.get("review_actor") != report["review_actor"]
            or item.get("decision") != report["review_decision"]
            or item.get("comparison_count") != report["comparison_count"]
            or item.get("matched_count") != report["matched_count"]
            or item.get("exception_count") != report["exception_count"]
        ):
            raise ReleasePromotionError(
                "multi-entity Shadow Close portfolio entity summary does not match raw report evidence"
            )
    portfolio = value["portfolio"]
    rationale = str(review.get("rationale") or "").strip()
    return {
        "manifest_fingerprint": integrity["manifest_fingerprint"],
        "period": value["period"],
        "entity_ids": list(value["entity_ids"]),
        "entity_report_fingerprints": [
            item["report_fingerprint"] for item in entity_summaries
        ],
        "entity_review_ids": [item["review_id"] for item in entity_summaries],
        "portfolio_run_id": portfolio["run_id"],
        "portfolio_result_fingerprint": portfolio["result_fingerprint"],
        "source_ledger_chain_head": portfolio["source_ledger_chain_head"],
        "source_count": portfolio["source_count"],
        "review_id": review["id"],
        "review_fingerprint": review["review_fingerprint"],
        "review_actor": _actor(
            review.get("actor"), field="multi-entity Shadow Close portfolio reviewer",
        ),
        "review_decision": review["decision"],
        "review_rationale_sha256": hashlib.sha256(rationale.encode("utf-8")).hexdigest(),
        "review_evidence_references": _evidence_references(
            review.get("evidence_references"),
            "multi-entity Shadow Close portfolio review evidence",
        ),
        "reviewed_at": reviewed_at.isoformat().replace("+00:00", "Z"),
        "raw_financial_values_persisted": False,
    }


def _network_connector_pack_ids(runtime: BoxRuntime) -> set[str]:
    return {
        item["pack_id"]
        for item in build_box_connector_registry(runtime).catalog(runtime)
        if item.get("network_access") is True
    }


def _validate_connector_shadow_promotion_artifact(
    runtime: BoxRuntime,
    path_value: Any,
    *,
    pack_id: str,
    clock: datetime,
    maximum_age_days: int,
) -> dict[str, Any]:
    path = str(path_value or "").strip()
    if not path or len(path) > 4096 or "\x00" in path:
        raise ReleasePromotionError(
            "connector_shadow_artifacts must contain non-empty file paths"
        )
    try:
        verified = verify_connector_shadow_artifact(runtime, path)
    except (ConnectorShadowArtifactError, OSError, ValueError) as exc:
        raise ReleasePromotionError(f"Connector Shadow artifact is invalid: {exc}") from exc
    if verified.get("real_sample_evidence") is not True:
        raise ReleasePromotionError(
            "stable promotion requires a schema v2 real_anonymized Connector Shadow; "
            "legacy/demo baselines are not promotion evidence"
        )
    if pack_id not in set(verified.get("covered_pack_ids") or []):
        raise ReleasePromotionError(
            "Connector Shadow artifact does not cover the target Pack"
        )
    if not verified.get("review_current"):
        raise ReleasePromotionError(
            "Connector Shadow artifact requires a current independent review"
        )
    reviewed_at = _timestamp(
        verified.get("reviewed_at"), "Connector Shadow review.reviewed_at",
    )
    if reviewed_at > clock + timedelta(minutes=5):
        raise ReleasePromotionError("Connector Shadow review is dated in the future")
    if reviewed_at < clock - timedelta(days=maximum_age_days):
        raise ReleasePromotionError(
            f"Connector Shadow review is older than {maximum_age_days} days"
        )
    baseline_preparer = _actor(
        verified.get("baseline_prepared_by"), field="Connector Shadow baseline preparer",
    )
    review_actor = _actor(
        verified.get("review_actor"), field="Connector Shadow reviewer",
    )
    if baseline_preparer == review_actor:
        raise ReleasePromotionError(
            "Connector Shadow reviewer must differ from baseline preparer"
        )
    baseline_id = str(verified.get("baseline_id") or "")
    return {
        "assessment_fingerprint": verified["assessment_fingerprint"],
        "baseline_id_sha256": hashlib.sha256(baseline_id.encode("utf-8")).hexdigest(),
        "baseline_sha256": verified["baseline_sha256"],
        "pipeline_result_sha256": verified["pipeline_result_sha256"],
        "pipeline_id": verified["pipeline_id"],
        "entity_id": verified["entity_id"],
        "sample_period": verified["sample_period"],
        "covered_pack_ids": list(verified["covered_pack_ids"]),
        "sample_classification": verified["sample_classification"],
        "real_sample_evidence": True,
        "source_independence_sha256": verified["source_independence_sha256"],
        "anonymization_sha256": verified["anonymization_sha256"],
        "source_count": verified["source_count"],
        "control_count": verified["control_count"],
        "passed": verified["passed"],
        "baseline_prepared_by": baseline_preparer,
        "review_id": verified["review_id"],
        "review_actor": review_actor,
        "review_decision": verified["decision"],
        "reviewed_at": reviewed_at.isoformat().replace("+00:00", "Z"),
        "review_rationale_sha256": verified["review_rationale_sha256"],
        "review_evidence_references": _evidence_references(
            verified.get("review_evidence_references"),
            "Connector Shadow review evidence",
        ),
        "raw_source_values_persisted": False,
        "financial_amounts_persisted": False,
    }


def _validate_thresholds(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ReleasePromotionError("thresholds must be an object")
    required = {
        "minimum_distinct_entities", "minimum_distinct_periods",
        "minimum_comparisons_per_report", "minimum_match_rate",
        "maximum_accepted_exceptions", "required_domains",
        "maximum_shadow_age_days", "maximum_gate_age_days", "maximum_rehearsal_age_days",
        "approved_by", "rationale",
    }
    if set(value) != required:
        raise ReleasePromotionError("stable promotion threshold fields do not match the strict contract")
    rate = value.get("minimum_match_rate")
    if not isinstance(rate, (int, float)) or isinstance(rate, bool) or not 0.98 <= float(rate) <= 1:
        raise ReleasePromotionError("minimum_match_rate must be from 0.98 to 1.0")
    domains = value.get("required_domains")
    if (
        not isinstance(domains, list) or any(not isinstance(item, str) for item in domains)
        or len(domains) != len(set(domains))
        or not {"trial_balance", "statement"}.issubset(domains)
        or not set(domains).issubset(REPORT_DOMAINS)
    ):
        raise ReleasePromotionError(
            "required_domains must include trial_balance and statement and contain only supported domains"
        )
    rationale = str(value.get("rationale") or "").strip()
    if len(rationale) < 12 or len(rationale) > 1000:
        raise ReleasePromotionError("threshold rationale must be 12-1000 characters")
    return {
        "minimum_distinct_entities": _bounded_int(
            value.get("minimum_distinct_entities"), "minimum_distinct_entities", 1, 100,
        ),
        "minimum_distinct_periods": _bounded_int(
            value.get("minimum_distinct_periods"), "minimum_distinct_periods", 2, 24,
        ),
        "minimum_comparisons_per_report": _bounded_int(
            value.get("minimum_comparisons_per_report"),
            "minimum_comparisons_per_report", 6, 10_000,
        ),
        "minimum_match_rate": float(rate),
        "maximum_accepted_exceptions": _bounded_int(
            value.get("maximum_accepted_exceptions"), "maximum_accepted_exceptions", 0, 10,
        ),
        "required_domains": sorted(domains),
        "maximum_shadow_age_days": _bounded_int(
            value.get("maximum_shadow_age_days"), "maximum_shadow_age_days", 1, 365,
        ),
        "maximum_gate_age_days": _bounded_int(
            value.get("maximum_gate_age_days"), "maximum_gate_age_days", 1, 90,
        ),
        "maximum_rehearsal_age_days": _bounded_int(
            value.get("maximum_rehearsal_age_days"), "maximum_rehearsal_age_days", 1, 365,
        ),
        "approved_by": _actor(value.get("approved_by"), field="threshold approver"),
        "rationale_sha256": hashlib.sha256(rationale.encode("utf-8")).hexdigest(),
    }


def _validate_timed_evidence(
    value: Any,
    *,
    field: str,
    runtime_fingerprint: str,
    clock: datetime,
    maximum_age_days: int,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ReleasePromotionError(f"{field} must be an object")
    required = {"passed", "completed_at", "runtime_fingerprint", "evidence_references"}
    if set(value) != required:
        raise ReleasePromotionError(f"{field} fields do not match the strict evidence contract")
    completed = _timestamp(value.get("completed_at"), f"{field}.completed_at")
    if completed > clock + timedelta(minutes=5):
        raise ReleasePromotionError(f"{field} completed_at is in the future")
    if completed < clock - timedelta(days=maximum_age_days):
        raise ReleasePromotionError(f"{field} evidence is older than {maximum_age_days} days")
    if value.get("runtime_fingerprint") != runtime_fingerprint:
        raise ReleasePromotionError(f"{field} belongs to a different Box fingerprint")
    return {
        "passed": value.get("passed") is True,
        "completed_at": completed.isoformat().replace("+00:00", "Z"),
        "evidence_references": _evidence_references(
            value.get("evidence_references"), f"{field}.evidence_references",
        ),
    }


def _validate_known_limitations(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > 20:
        raise ReleasePromotionError("known_limitations must be a list of at most 20 items")
    output = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {
            "limitation", "owner", "disposition", "evidence_references",
        }:
            raise ReleasePromotionError("known limitation fields do not match the strict contract")
        limitation = str(item.get("limitation") or "").strip()
        if len(limitation) < 6 or len(limitation) > 500:
            raise ReleasePromotionError("known limitation must be 6-500 characters")
        disposition = str(item.get("disposition") or "")
        if disposition not in {"resolved", "accepted_scope"}:
            raise ReleasePromotionError("known limitation disposition must be resolved or accepted_scope")
        output.append({
            "limitation_sha256": hashlib.sha256(limitation.encode("utf-8")).hexdigest(),
            "owner": _actor(item.get("owner"), field="known limitation owner"),
            "disposition": disposition,
            "evidence_references": _evidence_references(
                item.get("evidence_references"), "known limitation evidence",
            ),
        })
    return output


def _validate_pilot_shadow_series_promotion_evidence(
    runtime: BoxRuntime,
    value: Any,
    *,
    clock: datetime,
) -> tuple[dict[str, Any], dict[str, Any]]:
    required = {
        "reviewed_receipt_path", "period_evidence_root", "pipeline_runs_root",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise ReleasePromotionError(
            "pilot_shadow_series fields do not match the strict evidence contract"
        )
    paths: dict[str, str] = {}
    for field in sorted(required):
        path = str(value.get(field) or "").strip()
        if not path or len(path) > 4096 or "\x00" in path:
            raise ReleasePromotionError(
                f"pilot_shadow_series.{field} must be a non-empty bounded path"
            )
        paths[field] = path
    try:
        verified = verify_pilot_shadow_series_for_promotion(
            runtime,
            paths["reviewed_receipt_path"],
            paths["period_evidence_root"],
            paths["pipeline_runs_root"],
            as_of=clock.date().isoformat(),
        )
    except (PilotShadowSeriesError, OSError, ValueError) as exc:
        raise ReleasePromotionError(
            f"pilot Shadow series promotion evidence is invalid: {exc}"
        ) from exc
    if (
        verified.get("decision") != "approved-for-promotion-evidence"
        or verified.get("consecutive_periods_verified") is not True
        or verified.get("eligible_to_prepare_stable_promotion_evidence") is not True
        or verified.get("system_defect_count") != 0
        or verified.get("period_count", 0) < 2
    ):
        raise ReleasePromotionError(
            "stable promotion requires an approved defect-free consecutive Pilot Shadow series"
        )
    summary = {
        "period_count": verified["period_count"],
        "first_period": verified["first_period"],
        "latest_period": verified["latest_period"],
        "comparison_count": verified["comparison_count"],
        "matched_count": verified["matched_count"],
        "exception_count": verified["exception_count"],
        "system_defect_count": verified["system_defect_count"],
        "decision": verified["decision"],
        "consecutive_periods_verified": True,
        "eligible_to_prepare_stable_promotion_evidence": True,
        "raw_financial_values_persisted": False,
    }
    return summary, verified


def build_stable_promotion_assessment(
    runtime: BoxRuntime,
    evidence: Any,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    if not isinstance(evidence, dict) or evidence.get("schema_version") != 1:
        raise ReleasePromotionError("stable promotion evidence requires schema_version 1")
    required = {
        "schema_version", "runtime_fingerprint", "pack_id", "pack_version",
        "prepared_by", "sample", "thresholds", "shadow_close_reports",
        "multi_entity_shadow_close_portfolios", "connector_shadow_artifacts",
        "pilot_shadow_series",
        "automated_gates", "rehearsals", "known_limitations",
        "contains_financial_results", "storage_boundary",
    }
    if set(evidence) != required:
        raise ReleasePromotionError("stable promotion evidence fields do not match the strict contract")
    if evidence.get("contains_financial_results") is not True:
        raise ReleasePromotionError("stable promotion evidence must declare that reports contain financial results")
    if evidence.get("storage_boundary") != "input_only_not_persisted":
        raise ReleasePromotionError("stable promotion financial results must remain input-only")
    runtime.reload()
    snapshot = runtime.snapshot()
    runtime_fingerprint = snapshot["fingerprint"]
    if evidence.get("runtime_fingerprint") != runtime_fingerprint:
        raise ReleasePromotionError("stable promotion evidence belongs to a different Box fingerprint")
    pack_id = str(evidence.get("pack_id") or "")
    if not PACK_PATTERN.fullmatch(pack_id):
        raise ReleasePromotionError("stable promotion pack_id is invalid")
    packs = {item["id"]: item for item in snapshot["packs"]}
    pack = packs.get(pack_id)
    if pack is None:
        raise ReleasePromotionError("stable promotion target Pack is not selected by this Box")
    if evidence.get("pack_version") != pack["version"]:
        raise ReleasePromotionError("stable promotion evidence targets a different Pack version")
    if pack.get("status") == "stable":
        raise ReleasePromotionError("a Pack already marked stable does not require stable candidate promotion")
    prepared_by = _actor(evidence.get("prepared_by"), field="promotion evidence preparer")
    sample = evidence.get("sample")
    if not isinstance(sample, dict) or set(sample) != {
        "description", "anonymized", "representative", "entity_ids", "periods",
        "operator_principals", "evidence_references",
    }:
        raise ReleasePromotionError("stable promotion sample fields do not match the strict contract")
    description = str(sample.get("description") or "").strip()
    if len(description) < 12 or len(description) > 500:
        raise ReleasePromotionError("sample description must be 12-500 characters")
    if sample.get("anonymized") is not True or sample.get("representative") is not True:
        raise ReleasePromotionError("stable promotion sample must be anonymized and explicitly representative")
    entity_ids = sample.get("entity_ids")
    periods = sample.get("periods")
    operators = sample.get("operator_principals")
    if (
        not isinstance(entity_ids, list) or not entity_ids
        or any(not isinstance(item, str) or not item for item in entity_ids)
        or len(entity_ids) != len(set(entity_ids))
    ):
        raise ReleasePromotionError("sample.entity_ids must be a unique non-empty string list")
    if set(entity_ids) - set(runtime.entities.ids()):
        raise ReleasePromotionError("stable promotion sample contains an entity outside this Box")
    if pack_id == "feature.multi_entity" and set(entity_ids) != set(runtime.entities.ids()):
        raise ReleasePromotionError(
            "feature.multi_entity promotion sample must cover every configured legal entity"
        )
    if (
        not isinstance(periods, list) or not periods
        or any(not isinstance(item, str) or not PERIOD_PATTERN.fullmatch(item) for item in periods)
        or len(periods) != len(set(periods))
    ):
        raise ReleasePromotionError("sample.periods must be unique YYYY-MM values")
    if (
        not isinstance(operators, list) or not operators
        or any(not isinstance(item, str) for item in operators)
        or len(operators) != len(set(operators))
    ):
        raise ReleasePromotionError("sample.operator_principals must be a unique non-empty list")
    operators = [_actor(item, field="sample operator") for item in operators]
    sample_references = _evidence_references(sample.get("evidence_references"), "sample evidence")
    thresholds = _validate_thresholds(evidence.get("thresholds"))
    clock = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    series_summary, series_private = _validate_pilot_shadow_series_promotion_evidence(
        runtime,
        evidence.get("pilot_shadow_series"),
        clock=clock,
    )
    if periods != series_private["periods"]:
        raise ReleasePromotionError(
            "sample.periods must exactly match the reviewed consecutive Pilot Shadow series"
        )
    if not set(entity_ids).issubset(series_private["entity_ids"]):
        raise ReleasePromotionError(
            "sample.entity_ids must remain inside the reviewed Pilot Shadow series scope"
        )
    series_reviewer = _actor(
        series_private.get("series_review_actor"),
        field="pilot Shadow series reviewer",
    )
    if series_reviewer in {prepared_by, thresholds["approved_by"], *operators}:
        raise ReleasePromotionError(
            "pilot Shadow series reviewer must be separate from the evidence preparer, "
            "threshold approver and sample operators"
        )
    if thresholds["approved_by"] in operators:
        raise ReleasePromotionError("threshold approver must be separate from shadow close operators")
    reports_value = evidence.get("shadow_close_reports")
    if not isinstance(reports_value, list) or not reports_value or len(reports_value) > 2400:
        raise ReleasePromotionError("shadow_close_reports must contain 1-2400 reports")
    reports = [
        _validate_shadow_report(item, runtime_fingerprint=runtime_fingerprint)
        for item in reports_value
    ]
    expected_report_bindings = {
        (item["entity_id"], item["period"]): item["content_sha256"]
        for item in series_private["report_content_bindings"]
        if item["entity_id"] in set(entity_ids)
    }
    actual_report_bindings = {
        (report["entity_id"], report["period"]): _hash(raw)
        for raw, report in zip(reports_value, reports)
    }
    if actual_report_bindings != expected_report_bindings:
        raise ReleasePromotionError(
            "shadow_close_reports must be the exact entity reports bound to the reviewed "
            "consecutive Pilot Shadow series"
        )
    report_scopes = {(item["entity_id"], item["period"]) for item in reports}
    if len(report_scopes) != len(reports):
        raise ReleasePromotionError("stable promotion evidence contains duplicate entity-period reports")
    if {item["entity_id"] for item in reports} != set(entity_ids):
        raise ReleasePromotionError("sample.entity_ids must exactly match shadow close report coverage")
    if {item["period"] for item in reports} != set(periods):
        raise ReleasePromotionError("sample.periods must exactly match shadow close report coverage")
    if any(item["review_actor"] in operators for item in reports):
        raise ReleasePromotionError("shadow close reviewer must be separate from every declared operator")
    if thresholds["approved_by"] in {item["review_actor"] for item in reports}:
        raise ReleasePromotionError("threshold approver must be separate from shadow close reviewers")
    for item in reports:
        reviewed_at = _timestamp(item["reviewed_at"], "shadow close review.reviewed_at")
        if reviewed_at > clock + timedelta(minutes=5):
            raise ReleasePromotionError("shadow close review is dated in the future")
        if reviewed_at < clock - timedelta(days=thresholds["maximum_shadow_age_days"]):
            raise ReleasePromotionError(
                f"shadow close review is older than {thresholds['maximum_shadow_age_days']} days"
            )
    portfolio_values = evidence.get("multi_entity_shadow_close_portfolios")
    if not isinstance(portfolio_values, list) or len(portfolio_values) > 24:
        raise ReleasePromotionError(
            "multi_entity_shadow_close_portfolios must be a list of at most 24 manifests"
        )
    portfolios = [
        _validate_portfolio_shadow_manifest(
            runtime,
            item,
            reports=reports,
            clock=clock,
            maximum_age_days=thresholds["maximum_shadow_age_days"],
        )
        for item in portfolio_values
    ]
    expected_portfolio_bindings = {
        item["period"]: item["content_sha256"]
        for item in series_private["portfolio_content_bindings"]
        if item["period"] in set(periods)
    }
    actual_portfolio_bindings = {
        portfolio["period"]: _hash(raw)
        for raw, portfolio in zip(portfolio_values, portfolios)
    }
    if len(entity_ids) > 1:
        if actual_portfolio_bindings != expected_portfolio_bindings:
            raise ReleasePromotionError(
                "multi-entity promotion portfolios must be the exact manifests bound to the "
                "reviewed consecutive Pilot Shadow series"
            )
        if len(portfolios) != len(periods):
            raise ReleasePromotionError(
                "multi-entity promotion evidence requires one portfolio Shadow Close per sample period"
            )
        if {item["period"] for item in portfolios} != set(periods):
            raise ReleasePromotionError(
                "portfolio Shadow Close periods must exactly match sample.periods"
            )
        if any(item["entity_ids"] != sorted(entity_ids) for item in portfolios):
            raise ReleasePromotionError(
                "portfolio Shadow Close entity scope must exactly match sample.entity_ids"
            )
    elif portfolios:
        raise ReleasePromotionError(
            "single-entity promotion evidence must not include a multi-entity portfolio"
        )
    portfolio_reviewers = {item["review_actor"] for item in portfolios}
    report_reviewers = {item["review_actor"] for item in reports}
    if portfolio_reviewers & (set(operators) | report_reviewers):
        raise ReleasePromotionError(
            "portfolio Shadow Close reviewer must be separate from operators and entity reviewers"
        )
    if thresholds["approved_by"] in portfolio_reviewers:
        raise ReleasePromotionError(
            "threshold approver must be separate from portfolio Shadow Close reviewers"
        )
    if series_reviewer in report_reviewers | portfolio_reviewers:
        raise ReleasePromotionError(
            "pilot Shadow series reviewer must be separate from entity and portfolio reviewers"
        )
    connector_paths = evidence.get("connector_shadow_artifacts")
    if (
        not isinstance(connector_paths, list) or len(connector_paths) > 2400
        or any(not isinstance(item, str) for item in connector_paths)
        or len(connector_paths) != len(set(connector_paths))
    ):
        raise ReleasePromotionError(
            "connector_shadow_artifacts must be a unique list of at most 2400 file paths"
        )
    connector_summaries = [
        _validate_connector_shadow_promotion_artifact(
            runtime,
            item,
            pack_id=pack_id,
            clock=clock,
            maximum_age_days=thresholds["maximum_shadow_age_days"],
        )
        for item in connector_paths
    ]
    connector_fingerprints = [
        item["assessment_fingerprint"] for item in connector_summaries
    ]
    if len(connector_fingerprints) != len(set(connector_fingerprints)):
        raise ReleasePromotionError(
            "stable promotion evidence contains duplicate Connector Shadow assessments"
        )
    connector_scopes = {
        (item["entity_id"], item["sample_period"])
        for item in connector_summaries
    }
    if len(connector_scopes) != len(connector_summaries):
        raise ReleasePromotionError(
            "stable promotion evidence contains duplicate Connector Shadow entity-period scope"
        )
    sample_scopes = {(entity_id, period) for entity_id in entity_ids for period in periods}
    if connector_scopes - sample_scopes:
        raise ReleasePromotionError(
            "Connector Shadow scope must remain inside the stable promotion sample"
        )
    connector_baseline_preparers = {
        item["baseline_prepared_by"] for item in connector_summaries
    }
    connector_reviewers = {item["review_actor"] for item in connector_summaries}
    if (connector_baseline_preparers | connector_reviewers) & set(operators):
        raise ReleasePromotionError(
            "Connector Shadow preparers and reviewers must be separate from sample operators"
        )
    if thresholds["approved_by"] in connector_baseline_preparers | connector_reviewers:
        raise ReleasePromotionError(
            "threshold approver must be separate from Connector Shadow principals"
        )
    if series_reviewer in connector_baseline_preparers | connector_reviewers:
        raise ReleasePromotionError(
            "pilot Shadow series reviewer must be separate from Connector Shadow principals"
        )
    network_connector_pack = pack_id in _network_connector_pack_ids(runtime)
    required_connector_scopes = {
        (entity_id, period)
        for entity_id in entity_ids
        if entity_id in runtime.connector_entity_ids(pack_id)
        for period in periods
    }
    gates_value = evidence.get("automated_gates")
    if not isinstance(gates_value, list):
        raise ReleasePromotionError("automated_gates must be a list")
    gate_index: dict[str, Any] = {}
    for item in gates_value:
        if not isinstance(item, dict) or "gate" not in item:
            raise ReleasePromotionError("automated gate result is invalid")
        gate = str(item.get("gate") or "")
        if gate in gate_index:
            raise ReleasePromotionError("automated gate results contain duplicate gate ids")
        gate_index[gate] = item
    if set(gate_index) != set(AUTOMATED_PROMOTION_GATES):
        raise ReleasePromotionError("automated gate evidence must cover the complete release gate set")
    gates = {
        gate: _validate_timed_evidence(
            {key: value for key, value in gate_index[gate].items() if key != "gate"},
            field=f"automated gate {gate}", runtime_fingerprint=runtime_fingerprint,
            clock=clock, maximum_age_days=thresholds["maximum_gate_age_days"],
        )
        for gate in AUTOMATED_PROMOTION_GATES
    }
    rehearsals_value = evidence.get("rehearsals")
    if not isinstance(rehearsals_value, dict) or set(rehearsals_value) != set(REQUIRED_REHEARSALS):
        raise ReleasePromotionError("rehearsal evidence must cover backup, rollback, authorization and recovery")
    rehearsals = {
        name: _validate_timed_evidence(
            rehearsals_value[name], field=f"rehearsal {name}",
            runtime_fingerprint=runtime_fingerprint, clock=clock,
            maximum_age_days=thresholds["maximum_rehearsal_age_days"],
        )
        for name in REQUIRED_REHEARSALS
    }
    limitations = _validate_known_limitations(evidence.get("known_limitations"))

    blockers: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    distinct_entities = len({item["entity_id"] for item in reports})
    distinct_periods = len({item["period"] for item in reports})
    total_comparisons = sum(item["comparison_count"] for item in reports)
    matched = sum(item["matched_count"] for item in reports)
    exceptions = sum(item["exception_count"] for item in reports)
    missing = sum(item["missing_count"] for item in reports)
    match_rate = matched / total_comparisons if total_comparisons else 0.0
    if distinct_entities < thresholds["minimum_distinct_entities"]:
        blockers.append({"code": "shadow_entity_coverage", "detail": "shadow close entity coverage is below the approved threshold"})
    if distinct_periods < thresholds["minimum_distinct_periods"]:
        blockers.append({"code": "shadow_period_coverage", "detail": "shadow close period coverage is below the approved threshold"})
    if any(item["comparison_count"] < thresholds["minimum_comparisons_per_report"] for item in reports):
        blockers.append({"code": "shadow_report_depth", "detail": "one or more shadow reports have too few comparison rows"})
    required_domains = set(thresholds["required_domains"])
    if any(not required_domains.issubset(item["domains"]) for item in reports):
        blockers.append({"code": "shadow_domain_coverage", "detail": "one or more shadow reports omit a required domain"})
    if match_rate < thresholds["minimum_match_rate"]:
        blockers.append({"code": "shadow_match_rate", "detail": "aggregate shadow close match rate is below the approved threshold"})
    if exceptions > thresholds["maximum_accepted_exceptions"]:
        blockers.append({"code": "shadow_exception_limit", "detail": "accepted shadow close exceptions exceed the approved limit"})
    if missing:
        blockers.append({"code": "shadow_missing_values", "detail": "stable promotion cannot accept Agent or manual baseline omissions"})
    if any("system_defect" in item["exception_classifications"] for item in reports):
        blockers.append({"code": "unresolved_system_defect", "detail": "a system defect cannot be accepted into a stable candidate"})
    if network_connector_pack and not required_connector_scopes:
        blockers.append({
            "code": "connector_binding_scope_empty",
            "detail": "network Connector Pack has no legal entity binding inside the promotion sample",
        })
    if network_connector_pack and connector_scopes != required_connector_scopes:
        blockers.append({
            "code": "connector_shadow_coverage",
            "detail": (
                "network Connector Pack stable promotion requires one reviewed Connector "
                "Shadow artifact for every bound sampled entity-period"
            ),
        })
    if network_connector_pack and any(
        not item["passed"] or item["review_decision"] != "passed"
        for item in connector_summaries
    ):
        blockers.append({
            "code": "connector_shadow_not_passed",
            "detail": "network Connector Pack stable promotion requires passed Connector Shadow controls",
        })
    failed_gates = [gate for gate, item in gates.items() if not item["passed"]]
    if failed_gates:
        blockers.append({"code": "automated_gate_failed", "detail": "one or more required automated gates did not pass"})
    failed_rehearsals = [name for name, item in rehearsals.items() if not item["passed"]]
    if failed_rehearsals:
        blockers.append({"code": "operational_rehearsal_failed", "detail": "one or more required operational rehearsals did not pass"})
    accepted_scope = sum(item["disposition"] == "accepted_scope" for item in limitations)
    if accepted_scope:
        warnings.append({"code": "accepted_scope_limitations", "detail": f"{accepted_scope} known limitations remain inside the approved stable scope"})

    evidence_fingerprint = _hash(evidence)
    evaluated_at = clock.isoformat().replace("+00:00", "Z")
    assessment_core = {
        "schema_version": 1,
        "evaluated_at": evaluated_at,
        "runtime_fingerprint": runtime_fingerprint,
        "pack_id": pack_id,
        "pack_version": pack["version"],
        "current_pack_status": pack["status"],
        "target_status": "stable_candidate",
        "prepared_by": prepared_by,
        "threshold_approved_by": thresholds["approved_by"],
        "evidence_fingerprint": evidence_fingerprint,
        "sample_fingerprint": _hash({
            "description": description,
            "entity_ids": sorted(entity_ids),
            "periods": sorted(periods),
            "evidence_references": sample_references,
        }),
        "pilot_shadow_series_summary": series_summary,
        "report_summaries": reports,
        "portfolio_shadow_summaries": portfolios,
        "connector_shadow_summaries": connector_summaries,
        "gate_evidence_fingerprints": {gate: _hash(item) for gate, item in gates.items()},
        "rehearsal_evidence_fingerprints": {name: _hash(item) for name, item in rehearsals.items()},
        "known_limitation_count": len(limitations),
        "known_limitation_fingerprint": _hash(limitations),
        "metrics": {
            "distinct_entities": distinct_entities,
            "distinct_periods": distinct_periods,
            "report_count": len(reports),
            "portfolio_shadow_count": len(portfolios),
            "connector_shadow_count": len(connector_summaries),
            "comparison_count": total_comparisons,
            "matched_count": matched,
            "exception_count": exceptions,
            "missing_count": missing,
            "match_rate": round(match_rate, 6),
            "automated_gates_passed": len(gates) - len(failed_gates),
            "automated_gates_required": len(gates),
            "rehearsals_passed": len(rehearsals) - len(failed_rehearsals),
            "rehearsals_required": len(rehearsals),
        },
        "thresholds": thresholds,
        "blockers": blockers,
        "warnings": warnings,
        "candidate_eligible": not blockers,
        "separation_principals": sorted(set(
            [
                prepared_by,
                thresholds["approved_by"],
                *operators,
                *(item["review_actor"] for item in reports),
                *(item["review_actor"] for item in portfolios),
                *(item["baseline_prepared_by"] for item in connector_summaries),
                *(item["review_actor"] for item in connector_summaries),
                *series_private["period_principals"],
                series_reviewer,
            ]
        )),
        "raw_shadow_reports_persisted": False,
        "raw_portfolio_shadow_manifests_persisted": False,
        "raw_connector_shadow_artifacts_persisted": False,
        "raw_pilot_shadow_series_artifacts_persisted": False,
        "raw_financial_values_persisted": False,
        "pack_manifest_changed": False,
        "external_actions_performed": False,
    }
    assessment_id = _hash(assessment_core)[:24]
    return {
        **assessment_core,
        "assessment_id": assessment_id,
        "control_note": (
            "Eligibility creates a stable candidate only. It never edits Pack status; an independent "
            "release reviewer must approve this exact assessment fingerprint."
        ),
    }


class ReleasePromotionStore:
    def __init__(self, root: str | Path):
        self.requested_root = Path(root).expanduser()
        self.root = self.requested_root.resolve()
        self.events_file = self.root / "release_promotion_events.jsonl"
        self.lock_file = self.root / ".release_promotion.lock"
        self._lock = threading.RLock()

    def _locked(self):
        if fcntl is None:
            raise ReleasePromotionError("release promotion store requires POSIX file locking")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        handle = self.lock_file.open("a+b")
        os.chmod(self.lock_file, 0o600)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        return handle

    def _events_unlocked(self) -> list[dict[str, Any]]:
        if not self.events_file.exists():
            return []
        if self.events_file.stat().st_size > MAX_LEDGER_BYTES:
            raise ReleasePromotionError("release promotion ledger exceeds 32 MiB")
        events: list[dict[str, Any]] = []
        previous = "GENESIS"
        with self.events_file.open("rb") as handle:
            for sequence, raw in enumerate(handle, 1):
                if len(raw) > MAX_EVENT_BYTES:
                    raise ReleasePromotionError("release promotion event exceeds 256 KiB")
                if sequence > MAX_EVENTS:
                    raise ReleasePromotionError("release promotion ledger exceeds 50000 events")
                try:
                    event = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ReleasePromotionError("release promotion ledger contains invalid JSON") from exc
                if (
                    not isinstance(event, dict)
                    or set(event) != {
                        "schema_version", "sequence", "event_type", "recorded_at",
                        "actor", "payload", "previous_event_hash", "event_hash",
                    }
                    or event.get("schema_version") != 1
                ):
                    raise ReleasePromotionError("release promotion ledger contains an unsupported event")
                _timestamp(event.get("recorded_at"), "release promotion event.recorded_at")
                _actor(event.get("actor"), field="release promotion event actor")
                if not isinstance(event.get("payload"), dict):
                    raise ReleasePromotionError("release promotion ledger event payload is invalid")
                if event.get("sequence") != sequence or event.get("previous_event_hash") != previous:
                    raise ReleasePromotionError("release promotion ledger sequence or chain is invalid")
                supplied = event.get("event_hash")
                unsigned = {key: value for key, value in event.items() if key != "event_hash"}
                if supplied != _hash(unsigned):
                    raise ReleasePromotionError("release promotion event fingerprint mismatch")
                previous = str(supplied)
                events.append(event)
        return events

    def _append_unlocked(self, event_type: str, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        events = self._events_unlocked()
        event = {
            "schema_version": 1,
            "sequence": len(events) + 1,
            "event_type": event_type,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "actor": _actor(actor),
            "payload": payload,
            "previous_event_hash": events[-1]["event_hash"] if events else "GENESIS",
        }
        event["event_hash"] = _hash(event)
        encoded = (_canonical(event) + "\n").encode("utf-8")
        if len(encoded) > MAX_EVENT_BYTES:
            raise ReleasePromotionError("release promotion event exceeds 256 KiB")
        with self.events_file.open("ab") as handle:
            os.chmod(self.events_file, 0o600)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        return event

    @staticmethod
    def _project(events: Iterable[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], dict[str, list[dict[str, Any]]]]:
        assessments: dict[str, dict[str, Any]] = {}
        reviews: dict[str, list[dict[str, Any]]] = {}
        for event in events:
            payload = event.get("payload") or {}
            if event.get("event_type") == "STABLE_PROMOTION_ASSESSMENT_RECORDED":
                assessment = payload.get("assessment")
                assessment_id = str((assessment or {}).get("assessment_id") or "")
                if (
                    set(payload) != {"assessment"}
                    or not isinstance(assessment, dict)
                    or not ASSESSMENT_PATTERN.fullmatch(assessment_id)
                    or assessment_id in assessments
                    or event.get("actor") != assessment.get("prepared_by")
                    or assessment.get("target_status") != "stable_candidate"
                    or assessment.get("raw_shadow_reports_persisted") is not False
                    or assessment.get("raw_financial_values_persisted") is not False
                    or assessment.get("pack_manifest_changed") is not False
                    or assessment.get("external_actions_performed") is not False
                ):
                    raise ReleasePromotionError("release promotion assessment event is invalid")
                assessment_core = {
                    key: value for key, value in assessment.items()
                    if key != "assessment_id"
                }
                if assessment_id != _hash(assessment_core)[:24]:
                    raise ReleasePromotionError("release promotion assessment fingerprint is invalid")
                _validate_persisted_assessment_shape(assessment)
                _timestamp(
                    assessment.get("evaluated_at"),
                    "stable promotion assessment.evaluated_at",
                )
                assessments[assessment_id] = dict(
                    assessment, event_hash=event["event_hash"], recorded_at=event["recorded_at"],
                )
            elif event.get("event_type") == "STABLE_PROMOTION_REVIEW_RECORDED":
                review = payload.get("review")
                assessment_id = str((review or {}).get("assessment_id") or "")
                assessment = assessments.get(assessment_id)
                expected_review_fields = {
                    "assessment_id", "runtime_fingerprint", "pack_id", "pack_version",
                    "decision", "actor", "rationale_sha256", "evidence_references",
                    "supersedes_event_hash", "pack_manifest_changed",
                    "external_actions_performed",
                }
                previous_reviews = reviews.get(assessment_id) or []
                if (
                    set(payload) != {"review"}
                    or not isinstance(review, dict)
                    or assessment is None
                    or set(review) != expected_review_fields
                    or event.get("actor") != review.get("actor")
                    or review.get("decision") not in REVIEW_DECISIONS
                    or review.get("runtime_fingerprint") != assessment.get("runtime_fingerprint")
                    or review.get("pack_id") != assessment.get("pack_id")
                    or review.get("pack_version") != assessment.get("pack_version")
                    or review.get("actor") in set(assessment.get("separation_principals") or [])
                    or not HEX64_PATTERN.fullmatch(str(review.get("rationale_sha256") or ""))
                    or review.get("supersedes_event_hash") != (
                        previous_reviews[-1]["event_hash"] if previous_reviews else None
                    )
                    or review.get("pack_manifest_changed") is not False
                    or review.get("external_actions_performed") is not False
                    or (
                        review.get("decision") == "approved"
                        and assessment.get("candidate_eligible") is not True
                    )
                ):
                    raise ReleasePromotionError("release promotion review event is invalid")
                _actor(review.get("actor"), field="release reviewer")
                _evidence_references(review.get("evidence_references"), "promotion review")
                evaluated_at = _timestamp(
                    assessment.get("evaluated_at"),
                    "stable promotion assessment.evaluated_at",
                )
                reviewed_at = _timestamp(
                    event.get("recorded_at"),
                    "stable promotion review.recorded_at",
                )
                if (
                    reviewed_at < evaluated_at - timedelta(minutes=5)
                    or reviewed_at
                    > evaluated_at + timedelta(days=MAX_ASSESSMENT_REVIEW_AGE_DAYS)
                ):
                    raise ReleasePromotionError(
                        "release promotion review is outside the assessment review window"
                    )
                reviews.setdefault(assessment_id, []).append(dict(
                    review, event_hash=event["event_hash"], recorded_at=event["recorded_at"],
                ))
            else:
                raise ReleasePromotionError("release promotion ledger contains an unknown event type")
        return assessments, reviews

    def record_assessment(self, assessment: dict[str, Any], *, actor: str) -> dict[str, Any]:
        actor = _actor(actor)
        if not isinstance(assessment, dict):
            raise ReleasePromotionError("stable promotion assessment must be an object")
        if actor != assessment.get("prepared_by"):
            raise ReleasePromotionError("assessment recorder must match the evidence preparer")
        assessment_id = str(assessment.get("assessment_id") or "")
        assessment_core = {
            key: value for key, value in assessment.items()
            if key not in {"assessment_id", "control_note"}
        }
        if (
            not ASSESSMENT_PATTERN.fullmatch(assessment_id)
            or assessment_id != _hash(assessment_core)[:24]
        ):
            raise ReleasePromotionError("stable promotion assessment_id is invalid")
        evaluated_at = _timestamp(
            assessment.get("evaluated_at"),
            "stable promotion assessment.evaluated_at",
        )
        clock = datetime.now(timezone.utc)
        if (
            evaluated_at > clock + timedelta(minutes=5)
            or clock > evaluated_at + timedelta(days=MAX_ASSESSMENT_REVIEW_AGE_DAYS)
        ):
            raise ReleasePromotionError(
                "stable promotion assessment recording window expired; build a new assessment"
            )
        persistable = {
            key: value for key, value in assessment.items()
            if key not in {"control_note"}
        }
        _validate_persisted_assessment_shape(persistable)
        if persistable.get("raw_shadow_reports_persisted") is not False or persistable.get("raw_financial_values_persisted") is not False:
            raise ReleasePromotionError("release promotion ledger must not persist raw financial results")
        with self._lock:
            handle = self._locked()
            try:
                events = self._events_unlocked()
                assessments, _ = self._project(events)
                if assessment_id in assessments:
                    raise ReleasePromotionError("this stable promotion assessment is already recorded")
                event = self._append_unlocked(
                    "STABLE_PROMOTION_ASSESSMENT_RECORDED", {"assessment": persistable}, actor,
                )
                return dict(persistable, event_hash=event["event_hash"], recorded=True)
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                handle.close()

    def review(
        self,
        assessment_id: str,
        *,
        runtime_fingerprint: str,
        actor: str,
        decision: str,
        rationale: str,
        evidence_references: list[str],
    ) -> dict[str, Any]:
        if not ASSESSMENT_PATTERN.fullmatch(str(assessment_id or "")):
            raise ReleasePromotionError("assessment_id is invalid")
        if decision not in REVIEW_DECISIONS:
            raise ReleasePromotionError("promotion review decision is invalid")
        actor = _actor(actor)
        rationale = str(rationale or "").strip()
        if len(rationale) < 12 or len(rationale) > 1000:
            raise ReleasePromotionError("promotion review rationale must be 12-1000 characters")
        evidence = _evidence_references(evidence_references, "promotion review")
        with self._lock:
            handle = self._locked()
            try:
                events = self._events_unlocked()
                assessments, reviews = self._project(events)
                assessment = assessments.get(assessment_id)
                if assessment is None:
                    raise ReleasePromotionError("stable promotion assessment was not found")
                evaluated_at = _timestamp(
                    assessment.get("evaluated_at"),
                    "stable promotion assessment.evaluated_at",
                )
                clock = datetime.now(timezone.utc)
                if (
                    evaluated_at > clock + timedelta(minutes=5)
                    or clock
                    > evaluated_at + timedelta(days=MAX_ASSESSMENT_REVIEW_AGE_DAYS)
                ):
                    raise ReleasePromotionError(
                        "stable promotion assessment review window expired; build a new assessment"
                    )
                if assessment.get("runtime_fingerprint") != runtime_fingerprint:
                    raise ReleasePromotionError("stable promotion assessment belongs to a different Box fingerprint")
                if actor in set(assessment.get("separation_principals") or []):
                    raise ReleasePromotionError(
                        "release reviewer must be separate from evidence preparer, operators and shadow reviewers"
                    )
                if decision == "approved" and not assessment.get("candidate_eligible"):
                    raise ReleasePromotionError("a blocked stable promotion assessment cannot be approved")
                review = {
                    "assessment_id": assessment_id,
                    "runtime_fingerprint": runtime_fingerprint,
                    "pack_id": assessment["pack_id"],
                    "pack_version": assessment["pack_version"],
                    "decision": decision,
                    "actor": actor,
                    "rationale_sha256": hashlib.sha256(rationale.encode("utf-8")).hexdigest(),
                    "evidence_references": evidence,
                    "supersedes_event_hash": (
                        reviews.get(assessment_id, [{}])[-1].get("event_hash")
                        if reviews.get(assessment_id) else None
                    ),
                    "pack_manifest_changed": False,
                    "external_actions_performed": False,
                }
                event = self._append_unlocked(
                    "STABLE_PROMOTION_REVIEW_RECORDED", {"review": review}, actor,
                )
                return dict(
                    review,
                    review_event_hash=event["event_hash"],
                    release_status="stable_candidate_approved" if decision == "approved" else decision,
                )
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                handle.close()

    def _status_from_events(
        self,
        events: list[dict[str, Any]],
        *,
        runtime_fingerprint: str,
        limit: int,
    ) -> dict[str, Any]:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 500:
            raise ReleasePromotionError("release promotion status limit must be an integer from 1 to 500")
        assessments, reviews = self._project(events)
        selected = [
            item for item in assessments.values()
            if item.get("runtime_fingerprint") == runtime_fingerprint
        ]
        rows = []
        for item in selected:
            current_review = (reviews.get(item["assessment_id"]) or [None])[-1]
            evaluated_at = _timestamp(
                item.get("evaluated_at"),
                "stable promotion assessment.evaluated_at",
            )
            review_window_open = (
                datetime.now(timezone.utc)
                <= evaluated_at + timedelta(days=MAX_ASSESSMENT_REVIEW_AGE_DAYS)
            )
            rows.append({
                "assessment_id": item["assessment_id"],
                "pack_id": item["pack_id"],
                "pack_version": item["pack_version"],
                "candidate_eligible": item["candidate_eligible"],
                "blocker_count": len(item.get("blockers") or []),
                "warning_count": len(item.get("warnings") or []),
                "metrics": item.get("metrics") or {},
                "recorded_at": item["recorded_at"],
                "review": current_review,
                "review_window_open": review_window_open,
                "release_status": (
                    "stable_candidate_approved"
                    if current_review and current_review.get("decision") == "approved"
                    else current_review.get("decision") if current_review
                    else "awaiting_independent_review" if review_window_open
                    else "assessment_expired"
                ),
            })
        rows.sort(key=lambda item: item["recorded_at"], reverse=True)
        return {
            "schema_version": 1,
            "assessments": rows[:limit],
            "counts": {
                "assessments": len(rows),
                "eligible": sum(item["candidate_eligible"] for item in rows),
                "approved_candidates": sum(
                    item["release_status"] == "stable_candidate_approved" for item in rows
                ),
                "blocked": sum(not item["candidate_eligible"] for item in rows),
                "review_expired": sum(
                    item["release_status"] == "assessment_expired" for item in rows
                ),
            },
            "list_limit": limit,
            "counts_may_be_truncated": len(rows) > limit,
            "raw_shadow_reports_included": False,
            "raw_financial_values_included": False,
            "pack_manifest_changed": False,
            "external_actions_performed": False,
        }

    def status(self, *, runtime_fingerprint: str, limit: int = 100) -> dict[str, Any]:
        with self._lock:
            handle = self._locked()
            try:
                events = self._events_unlocked()
                return self._status_from_events(
                    events,
                    runtime_fingerprint=runtime_fingerprint,
                    limit=limit,
                )
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                handle.close()

    def readiness_snapshot(self, *, runtime_fingerprint: str) -> dict[str, Any]:
        """Read a private ledger without creating files or exposing review identities.

        Production readiness may mount this directory read-only. A concurrent writer is
        synchronized through the existing lock file; missing or weak permissions fail closed.
        """
        if fcntl is None:
            raise ReleasePromotionError("release promotion readiness requires POSIX file locking")
        if not self.requested_root.is_absolute():
            raise ReleasePromotionError("release promotion readiness root must be absolute")
        if self.requested_root.is_symlink() or not self.root.is_dir():
            raise ReleasePromotionError(
                "release promotion readiness root must be an existing real directory"
            )
        if os.name != "nt" and stat.S_IMODE(self.root.stat().st_mode) != 0o700:
            raise ReleasePromotionError("release promotion readiness root must use mode 0700")
        entries = {item.name for item in self.root.iterdir()}
        if not self.events_file.exists():
            if entries:
                raise ReleasePromotionError(
                    "empty release promotion readiness root must not contain unexpected entries"
                )
            events: list[dict[str, Any]] = []
            status = self._status_from_events(
                events,
                runtime_fingerprint=runtime_fingerprint,
                limit=500,
            )
        else:
            if entries != {self.events_file.name, self.lock_file.name}:
                raise ReleasePromotionError(
                    "release promotion readiness root contains unexpected entries"
                )
            for path, label in (
                (self.events_file, "ledger"),
                (self.lock_file, "lock"),
            ):
                if path.is_symlink() or not path.is_file():
                    raise ReleasePromotionError(
                        f"release promotion readiness {label} must be a regular file"
                    )
                if path.stat().st_nlink != 1:
                    raise ReleasePromotionError(
                        f"release promotion readiness {label} must not be hard-linked"
                    )
                if os.name != "nt" and stat.S_IMODE(path.stat().st_mode) != 0o600:
                    raise ReleasePromotionError(
                        f"release promotion readiness {label} must use mode 0600"
                    )
            with self.lock_file.open("rb") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
                try:
                    events = self._events_unlocked()
                    status = self._status_from_events(
                        events,
                        runtime_fingerprint=runtime_fingerprint,
                        limit=500,
                    )
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return {
            "schema_version": 1,
            "ledger_integrity_valid": True,
            "event_count": len(events),
            "counts": dict(status["counts"]),
            "candidates": [{
                "pack_id": item["pack_id"],
                "pack_version": item["pack_version"],
                "candidate_eligible": item["candidate_eligible"],
                "release_status": item["release_status"],
            } for item in status["assessments"]],
            "read_only_inspection": True,
            "paths_returned": False,
            "assessment_ids_returned": False,
            "actors_returned": False,
            "evidence_references_returned": False,
            "raw_shadow_reports_included": False,
            "raw_financial_values_included": False,
            "pack_manifest_changed": False,
            "external_actions_performed": False,
        }

    def verify(self) -> dict[str, Any]:
        with self._lock:
            handle = self._locked()
            try:
                events = self._events_unlocked()
                assessments, reviews = self._project(events)
                return {
                    "valid": True,
                    "integrity": "sha256_hash_chain",
                    "integrity_limit": "tamper_evident_not_immutable",
                    "event_count": len(events),
                    "assessment_count": len(assessments),
                    "review_count": sum(len(items) for items in reviews.values()),
                    "chain_head": events[-1]["event_hash"] if events else "GENESIS",
                    "raw_shadow_reports_stored": False,
                    "raw_financial_values_stored": False,
                    "pack_manifest_changed": False,
                    "external_actions_performed": False,
                }
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                handle.close()


def stable_promotion_policy(runtime_fingerprint: str, packs: Iterable[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "runtime_fingerprint": runtime_fingerprint,
        "targets": [{
            "pack_id": item["id"],
            "pack_version": item["version"],
            "current_status": item["status"],
            "target_status": "stable_candidate",
        } for item in packs if item.get("status") != "stable"],
        "minimum_controls": {
            "shadow_close": {
                "minimum_distinct_periods": 2,
                "minimum_match_rate": 0.98,
                "minimum_comparisons_per_report": 6,
                "required_domains": ["trial_balance", "statement"],
                "missing_values_allowed": False,
                "accepted_exception_limit_maximum": 10,
                "system_defect_may_be_accepted": False,
                "current_fingerprint_bound_review_required": True,
            },
            "consecutive_pilot_shadow_series": {
                "required": True,
                "minimum_consecutive_periods": 2,
                "maximum_periods": 24,
                "current_source_reverification_required": True,
                "approved_independent_review_required": True,
                "exact_report_and_portfolio_content_binding_required": True,
                "system_defect_count_required": 0,
                "private_paths_input_only": True,
            },
            "network_connector_shadow": {
                "required_for_network_connector_packs": True,
                "minimum_schema_version": 2,
                "required_sample_classification": "real_anonymized",
                "independent_source_required": True,
                "private_source_evidence_retention_required": True,
                "legacy_or_demo_baseline_allowed": False,
                "current_independent_review_required": True,
            },
            "automated_gates": list(AUTOMATED_PROMOTION_GATES),
            "operational_rehearsals": list(REQUIRED_REHEARSALS),
            "required_role_separation": [
                "shadow_operator", "shadow_finance_reviewer",
                "shadow_continuity_reviewer", "promotion_evidence_preparer",
                "release_reviewer",
            ],
            "assessment_review_maximum_age_days": MAX_ASSESSMENT_REVIEW_AGE_DAYS,
        },
        "ledger": {
            "default_storage": ".opc-finance-data/release_promotion/release_promotion_events.jsonl",
            "format": "append_only_jsonl",
            "integrity": "sha256_hash_chain",
            "cross_process_locking": True,
            "directory_mode": "0700",
            "file_mode": "0600",
            "raw_shadow_reports_persisted": False,
            "raw_pilot_shadow_series_artifacts_persisted": False,
            "raw_financial_values_persisted": False,
        },
        "shadow_close_artifacts": {
            "commands": [
                "shadow-close-template",
                "shadow-close-compare",
                "shadow-close-review",
                "shadow-close-verify",
                "shadow-close-portfolio-assemble",
                "shadow-close-portfolio-review",
                "shadow-close-portfolio-verify",
            ],
            "scope": "single_entity_reports_plus_all_configured_entities_portfolio",
            "report_file_mode": "0600",
            "overwrite_allowed": False,
            "raw_financial_values_returned_to_stdout": False,
            "portfolio_manifest_persists_raw_financial_values": False,
            "portfolio_review_separate_from_entity_reviewers": True,
            "ledger_changed": False,
        },
        "connector_shadow_artifacts": {
            "commands": [
                "connector-access-request-init",
                "connector-access-request-verify",
                "connector-access-probe",
                "connector-access-receipt-verify",
                "connector-shadow-baseline-init",
                "connector-shadow-baseline-finalize",
                "connector-shadow-assess",
                "connector-shadow-review",
                "connector-shadow-verify",
                "shopify-monthly-shadow-request-init",
                "shopify-monthly-shadow-request-verify",
                "shopify-monthly-shadow-observe",
                "stripe-shadow-request-init",
                "stripe-shadow-request-verify",
                "stripe-shadow-observe",
                "wise-shadow-request-init",
                "wise-shadow-request-verify",
                "wise-shadow-observe",
                "xero-shadow-request-init",
                "xero-shadow-request-verify",
                "xero-shadow-observe",
                "paypal-shadow-request-init",
                "paypal-shadow-request-verify",
                "paypal-shadow-observe",
                "woocommerce-shadow-request-init",
                "woocommerce-shadow-request-verify",
                "woocommerce-shadow-observe",
                "shipbob-shadow-request-init",
                "shipbob-shadow-request-verify",
                "shipbob-shadow-observe",
                "amazon-seller-shadow-request-init",
                "amazon-seller-shadow-request-verify",
                "amazon-seller-shadow-observe",
            ],
            "provider_access_probe": {
                "supported_packs": [
                    "connector.shopify", "connector.stripe",
                    "connector.wise", "connector.xero",
                    "connector.paypal", "connector.woocommerce",
                    "connector.shipbob", "connector.amazon_seller",
                ],
                "request_init_command": (
                    "connector-access-request-init <box-config.json> --pack <pack-id> "
                    "--entity <entity-id> --output <private-request.json>"
                ),
                "request_verify_command": (
                    "connector-access-request-verify <box-config.json> <private-request.json>"
                ),
                "probe_command": (
                    "connector-access-probe <box-config.json> <private-request.json> "
                    "--allow-network --output <private-receipt.json>"
                ),
                "receipt_verify_command": (
                    "connector-access-receipt-verify <box-config.json> "
                    "<private-request.json> <private-receipt.json>"
                ),
                "receipt_maximum_age_days": 30,
                "receipt_is_digital_signature": False,
                "current_receipt_required_for_shopify_and_stripe_shadow": True,
                "current_receipt_required_for_supported_pack_shadow": True,
                "entity_credential_alias_binding_required_for_paypal_woocommerce_shipbob_and_amazon_seller": True,
                "multi_environment_credential_group_receipt_schema": 2,
                "operator_network_opt_in_required": True,
                "browser_initiation_allowed": False,
                "stripe_restricted_key_required": True,
                "provider_account_identifiers_returned": False,
                "raw_provider_responses_returned": False,
                "financial_values_returned": False,
                "paypal_balance_values_requested_but_not_retained": True,
                "woocommerce_write_permission_provider_verified": False,
                "shipbob_exact_read_scope_set_required": True,
                "amazon_seller_financial_values_requested_but_not_retained": True,
                "amazon_seller_id_provider_verified": False,
                "shadow_dispatch_authorized": False,
            },
            "airwallex_observation_command": (
                "airwallex-webhook-process <box-config.json> --request-base "
                "<request-base.json> --actor <worker> --limit 1 --shadow-output "
                "<private-observation.json>"
            ),
            "xero_observation_command": (
                "xero-shadow-observe <box-config.json> <live-trial-balance-request.json> "
                "--access-request <private-access-request.json> "
                "--access-receipt <private-access-receipt.json> "
                "--output <private-observation.json>"
            ),
            "xero_request_init_command": (
                "xero-shadow-request-init <box-config.json> --entity <entity-id> "
                "--period <YYYY-MM> --output <private-request.json>"
            ),
            "xero_request_verify_command": (
                "xero-shadow-request-verify <box-config.json> <private-request.json>"
            ),
            "wise_observation_command": (
                "wise-shadow-observe <box-config.json> <live-monthly-statement-request.json> "
                "--access-request <private-access-request.json> "
                "--access-receipt <private-access-receipt.json> "
                "--output <private-observation.json>"
            ),
            "wise_request_init_command": (
                "wise-shadow-request-init <box-config.json> --entity <entity-id> "
                "--period <YYYY-MM> --output <private-request.json>"
            ),
            "wise_request_verify_command": (
                "wise-shadow-request-verify <box-config.json> <private-request.json>"
            ),
            "paypal_observation_command": (
                "paypal-shadow-observe <box-config.json> <private-request.json> "
                "--access-request <private-access-request.json> "
                "--access-receipt <private-access-receipt.json> "
                "--output <private-observation.json>"
            ),
            "paypal_request_init_command": (
                "paypal-shadow-request-init <box-config.json> --entity <entity-id> "
                "--period <YYYY-MM> --output <private-request.json>"
            ),
            "paypal_request_verify_command": (
                "paypal-shadow-request-verify <box-config.json> <private-request.json>"
            ),
            "woocommerce_observation_command": (
                "woocommerce-shadow-observe <box-config.json> <private-request.json> "
                "--access-request <private-access-request.json> "
                "--access-receipt <private-access-receipt.json> "
                "--output <private-observation.json>"
            ),
            "woocommerce_request_init_command": (
                "woocommerce-shadow-request-init <box-config.json> --entity <entity-id> "
                "--period <YYYY-MM> --output <private-request.json>"
            ),
            "woocommerce_request_verify_command": (
                "woocommerce-shadow-request-verify <box-config.json> <private-request.json>"
            ),
            "shipbob_observation_command": (
                "shipbob-shadow-observe <box-config.json> <private-request.json> "
                "--access-request <private-access-request.json> "
                "--access-receipt <private-access-receipt.json> "
                "--output <private-observation.json>"
            ),
            "shipbob_request_init_command": (
                "shipbob-shadow-request-init <box-config.json> --entity <entity-id> "
                "--period <YYYY-MM> --output <private-request.json>"
            ),
            "shipbob_request_verify_command": (
                "shipbob-shadow-request-verify <box-config.json> <private-request.json>"
            ),
            "amazon_seller_observation_command": (
                "amazon-seller-shadow-observe <box-config.json> <private-request.json> "
                "--access-request <private-access-request.json> "
                "--access-receipt <private-access-receipt.json> "
                "--output <private-observation.json>"
            ),
            "amazon_seller_request_init_command": (
                "amazon-seller-shadow-request-init <box-config.json> --entity <entity-id> "
                "--period <YYYY-MM> --marketplace-id <marketplace-id> "
                "--output <private-request.json>"
            ),
            "amazon_seller_request_verify_command": (
                "amazon-seller-shadow-request-verify <box-config.json> <private-request.json>"
            ),
            "shopify_monthly_request_init_command": (
                "shopify-monthly-shadow-request-init <box-config.json> --entity <entity-id> "
                "--period <YYYY-MM> --output <private-request.json>"
            ),
            "shopify_monthly_request_verify_command": (
                "shopify-monthly-shadow-request-verify <box-config.json> "
                "<private-request.json>"
            ),
            "shopify_monthly_observation_command": (
                "shopify-monthly-shadow-observe <box-config.json> <private-request.json> "
                "--shopify-access-request <private-shopify-access-request.json> "
                "--shopify-access-receipt <private-shopify-access-receipt.json> "
                "--stripe-access-request <private-stripe-access-request.json> "
                "--stripe-access-receipt <private-stripe-access-receipt.json> "
                "--output <private-observation.json>"
            ),
            "stripe_request_init_command": (
                "stripe-shadow-request-init <box-config.json> --entity <entity-id> "
                "--period <YYYY-MM> --output <private-request.json>"
            ),
            "stripe_request_verify_command": (
                "stripe-shadow-request-verify <box-config.json> <private-request.json>"
            ),
            "stripe_observation_command": (
                "stripe-shadow-observe <box-config.json> <private-request.json> "
                "--access-request <private-stripe-access-request.json> "
                "--access-receipt <private-stripe-access-receipt.json> "
                "--output <private-observation.json>"
            ),
            "baseline_schema": "connector-shadow-baseline.schema.json",
            "minimum_promotion_schema_version": 2,
            "required_sample_classification": "real_anonymized",
            "source_independence_attestation_required": True,
            "anonymization_attestation_required": True,
            "legacy_or_demo_artifacts_are_promotion_evidence": False,
            "raw_financial_values_persisted": False,
            "observation_binds_complete_private_pipeline_result_sha256": True,
            "independent_private_source_evidence_required_separately": True,
            "external_actions_performed": False,
        },
        "approval_effect": "stable_candidate_only",
        "pack_manifest_changed_automatically": False,
        "external_actions_performed": False,
        "control_note": (
            "An approved assessment is evidence for a reviewed source change. It never edits a Pack "
            "manifest, never upgrades tax_readiness and never authorizes filing or money movement."
        ),
    }
