from __future__ import annotations

import json
import os
import tempfile
import hashlib
import ipaddress
import re
import signal
import threading
import zipfile
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .reconcile import dashboard_payload, discover_workbook, parse_files, parse_workbook_configured
from .finance_ops import build_finance_ops, create_bank_reconciliation_review
from .procurement import (
    apply_acceptance_decision, apply_delivery_acceptance_decision,
    create_procurement_request, create_purchase_order_from_request, decide_procurement_request,
    parse_purchase_workbook, procurement_budget_snapshot, procurement_payload,
    procurement_workflow_payload, record_purchase_delivery,
)
from .banking import banking_payload, parse_bank_workbook, suggest_matches
from .company_profile import load_profile, profile_gaps, save_profile
from .invoices import (
    invoice_payload, match_invoices_to_purchases, parse_invoice_workbook,
    roll_invoice_totals_to_purchases,
)
from .payroll import parse_payroll_workbook, payroll_payload
from .ledger_store import DATASETS, LedgerStore
from .planning import build_planning_analysis, parse_plan_workbook, planning_payload
from .accountant_pack import build_accountant_pack
from .close_control import assess_close
from .general_ledger import parse_opening_balance_workbook, opening_balance_payload
from .master_data import parse_master_workbook, parse_profile_workbook, master_quality
from .game_kpis import parse_kpi_workbook, enrich_kpis, kpi_quality
from .onboarding import build_onboarding
from .first_close_readiness import build_first_close_readiness, make_not_applicable_declaration
from .business_partner import build_bp_analysis
from .finance_qa import answer_finance_question, SUGGESTED_QUESTIONS
from .tax_export import build_tax_workbook
from .demo_scenarios import build_demo_payload, build_group_demo_payload, load_demo_scenarios
from .agent_runtime import AgentRuntimeStore, build_goal_snapshot
from .agent_workspace import (
    build_confirmation_queue, build_deliverable_register, latest_period,
    plan_safe_document_automations,
)
from .finance_inbox import FinanceInboxStore
from .business_flows import (
    build_flow_overview, build_payables_register, build_payroll_payables, build_receivables_register, create_cash_allocation,
    create_collection_action, create_expense_claim, create_payment_request, decide_expense_claim, decide_payment_request,
)
from .accounting_engine import (
    create_accrual, create_asset_card, post_reviewed_vouchers, review_accounting_item,
    roll_forward_opening_balances,
)
from .game_accounting import create_revenue_policy, review_revenue_policy
from .revenue_close import prepare_settlement_candidates, review_settlement_candidates, revenue_close_payload
from .ledger_adapters import create_adapter_review, get_ledger_adapter
from .tax_workflow import build_tax_delivery, record_tax_submission, review_tax_form
from .tax_filing_assist import build_filing_assist, build_filing_assist_package, form_fingerprint_from_workspace
from .shadow_close import compare_shadow_close, parse_shadow_close_workbook, review_shadow_close
from .workbook_templates import build_demo_workbook, build_onboarding_template, build_shadow_close_template
from .box_api import load_default_box_runtime
from .box_runtime import BoxRuntimeError
from .box_service_api import BoxServiceRequestError, build_box_bootstrap, dispatch_box_service_request
from .box_pipeline import BoxPipelineError, dispatch_box_pipeline_request
from .box_compiler import build_pipeline_runtime_catalog, preflight_pipeline_request
from .box_builder import (
    build_box_candidate_bundle, list_box_builder_options, preview_box_candidate,
)
from .box_config import load_pack_catalog
from .connector_onboarding import build_connector_onboarding
from .tax_workspace import build_tax_workspace
from .activation_orchestrator import build_activation_workspace
from .activation_workspace import (
    ActivationWorkspaceError,
    build_initialized_activation_status,
)
from .pilot_readiness import build_pilot_readiness_workspace
from .pilot_data_handoff import build_pilot_data_handoff_workspace
from .pilot_shadow_run import build_pilot_shadow_run_workspace
from .pilot_shadow_observation import build_pilot_shadow_observation_workspace
from .pilot_shadow_series import build_pilot_shadow_series_workspace
from .pilot_shadow_period_index import (
    ACTIVATION_WORKSPACE_ROOT_ENV,
    PilotShadowPeriodIndexError,
    build_pilot_shadow_period_workspace_index,
)
from .production_readiness import build_production_readiness_workspace
from .connector_shadow_registry import build_connector_shadow_registry_workspace
from .default_services import build_default_service_registry
from .cfo_metric_evaluator import CfoMetricEvaluationError
from .pack_services import PackServiceError
from .resource_paths import find_resource_root
from .pipeline_run_store import PipelineRunStore, PipelineRunStoreError
from .pipeline_scheduler import (
    PipelineScheduleError, inspect_pipeline_schedule, run_due_pipeline_schedule,
)
from .pipeline_observability import build_pipeline_observability, render_pipeline_prometheus
from .api_auth import ApiAuthError, load_api_auth_policy
from .runtime_storage import initialize_runtime_data, inspect_runtime_data
from .trial_workspace import (
    TRIAL_WORKSPACE_ROOT_ENV,
    TrialWorkspaceError,
    build_trial_onboarding_plan,
)
from .connector_sync import ConnectorSyncError, ConnectorSyncStore
from .airwallex_webhooks import (
    AirwallexWebhookError, AirwallexWebhookStore, ENTITY_BINDINGS_ENV,
    MAX_BODY_BYTES as AIRWALLEX_WEBHOOK_MAX_BODY_BYTES, WEBHOOK_SECRET_ENV,
)
from .vendor_controls import (
    approved_vendor_bank_accounts, create_vendor_bank_change, decide_vendor_bank_change,
    public_vendor_bank_record,
)


ROOT = find_resource_root()
PUBLIC = ROOT / "public"
SOURCE_PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RUNTIME_DATA = (
    SOURCE_PROJECT_ROOT / "data"
    if ROOT == SOURCE_PROJECT_ROOT
    else Path.cwd() / ".opc-finance-data"
)
DATA = Path(os.environ.get("OPC_FINANCE_DATA_DIR") or DEFAULT_RUNTIME_DATA)
RUNTIME_DATA_INSPECTION = inspect_runtime_data(DATA)
if RUNTIME_DATA_INSPECTION["state"] == "absent" or (
    RUNTIME_DATA_INSPECTION["state"] == "uninitialized"
    and not RUNTIME_DATA_INSPECTION["adoption_required"]
):
    initialize_runtime_data(DATA, actor="workbench-bootstrap")
elif RUNTIME_DATA_INSPECTION["state"] != "ready" and (
    os.environ.get("OPC_FINANCE_DATA_DIR") or ROOT != SOURCE_PROJECT_ROOT
):
    raise RuntimeError(
        "runtime data layout is not ready; stop the service and run "
        "opc-finance-box runtime-data-upgrade-preflight/runtime-data-init"
    )
DEMO_SCENARIOS = DATA / "demo_scenarios.json"
if not DEMO_SCENARIOS.exists() and (ROOT / "data" / "demo_scenarios.json").exists():
    DEMO_SCENARIOS = ROOT / "data" / "demo_scenarios.json"
TEMPLATES = DATA / "import_templates.json"
COMPANY_PROFILE = DATA / "company_profile.json"
LEDGER = LedgerStore(DATA / "ledger")
AGENT_RUNTIME = AgentRuntimeStore(DATA / "agent_runtime")
FINANCE_INBOX = FinanceInboxStore(DATA / "finance_inbox")
BOX_RUNTIME = load_default_box_runtime(ROOT)
BOX_SERVICES = build_default_service_registry()
PIPELINE_RUNS = PipelineRunStore(DATA / "pipeline_runs")
CONNECTOR_SYNCS = ConnectorSyncStore(DATA / "connector_sync")
AIRWALLEX_WEBHOOKS = AirwallexWebhookStore(
    DATA / "connector_sync" / "airwallex_webhooks"
)
PIPELINE_SCHEDULE_FILE = os.environ.get("OPC_FINANCE_PIPELINE_SCHEDULE_FILE") or None
OUTPUTS = SOURCE_PROJECT_ROOT / "outputs" if ROOT == SOURCE_PROJECT_ROOT else Path.cwd() / "outputs"
ONBOARDING_TEMPLATE = OUTPUTS / "templates" / "智能财务工作台-首次上线模板.xlsx"
SHADOW_CLOSE_TEMPLATE = OUTPUTS / "templates" / "智能财务工作台-Shadow-Close基准模板.xlsx"
TAX_OUTPUTS = OUTPUTS / "tax-returns"
DEMO_OUTPUTS = OUTPUTS / "demo-data"
IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,119}$")


def _idempotency_key(request: dict, *, required: bool = False) -> str:
    key = str(request.get("idempotency_key") or "").strip()
    if not key:
        if required:
            raise ValueError("认证模式下的资金动作必须提供 idempotency_key")
        return ""
    if not IDEMPOTENCY_KEY_PATTERN.fullmatch(key):
        raise ValueError("idempotency_key 必须为 8-120 位字母、数字或 ._:-")
    return key


def _idempotent_replay(dataset: str, key: str, entity_id: str = "") -> dict | None:
    if not key:
        return None
    return next((
        row for row in LEDGER.load_dataset(dataset)
        if row.get("idempotency_key") == key
        and (not entity_id or str(row.get("entity_id") or "") == entity_id)
    ), None)


def _validate_server_binding(
    host: str, api_token: str | None, auth_file: str | None = None,
) -> None:
    normalized = str(host or "").strip().lower()
    try:
        loopback = normalized == "localhost" or ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        loopback = False
    try:
        policy = load_api_auth_policy(
            legacy_token=api_token or "", policy_path=auth_file or "",
        )
    except ApiAuthError as exc:
        raise RuntimeError(str(exc)) from exc
    if not loopback and policy is None:
        raise RuntimeError(
            "non-loopback binding requires OPC_FINANCE_API_TOKEN or OPC_FINANCE_API_AUTH_FILE"
        )


def _box_entities() -> list[dict]:
    return [
        {"id": entity.entity_id, "name": entity.legal_name, "jurisdiction": entity.jurisdiction,
         "functional_currency": entity.functional_currency}
        for entity in BOX_RUNTIME.entities.all()
    ]


GAME_REFERENCE_GET_PATHS = {
    "/api/agent-goals", "/api/agent-workspace", "/api/agent-goal", "/api/agent-events",
    "/api/shadow-close", "/api/shadow-close-template", "/api/procurement-workflow",
    "/api/revenue-close",
}
GAME_REFERENCE_POST_PATHS = {
    "/api/agent-goals", "/api/agent-run", "/api/agent-refresh", "/api/agent-decision",
    "/api/shadow-close-review", "/api/shadow-close-import",
    "/api/revenue-close-review",
}


def _game_reference_enabled() -> bool:
    return any(
        pack.get("id") == "industry.game_studio"
        for pack in BOX_RUNTIME.snapshot().get("packs", [])
    )


def _confirmed_document_entity(document: dict) -> tuple[str, str]:
    scope = document.get("entity_scope") or {}
    entity_id = str(scope.get("entity_id") or "")
    if scope.get("status") != "confirmed" or not entity_id:
        raise ValueError("请先确认资料所属法律主体；不能按币种、国家或渠道自动猜测")
    BOX_RUNTIME.require_entity(entity_id)
    entity = BOX_RUNTIME.entities.get(entity_id)
    return entity_id, entity.legal_name


def _ensure_onboarding_template() -> Path:
    return ONBOARDING_TEMPLATE if ONBOARDING_TEMPLATE.exists() else build_onboarding_template(ONBOARDING_TEMPLATE)


def _ensure_shadow_close_template() -> Path:
    return SHADOW_CLOSE_TEMPLATE if SHADOW_CLOSE_TEMPLATE.exists() else build_shadow_close_template(SHADOW_CLOSE_TEMPLATE)


def _first_close_readiness(entity_id: str, period: str) -> dict:
    BOX_RUNTIME.require_entity(entity_id)
    runtime_entity = BOX_RUNTIME.entities.get(entity_id)
    entity = {
        "id": runtime_entity.entity_id, "name": runtime_entity.legal_name,
        "jurisdiction": runtime_entity.jurisdiction,
        "functional_currency": runtime_entity.functional_currency,
        "accounting_basis": runtime_entity.accounting_basis,
        "tax_readiness": runtime_entity.tax_readiness,
    }
    datasets = LEDGER.load_all()
    shadow_reports = []
    reviews = datasets.get("shadow_close_reviews") or []
    for baseline in datasets.get("shadow_close_baselines") or []:
        if baseline.get("entity_id") == entity_id and baseline.get("period") == period:
            shadow_reports.append(compare_shadow_close(
                baseline,
                _finance_from_store(period, entity_id),
                reviews,
                runtime_fingerprint=BOX_RUNTIME.snapshot()["fingerprint"],
            ))
    return build_first_close_readiness(
        entity_id=entity_id, period=period, entity=entity,
        profile_gaps=profile_gaps(load_profile(COMPANY_PROFILE)), datasets=datasets,
        documents=FINANCE_INBOX.list(1000),
        declarations=datasets.get("onboarding_declarations") or [],
        shadow_reports=shadow_reports, master_records=datasets.get("master_records") or [],
    )


def _combined_first_close_readiness(period: str) -> dict:
    reports = [_first_close_readiness(entity.entity_id, period) for entity in BOX_RUNTIME.entities.all()]
    blockers = [
        f"{report['entity_id']} · {blocker}"
        for report in reports for blocker in report.get("blockers") or []
    ]
    return {
        "period": period, "entities": reports, "blockers": blockers,
        "ready_for_shadow_close": not blockers,
        "control_note": "管理工作区集中查看首月覆盖，但每个主体单独补数、声明不适用并完成 Shadow Close。",
    }


def _ensure_demo_workbook(scenario: str) -> Path | None:
    scenarios = load_demo_scenarios(DEMO_SCENARIOS)
    if scenario == "group":
        target = DEMO_OUTPUTS / "智能财务工作台-全球管理汇总示例包.zip"
        if target.exists():
            return target
        domestic = _ensure_demo_workbook("domestic")
        overseas = _ensure_demo_workbook("overseas")
        if not domestic or not overseas:
            return None
        target.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.write(domestic, domestic.name)
            archive.write(overseas, overseas.name)
        return target
    config = scenarios.get(scenario)
    if not config:
        return None
    target = DEMO_OUTPUTS / config["filename"]
    return target if target.exists() else build_demo_workbook(config, target)


def _demo_payload(scenarios: dict, scenario: str) -> dict | None:
    if scenario == "group":
        return build_group_demo_payload(scenarios)
    config = scenarios.get(scenario)
    entity_id = {"domestic": "cn_studio", "overseas": "sg_publisher"}.get(scenario)
    return build_demo_payload(config, entity_id) if config else None


def _deep_update(target: dict, patch: dict) -> dict:
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_update(target[key], value)
        else:
            target[key] = value
    return target


def _analysis_datasets() -> tuple[dict[str, list[dict]], bool]:
    datasets = LEDGER.load_all()
    demo_mode = not any(datasets.values())
    if demo_mode:
        scenarios = load_demo_scenarios(DEMO_SCENARIOS)
        if scenarios.get("domestic"):
            datasets.update(build_demo_payload(scenarios["domestic"])["datasets"])
    return datasets, demo_mode


def _build_finance(datasets: dict[str, list[dict]], profile: dict, period: str) -> dict:
    return build_finance_ops(
        datasets["settlements"], period, datasets["purchases"],
        datasets["bank_transactions"], datasets["invoices"], datasets["payroll_rows"],
        profile, datasets["opening_balances"],
        datasets["asset_cards"], datasets["accruals"], datasets["posted_vouchers"],
        datasets["game_revenue_policies"],
        datasets["expense_claims"],
        datasets["ledger_adapter_reviews"],
        datasets["bank_reconciliation_reviews"],
    )


def _finance_from_store(period: str, entity_id: str | None = None):
    """Build finance only from the persistent ledger for state-changing workflows."""
    datasets = LEDGER.load_all()
    profile = load_profile(COMPANY_PROFILE)
    requested_entity = str(entity_id or "").strip()
    default_entity = str(profile.get("entity_id") or "").strip()
    entity_id = requested_entity or default_entity
    if entity_id:
        scoped = {}
        for name, records in datasets.items():
            if name == "tax_filing_reviews":
                scoped[name] = [row for row in records if str(row.get("entity_id") or "") == entity_id]
            else:
                scoped[name] = [
                    row for row in records
                    if str(row.get("entity_id") or (default_entity if not requested_entity else "")) == entity_id
                ]
        datasets = scoped
        profile = _entity_profile(entity_id)
    return _build_finance(datasets, profile, period)


def _statutory_entity(entity_id: str | None) -> tuple[str, object]:
    value = str(entity_id or "").strip()
    if not value:
        raise ValueError("法定财务动作必须选择法律主体")
    BOX_RUNTIME.require_entity(value)
    return value, BOX_RUNTIME.entities.get(value)


def _entity_profile(entity_id: str) -> dict:
    BOX_RUNTIME.require_entity(entity_id)
    entity = BOX_RUNTIME.entities.get(entity_id)
    profile = load_profile(COMPANY_PROFILE)
    profile["entity_id"] = entity_id
    profile["company_name"] = entity.legal_name
    profile["base_currency"] = entity.functional_currency
    profile["jurisdiction"] = entity.jurisdiction
    profile["accounting_basis"] = entity.accounting_basis
    profile["tax_pack"] = entity.tax_pack
    profile["tax_readiness"] = entity.tax_readiness or "design"
    try:
        profile["tax_authority_scope"] = str(
            (BOX_RUNTIME.tax_rules(entity_id).get("jurisdiction") or {}).get("authority_scope") or ""
        )
    except BoxRuntimeError:
        profile["tax_authority_scope"] = ""
    return profile


def _scoped_datasets(entity_id: str) -> dict[str, list[dict]]:
    return {
        name: [row for row in rows if str(row.get("entity_id") or "") == entity_id]
        for name, rows in LEDGER.load_all().items()
    }


def _guard_cn_filing_entity(entity_id: str | None) -> None:
    """CN filing forms must never be generated for the overseas legal entity."""
    entity_id = str(entity_id or load_profile(COMPANY_PROFILE).get("entity_id") or "cn_studio").strip()
    if entity_id != "cn_studio":
        raise ValueError("当前中国税务申报辅助包仅适用于 cn_studio；sg_publisher 应进入新加坡主体税务工作区")


def _agent_context(
    data_mode: str | None = None, demo_scenario: str = "group",
) -> tuple[dict[str, list[dict]], dict, str]:
    live = LEDGER.load_all()
    has_live = any(live.values())
    requested = data_mode if data_mode in {"live", "demo"} else ("live" if has_live else "demo")
    if requested == "live":
        return live, load_profile(COMPANY_PROFILE), "live"
    scenarios = load_demo_scenarios(DEMO_SCENARIOS)
    demo_scenario = demo_scenario if demo_scenario in {"group", "domestic", "overseas"} else "group"
    payload = _demo_payload(scenarios, demo_scenario)
    if not payload:
        return live, load_profile(COMPANY_PROFILE), "live"
    normalized = {name: [] for name in DATASETS}
    normalized.update(payload["datasets"])
    return normalized, payload["company_profile"], "demo"


def _ensure_default_goal(demo_scenario: str = "group") -> dict:
    datasets, _, data_mode = _agent_context(demo_scenario=demo_scenario)
    period = latest_period(datasets)
    for goal in AGENT_RUNTIME.list(500):
        goal_mode = goal.get("data_mode") or ("live" if any(LEDGER.load_all().values()) else "demo")
        goal_scenario = goal.get("demo_scenario") or "domestic"
        scenario_matches = data_mode == "live" or goal_scenario == demo_scenario
        if goal_mode == data_mode and scenario_matches and goal.get("status") not in {"已完成", "已取消"}:
            if goal.get("origin") != "system_default" or goal.get("period") == period:
                return goal
            goal["status"] = "已取消"
            goal["superseded_by_period"] = period
            AGENT_RUNTIME.save(goal)
            AGENT_RUNTIME.append_event(goal["id"], "GOAL_SUPERSEDED", "Agent", {
                "old_period": goal.get("period"), "new_period": period,
                "reason": "实际业务账期变化；未来预算期不作为月结账期",
            })
    objective = f"完成{period}财务月结、税务准备与90天资金安全检查"
    goal = AGENT_RUNTIME.create(
        objective, period, "Agent", data_mode=data_mode, origin="system_default",
        demo_scenario=demo_scenario,
    )
    if data_mode == "live":
        LEDGER.append_audit("Agent", "CREATE_DEFAULT_AGENT_GOAL", goal["id"], {
            "period": period, "data_mode": data_mode,
        })
    return goal


