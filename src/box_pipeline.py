from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
from typing import Any

from .box_runtime import BoxRuntime
from .cfo_metric_assembly import attach_cfo_metric_assembly
from .connector_sdk import ConnectorError, ConnectorRegistry
from .default_connectors import build_box_connector_registry
from .default_services import build_default_service_registry
from .pack_services import PackServiceError, PackServiceRegistry


class BoxPipelineError(RuntimeError):
    """Raised when a Box pipeline cannot preserve its quality or scope boundary."""


def run_commerce_import_analysis_pipeline(
    runtime: BoxRuntime,
    connector_id: str,
    connector_request: dict[str, Any],
    *,
    connector_registry: ConnectorRegistry | None = None,
    service_registry: PackServiceRegistry | None = None,
) -> dict[str, Any]:
    """Import standard Commerce datasets, stop on quality failure, then run deterministic analysis."""
    try:
        runtime.require_capability("channel.dtc_order_import")
    except Exception as exc:
        raise BoxPipelineError(str(exc)) from exc
    if connector_id not in COMMERCE_CHANNEL_CONNECTORS:
        raise BoxPipelineError("connector_id must be an enabled DTC Commerce Connector")
    connectors = connector_registry or build_box_connector_registry(runtime)
    services = service_registry or build_default_service_registry()
    imported = connectors.dispatch(runtime, connector_id, connector_request)
    batch = imported["batch"]
    canonical = json.dumps({
        "runtime_fingerprint": runtime.snapshot()["fingerprint"],
        "connector_id": connector_id,
        "batch_id": batch["batch_id"],
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    run_id = hashlib.sha256(canonical.encode()).hexdigest()[:24]
    base = {
        "pipeline": {
            "pipeline_id": "commerce.import_analyze",
            "run_id": run_id,
            "executed_at": datetime.now(timezone.utc).isoformat(),
            "stages": ["connector", "quality_gate", "deterministic_analysis"],
            "idempotency_basis": "runtime fingerprint + connector id + connector batch id",
            "required_review_gates": [],
        },
        "connector": imported["connector"],
        "batch": batch,
        "connector_batches": {
            connector_id: {
                "batch_id": batch["batch_id"],
                "source": batch["source"],
                "quality": batch["quality"],
            },
        },
        "external_actions_performed": False,
        "network_access_performed": bool(batch["source"].get("network_access_performed")),
    }
    if not batch["quality"]["ready"]:
        return {
            **base,
            "ready": False,
            "blocked_at": "quality_gate",
            "analysis": None,
            "lineage": {
                "batch_id": batch["batch_id"],
                "source": batch["source"],
                "accepted_record_count": batch["quality"]["record_count"],
                "service_executed": False,
            },
            "blockers": [
                "Connector batch contains rejected rows or duplicate business keys; deterministic analysis was not run."
            ],
        }
    orders = batch["datasets"].get("commerce.orders", [])
    settlements = batch["datasets"].get("commerce.settlements", [])
    scoped_entities = sorted({
        str(row["entity_id"]) for row in orders + settlements if row.get("entity_id")
    })
    if not scoped_entities:
        raise BoxPipelineError("Commerce pipeline requires at least one accepted entity-scoped record")
    analyzed = services.dispatch(
        runtime,
        "commerce.analyze",
        {"orders": orders, "settlements": settlements},
        entity_ids=scoped_entities,
    )
    analysis = analyzed["output"]
    blockers = [
        issue for issue in analysis.get("issues", [])
        if issue.get("severity") in {"blocking", "high"}
    ]
    return {
        **base,
        "ready": bool(analysis.get("ready")),
        "blocked_at": None if analysis.get("ready") else "deterministic_analysis",
        "analysis": analyzed,
        "lineage": {
            "batch_id": batch["batch_id"],
            "source": batch["source"],
            "accepted_record_count": batch["quality"]["record_count"],
            "entity_ids": scoped_entities,
            "service_executed": True,
            "service_id": analyzed["service"]["service_id"],
            "service_entity_ids": analyzed["service"]["entity_ids"],
        },
        "blockers": blockers,
    }


def _entity_scoped_connector_request(request: Any, field: str, entity_id: str) -> dict[str, Any]:
    if not isinstance(request, dict):
        raise BoxPipelineError(f"{field} must be an object")
    output = dict(request)
    configured_entity = output.get("default_entity_id")
    if configured_entity is not None and configured_entity != entity_id:
        raise BoxPipelineError(f"{field}.default_entity_id does not match pipeline entity_id")
    output["default_entity_id"] = entity_id
    return output


def _canonical_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Make semantically unordered evidence rows stable without changing dispatched inputs."""
    return sorted(
        records,
        key=lambda row: json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
    )


GAME_SETTLEMENT_CONNECTORS = frozenset({
    "file.app_store_settlements",
    "file.google_play_settlements",
    "file.domestic_game_settlements",
})
COMMERCE_CHANNEL_CONNECTORS = frozenset({
    "file.commerce",
    "file.csv_commerce",
    "file.xlsx_commerce",
    "example.commerce_api_payload",
})
MARKETPLACE_CHANNEL_CONNECTORS = frozenset({
    "file.marketplace_commerce",
    "example.marketplace_api_payload",
})
BANK_STATEMENT_CONNECTORS = frozenset({"file.bank_statement", "wise.balance_statement"})
TRIAL_BALANCE_CONNECTORS = frozenset({"file.trial_balance", "xero.trial_balance"})
# Close-reconciliation workflows need explicit opening and period movements. The
# first Xero report connector intentionally exposes only point-in-time closing
# balances and keeps Xero YTD columns separate, so it is not eligible here.
CLOSE_TRIAL_BALANCE_CONNECTORS = frozenset({"file.trial_balance"})
GENERAL_LEDGER_CONNECTORS = frozenset({"file.general_ledger"})


def run_expense_evidence_review_pipeline(
    runtime: BoxRuntime,
    request: dict[str, Any],
    *,
    connector_registry: ConnectorRegistry | None = None,
    service_registry: PackServiceRegistry | None = None,
) -> dict[str, Any]:
    entity_id = str(request.get("entity_id") or "")
    if entity_id not in runtime.entities.ids():
        raise BoxPipelineError("expense evidence pipeline requires a configured entity_id")
    connector_request = _entity_scoped_connector_request(
        request.get("connector_request"), "connector_request", entity_id,
    )
    connectors = connector_registry or build_box_connector_registry(runtime)
    services = service_registry or build_default_service_registry()
    imported = connectors.dispatch(
        runtime, "airwallex.approved_expenses", connector_request,
    )
    batch = imported["batch"]
    base = {
        "pipeline": {
            "pipeline_id": "finance.expense_evidence_review",
            "run_id": hashlib.sha256(json.dumps({
                "runtime_fingerprint": runtime.snapshot()["fingerprint"],
                "entity_id": entity_id, "batch_id": batch["batch_id"],
            }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:24],
            "executed_at": datetime.now(timezone.utc).isoformat(),
            "stages": ["expense_connector", "quality_gate", "entity_scope", "expense_evidence_review"],
            "required_review_gates": [
                "airwallex_entity_account_binding_review",
                "airwallex_update_capture_review",
                "airwallex_webhook_binding_and_quarantine_review",
                "airwallex_expense_evidence_review",
                "expense_accounting_mapping_review",
            ],
        },
        "connector": imported["connector"], "batch": batch,
        "connector_batches": {"airwallex.approved_expenses": {
            "batch_id": batch["batch_id"], "source": batch["source"],
            "quality": batch["quality"],
        }},
        "external_actions_performed": False,
        "network_access_performed": bool(batch["source"].get("network_access_performed")),
    }
    if not batch["quality"]["ready"]:
        return {
            **base, "ready": False, "blocked_at": "quality_gate", "services": {},
            "blockers": ["Airwallex expense batch contains rejected or duplicate evidence."],
        }
    rows = batch["datasets"].get("finance.expense_evidence") or []
    state_changes = batch["datasets"].get("finance.expense_evidence_state_changes") or []
    if any(item.get("entity_id") != entity_id for item in rows):
        raise BoxPipelineError("expense evidence escaped the requested legal entity")
    if any(item.get("entity_id") != entity_id for item in state_changes):
        raise BoxPipelineError("expense evidence state change escaped the requested legal entity")
    reviewed = services.dispatch(
        runtime, "airwallex.review_expense_evidence",
        {
            "expense_evidence": rows,
            "expense_evidence_state_changes": state_changes,
        }, entity_id=entity_id,
    )
    output = reviewed["output"]
    return {
        **base, "ready": bool(output["ready_for_review"]),
        "blocked_at": None if output["ready_for_review"] else "expense_evidence_review",
        "services": {"expense_evidence_review": reviewed},
        "founder_briefing": {
            "record_count": output["record_count"],
            "state_change_count": output["state_change_count"],
            "receipt_missing_count": output["receipt_missing_count"],
            "accounting_mapping_missing_count": output["accounting_mapping_missing_count"],
            "currency_record_counts": output["currency_record_counts"],
            "candidate_only": True,
        },
        "blockers": output["blockers"],
        "expense_claims_created": False, "posting_performed": False,
        "payment_performed": False,
    }


def run_bank_statement_close_pipeline(
    runtime: BoxRuntime,
    request: dict[str, Any],
    *,
    connector_registry: ConnectorRegistry | None = None,
    service_registry: PackServiceRegistry | None = None,
) -> dict[str, Any]:
    """Import one entity's bank statement and produce reconciliation candidates only."""
    entity_id = str(request.get("entity_id") or "").strip()
    period = str(request.get("period") or "").strip()
    connector_id = str(request.get("connector_id") or "").strip()
    if entity_id not in runtime.entities.ids():
        raise BoxPipelineError("bank statement pipeline requires a configured entity_id")
    if not re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", period):
        raise BoxPipelineError("bank statement pipeline period must use YYYY-MM")
    if connector_id not in BANK_STATEMENT_CONNECTORS:
        raise BoxPipelineError("connector_id must be an enabled bank statement Connector")
    try:
        runtime.require_capability("finance.bank_reconciliation")
    except Exception as exc:
        raise BoxPipelineError(str(exc)) from exc
    connector_request = _entity_scoped_connector_request(
        request.get("connector_request"), "connector_request", entity_id,
    )
    connectors = connector_registry or build_box_connector_registry(runtime)
    services = service_registry or build_default_service_registry()
    imported = connectors.dispatch(runtime, connector_id, connector_request)
    batch = imported["batch"]
    canonical = json.dumps({
        "runtime_fingerprint": runtime.snapshot()["fingerprint"],
        "entity_id": entity_id, "period": period,
        "connector_id": connector_id, "batch_id": batch["batch_id"],
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    base = {
        "pipeline": {
            "pipeline_id": "finance.bank_statement_close",
            "run_id": hashlib.sha256(canonical.encode()).hexdigest()[:24],
            "executed_at": datetime.now(timezone.utc).isoformat(),
            "stages": ["bank_statement_connector", "quality_gate", "entity_scope", "bank_reconciliation_candidate"],
            "idempotency_basis": "runtime fingerprint + entity + period + connector batch",
            "required_review_gates": [
                "bank_statement_mapping_review", "bank_balance_reconciliation",
                *(
                    [
                        "wise_entity_profile_binding_review",
                        "wise_balance_account_mapping_review",
                        "wise_statement_access_review",
                    ]
                    if connector_id == "wise.balance_statement" else []
                ),
            ],
        },
        "connector": imported["connector"],
        "batch": batch,
        "connector_batches": {connector_id: {
            "batch_id": batch["batch_id"], "source": batch["source"],
            "quality": batch["quality"],
        }},
        "external_actions_performed": False,
        "network_access_performed": bool(batch["source"].get("network_access_performed")),
    }
    if not batch["quality"]["ready"]:
        return {
            **base, "ready": False, "blocked_at": "quality_gate",
            "services": {},
            "lineage": {
                "entity_id": entity_id, "period": period,
                "accepted_record_count": batch["quality"]["record_count"],
                "service_executed": False,
            },
            "blockers": [
                "Bank statement batch contains rejected rows or duplicate transaction ids."
            ],
            "retryable": False,
        }
    transactions = batch["datasets"].get("finance.bank_transactions", [])
    out_of_scope = sorted({
        str(row.get("entity_id")) for row in transactions
        if row.get("entity_id") != entity_id
    })
    if out_of_scope:
        raise BoxPipelineError("bank statement records cross the requested legal entity scope")
    reconciled = services.dispatch(
        runtime, "core.reconcile_bank_activity",
        {"period": period, "transactions": transactions}, entity_id=entity_id,
    )
    output = reconciled["output"]
    return {
        **base,
        "ready": True,
        "blocked_at": None,
        "services": {"bank_reconciliation_candidate": reconciled},
        "lineage": {
            "entity_id": entity_id, "period": period,
            "batch_id": batch["batch_id"],
            "accepted_record_count": len(transactions),
            "service_executed": True,
            "service_id": reconciled["service"]["service_id"],
        },
        "founder_briefing": {
            "entity_id": entity_id, "period": period,
            "account_count": len(output.get("accounts") or []),
            "pending_transaction_count": output.get("pending_count", 0),
            "reconciliation_status": output.get("status"),
            "candidate_only": True,
            "bank_balance_confirmed": bool(output.get("complete")),
            "posting_or_cash_allocation_performed": False,
            "cross_entity_or_currency_netting_prohibited": True,
        },
        "blockers": [],
        "retryable": False,
    }


def run_trial_balance_review_pipeline(
    runtime: BoxRuntime,
    request: dict[str, Any],
    *,
    connector_registry: ConnectorRegistry | None = None,
    service_registry: PackServiceRegistry | None = None,
) -> dict[str, Any]:
    """Import one entity-period trial balance and perform deterministic controls only."""
    entity_id = str(request.get("entity_id") or "").strip()
    period = str(request.get("period") or "").strip()
    connector_id = str(request.get("connector_id") or "").strip()
    if entity_id not in runtime.entities.ids():
        raise BoxPipelineError("trial balance pipeline requires a configured entity_id")
    if not re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", period):
        raise BoxPipelineError("trial balance pipeline period must use YYYY-MM")
    if connector_id not in TRIAL_BALANCE_CONNECTORS:
        raise BoxPipelineError("connector_id must be an enabled trial balance Connector")
    try:
        runtime.require_capability("finance.record_to_report")
    except Exception as exc:
        raise BoxPipelineError(str(exc)) from exc
    connector_request = _entity_scoped_connector_request(
        request.get("connector_request"), "connector_request", entity_id,
    )
    configured_period = connector_request.get("default_period")
    if configured_period is not None and configured_period != period:
        raise BoxPipelineError("connector_request.default_period does not match pipeline period")
    connector_request["default_period"] = period
    connectors = connector_registry or build_box_connector_registry(runtime)
    services = service_registry or build_default_service_registry()
    imported = connectors.dispatch(runtime, connector_id, connector_request)
    batch = imported["batch"]
    canonical = json.dumps({
        "runtime_fingerprint": runtime.snapshot()["fingerprint"],
        "entity_id": entity_id, "period": period,
        "connector_id": connector_id, "batch_id": batch["batch_id"],
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    base = {
        "pipeline": {
            "pipeline_id": "finance.trial_balance_review",
            "run_id": hashlib.sha256(canonical.encode()).hexdigest()[:24],
            "executed_at": datetime.now(timezone.utc).isoformat(),
            "stages": [
                "accounting_export_connector", "quality_gate", "entity_period_scope",
                "trial_balance_validation",
            ],
            "idempotency_basis": "runtime fingerprint + entity + period + connector batch",
            "required_review_gates": [
                "accounting_export_mapping_review", "trial_balance_control_total_review",
                *(
                    ["xero_entity_binding_review", "xero_trial_balance_mapping_review"]
                    if connector_id == "xero.trial_balance" else []
                ),
            ],
        },
        "connector": imported["connector"],
        "batch": batch,
        "connector_batches": {connector_id: {
            "batch_id": batch["batch_id"], "source": batch["source"],
            "quality": batch["quality"],
        }},
        "external_actions_performed": False,
        "network_access_performed": bool(
            batch.get("source", {}).get("network_access_performed")
        ),
        "ledger_or_opening_balances_modified": False,
        "posting_performed": False,
        "period_close_performed": False,
    }
    if not batch["quality"]["ready"]:
        return {
            **base, "ready": False, "blocked_at": "quality_gate", "services": {},
            "lineage": {
                "entity_id": entity_id, "period": period,
                "accepted_record_count": batch["quality"]["record_count"],
                "service_executed": False,
            },
            "blockers": [
                "Trial balance batch contains rejected rows or duplicate account lines."
            ],
            "retryable": False,
        }
    lines = batch["datasets"].get("finance.trial_balance_lines", [])
    out_of_scope = sorted({
        (str(row.get("entity_id") or ""), str(row.get("period") or ""))
        for row in lines
        if row.get("entity_id") != entity_id or row.get("period") != period
    })
    if out_of_scope:
        raise BoxPipelineError("trial balance lines cross the requested entity-period scope")
    validated = services.dispatch(
        runtime, "core.validate_trial_balance_import",
        {"trial_balance_lines": lines}, entity_id=entity_id,
    )
    output = validated["output"]
    ready = bool(output.get("ready"))
    return {
        **base,
        "ready": ready,
        "blocked_at": None if ready else "trial_balance_validation",
        "services": {"trial_balance_validation": validated},
        "lineage": {
            "entity_id": entity_id, "period": period,
            "batch_id": batch["batch_id"], "accepted_record_count": len(lines),
            "service_executed": True,
            "service_id": validated["service"]["service_id"],
        },
        "founder_briefing": {
            "entity_id": entity_id, "period": period,
            "scope_count": len(output.get("summaries") or []),
            "summaries": output.get("summaries") or [],
            "candidate_only": True,
            "balanced_export": ready,
            "account_mapping_or_completeness_proven": False,
            "posting_or_period_close_performed": False,
            "cross_entity_or_currency_netting_prohibited": True,
        },
        "blockers": output.get("issues") or [],
        "retryable": False,
    }


def run_accounting_close_review_pipeline(
    runtime: BoxRuntime,
    request: dict[str, Any],
    *,
    connector_registry: ConnectorRegistry | None = None,
    service_registry: PackServiceRegistry | None = None,
) -> dict[str, Any]:
    """Reconcile external GL and trial balance before producing statement candidates."""
    entity_id = str(request.get("entity_id") or "").strip()
    period = str(request.get("period") or "").strip()
    ledger_connector_id = str(request.get("general_ledger_connector_id") or "").strip()
    trial_connector_id = str(request.get("trial_balance_connector_id") or "").strip()
    if entity_id not in runtime.entities.ids():
        raise BoxPipelineError("accounting close pipeline requires a configured entity_id")
    if not re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", period):
        raise BoxPipelineError("accounting close pipeline period must use YYYY-MM")
    if ledger_connector_id not in GENERAL_LEDGER_CONNECTORS:
        raise BoxPipelineError("general_ledger_connector_id must be file.general_ledger")
    if trial_connector_id not in CLOSE_TRIAL_BALANCE_CONNECTORS:
        raise BoxPipelineError(
            "trial_balance_connector_id must provide explicit opening and period movements"
        )
    account_mappings = request.get("account_mappings")
    if not isinstance(account_mappings, list) or not account_mappings:
        raise BoxPipelineError("accounting close pipeline requires non-empty account_mappings")
    try:
        runtime.require_capability("finance.record_to_report")
        runtime.require_capability("connector.general_ledger_import")
        runtime.require_capability("connector.trial_balance_import")
    except Exception as exc:
        raise BoxPipelineError(str(exc)) from exc

    ledger_request = _entity_scoped_connector_request(
        request.get("general_ledger_connector_request"),
        "general_ledger_connector_request", entity_id,
    )
    trial_request = _entity_scoped_connector_request(
        request.get("trial_balance_connector_request"),
        "trial_balance_connector_request", entity_id,
    )
    for field, connector_request in (
        ("general_ledger_connector_request", ledger_request),
        ("trial_balance_connector_request", trial_request),
    ):
        configured_period = connector_request.get("default_period")
        if configured_period is not None and configured_period != period:
            raise BoxPipelineError(f"{field}.default_period does not match pipeline period")
        connector_request["default_period"] = period

    connectors = connector_registry or build_box_connector_registry(runtime)
    services = service_registry or build_default_service_registry()
    ledger_import = connectors.dispatch(runtime, ledger_connector_id, ledger_request)
    trial_import = connectors.dispatch(runtime, trial_connector_id, trial_request)
    ledger_batch = ledger_import["batch"]
    trial_batch = trial_import["batch"]
    canonical = json.dumps({
        "runtime_fingerprint": runtime.snapshot()["fingerprint"],
        "entity_id": entity_id,
        "period": period,
        "general_ledger_batch_id": ledger_batch["batch_id"],
        "trial_balance_batch_id": trial_batch["batch_id"],
        "account_mappings": _canonical_records(account_mappings),
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    connector_batches = {
        ledger_connector_id: {
            "batch_id": ledger_batch["batch_id"], "source": ledger_batch["source"],
            "quality": ledger_batch["quality"],
        },
        trial_connector_id: {
            "batch_id": trial_batch["batch_id"], "source": trial_batch["source"],
            "quality": trial_batch["quality"],
        },
    }
    base = {
        "pipeline": {
            "pipeline_id": "finance.accounting_close_review",
            "run_id": hashlib.sha256(canonical.encode()).hexdigest()[:24],
            "executed_at": datetime.now(timezone.utc).isoformat(),
            "stages": [
                "general_ledger_connector", "trial_balance_connector", "quality_gate",
                "entity_period_scope", "journal_and_trial_balance_validation",
                "ledger_trial_balance_reconciliation", "financial_statement_candidates",
            ],
            "idempotency_basis": (
                "runtime fingerprint + entity + period + both connector batches + explicit mappings"
            ),
            "required_review_gates": [
                "accounting_export_mapping_review", "trial_balance_control_total_review",
                "financial_statement_mapping_review", "accounting_policy_decision",
            ],
        },
        "connectors": {
            "general_ledger": ledger_import["connector"],
            "trial_balance": trial_import["connector"],
        },
        "batches": {
            "general_ledger": ledger_batch,
            "trial_balance": trial_batch,
        },
        "connector_batches": connector_batches,
        "external_actions_performed": False,
        "network_access_performed": False,
        "ledger_modified": False,
        "opening_balances_modified": False,
        "posting_performed": False,
        "period_close_performed": False,
        "external_filing_performed": False,
    }
    failed_batches = [
        name for name, batch in (
            ("general_ledger", ledger_batch), ("trial_balance", trial_batch),
        )
        if not batch["quality"]["ready"]
    ]
    if failed_batches:
        return {
            **base,
            "ready": False,
            "blocked_at": "quality_gate",
            "services": {},
            "lineage": {
                "entity_id": entity_id, "period": period,
                "service_executed": False,
                "failed_batches": failed_batches,
            },
            "blockers": [
                f"Accounting export batch failed quality gate: {name}"
                for name in failed_batches
            ],
            "retryable": False,
        }

    ledger_lines = ledger_batch["datasets"].get("finance.general_ledger_lines", [])
    trial_lines = trial_batch["datasets"].get("finance.trial_balance_lines", [])
    out_of_scope = sorted({
        (str(row.get("entity_id") or ""), str(row.get("period") or ""))
        for row in ledger_lines + trial_lines
        if row.get("entity_id") != entity_id or row.get("period") != period
    })
    if out_of_scope:
        raise BoxPipelineError("accounting export rows cross the requested entity-period scope")
    reconciled = services.dispatch(
        runtime,
        "core.reconcile_accounting_close_exports",
        {
            "period": period,
            "general_ledger_lines": ledger_lines,
            "trial_balance_lines": trial_lines,
            "account_mappings": account_mappings,
        },
        entity_id=entity_id,
    )
    output = reconciled["output"]
    ready = bool(output.get("ready"))
    return {
        **base,
        "ready": ready,
        "blocked_at": None if ready else "accounting_close_reconciliation",
        "services": {"accounting_close_reconciliation": reconciled},
        "lineage": {
            "entity_id": entity_id, "period": period,
            "general_ledger_batch_id": ledger_batch["batch_id"],
            "trial_balance_batch_id": trial_batch["batch_id"],
            "general_ledger_line_count": len(ledger_lines),
            "trial_balance_line_count": len(trial_lines),
            "service_executed": True,
            "service_id": reconciled["service"]["service_id"],
        },
        "founder_briefing": {
            "entity_id": entity_id, "period": period,
            "currency_scopes": [
                item.get("currency")
                for item in output.get("financial_statement_candidates") or []
            ],
            "mapping_coverage": output.get("mapping_coverage"),
            "account_reconciliation_exception_count": sum(
                not item.get("matched")
                for item in output.get("account_reconciliation") or []
            ),
            "candidate_only": True,
            "mapping_or_completeness_approved": False,
            "posting_or_period_close_performed": False,
            "cross_entity_or_currency_netting_prohibited": True,
        },
        "blockers": output.get("issues") or [],
        "retryable": False,
    }


def run_month_close_control_pipeline(
    runtime: BoxRuntime,
    request: dict[str, Any],
    *,
    connector_registry: ConnectorRegistry | None = None,
    service_registry: PackServiceRegistry | None = None,
) -> dict[str, Any]:
    """Reconcile bank, general-ledger and trial-balance exports into a close-control candidate."""
    entity_id = str(request.get("entity_id") or "").strip()
    period = str(request.get("period") or "").strip()
    bank_connector_id = str(request.get("bank_connector_id") or "").strip()
    ledger_connector_id = str(request.get("general_ledger_connector_id") or "").strip()
    trial_connector_id = str(request.get("trial_balance_connector_id") or "").strip()
    if entity_id not in runtime.entities.ids():
        raise BoxPipelineError("month close control requires a configured entity_id")
    if not re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", period):
        raise BoxPipelineError("month close control period must use YYYY-MM")
    if bank_connector_id not in BANK_STATEMENT_CONNECTORS:
        raise BoxPipelineError("bank_connector_id must be an enabled bank statement Connector")
    if ledger_connector_id not in GENERAL_LEDGER_CONNECTORS:
        raise BoxPipelineError("general_ledger_connector_id must be file.general_ledger")
    if trial_connector_id not in CLOSE_TRIAL_BALANCE_CONNECTORS:
        raise BoxPipelineError(
            "trial_balance_connector_id must provide explicit opening and period movements"
        )
    account_mappings = request.get("account_mappings")
    bank_gl_mappings = request.get("bank_gl_mappings")
    if not isinstance(account_mappings, list) or not account_mappings:
        raise BoxPipelineError("month close control requires non-empty account_mappings")
    if not isinstance(bank_gl_mappings, list) or not bank_gl_mappings:
        raise BoxPipelineError("month close control requires non-empty bank_gl_mappings")
    try:
        runtime.require_capability("finance.bank_reconciliation")
        runtime.require_capability("finance.record_to_report")
        runtime.require_capability("connector.general_ledger_import")
        runtime.require_capability("connector.trial_balance_import")
    except Exception as exc:
        raise BoxPipelineError(str(exc)) from exc

    bank_request = _entity_scoped_connector_request(
        request.get("bank_connector_request"), "bank_connector_request", entity_id,
    )
    ledger_request = _entity_scoped_connector_request(
        request.get("general_ledger_connector_request"),
        "general_ledger_connector_request", entity_id,
    )
    trial_request = _entity_scoped_connector_request(
        request.get("trial_balance_connector_request"),
        "trial_balance_connector_request", entity_id,
    )
    for field, connector_request in (
        ("general_ledger_connector_request", ledger_request),
        ("trial_balance_connector_request", trial_request),
    ):
        configured_period = connector_request.get("default_period")
        if configured_period is not None and configured_period != period:
            raise BoxPipelineError(f"{field}.default_period does not match pipeline period")
        connector_request["default_period"] = period

    connectors = connector_registry or build_box_connector_registry(runtime)
    services = service_registry or build_default_service_registry()
    bank_import = connectors.dispatch(runtime, bank_connector_id, bank_request)
    ledger_import = connectors.dispatch(runtime, ledger_connector_id, ledger_request)
    trial_import = connectors.dispatch(runtime, trial_connector_id, trial_request)
    bank_batch = bank_import["batch"]
    ledger_batch = ledger_import["batch"]
    trial_batch = trial_import["batch"]
    canonical = json.dumps({
        "runtime_fingerprint": runtime.snapshot()["fingerprint"],
        "entity_id": entity_id,
        "period": period,
        "bank_batch_id": bank_batch["batch_id"],
        "general_ledger_batch_id": ledger_batch["batch_id"],
        "trial_balance_batch_id": trial_batch["batch_id"],
        "account_mappings": _canonical_records(account_mappings),
        "bank_gl_mappings": _canonical_records(bank_gl_mappings),
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    connector_batches = {
        bank_connector_id: {
            "batch_id": bank_batch["batch_id"], "source": bank_batch["source"],
            "quality": bank_batch["quality"],
        },
        ledger_connector_id: {
            "batch_id": ledger_batch["batch_id"], "source": ledger_batch["source"],
            "quality": ledger_batch["quality"],
        },
        trial_connector_id: {
            "batch_id": trial_batch["batch_id"], "source": trial_batch["source"],
            "quality": trial_batch["quality"],
        },
    }
    return _finish_month_close_control_pipeline({
        "runtime": runtime, "services": services,
        "entity_id": entity_id, "period": period,
        "canonical": canonical,
        "bank_import": bank_import, "ledger_import": ledger_import,
        "trial_import": trial_import,
        "bank_batch": bank_batch, "ledger_batch": ledger_batch,
        "trial_batch": trial_batch, "connector_batches": connector_batches,
        "account_mappings": account_mappings,
        "bank_gl_mappings": bank_gl_mappings,
    })


def run_first_close_discovery_pipeline(
    runtime: BoxRuntime,
    request: dict[str, Any],
    *,
    connector_registry: ConnectorRegistry | None = None,
    service_registry: PackServiceRegistry | None = None,
) -> dict[str, Any]:
    """Import three close sources and produce exact, fail-closed mapping starters."""
    entity_id = str(request.get("entity_id") or "").strip()
    period = str(request.get("period") or "").strip()
    bank_connector_id = str(request.get("bank_connector_id") or "").strip()
    ledger_connector_id = str(request.get("general_ledger_connector_id") or "").strip()
    trial_connector_id = str(request.get("trial_balance_connector_id") or "").strip()
    if entity_id not in runtime.entities.ids():
        raise BoxPipelineError("first close discovery requires a configured entity_id")
    if not re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", period):
        raise BoxPipelineError("first close discovery period must use YYYY-MM")
    if bank_connector_id not in BANK_STATEMENT_CONNECTORS:
        raise BoxPipelineError("bank_connector_id must be an enabled bank statement Connector")
    if ledger_connector_id not in GENERAL_LEDGER_CONNECTORS:
        raise BoxPipelineError("general_ledger_connector_id must be file.general_ledger")
    if trial_connector_id not in CLOSE_TRIAL_BALANCE_CONNECTORS:
        raise BoxPipelineError(
            "trial_balance_connector_id must provide explicit opening and period movements"
        )
    try:
        runtime.require_capability("finance.bank_reconciliation")
        runtime.require_capability("finance.record_to_report")
        runtime.require_capability("connector.general_ledger_import")
        runtime.require_capability("connector.trial_balance_import")
    except Exception as exc:
        raise BoxPipelineError(str(exc)) from exc

    bank_request = _entity_scoped_connector_request(
        request.get("bank_connector_request"), "bank_connector_request", entity_id,
    )
    ledger_request = _entity_scoped_connector_request(
        request.get("general_ledger_connector_request"),
        "general_ledger_connector_request", entity_id,
    )
    trial_request = _entity_scoped_connector_request(
        request.get("trial_balance_connector_request"),
        "trial_balance_connector_request", entity_id,
    )
    for field, connector_request in (
        ("general_ledger_connector_request", ledger_request),
        ("trial_balance_connector_request", trial_request),
    ):
        configured_period = connector_request.get("default_period")
        if configured_period is not None and configured_period != period:
            raise BoxPipelineError(f"{field}.default_period does not match pipeline period")
        connector_request["default_period"] = period

    connectors = connector_registry or build_box_connector_registry(runtime)
    services = service_registry or build_default_service_registry()
    bank_import = connectors.dispatch(runtime, bank_connector_id, bank_request)
    ledger_import = connectors.dispatch(runtime, ledger_connector_id, ledger_request)
    trial_import = connectors.dispatch(runtime, trial_connector_id, trial_request)
    bank_batch = bank_import["batch"]
    ledger_batch = ledger_import["batch"]
    trial_batch = trial_import["batch"]
    canonical = json.dumps({
        "runtime_fingerprint": runtime.snapshot()["fingerprint"],
        "entity_id": entity_id, "period": period,
        "bank_batch_id": bank_batch["batch_id"],
        "general_ledger_batch_id": ledger_batch["batch_id"],
        "trial_balance_batch_id": trial_batch["batch_id"],
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    connector_batches = {
        bank_connector_id: {
            "batch_id": bank_batch["batch_id"], "source": bank_batch["source"],
            "quality": bank_batch["quality"],
        },
        ledger_connector_id: {
            "batch_id": ledger_batch["batch_id"], "source": ledger_batch["source"],
            "quality": ledger_batch["quality"],
        },
        trial_connector_id: {
            "batch_id": trial_batch["batch_id"], "source": trial_batch["source"],
            "quality": trial_batch["quality"],
        },
    }
    base = {
        "pipeline": {
            "pipeline_id": "finance.first_close_discovery",
            "run_id": hashlib.sha256(canonical.encode()).hexdigest()[:24],
            "executed_at": datetime.now(timezone.utc).isoformat(),
            "stages": [
                "bank_statement_connector", "general_ledger_connector",
                "trial_balance_connector", "quality_gate", "entity_period_scope",
                "bank_source_inventory", "ledger_trial_movement_reconciliation",
                "fail_closed_mapping_starters",
            ],
            "idempotency_basis": (
                "runtime fingerprint + entity + period + three connector batches"
            ),
            "required_review_gates": [
                "bank_statement_mapping_review", "accounting_export_mapping_review",
                "trial_balance_control_total_review", "first_close_configuration_review",
                *(
                    [
                        "wise_entity_profile_binding_review",
                        "wise_balance_account_mapping_review",
                        "wise_statement_access_review",
                    ]
                    if bank_connector_id == "wise.balance_statement" else []
                ),
            ],
        },
        "connectors": {
            "bank_statement": bank_import["connector"],
            "general_ledger": ledger_import["connector"],
            "trial_balance": trial_import["connector"],
        },
        "batches": {
            "bank_statement": bank_batch,
            "general_ledger": ledger_batch,
            "trial_balance": trial_batch,
        },
        "connector_batches": connector_batches,
        "external_actions_performed": False,
        "network_access_performed": any(
            batch["source"].get("network_access_performed")
            for batch in (bank_batch, ledger_batch, trial_batch)
        ),
        "account_classification_inferred": False,
        "bank_gl_mapping_inferred": False,
        "transaction_matching_performed": False,
        "ledger_modified": False,
        "posting_performed": False,
        "period_close_performed": False,
        "external_filing_performed": False,
    }
    failed_batches = [
        name for name, batch in (
            ("bank_statement", bank_batch),
            ("general_ledger", ledger_batch),
            ("trial_balance", trial_batch),
        )
        if not batch["quality"]["ready"]
    ]
    if failed_batches:
        return {
            **base, "ready": False, "blocked_at": "quality_gate", "services": {},
            "lineage": {
                "entity_id": entity_id, "period": period,
                "service_executed": False, "failed_batches": failed_batches,
            },
            "configuration_starter": None,
            "blockers": [
                f"First-close source batch failed quality gate: {name}"
                for name in failed_batches
            ],
            "retryable": False,
        }

    bank_transactions = bank_batch["datasets"].get("finance.bank_transactions", [])
    ledger_lines = ledger_batch["datasets"].get("finance.general_ledger_lines", [])
    trial_lines = trial_batch["datasets"].get("finance.trial_balance_lines", [])
    bank_out_of_scope = [
        row for row in bank_transactions
        if row.get("entity_id") != entity_id
        or str(row.get("transaction_date") or "")[:7] != period
    ]
    accounting_out_of_scope = [
        row for row in ledger_lines + trial_lines
        if row.get("entity_id") != entity_id or row.get("period") != period
    ]
    if bank_out_of_scope or accounting_out_of_scope:
        raise BoxPipelineError("first-close source rows cross the requested entity-period scope")

    bank_reconciled = services.dispatch(
        runtime, "core.reconcile_bank_activity",
        {"period": period, "transactions": bank_transactions}, entity_id=entity_id,
    )
    discovered = services.dispatch(
        runtime, "core.discover_first_close_configuration",
        {
            "period": period,
            "bank_reconciliation": bank_reconciled["output"],
            "general_ledger_lines": ledger_lines,
            "trial_balance_lines": trial_lines,
        },
        entity_id=entity_id,
    )
    output = discovered["output"]
    ready = bool(output.get("ready"))
    next_request = {
        "pipeline_id": "finance.month_close_control",
        "payload": {
            "entity_id": entity_id, "period": period,
            "bank_connector_id": bank_connector_id,
            "bank_connector_request": bank_request,
            "general_ledger_connector_id": ledger_connector_id,
            "general_ledger_connector_request": ledger_request,
            "trial_balance_connector_id": trial_connector_id,
            "trial_balance_connector_request": trial_request,
            "account_mappings": output.get("account_mapping_starters") or [],
            "bank_gl_mappings": output.get("bank_gl_mapping_starters") or [],
        },
    }
    return {
        **base,
        "ready": ready,
        "blocked_at": None if ready else "source_discovery",
        "services": {
            "bank_reconciliation_candidate": bank_reconciled,
            "first_close_configuration_discovery": discovered,
        },
        "lineage": {
            "entity_id": entity_id, "period": period,
            "bank_batch_id": bank_batch["batch_id"],
            "general_ledger_batch_id": ledger_batch["batch_id"],
            "trial_balance_batch_id": trial_batch["batch_id"],
            "bank_transaction_count": len(bank_transactions),
            "general_ledger_line_count": len(ledger_lines),
            "trial_balance_line_count": len(trial_lines),
            "service_executed": True,
            "service_ids": [
                bank_reconciled["service"]["service_id"],
                discovered["service"]["service_id"],
            ],
        },
        "configuration_starter": {
            "ready_for_human_configuration": ready,
            "runnable_without_review": False,
            "next_request": next_request,
            "tasks": output.get("configuration_tasks") or {},
            "guardrail": output.get("guardrail"),
        },
        "founder_briefing": {
            "entity_id": entity_id, "period": period,
            "source_discovery_ready": ready,
            "bank_account_count": len(output.get("bank_account_inventory") or []),
            "active_account_count": len(output.get("account_inventory") or []),
            "statement_mappings_to_review": len(output.get("account_mapping_starters") or []),
            "bank_gl_mappings_to_review": len(output.get("bank_gl_mapping_starters") or []),
            "candidate_only": True,
            "account_or_cash_classification_inferred": False,
            "posting_or_period_close_performed": False,
        },
        "blockers": output.get("issues") or [],
        "retryable": False,
    }


def _finish_month_close_control_pipeline(state: dict[str, Any]) -> dict[str, Any]:
    runtime = state["runtime"]
    services = state["services"]
    entity_id = state["entity_id"]
    period = state["period"]
    canonical = state["canonical"]
    bank_import = state["bank_import"]
    ledger_import = state["ledger_import"]
    trial_import = state["trial_import"]
    bank_batch = state["bank_batch"]
    ledger_batch = state["ledger_batch"]
    trial_batch = state["trial_batch"]
    connector_batches = state["connector_batches"]
    account_mappings = state["account_mappings"]
    bank_gl_mappings = state["bank_gl_mappings"]
    wise_bank = bank_import["connector"]["connector_id"] == "wise.balance_statement"
    base = {
        "pipeline": {
            "pipeline_id": "finance.month_close_control",
            "run_id": hashlib.sha256(canonical.encode()).hexdigest()[:24],
            "executed_at": datetime.now(timezone.utc).isoformat(),
            "stages": [
                "bank_statement_connector", "general_ledger_connector",
                "trial_balance_connector", "quality_gate", "entity_period_scope",
                "bank_reconciliation_candidate", "accounting_close_reconciliation",
                "explicit_bank_gl_mapping", "month_close_control_candidate",
                "founder_monthly_briefing",
            ],
            "idempotency_basis": (
                "runtime fingerprint + entity + period + three connector batches + "
                "explicit statement and bank-to-GL mappings"
            ),
            "required_review_gates": [
                "bank_statement_mapping_review", "bank_balance_reconciliation",
                "accounting_export_mapping_review", "trial_balance_control_total_review",
                "financial_statement_mapping_review", "accounting_policy_decision",
                "month_close_control_review",
                *(
                    [
                        "wise_entity_profile_binding_review",
                        "wise_balance_account_mapping_review",
                        "wise_statement_access_review",
                    ]
                    if wise_bank else []
                ),
            ],
        },
        "connectors": {
            "bank_statement": bank_import["connector"],
            "general_ledger": ledger_import["connector"],
            "trial_balance": trial_import["connector"],
        },
        "batches": {
            "bank_statement": bank_batch,
            "general_ledger": ledger_batch,
            "trial_balance": trial_batch,
        },
        "connector_batches": connector_batches,
        "external_actions_performed": False,
        "network_access_performed": any(
            batch["source"].get("network_access_performed")
            for batch in (bank_batch, ledger_batch, trial_batch)
        ),
        "transaction_matching_performed": False,
        "cash_allocation_performed": False,
        "ledger_modified": False,
        "opening_balances_modified": False,
        "posting_performed": False,
        "period_close_performed": False,
        "external_filing_performed": False,
    }
    failed_batches = [
        name for name, batch in (
            ("bank_statement", bank_batch),
            ("general_ledger", ledger_batch),
            ("trial_balance", trial_batch),
        )
        if not batch["quality"]["ready"]
    ]
    if failed_batches:
        return {
            **base, "ready": False, "blocked_at": "quality_gate", "services": {},
            "lineage": {
                "entity_id": entity_id, "period": period,
                "service_executed": False, "failed_batches": failed_batches,
            },
            "founder_briefing": None,
            "blockers": [
                f"Month-close source batch failed quality gate: {name}"
                for name in failed_batches
            ],
            "retryable": False,
        }

    bank_transactions = bank_batch["datasets"].get("finance.bank_transactions", [])
    ledger_lines = ledger_batch["datasets"].get("finance.general_ledger_lines", [])
    trial_lines = trial_batch["datasets"].get("finance.trial_balance_lines", [])
    out_of_scope = sorted({
        (str(row.get("entity_id") or ""), str(row.get("transaction_date") or "")[:7])
        for row in bank_transactions
        if (
            row.get("entity_id") != entity_id
            or str(row.get("transaction_date") or "")[:7] != period
        )
    } | {
        (str(row.get("entity_id") or ""), str(row.get("period") or ""))
        for row in ledger_lines + trial_lines
        if row.get("entity_id") != entity_id or row.get("period") != period
    })
    if out_of_scope:
        raise BoxPipelineError("month-close source rows cross the requested entity-period scope")

    bank_reconciled = services.dispatch(
        runtime, "core.reconcile_bank_activity",
        {"period": period, "transactions": bank_transactions}, entity_id=entity_id,
    )
    accounting_reconciled = services.dispatch(
        runtime, "core.reconcile_accounting_close_exports",
        {
            "period": period,
            "general_ledger_lines": ledger_lines,
            "trial_balance_lines": trial_lines,
            "account_mappings": account_mappings,
        },
        entity_id=entity_id,
    )
    controlled = services.dispatch(
        runtime, "core.build_month_close_control",
        {
            "period": period,
            "bank_reconciliation": bank_reconciled["output"],
            "accounting_close": accounting_reconciled["output"],
            "trial_balance_lines": trial_lines,
            "bank_gl_mappings": bank_gl_mappings,
        },
        entity_id=entity_id,
    )
    output = controlled["output"]
    ready = bool(output.get("ready"))
    return {
        **base,
        "ready": ready,
        "blocked_at": None if ready else "month_close_control",
        "services": {
            "bank_reconciliation_candidate": bank_reconciled,
            "accounting_close_reconciliation": accounting_reconciled,
            "month_close_control": controlled,
        },
        "lineage": {
            "entity_id": entity_id, "period": period,
            "bank_batch_id": bank_batch["batch_id"],
            "general_ledger_batch_id": ledger_batch["batch_id"],
            "trial_balance_batch_id": trial_batch["batch_id"],
            "bank_transaction_count": len(bank_transactions),
            "general_ledger_line_count": len(ledger_lines),
            "trial_balance_line_count": len(trial_lines),
            "service_executed": True,
            "service_ids": [
                bank_reconciled["service"]["service_id"],
                accounting_reconciled["service"]["service_id"],
                controlled["service"]["service_id"],
            ],
        },
        "founder_briefing": {
            "entity_id": entity_id, "period": period,
            "close_control_ready_for_review": ready,
            "currency_summaries": output.get("currency_briefing") or [],
            "bank_gl_account_controls": output.get("account_controls") or [],
            "accounting_mapping_coverage": accounting_reconciled["output"].get("mapping_coverage"),
            "control_exception_count": len(output.get("issues") or []),
            "candidate_only": True,
            "transaction_matching_or_cash_allocation_performed": False,
            "posting_or_period_close_performed": False,
            "cross_entity_or_currency_netting_prohibited": True,
        },
        "blockers": output.get("issues") or [],
        "retryable": False,
    }


def run_multi_entity_month_close_portfolio_pipeline(
    runtime: BoxRuntime,
    request: dict[str, Any],
    *,
    service_registry: PackServiceRegistry | None = None,
) -> dict[str, Any]:
    """Combine explicit single-entity close candidates into a review-only founder portfolio."""
    period = str(request.get("period") or "").strip()
    raw_entity_ids = request.get("entity_ids")
    controls = request.get("entity_close_controls")
    fx_rates = request.get("fx_rates")
    if not re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", period):
        raise BoxPipelineError("multi-entity month-close portfolio period must use YYYY-MM")
    if (
        not isinstance(raw_entity_ids, list)
        or len(raw_entity_ids) < 2
        or any(not isinstance(item, str) or not item.strip() for item in raw_entity_ids)
    ):
        raise BoxPipelineError("multi-entity month-close portfolio requires at least two entity_ids")
    entity_ids = [item.strip() for item in raw_entity_ids]
    if len(entity_ids) != len(set(entity_ids)):
        raise BoxPipelineError("multi-entity month-close portfolio entity_ids must be unique")
    configured_entities = set(runtime.entities.ids())
    unknown_entities = sorted(set(entity_ids) - configured_entities)
    if unknown_entities:
        raise BoxPipelineError(
            f"multi-entity month-close portfolio has unconfigured entities: {', '.join(unknown_entities)}"
        )
    if not isinstance(controls, list) or any(not isinstance(item, dict) for item in controls):
        raise BoxPipelineError("entity_close_controls must be a list of objects")
    if not isinstance(fx_rates, dict):
        raise BoxPipelineError("fx_rates must be an object keyed by source currency")
    try:
        runtime.require_capability("entity.management_consolidation")
    except Exception as exc:
        raise BoxPipelineError(str(exc)) from exc
    services = service_registry or build_default_service_registry()
    canonical = json.dumps({
        "runtime_fingerprint": runtime.snapshot()["fingerprint"],
        "period": period,
        "entity_ids": sorted(entity_ids),
        "entity_close_controls": _canonical_records(controls),
        "fx_rates": fx_rates,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    portfolio = services.dispatch(
        runtime,
        "entity.build_month_close_portfolio",
        {
            "period": period,
            "entity_close_controls": controls,
            "fx_rates": fx_rates,
        },
        entity_ids=entity_ids,
    )
    output = portfolio["output"]
    ready = bool(output.get("ready"))
    source_runs = [{
        "entity_id": item.get("entity_id"),
        "source_run_id": item.get("source_run_id"),
        "source_attempt_id": item.get("source_attempt_id"),
        "source_evidence": list(item.get("source_evidence") or []),
    } for item in output.get("native_entity_candidates") or []]
    return {
        "pipeline": {
            "pipeline_id": "finance.multi_entity_month_close_portfolio",
            "run_id": hashlib.sha256(canonical.encode()).hexdigest()[:24],
            "executed_at": datetime.now(timezone.utc).isoformat(),
            "stages": [
                "single_entity_close_candidate_scope",
                "per_entity_readiness",
                "explicit_fx_review",
                "management_portfolio_candidate",
                "founder_portfolio_briefing",
            ],
            "idempotency_basis": (
                "runtime fingerprint + period + selected entities + source close controls + explicit FX rates"
            ),
            "required_review_gates": ["month_close_portfolio_review"],
        },
        "ready": ready,
        "blocked_at": None if ready else "month_close_portfolio",
        "services": {"month_close_portfolio": portfolio},
        "lineage": {
            "period": period,
            "entity_ids": sorted(entity_ids),
            "source_runs": source_runs,
            "service_executed": True,
            "service_id": portfolio["service"]["service_id"],
        },
        "founder_briefing": {
            "period": period,
            "reporting_currency": output.get("reporting_currency"),
            "entity_count": output.get("entity_count"),
            "ready_entity_count": output.get("ready_entity_count"),
            "statutory_readiness": output.get("statutory_readiness") or [],
            "management_portfolio_totals": output.get("management_portfolio_totals"),
            "native_entity_candidates": output.get("native_entity_candidates") or [],
            "candidate_only": True,
            "pre_elimination_view": True,
            "cross_entity_native_currency_netting_performed": False,
            "consolidated_financial_statements_produced": False,
            "posting_or_period_close_performed": False,
        },
        "blockers": output.get("blockers") or [],
        "source_access_performed": False,
        "source_run_ledger_verified": False,
        "network_access_performed": False,
        "external_actions_performed": False,
        "statutory_books_modified": False,
        "posting_performed": False,
        "period_close_performed": False,
        "external_filing_performed": False,
        "retryable": False,
    }
def _game_text(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise BoxPipelineError(f"{field} must be a non-empty string")
    return text


def _game_mapping_number(
    value: Any,
    field: str,
    *,
    minimum: Decimal | None = None,
    maximum: Decimal | None = None,
) -> Decimal:
    if isinstance(value, bool) or value in (None, ""):
        raise BoxPipelineError(f"{field} must be a finite decimal")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise BoxPipelineError(f"{field} must be a finite decimal") from exc
    if not number.is_finite():
        raise BoxPipelineError(f"{field} must be a finite decimal")
    if minimum is not None and number < minimum:
        raise BoxPipelineError(f"{field} must be at least {minimum}")
    if maximum is not None and number > maximum:
        raise BoxPipelineError(f"{field} must be at most {maximum}")
    return number


def _normalized_game_channel(value: Any, field: str) -> str:
    return "".join(_game_text(value, field).split()).casefold()


def _game_settlement_key(row: dict[str, Any], field: str) -> tuple[str, str, str, str, str]:
    currency = _game_text(row.get("currency"), f"{field}.currency").upper()
    if len(currency) != 3 or not currency.isascii() or not currency.isalpha():
        raise BoxPipelineError(f"{field}.currency must be a three-letter code")
    period = _game_text(row.get("period"), f"{field}.period")
    if not re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", period):
        raise BoxPipelineError(f"{field}.period must use YYYY-MM")
    return (
        _game_text(row.get("entity_id"), f"{field}.entity_id"),
        period,
        _game_text(row.get("game") or row.get("project_code"), f"{field}.game"),
        _normalized_game_channel(row.get("channel"), f"{field}.channel"),
        currency,
    )


def _merge_game_contract_mappings(
    settlements: list[dict[str, Any]],
    mappings: Any,
    *,
    entity_id: str,
) -> list[dict[str, Any]]:
    """Join explicit contract evidence one-to-one; never infer a commercial term."""
    if not isinstance(mappings, list) or not mappings:
        raise BoxPipelineError("contract_mappings must be a non-empty list of objects")
    if any(not isinstance(mapping, dict) for mapping in mappings):
        raise BoxPipelineError("contract_mappings must contain only objects")
    allowed_fields = {
        "settlement_id", "entity_id", "period", "game", "channel", "currency",
        "contract_basis", "contract_rate", "contract_adjustments", "evidence",
    }
    rows_by_key: dict[tuple[str, str, str, str, str], list[dict[str, Any]]] = {}
    rows_by_id: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(settlements, 1):
        key = _game_settlement_key(row, f"settlements[{index}]")
        rows_by_key.setdefault(key, []).append(row)
        settlement_id = _game_text(row.get("id") or row.get("settlement_id"), f"settlements[{index}].id")
        rows_by_id[settlement_id] = row

    merged: list[dict[str, Any]] = []
    used_ids: set[str] = set()
    for index, mapping in enumerate(mappings, 1):
        field = f"contract_mappings[{index}]"
        unknown_fields = sorted(set(mapping) - allowed_fields)
        if unknown_fields:
            raise BoxPipelineError(f"{field} contains unknown fields: {', '.join(unknown_fields)}")
        if mapping.get("entity_id") != entity_id:
            raise BoxPipelineError(f"{field}.entity_id does not match pipeline entity_id")
        key = _game_settlement_key(mapping, field)
        evidence = mapping.get("evidence")
        if not isinstance(evidence, dict):
            raise BoxPipelineError(f"{field}.evidence must be an object")
        source_reference = _game_text(evidence.get("source_reference"), f"{field}.evidence.source_reference")
        captured_at = _game_text(evidence.get("captured_at"), f"{field}.evidence.captured_at")
        try:
            datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise BoxPipelineError(f"{field}.evidence.captured_at must be an ISO date or timestamp") from exc
        basis = _game_mapping_number(mapping.get("contract_basis"), f"{field}.contract_basis")
        rate = _game_mapping_number(
            mapping.get("contract_rate"), f"{field}.contract_rate",
            minimum=Decimal("0"), maximum=Decimal("1"),
        )
        adjustments = _game_mapping_number(
            mapping.get("contract_adjustments", 0), f"{field}.contract_adjustments",
        )
        settlement_id_value = mapping.get("settlement_id")
        if settlement_id_value not in (None, ""):
            settlement_id = _game_text(settlement_id_value, f"{field}.settlement_id")
            row = rows_by_id.get(settlement_id)
            if row is None:
                raise BoxPipelineError(f"{field}.settlement_id does not exist in the imported batch")
            if _game_settlement_key(row, f"settlement {settlement_id}") != key:
                raise BoxPipelineError(f"{field} business key does not match settlement_id")
        else:
            candidates = rows_by_key.get(key, [])
            if len(candidates) != 1:
                raise BoxPipelineError(
                    f"{field} must match exactly one imported row; provide settlement_id when the business key is ambiguous"
                )
            row = candidates[0]
            settlement_id = _game_text(row.get("id") or row.get("settlement_id"), f"{field}.settlement_id")
        if settlement_id in used_ids:
            raise BoxPipelineError(f"{field} reuses an imported settlement")
        used_ids.add(settlement_id)
        merged.append({
            **row,
            "contract_basis": str(basis),
            "contract_rate": str(rate),
            "contract_adjustments": str(adjustments),
            "evidence": {
                "settlement": dict(row.get("evidence") or {}),
                "contract_mapping": {
                    "source_reference": source_reference,
                    "captured_at": captured_at,
                },
            },
        })
    missing = sorted(set(rows_by_id) - used_ids)
    if missing:
        raise BoxPipelineError(
            "contract_mappings do not cover every imported settlement: " + ", ".join(missing)
        )
    return sorted(
        merged,
        key=lambda row: _game_text(row.get("id") or row.get("settlement_id"), "settlement.id"),
    )


def run_game_channel_settlement_close_pipeline(
    runtime: BoxRuntime,
    request: dict[str, Any],
    *,
    connector_registry: ConnectorRegistry | None = None,
    service_registry: PackServiceRegistry | None = None,
) -> dict[str, Any]:
    """Import one game channel batch, apply explicit contract evidence and reconcile it."""
    if not isinstance(request, dict):
        raise BoxPipelineError("Game settlement pipeline request must be an object")
    entity_id = request.get("entity_id")
    if not isinstance(entity_id, str) or not entity_id.strip():
        raise BoxPipelineError("Game settlement pipeline requires entity_id")
    try:
        runtime.require_entity(entity_id)
        runtime.require_capability("game.channel_settlement")
    except Exception as exc:
        raise BoxPipelineError(str(exc)) from exc
    connector_id = request.get("connector_id")
    if connector_id not in GAME_SETTLEMENT_CONNECTORS:
        raise BoxPipelineError("connector_id must be an enabled game settlement Connector")
    connector_request = _entity_scoped_connector_request(
        request.get("connector_request"), "connector_request", entity_id,
    )
    mappings = request.get("contract_mappings")
    if not isinstance(mappings, list):
        raise BoxPipelineError("contract_mappings must be a list of objects")
    tolerance = _game_mapping_number(
        request.get("tolerance", 0.01), "tolerance",
        minimum=Decimal("0"), maximum=Decimal("1"),
    )
    executed_at = datetime.now(timezone.utc).isoformat()
    stages = [
        "game_settlement_connector", "quality_gate", "contract_mapping",
        "settlement_reconciliation",
    ]
    required_review_gates = ["channel_contract_mapping", "game_principal_agent_assessment"]
    connectors = connector_registry or build_box_connector_registry(runtime)
    services = service_registry or build_default_service_registry()
    try:
        imported = connectors.dispatch(runtime, connector_id, connector_request)
    except (ConnectorError, OSError, ValueError) as exc:
        return {
            "pipeline": {
                "pipeline_id": "game.channel_settlement_close",
                "executed_at": executed_at,
                "stages": stages,
                "required_review_gates": required_review_gates,
            },
            "ready": False,
            "blocked_at": "game_settlement_connector",
            "retryable": True,
            "error": str(exc),
            "connector_batches": {},
            "services": {},
            "external_actions_performed": False,
            "network_access_performed": False,
        }
    batch = imported["batch"]
    canonical = json.dumps({
        "runtime_fingerprint": runtime.snapshot()["fingerprint"],
        "entity_id": entity_id,
        "connector_id": connector_id,
        "batch_id": batch["batch_id"],
        "contract_mappings": _canonical_records(mappings),
        "tolerance": str(tolerance),
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    run_id = hashlib.sha256(canonical.encode()).hexdigest()[:24]
    base = {
        "pipeline": {
            "pipeline_id": "game.channel_settlement_close",
            "run_id": run_id,
            "executed_at": executed_at,
            "stages": stages,
            "idempotency_basis": (
                "runtime fingerprint + connector/batch + canonical contract mapping evidence + tolerance"
            ),
            "required_review_gates": required_review_gates,
        },
        "connector_batches": {
            connector_id: {
                "batch_id": batch["batch_id"],
                "source": batch["source"],
                "quality": batch["quality"],
            },
        },
        "external_actions_performed": False,
        "network_access_performed": bool(batch["source"].get("network_access_performed")),
    }
    return _finish_game_channel_settlement_close(
        runtime=runtime,
        base=base,
        batch=batch,
        mappings=mappings,
        entity_id=entity_id,
        services=services,
        tolerance=tolerance,
        run_id=run_id,
        connector_id=connector_id,
    )


def run_commerce_channel_close_pipeline(
    runtime: BoxRuntime,
    request: dict[str, Any],
    *,
    connector_registry: ConnectorRegistry | None = None,
    service_registry: PackServiceRegistry | None = None,
) -> dict[str, Any]:
    """Close one Commerce entity/channel evidence batch without deciding revenue or tax."""
    if not isinstance(request, dict):
        raise BoxPipelineError("Commerce channel close request must be an object")
    entity_id = request.get("entity_id")
    if not isinstance(entity_id, str) or not entity_id.strip():
        raise BoxPipelineError("Commerce channel close requires entity_id")
    try:
        runtime.require_entity(entity_id)
        runtime.require_capability("channel.dtc_payment_reconciliation")
    except Exception as exc:
        raise BoxPipelineError(str(exc)) from exc
    connector_id = request.get("connector_id")
    if connector_id not in COMMERCE_CHANNEL_CONNECTORS:
        raise BoxPipelineError("connector_id must be an enabled Commerce Connector")
    connector_request = _entity_scoped_connector_request(
        request.get("connector_request"), "connector_request", entity_id,
    )
    tolerance = _game_mapping_number(
        request.get("tolerance", 0.01), "tolerance",
        minimum=Decimal("0"), maximum=Decimal("1"),
    )
    executed_at = datetime.now(timezone.utc).isoformat()
    stages = [
        "commerce_connector", "quality_gate", "entity_scope",
        "order_settlement_reconciliation", "refund_summary",
        "return_inventory_reconciliation", "import_landed_cost_candidates",
        "fulfillment_cost_summary", "destination_evidence",
    ]
    required_review_gates = [
        "commerce_source_mapping", "revenue_cutoff", "inventory_valuation_policy",
        "return_disposition_review", "import_landed_cost_policy", "sales_tax_nexus_review",
    ]
    connectors = connector_registry or build_box_connector_registry(runtime)
    services = service_registry or build_default_service_registry()
    try:
        imported = connectors.dispatch(runtime, connector_id, connector_request)
    except (ConnectorError, OSError, ValueError) as exc:
        return {
            "pipeline": {
                "pipeline_id": "commerce.channel_close",
                "executed_at": executed_at,
                "stages": stages,
                "required_review_gates": required_review_gates,
            },
            "ready": False,
            "blocked_at": "commerce_connector",
            "retryable": True,
            "error": str(exc),
            "connector_batches": {},
            "services": {},
            "external_actions_performed": False,
            "network_access_performed": False,
        }
    batch = imported["batch"]
    canonical = json.dumps({
        "runtime_fingerprint": runtime.snapshot()["fingerprint"],
        "entity_id": entity_id,
        "connector_id": connector_id,
        "connector_request": connector_request,
        "batch_id": batch["batch_id"],
        "tolerance": str(tolerance),
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    run_id = hashlib.sha256(canonical.encode()).hexdigest()[:24]
    base = {
        "pipeline": {
            "pipeline_id": "commerce.channel_close",
            "run_id": run_id,
            "executed_at": executed_at,
            "stages": stages,
            "idempotency_basis": (
                "runtime fingerprint + entity + canonical connector request/batch + tolerance"
            ),
            "required_review_gates": required_review_gates,
        },
        "connector_batches": {
            connector_id: {
                "batch_id": batch["batch_id"],
                "source": batch["source"],
                "quality": batch["quality"],
            },
        },
        "external_actions_performed": False,
        "network_access_performed": bool(batch["source"].get("network_access_performed")),
    }
    if not batch["quality"]["ready"]:
        return {
            **base,
            "ready": False,
            "blocked_at": "quality_gate",
            "retryable": False,
            "services": {},
            "blockers": [f"Connector quality failed: {connector_id}"],
        }
    datasets = batch["datasets"]
    orders = datasets.get("commerce.orders", [])
    settlements = datasets.get("commerce.settlements", [])
    returns = datasets.get("commerce.returns", [])
    return_receipts = datasets.get("commerce.return_receipts", [])
    import_costs = datasets.get("commerce.import_costs", [])
    actual_entities = sorted({
        str(row.get("entity_id"))
        for row in [*orders, *settlements, *returns, *return_receipts, *import_costs]
        if row.get("entity_id")
    })
    if actual_entities != [entity_id]:
        return {
            **base,
            "ready": False,
            "blocked_at": "entity_scope",
            "retryable": False,
            "services": {},
            "blockers": [
                "Commerce close batch must contain exactly the pipeline entity; actual entities: "
                + ", ".join(actual_entities)
            ],
            "lineage": {
                "run_id": run_id,
                "entity_id": entity_id,
                "connector_batch_ids": {connector_id: batch["batch_id"]},
                "accepted_record_count": batch["quality"]["record_count"],
                "service_executed": False,
            },
        }
    invocations = (
        (
            "order_settlement_reconciliation", "commerce.order_to_cash",
            {"orders": orders, "settlements": settlements, "tolerance": str(tolerance)},
        ),
        ("refund_summary", "commerce.refund_summary", {"orders": orders}),
        (
            "return_inventory_reconciliation", "commerce.reconcile_return_inventory",
            {"orders": orders, "returns": returns, "return_receipts": return_receipts},
        ),
        (
            "import_landed_cost_candidates", "commerce.build_import_landed_cost_candidates",
            {"import_costs": import_costs},
        ),
        ("fulfillment_cost_summary", "commerce.fulfillment_cost_summary", {"orders": orders}),
        ("destination_evidence", "dtc.destination_evidence", {"orders": orders}),
    )
    service_map: dict[str, dict[str, Any]] = {}
    for stage, service_id, payload in invocations:
        try:
            service_map[stage] = services.dispatch(
                runtime, service_id, payload, entity_ids=[entity_id],
            )
        except (ValueError, PackServiceError) as exc:
            return {
                **base,
                "ready": False,
                "blocked_at": stage,
                "retryable": False,
                "services": service_map,
                "blockers": [str(exc)],
                "lineage": {
                    "run_id": run_id,
                    "entity_id": entity_id,
                    "connector_batch_ids": {connector_id: batch["batch_id"]},
                    "accepted_record_count": batch["quality"]["record_count"],
                    "service_executed": bool(service_map),
                    "service_ids": [
                        value["service"]["service_id"] for value in service_map.values()
                    ],
                },
            }
    readiness = {stage: bool(value["output"].get("ready")) for stage, value in service_map.items()}
    ready = all(readiness.values())
    blocked_at = next((stage for stage, stage_ready in readiness.items() if not stage_ready), None)
    reconciliation_output = service_map["order_settlement_reconciliation"]["output"]
    return_output = service_map["return_inventory_reconciliation"]["output"]
    import_cost_output = service_map["import_landed_cost_candidates"]["output"]
    return {
        **base,
        "ready": ready,
        "blocked_at": blocked_at,
        "retryable": False,
        "services": service_map,
        "founder_briefing": {
            "order_to_cash_by_entity_channel_currency": reconciliation_output["reconciliations"],
            "refunds_by_entity_channel_currency": service_map["refund_summary"]["output"]["refund_summary"],
            "returns_by_authorization": return_output["reconciliations"],
            "return_receipts_by_warehouse_disposition": return_output["warehouse_disposition_summary"],
            "restock_candidates": return_output["restock_candidates"],
            "import_landed_cost_candidates": import_cost_output["candidates"],
            "destination_tax_evidence": service_map["destination_evidence"]["output"]["destination_summary"],
            "risk_signals": {
                "financial": reconciliation_output["issues"],
                "returns": return_output["issues"],
                "import_costs": import_cost_output["issues"],
            },
            "candidate_only": True,
            "cross_currency_total_prohibited": True,
            "revenue_claim_prohibited": True,
            "tax_due_claim_prohibited": True,
            "margin_requires_inventory_policy_review": True,
            "inventory_adjustment_prohibited": True,
            "customs_or_import_tax_conclusion_prohibited": True,
        },
        "lineage": {
            "run_id": run_id,
            "entity_id": entity_id,
            "connector_batch_ids": {connector_id: batch["batch_id"]},
            "accepted_record_count": batch["quality"]["record_count"],
            "service_executed": True,
            "service_ids": [value["service"]["service_id"] for value in service_map.values()],
        },
        "resume_contract": (
            "No state was changed. Correct source mapping, entity scope or the earliest deterministic "
            "exception and rerun the complete request; identical evidence reproduces run_id."
        ),
    }


def _finish_game_channel_settlement_close(
    *,
    runtime: BoxRuntime,
    base: dict[str, Any],
    batch: dict[str, Any],
    mappings: list[dict[str, Any]],
    entity_id: str,
    services: PackServiceRegistry,
    tolerance: Decimal,
    run_id: str,
    connector_id: str,
) -> dict[str, Any]:
    if not batch["quality"]["ready"]:
        return {
            **base,
            "ready": False,
            "blocked_at": "quality_gate",
            "retryable": False,
            "services": {},
            "blockers": [f"Connector quality failed: {connector_id}"],
        }
    settlements = batch["datasets"]["game.settlements"]
    try:
        mapped = _merge_game_contract_mappings(settlements, mappings, entity_id=entity_id)
    except BoxPipelineError as exc:
        return {
            **base,
            "ready": False,
            "blocked_at": "contract_mapping",
            "retryable": False,
            "services": {},
            "blockers": [str(exc)],
            "lineage": {
                "run_id": run_id,
                "entity_id": entity_id,
                "connector_batch_ids": {connector_id: batch["batch_id"]},
                "accepted_record_count": batch["quality"]["record_count"],
                "contract_mapping_evidence_count": len(mappings),
                "service_executed": False,
            },
        }
    reconciliation = services.dispatch(
        runtime,
        "game.reconcile_channel_settlements",
        {"settlements": mapped, "tolerance": str(tolerance)},
        entity_ids=[entity_id],
    )
    output = reconciliation["output"]
    ready = bool(output.get("ready"))
    by_currency: dict[str, dict[str, Any]] = {}
    for row in output.get("reconciliations", []):
        currency = str(row["currency"])
        summary = by_currency.setdefault(currency, {
            "currency": currency, "settlement_count": 0,
            "expected_settlement": 0.0, "reported_settlement": 0.0,
            "settlement_difference": 0.0, "reported_net_receivable": 0.0,
        })
        summary["settlement_count"] += 1
        for field in (
            "expected_settlement", "reported_settlement", "settlement_difference",
            "reported_net_receivable",
        ):
            summary[field] = round(summary[field] + float(row[field]), 2)
    return {
        **base,
        "ready": ready,
        "blocked_at": None if ready else "settlement_reconciliation",
        "retryable": False,
        "services": {"settlement_reconciliation": reconciliation},
        "founder_briefing": {
            "facts_by_currency": [by_currency[key] for key in sorted(by_currency)],
            "risk_signals": [
                {"settlement_id": item.get("id"), "status": item.get("status") or item.get("reason")}
                for item in output.get("issues", [])
            ],
            "candidate_only": True,
            "cross_currency_total_prohibited": True,
            "revenue_claim_prohibited": True,
        },
        "lineage": {
            "run_id": run_id,
            "entity_id": entity_id,
            "connector_batch_ids": {connector_id: batch["batch_id"]},
            "accepted_record_count": batch["quality"]["record_count"],
            "contract_mapping_evidence_count": len(mappings),
            "service_executed": True,
            "service_ids": [reconciliation["service"]["service_id"]],
        },
        "resume_contract": (
            "No state was changed. Correct the connector batch, explicit contract mapping or settlement "
            "difference and rerun the complete request; identical evidence reproduces run_id."
        ),
    }


def run_marketplace_channel_close_pipeline(
    runtime: BoxRuntime,
    request: dict[str, Any],
    *,
    connector_registry: ConnectorRegistry | None = None,
    service_registry: PackServiceRegistry | None = None,
) -> dict[str, Any]:
    """Reconcile marketplace fees, receivable and inventory without changing stock or books."""
    if not isinstance(request, dict):
        raise BoxPipelineError("Marketplace channel close request must be an object")
    entity_id = request.get("entity_id")
    if not isinstance(entity_id, str) or not entity_id.strip():
        raise BoxPipelineError("Marketplace channel close requires entity_id")
    try:
        runtime.require_entity(entity_id)
        runtime.require_capability("channel.marketplace_receivable")
        runtime.require_capability("channel.marketplace_inventory_reconciliation")
    except Exception as exc:
        raise BoxPipelineError(str(exc)) from exc
    connector_id = request.get("connector_id")
    if connector_id not in MARKETPLACE_CHANNEL_CONNECTORS:
        raise BoxPipelineError("connector_id must be an enabled Marketplace Connector")
    connector_request = _entity_scoped_connector_request(
        request.get("connector_request"), "connector_request", entity_id,
    )
    platform_inventory = request.get("platform_inventory")
    ledger_inventory = request.get("ledger_inventory")
    for field, rows in (
        ("platform_inventory", platform_inventory), ("ledger_inventory", ledger_inventory),
    ):
        if not isinstance(rows, list) or not rows or any(not isinstance(row, dict) for row in rows):
            raise BoxPipelineError(f"{field} must be a non-empty list of objects")
    tolerance = _game_mapping_number(
        request.get("tolerance", 0.01), "tolerance",
        minimum=Decimal("0"), maximum=Decimal("1"),
    )
    executed_at = datetime.now(timezone.utc).isoformat()
    stages = [
        "marketplace_connector", "quality_gate", "entity_scope",
        "marketplace_fee_reconciliation", "marketplace_receivable_reconciliation",
        "return_inventory_reconciliation", "import_landed_cost_candidates",
        "marketplace_inventory_reconciliation",
    ]
    required_review_gates = [
        "commerce_source_mapping", "marketplace_contract_mapping",
        "marketplace_inventory_mapping", "revenue_cutoff", "inventory_valuation_policy",
        "return_disposition_review", "import_landed_cost_policy",
    ]
    connectors = connector_registry or build_box_connector_registry(runtime)
    services = service_registry or build_default_service_registry()
    try:
        imported = connectors.dispatch(runtime, connector_id, connector_request)
    except (ConnectorError, OSError, ValueError) as exc:
        return {
            "pipeline": {
                "pipeline_id": "marketplace.channel_close",
                "executed_at": executed_at,
                "stages": stages,
                "required_review_gates": required_review_gates,
            },
            "ready": False,
            "blocked_at": "marketplace_connector",
            "retryable": True,
            "error": str(exc),
            "connector_batches": {},
            "services": {},
            "external_actions_performed": False,
            "network_access_performed": False,
        }
    batch = imported["batch"]
    canonical = json.dumps({
        "runtime_fingerprint": runtime.snapshot()["fingerprint"],
        "entity_id": entity_id,
        "connector_id": connector_id,
        "connector_request": connector_request,
        "batch_id": batch["batch_id"],
        "platform_inventory": _canonical_records(platform_inventory),
        "ledger_inventory": _canonical_records(ledger_inventory),
        "tolerance": str(tolerance),
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    run_id = hashlib.sha256(canonical.encode()).hexdigest()[:24]
    base = {
        "pipeline": {
            "pipeline_id": "marketplace.channel_close",
            "run_id": run_id,
            "executed_at": executed_at,
            "stages": stages,
            "idempotency_basis": (
                "runtime fingerprint + entity + connector request/batch + canonical inventory evidence + tolerance"
            ),
            "required_review_gates": required_review_gates,
        },
        "connector_batches": {
            connector_id: {
                "batch_id": batch["batch_id"],
                "source": batch["source"],
                "quality": batch["quality"],
            },
        },
        "external_actions_performed": False,
        "network_access_performed": bool(batch["source"].get("network_access_performed")),
    }
    if not batch["quality"]["ready"]:
        return {
            **base, "ready": False, "blocked_at": "quality_gate", "retryable": False,
            "services": {}, "blockers": [f"Connector quality failed: {connector_id}"],
        }
    datasets = batch["datasets"]
    orders = datasets.get("commerce.orders", [])
    settlements = datasets.get("commerce.settlements", [])
    returns = datasets.get("commerce.returns", [])
    return_receipts = datasets.get("commerce.return_receipts", [])
    import_costs = datasets.get("commerce.import_costs", [])
    all_rows = [
        *orders, *settlements, *returns, *return_receipts, *import_costs,
        *platform_inventory, *ledger_inventory,
    ]
    actual_entities = sorted({str(row.get("entity_id")) for row in all_rows if row.get("entity_id")})
    if actual_entities != [entity_id]:
        return {
            **base, "ready": False, "blocked_at": "entity_scope", "retryable": False,
            "services": {},
            "blockers": [
                "Marketplace close evidence must contain exactly the pipeline entity; actual entities: "
                + ", ".join(actual_entities)
            ],
            "lineage": {
                "run_id": run_id, "entity_id": entity_id,
                "connector_batch_ids": {connector_id: batch["batch_id"]},
                "accepted_record_count": batch["quality"]["record_count"],
                "inventory_mapping_evidence_count": len(platform_inventory) + len(ledger_inventory),
                "service_executed": False,
            },
        }
    financial_payload = {
        "orders": orders, "settlements": settlements, "tolerance": str(tolerance),
    }
    invocations = (
        ("marketplace_fee_reconciliation", "marketplace.reconcile_fees", financial_payload),
        (
            "marketplace_receivable_reconciliation", "marketplace.reconcile_receivable",
            financial_payload,
        ),
        (
            "return_inventory_reconciliation", "commerce.reconcile_return_inventory",
            {"orders": orders, "returns": returns, "return_receipts": return_receipts},
        ),
        (
            "import_landed_cost_candidates", "commerce.build_import_landed_cost_candidates",
            {"import_costs": import_costs},
        ),
        (
            "marketplace_inventory_reconciliation", "marketplace.reconcile_inventory",
            {"platform_inventory": platform_inventory, "ledger_inventory": ledger_inventory},
        ),
    )
    service_map: dict[str, dict[str, Any]] = {}
    for stage, service_id, payload in invocations:
        try:
            service_map[stage] = services.dispatch(
                runtime, service_id, payload, entity_ids=[entity_id],
            )
        except (ValueError, PackServiceError) as exc:
            return {
                **base, "ready": False, "blocked_at": stage, "retryable": False,
                "services": service_map, "blockers": [str(exc)],
                "lineage": {
                    "run_id": run_id, "entity_id": entity_id,
                    "connector_batch_ids": {connector_id: batch["batch_id"]},
                    "accepted_record_count": batch["quality"]["record_count"],
                    "inventory_mapping_evidence_count": len(platform_inventory) + len(ledger_inventory),
                    "service_executed": bool(service_map),
                    "service_ids": [
                        value["service"]["service_id"] for value in service_map.values()
                    ],
                },
            }
    readiness = {stage: bool(value["output"].get("ready")) for stage, value in service_map.items()}
    ready = all(readiness.values())
    blocked_at = next((stage for stage, stage_ready in readiness.items() if not stage_ready), None)
    fee_output = service_map["marketplace_fee_reconciliation"]["output"]
    receivable_output = service_map["marketplace_receivable_reconciliation"]["output"]
    inventory_output = service_map["marketplace_inventory_reconciliation"]["output"]
    return_output = service_map["return_inventory_reconciliation"]["output"]
    import_cost_output = service_map["import_landed_cost_candidates"]["output"]
    return {
        **base,
        "ready": ready,
        "blocked_at": blocked_at,
        "retryable": False,
        "services": service_map,
        "founder_briefing": {
            "fees_by_entity_channel_currency": fee_output["fee_reconciliation"],
            "receivable_by_entity_channel_currency": receivable_output["receivable_reconciliation"],
            "inventory_by_sku_warehouse": inventory_output["rows"],
            "returns_by_authorization": return_output["reconciliations"],
            "return_receipts_by_warehouse_disposition": return_output["warehouse_disposition_summary"],
            "restock_candidates": return_output["restock_candidates"],
            "import_landed_cost_candidates": import_cost_output["candidates"],
            "risk_signals": {
                "financial": receivable_output["issues"],
                "returns": return_output["issues"],
                "import_costs": import_cost_output["issues"],
                "inventory": inventory_output["issues"],
            },
            "candidate_only": True,
            "cross_currency_total_prohibited": True,
            "revenue_claim_prohibited": True,
            "tax_due_claim_prohibited": True,
            "inventory_adjustment_prohibited": True,
            "customs_or_import_tax_conclusion_prohibited": True,
        },
        "lineage": {
            "run_id": run_id, "entity_id": entity_id,
            "connector_batch_ids": {connector_id: batch["batch_id"]},
            "accepted_record_count": batch["quality"]["record_count"],
            "inventory_mapping_evidence_count": len(platform_inventory) + len(ledger_inventory),
            "service_executed": True,
            "service_ids": [value["service"]["service_id"] for value in service_map.values()],
        },
        "resume_contract": (
            "No state was changed. Correct marketplace source/contract mapping, receivable evidence or "
            "inventory differences and rerun the complete request; identical evidence reproduces run_id."
        ),
    }


def run_stripe_daily_close_pipeline(
    runtime: BoxRuntime,
    request: dict[str, Any],
    *,
    connector_registry: ConnectorRegistry | None = None,
    service_registry: PackServiceRegistry | None = None,
) -> dict[str, Any]:
    """Run Stripe evidence imports, quality gates, deterministic summaries and bank candidates."""
    if not isinstance(request, dict):
        raise BoxPipelineError("Stripe pipeline request must be an object")
    entity_id = request.get("entity_id")
    if not isinstance(entity_id, str) or not entity_id.strip():
        raise BoxPipelineError("Stripe pipeline requires entity_id")
    try:
        runtime.require_entity(entity_id)
    except Exception as exc:
        raise BoxPipelineError(str(exc)) from exc
    bank_transactions = request.get("bank_transactions")
    if not isinstance(bank_transactions, list) or any(not isinstance(row, dict) for row in bank_transactions):
        raise BoxPipelineError("bank_transactions must be a list of objects")
    balance_request = _entity_scoped_connector_request(
        request.get("balance_request"), "balance_request", entity_id,
    )
    payout_request = _entity_scoped_connector_request(
        request.get("payout_request"), "payout_request", entity_id,
    )
    connectors = connector_registry or build_box_connector_registry(runtime)
    services = service_registry or build_default_service_registry()
    executed_at = datetime.now(timezone.utc).isoformat()
    stages = [
        "stripe_balance_connector", "stripe_payout_connector", "quality_gate",
        "balance_activity_summary", "payout_bank_reconciliation",
    ]

    imported: dict[str, dict[str, Any]] = {}
    for stage, connector_id, connector_request in (
        ("stripe_balance_connector", "stripe.balance_transactions", balance_request),
        ("stripe_payout_connector", "stripe.payouts", payout_request),
    ):
        try:
            imported[connector_id] = connectors.dispatch(runtime, connector_id, connector_request)
        except ConnectorError as exc:
            return {
                "pipeline": {
                    "pipeline_id": "stripe.daily_close",
                    "executed_at": executed_at,
                    "stages": stages,
                    "required_review_gates": ["stripe_mapping_approval"],
                },
                "ready": False,
                "blocked_at": stage,
                "retryable": True,
                "error": str(exc),
                "connector_batches": {
                    key: {
                        "batch_id": value["batch"]["batch_id"],
                        "quality": value["batch"]["quality"],
                    }
                    for key, value in imported.items()
                },
                "services": {},
                "external_actions_performed": False,
            }

    balance_import = imported["stripe.balance_transactions"]
    payout_import = imported["stripe.payouts"]
    balance_batch = balance_import["batch"]
    payout_batch = payout_import["batch"]
    canonical = json.dumps({
        "runtime_fingerprint": runtime.snapshot()["fingerprint"],
        "entity_id": entity_id,
        "balance_batch_id": balance_batch["batch_id"],
        "payout_batch_id": payout_batch["batch_id"],
        "bank_transactions": _canonical_records(bank_transactions),
        "arrival_date_tolerance_days": request.get("arrival_date_tolerance_days", 3),
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    run_id = hashlib.sha256(canonical.encode()).hexdigest()[:24]
    base = {
        "pipeline": {
            "pipeline_id": "stripe.daily_close",
            "run_id": run_id,
            "executed_at": executed_at,
            "stages": stages,
            "idempotency_basis": "runtime fingerprint + connector batch ids + canonical bank evidence",
            "required_review_gates": ["stripe_mapping_approval"],
        },
        "connector_batches": {
            "stripe.balance_transactions": {
                "batch_id": balance_batch["batch_id"],
                "source": balance_batch["source"],
                "quality": balance_batch["quality"],
            },
            "stripe.payouts": {
                "batch_id": payout_batch["batch_id"],
                "source": payout_batch["source"],
                "quality": payout_batch["quality"],
            },
        },
        "external_actions_performed": False,
        "network_access_performed": bool(
            balance_batch["source"].get("network_access_performed")
            or payout_batch["source"].get("network_access_performed")
        ),
    }
    failed_quality = [
        connector_id for connector_id, batch in (
            ("stripe.balance_transactions", balance_batch),
            ("stripe.payouts", payout_batch),
        ) if not batch["quality"]["ready"]
    ]
    if failed_quality:
        return {
            **base,
            "ready": False,
            "blocked_at": "quality_gate",
            "retryable": False,
            "services": {},
            "blockers": [
                f"Connector quality failed: {connector_id}" for connector_id in failed_quality
            ],
        }

    balance_rows = balance_batch["datasets"]["payments.stripe_balance_transactions"]
    payout_rows = payout_batch["datasets"]["payments.stripe_payouts"]
    balance_summary = services.dispatch(
        runtime,
        "stripe.summarize_balance_activity",
        {"balance_transactions": balance_rows},
        entity_id=entity_id,
    )
    reconciliation = services.dispatch(
        runtime,
        "stripe.reconcile_payouts",
        {
            "balance_transactions": balance_rows,
            "payouts": payout_rows,
            "bank_transactions": bank_transactions,
            "arrival_date_tolerance_days": request.get("arrival_date_tolerance_days", 3),
        },
        entity_id=entity_id,
    )
    service_output = reconciliation["output"]
    ready = bool(balance_summary["output"].get("ready") and service_output.get("ready"))
    return {
        **base,
        "ready": ready,
        "blocked_at": None if ready else "payout_bank_reconciliation",
        "retryable": False,
        "services": {
            "balance_activity_summary": balance_summary,
            "payout_bank_reconciliation": reconciliation,
        },
        "founder_briefing": {
            "balance_activity": balance_summary["output"]["founder_briefing"],
            "payout_reconciliation": service_output["founder_briefing"],
            "candidate_only": True,
        },
        "lineage": {
            "run_id": run_id,
            "entity_id": entity_id,
            "balance_batch_id": balance_batch["batch_id"],
            "payout_batch_id": payout_batch["batch_id"],
            "bank_evidence_count": len(bank_transactions),
            "service_ids": [
                balance_summary["service"]["service_id"],
                reconciliation["service"]["service_id"],
            ],
        },
        "resume_contract": (
            "No state was changed. Fix the blocked input and rerun the full request; stable source batches "
            "and bank evidence reproduce the same run_id."
        ),
    }


def _bank_rows_to_minor_units(
    rows: list[dict[str, Any]], currency_minor_units: Any, entity_id: str,
) -> list[dict[str, Any]]:
    if not isinstance(currency_minor_units, dict) or not currency_minor_units:
        raise BoxPipelineError("currency_minor_units must be a non-empty object")
    exponents: dict[str, int] = {}
    for key, exponent in currency_minor_units.items():
        currency = str(key).upper()
        if not re.fullmatch(r"[A-Z]{3}", currency):
            raise BoxPipelineError("currency_minor_units keys must be three-letter currency codes")
        if not isinstance(exponent, int) or isinstance(exponent, bool) or not 0 <= exponent <= 4:
            raise BoxPipelineError(
                f"currency_minor_units.{currency} must be an integer from 0 to 4"
            )
        exponents[currency] = exponent
    output: list[dict[str, Any]] = []
    for index, row in enumerate(rows, 1):
        if row.get("entity_id") != entity_id:
            raise BoxPipelineError("bank Connector returned a record outside the requested entity")
        currency = str(row.get("currency") or "").upper()
        if currency not in exponents:
            raise BoxPipelineError(f"currency_minor_units is missing {currency}")
        try:
            amount = Decimal(str(row.get("amount")))
        except (InvalidOperation, ValueError) as exc:
            raise BoxPipelineError(f"bank transaction {index} amount must be decimal") from exc
        if not amount.is_finite() or amount <= 0:
            raise BoxPipelineError(f"bank transaction {index} amount must be finite and positive")
        minor = amount * (Decimal(10) ** exponents[currency])
        if minor != minor.to_integral_value():
            raise BoxPipelineError(
                f"bank transaction {index} amount has more precision than configured for {currency}"
            )
        direction = str(row.get("direction_code") or "").lower()
        if direction not in {"inflow", "outflow"}:
            raise BoxPipelineError(f"bank transaction {index} requires inflow or outflow direction")
        evidence = row.get("evidence")
        if not isinstance(evidence, dict) or not evidence.get("source_file") or not evidence.get("batch_id"):
            raise BoxPipelineError(f"bank transaction {index} requires source evidence")
        output.append({
            "bank_transaction_id": str(row.get("bank_transaction_id") or ""),
            "transaction_id": str(row.get("transaction_id") or ""),
            "entity_id": entity_id,
            "amount_minor": int(minor),
            "currency": currency,
            "direction": direction,
            "transaction_date": row.get("transaction_date"),
            "account_masked": row.get("account_masked"),
            "reference": row.get("summary") or "",
            "summary": row.get("summary") or "",
            "evidence": dict(evidence),
        })
    return output


def run_shopify_stripe_daily_close_pipeline(
    runtime: BoxRuntime,
    request: dict[str, Any],
    *,
    connector_registry: ConnectorRegistry | None = None,
    service_registry: PackServiceRegistry | None = None,
) -> dict[str, Any]:
    """Run Shopify + Stripe evidence imports and the complete deterministic order-to-cash review."""
    if not isinstance(request, dict):
        raise BoxPipelineError("Shopify + Stripe pipeline request must be an object")
    entity_id = request.get("entity_id")
    if not isinstance(entity_id, str) or not entity_id.strip():
        raise BoxPipelineError("Shopify + Stripe pipeline requires entity_id")
    try:
        runtime.require_entity(entity_id)
        runtime.require_capability("integration.shopify_stripe_order_to_cash")
    except Exception as exc:
        raise BoxPipelineError(str(exc)) from exc
    bank_connector_id = str(request.get("bank_connector_id") or "").strip()
    bank_transactions = request.get("bank_transactions")
    processor_links = request.get("processor_links")
    if bank_connector_id:
        if bank_connector_id not in BANK_STATEMENT_CONNECTORS:
            raise BoxPipelineError("bank_connector_id must be an enabled bank statement Connector")
        if bank_transactions not in (None, []):
            raise BoxPipelineError(
                "use bank_connector_id or direct bank_transactions, not both"
            )
        bank_transactions = []
    elif not isinstance(bank_transactions, list) or any(
        not isinstance(row, dict) for row in bank_transactions
    ):
        raise BoxPipelineError("bank_transactions must be a list of objects")
    if not isinstance(processor_links, list) or any(not isinstance(row, dict) for row in processor_links):
        raise BoxPipelineError("processor_links must be a list of objects")
    shopify_request = _entity_scoped_connector_request(
        request.get("shopify_request"), "shopify_request", entity_id,
    )
    balance_request = _entity_scoped_connector_request(
        request.get("stripe_balance_request"), "stripe_balance_request", entity_id,
    )
    payout_request = _entity_scoped_connector_request(
        request.get("stripe_payout_request"), "stripe_payout_request", entity_id,
    )
    bank_request = (
        _entity_scoped_connector_request(
            request.get("bank_connector_request"), "bank_connector_request", entity_id,
        )
        if bank_connector_id else None
    )
    connectors = connector_registry or build_box_connector_registry(runtime)
    services = service_registry or build_default_service_registry()
    stages = [
        "shopify_orders_connector", "stripe_balance_connector", "stripe_payout_connector",
        *(["bank_statement_connector"] if bank_connector_id else []),
        "quality_gate", "shopify_order_activity", "stripe_balance_activity",
        "shopify_stripe_activity_reconciliation", "stripe_payout_bank_reconciliation",
    ]
    executed_at = datetime.now(timezone.utc).isoformat()
    imported: dict[str, dict[str, Any]] = {}
    connector_invocations = [
        ("shopify_orders_connector", "shopify.orders", shopify_request),
        ("stripe_balance_connector", "stripe.balance_transactions", balance_request),
        ("stripe_payout_connector", "stripe.payouts", payout_request),
    ]
    if bank_connector_id and bank_request is not None:
        connector_invocations.append(("bank_statement_connector", bank_connector_id, bank_request))
    wise_gates = (
        [
            "wise_entity_profile_binding_review",
            "wise_balance_account_mapping_review",
            "wise_statement_access_review",
        ]
        if bank_connector_id == "wise.balance_statement" else []
    )
    for stage, connector_id, connector_request in connector_invocations:
        try:
            imported[connector_id] = connectors.dispatch(runtime, connector_id, connector_request)
        except ConnectorError as exc:
            return {
                "pipeline": {
                    "pipeline_id": "dtc.shopify_stripe_daily_close",
                    "executed_at": executed_at,
                    "stages": stages,
                    "required_review_gates": [
                        "shopify_mapping_approval", "processor_link_mapping_approval",
                        "stripe_mapping_approval",
                        *wise_gates,
                    ],
                },
                "ready": False,
                "blocked_at": stage,
                "retryable": True,
                "error": str(exc),
                "connector_batches": {
                    key: {
                        "batch_id": value["batch"]["batch_id"],
                        "quality": value["batch"]["quality"],
                    }
                    for key, value in imported.items()
                },
                "services": {},
                "external_actions_performed": False,
            }

    batches = {key: value["batch"] for key, value in imported.items()}
    bank_adapter_error = None
    if bank_connector_id and batches[bank_connector_id]["quality"]["ready"]:
        try:
            bank_transactions = _bank_rows_to_minor_units(
                batches[bank_connector_id]["datasets"].get("finance.bank_transactions", []),
                request.get("currency_minor_units"), entity_id,
            )
        except BoxPipelineError as exc:
            bank_adapter_error = str(exc)
    canonical = json.dumps({
        "runtime_fingerprint": runtime.snapshot()["fingerprint"],
        "entity_id": entity_id,
        "connector_batch_ids": {key: value["batch_id"] for key, value in sorted(batches.items())},
        "processor_links": _canonical_records(processor_links),
        "currency_minor_units": request.get("currency_minor_units"),
        "bank_transactions": _canonical_records(bank_transactions),
        "arrival_date_tolerance_days": request.get("arrival_date_tolerance_days", 3),
        "include_test_orders": request.get("include_test_orders") is True,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    run_id = hashlib.sha256(canonical.encode()).hexdigest()[:24]
    base = {
        "pipeline": {
            "pipeline_id": "dtc.shopify_stripe_daily_close",
            "run_id": run_id,
            "executed_at": executed_at,
            "stages": stages,
            "idempotency_basis": "runtime fingerprint + connector batches + canonical links/bank/config evidence",
            "required_review_gates": [
                "shopify_mapping_approval", "processor_link_mapping_approval",
                "stripe_mapping_approval",
                *wise_gates,
            ],
        },
        "connector_batches": {
            key: {
                "batch_id": batch["batch_id"], "source": batch["source"], "quality": batch["quality"],
            } for key, batch in sorted(batches.items())
        },
        "external_actions_performed": False,
        "network_access_performed": any(
            batch["source"].get("network_access_performed") for batch in batches.values()
        ),
    }
    failed_quality = [key for key, batch in batches.items() if not batch["quality"]["ready"]]
    if failed_quality:
        return {
            **base,
            "ready": False,
            "blocked_at": "quality_gate",
            "retryable": False,
            "services": {},
            "blockers": [f"Connector quality failed: {key}" for key in sorted(failed_quality)],
        }
    if bank_adapter_error:
        return {
            **base,
            "ready": False,
            "blocked_at": "bank_minor_unit_adapter",
            "retryable": False,
            "services": {},
            "blockers": [bank_adapter_error],
        }

    shopify_data = batches["shopify.orders"]["datasets"]
    balance_rows = batches["stripe.balance_transactions"]["datasets"]["payments.stripe_balance_transactions"]
    payout_rows = batches["stripe.payouts"]["datasets"]["payments.stripe_payouts"]
    shopify_summary = services.dispatch(
        runtime, "shopify.summarize_order_activity", {
            "orders": shopify_data["commerce.shopify_orders"],
            "transactions": shopify_data["commerce.shopify_transactions"],
            "refunds": shopify_data["commerce.shopify_refunds"],
            "include_test_orders": request.get("include_test_orders") is True,
        }, entity_id=entity_id,
    )
    stripe_summary = services.dispatch(
        runtime, "stripe.summarize_balance_activity",
        {"balance_transactions": balance_rows}, entity_id=entity_id,
    )
    activity_reconciliation = services.dispatch(
        runtime, "dtc.reconcile_shopify_stripe_activity", {
            "shopify_transactions": shopify_data["commerce.shopify_transactions"],
            "stripe_balance_transactions": balance_rows,
            "processor_links": processor_links,
            "currency_minor_units": request.get("currency_minor_units"),
        }, entity_id=entity_id,
    )
    payout_reconciliation = services.dispatch(
        runtime, "stripe.reconcile_payouts", {
            "payouts": payout_rows,
            "balance_transactions": balance_rows,
            "bank_transactions": bank_transactions,
            "arrival_date_tolerance_days": request.get("arrival_date_tolerance_days", 3),
        }, entity_id=entity_id,
    )
    service_map = {
        "shopify_order_activity": shopify_summary,
        "stripe_balance_activity": stripe_summary,
        "shopify_stripe_activity_reconciliation": activity_reconciliation,
        "stripe_payout_bank_reconciliation": payout_reconciliation,
    }
    readiness = {
        key: bool(value["output"].get("ready")) for key, value in service_map.items()
    }
    ready = all(readiness.values())
    blocked_at = next((key for key, value in readiness.items() if not value), None)
    return {
        **base,
        "ready": ready,
        "blocked_at": blocked_at,
        "retryable": False,
        "services": service_map,
        "founder_briefing": {
            "shopify_orders": shopify_summary["output"]["founder_briefing"],
            "shopify_stripe_activity": activity_reconciliation["output"]["founder_briefing"],
            "stripe_payouts": payout_reconciliation["output"]["founder_briefing"],
            "candidate_only": True,
            "margin_claim_prohibited": True,
            "revenue_claim_prohibited": True,
        },
        "lineage": {
            "run_id": run_id,
            "entity_id": entity_id,
            "connector_batch_ids": {key: value["batch_id"] for key, value in sorted(batches.items())},
            "processor_link_evidence_count": len(processor_links),
            "bank_evidence_count": len(bank_transactions),
            "bank_connector_id": bank_connector_id or None,
            "service_ids": [value["service"]["service_id"] for value in service_map.values()],
        },
        "resume_contract": (
            "No state was changed. Correct the earliest blocked connector, quality or reconciliation input "
            "and rerun the complete request; identical evidence reproduces run_id."
        ),
    }


def run_shopify_stripe_month_close_pipeline(
    runtime: BoxRuntime,
    request: dict[str, Any],
    *,
    connector_registry: ConnectorRegistry | None = None,
    service_registry: PackServiceRegistry | None = None,
) -> dict[str, Any]:
    """Run the close-captured Shopify monthly metric source with same-window Stripe evidence."""
    try:
        runtime.require_capability("integration.shopify_stripe_monthly_close")
    except Exception as exc:
        raise BoxPipelineError(str(exc)) from exc
    if not isinstance(request, dict):
        raise BoxPipelineError("Shopify Stripe month close request must be an object")
    entity_id = request.get("entity_id")
    if entity_id not in runtime.entities.ids():
        raise BoxPipelineError("entity_id must be a configured legal entity")
    processor_links = request.get("processor_links")
    if not isinstance(processor_links, list) or any(
        not isinstance(row, dict) for row in processor_links
    ):
        raise BoxPipelineError("processor_links must be a list of objects")
    shopify_request = _entity_scoped_connector_request(
        request.get("shopify_monthly_request"), "shopify_monthly_request", entity_id,
    )
    balance_request = _entity_scoped_connector_request(
        request.get("stripe_balance_request"), "stripe_balance_request", entity_id,
    )
    connectors = connector_registry or build_box_connector_registry(runtime)
    services = service_registry or build_default_service_registry()
    stages = [
        "shopify_monthly_order_evidence_connector", "stripe_balance_connector",
        "quality_gate", "canonical_month_scope_gate", "shopify_monthly_commerce_scope",
        "stripe_balance_activity", "shopify_stripe_activity_reconciliation",
    ]
    executed_at = datetime.now(timezone.utc).isoformat()
    imported: dict[str, dict[str, Any]] = {}
    for stage, connector_id, connector_request in (
        (
            "shopify_monthly_order_evidence_connector",
            "shopify.monthly_order_evidence", shopify_request,
        ),
        ("stripe_balance_connector", "stripe.balance_transactions", balance_request),
    ):
        try:
            imported[connector_id] = connectors.dispatch(runtime, connector_id, connector_request)
        except ConnectorError as exc:
            return {
                "pipeline": {
                    "pipeline_id": "dtc.shopify_stripe_month_close",
                    "executed_at": executed_at,
                    "stages": stages,
                    "required_review_gates": [
                        "shopify_mapping_approval", "processor_link_mapping_approval",
                        "stripe_mapping_approval", "tax_inclusive_policy_confirmed",
                        "return_authorization_and_receipt_scope_aligned",
                    ],
                },
                "ready": False,
                "blocked_at": stage,
                "retryable": True,
                "error": str(exc),
                "connector_batches": {
                    key: {
                        "batch_id": value["batch"]["batch_id"],
                        "quality": value["batch"]["quality"],
                    }
                    for key, value in imported.items()
                },
                "services": {},
                "external_actions_performed": False,
            }

    batches = {key: value["batch"] for key, value in imported.items()}
    canonical = json.dumps({
        "runtime_fingerprint": runtime.snapshot()["fingerprint"],
        "entity_id": entity_id,
        "connector_batch_ids": {
            key: value["batch_id"] for key, value in sorted(batches.items())
        },
        "processor_links": _canonical_records(processor_links),
        "currency_minor_units": request.get("currency_minor_units"),
        "include_test_orders": request.get("include_test_orders") is True,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    run_id = hashlib.sha256(canonical.encode()).hexdigest()[:24]
    base = {
        "pipeline": {
            "pipeline_id": "dtc.shopify_stripe_month_close",
            "run_id": run_id,
            "executed_at": executed_at,
            "stages": stages,
            "idempotency_basis": (
                "runtime fingerprint + monthly Shopify batch + bounded Stripe batch + canonical links"
            ),
            "required_review_gates": [
                "shopify_mapping_approval", "processor_link_mapping_approval",
                "stripe_mapping_approval", "tax_inclusive_policy_confirmed",
                "return_authorization_and_receipt_scope_aligned",
            ],
        },
        "connector_batches": {
            key: {
                "batch_id": batch["batch_id"], "source": batch["source"],
                "quality": batch["quality"],
            }
            for key, batch in sorted(batches.items())
        },
        "external_actions_performed": False,
        "network_access_performed": any(
            batch["source"].get("network_access_performed") for batch in batches.values()
        ),
    }
    failed_quality = [
        key for key, batch in batches.items() if not batch["quality"]["ready"]
    ]
    if failed_quality:
        return {
            **base, "ready": False, "blocked_at": "quality_gate", "retryable": False,
            "services": {},
            "blockers": [f"Connector quality failed: {key}" for key in sorted(failed_quality)],
        }

    shopify_batch = batches["shopify.monthly_order_evidence"]
    balance_batch = batches["stripe.balance_transactions"]
    shopify_source = shopify_batch["source"]
    stripe_window = balance_batch["source"].get("created_window") or {}
    try:
        start_unix = int(datetime.fromisoformat(
            str(shopify_source["interval_start"]).replace("Z", "+00:00")
        ).timestamp())
        end_unix = int(datetime.fromisoformat(
            str(shopify_source["interval_end"]).replace("Z", "+00:00")
        ).timestamp())
    except (KeyError, TypeError, ValueError) as exc:
        raise BoxPipelineError("Shopify monthly source omitted canonical interval bounds") from exc
    scope_blockers = []
    if stripe_window.get("complete_bounds_declared") is not True:
        scope_blockers.append("Stripe balance source did not declare both created bounds")
    if stripe_window.get("gte") != start_unix or stripe_window.get("lt") != end_unix:
        scope_blockers.append("Shopify and Stripe half-open month windows do not match")
    if scope_blockers:
        return {
            **base, "ready": False, "blocked_at": "canonical_month_scope_gate",
            "retryable": False, "services": {}, "blockers": scope_blockers,
            "lineage": {
                "run_id": run_id, "entity_id": entity_id,
                "period": shopify_source.get("canonical_month_period"),
                "canonical_month_scope": False,
                "connector_batch_ids": {
                    key: value["batch_id"] for key, value in sorted(batches.items())
                },
            },
        }

    shopify_data = shopify_batch["datasets"]
    balance_rows = balance_batch["datasets"]["payments.stripe_balance_transactions"]
    monthly_scope = services.dispatch(
        runtime, "shopify.build_monthly_commerce_scope", {
            "orders": shopify_data["commerce.shopify_orders"],
            "refunds": shopify_data["commerce.shopify_refunds"],
            "source_scope": shopify_source,
            "include_test_orders": request.get("include_test_orders") is True,
        }, entity_id=entity_id,
    )
    stripe_summary = services.dispatch(
        runtime, "stripe.summarize_balance_activity",
        {"balance_transactions": balance_rows}, entity_id=entity_id,
    )
    activity_reconciliation = services.dispatch(
        runtime, "dtc.reconcile_shopify_stripe_activity", {
            "shopify_transactions": shopify_data["commerce.shopify_transactions"],
            "stripe_balance_transactions": balance_rows,
            "processor_links": processor_links,
            "currency_minor_units": request.get("currency_minor_units"),
        }, entity_id=entity_id,
    )
    service_map = {
        "shopify_monthly_commerce_scope": monthly_scope,
        "stripe_balance_activity": stripe_summary,
        "shopify_stripe_activity_reconciliation": activity_reconciliation,
    }
    readiness = {key: bool(value["output"].get("ready")) for key, value in service_map.items()}
    ready = all(readiness.values())
    blocked_at = next((key for key, value in readiness.items() if not value), None)
    period = str(shopify_source["canonical_month_period"])
    return {
        **base,
        "ready": ready,
        "blocked_at": blocked_at,
        "retryable": False,
        "services": service_map,
        "founder_briefing": {
            "monthly_commerce_scope": monthly_scope["output"].get("monthly_commerce_scope", []),
            "shopify_stripe_activity": activity_reconciliation["output"]["founder_briefing"],
            "candidate_only": True,
            "tax_policy_review_required": True,
            "return_receipt_review_required": True,
            "margin_claim_prohibited": True,
        },
        "lineage": {
            "run_id": run_id,
            "entity_id": entity_id,
            "period": period,
            "canonical_month_scope": True,
            "interval_start": shopify_source["interval_start"],
            "interval_end": shopify_source["interval_end"],
            "connector_batch_ids": {
                key: value["batch_id"] for key, value in sorted(batches.items())
            },
            "processor_link_evidence_count": len(processor_links),
            "service_ids": [value["service"]["service_id"] for value in service_map.values()],
        },
        "resume_contract": (
            "No state was changed. Correct the earliest connector, scope or reconciliation blocker and "
            "rerun the full close-captured month request; identical evidence reproduces run_id."
        ),
    }


def run_shipbob_fulfillment_close_pipeline(
    runtime: BoxRuntime,
    request: dict[str, Any],
    *,
    connector_registry: ConnectorRegistry | None = None,
    service_registry: PackServiceRegistry | None = None,
) -> dict[str, Any]:
    """Import one entity's ShipBob evidence and build a non-posting fulfillment review."""
    if not isinstance(request, dict):
        raise BoxPipelineError("ShipBob fulfillment pipeline request must be an object")
    entity_id = request.get("entity_id")
    if not isinstance(entity_id, str) or not entity_id.strip():
        raise BoxPipelineError("ShipBob fulfillment pipeline requires entity_id")
    try:
        runtime.require_entity(entity_id)
        runtime.require_capability("connector.shipbob_fulfillment_evidence")
    except Exception as exc:
        raise BoxPipelineError(str(exc)) from exc
    connector_request = _entity_scoped_connector_request(
        request.get("shipbob_request"), "shipbob_request", entity_id,
    )
    connectors = connector_registry or build_box_connector_registry(runtime)
    services = service_registry or build_default_service_registry()
    executed_at = datetime.now(timezone.utc).isoformat()
    stages = [
        "shipbob_fulfillment_connector", "quality_gate", "entity_scope",
        "fulfillment_and_return_evidence_summary", "founder_fulfillment_briefing",
    ]
    review_gates = [
        "shipbob_entity_binding_review", "shipbob_order_mapping_review",
        "shipbob_fulfillment_cost_review", "return_disposition_review",
    ]
    try:
        imported = connectors.dispatch(
            runtime, "shipbob.fulfillment", connector_request,
        )
    except ConnectorError as exc:
        return {
            "pipeline": {
                "pipeline_id": "commerce.shipbob_fulfillment_close",
                "executed_at": executed_at,
                "stages": stages,
                "required_review_gates": review_gates,
            },
            "ready": False,
            "blocked_at": "shipbob_fulfillment_connector",
            "retryable": True,
            "error": str(exc),
            "connector_batches": {},
            "services": {},
            "external_actions_performed": False,
        }
    batch = imported["batch"]
    canonical = json.dumps({
        "runtime_fingerprint": runtime.snapshot()["fingerprint"],
        "entity_id": entity_id,
        "batch_id": batch["batch_id"],
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    run_id = hashlib.sha256(canonical.encode()).hexdigest()[:24]
    base = {
        "pipeline": {
            "pipeline_id": "commerce.shipbob_fulfillment_close",
            "run_id": run_id,
            "executed_at": executed_at,
            "stages": stages,
            "idempotency_basis": "runtime fingerprint + entity id + ShipBob connector batch id",
            "required_review_gates": review_gates,
        },
        "connector_batches": {
            "shipbob.fulfillment": {
                "batch_id": batch["batch_id"],
                "source": batch["source"],
                "quality": batch["quality"],
            },
        },
        "external_actions_performed": False,
        "network_access_performed": bool(batch["source"].get("network_access_performed")),
    }
    if not batch["quality"]["ready"]:
        return {
            **base,
            "ready": False,
            "blocked_at": "quality_gate",
            "retryable": False,
            "services": {},
            "blockers": [
                "ShipBob batch contains rejected rows or duplicate business keys; no summary was produced."
            ],
        }
    datasets = batch["datasets"]
    service_payload = {
        "orders": datasets.get("commerce.shipbob_orders", []),
        "shipments": datasets.get("commerce.shipbob_shipments", []),
        "returns": datasets.get("commerce.shipbob_returns", []),
        "return_items": datasets.get("commerce.shipbob_return_items", []),
    }
    accepted_entity_ids = sorted({
        str(row.get("entity_id") or "")
        for rows in service_payload.values() for row in rows
    })
    if accepted_entity_ids and accepted_entity_ids != [entity_id]:
        raise BoxPipelineError("ShipBob accepted evidence crossed the requested statutory entity")
    summarized = services.dispatch(
        runtime, "shipbob.summarize_fulfillment_evidence",
        service_payload, entity_id=entity_id,
    )
    output = summarized["output"]
    ready = bool(output.get("ready"))
    return {
        **base,
        "ready": ready,
        "blocked_at": None if ready else "fulfillment_and_return_evidence_summary",
        "retryable": False,
        "services": {"fulfillment_and_return_evidence_summary": summarized},
        "blockers": list(output.get("blockers") or []),
        "founder_briefing": {
            "counts": output["counts"],
            "orders_without_shipments": output["order_fulfillment"]["orders_without_shipments"],
            "fulfillment_invoice_by_currency": output["fulfillment_invoice_summary"],
            "unprocessed_return_item_count": len(output["unprocessed_return_items"]),
            "cross_window_return_reference_count": len(output["cross_window_return_references"]),
            "candidate_only": True,
            "revenue_claim_prohibited": True,
            "inventory_adjustment_claim_prohibited": True,
        },
        "lineage": {
            "run_id": run_id,
            "entity_id": entity_id,
            "connector_batch_id": batch["batch_id"],
            "accepted_record_count": batch["quality"]["record_count"],
            "service_id": summarized["service"]["service_id"],
        },
        "resume_contract": (
            "No ShipBob or accounting state was changed. Correct the earliest connector, quality or "
            "structural blocker and rerun the complete entity-scoped request."
        ),
    }


def run_paypal_transaction_close_pipeline(
    runtime: BoxRuntime,
    request: dict[str, Any],
    *,
    connector_registry: ConnectorRegistry | None = None,
    service_registry: PackServiceRegistry | None = None,
) -> dict[str, Any]:
    """Import one entity's PayPal balance activity and build a non-posting review."""
    if not isinstance(request, dict):
        raise BoxPipelineError("PayPal transaction pipeline request must be an object")
    entity_id = request.get("entity_id")
    if not isinstance(entity_id, str) or not entity_id.strip():
        raise BoxPipelineError("PayPal transaction pipeline requires entity_id")
    try:
        runtime.require_entity(entity_id)
        runtime.require_capability("connector.paypal_transaction_activity")
    except Exception as exc:
        raise BoxPipelineError(str(exc)) from exc
    connector_request = _entity_scoped_connector_request(
        request.get("paypal_request"), "paypal_request", entity_id,
    )
    connectors = connector_registry or build_box_connector_registry(runtime)
    services = service_registry or build_default_service_registry()
    executed_at = datetime.now(timezone.utc).isoformat()
    stages = [
        "paypal_transaction_connector", "quality_gate", "entity_scope",
        "transaction_activity_summary", "founder_paypal_briefing",
    ]
    review_gates = [
        "paypal_entity_account_binding_review", "paypal_transaction_event_mapping_review",
        "paypal_fee_treatment_review", "paypal_refund_reversal_review",
    ]
    try:
        imported = connectors.dispatch(
            runtime, "paypal.transaction_activity", connector_request,
        )
    except ConnectorError as exc:
        return {
            "pipeline": {
                "pipeline_id": "paypal.transaction_close",
                "executed_at": executed_at,
                "stages": stages,
                "required_review_gates": review_gates,
            },
            "ready": False,
            "blocked_at": "paypal_transaction_connector",
            "retryable": True,
            "error": str(exc),
            "connector_batches": {},
            "services": {},
            "external_actions_performed": False,
        }
    batch = imported["batch"]
    canonical = json.dumps({
        "runtime_fingerprint": runtime.snapshot()["fingerprint"],
        "entity_id": entity_id,
        "batch_id": batch["batch_id"],
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    run_id = hashlib.sha256(canonical.encode()).hexdigest()[:24]
    base = {
        "pipeline": {
            "pipeline_id": "paypal.transaction_close",
            "run_id": run_id,
            "executed_at": executed_at,
            "stages": stages,
            "idempotency_basis": "runtime fingerprint + entity id + PayPal connector batch id",
            "required_review_gates": review_gates,
        },
        "connector_batches": {
            "paypal.transaction_activity": {
                "batch_id": batch["batch_id"],
                "source": batch["source"],
                "quality": batch["quality"],
            },
        },
        "external_actions_performed": False,
        "network_access_performed": bool(batch["source"].get("network_access_performed")),
    }
    if not batch["quality"]["ready"]:
        return {
            **base,
            "ready": False,
            "blocked_at": "quality_gate",
            "retryable": False,
            "services": {},
            "blockers": [
                "PayPal batch contains rejected rows or duplicate business keys; no summary was produced."
            ],
        }
    rows = batch["datasets"].get("payments.paypal_balance_activity", [])
    accepted_entity_ids = sorted({str(row.get("entity_id") or "") for row in rows})
    if accepted_entity_ids and accepted_entity_ids != [entity_id]:
        raise BoxPipelineError("PayPal accepted evidence crossed the requested statutory entity")
    summarized = services.dispatch(
        runtime, "paypal.summarize_transaction_activity",
        {"transactions": rows}, entity_id=entity_id,
    )
    output = summarized["output"]
    ready = bool(output.get("ready"))
    return {
        **base,
        "ready": ready,
        "blocked_at": None if ready else "transaction_activity_summary",
        "retryable": False,
        "services": {"transaction_activity_summary": summarized},
        "blockers": list(output.get("blockers") or []),
        "founder_briefing": {
            "transaction_count": output["transaction_count"],
            "currency_summary": output["currency_summary"],
            "refund_candidate_count": output["refund_candidate_count"],
            "reversal_candidate_count": output["reversal_candidate_count"],
            "reference_review_required_count": output["reference_review_required_count"],
            "candidate_only": True,
            "revenue_claim_prohibited": True,
            "bank_receipt_claim_prohibited": True,
        },
        "lineage": {
            "run_id": run_id,
            "entity_id": entity_id,
            "connector_batch_id": batch["batch_id"],
            "accepted_record_count": batch["quality"]["record_count"],
            "service_id": summarized["service"]["service_id"],
        },
        "resume_contract": (
            "No PayPal, bank or accounting state was changed. Correct the earliest connector, quality or "
            "arithmetic blocker and rerun the complete entity-scoped request."
        ),
    }


def run_woocommerce_order_refund_close_pipeline(
    runtime: BoxRuntime,
    request: dict[str, Any],
    *,
    connector_registry: ConnectorRegistry | None = None,
    service_registry: PackServiceRegistry | None = None,
) -> dict[str, Any]:
    """Import one entity's WooCommerce order changes and refund events for review."""
    if not isinstance(request, dict):
        raise BoxPipelineError("WooCommerce order/refund pipeline request must be an object")
    entity_id = request.get("entity_id")
    if not isinstance(entity_id, str) or not entity_id.strip():
        raise BoxPipelineError("WooCommerce order/refund pipeline requires entity_id")
    try:
        runtime.require_entity(entity_id)
        runtime.require_capability("connector.woocommerce_order_refund_activity")
    except Exception as exc:
        raise BoxPipelineError(str(exc)) from exc
    connector_request = _entity_scoped_connector_request(
        request.get("woocommerce_request"), "woocommerce_request", entity_id,
    )
    connectors = connector_registry or build_box_connector_registry(runtime)
    services = service_registry or build_default_service_registry()
    executed_at = datetime.now(timezone.utc).isoformat()
    stages = [
        "woocommerce_order_refund_connector", "quality_gate", "entity_scope",
        "order_refund_activity_summary", "founder_woocommerce_briefing",
    ]
    review_gates = [
        "woocommerce_site_entity_binding_review",
        "woocommerce_order_status_mapping_review",
        "woocommerce_refund_completeness_review",
        "woocommerce_tax_and_revenue_policy_review",
    ]
    try:
        imported = connectors.dispatch(
            runtime, "woocommerce.order_refund_activity", connector_request,
        )
    except ConnectorError as exc:
        return {
            "pipeline": {
                "pipeline_id": "woocommerce.order_refund_close",
                "executed_at": executed_at,
                "stages": stages,
                "required_review_gates": review_gates,
            },
            "ready": False,
            "blocked_at": "woocommerce_order_refund_connector",
            "retryable": True,
            "error": str(exc),
            "connector_batches": {},
            "services": {},
            "external_actions_performed": False,
        }
    batch = imported["batch"]
    canonical = json.dumps({
        "runtime_fingerprint": runtime.snapshot()["fingerprint"],
        "entity_id": entity_id, "batch_id": batch["batch_id"],
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    run_id = hashlib.sha256(canonical.encode()).hexdigest()[:24]
    base = {
        "pipeline": {
            "pipeline_id": "woocommerce.order_refund_close",
            "run_id": run_id,
            "executed_at": executed_at,
            "stages": stages,
            "idempotency_basis": "runtime fingerprint + entity id + WooCommerce connector batch id",
            "required_review_gates": review_gates,
        },
        "connector_batches": {
            "woocommerce.order_refund_activity": {
                "batch_id": batch["batch_id"],
                "source": batch["source"],
                "quality": batch["quality"],
            },
        },
        "external_actions_performed": False,
        "network_access_performed": bool(batch["source"].get("network_access_performed")),
    }

    if not batch["quality"]["ready"]:
        return {
            **base,
            "ready": False,
            "blocked_at": "quality_gate",
            "retryable": False,
            "services": {},
            "blockers": [
                "WooCommerce batch contains rejected rows or duplicate business keys; no summary was produced."
            ],
        }
    orders = batch["datasets"].get("commerce.woocommerce_orders", [])
    refunds = batch["datasets"].get("commerce.woocommerce_refunds", [])
    accepted_entity_ids = sorted({
        str(row.get("entity_id") or "") for row in [*orders, *refunds]
    })
    if accepted_entity_ids and accepted_entity_ids != [entity_id]:
        raise BoxPipelineError("WooCommerce accepted evidence crossed the requested statutory entity")
    summarized = services.dispatch(
        runtime, "woocommerce.summarize_order_refund_activity",
        {"orders": orders, "refunds": refunds}, entity_id=entity_id,
    )
    output = summarized["output"]
    ready = bool(output.get("ready"))
    return {
        **base,
        "ready": ready,
        "blocked_at": None if ready else "order_refund_activity_summary",
        "retryable": False,
        "services": {"order_refund_activity_summary": summarized},
        "blockers": list(output.get("blockers") or []),
        "founder_briefing": {
            "order_count": output["order_count"],
            "refund_event_count": output["refund_event_count"],
            "status_counts": output["status_counts"],
            "currency_summary": output["currency_summary"],
            "destination_review_required_count": output["destination_review_required_count"],
            "unpaid_or_unconfirmed_order_count": output["unpaid_or_unconfirmed_order_count"],
            "candidate_only": True,
            "revenue_claim_prohibited": True,
            "tax_liability_claim_prohibited": True,
            "payment_settlement_claim_prohibited": True,
        },
        "lineage": {
            "run_id": run_id,
            "entity_id": entity_id,
            "connector_batch_id": batch["batch_id"],
            "accepted_record_count": batch["quality"]["record_count"],
            "service_id": summarized["service"]["service_id"],
        },
        "resume_contract": (
            "No WooCommerce, inventory, bank or accounting state was changed. Correct the earliest "
            "connector, quality or structural blocker and rerun the complete entity-scoped request."
        ),
    }


def run_amazon_seller_transaction_close_pipeline(
    runtime: BoxRuntime,
    request: dict[str, Any],
    *,
    connector_registry: ConnectorRegistry | None = None,
    service_registry: PackServiceRegistry | None = None,
) -> dict[str, Any]:
    """Import one entity's Amazon Seller financial activity for non-posting review."""
    if not isinstance(request, dict):
        raise BoxPipelineError("Amazon Seller transaction pipeline request must be an object")
    entity_id = request.get("entity_id")
    if not isinstance(entity_id, str) or not entity_id.strip():
        raise BoxPipelineError("Amazon Seller transaction pipeline requires entity_id")
    try:
        runtime.require_entity(entity_id)
        runtime.require_capability("connector.amazon_seller_transaction_activity")
    except Exception as exc:
        raise BoxPipelineError(str(exc)) from exc
    connector_request = _entity_scoped_connector_request(
        request.get("amazon_seller_request"), "amazon_seller_request", entity_id,
    )
    connectors = connector_registry or build_box_connector_registry(runtime)
    services = service_registry or build_default_service_registry()
    executed_at = datetime.now(timezone.utc).isoformat()
    stages = [
        "amazon_seller_transaction_connector", "quality_gate", "entity_scope",
        "transaction_activity_summary", "founder_amazon_seller_briefing",
    ]
    review_gates = [
        "amazon_seller_entity_account_binding_review",
        "amazon_seller_marketplace_scope_review",
        "amazon_seller_transaction_mapping_review",
        "amazon_seller_fee_tax_policy_review",
        "amazon_seller_settlement_completeness_review",
    ]
    try:
        imported = connectors.dispatch(
            runtime, "amazon_seller.transaction_activity", connector_request,
        )
    except ConnectorError as exc:
        return {
            "pipeline": {
                "pipeline_id": "amazon_seller.transaction_close",
                "executed_at": executed_at,
                "stages": stages,
                "required_review_gates": review_gates,
            },
            "ready": False,
            "blocked_at": "amazon_seller_transaction_connector",
            "retryable": True,
            "error": str(exc),
            "connector_batches": {},
            "services": {},
            "external_actions_performed": False,
        }
    batch = imported["batch"]
    canonical = json.dumps({
        "runtime_fingerprint": runtime.snapshot()["fingerprint"],
        "entity_id": entity_id,
        "batch_id": batch["batch_id"],
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    run_id = hashlib.sha256(canonical.encode()).hexdigest()[:24]
    base = {
        "pipeline": {
            "pipeline_id": "amazon_seller.transaction_close",
            "run_id": run_id,
            "executed_at": executed_at,
            "stages": stages,
            "idempotency_basis": "runtime fingerprint + entity id + Amazon Seller connector batch id",
            "required_review_gates": review_gates,
        },
        "connector_batches": {
            "amazon_seller.transaction_activity": {
                "batch_id": batch["batch_id"],
                "source": batch["source"],
                "quality": batch["quality"],
            },
        },
        "external_actions_performed": False,
        "network_access_performed": bool(batch["source"].get("network_access_performed")),
    }
    if not batch["quality"]["ready"]:
        return {
            **base,
            "ready": False,
            "blocked_at": "quality_gate",
            "retryable": False,
            "services": {},
            "blockers": [
                "Amazon Seller batch contains rejected rows or duplicate business keys; no summary was produced."
            ],
        }
    rows = batch["datasets"].get("commerce.amazon_seller_transactions", [])
    accepted_entity_ids = sorted({str(row.get("entity_id") or "") for row in rows})
    if accepted_entity_ids and accepted_entity_ids != [entity_id]:
        raise BoxPipelineError("Amazon Seller accepted evidence crossed the requested statutory entity")
    summarized = services.dispatch(
        runtime, "amazon_seller.summarize_transaction_activity",
        {"transactions": rows}, entity_id=entity_id,
    )
    output = summarized["output"]
    ready = bool(output.get("ready"))
    return {
        **base,
        "ready": ready,
        "blocked_at": None if ready else "transaction_activity_summary",
        "retryable": False,
        "services": {"transaction_activity_summary": summarized},
        "blockers": list(output.get("blockers") or []),
        "founder_briefing": {
            "transaction_count": output["transaction_count"],
            "status_counts": output["status_counts"],
            "transaction_type_counts": output["transaction_type_counts"],
            "currency_summary": output["currency_summary"],
            "refund_candidate_count": len(output["refund_candidate_keys"]),
            "fee_candidate_count": len(output["fee_candidate_keys"]),
            "deferred_transaction_count": output["deferred_transaction_count"],
            "settlement_reference_missing_count": output["settlement_reference_missing_count"],
            "candidate_only": True,
            "revenue_claim_prohibited": True,
            "tax_liability_claim_prohibited": True,
            "settlement_or_bank_reconciliation_claim_prohibited": True,
        },
        "lineage": {
            "run_id": run_id,
            "entity_id": entity_id,
            "connector_batch_id": batch["batch_id"],
            "accepted_record_count": batch["quality"]["record_count"],
            "service_id": summarized["service"]["service_id"],
        },
        "resume_contract": (
            "No Amazon Seller, bank, inventory, tax or accounting state was changed. Correct the "
            "earliest connector, quality or structural blocker and rerun the complete entity-scoped request."
        ),
    }


def run_amazon_seller_marketplace_close_pipeline(
    runtime: BoxRuntime,
    request: dict[str, Any],
    *,
    connector_registry: ConnectorRegistry | None = None,
    service_registry: PackServiceRegistry | None = None,
) -> dict[str, Any]:
    """Cross-check one Seller's Orders, current FBA Inventory and Finances evidence."""
    if not isinstance(request, dict):
        raise BoxPipelineError("Amazon Seller marketplace pipeline request must be an object")
    entity_id = request.get("entity_id")
    if not isinstance(entity_id, str) or not entity_id.strip():
        raise BoxPipelineError("Amazon Seller marketplace pipeline requires entity_id")
    try:
        runtime.require_entity(entity_id)
        runtime.require_capability("connector.amazon_seller_marketplace_evidence")
    except Exception as exc:
        raise BoxPipelineError(str(exc)) from exc
    connector_request = _entity_scoped_connector_request(
        request.get("amazon_seller_marketplace_request"),
        "amazon_seller_marketplace_request", entity_id,
    )
    connectors = connector_registry or build_box_connector_registry(runtime)
    services = service_registry or build_default_service_registry()
    executed_at = datetime.now(timezone.utc).isoformat()
    stages = [
        "amazon_seller_marketplace_connector", "quality_gate", "entity_scope",
        "orders_inventory_finances_reconciliation", "founder_marketplace_briefing",
    ]
    review_gates = [
        "amazon_seller_entity_account_binding_review",
        "amazon_seller_marketplace_scope_review",
        "amazon_seller_orders_scope_review",
        "amazon_seller_transaction_mapping_review",
        "amazon_seller_order_finance_completeness_review",
        "amazon_seller_inventory_scope_review",
        "amazon_seller_inventory_reconciliation_review",
        "amazon_seller_fee_tax_policy_review",
        "amazon_seller_settlement_completeness_review",
    ]
    try:
        imported = connectors.dispatch(
            runtime, "amazon_seller.marketplace_evidence", connector_request,
        )
    except ConnectorError as exc:
        return {
            "pipeline": {
                "pipeline_id": "amazon_seller.marketplace_close",
                "executed_at": executed_at,
                "stages": stages,
                "required_review_gates": review_gates,
            },
            "ready": False,
            "blocked_at": "amazon_seller_marketplace_connector",
            "retryable": True,
            "error": str(exc),
            "connector_batches": {},
            "services": {},
            "external_actions_performed": False,
        }
    batch = imported["batch"]
    canonical = json.dumps({
        "runtime_fingerprint": runtime.snapshot()["fingerprint"],
        "entity_id": entity_id,
        "batch_id": batch["batch_id"],
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    run_id = hashlib.sha256(canonical.encode()).hexdigest()[:24]
    base = {
        "pipeline": {
            "pipeline_id": "amazon_seller.marketplace_close",
            "run_id": run_id,
            "executed_at": executed_at,
            "stages": stages,
            "idempotency_basis": (
                "runtime fingerprint + entity id + shared Amazon marketplace evidence batch id"
            ),
            "required_review_gates": review_gates,
        },
        "connector_batches": {
            "amazon_seller.marketplace_evidence": {
                "batch_id": batch["batch_id"],
                "source": batch["source"],
                "quality": batch["quality"],
            },
        },
        "external_actions_performed": False,
        "network_access_performed": bool(batch["source"].get("network_access_performed")),
    }
    if not batch["quality"]["ready"]:
        return {
            **base,
            "ready": False,
            "blocked_at": "quality_gate",
            "retryable": False,
            "services": {},
            "blockers": [
                "Amazon Seller marketplace batch contains rejected rows or duplicate business keys; "
                "no cross-source reconciliation was produced."
            ],
        }
    orders = batch["datasets"].get("commerce.amazon_seller_orders", [])
    inventory = batch["datasets"].get("commerce.amazon_seller_inventory", [])
    transactions = batch["datasets"].get("commerce.amazon_seller_transactions", [])
    accepted_entity_ids = sorted({
        str(row.get("entity_id") or "") for row in [*orders, *inventory, *transactions]
    })
    if accepted_entity_ids and accepted_entity_ids != [entity_id]:
        raise BoxPipelineError("Amazon Seller marketplace evidence crossed the requested statutory entity")
    reconciled = services.dispatch(
        runtime, "amazon_seller.reconcile_marketplace_evidence",
        {
            "orders": orders,
            "inventory": inventory,
            "transactions": transactions,
            "source_scope": {
                key: batch["source"].get(key)
                for key in (
                    "canonical_month_period", "canonical_month_scope", "marketplace_id",
                    "interval_start", "interval_end", "orders_time_basis",
                    "inventory_observed_at", "inventory_observation_type",
                )
            },
        },
        entity_id=entity_id,
    )
    output = reconciled["output"]
    ready = bool(output.get("ready"))
    return {
        **base,
        "ready": ready,
        "blocked_at": None if ready else "orders_inventory_finances_reconciliation",
        "retryable": False,
        "services": {"marketplace_evidence_reconciliation": reconciled},
        "blockers": list(output.get("blockers") or []),
        "founder_briefing": {
            "order_count": output["order_count"],
            "inventory_sku_count": output["inventory_sku_count"],
            "transaction_count": output["transaction_count"],
            "period": output["period"],
            "canonical_month_scope": output["canonical_month_scope"],
            "marketplace_id": output["marketplace_id"],
            "eligible_three_way_order_count": output["eligible_three_way_order_count"],
            "matched_three_way_order_count": output["matched_three_way_order_count"],
            "three_way_match_rate": output["three_way_match_rate"],
            "order_status_counts": output["order_status_counts"],
            "fulfilled_by_counts": output["fulfilled_by_counts"],
            "inventory_quantity_summary": output["inventory_quantity_summary"],
            "transaction_currency_summary": output["transaction_currency_summary"],
            "finance_without_order_count": len(output["finance_without_order_keys"]),
            "shipped_order_without_finance_count": len(
                output["shipped_order_without_finance_keys"]
            ),
            "fba_order_sku_without_inventory_count": len(
                output["fba_order_sku_without_inventory_keys"]
            ),
            "inventory_sku_without_window_order_count": len(
                output["inventory_sku_without_window_order_keys"]
            ),
            "inventory_quantity_field_missing_count": len(
                output["inventory_quantity_field_missing_keys"]
            ),
            "candidate_only": True,
            "current_inventory_not_historical_period_end": True,
            "order_or_financial_completeness_claim_prohibited": True,
            "inventory_valuation_or_cogs_claim_prohibited": True,
            "revenue_tax_settlement_claim_prohibited": True,
        },
        "lineage": {
            "run_id": run_id,
            "entity_id": entity_id,
            "period": output["period"],
            "marketplace_id": output["marketplace_id"],
            "connector_batch_id": batch["batch_id"],
            "accepted_record_count": batch["quality"]["record_count"],
            "dataset_counts": batch["quality"]["dataset_counts"],
            "service_id": reconciled["service"]["service_id"],
        },
        "resume_contract": (
            "No Amazon Seller, inventory, bank, tax or accounting state was changed. Correct the "
            "earliest connector, quality or structural blocker and rerun the complete entity-scoped request."
        ),
    }


def _dispatch_box_pipeline_request_unassembled(
    runtime: BoxRuntime,
    request: dict[str, Any],
    *,
    connector_registry: ConnectorRegistry | None = None,
    service_registry: PackServiceRegistry | None = None,
) -> dict[str, Any]:
    """Validate and dispatch a named Box pipeline request."""
    if not isinstance(request, dict):
        raise BoxPipelineError("pipeline request must be an object")
    pipeline_id = request.get("pipeline_id")
    payload = request.get("payload")
    if not isinstance(pipeline_id, str) or not pipeline_id:
        raise BoxPipelineError("pipeline_id is required")
    if not isinstance(payload, dict):
        raise BoxPipelineError("pipeline payload must be an object")
    if pipeline_id == "commerce.import_analyze":
        connector_id = payload.get("connector_id")
        connector_request = payload.get("connector_request")
        if not isinstance(connector_id, str) or not connector_id:
            raise BoxPipelineError("commerce.import_analyze requires connector_id")
        if not isinstance(connector_request, dict):
            raise BoxPipelineError("commerce.import_analyze requires connector_request object")
        return run_commerce_import_analysis_pipeline(
            runtime,
            connector_id,
            connector_request,
            connector_registry=connector_registry,
            service_registry=service_registry,
        )
    if pipeline_id == "finance.bank_statement_close":
        return run_bank_statement_close_pipeline(
            runtime,
            payload,
            connector_registry=connector_registry,
            service_registry=service_registry,
        )
    if pipeline_id == "finance.expense_evidence_review":
        return run_expense_evidence_review_pipeline(
            runtime, payload, connector_registry=connector_registry,
            service_registry=service_registry,
        )
    if pipeline_id == "finance.trial_balance_review":
        return run_trial_balance_review_pipeline(
            runtime,
            payload,
            connector_registry=connector_registry,
            service_registry=service_registry,
        )
    if pipeline_id == "finance.accounting_close_review":
        return run_accounting_close_review_pipeline(
            runtime,
            payload,
            connector_registry=connector_registry,
            service_registry=service_registry,
        )
    if pipeline_id == "finance.first_close_discovery":
        return run_first_close_discovery_pipeline(
            runtime,
            payload,
            connector_registry=connector_registry,
            service_registry=service_registry,
        )
    if pipeline_id == "finance.month_close_control":
        return run_month_close_control_pipeline(
            runtime,
            payload,
            connector_registry=connector_registry,
            service_registry=service_registry,
        )
    if pipeline_id == "finance.multi_entity_month_close_portfolio":
        return run_multi_entity_month_close_portfolio_pipeline(
            runtime,
            payload,
            service_registry=service_registry,
        )
    if pipeline_id == "game.channel_settlement_close":
        return run_game_channel_settlement_close_pipeline(
            runtime,
            payload,
            connector_registry=connector_registry,
            service_registry=service_registry,
        )
    if pipeline_id == "commerce.channel_close":
        return run_commerce_channel_close_pipeline(
            runtime,
            payload,
            connector_registry=connector_registry,
            service_registry=service_registry,
        )
    if pipeline_id == "marketplace.channel_close":
        return run_marketplace_channel_close_pipeline(
            runtime,
            payload,
            connector_registry=connector_registry,
            service_registry=service_registry,
        )
    if pipeline_id == "stripe.daily_close":
        return run_stripe_daily_close_pipeline(
            runtime,
            payload,
            connector_registry=connector_registry,
            service_registry=service_registry,
        )
    if pipeline_id == "dtc.shopify_stripe_daily_close":
        return run_shopify_stripe_daily_close_pipeline(
            runtime,
            payload,
            connector_registry=connector_registry,
            service_registry=service_registry,
        )
    if pipeline_id == "dtc.shopify_stripe_month_close":
        return run_shopify_stripe_month_close_pipeline(
            runtime,
            payload,
            connector_registry=connector_registry,
            service_registry=service_registry,
        )
    if pipeline_id == "commerce.shipbob_fulfillment_close":
        return run_shipbob_fulfillment_close_pipeline(
            runtime,
            payload,
            connector_registry=connector_registry,
            service_registry=service_registry,
        )
    if pipeline_id == "paypal.transaction_close":
        return run_paypal_transaction_close_pipeline(
            runtime,
            payload,
            connector_registry=connector_registry,
            service_registry=service_registry,
        )
    if pipeline_id == "woocommerce.order_refund_close":
        return run_woocommerce_order_refund_close_pipeline(
            runtime,
            payload,
            connector_registry=connector_registry,
            service_registry=service_registry,
        )
    if pipeline_id == "amazon_seller.transaction_close":
        return run_amazon_seller_transaction_close_pipeline(
            runtime,
            payload,
            connector_registry=connector_registry,
            service_registry=service_registry,
        )
    if pipeline_id == "amazon_seller.marketplace_close":
        return run_amazon_seller_marketplace_close_pipeline(
            runtime,
            payload,
            connector_registry=connector_registry,
            service_registry=service_registry,
        )
    raise BoxPipelineError(f"Unknown pipeline: {pipeline_id}")


def dispatch_box_pipeline_request(
    runtime: BoxRuntime,
    request: dict[str, Any],
    *,
    connector_registry: ConnectorRegistry | None = None,
    service_registry: PackServiceRegistry | None = None,
) -> dict[str, Any]:
    """Dispatch a Pipeline and assemble source-bound CFO operands before leaving trust scope."""
    result = _dispatch_box_pipeline_request_unassembled(
        runtime,
        request,
        connector_registry=connector_registry,
        service_registry=service_registry,
    )
    pipeline_id = request.get("pipeline_id") if isinstance(request, dict) else None
    if isinstance(pipeline_id, str):
        attach_cfo_metric_assembly(runtime, "pipeline", pipeline_id, result)
    return result
