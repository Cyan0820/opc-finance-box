from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from .box_runtime import BoxRuntime


class TaxPackLifecycleError(ValueError):
    """Raised when a jurisdiction Pack lifecycle cannot be evaluated safely."""


def source_freshness_from_bundle(
    bundle: dict[str, Any], as_of: Any = None,
) -> dict[str, Any]:
    """Compatibility-shaped freshness result driven by the Pack review policy."""
    try:
        verified_at = date.fromisoformat(str(bundle["rules"]["verified_at"]))
        evaluation_date = _as_of_date(as_of)
        policy = bundle["rules"]["review_policy"]
        max_age_days = policy["max_age_days"]
        warning_days = policy["warning_days_before_expiry"]
    except (KeyError, TypeError, ValueError) as exc:
        raise TaxPackLifecycleError("jurisdiction Pack review policy is invalid") from exc
    age_days = (evaluation_date - verified_at).days
    if age_days < 0:
        raise TaxPackLifecycleError("as_of cannot predate this Pack's rules_verified_at")
    expires_at = verified_at + timedelta(days=max_age_days)
    review_due_at = expires_at - timedelta(days=warning_days)
    if evaluation_date > expires_at:
        status, current = "source_review_expired", False
    elif evaluation_date >= review_due_at:
        status, current = "review_due", True
    else:
        status, current = "current", True
    return {
        "rules_verified_at": verified_at.isoformat(),
        "as_of": evaluation_date.isoformat(),
        "age_days": age_days,
        "max_age_days": max_age_days,
        "warning_days_before_expiry": warning_days,
        "review_due_at": review_due_at.isoformat(),
        "expires_at": expires_at.isoformat(),
        "status": status,
        "current": current,
        "calendar_release_allowed": current,
        "external_filing_release_allowed": current,
    }


def _as_of_date(value: str | date | datetime | None) -> date:
    if value is None:
        return datetime.now(timezone.utc).date()
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise TaxPackLifecycleError("as_of datetime must include timezone")
        return value.astimezone(timezone.utc).date()
    if isinstance(value, date):
        return value
    try:
        parsed = date.fromisoformat(str(value))
    except ValueError as exc:
        raise TaxPackLifecycleError("as_of must use YYYY-MM-DD") from exc
    if parsed.isoformat() != str(value):
        raise TaxPackLifecycleError("as_of must use canonical YYYY-MM-DD")
    return parsed


def evaluate_tax_rule_lifecycle(
    runtime: BoxRuntime,
    *,
    as_of: str | date | datetime | None = None,
) -> dict[str, Any]:
    """Evaluate source-review freshness without calculating tax or releasing filings."""
    evaluation_date = _as_of_date(as_of)
    entities = []
    for entity in runtime.snapshot()["entities"]:
        bundle = runtime.tax_rules(entity["id"])
        rules = bundle["rules"]
        policy = rules["review_policy"]
        verified_at = date.fromisoformat(rules["verified_at"])
        if evaluation_date < verified_at:
            raise TaxPackLifecycleError(
                f"as_of cannot predate {entity['id']} rules_verified_at"
            )
        expires_at = verified_at + timedelta(days=policy["max_age_days"])
        review_due_at = expires_at - timedelta(days=policy["warning_days_before_expiry"])
        if evaluation_date > expires_at:
            status = "expired"
        elif evaluation_date >= review_due_at:
            status = "review_due"
        else:
            status = "current"
        entities.append({
            "entity_id": entity["id"],
            "jurisdiction": entity["jurisdiction"],
            "pack_id": bundle["pack_id"],
            "pack_version": bundle["pack_version"],
            "tax_readiness": entity.get("tax_readiness"),
            "rules_verified_at": verified_at.isoformat(),
            "review_due_at": review_due_at.isoformat(),
            "expires_at": expires_at.isoformat(),
            "age_days": (evaluation_date - verified_at).days,
            "days_until_expiry": (expires_at - evaluation_date).days,
            "status": status,
            "calendar_release_allowed": status != "expired",
            "external_filing_release_allowed": (
                status != "expired" and entity.get("tax_readiness") == "filing_assist"
            ),
            "expiry_effect": policy["expiry_effect"],
            "reverification_triggers": list(policy["reverification_triggers"]),
            "human_review_required": True,
            "tax_calculation_performed": False,
            "external_actions_performed": False,
        })
    counts = {
        status: sum(item["status"] == status for item in entities)
        for status in ("current", "review_due", "expired")
    }
    return {
        "schema_version": 1,
        "as_of": evaluation_date.isoformat(),
        "entities": entities,
        "counts": counts,
        "all_current": counts["review_due"] == 0 and counts["expired"] == 0,
        "calendar_release_allowed": counts["expired"] == 0,
        "external_filing_release_allowed": (
            bool(entities) and counts["expired"] == 0
            and all(item["external_filing_release_allowed"] for item in entities)
        ),
        "tax_calculation_performed": False,
        "external_actions_performed": False,
    }