def _refresh_agent_goal(goal_id: str, actor: str = "Agent", event_type: str = "GOAL_REFRESHED") -> dict:
    goal = AGENT_RUNTIME.load(goal_id)
    datasets, profile, data_mode = _agent_context(
        goal.get("data_mode"), goal.get("demo_scenario") or "domestic",
    )
    goal["data_mode"] = data_mode
    if data_mode == "demo":
        # Backward-compatible migration for goals created before scenario isolation.
        goal["demo_scenario"] = goal.get("demo_scenario") or "domestic"
    period = goal["period"]
    previous_status = goal.get("status")
    previous = {item.get("id"): item.get("status") for item in goal.get("actions") or []}
    finance = _build_finance(datasets, profile, period)
    goal_entity_id = str(goal.get("entity_id") or "")
    period_state = LEDGER.load_period(period, goal_entity_id) if data_mode == "live" and goal_entity_id else (
        LEDGER.load_period(period) if data_mode == "live" else {
        "period": period, "status": "开放", "decisions": [], "voucher_reviews": {}, "close_events": [],
        }
    )
    finance["period_state"] = period_state
    planning = build_planning_analysis(
        datasets["plan_lines"], datasets["settlements"], datasets["purchases"],
        datasets["bank_transactions"], datasets["payroll_rows"], profile, period, "基准",
        datasets["collection_actions"], datasets["cash_allocations"],
    )
    analysis = build_bp_analysis(datasets, profile, period, "基准")
    shadow_reports = []
    if data_mode == "live":
        reviews = LEDGER.load_dataset("shadow_close_reviews")
        for baseline in LEDGER.load_dataset("shadow_close_baselines"):
            if baseline.get("period") != period:
                continue
            entity_id = str(baseline.get("entity_id") or "")
            shadow_reports.append(compare_shadow_close(
                baseline,
                _finance_from_store(period, entity_id),
                reviews,
                runtime_fingerprint=BOX_RUNTIME.snapshot()["fingerprint"],
            ))
    refreshed = build_goal_snapshot(
        goal,
        finance,
        {
            **build_onboarding(profile, datasets),
            **({"first_close": _combined_first_close_readiness(period)} if data_mode == "live" else {}),
        },
        period_state,
        build_flow_overview(datasets),
        planning,
        analysis,
        shadow_reports,
    )
    AGENT_RUNTIME.save(refreshed)
    transitions = [
        {"action_id": item["id"], "from": previous.get(item["id"]), "to": item["status"]}
        for item in refreshed.get("actions") or []
        if previous.get(item["id"]) != item.get("status")
    ]
    if transitions or previous_status != refreshed.get("status") or event_type in {"AGENT_RUN", "GOAL_PLANNED"}:
        AGENT_RUNTIME.append_event(goal_id, event_type, actor, {
            "status": refreshed["status"], "progress": refreshed["progress"], "transitions": transitions,
            "data_mode": data_mode,
        })
    return refreshed


def _agent_workspace(
    goal_id: str | None = None, *, actor: str = "Agent", event_type: str = "GOAL_REFRESHED",
    demo_scenario: str = "group",
) -> dict:
    seed = AGENT_RUNTIME.load(goal_id) if goal_id else _ensure_default_goal(demo_scenario)
    goal = _refresh_agent_goal(seed["id"], actor, event_type)
    datasets, profile, data_mode = _agent_context(
        goal.get("data_mode"), goal.get("demo_scenario") or demo_scenario,
    )
    finance = _build_finance(datasets, profile, goal["period"])
    goal_entity_id = str(goal.get("entity_id") or "")
    period_state = LEDGER.load_period(goal["period"], goal_entity_id) if data_mode == "live" and goal_entity_id else (
        LEDGER.load_period(goal["period"]) if data_mode == "live" else {
        "period": goal["period"], "status": "开放", "voucher_reviews": {},
        }
    )
    finance["period_state"] = period_state
    planning = build_planning_analysis(
        datasets["plan_lines"], datasets["settlements"], datasets["purchases"],
        datasets["bank_transactions"], datasets["payroll_rows"], profile, goal["period"], "基准",
        datasets["collection_actions"], datasets["cash_allocations"],
    )
    analysis = build_bp_analysis(datasets, profile, goal["period"], "基准")
    shadow_reports = []
    if data_mode == "live":
        reviews = LEDGER.load_dataset("shadow_close_reviews")
        for baseline in LEDGER.load_dataset("shadow_close_baselines"):
            if baseline.get("period") == goal["period"]:
                shadow_reports.append(compare_shadow_close(
                    baseline,
                    _finance_from_store(goal["period"], str(baseline.get("entity_id") or "")),
                    reviews,
                    runtime_fingerprint=BOX_RUNTIME.snapshot()["fingerprint"],
                ))
    tax_delivery = build_tax_delivery(
        finance["tax_pack"]["returns_workspace"], datasets.get("tax_filing_reviews") or [],
    )
    confirmations = build_confirmation_queue(
        goal, datasets, finance, tax_delivery, FINANCE_INBOX.list(500),
    )
    deliverables = build_deliverable_register(goal, finance, tax_delivery)
    entity_statutory_workspaces = []
    if data_mode == "demo" and (goal.get("demo_scenario") or demo_scenario) == "group":
        entity_results = []
        for scenario, entity_id, entity_name in (
            ("domestic", "cn_studio", "国服研发运营主体（演示）"),
            ("overseas", "sg_publisher", "海外发行主体（演示）"),
        ):
            entity_workspace = _agent_workspace(demo_scenario=scenario)
            entity_results.append((entity_id, entity_name, entity_workspace))
            entity_statutory_workspaces.append({
                "entity_id": entity_id, "entity_name": entity_name,
                "goal_id": entity_workspace["goal"]["id"], "status": entity_workspace["goal"]["status"],
                "confirmation_count": entity_workspace["confirmations"]["count"],
                "blocked_count": entity_workspace["confirmations"]["blocked_count"],
                "generated_deliverables": entity_workspace["deliverables"]["generated_count"],
            })
        combined_items = []
        combined_deliverables = []
        for entity_id, entity_name, entity_workspace in entity_results:
            for item in entity_workspace["confirmations"]["items"]:
                combined_items.append({**item, "entity_id": entity_id, "entity_name": entity_name})
            for item in entity_workspace["deliverables"]["items"]:
                combined_deliverables.append({**item, "entity_id": entity_id, "entity_name": entity_name})
        priority_order = {"紧急": 0, "高": 1, "中": 2, "普通": 3}
        combined_items.sort(key=lambda item: (priority_order.get(item.get("priority"), 9), item.get("entity_id", "")))
        exposures: dict[str, float] = {}
        for item in combined_items:
            amount = item.get("amount") or {}
            if amount.get("currency") and amount.get("value") is not None:
                currency = str(amount["currency"])
                exposures[currency] = exposures.get(currency, 0.0) + float(amount["value"] or 0)
        confirmations = {
            "items": combined_items, "count": len(combined_items),
            "decision_ready_count": sum(bool(item.get("decision")) and item.get("status") != "阻塞" for item in combined_items),
            "blocked_count": sum(item.get("status") == "阻塞" for item in combined_items),
            "amount_exposure_by_currency": [
                {"currency": currency, "value": round(value, 2)}
                for currency, value in sorted(exposures.items())
            ],
            "scope_guardrail": "全球队列只是集中查看；每个决定仍写入其 entity_id 对应的主体工作区。",
        }
        deliverables = {
            "items": combined_deliverables,
            "generated_count": sum(item.get("status") == "已生成" for item in combined_deliverables),
            "complete_count": sum(item.get("status") == "已完成" for item in combined_deliverables),
            "draft_count": sum(item.get("status") == "草稿待确认" for item in combined_deliverables),
            "scope_guardrail": "凭证、法定报表与税务工作底稿不生成全球法定合并版，始终按主体交付。",
        }
    demo_payload = _demo_payload(load_demo_scenarios(DEMO_SCENARIOS), goal.get("demo_scenario") or demo_scenario) if data_mode == "demo" else None
    return {
        "goal": goal,
        "goals": AGENT_RUNTIME.list(100),
        "confirmations": confirmations,
        "deliverables": deliverables,
        "events": AGENT_RUNTIME.events(goal["id"], 100),
        "operating_alerts": goal.get("operating_alerts") or [],
        "cash_safety": {
            "as_of_period": planning.get("as_of_period"),
            "opening_cash_cny": planning.get("opening_cash_cny"),
            "minimum_buffer_cny": planning.get("minimum_buffer_cny"),
            "buffer_breach_period": planning.get("buffer_breach_period"),
            "runway_months": planning.get("runway_months"),
            "next_90_days": (planning.get("forecast") or [])[:3],
            "recommendations": planning.get("recommendations") or [],
            "guardrail": planning.get("guardrail"),
        },
        "management_analysis": {
            "period": analysis.get("period"), "totals": analysis.get("totals") or {},
            "change_vs_previous": analysis.get("change_vs_previous") or {},
            "proactive_insights": analysis.get("proactive_insights") or [],
            "data_quality": analysis.get("data_quality") or {},
        },
        "shadow_close": {
            "reports": shadow_reports,
            "baseline_count": len(shadow_reports),
            "signed_count": sum(bool(item.get("review_current")) for item in shadow_reports),
            "exception_count": sum(int(item.get("exception_count") or 0) for item in shadow_reports),
            "guardrail": "只读验证按法律主体隔离；管理汇总不得覆盖主体总账、税务、银行和签认。",
        },
        "data_mode": data_mode,
        "is_demo": data_mode == "demo",
        "demo_scenario": goal.get("demo_scenario") if data_mode == "demo" else None,
        "scope_mode": (demo_payload or {}).get("scope_mode") or ("entity" if data_mode == "demo" else "live"),
        "entities": (demo_payload or {}).get("entities") or [],
        "entity_workspaces": (demo_payload or {}).get("entity_workspaces") or [],
        "statutory_guardrail": (demo_payload or {}).get("statutory_guardrail"),
        "elimination_policy": (demo_payload or {}).get("elimination_policy"),
        "entity_statutory_workspaces": entity_statutory_workspaces,
        "datasets": datasets,
        "scale_principle": "小团队不等于低营收：系统按高流水、多渠道、多币种设计，自动处理明细，人工只确认关键判断和资金动作。",
        "automation_boundary": {
            "automatic": ["资料分类与识别", "规则校验", "勾稽匹配", "计算与草拟", "异常排序", "交付包生成"],
            "human_confirmation": ["业务验收", "付款和报销", "收入会计政策", "凭证过账", "税务口径与申报结果"],
            "never_claimed": ["未获授权的银行付款", "未取得回执的法定申报", "未复核的职业判断"],
        },
    }


def _run_safe_agent_automations(goal: dict, actor: str) -> dict:
    documents = FINANCE_INBOX.list(500)
    fallback_period = goal.get("period") if goal.get("data_mode") == "live" else None
    planned = plan_safe_document_automations(documents, fallback_period=fallback_period)
    completed, failed = [], []
    for action in planned:
        try:
            document = FINANCE_INBOX.recognize(
                action["document_id"], action["document_type"], action["period"],
                LEDGER.load_all(), "Agent", action["entity_id"],
                BOX_RUNTIME.entities.get(action["entity_id"]).legal_name,
                _entity_profile(action["entity_id"]),
            )
            completed.append({
                **action,
                "status": document.get("status"),
                "record_count": (document.get("recognition") or {}).get("record_count", 0),
            })
        except (OSError, RuntimeError, ValueError) as error:
            failed.append({**action, "error": str(error)[:500]})
    result = {
        "planned_count": len(planned), "completed_count": len(completed), "failed_count": len(failed),
        "completed": completed, "failed": failed,
        "guardrail": "Agent 只完成资料识别与预览；写入正式台账仍需 CONFIRM_IMPORT。",
    }
    if planned:
        AGENT_RUNTIME.append_event(goal["id"], "SAFE_AUTOMATIONS_RUN", actor, result)
    return result


def _record_period(dataset: str, record: dict) -> str:
    field = {
        "settlements": "period", "payroll_rows": "period", "plan_lines": "period",
        "opening_balances": "period", "game_kpis": "period",
        "cash_allocations": "period", "payment_requests": "period", "expense_claims": "claim_date",
        "purchases": "order_date", "purchase_deliveries": "delivery_date",
        "bank_transactions": "transaction_date", "invoices": "invoice_date",
    }.get(dataset, "period")
    value = str(record.get(field) or "")
    return value[:7] if len(value) >= 7 else ""


def _guard_open_periods(dataset: str, records: list[dict]):
    closed = sorted({
        (str(record.get("entity_id") or ""), period)
        for record in records if (period := _record_period(dataset, record))
        and LEDGER.load_period(period, str(record.get("entity_id") or "")).get("status") == "已关账"
    })
    if closed:
        labels = [f"{entity_id or '旧单主体'}:{period}" for entity_id, period in closed]
        raise ValueError(f"主体期间 {'、'.join(labels)} 已关账，不能直接改写；请通过下期调整或有记录的重开流程处理")


def _bind_import_entity(records: list[dict], entity_id: str) -> list[dict]:
    """Bind a formal import to one statutory entity without trusting workbook content."""
    entity_id, _ = _statutory_entity(entity_id)
    bound = []
    for source in records:
        record = dict(source)
        embedded = str(record.get("entity_id") or "").strip()
        if embedded and embedded != entity_id:
            raise ValueError(
                f"导入记录声明主体 {embedded}，与所选主体 {entity_id} 不一致；请拆分文件后分别导入"
            )
        record["entity_id"] = entity_id
        bound.append(record)
    return bound


def _guard_profile_change(profile: dict):
    closed_periods = []
    if LEDGER.periods.exists():
        for path in LEDGER.periods.glob("*.json"):
            try:
                if LEDGER.load_period(path.stem).get("status") == "已关账":
                    closed_periods.append(path.stem)
            except ValueError:
                continue
    if not closed_periods:
        return
    old = load_profile(COMPANY_PROFILE)
    protected = ("base_currency", "accounting_standard", "vat_taxpayer_type", "cit_collection_method")
    changed = [key for key in protected if profile.get(key, old.get(key)) != old.get(key)]
    old_fx = (old.get("fx_policy") or {}).get("month_end_rates") or {}
    new_fx = (profile.get("fx_policy") or {}).get("month_end_rates") or {}
    changed_fx = [period for period in closed_periods if new_fx.get(period, old_fx.get(period)) != old_fx.get(period)]
    if changed or changed_fx:
        detail = "、".join(changed + [f"{period}汇率" for period in changed_fx])
        raise ValueError(f"已关账期间存在时不能直接修改 {detail}；请先执行有审计记录的重开流程")


def _load_templates():
    if not TEMPLATES.exists():
        return []
    try:
        payload = json.loads(TEMPLATES.read_text(encoding="utf-8"))
        return payload if isinstance(payload, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _save_templates(templates):
    TEMPLATES.parent.mkdir(parents=True, exist_ok=True)
    TEMPLATES.write_text(json.dumps(templates, ensure_ascii=False, indent=2), encoding="utf-8")


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(PUBLIC), **kwargs)

    def _json(self, payload, status=HTTPStatus.OK, *, headers: dict[str, str] | None = None):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store, max-age=0")
        self._security_headers()
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _text(self, body: str, content_type: str):
        encoded = body.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store, max-age=0")
        self._security_headers()
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _security_headers(self):
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")

    @staticmethod
    def _required_api_role(method: str, path: str) -> str:
        if method in {"GET", "HEAD"}:
            return "reader"
        if path in {
            "/api/box/pipelines/preflight", "/api/box-builder/preview",
            "/api/box-builder/bundle",
        }:
            return "reader"
        if path in {
            "/api/box/pipeline-run-reviews", "/api/payment-decision", "/api/expense-decision",
            "/api/purchase-acceptance", "/api/procurement-request-decision", "/api/vendor-bank-change-decision", "/api/voucher-review", "/api/tax-review",
            "/api/tax-form-review", "/api/tax-submission", "/api/shadow-close-review",
            "/api/onboarding-declaration", "/api/asset-review", "/api/accrual-review",
            "/api/ledger-adapter-review", "/api/bank-reconciliation-review",
        }:
            return "reviewer"
        if path in {
            "/api/box/services/dispatch", "/api/box/pipelines/dispatch",
            "/api/box/pipeline-runs", "/api/box/pipeline-schedule/run",
            "/api/payment-request", "/api/expense-claim", "/api/asset-card", "/api/accrual",
            "/api/cash-allocation", "/api/collection-action", "/api/procurement-request", "/api/purchase-order", "/api/purchase-delivery", "/api/vendor-bank-change", "/api/shadow-close-import",
            "/api/inbox-recognize", "/api/inbox-commit", "/api/inbox-entity",
            "/api/inbox-correct", "/api/inbox-link",
        }:
            return "operator"
        return "admin"

    def _require_api_auth(self, path: str, method: str) -> bool:
        self._api_principal = None
        self._api_auth_mode = "anonymous_loopback"
        if not path.startswith("/api/") or path == "/api/health":
            return False
        try:
            policy = load_api_auth_policy()
        except ApiAuthError:
            self._json(
                {"error": "API authentication is misconfigured", "type": "authentication_misconfigured"},
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return True
        if policy is None:
            return False
        supplied = self.headers.get("Authorization") or ""
        scheme, separator, candidate = supplied.partition(" ")
        principal = policy.authenticate(candidate if separator and scheme.lower() == "bearer" else "")
        if principal is None:
            self._json(
                {"error": "valid Bearer authentication is required", "type": "authentication_required"},
                HTTPStatus.UNAUTHORIZED,
                headers={"WWW-Authenticate": "Bearer"},
            )
            return True
        required_role = self._required_api_role(method, path)
        if not principal.allows(required_role):
            self._json(
                {
                    "error": f"API principal requires {required_role} role for this operation",
                    "type": "authorization_forbidden",
                },
                HTTPStatus.FORBIDDEN,
            )
            return True
        self._api_principal = principal
        self._api_auth_mode = policy.mode
        return False

    def _receive_airwallex_webhook(self):
        try:
            BOX_RUNTIME.require_capability("connector.airwallex_approved_expenses")
        except BoxRuntimeError:
            return self._json(
                {"error": "Airwallex webhook is not enabled for this Box", "type": "webhook_not_enabled"},
                HTTPStatus.NOT_FOUND,
            )
        raw_length = self.headers.get("Content-Length")
        try:
            content_length = int(raw_length) if raw_length is not None else -1
        except (TypeError, ValueError):
            content_length = -1
        if content_length < 0:
            return self._json(
                {"error": "Airwallex webhook requires a valid Content-Length", "type": "invalid_content_length"},
                HTTPStatus.BAD_REQUEST,
            )
        if content_length > AIRWALLEX_WEBHOOK_MAX_BODY_BYTES:
            return self._json(
                {"error": "Airwallex webhook body exceeds 1 MiB", "type": "webhook_payload_too_large"},
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            )
        raw_body = self.rfile.read(content_length)
        if len(raw_body) != content_length:
            return self._json(
                {"error": "Airwallex webhook body was truncated", "type": "invalid_webhook_payload"},
                HTTPStatus.BAD_REQUEST,
            )
        try:
            result = AIRWALLEX_WEBHOOKS.receive(
                raw_body,
                timestamp=self.headers.get("x-timestamp") or "",
                signature=self.headers.get("x-signature") or "",
                secret=os.environ.get(WEBHOOK_SECRET_ENV, ""),
                entity_bindings_json=os.environ.get(ENTITY_BINDINGS_ENV, ""),
                allowed_entity_ids=set(BOX_RUNTIME.entities.ids()),
                runtime_fingerprint=BOX_RUNTIME.snapshot()["fingerprint"],
            )
        except AirwallexWebhookError as error:
            return self._json(
                {"error": str(error), "type": error.error_type},
                error.http_status,
            )
        return self._json(result, HTTPStatus.OK)

    def end_headers(self):
        path = urlparse(self.path).path
        if path == "/" or path.endswith((".html", ".css", ".js")):
            self.send_header("Cache-Control", "no-store, max-age=0")
        super().end_headers()

    def _download(
        self,
        body: bytes,
        filename: str,
        content_type: str = "application/octet-stream",
        response_headers: dict[str, str] | None = None,
    ):
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store, max-age=0")
        self._security_headers()
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(body)))
        for name, value in sorted((response_headers or {}).items()):
            if not re.fullmatch(r"[A-Za-z0-9-]+", name):
                raise ValueError("download response header name is invalid")
            if "\r" in value or "\n" in value:
                raise ValueError("download response header value is invalid")
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        if self._require_api_auth(parsed.path, "GET"):
            return
        if not _game_reference_enabled():
            if parsed.path in {"/api/sample", "/api/demo-workbook"}:
                return self._json({
                    "error": "the selected Box has no compatible bundled demo dataset",
                    "type": "box_demo_unavailable",
                }, HTTPStatus.NOT_FOUND)
            if parsed.path in GAME_REFERENCE_GET_PATHS:
                return self._json({
                    "error": "the game Agent reference workflow is not enabled for this Box",
                    "type": "workbench_profile_mismatch",
                }, HTTPStatus.CONFLICT)
        if parsed.path == "/api/health":
            return self._json({"status": "ok", "product": "智能财务工作台"})
        if parsed.path == "/api/auth/whoami":
            principal = getattr(self, "_api_principal", None)
            return self._json({
                "authentication_mode": self._api_auth_mode,
                "principal": principal.public_dict() if principal else {
                    "principal_id": "anonymous_loopback", "roles": ["admin"],
                },
                "local_anonymous_mode": principal is None,
            })
        if parsed.path == "/api/box":
            query = parse_qs(parsed.query)
            scope = (query.get("scope") or ["management"])[0]
            entity_id = (query.get("entity_id") or [None])[0]
            entity_ids = [item for item in (query.get("entity_ids") or []) if item]
            try:
                return self._json(build_box_bootstrap(
                    BOX_RUNTIME, BOX_SERVICES, scope=scope, entity_id=entity_id,
                    entity_ids=entity_ids or None,
                ))
            except (BoxRuntimeError, ValueError) as error:
                return self._json({"error": str(error), "type": "box_scope_error"}, HTTPStatus.BAD_REQUEST)
        if parsed.path == "/api/box/pipelines":
            try:
                return self._json(build_pipeline_runtime_catalog(BOX_RUNTIME, BOX_SERVICES))
            except (BoxRuntimeError, ValueError) as error:
                return self._json(
                    {"error": str(error), "type": "pipeline_catalog_error"},
                    HTTPStatus.BAD_REQUEST,
                )
        if parsed.path == "/api/box-builder/options":
            try:
                return self._json(list_box_builder_options(
                    load_pack_catalog(BOX_RUNTIME.packs_root),
                ))
            except ValueError as error:
                return self._json(
                    {"error": str(error), "type": "box_builder_options_error"},
                    HTTPStatus.BAD_REQUEST,
                )
        if parsed.path == "/api/box/trial-onboarding":
            trial_root = str(os.environ.get(TRIAL_WORKSPACE_ROOT_ENV) or "").strip()
            if not trial_root:
                return self._json({
                    "schema_version": 1,
                    "artifact_type": "opc_finance_box_trial_onboarding_projection",
                    "available": False,
                    "reason": "trial_workspace_not_configured",
                    "control_boundary": {
                        "trial_workspace_path_returned": False,
                        "commands_executed": False,
                        "production_readiness_inferred": False,
                        "external_actions_performed": False,
                    },
                })
            try:
                return self._json({
                    "available": True,
                    **build_trial_onboarding_plan(
                        Path(trial_root), BOX_RUNTIME.packs_root,
                    ),
                })
            except (TrialWorkspaceError, OSError, ValueError) as error:
                return self._json(
                    {"error": str(error), "type": "trial_onboarding_error"},
                    HTTPStatus.BAD_REQUEST,
                )
        if parsed.path == "/api/box/connectors/readiness":
            try:
                return self._json(build_connector_onboarding(BOX_RUNTIME))
            except (BoxRuntimeError, ValueError) as error:
                return self._json(
                    {"error": str(error), "type": "connector_readiness_error"},
                    HTTPStatus.BAD_REQUEST,
                )
        if parsed.path == "/api/box/connector-shadow":
            query = parse_qs(parsed.query)
            try:
                as_of_values = query.get("as_of") or []
                if len(as_of_values) > 1:
                    raise ValueError("as_of query parameter must appear at most once")
                return self._json(build_connector_shadow_registry_workspace(
                    BOX_RUNTIME,
                    (
                        Path(os.environ["OPC_CONNECTOR_SHADOW_REVIEW_DIR"])
                        if os.environ.get("OPC_CONNECTOR_SHADOW_REVIEW_DIR") else None
                    ),
                    as_of=as_of_values[0] if as_of_values else None,
                ))
            except (BoxRuntimeError, ValueError) as error:
                return self._json(
                    {"error": str(error), "type": "connector_shadow_registry_error"},
                    HTTPStatus.BAD_REQUEST,
                )
        if parsed.path == "/api/box/production-readiness":
            query = parse_qs(parsed.query)
            try:
                as_of_values = query.get("as_of") or []
                if len(as_of_values) > 1:
                    raise ValueError("as_of query parameter must appear at most once")
                return self._json(build_production_readiness_workspace(
                    BOX_RUNTIME,
                    BOX_SERVICES,
                    runs_root=PIPELINE_RUNS.root,
                    as_of=as_of_values[0] if as_of_values else None,
                ))
            except (BoxRuntimeError, ValueError) as error:
                return self._json(
                    {"error": str(error), "type": "production_readiness_error"},
                    HTTPStatus.BAD_REQUEST,
                )
        if parsed.path == "/api/box/activation":
            query = parse_qs(parsed.query)
            try:
                as_of_values = query.get("as_of") or []
                if len(as_of_values) > 1:
                    raise ValueError("as_of query parameter must appear at most once")
                activation_root = os.environ.get(ACTIVATION_WORKSPACE_ROOT_ENV)
                if activation_root:
                    initialized = build_initialized_activation_status(
                        BOX_RUNTIME,
                        BOX_SERVICES,
                        Path(activation_root),
                        as_of=as_of_values[0] if as_of_values else None,
                    )
                    result = {
                        **initialized["activation"],
                        "initialized_workspace": initialized["workspace"],
                        "connector_access": initialized["connector_access"],
                        "connector_access_alerts": initialized[
                            "connector_access_alerts"
                        ],
                        "control_boundary": {
                            **initialized["activation"]["control_boundary"],
                            "server_mounted_activation_workspace_used": True,
                            "activation_root_accepted_from_request": False,
                            "private_paths_returned": False,
                            "provider_account_identifiers_returned": False,
                            "credential_values_returned": False,
                        },
                    }
                else:
                    result = build_activation_workspace(
                        BOX_RUNTIME,
                        BOX_SERVICES,
                        runs_root=PIPELINE_RUNS.root,
                        as_of=as_of_values[0] if as_of_values else None,
                    )
                return self._json(result)
            except (ActivationWorkspaceError, BoxRuntimeError, ValueError) as error:
                return self._json(
                    {"error": str(error), "type": "activation_workspace_error"},
                    HTTPStatus.BAD_REQUEST,
                )
        if parsed.path == "/api/box/pilot-readiness":
            query = parse_qs(parsed.query)
            try:
                as_of_values = query.get("as_of") or []
                if len(as_of_values) > 1:
                    raise ValueError("as_of query parameter must appear at most once")
                return self._json(build_pilot_readiness_workspace(
                    BOX_RUNTIME,
                    (
                        Path(os.environ["OPC_PILOT_READINESS_REVIEW"])
                        if os.environ.get("OPC_PILOT_READINESS_REVIEW") else None
                    ),
                    tax_review_dir=(
                        Path(os.environ["OPC_TAX_APPLICABILITY_REVIEW_DIR"])
                        if os.environ.get("OPC_TAX_APPLICABILITY_REVIEW_DIR") else None
                    ),
                    tax_registry_receipt=(
                        Path(os.environ["OPC_TAX_APPLICABILITY_REGISTRY_RECEIPT"])
                        if os.environ.get(
                            "OPC_TAX_APPLICABILITY_REGISTRY_RECEIPT"
                        ) else None
                    ),
                    as_of=as_of_values[0] if as_of_values else None,
                ))
            except (BoxRuntimeError, ValueError) as error:
                return self._json(
                    {"error": str(error), "type": "pilot_readiness_error"},
                    HTTPStatus.BAD_REQUEST,
                )
        if parsed.path == "/api/box/pilot-data-handoff":
            query = parse_qs(parsed.query)
            try:
                as_of_values = query.get("as_of") or []
                if len(as_of_values) > 1:
                    raise ValueError("as_of query parameter must appear at most once")
                return self._json(build_pilot_data_handoff_workspace(
                    BOX_RUNTIME,
                    (
                        Path(os.environ["OPC_PILOT_DATA_HANDOFF_REVIEW"])
                        if os.environ.get("OPC_PILOT_DATA_HANDOFF_REVIEW") else None
                    ),
                    (
                        Path(os.environ["OPC_PILOT_READINESS_REVIEW"])
                        if os.environ.get("OPC_PILOT_READINESS_REVIEW") else None
                    ),
                    as_of=as_of_values[0] if as_of_values else None,
                ))
            except (BoxRuntimeError, ValueError) as error:
                return self._json(
                    {"error": str(error), "type": "pilot_data_handoff_error"},
                    HTTPStatus.BAD_REQUEST,
                )
        if parsed.path == "/api/box/pilot-shadow-run":
            query = parse_qs(parsed.query)
            try:
                as_of_values = query.get("as_of") or []
                if len(as_of_values) > 1:
                    raise ValueError("as_of query parameter must appear at most once")
                return self._json(build_pilot_shadow_run_workspace(
                    BOX_RUNTIME,
                    (
                        Path(os.environ["OPC_PILOT_SHADOW_RUN_REGISTRATION"])
                        if os.environ.get("OPC_PILOT_SHADOW_RUN_REGISTRATION")
                        else None
                    ),
                    (
                        Path(os.environ["OPC_PILOT_DATA_HANDOFF_REVIEW"])
                        if os.environ.get("OPC_PILOT_DATA_HANDOFF_REVIEW") else None
                    ),
                    (
                        Path(os.environ["OPC_PILOT_READINESS_REVIEW"])
                        if os.environ.get("OPC_PILOT_READINESS_REVIEW") else None
                    ),
                    PIPELINE_RUNS.root,
                    as_of=as_of_values[0] if as_of_values else None,
                ))
            except (BoxRuntimeError, ValueError) as error:
                return self._json(
                    {"error": str(error), "type": "pilot_shadow_run_error"},
                    HTTPStatus.BAD_REQUEST,
                )
        if parsed.path == "/api/box/pilot-shadow-observation":
            query = parse_qs(parsed.query)
            try:
                as_of_values = query.get("as_of") or []
                if len(as_of_values) > 1:
                    raise ValueError("as_of query parameter must appear at most once")
                return self._json(build_pilot_shadow_observation_workspace(
                    BOX_RUNTIME,
                    (
                        Path(os.environ["OPC_PILOT_SHADOW_OBSERVATION_REVIEW"])
                        if os.environ.get("OPC_PILOT_SHADOW_OBSERVATION_REVIEW")
                        else None
                    ),
                    (
                        Path(os.environ["OPC_PILOT_SHADOW_RUN_REGISTRATION"])
                        if os.environ.get("OPC_PILOT_SHADOW_RUN_REGISTRATION")
                        else None
                    ),
                    (
                        Path(os.environ["OPC_PILOT_DATA_HANDOFF_REVIEW"])
                        if os.environ.get("OPC_PILOT_DATA_HANDOFF_REVIEW") else None
                    ),
                    (
                        Path(os.environ["OPC_PILOT_READINESS_REVIEW"])
                        if os.environ.get("OPC_PILOT_READINESS_REVIEW") else None
                    ),
                    PIPELINE_RUNS.root,
                    (
                        Path(os.environ["OPC_PILOT_SHADOW_ENTITY_REPORT_DIR"])
                        if os.environ.get("OPC_PILOT_SHADOW_ENTITY_REPORT_DIR")
                        else None
                    ),
                    portfolio_review_path=(
                        Path(os.environ["OPC_PILOT_SHADOW_PORTFOLIO_REVIEW"])
                        if os.environ.get("OPC_PILOT_SHADOW_PORTFOLIO_REVIEW")
                        else None
                    ),
                    as_of=as_of_values[0] if as_of_values else None,
                ))
            except (BoxRuntimeError, ValueError) as error:
                return self._json(
                    {"error": str(error), "type": "pilot_shadow_observation_error"},
                    HTTPStatus.BAD_REQUEST,
                )
        if parsed.path == "/api/box/pilot-shadow-series":
            query = parse_qs(parsed.query)
            try:
                as_of_values = query.get("as_of") or []
                if len(as_of_values) > 1:
                    raise ValueError("as_of query parameter must appear at most once")
                return self._json(build_pilot_shadow_series_workspace(
                    BOX_RUNTIME,
                    (
                        Path(os.environ["OPC_PILOT_SHADOW_SERIES_REVIEW"])
                        if os.environ.get("OPC_PILOT_SHADOW_SERIES_REVIEW")
                        else None
                    ),
                    (
                        Path(os.environ["OPC_PILOT_SHADOW_SERIES_EVIDENCE_ROOT"])
                        if os.environ.get("OPC_PILOT_SHADOW_SERIES_EVIDENCE_ROOT")
                        else None
                    ),
                    PIPELINE_RUNS.root,
                    as_of=as_of_values[0] if as_of_values else None,
                ))
            except (BoxRuntimeError, ValueError) as error:
                return self._json(
                    {"error": str(error), "type": "pilot_shadow_series_error"},
                    HTTPStatus.BAD_REQUEST,
                )
        if parsed.path == "/api/box/pilot-shadow-periods":
            try:
                return self._json(build_pilot_shadow_period_workspace_index(
                    BOX_RUNTIME,
                    (
                        Path(os.environ[ACTIVATION_WORKSPACE_ROOT_ENV])
                        if os.environ.get(ACTIVATION_WORKSPACE_ROOT_ENV)
                        else None
                    ),
                ))
            except (BoxRuntimeError, PilotShadowPeriodIndexError) as error:
                return self._json(
                    {"error": str(error), "type": "pilot_shadow_period_index_error"},
                    HTTPStatus.BAD_REQUEST,
                )
        if parsed.path == "/api/box/tax/workspace":
            query = parse_qs(parsed.query)
            try:
                anchor_values = query.get("anchors") or []
                if len(anchor_values) > 1:
                    raise ValueError("anchors query parameter must appear at most once")
                if anchor_values and len(anchor_values[0]) > 10000:
                    raise ValueError("anchors query parameter is too large")
                anchors = json.loads(anchor_values[0]) if anchor_values else None
                return self._json(build_tax_workspace(
                    BOX_RUNTIME,
                    BOX_SERVICES,
                    period_year=(query.get("period_year") or [None])[0],
                    as_of=(query.get("as_of") or [None])[0],
                    anchors=anchors,
                    applicability_review_dir=(
                        Path(os.environ["OPC_TAX_APPLICABILITY_REVIEW_DIR"])
                        if os.environ.get("OPC_TAX_APPLICABILITY_REVIEW_DIR") else None
                    ),
                    applicability_registry_receipt=(
                        Path(os.environ["OPC_TAX_APPLICABILITY_REGISTRY_RECEIPT"])
                        if os.environ.get(
                            "OPC_TAX_APPLICABILITY_REGISTRY_RECEIPT"
                        ) else None
                    ),
                ))
            except (BoxRuntimeError, json.JSONDecodeError, PackServiceError, ValueError) as error:
                return self._json(
                    {"error": str(error), "type": "tax_workspace_error"},
                    HTTPStatus.BAD_REQUEST,
                )
        if parsed.path == "/api/box/pipeline-runs":
            query = parse_qs(parsed.query)
            try:
                limit = int((query.get("limit") or ["50"])[0])
                return self._json({"runs": PIPELINE_RUNS.list(
                    runtime_fingerprint=BOX_RUNTIME.snapshot()["fingerprint"],
                    pipeline_id=(query.get("pipeline_id") or [None])[0],
                    entity_id=(query.get("entity_id") or [None])[0],
                    limit=limit,
                )})
            except (ValueError, PipelineRunStoreError) as error:
                return self._json({"error": str(error), "type": "invalid_pipeline_run_query"}, HTTPStatus.BAD_REQUEST)
        if parsed.path == "/api/box/pipeline-review-queue":
            query = parse_qs(parsed.query)
            try:
                limit = int((query.get("limit") or ["100"])[0])
                if not 1 <= limit <= 500:
                    raise ValueError("limit must be an integer from 1 to 500")
                return self._json({"review_tasks": PIPELINE_RUNS.review_queue(
                    runtime_fingerprint=BOX_RUNTIME.snapshot()["fingerprint"],
                    pipeline_id=(query.get("pipeline_id") or [None])[0],
                    entity_id=(query.get("entity_id") or [None])[0],
                    limit=limit,
                )})
            except (ValueError, PipelineRunStoreError) as error:
                return self._json(
                    {"error": str(error), "type": "invalid_pipeline_review_queue_query"},
                    HTTPStatus.BAD_REQUEST,
                )
        if parsed.path == "/api/box/pipeline-run-integrity":
            try:
                return self._json({"integrity": PIPELINE_RUNS.verify(
                    runtime_fingerprint=BOX_RUNTIME.snapshot()["fingerprint"],
                )})
            except PipelineRunStoreError as error:
                return self._json(
                    {"error": str(error), "type": "pipeline_run_ledger_invalid"},
                    HTTPStatus.CONFLICT,
                )
        if parsed.path == "/api/box/pipeline-schedule":
            if not PIPELINE_SCHEDULE_FILE:
                return self._json({
                    "configured": False,
                    "environment_reference": "OPC_FINANCE_PIPELINE_SCHEDULE_FILE",
                    "schedule_path_returned": False,
                    "dispatch_performed": False,
                    "external_actions_performed": False,
                })
            try:
                return self._json({
                    "configured": True,
                    "schedule": inspect_pipeline_schedule(
                        PIPELINE_SCHEDULE_FILE, BOX_RUNTIME, PIPELINE_RUNS,
                    ),
                    "schedule_path_returned": False,
                })
            except (PipelineScheduleError, PipelineRunStoreError) as error:
                return self._json(
                    {"error": str(error), "type": "pipeline_schedule_invalid"},
                    HTTPStatus.CONFLICT,
                )
        if parsed.path == "/api/box/pipeline-observability":
            query = parse_qs(parsed.query)
            formats = query.get("format") or ["json"]
            if len(formats) != 1 or formats[0] not in {"json", "prometheus"}:
                return self._json({
                    "error": "format must be json or prometheus",
                    "type": "invalid_pipeline_observability_query",
                }, HTTPStatus.BAD_REQUEST)
            try:
                result = build_pipeline_observability(
                    BOX_RUNTIME, PIPELINE_RUNS,
                    schedule_path=PIPELINE_SCHEDULE_FILE,
                )
                if formats[0] == "prometheus":
                    return self._text(
                        render_pipeline_prometheus(result),
                        "text/plain; version=0.0.4; charset=utf-8",
                    )
                return self._json(result)
            except (PipelineScheduleError, PipelineRunStoreError) as error:
                return self._json(
                    {"error": str(error), "type": "pipeline_observability_invalid"},
                    HTTPStatus.CONFLICT,
                )
        if parsed.path == "/api/box/connector-sync":
            query = parse_qs(parsed.query)
            try:
                limits = query.get("limit") or ["100"]
                if len(limits) != 1:
                    raise ValueError("limit must be supplied at most once")
                limit = int(limits[0])
                if not 1 <= limit <= 500:
                    raise ValueError("limit must be an integer from 1 to 500")
            except ValueError as error:
                return self._json(
                    {"error": str(error), "type": "invalid_connector_sync_query"},
                    HTTPStatus.BAD_REQUEST,
                )
            try:
                return self._json(CONNECTOR_SYNCS.status(
                    runtime_fingerprint=BOX_RUNTIME.snapshot()["fingerprint"],
                    limit=limit,
                ))
            except ConnectorSyncError as error:
                return self._json(
                    {"error": str(error), "type": "connector_sync_ledger_invalid"},
                    HTTPStatus.CONFLICT,
                )
        if parsed.path.startswith("/api/box/pipeline-runs/"):
            attempt_id = parsed.path.rsplit("/", 1)[-1]
            try:
                record = PIPELINE_RUNS.get(
                    attempt_id, runtime_fingerprint=BOX_RUNTIME.snapshot()["fingerprint"],
                )
            except PipelineRunStoreError as error:
                return self._json({"error": str(error), "type": "invalid_pipeline_run_query"}, HTTPStatus.BAD_REQUEST)
            if record is None:
                return self._json(
                    {"error": "pipeline run attempt not found", "type": "pipeline_run_not_found"},
                    HTTPStatus.NOT_FOUND,
                )
            return self._json({"run": record})
        if parsed.path == "/api/sample":
            scenario = (parse_qs(parsed.query).get("scenario") or ["group"])[0]
            scenarios = load_demo_scenarios(DEMO_SCENARIOS)
            payload = _demo_payload(scenarios, scenario)
            if payload:
                return self._json(payload)
            return self._json({"error": "示例数据不存在，请选择 group、domestic 或 overseas"}, HTTPStatus.NOT_FOUND)
        if parsed.path == "/api/demo-workbook":
            scenario = (parse_qs(parsed.query).get("scenario") or ["group"])[0]
            target = _ensure_demo_workbook(scenario)
            if not target:
                return self._json({"error": "示例不存在，请选择 group、domestic 或 overseas"}, HTTPStatus.NOT_FOUND)
            if target.suffix == ".zip":
                return self._download(target.read_bytes(), "finance-workspace-group-demo.zip", "application/zip")
            return self._download(target.read_bytes(), f"finance-workspace-{scenario}-demo.xlsx",
                                  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        if parsed.path == "/api/templates":
            return self._json({"templates": _load_templates()})
        if parsed.path == "/api/onboarding-template":
            target = _ensure_onboarding_template()
            return self._download(target.read_bytes(), "finance-workspace-onboarding.xlsx",
                                  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        if parsed.path == "/api/shadow-close-template":
            target = _ensure_shadow_close_template()
            return self._download(target.read_bytes(), "finance-workspace-shadow-close.xlsx",
                                  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        if parsed.path == "/api/company-profile":
            profile = load_profile(COMPANY_PROFILE)
            return self._json({"profile": profile, "gaps": profile_gaps(profile)})
        if parsed.path == "/api/workspace":
            datasets = LEDGER.load_all()
            periods = sorted({
                record.get("period") for name in ("settlements", "payroll_rows")
                for record in datasets[name] if record.get("period")
            }, reverse=True)
            return self._json({
                "datasets": datasets,
                "periods": periods,
                "period_states": {
                    entity.entity_id: {
                        period: LEDGER.load_period(period, entity.entity_id) for period in periods
                    }
                    for entity in BOX_RUNTIME.entities.all()
                },
                "legacy_period_states": {period: LEDGER.load_period(period) for period in periods},
                "has_persistent_data": any(datasets.values()),
            })
        if parsed.path == "/api/audit":
            raw_limit = (parse_qs(parsed.query).get("limit") or ["200"])[0]
            try:
                limit = int(raw_limit)
            except ValueError:
                limit = 200
            return self._json({"events": LEDGER.audit_events(limit)})
        if parsed.path == "/api/agent-goals":
            scenario = (parse_qs(parsed.query).get("scenario") or ["group"])[0]
            _ensure_default_goal(scenario)
            goals = []
            for item in AGENT_RUNTIME.list():
                try:
                    goals.append(_refresh_agent_goal(item["id"]))
                except ValueError:
                    goals.append(item)
            return self._json({"goals": goals})
        if parsed.path == "/api/agent-workspace":
            query = parse_qs(parsed.query)
            goal_id = (query.get("goal_id") or [None])[0]
            scenario = (query.get("scenario") or ["group"])[0]
            try:
                return self._json(_agent_workspace(goal_id, demo_scenario=scenario))
            except ValueError as error:
                return self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        if parsed.path == "/api/agent-goal":
            goal_id = (parse_qs(parsed.query).get("id") or [""])[0]
            try:
                return self._json({"goal": _refresh_agent_goal(goal_id)})
            except ValueError as error:
                return self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        if parsed.path == "/api/agent-events":
            query = parse_qs(parsed.query)
            goal_id = (query.get("id") or [""])[0]
            try:
                AGENT_RUNTIME.load(goal_id)
                limit = int((query.get("limit") or ["200"])[0])
                return self._json({"events": AGENT_RUNTIME.events(goal_id, limit)})
            except (ValueError, TypeError) as error:
                return self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        if parsed.path == "/api/inbox":
            raw_limit = (parse_qs(parsed.query).get("limit") or ["200"])[0]
            try:
                return self._json({"documents": FINANCE_INBOX.list(int(raw_limit)), "entities": _box_entities()})
            except (ValueError, TypeError) as error:
                return self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        if parsed.path == "/api/inbox-document":
            document_id = (parse_qs(parsed.query).get("id") or [""])[0]
            try:
                return self._json({
                    "document": FINANCE_INBOX.load(document_id),
                    "events": FINANCE_INBOX.events(document_id),
                })
            except ValueError as error:
                return self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        if parsed.path == "/api/receivables":
            try:
                query = parse_qs(parsed.query)
                entity_id, _ = _statutory_entity((query.get("entity_id") or [""])[0])
                datasets = _scoped_datasets(entity_id)
                as_of = (query.get("as_of") or [None])[0]
                return self._json(build_receivables_register(
                    datasets["settlements"], datasets["cash_allocations"], as_of,
                    datasets["master_records"], datasets["collection_actions"],
                ))
            except (BoxRuntimeError, ValueError) as error:
                return self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        if parsed.path == "/api/revenue-close":
            try:
                query = parse_qs(parsed.query)
                entity_id, _ = _statutory_entity((query.get("entity_id") or [""])[0])
                datasets = _scoped_datasets(entity_id)
                return self._json({
                    "entity_id": entity_id,
                    **revenue_close_payload(datasets["settlement_candidates"], datasets["settlements"]),
                })
            except (BoxRuntimeError, ValueError) as error:
                return self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        if parsed.path == "/api/payables":
            try:
                query = parse_qs(parsed.query)
                entity_id, _ = _statutory_entity((query.get("entity_id") or [""])[0])
                datasets = _scoped_datasets(entity_id)
                return self._json(build_payables_register(
                    datasets["purchases"], datasets["invoices"], datasets["cash_allocations"],
                ))
            except (BoxRuntimeError, ValueError) as error:
                return self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        if parsed.path == "/api/procurement-workflow":
            try:
                entity_id, _ = _statutory_entity(
                    (parse_qs(parsed.query).get("entity_id") or [""])[0]
                )
                datasets = LEDGER.load_all()
                return self._json(procurement_workflow_payload(
                    datasets["procurement_requests"], datasets["purchases"],
                    datasets["purchase_deliveries"], entity_id=entity_id,
                ))
            except (BoxRuntimeError, ValueError) as error:
                return self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        if parsed.path == "/api/payment-requests":
            try:
                entity_id, _ = _statutory_entity(
                    (parse_qs(parsed.query).get("entity_id") or [""])[0]
                )
                return self._json({"entity_id": entity_id, "requests": _scoped_datasets(entity_id)["payment_requests"]})
            except (BoxRuntimeError, ValueError) as error:
                return self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        if parsed.path == "/api/vendor-bank-accounts":
            try:
                query = parse_qs(parsed.query)
                entity_id, _ = _statutory_entity((query.get("entity_id") or [""])[0])
                vendor = (query.get("vendor") or [""])[0]
                currency = (query.get("currency") or [""])[0]
                records = _scoped_datasets(entity_id)["vendor_bank_changes"]
                return self._json({
                    "entity_id": entity_id,
                    "accounts": [public_vendor_bank_record(row) for row in approved_vendor_bank_accounts(
                        records, entity_id=entity_id, vendor=vendor, currency=currency,
                    )],
                    "pending": [public_vendor_bank_record(row) for row in records if row.get("status") in {"待批准", "阻塞"}],
                    "guardrail": "完整账号不写入台账；付款只绑定已批准账户的脱敏尾号与指纹。",
                })
            except (BoxRuntimeError, ValueError) as error:
                return self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        if parsed.path == "/api/expense-claims":
            try:
                entity_id, _ = _statutory_entity(
                    (parse_qs(parsed.query).get("entity_id") or [""])[0]
                )
                return self._json({"entity_id": entity_id, "claims": _scoped_datasets(entity_id)["expense_claims"]})
            except (BoxRuntimeError, ValueError) as error:
                return self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        if parsed.path == "/api/business-flows":
            query = parse_qs(parsed.query)
            as_of = (query.get("as_of") or [None])[0]
            entity_id = str((query.get("entity_id") or [""])[0]).strip()
            try:
                datasets = LEDGER.load_all()
                if entity_id:
                    datasets = {
                        name: [row for row in rows if str(row.get("entity_id") or "") == entity_id]
                        for name, rows in datasets.items()
                    }
                overview = build_flow_overview(datasets, as_of)
                overview["entity_id"] = entity_id
                overview["scope"] = "legal_entity" if entity_id else "management"
                return self._json(overview)
            except ValueError as error:
                return self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        if parsed.path == "/api/accounting-registers":
            datasets = LEDGER.load_all()
            return self._json({
                "asset_cards": datasets["asset_cards"], "accruals": datasets["accruals"],
                "posted_vouchers": datasets["posted_vouchers"],
                "game_revenue_policies": datasets["game_revenue_policies"],
            })
        if parsed.path == "/api/tax-delivery":
            query = parse_qs(parsed.query)
            period = (query.get("period") or [""])[0]
            entity_id = (query.get("entity_id") or [""])[0]
            try:
                _guard_cn_filing_entity(entity_id)
                finance = _finance_from_store(period, entity_id)
                return self._json(build_tax_delivery(
                    finance["tax_pack"]["returns_workspace"], LEDGER.load_dataset("tax_filing_reviews"),
                ))
            except ValueError as error:
                return self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        if parsed.path == "/api/tax-filing-assist":
            query = parse_qs(parsed.query)
            period = (query.get("period") or [""])[0]
            entity_id = (query.get("entity_id") or [""])[0]
            try:
                _guard_cn_filing_entity(entity_id)
                finance = _finance_from_store(period, entity_id)
                workspace = finance["tax_pack"]["returns_workspace"]
                return self._json(build_filing_assist(
                    workspace, LEDGER.load_dataset("tax_filing_reviews"),
                ))
            except ValueError as error:
                return self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        if parsed.path == "/api/onboarding":
            try:
                query = parse_qs(parsed.query)
                entity_id, _ = _statutory_entity((query.get("entity_id") or [""])[0])
                datasets = _scoped_datasets(entity_id)
                result = build_onboarding(_entity_profile(entity_id), datasets)
                result["template_available"] = True
                result["entity_id"] = entity_id
                period = str((query.get("period") or [latest_period(datasets)])[0])
                result["first_close"] = _first_close_readiness(entity_id, period)
                result["entities"] = _box_entities()
                return self._json(result)
            except (BoxRuntimeError, ValueError) as error:
                return self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        if parsed.path == "/api/bp":
            datasets, demo_mode = _analysis_datasets()
            query = parse_qs(parsed.query)
            result = build_bp_analysis(
                datasets, load_profile(COMPANY_PROFILE),
                (query.get("period") or [None])[0], (query.get("scenario") or ["基准"])[0],
            )
            result["demo_mode"] = demo_mode
            return self._json(result)
        if parsed.path == "/api/planning":
            query = parse_qs(parsed.query)
            try:
                entity_id, _ = _statutory_entity((query.get("entity_id") or [""])[0])
                datasets = _scoped_datasets(entity_id)
                result = build_planning_analysis(
                    datasets["plan_lines"], datasets["settlements"], datasets["purchases"],
                    datasets["bank_transactions"], datasets["payroll_rows"], _entity_profile(entity_id),
                    (query.get("period") or [None])[0], (query.get("scenario") or ["基准"])[0],
                    datasets["collection_actions"], datasets["cash_allocations"],
                )
                result["entity_id"] = entity_id
                return self._json(result)
            except (BoxRuntimeError, ValueError) as error:
                return self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        if parsed.path == "/api/accountant-pack":
            query = parse_qs(parsed.query)
            period = (query.get("period") or [""])[0]
            entity_id = (query.get("entity_id") or [""])[0]
            try:
                entity_id, entity = _statutory_entity(entity_id)
                finance = _finance_from_store(period, entity_id)
                period_state = LEDGER.load_period(period, entity_id)
                finance["period_state"] = period_state
                finance["close_assessment"] = assess_close(
                    finance, period_state,
                    profile_gaps(_entity_profile(entity_id)) if entity_id == "cn_studio" else [],
                )
                body = build_accountant_pack(
                    finance, period_state, LEDGER.audit_events(1000),
                    _scoped_datasets(entity_id), _entity_profile(entity_id),
                    entity=entity.to_dict(),
                )
                LEDGER.append_audit("财务工作台用户", "EXPORT_ACCOUNTANT_PACK", f"{entity_id}:{period}", {
                    "entity_id": entity_id, "period": period,
                    "reporting_basis": finance["posting"]["reporting_basis"],
                    "can_close": finance["close_assessment"]["can_close"],
                })
            except (BoxRuntimeError, ValueError) as error:
                return self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            return self._download(body, f"finance-review-{entity_id}-{period}.zip", "application/zip")
        if parsed.path == "/api/shadow-close":
            query = parse_qs(parsed.query)
            entity_id = str((query.get("entity_id") or [""])[0]).strip()
            period = str((query.get("period") or [""])[0]).strip()
            if not entity_id or not period:
                return self._json({"error": "请选择法律主体和期间"}, HTTPStatus.BAD_REQUEST)
            try:
                BOX_RUNTIME.require_entity(entity_id)
                baseline = next((row for row in LEDGER.load_dataset("shadow_close_baselines") if row.get("entity_id") == entity_id and row.get("period") == period), None)
                if not baseline:
                    return self._json({
                        "entity_id": entity_id, "period": period, "status": "未导入基准",
                        "baseline": None, "report": None,
                        "guardrail": "Shadow close 只读比较，不覆盖正式台账。",
                    })
                finance = _finance_from_store(period, entity_id)
                report = compare_shadow_close(
                    baseline,
                    finance,
                    LEDGER.load_dataset("shadow_close_reviews"),
                    runtime_fingerprint=BOX_RUNTIME.snapshot()["fingerprint"],
                )
                return self._json({"entity_id": entity_id, "period": period, "status": report["status"], "baseline": baseline, "report": report})
            except ValueError as error:
                return self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        if parsed.path == "/api/tax-return-workbook":
            query = parse_qs(parsed.query)
            period = (query.get("period") or [""])[0]
            entity_id = (query.get("entity_id") or [""])[0]
            try:
                _guard_cn_filing_entity(entity_id)
                finance = _finance_from_store(period, entity_id)
                workspace = finance["tax_pack"]["returns_workspace"]
                TAX_OUTPUTS.mkdir(parents=True, exist_ok=True)
                target = TAX_OUTPUTS / f"税务申报工作底稿-{period}.xlsx"
                build_tax_workbook(workspace, target)
                LEDGER.append_audit("财务工作台用户", "EXPORT_TAX_WORKBOOK", period, {
                    "form_count": workspace["summary"]["form_count"],
                    "direct_upload_ready": workspace["summary"]["direct_upload_ready"],
                })
                return self._download(target.read_bytes(), f"tax-return-workpaper-{period}.xlsx",
                                      "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            except (ValueError, RuntimeError) as error:
                return self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        if parsed.path == "/api/tax-filing-assist-package":
            query = parse_qs(parsed.query)
            period = (query.get("period") or [""])[0]
            entity_id = (query.get("entity_id") or [""])[0]
            try:
                _guard_cn_filing_entity(entity_id)
                finance = _finance_from_store(period, entity_id)
                workspace = finance["tax_pack"]["returns_workspace"]
                assist = build_filing_assist(workspace, LEDGER.load_dataset("tax_filing_reviews"))
                body = build_filing_assist_package(workspace, assist)
                LEDGER.append_audit("财务工作台用户", "EXPORT_TAX_FILING_ASSIST", period, {
                    "entity_id": assist.get("entity_id"), "form_count": assist["summary"]["form_count"],
                    "ready_for_release": assist["summary"]["ready_for_release"],
                    "direct_upload_ready": 0,
                })
                return self._download(body, f"tax-filing-assist-{period}.zip", "application/zip")
            except (ValueError, RuntimeError) as error:
                return self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        if parsed.path == "/api/finance-ops":
            datasets = LEDGER.load_all()
            query = parse_qs(parsed.query)
            entity_id = (query.get("entity_id") or [""])[0]
            if entity_id:
                try:
                    entity_id, _ = _statutory_entity(entity_id)
                except (BoxRuntimeError, ValueError) as error:
                    return self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
                datasets = {
                    name: [row for row in rows if str(row.get("entity_id") or "") == entity_id]
                    for name, rows in datasets.items()
                }
            records = datasets["settlements"]
            if not records:
                scenarios = load_demo_scenarios(DEMO_SCENARIOS)
                if scenarios.get("domestic"):
                    demo = build_demo_payload(scenarios["domestic"])["datasets"]
                    if entity_id:
                        demo = {
                            name: [row for row in rows if str(row.get("entity_id") or "") == entity_id]
                            for name, rows in demo.items()
                        }
                    records = demo["settlements"]
            requested = (query.get("period") or [""])[0]
            periods = sorted({record.get("period") for record in records if record.get("period")})
            period = requested or (periods[-1] if periods else datetime.now().strftime("%Y-%m"))
            return self._json(build_finance_ops(
                records, period, datasets["purchases"], datasets["bank_transactions"],
                datasets["invoices"], datasets["payroll_rows"],
                _entity_profile(entity_id) if entity_id else load_profile(COMPANY_PROFILE),
                datasets["opening_balances"],
                ledger_adapter_reviews=datasets["ledger_adapter_reviews"],
                bank_reconciliation_reviews=datasets["bank_reconciliation_reviews"],
            ))
        return super().do_GET()

    def do_HEAD(self):
        parsed = urlparse(self.path)
        if self._require_api_auth(parsed.path, "HEAD"):
            return
        if parsed.path in {"/api/onboarding-template", "/api/shadow-close-template"}:
            target = _ensure_onboarding_template() if parsed.path == "/api/onboarding-template" else _ensure_shadow_close_template()
            size = target.stat().st_size
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            self._security_headers()
            filename = "finance-workspace-onboarding.xlsx" if parsed.path == "/api/onboarding-template" else "finance-workspace-shadow-close.xlsx"
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Content-Length", str(size))
            self.end_headers()
            return
        return super().do_HEAD()

    def _multipart(self):
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            raise ValueError("请求不是文件上传格式")
        boundary_token = content_type.split("boundary=", 1)[-1].strip().strip('"')
        boundary = ("--" + boundary_token).encode()
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length > 120 * 1024 * 1024:
            raise OverflowError("单次上传上限为120MB")
        body = self.rfile.read(content_length)
        files, fields = [], {}
        for part in body.split(boundary):
            if b"Content-Disposition" not in part:
                continue
            header, _, data = part.partition(b"\r\n\r\n")
            if data.endswith(b"\r\n"):
                data = data[:-2]
            disposition = header.split(b"Content-Disposition:", 1)[-1].split(b"\r\n", 1)[0]
            name_match = disposition.split(b'name="', 1)
            name = name_match[1].split(b'"', 1)[0].decode("utf-8", "ignore") if len(name_match) > 1 else ""
            if b"filename=" in disposition:
                filename = disposition.split(b"filename=", 1)[1].strip().strip(b'"').decode("utf-8", "ignore")
                files.append({"field": name, "filename": Path(filename).name, "data": data})
            else:
                fields[name] = data.decode("utf-8", "ignore")
        return files, fields

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/webhooks/airwallex/spend":
            return self._receive_airwallex_webhook()
        if self._require_api_auth(parsed.path, "POST"):
            return
        if parsed.path in GAME_REFERENCE_POST_PATHS and not _game_reference_enabled():
            return self._json({
                "error": "the game Agent reference workflow is not enabled for this Box",
                "type": "workbench_profile_mismatch",
            }, HTTPStatus.CONFLICT)
        json_paths = {
            "/api/datasets", "/api/voucher-review", "/api/period-state", "/api/close-period",
            "/api/reopen-period",
            "/api/tax-review",
            "/api/purchase-acceptance", "/api/purchase-order", "/api/purchase-delivery",
            "/api/agent-goals", "/api/agent-refresh", "/api/agent-decision",
            "/api/agent-run",
            "/api/inbox-recognize", "/api/inbox-commit", "/api/inbox-entity",
            "/api/inbox-correct", "/api/inbox-link",
            "/api/cash-allocation", "/api/payment-request", "/api/payment-decision",
            "/api/collection-action", "/api/procurement-request", "/api/procurement-request-decision",
            "/api/vendor-bank-change", "/api/vendor-bank-change-decision",
            "/api/expense-claim", "/api/expense-decision",
            "/api/asset-card", "/api/asset-review", "/api/accrual", "/api/accrual-review",
            "/api/post-vouchers",
            "/api/ledger-adapter-review", "/api/bank-reconciliation-review",
            "/api/game-revenue-policy", "/api/game-revenue-policy-review",
            "/api/revenue-close-review",
            "/api/roll-forward",
            "/api/tax-form-review", "/api/tax-submission",
            "/api/shadow-close-review",
            "/api/onboarding-declaration",
            "/api/box/services/dispatch",
            "/api/box/pipelines/dispatch",
            "/api/box/pipelines/preflight",
            "/api/box-builder/preview",
            "/api/box-builder/bundle",
            "/api/box/pipeline-runs",
            "/api/box/pipeline-run-reviews",
            "/api/box/pipeline-schedule/run",
            "/api/finance-ops", "/api/qa", "/api/company-profile", "/api/templates",
        }
        raw_length = self.headers.get("Content-Length", "0")
        if parsed.path in json_paths:
            try:
                guarded_length = int(raw_length)
            except (TypeError, ValueError):
                return self._json({"error": "Content-Length 必须是非负整数", "type": "invalid_content_length"}, HTTPStatus.BAD_REQUEST)
            if guarded_length < 0:
                return self._json({"error": "Content-Length 必须是非负整数", "type": "invalid_content_length"}, HTTPStatus.BAD_REQUEST)
            if guarded_length > 28 * 1024 * 1024:
                return self._json({"error": "JSON 请求体不能超过 28 MiB", "type": "payload_too_large"}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            if parsed.path in {"/api/box-builder/preview", "/api/box-builder/bundle"} and guarded_length > 256 * 1024:
                return self._json(
                    {"error": "Box Builder 请求不能超过 256 KiB", "type": "payload_too_large"},
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                )
        if parsed.path in json_paths - {"/api/finance-ops", "/api/qa", "/api/company-profile", "/api/templates"}:
            content_length = guarded_length
            try:
                request = json.loads(self.rfile.read(content_length).decode("utf-8"))
                principal = getattr(self, "_api_principal", None)
                actor = (
                    principal.principal_id if principal
                    else str(request.get("actor") or "财务工作台用户")[:80]
                )
                if parsed.path == "/api/box/services/dispatch":
                    try:
                        return self._json(dispatch_box_service_request(BOX_RUNTIME, BOX_SERVICES, request))
                    except (BoxServiceRequestError, CfoMetricEvaluationError) as error:
                        return self._json({"error": str(error), "type": "invalid_service_request"}, HTTPStatus.BAD_REQUEST)
                    except PackServiceError as error:
                        return self._json({"error": str(error), "type": "service_forbidden"}, HTTPStatus.FORBIDDEN)
                    except BoxRuntimeError as error:
                        return self._json({"error": str(error), "type": "box_runtime_error"}, HTTPStatus.BAD_REQUEST)
                if parsed.path == "/api/box/pipelines/dispatch":
                    try:
                        return self._json(dispatch_box_pipeline_request(BOX_RUNTIME, request))
                    except BoxPipelineError as error:
                        return self._json({"error": str(error), "type": "invalid_pipeline_request"}, HTTPStatus.BAD_REQUEST)
                    except PackServiceError as error:
                        return self._json({"error": str(error), "type": "pipeline_forbidden"}, HTTPStatus.FORBIDDEN)
                    except BoxRuntimeError as error:
                        return self._json({"error": str(error), "type": "box_runtime_error"}, HTTPStatus.BAD_REQUEST)
                if parsed.path == "/api/box/pipelines/preflight":
                    try:
                        return self._json(preflight_pipeline_request(
                            BOX_RUNTIME, request, BOX_SERVICES,
                        ))
                    except (BoxRuntimeError, ValueError) as error:
                        return self._json(
                            {"error": str(error), "type": "invalid_pipeline_preflight"},
                            HTTPStatus.BAD_REQUEST,
                        )
                if parsed.path == "/api/box-builder/preview":
                    try:
                        return self._json(preview_box_candidate(
                            request, BOX_RUNTIME.packs_root,
                        ))
                    except (ValueError, OSError) as error:
                        return self._json(
                            {"error": str(error), "type": "invalid_box_candidate"},
                            HTTPStatus.BAD_REQUEST,
                        )
                if parsed.path == "/api/box-builder/bundle":
                    try:
                        body, filename, manifest = build_box_candidate_bundle(
                            request, BOX_RUNTIME.packs_root,
                        )
                        return self._download(
                            body,
                            filename,
                            "application/zip",
                            {
                                "X-OPC-Handoff-SHA256": hashlib.sha256(body).hexdigest(),
                                "X-OPC-Runtime-Fingerprint": str(
                                    manifest["runtime_fingerprint"]
                                ),
                                "X-OPC-Manifest-Schema": str(manifest["schema_version"]),
                                "X-OPC-Manifest-File-Count": str(manifest["file_count"]),
                            },
                        )
                    except (ValueError, OSError) as error:
                        return self._json(
                            {"error": str(error), "type": "invalid_box_bundle"},
                            HTTPStatus.BAD_REQUEST,
                        )
                if parsed.path == "/api/box/pipeline-runs":
                    pipeline_request = request.get("request")
                    if not isinstance(pipeline_request, dict):
                        return self._json(
                            {"error": "request must contain a Pipeline request object", "type": "invalid_pipeline_run_request"},
                            HTTPStatus.BAD_REQUEST,
                        )
                    try:
                        source_verification = None
                        if pipeline_request.get("pipeline_id") == "finance.multi_entity_month_close_portfolio":
                            source_verification = PIPELINE_RUNS.verify_month_close_portfolio_sources(
                                pipeline_request,
                                runtime_fingerprint=BOX_RUNTIME.snapshot()["fingerprint"],
                            )
                        result = dispatch_box_pipeline_request(BOX_RUNTIME, pipeline_request)
                        if source_verification is not None:
                            result["source_run_ledger_verified"] = True
                            result["source_run_ledger_verification"] = source_verification
                            result.setdefault("lineage", {})["source_run_ledger_verified"] = True
                            result["lineage"]["source_attempt_ids"] = [
                                item["attempt_id"] for item in source_verification["sources"]
                            ]
                        record = PIPELINE_RUNS.record(
                            BOX_RUNTIME.snapshot(), pipeline_request, result, actor=actor,
                        )
                        return self._json(
                            {"pipeline_result": result, "run_record": record}, HTTPStatus.CREATED,
                        )
                    except (BoxPipelineError, PipelineRunStoreError) as error:
                        return self._json({"error": str(error), "type": "invalid_pipeline_run_request"}, HTTPStatus.BAD_REQUEST)
                    except PackServiceError as error:
                        return self._json({"error": str(error), "type": "pipeline_forbidden"}, HTTPStatus.FORBIDDEN)
                    except BoxRuntimeError as error:
                        return self._json({"error": str(error), "type": "box_runtime_error"}, HTTPStatus.BAD_REQUEST)
                if parsed.path == "/api/box/pipeline-run-reviews":
                    try:
                        record = PIPELINE_RUNS.review(
                            request.get("attempt_id"),
                            runtime_fingerprint=BOX_RUNTIME.snapshot()["fingerprint"],
                            gate=request.get("gate"),
                            decision=request.get("decision"),
                            actor=actor,
                            rationale=request.get("rationale"),
                            evidence_references=request.get("evidence_references"),
                        )
                        return self._json({"run": record}, HTTPStatus.CREATED)
                    except PipelineRunStoreError as error:
                        return self._json(
                            {"error": str(error), "type": "invalid_pipeline_run_review"},
                            HTTPStatus.BAD_REQUEST,
                        )
                if parsed.path == "/api/box/pipeline-schedule/run":
                    if not PIPELINE_SCHEDULE_FILE:
                        return self._json({
                            "error": "pipeline schedule is not configured",
                            "type": "pipeline_schedule_not_configured",
                        }, HTTPStatus.CONFLICT)
                    unknown = set(request) - {"actor", "job_id"}
                    if unknown:
                        return self._json({
                            "error": "schedule run request contains unsupported fields",
                            "type": "invalid_pipeline_schedule_run",
                        }, HTTPStatus.BAD_REQUEST)
                    try:
                        return self._json(run_due_pipeline_schedule(
                            PIPELINE_SCHEDULE_FILE, BOX_RUNTIME, PIPELINE_RUNS,
                            actor=actor, job_id=request.get("job_id"),
                        ), HTTPStatus.CREATED)
                    except (PipelineScheduleError, PipelineRunStoreError) as error:
                        return self._json({
                            "error": str(error), "type": "invalid_pipeline_schedule_run",
                        }, HTTPStatus.BAD_REQUEST)
                if parsed.path == "/api/agent-goals":
                    period = str(request.get("period") or datetime.now().strftime("%Y-%m"))
                    objective = str(request.get("objective") or f"完成{period}整套财务工作")
                    data_mode = str(request.get("data_mode") or "live")
                    demo_scenario = str(request.get("demo_scenario") or "group")
                    goal = AGENT_RUNTIME.create(
                        objective, period, actor, data_mode=data_mode, demo_scenario=demo_scenario,
                    )
                    refreshed = _refresh_agent_goal(goal["id"], actor, "GOAL_PLANNED")
                    LEDGER.append_audit(actor, "CREATE_AGENT_GOAL", goal["id"], {
                        "objective": objective, "period": period,
                    })
                    return self._json({"goal": refreshed}, HTTPStatus.CREATED)
                if parsed.path == "/api/agent-run":
                    goal_id = str(request.get("goal_id") or "") or None
                    demo_scenario = str(request.get("demo_scenario") or "group")
                    seed = AGENT_RUNTIME.load(goal_id) if goal_id else _ensure_default_goal(demo_scenario)
                    automation_run = _run_safe_agent_automations(seed, actor)
                    workspace = _agent_workspace(
                        seed["id"], actor=actor, event_type="AGENT_RUN", demo_scenario=demo_scenario,
                    )
                    workspace["automation_run"] = automation_run
                    if workspace["data_mode"] == "live":
                        LEDGER.append_audit(actor, "RUN_FINANCE_AGENT", workspace["goal"]["id"], {
                            "period": workspace["goal"]["period"],
                            "data_mode": workspace["data_mode"],
                            "confirmation_count": workspace["confirmations"]["count"],
                            "blocked_count": workspace["confirmations"]["blocked_count"],
                        })
                    return self._json(workspace)
                if parsed.path == "/api/agent-refresh":
                    goal_id = str(request.get("goal_id") or "")
                    return self._json({"goal": _refresh_agent_goal(goal_id, actor, "AGENT_RUN")})
                if parsed.path == "/api/agent-decision":
                    goal_id = str(request.get("goal_id") or "")
                    action_id = str(request.get("action_id") or "")[:40]
                    decision = AGENT_RUNTIME.decide(
                        goal_id, action_id, str(request.get("decision") or ""), actor,
                        str(request.get("rationale") or ""), request.get("evidence") or [],
                    )
                    refreshed = _refresh_agent_goal(goal_id, actor, "GOAL_REPLANNED_AFTER_DECISION")
                    LEDGER.append_audit(actor, "AGENT_DECISION", f"{goal_id}/{action_id}", decision)
                    return self._json({"decision": decision, "goal": refreshed})
                if parsed.path == "/api/inbox-recognize":
                    document_id = str(request.get("document_id") or "")
                    document = FINANCE_INBOX.load(document_id)
                    document_type = str(
                        request.get("document_type")
                        or (document.get("classification") or {}).get("document_type")
                        or ""
                    )
                    period = str(request.get("period") or "")
                    requested_entity = str(request.get("entity_id") or "")
                    if requested_entity:
                        document = FINANCE_INBOX.assign_entity_scope(
                            document_id, requested_entity, actor, _box_entities(),
                            str(request.get("entity_note") or "识别前由用户确认主体"),
                        )
                    entity_id, entity_name = _confirmed_document_entity(document)
                    recognized = FINANCE_INBOX.recognize(
                        document_id, document_type, period, LEDGER.load_all(), actor,
                        entity_id, entity_name, _entity_profile(entity_id),
                    )
                    return self._json({"document": recognized})
                if parsed.path == "/api/inbox-entity":
                    document_id = str(request.get("document_id") or "")
                    entity_id = str(request.get("entity_id") or "")
                    document = FINANCE_INBOX.assign_entity_scope(
                        document_id, entity_id, actor, _box_entities(),
                        str(request.get("note") or "用户确认资料所属主体"),
                    )
                    return self._json({"document": document})
                if parsed.path == "/api/inbox-commit":
                    document_id = str(request.get("document_id") or "")
                    if request.get("confirmation") != "CONFIRM_IMPORT":
                        raise ValueError("写入正式台账前需明确确认 CONFIRM_IMPORT")
                    document = FINANCE_INBOX.load(document_id)
                    entity_id, _ = _confirmed_document_entity(document)
                    if document.get("status") == "已入台账":
                        return self._json({"document": document, "already_committed": True})
                    recognition = document.get("recognition") or {}
                    dataset = str(recognition.get("dataset") or "")
                    records = recognition.get("records") or []
                    batches = recognition.get("batches") or []
                    if not ((dataset and records) or batches):
                        raise ValueError("资料尚未解析出可写入台账的记录")
                    commit_blockers = FINANCE_INBOX.commit_blockers(document_id)
                    if commit_blockers:
                        raise ValueError("；".join(commit_blockers))
                    imports = ([{"dataset": dataset, "records": records}] if dataset and records else []) + [
                        {"dataset": str(batch.get("dataset") or ""), "records": batch.get("records") or []}
                        for batch in batches
                    ]
                    for item in imports:
                        for record in item["records"]:
                            record_entity = str(record.get("entity_id") or "")
                            if record_entity != entity_id:
                                raise ValueError("解析记录主体与资料确认主体不一致，已阻止入台账")
                            if record.get("source_document_id") != document_id:
                                raise ValueError("解析记录缺少当前原始资料证据链，已阻止入台账")
                    for item in imports:
                        _guard_open_periods(item["dataset"], item["records"])
                    saved_items = [
                        LEDGER.upsert_dataset(
                            item["dataset"], item["records"], actor,
                            f"统一收件箱：{document.get('original_filename')}",
                        )
                        for item in imports
                    ]
                    imported_datasets = [item["dataset"] for item in imports]
                    record_count = sum(len(item["records"]) for item in imports)
                    committed = FINANCE_INBOX.mark_committed(
                        document_id, actor, dataset or None, record_count, imported_datasets, entity_id,
                    )
                    LEDGER.append_audit(actor, "COMMIT_INBOX_DOCUMENT", document_id, {
                        "datasets": imported_datasets, "record_count": record_count,
                        "sha256": document.get("sha256"), "entity_id": entity_id,
                    })
                    return self._json({"document": committed, "saved": saved_items})
                if parsed.path == "/api/inbox-link":
                    document_id = str(request.get("document_id") or "")
                    target_type = str(request.get("target_type") or "")
                    target_id = str(request.get("target_id") or "")
                    entity_id = str(request.get("entity_id") or "")
                    target_dataset = {"purchase": "purchases", "settlement": "settlements"}.get(target_type)
                    if not target_dataset:
                        raise ValueError("当前只支持关联采购/验收或收入结算记录")
                    target = next(
                        (item for item in LEDGER.load_dataset(target_dataset) if item.get("id") == target_id), None,
                    )
                    if not target:
                        raise ValueError("找不到目标业务记录")
                    if str(target.get("entity_id") or "") != entity_id:
                        raise ValueError("业务记录与请求的法律主体不一致")
                    document = FINANCE_INBOX.link_to_business_record(
                        document_id, target_type=target_type, target_id=target_id,
                        entity_id=entity_id, actor=actor, note=str(request.get("note") or ""),
                    )
                    reference = f"document:{document_id}"
                    updated = dict(target)
                    evidence_field = "acceptance_evidence" if target_type == "purchase" else "reconciliation_evidence"
                    updated[evidence_field] = list(dict.fromkeys([
                        *(target.get(evidence_field) or []), reference,
                    ]))
                    updated["evidence_links"] = [
                        *[item for item in target.get("evidence_links") or [] if item.get("document_id") != document_id],
                        {
                            "document_id": document_id, "sha256": document.get("sha256"),
                            "filename": document.get("original_filename"), "linked_by": actor,
                            "linked_at": (document.get("business_links") or [])[-1].get("linked_at"),
                        },
                    ]
                    LEDGER.upsert_dataset(target_dataset, [updated], actor, "业务证据关联")
                    LEDGER.append_audit(actor, "LINK_INBOX_DOCUMENT", f"{document_id}/{target_id}", {
                        "entity_id": entity_id, "document_sha256": document.get("sha256"),
                    })
                    return self._json({"document": document, "target": updated, target_type: updated})
                if parsed.path == "/api/inbox-correct":
                    document = FINANCE_INBOX.correct(
                        str(request.get("document_id") or ""), request.get("patches") or [], actor,
                        bool(request.get("confirmed_against_original")), str(request.get("note") or ""),
                    )
                    return self._json({"document": document})
                if parsed.path == "/api/cash-allocation":
                    idempotency_key = _idempotency_key(
                        request, required=getattr(self, "_api_principal", None) is not None,
                    )
                    entity_id, _ = _statutory_entity(request.get("entity_id"))
                    if replay := _idempotent_replay("cash_allocations", idempotency_key, entity_id):
                        return self._json({"allocation": replay, "idempotent_replay": True})
                    transaction_id = str(request.get("transaction_id") or "")
                    target_type = str(request.get("target_type") or "")
                    target_id = str(request.get("target_id") or "")
                    datasets = _scoped_datasets(entity_id)
                    transaction = next(
                        (item for item in datasets["bank_transactions"] if item.get("id") == transaction_id), None
                    )
                    if not transaction:
                        raise ValueError("找不到银行流水")
                    if target_type == "receivable":
                        target = next((item for item in datasets["settlements"] if item.get("id") == target_id), None)
                    elif target_type == "payable":
                        target = next((item for item in build_payables_register(
                            datasets["purchases"], datasets["invoices"], datasets["cash_allocations"],
                        )["rows"] if item.get("id") == target_id), None)
                    elif target_type == "expense":
                        target = next((item for item in datasets["expense_claims"] if item.get("id") == target_id), None)
                    elif target_type == "payroll":
                        target = next((item for item in build_payroll_payables(
                            datasets["payroll_rows"], datasets["cash_allocations"],
                        )["rows"] if item.get("id") == target_id), None)
                    else:
                        target = None
                    if not target:
                        raise ValueError("找不到核销目标")
                    authorization_reference = str(request.get("authorization_reference") or "").strip()
                    authorization = next(
                        (item for item in datasets["payment_requests"] if item.get("id") == authorization_reference),
                        None,
                    ) if authorization_reference else None
                    allocation = create_cash_allocation(
                        transaction, target_type, target, request.get("amount"),
                        datasets["cash_allocations"], actor,
                        note=str(request.get("note") or ""),
                        difference_reason=str(request.get("difference_reason") or ""),
                        authorization_reference=authorization_reference, authorization=authorization,
                    )
                    allocation["period"] = str(transaction.get("transaction_date") or "")[:7]
                    allocation["idempotency_key"] = idempotency_key
                    LEDGER.upsert_dataset("cash_allocations", [allocation], actor, "资金核销")
                    LEDGER.append_audit(actor, "CREATE_CASH_ALLOCATION", allocation["id"], {
                        "transaction_id": transaction_id, "target_type": target_type,
                        "target_id": target_id, "entity_id": entity_id,
                        "amount": allocation["amount"], "currency": allocation["currency"],
                    })
                    return self._json({"allocation": allocation, "idempotent_replay": False}, HTTPStatus.CREATED)
                if parsed.path == "/api/collection-action":
                    idempotency_key = _idempotency_key(
                        request, required=getattr(self, "_api_principal", None) is not None,
                    )
                    entity_id, _ = _statutory_entity(request.get("entity_id"))
                    if replay := _idempotent_replay("collection_actions", idempotency_key, entity_id):
                        return self._json({"action": replay, "idempotent_replay": True})
                    datasets = _scoped_datasets(entity_id)
                    receivables = build_receivables_register(
                        datasets["settlements"], datasets["cash_allocations"],
                        str(request.get("as_of") or datetime.now().date().isoformat()),
                        datasets["master_records"], datasets["collection_actions"],
                    )["rows"]
                    settlement_id = str(request.get("settlement_id") or "")
                    receivable = next((row for row in receivables if row.get("id") == settlement_id), None)
                    if not receivable:
                        raise ValueError("找不到该主体的应收记录")
                    action = create_collection_action(
                        receivable, str(request.get("action_type") or ""),
                        str(request.get("owner") or ""), actor,
                        action_date=str(request.get("action_date") or ""),
                        note=str(request.get("note") or ""),
                        promised_date=str(request.get("promised_date") or ""),
                        promised_amount=request.get("promised_amount"),
                        dispute_reason=str(request.get("dispute_reason") or ""),
                        existing_actions=datasets["collection_actions"],
                    )
                    action["idempotency_key"] = idempotency_key
                    LEDGER.upsert_dataset("collection_actions", [action], actor, "应收催收记录")
                    LEDGER.append_audit(actor, "CREATE_COLLECTION_ACTION", action["id"], {
                        "entity_id": entity_id, "settlement_id": settlement_id,
                        "action_type": action["action_type"], "promised_date": action["promised_date"],
                        "promised_amount": action["promised_amount"], "currency": action["currency"],
                    })
                    return self._json({"action": action, "idempotent_replay": False}, HTTPStatus.CREATED)
                if parsed.path == "/api/procurement-request":
                    idempotency_key = _idempotency_key(
                        request, required=getattr(self, "_api_principal", None) is not None,
                    )
                    entity_id, _ = _statutory_entity(request.get("entity_id"))
                    if replay := _idempotent_replay("procurement_requests", idempotency_key, entity_id):
                        return self._json({"request": replay, "idempotent_replay": True})
                    datasets = _scoped_datasets(entity_id)
                    period = str(request.get("period") or "")
                    snapshot = procurement_budget_snapshot(
                        datasets["plan_lines"], datasets["procurement_requests"], entity_id=entity_id,
                        project=str(request.get("project") or ""), category=str(request.get("category") or ""),
                        period=period, currency=str(request.get("currency") or "CNY"),
                    )
                    item = create_procurement_request(
                        entity_id=entity_id, project=str(request.get("project") or ""),
                        category=str(request.get("category") or ""), description=str(request.get("description") or ""),
                        amount=request.get("amount"), currency=str(request.get("currency") or "CNY"),
                        period=period, needed_by=str(request.get("needed_by") or ""), requester=actor,
                        sourcing_method=str(request.get("sourcing_method") or ""),
                        selected_vendor=str(request.get("selected_vendor") or ""),
                        quotes=request.get("quotes") or [], evidence=request.get("evidence") or [],
                        budget_snapshot=snapshot, selection_rationale=str(request.get("selection_rationale") or ""),
                        sourcing_exception_reason=str(request.get("sourcing_exception_reason") or ""),
                        budget_exception_reason=str(request.get("budget_exception_reason") or ""),
                        framework_reference=str(request.get("framework_reference") or ""),
                    )
                    item["idempotency_key"] = idempotency_key
                    LEDGER.upsert_dataset("procurement_requests", [item], actor, "采购申请")
                    LEDGER.append_audit(actor, "CREATE_PROCUREMENT_REQUEST", item["id"], {
                        "entity_id": entity_id, "period": period, "amount": item["amount"],
                        "currency": item["currency"], "status": item["status"],
                        "sourcing_method": item["sourcing_method"],
                    })
                    return self._json({"request": item, "idempotent_replay": False}, HTTPStatus.CREATED)
                if parsed.path == "/api/procurement-request-decision":
                    request_id = str(request.get("request_id") or "")
                    records = LEDGER.load_dataset("procurement_requests")
                    index = next((i for i, row in enumerate(records) if row.get("id") == request_id), None)
                    if index is None:
                        raise ValueError("找不到采购申请")
                    entity_id, _ = _statutory_entity(request.get("entity_id") or records[index].get("entity_id"))
                    if str(records[index].get("entity_id") or "") != entity_id:
                        raise ValueError("采购申请不属于所选法律主体")
                    updated = decide_procurement_request(
                        records[index], str(request.get("decision") or ""), actor,
                        str(request.get("rationale") or ""),
                    )
                    records[index] = updated
                    LEDGER.save_dataset("procurement_requests", records, actor, "采购申请审批")
                    LEDGER.append_audit(actor, "PROCUREMENT_REQUEST_DECISION", request_id, {
                        "entity_id": entity_id, "decision": request.get("decision"), "status": updated["status"],
                    })
                    return self._json({"request": updated})
                if parsed.path == "/api/purchase-order":
                    idempotency_key = _idempotency_key(
                        request, required=getattr(self, "_api_principal", None) is not None,
                    )
                    entity_id, _ = _statutory_entity(request.get("entity_id"))
                    if replay := _idempotent_replay("purchases", idempotency_key, entity_id):
                        return self._json({"order": replay, "idempotent_replay": True})
                    requests = LEDGER.load_dataset("procurement_requests")
                    request_id = str(request.get("request_id") or "")
                    index = next((i for i, row in enumerate(requests) if row.get("id") == request_id), None)
                    if index is None or str(requests[index].get("entity_id") or "") != entity_id:
                        raise ValueError("找不到该主体的采购申请")
                    purchases = LEDGER.load_dataset("purchases")
                    if any(row.get("po_number") == request.get("po_number") and row.get("entity_id") == entity_id for row in purchases):
                        raise ValueError("该主体的 PO 编号已存在")
                    updated_request, order = create_purchase_order_from_request(
                        requests[index], po_number=str(request.get("po_number") or ""),
                        order_date=str(request.get("order_date") or ""), actor=actor,
                        milestones=request.get("milestones") or [], evidence=request.get("evidence") or [],
                        item=str(request.get("item") or ""),
                    )
                    order["idempotency_key"] = idempotency_key
                    requests[index] = updated_request
                    LEDGER.save_dataset("procurement_requests", requests, actor, "采购申请转订单")
                    LEDGER.upsert_dataset("purchases", [order], actor, "采购订单")
                    LEDGER.append_audit(actor, "CREATE_PURCHASE_ORDER", order["id"], {
                        "entity_id": entity_id, "procurement_request_id": request_id,
                        "po_number": order["po_number"], "amount": order["ordered_amount"],
                        "currency": order["currency"], "milestone_count": len(order["milestones"]),
                    })
                    return self._json({"request": updated_request, "order": order, "idempotent_replay": False}, HTTPStatus.CREATED)
                if parsed.path == "/api/purchase-delivery":
                    idempotency_key = _idempotency_key(
                        request, required=getattr(self, "_api_principal", None) is not None,
                    )
                    entity_id, _ = _statutory_entity(request.get("entity_id"))
                    if replay := _idempotent_replay("purchase_deliveries", idempotency_key, entity_id):
                        return self._json({"delivery": replay, "idempotent_replay": True})
                    purchases = LEDGER.load_dataset("purchases")
                    purchase = next((row for row in purchases if row.get("id") == request.get("purchase_id")), None)
                    if not purchase or str(purchase.get("entity_id") or "") != entity_id:
                        raise ValueError("找不到该主体的采购订单")
                    deliveries = LEDGER.load_dataset("purchase_deliveries")
                    delivery = record_purchase_delivery(
                        purchase, milestone_id=str(request.get("milestone_id") or ""),
                        delivered_amount=request.get("delivered_amount"),
                        delivery_date=str(request.get("delivery_date") or ""), delivered_by=actor,
                        evidence=request.get("evidence") or [], note=str(request.get("note") or ""),
                        existing_deliveries=deliveries,
                    )
                    delivery["idempotency_key"] = idempotency_key
                    LEDGER.upsert_dataset("purchase_deliveries", [delivery], actor, "采购里程碑交付")
                    LEDGER.append_audit(actor, "RECORD_PURCHASE_DELIVERY", delivery["id"], {
                        "entity_id": entity_id, "purchase_id": purchase["id"],
                        "milestone_id": delivery["milestone_id"], "amount": delivery["delivered_amount"],
                    })
                    return self._json({"delivery": delivery, "idempotent_replay": False}, HTTPStatus.CREATED)
                if parsed.path == "/api/vendor-bank-change":
                    idempotency_key = _idempotency_key(
                        request, required=getattr(self, "_api_principal", None) is not None,
                    )
                    entity_id, _ = _statutory_entity(request.get("entity_id"))
                    if replay := _idempotent_replay("vendor_bank_changes", idempotency_key, entity_id):
                        return self._json({"change": public_vendor_bank_record(replay), "idempotent_replay": True})
                    records = LEDGER.load_dataset("vendor_bank_changes")
                    item = create_vendor_bank_change(
                        entity_id=entity_id, vendor=str(request.get("vendor") or ""),
                        beneficiary_name=str(request.get("beneficiary_name") or ""),
                        bank_name=str(request.get("bank_name") or ""),
                        bank_country=str(request.get("bank_country") or ""),
                        currency=str(request.get("currency") or ""),
                        account_number=str(request.get("account_number") or ""), requester=actor,
                        evidence=request.get("evidence") or [], change_type=str(request.get("change_type") or "新增"),
                        previous_account_id=str(request.get("previous_account_id") or ""), existing_records=records,
                    )
                    item["swift_bic"] = str(request.get("swift_bic") or "").strip().upper()[:20]
                    item["idempotency_key"] = idempotency_key
                    LEDGER.upsert_dataset("vendor_bank_changes", [item], actor, "供应商收款账户申请")
                    LEDGER.append_audit(actor, "CREATE_VENDOR_BANK_CHANGE", item["id"], {
                        "entity_id": entity_id, "vendor": item["vendor"], "currency": item["currency"],
                        "account_masked": item["account_masked"], "status": item["status"],
                    })
                    return self._json({"change": public_vendor_bank_record(item), "idempotent_replay": False}, HTTPStatus.CREATED)
                if parsed.path == "/api/vendor-bank-change-decision":
                    entity_id, _ = _statutory_entity(request.get("entity_id"))
                    records = LEDGER.load_dataset("vendor_bank_changes")
                    target = next((row for row in records if row.get("id") == request.get("change_id")), None)
                    if not target or str(target.get("entity_id") or "") != entity_id:
                        raise ValueError("找不到该主体的供应商账户申请")
                    records, item = decide_vendor_bank_change(
                        records, str(request.get("change_id") or ""), str(request.get("decision") or ""), actor,
                        str(request.get("rationale") or ""), str(request.get("verification_method") or ""),
                        str(request.get("verification_reference") or ""),
                    )
                    LEDGER.save_dataset("vendor_bank_changes", records, actor, "供应商收款账户独立复核")
                    LEDGER.append_audit(actor, "VENDOR_BANK_CHANGE_DECISION", item["id"], {
                        "entity_id": entity_id, "decision": request.get("decision"),
                        "verification_method": (item.get("review") or {}).get("verification_method"),
                        "account_masked": item.get("account_masked"),
                    })
                    return self._json({"change": public_vendor_bank_record(item)})
                if parsed.path == "/api/payment-request":
                    idempotency_key = _idempotency_key(
                        request, required=getattr(self, "_api_principal", None) is not None,
                    )
                    entity_id, _ = _statutory_entity(request.get("entity_id"))
                    if replay := _idempotent_replay("payment_requests", idempotency_key, entity_id):
                        return self._json({"request": replay, "idempotent_replay": True})
                    target_type = str(request.get("target_type") or "")
                    target_id = str(request.get("target_id") or "")
                    datasets = _scoped_datasets(entity_id)
                    if target_type == "payable":
                        target = next((item for item in build_payables_register(
                            datasets["purchases"], datasets["invoices"], datasets["cash_allocations"],
                        )["rows"] if item.get("id") == target_id), None)
                    elif target_type == "expense":
                        target = next((item for item in datasets["expense_claims"] if item.get("id") == target_id), None)
                    elif target_type == "payroll":
                        target = next((item for item in build_payroll_payables(
                            datasets["payroll_rows"], datasets["cash_allocations"],
                        )["rows"] if item.get("id") == target_id), None)
                    else:
                        target = None
                    if not target:
                        raise ValueError("找不到付款申请目标")
                    payment = create_payment_request(
                        target_type, target, request.get("amount"), actor,
                        purpose=str(request.get("purpose") or ""), evidence=request.get("evidence") or [],
                        prepayment=bool(request.get("prepayment")), existing_requests=datasets["payment_requests"],
                        vendor_bank_accounts=datasets["vendor_bank_changes"],
                        bank_account_id=str(request.get("bank_account_id") or ""),
                        require_approved_vendor_account=(target_type == "payable" and bool(target.get("vendor"))),
                    )
                    payment["period"] = str(request.get("period") or datetime.now().strftime("%Y-%m"))
                    payment["idempotency_key"] = idempotency_key
                    LEDGER.upsert_dataset("payment_requests", [payment], actor, "付款申请")
                    LEDGER.append_audit(actor, "CREATE_PAYMENT_REQUEST", payment["id"], {
                        "target_type": target_type, "target_id": target_id, "entity_id": entity_id,
                        "amount": payment["amount"], "currency": payment["currency"], "status": payment["status"],
                    })
                    return self._json({"request": payment, "idempotent_replay": False}, HTTPStatus.CREATED)
                if parsed.path == "/api/payment-decision":
                    request_id = str(request.get("request_id") or "")
                    requests = LEDGER.load_dataset("payment_requests")
                    index = next((i for i, item in enumerate(requests) if item.get("id") == request_id), None)
                    if index is None:
                        raise ValueError("找不到付款申请")
                    entity_id, _ = _statutory_entity(
                        request.get("entity_id") or requests[index].get("entity_id")
                    )
                    if str(requests[index].get("entity_id") or "") != entity_id:
                        raise ValueError("付款申请不属于所选法律主体")
                    updated = decide_payment_request(
                        requests[index], str(request.get("decision") or ""), actor,
                        str(request.get("rationale") or ""),
                        _scoped_datasets(entity_id)["vendor_bank_changes"],
                    )
                    requests[index] = updated
                    LEDGER.save_dataset("payment_requests", requests, actor, "付款审批")
                    LEDGER.append_audit(actor, "PAYMENT_DECISION", request_id, {
                        **updated["approval"], "entity_id": entity_id,
                    })
                    return self._json({"request": updated})
                if parsed.path == "/api/expense-claim":
                    idempotency_key = _idempotency_key(
                        request, required=getattr(self, "_api_principal", None) is not None,
                    )
                    entity_id = str(request.get("entity_id") or "").strip()
                    if not entity_id:
                        raise ValueError("费用报销必须选择所属法律主体")
                    BOX_RUNTIME.require_entity(entity_id)
                    if replay := _idempotent_replay("expense_claims", idempotency_key, entity_id):
                        return self._json({"claim": replay, "idempotent_replay": True})
                    claim = create_expense_claim(
                        str(request.get("claimant") or ""), str(request.get("claim_date") or ""),
                        request.get("amount"), str(request.get("currency") or "CNY"),
                        str(request.get("project") or ""), str(request.get("category") or ""),
                        str(request.get("purpose") or ""), request.get("evidence") or [], actor,
                        entity_id,
                    )
                    claim["idempotency_key"] = idempotency_key
                    LEDGER.upsert_dataset("expense_claims", [claim], actor, "费用报销提交")
                    LEDGER.append_audit(actor, "CREATE_EXPENSE_CLAIM", claim["id"], {
                        "amount": claim["amount"], "currency": claim["currency"], "project": claim["project"],
                    })
                    return self._json({"claim": claim, "idempotent_replay": False}, HTTPStatus.CREATED)
                if parsed.path == "/api/shadow-close-review":
                    entity_id = str(request.get("entity_id") or "").strip()
                    period = str(request.get("period") or "").strip()
                    BOX_RUNTIME.require_entity(entity_id)
                    baseline = next((row for row in LEDGER.load_dataset("shadow_close_baselines") if row.get("entity_id") == entity_id and row.get("period") == period), None)
                    if not baseline:
                        raise ValueError("找不到该主体和期间的 shadow close 基准")
                    report = compare_shadow_close(
                        baseline,
                        _finance_from_store(period, entity_id),
                        LEDGER.load_dataset("shadow_close_reviews"),
                        runtime_fingerprint=BOX_RUNTIME.snapshot()["fingerprint"],
                    )
                    review = review_shadow_close(
                        report, str(request.get("decision") or ""), actor,
                        str(request.get("rationale") or ""), request.get("evidence") or [],
                        request.get("exception_resolutions") or [],
                    )
                    LEDGER.upsert_dataset("shadow_close_reviews", [review], actor, "Shadow close 独立签认")
                    LEDGER.append_audit(actor, "REVIEW_SHADOW_CLOSE", baseline["id"], {
                        "entity_id": entity_id, "period": period, "decision": review["decision"],
                        "report_fingerprint": review["report_fingerprint"],
                    })
                    return self._json({
                        "review": review,
                        "report": compare_shadow_close(
                            baseline,
                            _finance_from_store(period, entity_id),
                            LEDGER.load_dataset("shadow_close_reviews"),
                            runtime_fingerprint=BOX_RUNTIME.snapshot()["fingerprint"],
                        ),
                    })
                if parsed.path == "/api/onboarding-declaration":
                    entity_id = str(request.get("entity_id") or "").strip()
                    period = str(request.get("period") or "").strip()
                    BOX_RUNTIME.require_entity(entity_id)
                    declaration = make_not_applicable_declaration(
                        entity_id=entity_id, period=period,
                        domain=str(request.get("domain") or ""),
                        decision=str(request.get("decision") or ""), actor=actor,
                        rationale=str(request.get("rationale") or ""),
                        evidence=request.get("evidence") or [],
                        now=datetime.now(timezone.utc).isoformat(),
                    )
                    LEDGER.upsert_dataset(
                        "onboarding_declarations", [declaration], actor,
                        "首月上线不适用声明",
                    )
                    LEDGER.append_audit(actor, "ONBOARDING_DECLARATION", declaration["id"], {
                        "entity_id": entity_id, "period": period,
                        "domain": declaration["domain"], "decision": declaration["decision"],
                    })
                    return self._json({
                        "declaration": declaration,
                        "first_close": _first_close_readiness(entity_id, period),
                    })
                if parsed.path == "/api/expense-decision":
                    claim_id = str(request.get("claim_id") or "")
                    claims = LEDGER.load_dataset("expense_claims")
                    index = next((i for i, item in enumerate(claims) if item.get("id") == claim_id), None)
                    if index is None:
                        raise ValueError("找不到报销单")
                    entity_id, _ = _statutory_entity(
                        request.get("entity_id") or claims[index].get("entity_id")
                    )
                    if str(claims[index].get("entity_id") or "") != entity_id:
                        raise ValueError("报销单不属于所选法律主体")
                    updated = decide_expense_claim(
                        claims[index], str(request.get("decision") or ""), actor,
                        str(request.get("rationale") or ""), request.get("approved_amount"),
                    )
                    claims[index] = updated
                    LEDGER.save_dataset("expense_claims", claims, actor, "费用报销审批")
                    LEDGER.append_audit(actor, "EXPENSE_DECISION", claim_id, updated["approval_history"][-1])
                    return self._json({"claim": updated})
                if parsed.path == "/api/asset-card":
                    idempotency_key = _idempotency_key(
                        request, required=getattr(self, "_api_principal", None) is not None,
                    )
                    entity_id, _ = _statutory_entity(request.get("entity_id"))
                    if replay := _idempotent_replay("asset_cards", idempotency_key, entity_id):
                        return self._json({"asset_card": replay, "idempotent_replay": True})
                    profile = _entity_profile(entity_id)
                    adapter = get_ledger_adapter(profile)
                    if LEDGER.load_period(str(request.get("acquisition_date") or "")[:7], entity_id).get("status") == "已关账":
                        raise ValueError("取得期间已关账，请在开放期间通过调整凭证处理")
                    card = create_asset_card(
                        str(request.get("name") or ""), str(request.get("asset_type") or ""),
                        str(request.get("acquisition_date") or ""), request.get("original_cost"),
                        request.get("useful_months"), request.get("residual_value") or 0,
                        str(request.get("project") or ""), str(request.get("vendor") or ""),
                        request.get("evidence") or [], actor,
                        currency=str(request.get("currency") or adapter.functional_currency),
                        cny_cost=request.get("cny_cost"),
                        functional_currency=adapter.functional_currency,
                        functional_cost=request.get("functional_cost"), fx_rate=request.get("fx_rate"),
                        ledger_adapter_id=adapter.id,
                    )
                    card["entity_id"] = entity_id
                    card["idempotency_key"] = idempotency_key
                    LEDGER.upsert_dataset("asset_cards", [card], actor, "资产卡片")
                    LEDGER.append_audit(actor, "CREATE_ASSET_CARD", card["id"], {
                        "entity_id": entity_id, "asset_type": card["asset_type"],
                        "functional_currency": card["functional_currency"],
                        "functional_cost": card["functional_cost"],
                        "useful_months": card["useful_months"], "status": card["status"],
                    })
                    return self._json({"asset_card": card, "idempotent_replay": False}, HTTPStatus.CREATED)
                if parsed.path in {"/api/asset-review", "/api/accrual-review"}:
                    dataset = "asset_cards" if parsed.path == "/api/asset-review" else "accruals"
                    item_id = str(request.get("item_id") or "")
                    items = LEDGER.load_dataset(dataset)
                    index = next((i for i, item in enumerate(items) if item.get("id") == item_id), None)
                    if index is None:
                        raise ValueError("找不到待复核事项")
                    entity_id, _ = _statutory_entity(
                        request.get("entity_id") or items[index].get("entity_id")
                    )
                    if str(items[index].get("entity_id") or "") != entity_id:
                        raise ValueError("会计事项不属于所选法律主体")
                    if (
                        str(request.get("decision") or "") == "批准"
                        and actor == str(items[index].get("submitted_by") or "")
                    ):
                        raise ValueError("会计事项提交人不能批准自己提交的事项")
                    updated = review_accounting_item(
                        items[index], str(request.get("decision") or ""), actor,
                        str(request.get("rationale") or ""),
                    )
                    items[index] = updated
                    LEDGER.save_dataset(dataset, items, actor, "会计事项复核")
                    LEDGER.append_audit(actor, "ACCOUNTING_ITEM_REVIEW", item_id, updated["review"])
                    return self._json({"item": updated})
                if parsed.path == "/api/accrual":
                    idempotency_key = _idempotency_key(
                        request, required=getattr(self, "_api_principal", None) is not None,
                    )
                    entity_id, _ = _statutory_entity(request.get("entity_id"))
                    if replay := _idempotent_replay("accruals", idempotency_key, entity_id):
                        return self._json({"accrual": replay, "idempotent_replay": True})
                    profile = _entity_profile(entity_id)
                    adapter = get_ledger_adapter(profile)
                    if LEDGER.load_period(str(request.get("period") or ""), entity_id).get("status") == "已关账":
                        raise ValueError("该期间已关账，不能新增暂估")
                    accrual = create_accrual(
                        str(request.get("period") or ""), str(request.get("description") or ""),
                        request.get("amount"), str(request.get("expense_account") or ""),
                        str(request.get("counterparty") or ""), str(request.get("project") or ""),
                        request.get("evidence") or [], actor, source_id=str(request.get("source_id") or ""),
                        auto_reverse=bool(request.get("auto_reverse", True)),
                        currency=str(request.get("currency") or adapter.functional_currency),
                        functional_currency=adapter.functional_currency,
                        functional_amount=request.get("functional_amount"), fx_rate=request.get("fx_rate"),
                        expense_role=str(request.get("expense_role") or "operating_expense"),
                        ledger_adapter_id=adapter.id,
                    )
                    accrual["entity_id"] = entity_id
                    accrual["idempotency_key"] = idempotency_key
                    LEDGER.upsert_dataset("accruals", [accrual], actor, "费用暂估")
                    LEDGER.append_audit(actor, "CREATE_ACCRUAL", accrual["id"], {
                        "entity_id": entity_id, "period": accrual["period"], "amount": accrual["amount"],
                        "functional_currency": accrual["functional_currency"],
                        "functional_amount": accrual["functional_amount"],
                        "auto_reverse": accrual["auto_reverse"], "status": accrual["status"],
                    })
                    return self._json({"accrual": accrual, "idempotent_replay": False}, HTTPStatus.CREATED)
                if parsed.path == "/api/ledger-adapter-review":
                    entity_id, _ = _statutory_entity(request.get("entity_id"))
                    profile = _entity_profile(entity_id)
                    adapter = get_ledger_adapter(profile)
                    review = create_adapter_review(
                        adapter, entity_id, str(request.get("decision") or ""), actor,
                        str(request.get("rationale") or ""), request.get("evidence") or [],
                    )
                    LEDGER.upsert_dataset("ledger_adapter_reviews", [review], actor, "总账适配器复核")
                    LEDGER.append_audit(actor, "LEDGER_ADAPTER_REVIEW", review["id"], {
                        "entity_id": entity_id, "adapter_id": adapter.id,
                        "adapter_fingerprint": adapter.fingerprint, "decision": review["decision"],
                    })
                    return self._json({"review": review, "ledger_adapter": adapter.public_payload([review])}, HTTPStatus.CREATED)
                if parsed.path == "/api/bank-reconciliation-review":
                    entity_id, _ = _statutory_entity(request.get("entity_id"))
                    period = str(request.get("period") or "")
                    if LEDGER.load_period(period, entity_id).get("status") == "已关账":
                        raise ValueError("该期间已关账，不能修改银行余额调节")
                    transactions = [
                        row for row in LEDGER.load_dataset("bank_transactions")
                        if str(row.get("entity_id") or "") == entity_id
                    ]
                    review = create_bank_reconciliation_review(
                        transactions, period, str(request.get("account_masked") or ""),
                        str(request.get("currency") or ""), request.get("ledger_ending_balance"),
                        request.get("deposits_in_transit"), request.get("outstanding_payments"),
                        request.get("bank_adjustments"), request.get("ledger_adjustments"),
                        str(request.get("decision") or "确认"), actor,
                        str(request.get("rationale") or ""), request.get("evidence") or [],
                    )
                    review["entity_id"] = entity_id
                    LEDGER.upsert_dataset("bank_reconciliation_reviews", [review], actor, "银行余额调节复核")
                    LEDGER.append_audit(actor, "BANK_RECONCILIATION_REVIEW", review["id"], {
                        "entity_id": entity_id, "period": period,
                        "account_masked": review["account_masked"], "currency": review["currency"],
                        "difference": review["difference"], "decision": review["decision"],
                    })
                    return self._json({"review": review}, HTTPStatus.CREATED)
                if parsed.path == "/api/post-vouchers":
                    period = str(request.get("period") or "")
                    entity_id, _ = _statutory_entity(request.get("entity_id"))
                    if LEDGER.load_period(period, entity_id).get("status") == "已关账":
                        raise ValueError("该期间已关账，不能继续过账")
                    finance = _finance_from_store(period, entity_id)
                    if not finance["ledger_adapter"]["posting_ready"]:
                        raise ValueError(
                            f"{entity_id} 的 {finance['ledger_adapter']['id']} 科目映射尚未经当地会计批准；"
                            "当前只能生成本位币工作底稿，不能正式过账。"
                        )
                    state = LEDGER.load_period(period, entity_id)
                    result = post_reviewed_vouchers(
                        finance["vouchers"], state.get("voucher_reviews") or {},
                        LEDGER.load_dataset("posted_vouchers"), period, actor, entity_id,
                    )
                    LEDGER.save_dataset("posted_vouchers", result["records"], actor, "凭证过账")
                    LEDGER.append_audit(actor, "POST_VOUCHERS", f"{entity_id}:{period}", {
                        "entity_id": entity_id, "period": period,
                        "created": [item["id"] for item in result["created"]],
                        "skipped": result["skipped"],
                    })
                    return self._json(result)
                if parsed.path == "/api/game-revenue-policy":
                    entity_id, _ = _statutory_entity(request.get("entity_id"))
                    policy = create_revenue_policy(
                        str(request.get("game") or ""), str(request.get("channel") or ""),
                        str(request.get("revenue_stream") or ""), str(request.get("presentation") or ""),
                        str(request.get("recognition_method") or ""), str(request.get("effective_from") or ""),
                        actor, request.get("evidence") or [], service_months=request.get("service_months"),
                        role_facts=request.get("role_facts") or {}, entity_id=entity_id,
                    )
                    LEDGER.upsert_dataset("game_revenue_policies", [policy], actor, "游戏收入政策")
                    LEDGER.append_audit(actor, "CREATE_GAME_REVENUE_POLICY", policy["id"], {
                        "entity_id": entity_id,
                        "game": policy["game"], "channel": policy["channel"],
                        "presentation": policy["presentation"], "recognition_method": policy["recognition_method"],
                    })
                    return self._json({"policy": policy}, HTTPStatus.CREATED)
                if parsed.path == "/api/game-revenue-policy-review":
                    policy_id = str(request.get("policy_id") or "")
                    policies = LEDGER.load_dataset("game_revenue_policies")
                    index = next((i for i, item in enumerate(policies) if item.get("id") == policy_id), None)
                    if index is None:
                        raise ValueError("找不到游戏收入政策")
                    entity_id, _ = _statutory_entity(
                        request.get("entity_id") or policies[index].get("entity_id")
                    )
                    if str(policies[index].get("entity_id") or "") != entity_id:
                        raise ValueError("游戏收入政策不属于所选法律主体")
                    updated = review_revenue_policy(
                        policies[index], str(request.get("decision") or ""), actor,
                        str(request.get("rationale") or ""),
                    )
                    policies[index] = updated
                    LEDGER.save_dataset("game_revenue_policies", policies, actor, "游戏收入政策复核")
                    LEDGER.append_audit(actor, "GAME_REVENUE_POLICY_REVIEW", policy_id, {
                        **updated["review"], "entity_id": entity_id,
                    })
                    return self._json({"policy": updated})
                if parsed.path == "/api/revenue-close-review":
                    entity_id, _ = _statutory_entity(request.get("entity_id"))
                    candidates = LEDGER.load_dataset("settlement_candidates")
                    scoped = [row for row in candidates if str(row.get("entity_id") or "") == entity_id]
                    other = [row for row in candidates if str(row.get("entity_id") or "") != entity_id]
                    updated, released = review_settlement_candidates(
                        scoped, request.get("candidate_ids") or [], str(request.get("decision") or ""),
                        actor, str(request.get("rationale") or ""),
                    )
                    _guard_open_periods("settlements", released)
                    LEDGER.save_dataset(
                        "settlement_candidates", [*other, *updated], actor, "收入结算候选复核",
                    )
                    if released:
                        LEDGER.upsert_dataset("settlements", released, actor, "收入结算复核释放")
                    LEDGER.append_audit(actor, "REVIEW_REVENUE_CLOSE", entity_id, {
                        "entity_id": entity_id, "decision": str(request.get("decision") or ""),
                        "candidate_ids": [row.get("id") for row in released] or list(request.get("candidate_ids") or []),
                        "released_count": len(released),
                    })
                    datasets = _scoped_datasets(entity_id)
                    return self._json({
                        "entity_id": entity_id,
                        **revenue_close_payload(datasets["settlement_candidates"], datasets["settlements"]),
                    })
                if parsed.path == "/api/roll-forward":
                    period = str(request.get("period") or "")
                    entity_id, _ = _statutory_entity(request.get("entity_id"))
                    if LEDGER.load_period(period, entity_id).get("status") != "已关账":
                        raise ValueError("只有已关账期间才能结转下期期初")
                    finance = _finance_from_store(period, entity_id)
                    profit_account = get_ledger_adapter(_entity_profile(entity_id)).account("current_profit")
                    carry = roll_forward_opening_balances(
                        _scoped_datasets(entity_id)["opening_balances"], finance["posted_trial_balance"], period, actor,
                        f"{profit_account['code']} {profit_account['name']}",
                    )
                    for record in carry["records"]:
                        record["entity_id"] = entity_id
                    LEDGER.upsert_dataset("opening_balances", carry["records"], actor, "关账余额结转")
                    LEDGER.append_audit(actor, "ROLL_FORWARD_OPENING", f"{entity_id}:{period}", {
                        "entity_id": entity_id, "period": period,
                        "next_period": carry["period"], "record_count": len(carry["records"]),
                        "debit": carry["debit"], "credit": carry["credit"],
                    })
                    return self._json(carry)
                if parsed.path == "/api/tax-form-review":
                    period = str(request.get("period") or "")
                    _guard_cn_filing_entity(str(request.get("entity_id") or ""))
                    finance = _finance_from_store(period, str(request.get("entity_id") or ""))
                    reviews = LEDGER.load_dataset("tax_filing_reviews")
                    workspace = finance["tax_pack"]["returns_workspace"]
                    build_filing_assist(workspace, reviews)
                    updated = review_tax_form(
                        workspace, reviews,
                        str(request.get("form_code") or ""), str(request.get("decision") or ""),
                        actor, str(request.get("rationale") or ""), request.get("evidence") or [],
                    )
                    LEDGER.upsert_dataset("tax_filing_reviews", [updated], actor, "税务表单复核")
                    LEDGER.append_audit(actor, "TAX_FORM_REVIEW", updated["id"], updated["latest_review"])
                    return self._json({"review": updated})
                if parsed.path == "/api/tax-submission":
                    review_id = str(request.get("review_id") or "")
                    reviews = LEDGER.load_dataset("tax_filing_reviews")
                    index = next((i for i, item in enumerate(reviews) if item.get("id") == review_id), None)
                    if index is None:
                        raise ValueError("找不到税务复核记录")
                    review = reviews[index]
                    _guard_cn_filing_entity(str(review.get("entity_id") or ""))
                    finance = _finance_from_store(
                        str(review.get("period") or ""), str(review.get("entity_id") or ""),
                    )
                    workspace = finance["tax_pack"]["returns_workspace"]
                    _, current_fingerprint = form_fingerprint_from_workspace(workspace, str(review.get("form_code") or ""))
                    if review.get("form_fingerprint") != current_fingerprint:
                        raise ValueError("税务工作底稿在复核后已发生变化，请重新复核后再记录申报状态")
                    updated = record_tax_submission(
                        review, str(request.get("status") or ""), actor,
                        str(request.get("reference") or ""), request.get("evidence") or [],
                        str(request.get("note") or ""),
                    )
                    reviews[index] = updated
                    LEDGER.save_dataset("tax_filing_reviews", reviews, actor, "税务申报状态")
                    LEDGER.append_audit(actor, "TAX_SUBMISSION_STATUS", review_id, updated["submission"]["latest"])
                    return self._json({"review": updated})
                if parsed.path == "/api/datasets":
                    _guard_open_periods(str(request.get("name") or ""), request.get("records") or [])
                    saved = LEDGER.save_dataset(
                        str(request.get("name") or ""), request.get("records") or [], actor,
                        str(request.get("source") or "界面更新")[:200],
                    )
                    return self._json({"saved": saved})
                period = str(request.get("period") or "")
                if parsed.path == "/api/purchase-acceptance":
                    purchase_id = str(request.get("purchase_id") or "")[:160]
                    if not purchase_id:
                        raise ValueError("采购记录编号不能为空")
                    purchases = LEDGER.load_dataset("purchases")
                    index = next((i for i, item in enumerate(purchases) if item.get("id") == purchase_id), None)
                    if index is None:
                        raise ValueError("找不到对应采购记录")
                    entity_id, _ = _statutory_entity(purchases[index].get("entity_id"))
                    requested_entity = str(request.get("entity_id") or entity_id)
                    if requested_entity != entity_id:
                        raise ValueError("采购验收主体与采购记录不一致")
                    if LEDGER.load_period(period, entity_id).get("status") == "已关账":
                        raise ValueError("该主体期间已关账，不能新增或修改采购验收")
                    delivery_id = str(request.get("delivery_id") or "")
                    deliveries = LEDGER.load_dataset("purchase_deliveries")
                    if purchases[index].get("milestones"):
                        delivery_index = next((i for i, row in enumerate(deliveries) if row.get("id") == delivery_id), None)
                        if delivery_index is None:
                            raise ValueError("里程碑订单只能验收已记录的交付事件")
                        updated, updated_delivery = apply_delivery_acceptance_decision(
                            purchases[index], deliveries[delivery_index],
                            str(request.get("decision") or ""), actor,
                            accepted_amount=request.get("accepted_amount"),
                            evidence=request.get("evidence") or [], note=str(request.get("note") or ""),
                            period=period, all_deliveries=deliveries,
                        )
                        deliveries[delivery_index] = updated_delivery
                        LEDGER.save_dataset("purchase_deliveries", deliveries, actor, "采购里程碑验收")
                    else:
                        updated = apply_acceptance_decision(
                            purchases[index], str(request.get("decision") or ""), actor,
                            accepted_amount=request.get("accepted_amount"),
                            evidence=request.get("evidence") or [],
                            note=str(request.get("note") or ""), period=period,
                        )
                    purchases[index] = updated
                    LEDGER.save_dataset("purchases", purchases, actor, "采购验收")
                    LEDGER.append_audit(actor, "PURCHASE_ACCEPTANCE", purchase_id, {
                        "entity_id": entity_id, "period": period, "decision": updated.get("acceptance_status"),
                        "accepted_amount": updated.get("accepted_amount"),
                        "delivery_id": delivery_id,
                        "evidence": request.get("evidence") or updated.get("acceptance_evidence") or [],
                    })
                    response = {"purchase": updated, "procurement": procurement_payload(purchases)}
                    if delivery_id:
                        response["delivery"] = updated_delivery
                    return self._json(response)
                if parsed.path == "/api/reopen-period":
                    entity_id, _ = _statutory_entity(request.get("entity_id"))
                    state = LEDGER.load_period(period, entity_id)
                    if state.get("status") != "已关账":
                        raise ValueError("只有已关账期间需要执行重开")
                    reason = str(request.get("reason") or "").strip()
                    if len(reason) < 8:
                        raise ValueError("重开原因至少填写8个字")
                    if request.get("confirmation") != f"REOPEN {entity_id} {period}":
                        raise ValueError(f"请输入确认词 REOPEN {entity_id} {period}")
                    state = LEDGER.save_period(period, {
                        "entity_id": entity_id,
                        "status": "开放", "reopened_at": datetime.now(timezone.utc).isoformat(),
                        "close_events": (state.get("close_events") or []) + [{
                            "actor": actor, "timestamp": datetime.now(timezone.utc).isoformat(),
                            "result": "重开", "reason": reason,
                        }],
                    }, actor, entity_id)
                    LEDGER.append_audit(actor, "REOPEN_PERIOD", f"{entity_id}:{period}", {
                        "entity_id": entity_id, "period": period, "reason": reason,
                    })
                    return self._json({"reopened": True, "period_state": state})
                if parsed.path == "/api/tax-review":
                    entity_id, _ = _statutory_entity(request.get("entity_id") or "cn_studio")
                    _guard_cn_filing_entity(entity_id)
                    if LEDGER.load_period(period, entity_id).get("status") == "已关账":
                        raise ValueError("该期间已关账，不能修改跨境税务复核结论")
                    channel = str(request.get("channel") or "").strip()[:200]
                    decision = str(request.get("decision") or "").strip()
                    reviewer = str(request.get("reviewer") or "").strip()[:100]
                    evidence = str(request.get("evidence") or "").strip()[:2000]
                    if not channel:
                        raise ValueError("渠道不能为空")
                    if decision not in {"境内应税", "跨境零税率", "跨境免税", "待补证据"}:
                        raise ValueError("跨境税务复核结论无效")
                    if not reviewer:
                        raise ValueError("请填写复核人或税务服务机构")
                    if decision != "待补证据" and len(evidence) < 8:
                        raise ValueError("请填写支持该税务结论的合同、消费地或履约证据")
                    profile = load_profile(COMPANY_PROFILE)
                    reviews = dict((profile.get("tax_policy") or {}).get("cross_border_reviews") or {})
                    review = {
                        "decision": decision, "reviewer": reviewer, "evidence": evidence,
                        "entity_id": entity_id, "period": period, "updated_at": datetime.now(timezone.utc).isoformat(),
                        "agent_position": (
                            "证据不完整时继续标记待补证据，不按收款币种推断。"
                            if decision == "待补证据" else
                            "已记录有权复核人的结论；Agent 仍保留交易事实和证据链。"
                        ),
                    }
                    reviews[channel] = review
                    profile.setdefault("tax_policy", {})["cross_border_reviews"] = reviews
                    save_profile(COMPANY_PROFILE, profile)
                    LEDGER.append_audit(actor, "CROSS_BORDER_TAX_REVIEW", f"{entity_id}:{period}:{channel}", review)
                    return self._json({"review": review, "profile": profile})
                if parsed.path == "/api/voucher-review":
                    entity_id, _ = _statutory_entity(request.get("entity_id"))
                    if LEDGER.load_period(period, entity_id).get("status") == "已关账":
                        raise ValueError("该期间已关账，不能修改凭证复核决定")
                    decision = str(request.get("decision") or "")
                    voucher_id = str(request.get("voucher_id") or "")[:160]
                    if not voucher_id:
                        raise ValueError("凭证编号不能为空")
                    finance = _finance_from_store(period, entity_id)
                    voucher = next((item for item in finance["vouchers"] if item.get("id") == voucher_id), None)
                    if not voucher:
                        raise ValueError("当前期间不存在该凭证")
                    if decision == "接受" and (voucher.get("status") == "阻塞" or not voucher.get("balanced")):
                        raise ValueError("阻塞或借贷不平的凭证不能接受，请先补齐汇率、验收或匹配证据")
                    review = LEDGER.record_review(
                        period, voucher_id, decision, actor,
                        str(request.get("rationale") or "")[:1000], request.get("evidence") or [],
                        entity_id,
                    )
                    return self._json({"review": review, "period_state": LEDGER.load_period(period, entity_id)})
                if parsed.path == "/api/period-state":
                    entity_id, _ = _statutory_entity(request.get("entity_id"))
                    if LEDGER.load_period(period, entity_id).get("status") == "已关账":
                        raise ValueError("已关账期间必须通过有原因和确认词的重开流程处理")
                    status = str(request.get("status") or "")
                    if status not in {"开放", "复核中"}:
                        raise ValueError("期间只能设为开放或复核中；关账请使用关账检查")
                    return self._json({"period_state": LEDGER.save_period(
                        period, {"status": status, "entity_id": entity_id}, actor, entity_id,
                    )})
                entity_id, _ = _statutory_entity(request.get("entity_id"))
                finance = _finance_from_store(period, entity_id)
                period_state = LEDGER.load_period(period, entity_id)
                gaps = profile_gaps(_entity_profile(entity_id)) if entity_id == "cn_studio" else []
                assessment = assess_close(finance, period_state, gaps)
                if not assessment["can_close"]:
                    LEDGER.append_audit(actor, "CLOSE_BLOCKED", f"{entity_id}:{period}", {
                        "entity_id": entity_id, "period": period, "blockers": assessment["blockers"],
                    })
                    return self._json({
                        "closed": False, "blockers": assessment["blockers"],
                        "recommendation": assessment["recommendation"],
                    }, HTTPStatus.CONFLICT)
                state = LEDGER.save_period(period, {
                    "entity_id": entity_id,
                    "status": "已关账", "closed_at": datetime.now(timezone.utc).isoformat(),
                    "close_events": (period_state.get("close_events") or []) + [{
                        "actor": actor, "timestamp": datetime.now(timezone.utc).isoformat(), "result": "通过",
                    }],
                }, actor, entity_id)
                LEDGER.append_audit(actor, "CLOSE_PERIOD", f"{entity_id}:{period}", {
                    "entity_id": entity_id, "period": period,
                    "voucher_count": len(assessment["eligible_voucher_ids"]),
                })
                return self._json({"closed": True, "period_state": state})
            except (UnicodeDecodeError, json.JSONDecodeError, AttributeError, ValueError) as error:
                return self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        if parsed.path == "/api/finance-ops":
            content_length = guarded_length
            try:
                request = json.loads(self.rfile.read(content_length).decode("utf-8"))
                records = request.get("records") or []
                purchases = request.get("purchases") or []
                bank_transactions = request.get("bank_transactions") or []
                invoices = request.get("invoices") or []
                payroll_rows = request.get("payroll_rows") or []
                opening_balances = request.get("opening_balances") or []
                company_profile = request.get("company_profile") or load_profile(COMPANY_PROFILE)
                entity_id = str(request.get("entity_id") or "").strip()
                if entity_id:
                    entity_id, _ = _statutory_entity(entity_id)
                    def scoped(rows):
                        return [row for row in rows if str(row.get("entity_id") or "") == entity_id]
                    records = scoped(records)
                    purchases = scoped(purchases)
                    bank_transactions = scoped(bank_transactions)
                    invoices = scoped(invoices)
                    payroll_rows = scoped(payroll_rows)
                    opening_balances = scoped(opening_balances)
                    company_profile = _entity_profile(entity_id)
                period = str(request.get("period") or "").strip()
                if not period:
                    periods = sorted({record.get("period") for record in records if record.get("period")})
                    period = periods[-1] if periods else datetime.now().strftime("%Y-%m")
            except (UnicodeDecodeError, json.JSONDecodeError, AttributeError, BoxRuntimeError, ValueError) as error:
                return self._json({"error": str(error) or "请求内容不是有效的财务数据"}, HTTPStatus.BAD_REQUEST)
            return self._json(build_finance_ops(
                records, period, purchases, bank_transactions, invoices, payroll_rows, company_profile,
                opening_balances,
                ledger_adapter_reviews=(
                    [row for row in LEDGER.load_dataset("ledger_adapter_reviews")
                     if not entity_id or str(row.get("entity_id") or "") == entity_id]
                ),
                bank_reconciliation_reviews=(
                    [row for row in LEDGER.load_dataset("bank_reconciliation_reviews")
                     if not entity_id or str(row.get("entity_id") or "") == entity_id]
                ),
            ))
        if parsed.path == "/api/qa":
            content_length = guarded_length
            try:
                request = json.loads(self.rfile.read(content_length).decode("utf-8"))
                question = str(request.get("question") or "")[:1000]
                requested_period = str(request.get("period") or "") or None
                scenario = str(request.get("scenario") or "基准")[:40]
                datasets, demo_mode = _analysis_datasets()
                profile = load_profile(COMPANY_PROFILE)
                bp = build_bp_analysis(datasets, profile, requested_period, scenario)
                finance = _finance_from_store(bp["period"]) if bp.get("period") and not demo_mode else None
                onboarding = build_onboarding(profile, LEDGER.load_all())
                answer = answer_finance_question(question, bp, finance, onboarding)
                answer["demo_mode"] = demo_mode
                LEDGER.append_audit("财务工作台用户", "FINANCE_QA", question[:100], {
                    "period": bp.get("period"), "confidence": answer.get("confidence"),
                })
                return self._json(answer)
            except (UnicodeDecodeError, json.JSONDecodeError, AttributeError, ValueError) as error:
                return self._json({"error": str(error), "suggested_questions": SUGGESTED_QUESTIONS}, HTTPStatus.BAD_REQUEST)
        if parsed.path == "/api/company-profile":
            content_length = guarded_length
            try:
                request = json.loads(self.rfile.read(content_length).decode("utf-8"))
                candidate = request.get("profile") or request
                _guard_profile_change(candidate)
                profile = save_profile(COMPANY_PROFILE, candidate)
                LEDGER.append_audit("财务工作台用户", "SAVE_COMPANY_PROFILE", profile.get("company_name") or "公司档案", {
                    "accounting_standard": profile.get("accounting_standard"),
                    "vat_taxpayer_type": profile.get("vat_taxpayer_type"),
                })
            except (UnicodeDecodeError, json.JSONDecodeError, AttributeError, ValueError) as error:
                return self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            return self._json({"profile": profile, "gaps": profile_gaps(profile)})
        if parsed.path == "/api/templates":
            content_length = guarded_length
            try:
                template = json.loads(self.rfile.read(content_length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return self._json({"error": "模板内容不是有效 JSON"}, HTTPStatus.BAD_REQUEST)
            name = str(template.get("name") or "").strip()
            if not name or not template.get("fingerprint") or not template.get("config"):
                return self._json({"error": "模板名称、表头指纹和配置不能为空"}, HTTPStatus.BAD_REQUEST)
            templates = _load_templates()
            template["id"] = template.get("id") or hashlib.sha1(
                f"{name}|{template['fingerprint']}".encode("utf-8")
            ).hexdigest()[:12]
            template["updated_at"] = datetime.now(timezone.utc).isoformat()
            templates = [item for item in templates if item.get("id") != template["id"]]
            templates.append(template)
            _save_templates(templates)
            return self._json({"template": template})
        if parsed.path not in {
            "/api/import", "/api/discover", "/api/configured-import",
            "/api/procurement-import", "/api/bank-import", "/api/invoice-import", "/api/payroll-import",
            "/api/planning-import", "/api/onboarding-import", "/api/kpi-import",
            "/api/opening-balance-import",
            "/api/inbox-upload",
            "/api/shadow-close-import",
        }:
            return self._json({"error": "接口不存在"}, HTTPStatus.NOT_FOUND)
        try:
            uploads, fields = self._multipart()
        except OverflowError as error:
            return self._json({"error": str(error)}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
        except ValueError as error:
            return self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        if parsed.path == "/api/inbox-upload":
            if not uploads:
                return self._json({"error": "未找到上传文件"}, HTTPStatus.BAD_REQUEST)
            actor = str(fields.get("actor") or "财务工作台用户")[:80]
            entity_id = str(fields.get("entity_id") or "").strip()
            documents = []
            errors = []
            for upload in uploads:
                try:
                    documents.append(FINANCE_INBOX.ingest(
                        upload["filename"], upload["data"], actor, entity_id, _box_entities(),
                    ))
                except ValueError as error:
                    errors.append({"filename": upload["filename"], "error": str(error)})
            if not documents:
                return self._json({"error": "没有可接收的财务资料", "files": errors}, HTTPStatus.BAD_REQUEST)
            return self._json({"documents": documents, "errors": errors}, HTTPStatus.CREATED)
        import_entity_id = ""
        if parsed.path not in {"/api/discover", "/api/shadow-close-import"}:
            try:
                import_entity_id, _ = _statutory_entity(fields.get("entity_id"))
            except (BoxRuntimeError, ValueError) as error:
                return self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        files = []
        with tempfile.TemporaryDirectory(prefix="settlement-mvp-") as temp_dir:
            for upload in uploads:
                filename = upload["filename"]
                if not filename.lower().endswith(".xlsx"):
                    continue
                path = Path(temp_dir) / filename
                path.write_bytes(upload["data"])
                files.append(path)
            if not files:
                return self._json({"error": "未找到可解析的.xlsx文件"}, HTTPStatus.BAD_REQUEST)
            try:
                if parsed.path == "/api/onboarding-import":
                    master_rows, kpi_rows, profile_patch = [], [], {}
                    warnings = []
                    for path in files:
                        master_rows.extend(parse_master_workbook(path))
                        kpi_rows.extend(parse_kpi_workbook(path))
                        _deep_update(profile_patch, parse_profile_workbook(path))
                    if not master_rows and not kpi_rows and not profile_patch:
                        raise ValueError("未找到主体配置、游戏项目、渠道规则、组织映射、供应商或经营KPI工作表")
                    master_rows = _bind_import_entity(master_rows, import_entity_id)
                    kpi_rows = _bind_import_entity(enrich_kpis(kpi_rows), import_entity_id)
                    if profile_patch:
                        profile = load_profile(COMPANY_PROFILE)
                        profile_entity_id = str(profile.get("entity_id") or "cn_studio")
                        if import_entity_id != profile_entity_id:
                            warnings.append(
                                f"上线包中的公司财务档案未写入：该页只维护 {profile_entity_id}；"
                                f"{import_entity_id} 的法定档案以 Box/Tax Pack 配置为准"
                            )
                            profile_patch = {}
                        else:
                            _guard_profile_change(_deep_update(profile, profile_patch))
                    if master_rows:
                        LEDGER.upsert_dataset("master_records", master_rows, source="前置上线包导入")
                    if kpi_rows:
                        _guard_open_periods("game_kpis", kpi_rows)
                        LEDGER.upsert_dataset("game_kpis", kpi_rows, source="前置上线包经营KPI导入")
                    if profile_patch:
                        save_profile(COMPANY_PROFILE, profile)
                    datasets = _scoped_datasets(import_entity_id)
                    payload = {
                        "master_imported": len(master_rows), "kpi_imported": len(kpi_rows),
                        "profile_fields_imported": len(profile_patch),
                        "entity_id": import_entity_id, "warnings": warnings,
                        "master_quality": master_quality(datasets["master_records"]),
                        "kpi_quality": kpi_quality(datasets["game_kpis"], {
                            row.get("code") for row in datasets["master_records"] if row.get("record_type") == "game"
                        }),
                        "onboarding": build_onboarding(_entity_profile(import_entity_id), datasets),
                    }
                elif parsed.path == "/api/shadow-close-import":
                    if len(files) != 1:
                        raise ValueError("每次只能导入一份 shadow close 基准工作簿")
                    baseline = parse_shadow_close_workbook(files[0])
                    BOX_RUNTIME.require_entity(baseline["entity_id"])
                    existing = [row for row in LEDGER.load_dataset("shadow_close_baselines") if row.get("entity_id") == baseline["entity_id"] and row.get("period") == baseline["period"]]
                    if existing and fields.get("confirmation") != "REPLACE_SHADOW_BASELINE":
                        raise ValueError("该主体和期间已有基准；如需替换，请明确确认 REPLACE_SHADOW_BASELINE")
                    principal = getattr(self, "_api_principal", None)
                    actor = str(principal.principal_id if principal else fields.get("actor") or "财务工作台用户")[:80]
                    baseline["imported_by"] = actor
                    LEDGER.upsert_dataset("shadow_close_baselines", [baseline], actor, "Shadow close 人工基准导入")
                    finance = _finance_from_store(baseline["period"], baseline["entity_id"])
                    report = compare_shadow_close(
                        baseline,
                        finance,
                        LEDGER.load_dataset("shadow_close_reviews"),
                        runtime_fingerprint=BOX_RUNTIME.snapshot()["fingerprint"],
                    )
                    LEDGER.append_audit(actor, "IMPORT_SHADOW_CLOSE_BASELINE", baseline["id"], {
                        "entity_id": baseline["entity_id"], "period": baseline["period"],
                        "row_count": baseline["row_count"], "source_fingerprint": baseline["source_fingerprint"],
                    })
                    payload = {"baseline": baseline, "report": report}
                elif parsed.path == "/api/kpi-import":
                    kpi_rows = []
                    for path in files:
                        kpi_rows.extend(parse_kpi_workbook(path))
                    if not kpi_rows:
                        raise ValueError("未找到“经营KPI”工作表或可用数据行")
                    enriched = _bind_import_entity(enrich_kpis(kpi_rows), import_entity_id)
                    _guard_open_periods("game_kpis", enriched)
                    LEDGER.upsert_dataset("game_kpis", enriched, source="经营KPI导入")
                    master_rows = LEDGER.load_dataset("master_records")
                    payload = {
                        "records": enriched,
                        "quality": kpi_quality(enriched, {
                            row.get("code") for row in master_rows if row.get("record_type") == "game"
                        }),
                    }
                elif parsed.path == "/api/opening-balance-import":
                    balance_rows = []
                    period = fields.get("period") or ""
                    for path in files:
                        balance_rows.extend(parse_opening_balance_workbook(path, period))
                    payload = opening_balance_payload(balance_rows)
                    payload["records"] = _bind_import_entity(payload["records"], import_entity_id)
                    _guard_open_periods("opening_balances", payload["records"])
                    LEDGER.upsert_dataset("opening_balances", payload["records"], source="期初科目余额导入")
                elif parsed.path == "/api/planning-import":
                    plan_lines = []
                    for path in files:
                        plan_lines.extend(parse_plan_workbook(path))
                    payload = planning_payload(plan_lines)
                    payload["records"] = _bind_import_entity(payload["records"], import_entity_id)
                    _guard_open_periods("plan_lines", payload["records"])
                    LEDGER.upsert_dataset("plan_lines", payload["records"], source="预算与滚动预测导入")
                elif parsed.path == "/api/payroll-import":
                    period = fields.get("period") or datetime.now().strftime("%Y-%m")
                    entity_id = import_entity_id
                    profile = _entity_profile(entity_id)
                    payroll_rows = []
                    for path in files:
                        payroll_rows.extend(parse_payroll_workbook(
                            path, period, str(profile.get("jurisdiction") or "CN"),
                            str(profile.get("functional_currency") or profile.get("base_currency") or "CNY"),
                        ))
                    payload = payroll_payload(payroll_rows, period)
                    for record in payload["records"]:
                        record["entity_id"] = entity_id
                    _guard_open_periods("payroll_rows", payload["records"])
                    LEDGER.upsert_dataset("payroll_rows", payload["records"], source="工资台账导入")
                elif parsed.path == "/api/invoice-import":
                    invoice_rows = []
                    for path in files:
                        invoice_rows.extend(parse_invoice_workbook(path))
                    scoped = _scoped_datasets(import_entity_id)
                    purchases = scoped["purchases"]
                    if not purchases:
                        purchases = _bind_import_entity(
                            json.loads(fields.get("purchases") or "[]"), import_entity_id,
                        )
                    existing_invoices = scoped["invoices"]
                    payload = invoice_payload(match_invoices_to_purchases(
                        invoice_rows, purchases, existing_invoices,
                    ))
                    payload["records"] = _bind_import_entity(payload["records"], import_entity_id)
                    _guard_open_periods("invoices", payload["records"])
                    combined_invoices = {
                        str(row.get("id") or ""): row for row in [*existing_invoices, *payload["records"]]
                    }
                    updated_purchases = roll_invoice_totals_to_purchases(
                        purchases, combined_invoices.values(),
                    )
                    LEDGER.upsert_dataset("invoices", payload["records"], source="发票台账导入")
                    all_purchases = [
                        row for row in LEDGER.load_dataset("purchases")
                        if str(row.get("entity_id") or "") != import_entity_id
                    ] + updated_purchases
                    LEDGER.save_dataset("purchases", all_purchases, source="发票匹配回写")
                    payload["procurement"] = procurement_payload(updated_purchases)
                elif parsed.path == "/api/bank-import":
                    bank_rows = []
                    for path in files:
                        bank_rows.extend(parse_bank_workbook(path))
                    bank_rows = _bind_import_entity(bank_rows, import_entity_id)
                    scoped = _scoped_datasets(import_entity_id)
                    payload = banking_payload(suggest_matches(
                        bank_rows, scoped["settlements"], scoped["purchases"], scoped["cash_allocations"],
                    ))
                    _guard_open_periods("bank_transactions", payload["transactions"])
                    LEDGER.upsert_dataset("bank_transactions", payload["transactions"], source="银行流水导入")
                elif parsed.path == "/api/procurement-import":
                    purchase_records = []
                    for path in files:
                        purchase_records.extend(parse_purchase_workbook(path))
                    payload = procurement_payload(purchase_records)
                    payload["records"] = _bind_import_entity(payload["records"], import_entity_id)
                    _guard_open_periods("purchases", payload["records"])
                    LEDGER.upsert_dataset("purchases", payload["records"], source="采购台账导入")
                elif parsed.path == "/api/discover":
                    discoveries = [discover_workbook(path) for path in files]
                    templates = _load_templates()
                    for discovery in discoveries:
                        fingerprint = (discovery.get("selected") or {}).get("fingerprint")
                        discovery["matched_template"] = next(
                            (template for template in templates if template.get("fingerprint") == fingerprint), None
                        )
                    payload = {"workbooks": discoveries, "template_count": len(templates)}
                elif parsed.path == "/api/configured-import":
                    config = json.loads(fields.get("config") or "{}")
                    payload = dashboard_payload(parse_workbook_configured(files[0], config))
                    payload["import_config"] = config
                    payload["records"] = _bind_import_entity(payload["records"], import_entity_id)
                    scoped = _scoped_datasets(import_entity_id)
                    payload["records"] = prepare_settlement_candidates(
                        payload["records"], scoped["master_records"],
                        existing_candidates=scoped["settlement_candidates"],
                        existing_settlements=scoped["settlements"],
                    )
                    LEDGER.upsert_dataset("settlement_candidates", payload["records"], source="对账单配置导入候选")
                    payload["revenue_close"] = revenue_close_payload(payload["records"], scoped["settlements"])
                    payload["summary"]["pending_count"] = sum(
                        row.get("release_status") == "ready_for_review" for row in payload["records"]
                    )
                    payload["summary"]["exception_count"] = sum(
                        row.get("release_status") == "blocked" for row in payload["records"]
                    )
                else:
                    payload = dashboard_payload(parse_files(files))
                    payload["records"] = _bind_import_entity(payload["records"], import_entity_id)
                    scoped = _scoped_datasets(import_entity_id)
                    payload["records"] = prepare_settlement_candidates(
                        payload["records"], scoped["master_records"],
                        existing_candidates=scoped["settlement_candidates"],
                        existing_settlements=scoped["settlements"],
                    )
                    LEDGER.upsert_dataset("settlement_candidates", payload["records"], source="对账单自动导入候选")
                    payload["revenue_close"] = revenue_close_payload(payload["records"], scoped["settlements"])
                    payload["summary"]["pending_count"] = sum(
                        row.get("release_status") == "ready_for_review" for row in payload["records"]
                    )
                    payload["summary"]["exception_count"] = sum(
                        row.get("release_status") == "blocked" for row in payload["records"]
                    )
            except Exception as error:
                return self._json({"error": f"解析失败：{error}"}, HTTPStatus.UNPROCESSABLE_ENTITY)
        return self._json(payload)

    def log_message(self, format, *args):
        print(f"[FinanceAgent] {self.address_string()} {format % args}")


def run(host="127.0.0.1", port=8765):
    _validate_server_binding(
        host,
        os.environ.get("OPC_FINANCE_API_TOKEN"),
        os.environ.get("OPC_FINANCE_API_AUTH_FILE"),
    )
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"智能财务工作台已启动：http://{host}:{port}")
    previous_sigterm = None
    if threading.current_thread() is threading.main_thread():
        previous_sigterm = signal.getsignal(signal.SIGTERM)

        def stop_after_current_requests(_signum, _frame):
            threading.Thread(target=server.shutdown, daemon=True).start()

        signal.signal(signal.SIGTERM, stop_after_current_requests)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if previous_sigterm is not None:
            signal.signal(signal.SIGTERM, previous_sigterm)


def _server_port_from_environment() -> int:
    raw = os.environ.get("OPC_FINANCE_PORT") or os.environ.get("SETTLEMENT_MVP_PORT", "8765")
    try:
        port = int(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("OPC_FINANCE_PORT must be an integer from 1 to 65535") from exc
    if not 1 <= port <= 65535:
        raise RuntimeError("OPC_FINANCE_PORT must be an integer from 1 to 65535")
    return port


def main():
    run(
        host=os.environ.get("OPC_FINANCE_HOST", "127.0.0.1"),
        port=_server_port_from_environment(),
    )


if __name__ == "__main__":
    main()