def build_tax_applicability_questionnaire(runtime: BoxRuntime) -> dict[str, Any]:
    """Build unanswered, non-identifying Pack-scoped tax applicability questions."""
    entities = []
    for entity in runtime.snapshot()["entities"]:
        bundle = runtime.tax_rules(entity["id"])
        rules = bundle["rules"]
        source_ids = [item["id"] for item in rules["sources"]]
        common = {
            "required": True,
            "answer": None,
            "evidence_references": [],
            "human_review_required": True,
            "system_determination_performed": False,
        }
        questions = [
            {
                **common,
                "question_id": "legal_form_and_pack_scope",
                "prompt": "Confirm the legal form and entity facts fall inside this Pack authority scope.",
                "answer_type": "enum",
                "allowed_answers": ["confirmed_in_scope", "confirmed_out_of_scope", "needs_advisor_review"],
                "review_gate": "tax_registration_confirmation",
                "source_ids": source_ids,
            },
            {
                **common,
                "question_id": "tax_residency_and_permanent_establishment",
                "prompt": "Confirm tax residence and any permanent-establishment or dual-residence facts.",
                "answer_type": "enum",
                "allowed_answers": ["confirmed_in_scope", "confirmed_out_of_scope", "needs_advisor_review"],
                "review_gate": "tax_advisor_review",
                "source_ids": source_ids,
            },
            {
                **common,
                "question_id": "direct_and_indirect_tax_registrations",
                "prompt": "Confirm actual direct-tax and indirect-tax registrations and filing frequencies.",
                "answer_type": "enum",
                "allowed_answers": ["confirmed_complete", "confirmed_not_applicable", "needs_evidence", "needs_advisor_review"],
                "review_gate": "tax_registration_confirmation",
                "source_ids": source_ids,
            },
            {
                **common,
                "question_id": "fiscal_year_and_return_periods",
                "prompt": "Confirm fiscal year end, short/broken periods and authority account periods.",
                "answer_type": "enum",
                "allowed_answers": ["confirmed", "needs_evidence", "needs_advisor_review"],
                "review_gate": "tax_advisor_review",
                "source_ids": source_ids,
            },
            {
                **common,
                "question_id": "special_cross_border_and_group_regimes",
                "prompt": "Review cross-border, group, special, payroll and withholding regimes excluded from automatic inference.",
                "answer_type": "enum",
                "allowed_answers": ["reviewed_no_additional_scope", "additional_scope_identified", "needs_advisor_review"],
                "review_gate": "tax_advisor_review",
                "source_ids": source_ids,
            },
        ]
        entities.append({
            "entity_id": entity["id"],
            "jurisdiction": entity["jurisdiction"],
            "functional_currency": entity["functional_currency"],
            "fiscal_year_end": entity["fiscal_year_end"],
            "configured_registration_labels": list(entity.get("tax_registrations") or []),
            "pack_id": bundle["pack_id"],
            "pack_version": bundle["pack_version"],
            "tax_readiness": entity.get("tax_readiness"),
            "authority_scope": (bundle.get("jurisdiction") or {}).get("authority_scope"),
            "scope_note": rules.get("scope_note"),
            "review_policy": dict(rules["review_policy"]),
            "applicability_review_policy": dict(
                rules["applicability_review_policy"]
            ),
            "questionnaire_status": "unanswered",
            "question_count": len(questions),
            "unanswered_count": len(questions),
            "questions": questions,
            "raw_tax_identifiers_requested": False,
            "tax_applicability_determined": False,
            "external_actions_performed": False,
        })
    return {
        "schema_version": 1,
        "artifact_type": "tax_applicability_questionnaire_template",
        "runtime_fingerprint": runtime.snapshot()["fingerprint"],
        "entities": entities,
        "instructions": [
            "Complete every answer from independent entity and authority evidence.",
            "Do not enter tax numbers, credentials or raw identifiers; use private evidence references.",
            "An authorized local tax reviewer must approve applicability before calendar or filing release.",
        ],
        "template_only": True,
        "raw_tax_identifiers_requested": False,
        "tax_calculation_performed": False,
        "external_actions_performed": False,
    }
