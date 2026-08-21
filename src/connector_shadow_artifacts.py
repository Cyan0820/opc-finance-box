from __future__ import annotations

import calendar
import hashlib
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .box_runtime import BoxRuntime


MAX_INPUT_BYTES = 50 * 1024 * 1024
PIPELINE_CONTROL_TYPES = {
    "stripe.daily_close": {
        "pipeline_ready": bool,
        "balance_transaction_count": int,
        "payout_count": int,
        "payout_bank_candidate_count": int,
        "payout_bank_exception_count": int,
    },
    "dtc.shopify_stripe_daily_close": {
        "pipeline_ready": bool,
        "processor_link_matched_count": int,
        "processor_link_exception_count": int,
        "payout_bank_candidate_count": int,
        "payout_bank_exception_count": int,
    },
    "dtc.shopify_stripe_month_close": {
        "pipeline_ready": bool,
        "shopify_order_count": int,
        "shopify_transaction_count": int,
        "shopify_refund_count": int,
        "stripe_balance_transaction_count": int,
        "created_population_count": int,
        "updated_population_count": int,
        "deduplicated_order_count": int,
        "monthly_created_order_count": int,
        "monthly_refund_event_count": int,
        "processor_link_matched_count": int,
        "processor_link_exception_count": int,
        "shopify_network_read_performed": bool,
        "stripe_network_read_performed": bool,
        "canonical_month_half_open_window": bool,
        "close_capture_within_72_hours": bool,
        "created_and_updated_population_contract": bool,
        "refund_processed_at_membership": bool,
        "refund_component_and_transaction_reconciled": bool,
        "entity_scope_matched": bool,
        "candidate_only_no_external_actions": bool,
    },
    "finance.expense_evidence_review": {
        "pipeline_ready": bool,
        "expense_record_count": int,
        "receipt_missing_count": int,
        "business_purpose_missing_count": int,
        "uncleared_count": int,
        "accounting_mapping_missing_count": int,
        "state_change_candidate_count": int,
        "network_refetch_performed": bool,
        "webhook_refetch_basis": bool,
        "external_actions_disabled": bool,
    },
    "finance.trial_balance_review": {
        "pipeline_ready": bool,
        "trial_balance_line_count": int,
        "scope_count": int,
        "balanced_scope_count": int,
        "unbalanced_scope_count": int,
        "roll_forward_checked_scope_count": int,
        "network_snapshot_performed": bool,
        "as_at_period_end": bool,
        "payments_only_disabled": bool,
        "entity_currency_binding_matched": bool,
        "point_in_time_snapshot": bool,
        "opening_and_period_movements_absent": bool,
        "ytd_columns_preserved_separately": bool,
        "external_actions_disabled": bool,
    },
    "finance.bank_statement_close": {
        "pipeline_ready": bool,
        "bank_transaction_count": int,
        "account_scope_count": int,
        "pending_transaction_count": int,
        "network_statement_performed": bool,
        "monthly_half_open_window": bool,
        "entity_currency_binding_matched": bool,
        "business_profile_verified": bool,
        "compact_english_statement": bool,
        "opening_closing_balance_controls_present": bool,
        "reconciliation_candidate_only": bool,
        "bank_balance_unconfirmed_without_review": bool,
        "external_actions_disabled": bool,
    },
    "commerce.shipbob_fulfillment_close": {
        "pipeline_ready": bool,
        "order_count": int,
        "shipment_count": int,
        "return_count": int,
        "return_item_count": int,
        "unfulfilled_order_count": int,
        "unprocessed_return_item_count": int,
        "cross_window_return_reference_count": int,
        "network_fulfillment_read_performed": bool,
        "monthly_half_open_window": bool,
        "entity_scope_matched": bool,
        "customer_pii_excluded": bool,
        "raw_source_ids_excluded": bool,
        "write_api_disabled": bool,
        "posting_and_inventory_actions_disabled": bool,
    },
    "paypal.transaction_close": {
        "pipeline_ready": bool,
        "transaction_count": int,
        "refund_candidate_count": int,
        "reversal_candidate_count": int,
        "reference_review_required_count": int,
        "cross_currency_fee_count": int,
        "network_transaction_search_performed": bool,
        "oauth_exchange_in_memory": bool,
        "monthly_half_open_window": bool,
        "entity_scope_matched": bool,
        "transaction_info_only": bool,
        "customer_pii_and_free_text_excluded": bool,
        "raw_source_ids_excluded": bool,
        "business_write_posting_actions_disabled": bool,
    },
    "woocommerce.order_refund_close": {
        "pipeline_ready": bool,
        "order_count": int,
        "refund_event_count": int,
        "orphan_refund_count": int,
        "arithmetic_exception_count": int,
        "destination_review_required_count": int,
        "unpaid_or_unconfirmed_order_count": int,
        "network_order_refund_read_performed": bool,
        "monthly_half_open_window": bool,
        "entity_site_scope_matched": bool,
        "customer_pii_excluded": bool,
        "product_detail_excluded": bool,
        "raw_source_ids_excluded": bool,
        "fixed_read_only_transport_controls": bool,
        "business_write_posting_revenue_tax_actions_disabled": bool,
    },
    "amazon_seller.transaction_close": {
        "pipeline_ready": bool,
        "transaction_count": int,
        "released_transaction_count": int,
        "deferred_transaction_count": int,
        "refund_candidate_count": int,
        "fee_candidate_count": int,
        "settlement_reference_missing_count": int,
        "network_finances_read_performed": bool,
        "lwa_exchange_in_memory": bool,
        "monthly_half_open_window": bool,
        "entity_seller_marketplace_scope_matched": bool,
        "customer_product_store_free_text_excluded": bool,
        "raw_source_ids_excluded": bool,
        "fixed_regional_read_only_transport_controls": bool,
        "nested_component_double_counting_disabled": bool,
        "business_write_posting_revenue_tax_settlement_actions_disabled": bool,
    },
    "amazon_seller.marketplace_close": {
        "pipeline_ready": bool,
        "order_count": int,
        "inventory_sku_count": int,
        "transaction_count": int,
        "finance_without_order_count": int,
        "shipped_order_without_finance_count": int,
        "fba_order_sku_without_inventory_count": int,
        "inventory_quantity_field_missing_count": int,
        "network_three_source_read_performed": bool,
        "single_lwa_exchange_in_memory": bool,
        "monthly_orders_finances_half_open_window": bool,
        "current_inventory_not_historical_period_end": bool,
        "entity_seller_marketplace_scope_matched": bool,
        "buyer_recipient_product_raw_ids_excluded": bool,
        "orders_restricted_datasets_not_requested": bool,
        "fixed_regional_read_only_transport_controls": bool,
        "business_write_posting_revenue_tax_inventory_actions_disabled": bool,
    },
}
LEGACY_PIPELINE_CONTROL_TYPES = {
    pipeline_id: dict(controls)
    for pipeline_id, controls in PIPELINE_CONTROL_TYPES.items()
}
for _control_id in (
    "state_change_candidate_count", "network_refetch_performed", "webhook_refetch_basis",
):
    LEGACY_PIPELINE_CONTROL_TYPES["finance.expense_evidence_review"].pop(_control_id)
CONNECTOR_SHADOW_PROFILES = {
    "stripe.daily_close": {
        "covered_pack_ids": ["connector.stripe"],
        "sources": [
            ("processor_balance_activity", "stripe.balance_transactions"),
            ("processor_payouts", "stripe.payouts"),
        ],
    },
    "dtc.shopify_stripe_daily_close": {
        "covered_pack_ids": [
            "connector.shopify", "connector.stripe", "connector.wise",
            "feature.shopify_stripe_order_to_cash",
        ],
        "sources": [
            ("store_orders_transactions_refunds", "shopify.orders"),
            ("processor_balance_activity", "stripe.balance_transactions"),
            ("processor_payouts", "stripe.payouts"),
            ("bank_balance_statement", "wise.balance_statement"),
        ],
    },
    "dtc.shopify_stripe_month_close": {
        "covered_pack_ids": [
            "connector.shopify", "feature.shopify_stripe_order_to_cash",
        ],
        "sources": [
            ("store_monthly_created_and_updated_order_evidence", "shopify.monthly_order_evidence"),
            ("processor_same_window_balance_activity", "stripe.balance_transactions"),
        ],
    },
    "finance.expense_evidence_review": {
        "covered_pack_ids": ["connector.airwallex"],
        "sources": [("approved_card_expenses", "airwallex.approved_expenses")],
    },
    "finance.trial_balance_review": {
        "covered_pack_ids": ["connector.xero"],
        "sources": [("accounting_trial_balance_snapshot", "xero.trial_balance")],
    },
    "finance.bank_statement_close": {
        "covered_pack_ids": ["connector.wise"],
        "sources": [("bank_balance_statement", "wise.balance_statement")],
    },
    "commerce.shipbob_fulfillment_close": {
        "covered_pack_ids": ["connector.shipbob"],
        "sources": [("fulfillment_orders_shipments_and_returns", "shipbob.fulfillment")],
    },
    "paypal.transaction_close": {
        "covered_pack_ids": ["connector.paypal"],
        "sources": [("paypal_balance_affecting_transaction_activity", "paypal.transaction_activity")],
    },
    "woocommerce.order_refund_close": {
        "covered_pack_ids": ["connector.woocommerce"],
        "sources": [("woocommerce_modified_orders_and_refund_events", "woocommerce.order_refund_activity")],
    },
    "amazon_seller.transaction_close": {
        "covered_pack_ids": ["connector.amazon_seller"],
        "sources": [("amazon_seller_finances_transaction_activity", "amazon_seller.transaction_activity")],
    },
    "amazon_seller.marketplace_close": {
        "covered_pack_ids": ["connector.amazon_seller"],
        "sources": [
            (
                "amazon_seller_orders_current_fba_inventory_and_finances",
                "amazon_seller.marketplace_evidence",
            )
        ],
    },
}
_DTC_SHOPIFY_STRIPE_PIPELINE = "dtc.shopify_stripe_daily_close"
_DTC_SHOPIFY_STRIPE_MONTHLY_PIPELINE = "dtc.shopify_stripe_month_close"
_STRIPE_PIPELINE = "stripe.daily_close"
_DTC_OPTIONAL_WISE_PACK = "connector.wise"
_DTC_OPTIONAL_WISE_SOURCE = ("bank_balance_statement", "wise.balance_statement")
BASELINE_WORKPAPER_INSTRUCTIONS = [
    "Obtain counts and controls from an independent source export or workpaper, not the Pipeline result.",
    "Replace every null and add private evidence references without secrets or query strings.",
    "Set the independence/anonymization attestations and finalization_ready only after source-scope review.",
    "Keep raw exports outside this baseline; the finalized artifact contains counts and booleans only.",
]
DECISIONS = {"passed", "accepted-differences", "needs-correction"}
PACK_PATTERN = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")
SECRET_PATTERN = re.compile(
    r"(?:secret|token|password|authorization|api[_-]?key|credential|bearer\s|"
    r"sk_|rk_|shpat_)", re.I,
)
NON_REAL_EVIDENCE_PATTERN = re.compile(
    r"(?:^|[/:_.-])(?:demo|fixture|example|synthetic)(?:$|[/:_.-])|"
    r"pipeline[-_ ]?(?:result|output)",
    re.I,
)


class ConnectorShadowArtifactError(ValueError):
    """Raised when a Connector Shadow artifact cannot preserve its evidence boundary."""


def _resolved_connector_shadow_profile(
    pipeline_id: str,
    *,
    selected_pack_ids: set[str] | None = None,
    covered_pack_ids: list[str] | None = None,
) -> dict[str, list[Any]]:
    """Resolve the exact evidence scope without making optional Packs mandatory."""
    profile = CONNECTOR_SHADOW_PROFILES.get(str(pipeline_id or ""))
    if profile is None:
        raise ConnectorShadowArtifactError(
            "Connector Shadow baseline profile is unsupported"
        )
    packs = list(profile["covered_pack_ids"])
    sources = list(profile["sources"])
    if pipeline_id != _DTC_SHOPIFY_STRIPE_PIPELINE:
        if covered_pack_ids is not None and covered_pack_ids != packs:
            raise ConnectorShadowArtifactError(
                "real Connector Shadow covered_pack_ids must exactly match the selected profile"
            )
        return {"covered_pack_ids": packs, "sources": sources}

    base_packs = [item for item in packs if item != _DTC_OPTIONAL_WISE_PACK]
    base_sources = [item for item in sources if item != _DTC_OPTIONAL_WISE_SOURCE]
    if selected_pack_ids is not None:
        include_wise = _DTC_OPTIONAL_WISE_PACK in selected_pack_ids
    elif covered_pack_ids is not None:
        if covered_pack_ids not in (base_packs, packs):
            raise ConnectorShadowArtifactError(
                "real Shopify + Stripe Shadow coverage must match the Box Connector scope"
            )
        include_wise = _DTC_OPTIONAL_WISE_PACK in covered_pack_ids
    else:
        include_wise = True
    return {
        "covered_pack_ids": packs if include_wise else base_packs,
        "sources": sources if include_wise else base_sources,
    }


def _entity_selected_pack_ids(
    runtime: BoxRuntime, snapshot: dict[str, Any], entity_id: str,
) -> set[str]:
    """Project selected Packs through the current legal-entity Connector bindings."""
    selected: set[str] = set()
    for item in snapshot["packs"]:
        pack_id = str(item["id"])
        if not pack_id.startswith("connector.") or entity_id in runtime.connector_entity_ids(pack_id):
            selected.add(pack_id)
    return selected


def build_connector_shadow_baseline_plan(runtime: BoxRuntime) -> dict[str, Any]:
    """Return the minimal supported real-evidence profiles for the selected Box."""
    runtime.reload()
    snapshot = runtime.snapshot()
    selected_pack_ids = {item["id"] for item in snapshot["packs"]}
    grouped: dict[tuple[str, tuple[str, ...], tuple[str, ...]], set[str]] = {}
    covered_connector_pack_ids: set[str] = set()
    covered_scopes: set[tuple[str, str]] = set()
    dtc_required = {
        "connector.shopify", "connector.stripe",
        "feature.shopify_stripe_order_to_cash",
    }

    def add_profile(entity_id: str, pipeline_id: str, entity_pack_ids: set[str]) -> None:
        profile = _resolved_connector_shadow_profile(
            pipeline_id, selected_pack_ids=entity_pack_ids,
        )
        connector_pack_ids = {
            item for item in profile["covered_pack_ids"] if item.startswith("connector.")
        }
        if not connector_pack_ids <= entity_pack_ids:
            return
        source_ids = tuple(item[1] for item in profile["sources"])
        key = (pipeline_id, tuple(profile["covered_pack_ids"]), source_ids)
        grouped.setdefault(key, set()).add(entity_id)
        for pack_id in connector_pack_ids:
            covered_scopes.add((entity_id, pack_id))
            covered_connector_pack_ids.add(pack_id)

    for entity_id in sorted(item["id"] for item in snapshot["entities"]):
        entity_pack_ids = _entity_selected_pack_ids(runtime, snapshot, entity_id)
        if dtc_required <= entity_pack_ids:
            add_profile(entity_id, _DTC_SHOPIFY_STRIPE_MONTHLY_PIPELINE, entity_pack_ids)
        if (
            "connector.stripe" in entity_pack_ids
            and (entity_id, "connector.stripe") not in covered_scopes
        ):
            add_profile(entity_id, _STRIPE_PIPELINE, entity_pack_ids)
        for pack_id, pipeline_id in (
            ("connector.airwallex", "finance.expense_evidence_review"),
            ("connector.xero", "finance.trial_balance_review"),
            ("connector.wise", "finance.bank_statement_close"),
            ("connector.shipbob", "commerce.shipbob_fulfillment_close"),
            ("connector.paypal", "paypal.transaction_close"),
            ("connector.woocommerce", "woocommerce.order_refund_close"),
            ("connector.amazon_seller", "amazon_seller.marketplace_close"),
        ):
            if pack_id in entity_pack_ids and (entity_id, pack_id) not in covered_scopes:
                add_profile(entity_id, pipeline_id, entity_pack_ids)

    plans = [{
        "pipeline_id": pipeline_id,
        "covered_pack_ids": list(pack_ids),
        "source_connector_ids": list(source_ids),
        "entity_ids": sorted(entity_ids),
    } for (pipeline_id, pack_ids, source_ids), entity_ids in sorted(grouped.items())]
    return {
        "schema_version": 1,
        "artifact_type": "connector_shadow_baseline_plan",
        "runtime_fingerprint": snapshot["fingerprint"],
        "profiles": plans,
        "covered_connector_pack_ids": sorted(covered_connector_pack_ids),
        "template_only": True,
        "baselines_created": False,
        "external_actions_performed": False,
    }


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _read(path: str | Path) -> Any:
    source = Path(path)
    if not source.is_file() or not 0 < source.stat().st_size <= MAX_INPUT_BYTES:
        raise ConnectorShadowArtifactError("Connector Shadow input must be a 1 byte to 50 MiB file")
    try:
        return json.loads(source.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConnectorShadowArtifactError("Connector Shadow input must be valid JSON") from exc


def _write_private(path: str | Path, payload: dict[str, Any]) -> Path:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise ConnectorShadowArtifactError("Connector Shadow output already exists") from exc
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    return destination


def _actor(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not re.fullmatch(r"[^\x00-\x1f\x7f]{1,80}", text):
        raise ConnectorShadowArtifactError(f"{field} must be 1-80 printable characters")
    return text


def _references(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ConnectorShadowArtifactError(f"{field} requires at least one evidence reference")
    output = []
    for item in value:
        text = str(item or "").strip()
        if not re.fullmatch(r"[^\x00-\x1f\x7f]{1,240}", text):
            raise ConnectorShadowArtifactError(f"{field} contains an invalid reference")
        output.append(text)
    return list(dict.fromkeys(output))


def _real_evidence_references(value: Any, field: str) -> list[str]:
    references = _references(value, field)
    if any(
        SECRET_PATTERN.search(item) or NON_REAL_EVIDENCE_PATTERN.search(item)
        or "?" in item or "#" in item
        for item in references
    ):
        raise ConnectorShadowArtifactError(
            f"{field} must use secret-free independent real-source references, not demo, "
            "fixture, synthetic or Pipeline-output evidence"
        )
    return references


def _period(value: Any) -> str:
    text = str(value or "")
    if not re.fullmatch(r"20\d{2}-(0[1-9]|1[0-2])", text):
        raise ConnectorShadowArtifactError("sample_period must use YYYY-MM")
    return text


def _period_end(period: str) -> str:
    year, month = (int(part) for part in period.split("-"))
    return f"{period}-{calendar.monthrange(year, month)[1]:02d}"


def build_connector_shadow_baseline_workpaper(
    runtime: BoxRuntime,
    *,
    pipeline_id: str,
    entity_id: str,
    sample_period: str,
    prepared_by: str,
    output: str | Path,
) -> dict[str, Any]:
    runtime.reload()
    runtime.require_entity(entity_id)
    sample_period = _period(sample_period)
    prepared_by = _actor(prepared_by, "prepared_by")
    snapshot = runtime.snapshot()
    selected_pack_ids = _entity_selected_pack_ids(runtime, snapshot, entity_id)
    profile = _resolved_connector_shadow_profile(
        pipeline_id, selected_pack_ids=selected_pack_ids,
    )
    missing = set(profile["covered_pack_ids"]) - selected_pack_ids
    if missing:
        raise ConnectorShadowArtifactError(
            "Connector Shadow profile requires selected Packs: " + ", ".join(sorted(missing))
        )
    workpaper = {
        "schema_version": 1,
        "artifact_type": "connector_shadow_baseline_workpaper",
        "runtime_fingerprint": snapshot["fingerprint"],
        "baseline_id": (
            f"{entity_id}-{sample_period}-{pipeline_id.replace('.', '-')}-real"
        )[:120],
        "pipeline_id": pipeline_id,
        "entity_id": entity_id,
        "sample_period": sample_period,
        "covered_pack_ids": list(profile["covered_pack_ids"]),
        "prepared_by": prepared_by,
        "prepared_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "sample_classification": "real_anonymized",
        "source_independence": {
            "prepared_from_independent_source": False,
            "pipeline_output_used_as_baseline": True,
            "source_scope_confirmed": False,
        },
        "anonymization": {
            "raw_identifiers_removed_from_baseline": True,
            "financial_amounts_removed_from_baseline": True,
            "private_source_evidence_retained": False,
        },
        "source_expectations": [{
            "source_role": source_role,
            "connector_id": connector_id,
            "expected_record_count": None,
            "evidence_references": [],
        } for source_role, connector_id in profile["sources"]],
        "control_expectations": [{
            "control_id": control_id,
            "expected_value": None,
        } for control_id in PIPELINE_CONTROL_TYPES[pipeline_id]],
        "evidence_references": [],
        "template_only": True,
        "finalization_ready": False,
        "instructions": list(BASELINE_WORKPAPER_INSTRUCTIONS),
    }
    destination = _write_private(output, workpaper)
    return {
        "output": str(destination),
        "pipeline_id": pipeline_id,
        "entity_id": entity_id,
        "sample_period": sample_period,
        "source_count": len(workpaper["source_expectations"]),
        "control_count": len(workpaper["control_expectations"]),
        "template_only": True,
        "finalization_ready": False,
        "raw_source_values_returned": False,
        "external_actions_performed": False,
    }


def validate_connector_shadow_baseline_workpaper(
    runtime: BoxRuntime, workpaper: Any,
) -> dict[str, Any]:
    """Validate a mutable private workpaper without treating it as a baseline."""
    required = {
        "schema_version", "artifact_type", "runtime_fingerprint", "baseline_id",
        "pipeline_id", "entity_id", "sample_period", "covered_pack_ids",
        "prepared_by", "prepared_at", "sample_classification",
        "source_independence", "anonymization", "source_expectations",
        "control_expectations", "evidence_references", "template_only",
        "finalization_ready", "instructions",
    }
    if not isinstance(workpaper, dict) or set(workpaper) != required:
        raise ConnectorShadowArtifactError("Connector Shadow baseline workpaper fields are invalid")
    if workpaper.get("schema_version") != 1 or workpaper.get("artifact_type") != "connector_shadow_baseline_workpaper":
        raise ConnectorShadowArtifactError("Connector Shadow baseline workpaper schema is unsupported")
    runtime.reload()
    snapshot = runtime.snapshot()
    if workpaper.get("runtime_fingerprint") != snapshot["fingerprint"]:
        raise ConnectorShadowArtifactError("Connector Shadow baseline workpaper belongs to another Box")
    runtime.require_entity(str(workpaper.get("entity_id") or ""))
    entity_id = str(workpaper.get("entity_id") or "")
    if not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}",
        str(workpaper.get("baseline_id") or ""),
    ):
        raise ConnectorShadowArtifactError(
            "Connector Shadow baseline workpaper baseline_id is invalid"
        )
    _period(workpaper.get("sample_period"))
    _actor(workpaper.get("prepared_by"), "prepared_by")
    try:
        prepared_at = datetime.fromisoformat(
            str(workpaper.get("prepared_at") or "").replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise ConnectorShadowArtifactError(
            "Connector Shadow baseline workpaper prepared_at must be ISO-8601"
        ) from exc
    if (
        prepared_at.tzinfo is None
        or prepared_at.astimezone(timezone.utc)
        > datetime.now(timezone.utc) + timedelta(minutes=5)
    ):
        raise ConnectorShadowArtifactError(
            "Connector Shadow baseline workpaper prepared_at is invalid"
        )
    if (
        workpaper.get("template_only") is not True
        or not isinstance(workpaper.get("finalization_ready"), bool)
        or workpaper.get("sample_classification") != "real_anonymized"
        or workpaper.get("instructions") != BASELINE_WORKPAPER_INSTRUCTIONS
    ):
        raise ConnectorShadowArtifactError(
            "Connector Shadow baseline workpaper template boundary is invalid"
        )
    entity_pack_ids = _entity_selected_pack_ids(runtime, snapshot, entity_id)
    profile = _resolved_connector_shadow_profile(
        str(workpaper.get("pipeline_id") or ""), selected_pack_ids=entity_pack_ids,
    )
    if workpaper.get("covered_pack_ids") != profile["covered_pack_ids"]:
        raise ConnectorShadowArtifactError(
            "Connector Shadow baseline workpaper does not match the entity Connector binding"
        )
    expected_sources = [
        {"source_role": role, "connector_id": connector}
        for role, connector in profile["sources"]
    ]
    sources = workpaper.get("source_expectations")
    if not isinstance(sources, list) or [
        {"source_role": item.get("source_role"), "connector_id": item.get("connector_id")}
        for item in sources if isinstance(item, dict)
    ] != expected_sources:
        raise ConnectorShadowArtifactError("Connector Shadow baseline source scope was altered")
    for index, item in enumerate(sources):
        if set(item) != {
            "source_role", "connector_id", "expected_record_count",
            "evidence_references",
        }:
            raise ConnectorShadowArtifactError(
                f"Connector Shadow baseline source {index} fields are invalid"
            )
        count = item.get("expected_record_count")
        if count is not None and (
            not isinstance(count, int) or isinstance(count, bool) or count < 0
        ):
            raise ConnectorShadowArtifactError(
                "Connector Shadow baseline source count must be null or non-negative"
            )
        references = item.get("evidence_references")
        if not isinstance(references, list):
            raise ConnectorShadowArtifactError(
                "Connector Shadow baseline source evidence must be a list"
            )
        if references:
            _real_evidence_references(
                references, f"source_expectations[{index}].evidence_references",
            )
    controls = workpaper.get("control_expectations")
    if not isinstance(controls, list) or [
        item.get("control_id") for item in controls if isinstance(item, dict)
    ] != list(PIPELINE_CONTROL_TYPES[workpaper["pipeline_id"]]):
        raise ConnectorShadowArtifactError("Connector Shadow baseline control scope was altered")
    for item in controls:
        if set(item) != {"control_id", "expected_value"}:
            raise ConnectorShadowArtifactError(
                "Connector Shadow baseline control fields are invalid"
            )
        expected = item.get("expected_value")
        expected_type = PIPELINE_CONTROL_TYPES[workpaper["pipeline_id"]][
            item["control_id"]
        ]
        if expected is None:
            continue
        if expected_type is bool and not isinstance(expected, bool):
            raise ConnectorShadowArtifactError(
                "Connector Shadow baseline boolean control must be null or boolean"
            )
        if expected_type is int and (
            not isinstance(expected, int) or isinstance(expected, bool) or expected < 0
        ):
            raise ConnectorShadowArtifactError(
                "Connector Shadow baseline count control must be null or non-negative"
            )
    if set(workpaper.get("source_independence") or {}) != {
        "prepared_from_independent_source", "pipeline_output_used_as_baseline",
        "source_scope_confirmed",
    } or any(
        not isinstance(item, bool)
        for item in (workpaper.get("source_independence") or {}).values()
    ):
        raise ConnectorShadowArtifactError(
            "Connector Shadow baseline independence attestations are invalid"
        )
    if set(workpaper.get("anonymization") or {}) != {
        "raw_identifiers_removed_from_baseline",
        "financial_amounts_removed_from_baseline",
        "private_source_evidence_retained",
    } or any(
        not isinstance(item, bool)
        for item in (workpaper.get("anonymization") or {}).values()
    ):
        raise ConnectorShadowArtifactError(
            "Connector Shadow baseline anonymization attestations are invalid"
        )
    evidence_references = workpaper.get("evidence_references")
    if not isinstance(evidence_references, list):
        raise ConnectorShadowArtifactError(
            "Connector Shadow baseline evidence_references must be a list"
        )
    if evidence_references:
        _real_evidence_references(evidence_references, "evidence_references")
    return dict(workpaper)


def finalize_connector_shadow_baseline_workpaper(
    runtime: BoxRuntime,
    workpaper_json: str | Path,
    output: str | Path,
) -> dict[str, Any]:
    workpaper = validate_connector_shadow_baseline_workpaper(
        runtime, _read(workpaper_json),
    )
    if workpaper.get("finalization_ready") is not True:
        raise ConnectorShadowArtifactError(
            "Connector Shadow baseline workpaper is not marked finalization_ready"
        )
    baseline = {
        key: workpaper[key] for key in (
            "baseline_id", "pipeline_id", "entity_id", "sample_period",
            "covered_pack_ids", "prepared_by", "prepared_at", "sample_classification",
            "source_independence", "anonymization", "source_expectations",
            "control_expectations", "evidence_references",
        )
    }
    baseline["schema_version"] = 2
    baseline = {"schema_version": baseline.pop("schema_version"), **baseline}
    validate_connector_shadow_baseline(baseline)
    destination = _write_private(output, baseline)
    return {
        "output": str(destination),
        "baseline_id": baseline["baseline_id"],
        "baseline_sha256": _hash(baseline),
        "pipeline_id": baseline["pipeline_id"],
        "entity_id": baseline["entity_id"],
        "sample_period": baseline["sample_period"],
        "sample_classification": "real_anonymized",
        "real_sample_evidence": True,
        "raw_source_values_returned": False,
        "financial_amounts_returned": False,
        "external_actions_performed": False,
    }


def validate_connector_shadow_baseline(value: Any) -> dict[str, Any]:
    legacy_fields = {
        "schema_version", "baseline_id", "pipeline_id", "entity_id", "sample_period",
        "covered_pack_ids", "prepared_by", "prepared_at", "source_expectations",
        "control_expectations", "evidence_references",
    }
    real_fields = legacy_fields | {
        "sample_classification", "source_independence", "anonymization",
    }
    if not isinstance(value, dict) or (
        value.get("schema_version") == 1 and set(value) != legacy_fields
    ) or (
        value.get("schema_version") == 2 and set(value) != real_fields
    ) or value.get("schema_version") not in {1, 2}:
        raise ConnectorShadowArtifactError("Connector Shadow baseline fields do not match the strict contract")
    pipeline_id = value.get("pipeline_id")
    if pipeline_id not in PIPELINE_CONTROL_TYPES:
        raise ConnectorShadowArtifactError("Connector Shadow baseline schema or pipeline is unsupported")
    real_sample = value["schema_version"] == 2
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}", str(value.get("baseline_id") or "")):
        raise ConnectorShadowArtifactError("baseline_id is invalid")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}", str(value.get("entity_id") or "")):
        raise ConnectorShadowArtifactError("entity_id is invalid")
    _period(value.get("sample_period"))
    covered_pack_ids = value.get("covered_pack_ids")
    if (
        not isinstance(covered_pack_ids, list) or not covered_pack_ids
        or covered_pack_ids != sorted(set(covered_pack_ids))
        or any(not isinstance(item, str) or not PACK_PATTERN.fullmatch(item) for item in covered_pack_ids)
    ):
        raise ConnectorShadowArtifactError("covered_pack_ids must be a sorted unique Pack id list")
    resolved_profile = (
        _resolved_connector_shadow_profile(
            pipeline_id, covered_pack_ids=covered_pack_ids,
        )
        if real_sample else None
    )
    _actor(value.get("prepared_by"), "prepared_by")
    try:
        parsed = datetime.fromisoformat(str(value.get("prepared_at") or "").replace("Z", "+00:00"))
    except ValueError as exc:
        raise ConnectorShadowArtifactError("prepared_at must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ConnectorShadowArtifactError("prepared_at must include timezone")
    if parsed.astimezone(timezone.utc) > datetime.now(timezone.utc) + timedelta(minutes=5):
        raise ConnectorShadowArtifactError("prepared_at must not be more than five minutes in the future")
    if real_sample:
        if value.get("sample_classification") != "real_anonymized":
            raise ConnectorShadowArtifactError("schema v2 Connector Shadow must be real_anonymized")
        if value.get("source_independence") != {
            "prepared_from_independent_source": True,
            "pipeline_output_used_as_baseline": False,
            "source_scope_confirmed": True,
        }:
            raise ConnectorShadowArtifactError(
                "real Connector Shadow requires explicit independent-source attestation"
            )
        if value.get("anonymization") != {
            "raw_identifiers_removed_from_baseline": True,
            "financial_amounts_removed_from_baseline": True,
            "private_source_evidence_retained": True,
        }:
            raise ConnectorShadowArtifactError(
                "real Connector Shadow requires explicit anonymization and evidence-retention attestation"
            )
    sources = value.get("source_expectations")
    if not isinstance(sources, list) or not sources:
        raise ConnectorShadowArtifactError("source_expectations must be non-empty")
    seen_connectors = set()
    for index, item in enumerate(sources):
        if not isinstance(item, dict) or set(item) != {
            "source_role", "connector_id", "expected_record_count", "evidence_references",
        }:
            raise ConnectorShadowArtifactError(f"source_expectations[{index}] is invalid")
        connector_id = str(item.get("connector_id") or "")
        if not re.fullmatch(r"[a-z][a-z0-9_.]{1,119}", connector_id) or connector_id in seen_connectors:
            raise ConnectorShadowArtifactError("source connector ids must be unique and valid")
        seen_connectors.add(connector_id)
        count = item.get("expected_record_count")
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise ConnectorShadowArtifactError("expected_record_count must be a non-negative integer")
        reference_validator = _real_evidence_references if real_sample else _references
        reference_validator(
            item.get("evidence_references"),
            f"source_expectations[{index}].evidence_references",
        )
    if real_sample and [
        (item["source_role"], item["connector_id"]) for item in sources
    ] != resolved_profile["sources"]:
        raise ConnectorShadowArtifactError(
            "real Connector Shadow source scope must exactly match the selected profile"
        )
    if (
        real_sample
        and pipeline_id == "finance.expense_evidence_review"
        and any(item["expected_record_count"] < 1 for item in sources)
    ):
        raise ConnectorShadowArtifactError(
            "real Airwallex update-capture Shadow requires at least one observed event"
        )
    if (
        real_sample
        and pipeline_id == "finance.trial_balance_review"
        and any(item["expected_record_count"] < 1 for item in sources)
    ):
        raise ConnectorShadowArtifactError(
            "real Xero Trial Balance Shadow requires at least one observed account line"
        )
    if (
        real_sample
        and pipeline_id == "commerce.shipbob_fulfillment_close"
        and any(item["expected_record_count"] < 1 for item in sources)
    ):
        raise ConnectorShadowArtifactError(
            "real ShipBob Shadow requires at least one observed fulfillment evidence record"
        )
    if (
        real_sample
        and pipeline_id == "paypal.transaction_close"
        and any(item["expected_record_count"] < 1 for item in sources)
    ):
        raise ConnectorShadowArtifactError(
            "real PayPal Shadow requires at least one observed balance-affecting transaction"
        )
    if (
        real_sample
        and pipeline_id == "woocommerce.order_refund_close"
        and any(item["expected_record_count"] < 1 for item in sources)
    ):
        raise ConnectorShadowArtifactError(
            "real WooCommerce Shadow requires at least one observed order or refund evidence record"
        )
    if (
        real_sample
        and pipeline_id == "amazon_seller.transaction_close"
        and any(item["expected_record_count"] < 1 for item in sources)
    ):
        raise ConnectorShadowArtifactError(
            "real Amazon Seller Shadow requires at least one observed Finances transaction"
        )
    if (
        real_sample
        and pipeline_id == "amazon_seller.marketplace_close"
        and any(item["expected_record_count"] < 3 for item in sources)
    ):
        raise ConnectorShadowArtifactError(
            "real Amazon Seller marketplace Shadow requires Orders, FBA Inventory and Finances evidence"
        )
    if (
        real_sample
        and pipeline_id == _DTC_SHOPIFY_STRIPE_MONTHLY_PIPELINE
        and any(item["expected_record_count"] < 1 for item in sources)
    ):
        raise ConnectorShadowArtifactError(
            "real Shopify monthly Shadow requires non-empty Shopify and Stripe month evidence"
        )
    controls = value.get("control_expectations")
    control_types = (
        PIPELINE_CONTROL_TYPES[pipeline_id]
        if real_sample else LEGACY_PIPELINE_CONTROL_TYPES[pipeline_id]
    )
    if not isinstance(controls, list) or {
        item.get("control_id") for item in controls if isinstance(item, dict)
    } != set(control_types) or len(controls) != len(control_types):
        raise ConnectorShadowArtifactError("control_expectations must cover every supported control exactly once")
    for item in controls:
        if set(item) != {"control_id", "expected_value"}:
            raise ConnectorShadowArtifactError("control expectation fields are invalid")
        expected = item["expected_value"]
        if control_types[item["control_id"]] is bool:
            if not isinstance(expected, bool):
                raise ConnectorShadowArtifactError("boolean control expected_value must be boolean")
        elif not isinstance(expected, int) or isinstance(expected, bool) or expected < 0:
            raise ConnectorShadowArtifactError("control expected_value must be a non-negative integer")
    if real_sample and pipeline_id == "finance.expense_evidence_review":
        expected_by_control = {
            item["control_id"]: item["expected_value"] for item in controls
        }
        if (
            expected_by_control["network_refetch_performed"] is not True
            or expected_by_control["webhook_refetch_basis"] is not True
        ):
            raise ConnectorShadowArtifactError(
                "real Airwallex update-capture Shadow must expect a webhook-triggered network refetch"
            )
    if real_sample and pipeline_id == "finance.trial_balance_review":
        expected_by_control = {
            item["control_id"]: item["expected_value"] for item in controls
        }
        source_count = sources[0]["expected_record_count"]
        required_true = (
            "pipeline_ready", "network_snapshot_performed", "as_at_period_end",
            "payments_only_disabled", "entity_currency_binding_matched",
            "point_in_time_snapshot", "opening_and_period_movements_absent",
            "ytd_columns_preserved_separately", "external_actions_disabled",
        )
        if (
            any(expected_by_control[item] is not True for item in required_true)
            or expected_by_control["trial_balance_line_count"] != source_count
            or expected_by_control["scope_count"] < 1
            or expected_by_control["balanced_scope_count"] != expected_by_control["scope_count"]
            or expected_by_control["unbalanced_scope_count"] != 0
            or expected_by_control["roll_forward_checked_scope_count"] != 0
        ):
            raise ConnectorShadowArtifactError(
                "real Xero Shadow must expect one clean month-end, accrual-basis, "
                "entity-bound, balanced, point-in-time network snapshot without roll-forward inference"
            )
    if real_sample and pipeline_id == "commerce.shipbob_fulfillment_close":
        expected_by_control = {
            item["control_id"]: item["expected_value"] for item in controls
        }
        required_true = (
            "pipeline_ready", "network_fulfillment_read_performed",
            "monthly_half_open_window", "entity_scope_matched",
            "customer_pii_excluded", "raw_source_ids_excluded",
            "write_api_disabled", "posting_and_inventory_actions_disabled",
        )
        source_count = sources[0]["expected_record_count"]
        component_count = sum(expected_by_control[item] for item in (
            "order_count", "shipment_count", "return_count", "return_item_count",
        ))
        if (
            any(expected_by_control[item] is not True for item in required_true)
            or expected_by_control["order_count"] < 1
            or component_count != source_count
        ):
            raise ConnectorShadowArtifactError(
                "real ShipBob Shadow must expect one clean, monthly, entity-bound network read "
                "with PII/source identifiers excluded and all write, posting and inventory actions disabled"
            )
    if real_sample and pipeline_id == "paypal.transaction_close":
        expected_by_control = {
            item["control_id"]: item["expected_value"] for item in controls
        }
        required_true = (
            "pipeline_ready", "network_transaction_search_performed",
            "oauth_exchange_in_memory", "monthly_half_open_window",
            "entity_scope_matched", "transaction_info_only",
            "customer_pii_and_free_text_excluded", "raw_source_ids_excluded",
            "business_write_posting_actions_disabled",
        )
        source_count = sources[0]["expected_record_count"]
        if (
            any(expected_by_control[item] is not True for item in required_true)
            or expected_by_control["transaction_count"] != source_count
        ):
            raise ConnectorShadowArtifactError(
                "real PayPal Shadow must expect one clean, monthly, entity-bound Transaction Search read "
                "with in-memory OAuth, minimized fields and all business writes/posting disabled"
            )
    if real_sample and pipeline_id == "woocommerce.order_refund_close":
        expected_by_control = {
            item["control_id"]: item["expected_value"] for item in controls
        }
        required_true = (
            "pipeline_ready", "network_order_refund_read_performed",
            "monthly_half_open_window", "entity_site_scope_matched",
            "customer_pii_excluded", "product_detail_excluded",
            "raw_source_ids_excluded", "fixed_read_only_transport_controls",
            "business_write_posting_revenue_tax_actions_disabled",
        )
        source_count = sources[0]["expected_record_count"]
        if (
            any(expected_by_control[item] is not True for item in required_true)
            or expected_by_control["order_count"] < 1
            or expected_by_control["order_count"] + expected_by_control["refund_event_count"]
            != source_count
            or expected_by_control["orphan_refund_count"] != 0
            or expected_by_control["arithmetic_exception_count"] != 0
        ):
            raise ConnectorShadowArtifactError(
                "real WooCommerce Shadow must expect one clean monthly entity/site-bound read with "
                "minimized customer/product data and no orphan refunds, arithmetic blockers or writes"
            )
    if real_sample and pipeline_id == "amazon_seller.transaction_close":
        expected_by_control = {
            item["control_id"]: item["expected_value"] for item in controls
        }
        required_true = (
            "pipeline_ready", "network_finances_read_performed", "lwa_exchange_in_memory",
            "monthly_half_open_window", "entity_seller_marketplace_scope_matched",
            "customer_product_store_free_text_excluded", "raw_source_ids_excluded",
            "fixed_regional_read_only_transport_controls",
            "nested_component_double_counting_disabled",
            "business_write_posting_revenue_tax_settlement_actions_disabled",
        )
        source_count = sources[0]["expected_record_count"]
        if (
            any(expected_by_control[item] is not True for item in required_true)
            or expected_by_control["transaction_count"] != source_count
            or expected_by_control["released_transaction_count"]
            + expected_by_control["deferred_transaction_count"] != source_count
        ):
            raise ConnectorShadowArtifactError(
                "real Amazon Seller Shadow must expect one clean monthly entity/seller/marketplace-bound "
                "Finances read with minimized data, in-memory LWA and all writes/accounting claims disabled"
            )
    if real_sample and pipeline_id == "amazon_seller.marketplace_close":
        expected_by_control = {
            item["control_id"]: item["expected_value"] for item in controls
        }
        required_true = (
            "pipeline_ready", "network_three_source_read_performed",
            "single_lwa_exchange_in_memory", "monthly_orders_finances_half_open_window",
            "current_inventory_not_historical_period_end",
            "entity_seller_marketplace_scope_matched",
            "buyer_recipient_product_raw_ids_excluded",
            "orders_restricted_datasets_not_requested",
            "fixed_regional_read_only_transport_controls",
            "business_write_posting_revenue_tax_inventory_actions_disabled",
        )
        source_count = sources[0]["expected_record_count"]
        if (
            any(expected_by_control[item] is not True for item in required_true)
            or expected_by_control["order_count"] < 1
            or expected_by_control["inventory_sku_count"] < 1
            or expected_by_control["transaction_count"] < 1
            or sum(expected_by_control[item] for item in (
                "order_count", "inventory_sku_count", "transaction_count",
            )) != source_count
        ):
            raise ConnectorShadowArtifactError(
                "real Amazon Seller marketplace Shadow must expect one clean monthly "
                "entity/seller/marketplace-bound three-source read with one in-memory LWA "
                "exchange, minimized data and all writes/accounting claims disabled"
            )
    if real_sample and pipeline_id == _STRIPE_PIPELINE:
        expected_by_control = {
            item["control_id"]: item["expected_value"] for item in controls
        }
        source_counts = {
            item["connector_id"]: item["expected_record_count"] for item in sources
        }
        if (
            expected_by_control["pipeline_ready"] is not True
            or source_counts.get("stripe.balance_transactions")
            != expected_by_control["balance_transaction_count"]
            or source_counts.get("stripe.payouts")
            != expected_by_control["payout_count"]
            or expected_by_control["balance_transaction_count"] < 1
            or expected_by_control["payout_count"] < 1
            or expected_by_control["payout_bank_candidate_count"]
            != expected_by_control["payout_count"]
            or expected_by_control["payout_bank_exception_count"] != 0
        ):
            raise ConnectorShadowArtifactError(
                "real Stripe Shadow must expect non-empty same-window Balance and Payout "
                "evidence with one review candidate per payout and no exceptions"
            )
    if real_sample and pipeline_id == _DTC_SHOPIFY_STRIPE_MONTHLY_PIPELINE:
        expected_by_control = {
            item["control_id"]: item["expected_value"] for item in controls
        }
        source_counts = {
            item["connector_id"]: item["expected_record_count"] for item in sources
        }
        required_true = (
            "pipeline_ready", "shopify_network_read_performed",
            "stripe_network_read_performed", "canonical_month_half_open_window",
            "close_capture_within_72_hours",
            "created_and_updated_population_contract",
            "refund_processed_at_membership",
            "refund_component_and_transaction_reconciled",
            "entity_scope_matched", "candidate_only_no_external_actions",
        )
        shopify_component_count = sum(expected_by_control[item] for item in (
            "shopify_order_count", "shopify_transaction_count", "shopify_refund_count",
        ))
        if (
            any(expected_by_control[item] is not True for item in required_true)
            or source_counts.get("shopify.monthly_order_evidence") != shopify_component_count
            or source_counts.get("stripe.balance_transactions")
            != expected_by_control["stripe_balance_transaction_count"]
            or expected_by_control["shopify_order_count"] < 1
            or expected_by_control["monthly_created_order_count"] < 1
            or expected_by_control["monthly_created_order_count"]
            > expected_by_control["created_population_count"]
            or expected_by_control["deduplicated_order_count"]
            != expected_by_control["shopify_order_count"]
            or expected_by_control["deduplicated_order_count"]
            > expected_by_control["created_population_count"]
            + expected_by_control["updated_population_count"]
            or expected_by_control["monthly_refund_event_count"]
            > expected_by_control["shopify_refund_count"]
            or expected_by_control["processor_link_matched_count"] < 1
            or expected_by_control["processor_link_exception_count"] != 0
        ):
            raise ConnectorShadowArtifactError(
                "real Shopify monthly Shadow must expect one clean close-captured calendar month, "
                "non-empty same-window processor evidence, reconciled refund components, "
                "explicit entity scope and no external actions"
            )
    (_real_evidence_references if real_sample else _references)(
        value.get("evidence_references"), "evidence_references",
    )
    return dict(value)


def _actual_controls(
    result: dict[str, Any], pipeline_id: str, *,
    expected_entity_id: str | None = None, expected_currency: str | None = None,
) -> dict[str, Any]:
    if pipeline_id == "amazon_seller.marketplace_close":
        batch = (
            (result.get("connector_batches") or {}).get("amazon_seller.marketplace_evidence")
            or {}
        )
        source = batch.get("source") or {}
        quality = batch.get("quality") or {}
        counts = quality.get("dataset_counts") or {}
        output = (
            ((result.get("services") or {}).get(
                "marketplace_evidence_reconciliation"
            ) or {}).get("output") or {}
        )
        briefing = result.get("founder_briefing") or {}
        entity_id = str((result.get("lineage") or {}).get("entity_id") or "")
        start = str(source.get("interval_start") or "").replace(".000000Z", "Z")
        end = str(source.get("interval_end") or "").replace(".000000Z", "Z")
        period = (
            start[:7]
            if re.fullmatch(r"20\d{2}-(?:0[1-9]|1[0-2])-01T00:00:00Z", start)
            else ""
        )
        if period:
            year, month = (int(part) for part in period.split("-"))
            next_period = f"{year + (month == 12):04d}-{1 if month == 12 else month + 1:02d}"
            period_window = end == f"{next_period}-01T00:00:00Z"
        else:
            period_window = False
        return {
            "pipeline_ready": result.get("ready") is True,
            "order_count": counts.get("commerce.amazon_seller_orders"),
            "inventory_sku_count": counts.get("commerce.amazon_seller_inventory"),
            "transaction_count": counts.get("commerce.amazon_seller_transactions"),
            "finance_without_order_count": (
                output.get("finance_without_order_count")
                if isinstance(output.get("finance_without_order_count"), int)
                and not isinstance(output.get("finance_without_order_count"), bool)
                else len(output.get("finance_without_order_keys") or [])
            ),
            "shipped_order_without_finance_count": (
                output.get("shipped_order_without_finance_count")
                if isinstance(output.get("shipped_order_without_finance_count"), int)
                and not isinstance(output.get("shipped_order_without_finance_count"), bool)
                else len(output.get("shipped_order_without_finance_keys") or [])
            ),
            "fba_order_sku_without_inventory_count": (
                output.get("fba_order_sku_without_inventory_count")
                if isinstance(output.get("fba_order_sku_without_inventory_count"), int)
                and not isinstance(output.get("fba_order_sku_without_inventory_count"), bool)
                else len(output.get("fba_order_sku_without_inventory_keys") or [])
            ),
            "inventory_quantity_field_missing_count": (
                output.get("inventory_quantity_field_missing_count")
                if isinstance(output.get("inventory_quantity_field_missing_count"), int)
                and not isinstance(output.get("inventory_quantity_field_missing_count"), bool)
                else len(output.get("inventory_quantity_field_missing_keys") or [])
            ),
            "network_three_source_read_performed": (
                result.get("network_access_performed") is True
                and source.get("kind") == "api"
                and source.get("name") == "amazon_seller.marketplace_evidence"
                and source.get("network_access_performed") is True
                and all(int(source.get(field) or 0) >= 1 for field in (
                    "order_page_count", "inventory_page_count", "transaction_page_count",
                ))
            ),
            "single_lwa_exchange_in_memory": (
                source.get("lwa_token_exchange_performed") is True
                and source.get("lwa_token_exchange_count") == 1
                and source.get("lwa_token_persisted") is False
            ),
            "monthly_orders_finances_half_open_window": period_window,
            "current_inventory_not_historical_period_end": (
                source.get("inventory_observation_type")
                == "current_at_fetch_not_historical_period_end"
                and output.get("current_inventory_not_historical_period_end") is True
                and briefing.get("current_inventory_not_historical_period_end") is True
            ),
            "entity_seller_marketplace_scope_matched": (
                bool(entity_id)
                and entity_id == expected_entity_id
                and output.get("entity_id") == entity_id
                and (
                    output.get("marketplace_scope_count") == 1
                    or len(output.get("marketplace_counts") or {}) == 1
                )
                and bool(re.fullmatch(
                    r"[0-9a-f]{64}", str(source.get("seller_binding_sha256") or ""),
                ))
            ),
            "buyer_recipient_product_raw_ids_excluded": (
                source.get("buyer_recipient_or_address_retained") is False
                and source.get("product_title_or_raw_identity_retained") is False
                and source.get("raw_seller_or_business_ids_retained") is False
            ),
            "orders_restricted_datasets_not_requested": (
                source.get("orders_included_data") == ["FULFILLMENT"]
                and source.get("proceeds_expense_tax_payment_or_tracking_requested") is False
            ),
            "fixed_regional_read_only_transport_controls": (
                (
                    source.get("regional_endpoint_class_valid") is True
                    or source.get("region") in {"NA", "EU", "FE"}
                )
                and isinstance(source.get("orders_role_required"), str)
                and isinstance(source.get("inventory_role_required"), str)
                and source.get("aws_sigv4_used") is False
                and source.get("fixed_regional_endpoint_used") is True
                and source.get("response_urls_followed") is False
            ),
            "business_write_posting_revenue_tax_inventory_actions_disabled": all(
                item is False for item in (
                    source.get("business_write_api_called"),
                    source.get("inventory_adjustment_performed"),
                    result.get("external_actions_performed"),
                    output.get("external_actions_performed"),
                    output.get("posting_or_inventory_adjustment_performed"),
                    output.get("inventory_valuation_or_cogs_performed"),
                    output.get("revenue_recognition_performed"),
                    output.get("tax_liability_determined"),
                    output.get("settlement_or_bank_reconciliation_performed"),
                )
            ) and all(
                briefing.get(field) is True for field in (
                    "order_or_financial_completeness_claim_prohibited",
                    "inventory_valuation_or_cogs_claim_prohibited",
                    "revenue_tax_settlement_claim_prohibited",
                )
            ),
        }
    if pipeline_id == "amazon_seller.transaction_close":
        batch = (
            (result.get("connector_batches") or {}).get("amazon_seller.transaction_activity")
            or {}
        )
        source = batch.get("source") or {}
        quality = batch.get("quality") or {}
        output = (
            ((result.get("services") or {}).get("transaction_activity_summary") or {}).get(
                "output"
            ) or {}
        )
        briefing = result.get("founder_briefing") or {}
        entity_id = str((result.get("lineage") or {}).get("entity_id") or "")
        start = str(source.get("interval_start") or "").replace(".000000Z", "Z")
        end = str(source.get("interval_end") or "").replace(".000000Z", "Z")
        period = start[:7] if re.fullmatch(r"20\d{2}-(?:0[1-9]|1[0-2])-01T00:00:00Z", start) else ""
        if period:
            year, month = (int(part) for part in period.split("-"))
            next_period = f"{year + (month == 12):04d}-{1 if month == 12 else month + 1:02d}"
            period_window = end == f"{next_period}-01T00:00:00Z"
        else:
            period_window = False
        statuses = output.get("status_counts") or {}
        return {
            "pipeline_ready": result.get("ready") is True,
            "transaction_count": quality.get("record_count"),
            "released_transaction_count": (
                int(statuses.get("RELEASED") or 0) + int(statuses.get("DEFERRED_RELEASED") or 0)
            ),
            "deferred_transaction_count": int(statuses.get("DEFERRED") or 0),
            "refund_candidate_count": len(output.get("refund_candidate_keys") or []),
            "fee_candidate_count": len(output.get("fee_candidate_keys") or []),
            "settlement_reference_missing_count": output.get("settlement_reference_missing_count"),
            "network_finances_read_performed": (
                result.get("network_access_performed") is True
                and source.get("kind") == "api"
                and source.get("name") == "amazon_seller.transaction_activity"
                and source.get("network_access_performed") is True
            ),
            "lwa_exchange_in_memory": (
                source.get("lwa_token_exchange_performed") is True
                and source.get("lwa_token_persisted") is False
            ),
            "monthly_half_open_window": period_window,
            "entity_seller_marketplace_scope_matched": (
                bool(entity_id)
                and entity_id == expected_entity_id
                and output.get("entity_id") == entity_id
                and len(output.get("marketplace_counts") or {}) == 1
                and bool(re.fullmatch(
                    r"[0-9a-f]{64}", str(source.get("seller_binding_sha256") or ""),
                ))
            ),
            "customer_product_store_free_text_excluded": all(
                source.get(field) is False for field in (
                    "customer_or_address_retained", "product_identity_or_description_retained",
                    "store_name_or_free_text_retained",
                )
            ),
            "raw_source_ids_excluded": source.get("raw_seller_or_business_ids_retained") is False,
            "fixed_regional_read_only_transport_controls": (
                source.get("region") in {"NA", "EU", "FE"}
                and source.get("finance_and_accounting_role_required") is True
                and source.get("aws_sigv4_used") is False
                and source.get("fixed_regional_endpoint_used") is True
                and source.get("response_urls_followed") is False
            ),
            "nested_component_double_counting_disabled": (
                output.get("nested_component_double_counting_prohibited") is True
            ),
            "business_write_posting_revenue_tax_settlement_actions_disabled": all(
                item is False for item in (
                    source.get("business_write_api_called"), result.get("external_actions_performed"),
                    output.get("external_actions_performed"), output.get("posting_performed"),
                    output.get("revenue_recognition_performed"), output.get("tax_liability_determined"),
                    output.get("settlement_or_bank_reconciliation_performed"),
                    output.get("inventory_or_cogs_modified"),
                )
            ) and all(
                briefing.get(field) is True for field in (
                    "revenue_claim_prohibited", "tax_liability_claim_prohibited",
                    "settlement_or_bank_reconciliation_claim_prohibited",
                )
            ),
        }
    if pipeline_id == "woocommerce.order_refund_close":
        batch = (
            (result.get("connector_batches") or {}).get("woocommerce.order_refund_activity")
            or {}
        )
        source = batch.get("source") or {}
        quality = batch.get("quality") or {}
        counts = quality.get("dataset_counts") or {}
        output = (
            ((result.get("services") or {}).get("order_refund_activity_summary") or {}).get(
                "output"
            ) or {}
        )
        briefing = result.get("founder_briefing") or {}
        entity_id = str((result.get("lineage") or {}).get("entity_id") or "")
        start = str(source.get("interval_start") or "")
        end = str(source.get("interval_end") or "")
        period = start[:7] if re.fullmatch(r"20\d{2}-(?:0[1-9]|1[0-2])-01T00:00:00Z", start) else ""
        if period:
            year, month = (int(part) for part in period.split("-"))
            next_period = f"{year + (month == 12):04d}-{1 if month == 12 else month + 1:02d}"
            period_window = end == f"{next_period}-01T00:00:00Z"
        else:
            period_window = False
        return {
            "pipeline_ready": result.get("ready") is True,
            "order_count": counts.get("commerce.woocommerce_orders"),
            "refund_event_count": counts.get("commerce.woocommerce_refunds"),
            "orphan_refund_count": len(output.get("orphan_refund_keys") or []),
            "arithmetic_exception_count": len(output.get("arithmetic_exception_keys") or []),
            "destination_review_required_count": output.get("destination_review_required_count"),
            "unpaid_or_unconfirmed_order_count": output.get("unpaid_or_unconfirmed_order_count"),
            "network_order_refund_read_performed": (
                result.get("network_access_performed") is True
                and source.get("kind") == "api"
                and source.get("name") == "woocommerce.order_refund_activity"
                and source.get("network_access_performed") is True
            ),
            "monthly_half_open_window": period_window,
            "entity_site_scope_matched": (
                bool(entity_id)
                and entity_id == expected_entity_id
                and output.get("entity_id") == entity_id
                and bool(re.fullmatch(r"[0-9a-f]{64}", str(source.get("site_binding_sha256") or "")))
            ),
            "customer_pii_excluded": all(
                source.get(field) is False for field in (
                    "customer_identity_retained", "address_retained",
                    "customer_ip_or_user_agent_retained", "customer_note_or_metadata_retained",
                )
            ),
            "product_detail_excluded": source.get("product_identity_or_name_retained") is False,
            "raw_source_ids_excluded": source.get("raw_source_ids_retained") is False,
            "fixed_read_only_transport_controls": (
                source.get("read_only_key_required") is True
                and source.get("basic_auth_header_used") is True
                and source.get("query_string_credentials_used") is False
                and source.get("link_headers_followed") is False
            ),
            "business_write_posting_revenue_tax_actions_disabled": all(
                item is False for item in (
                    source.get("business_write_api_called"), result.get("external_actions_performed"),
                    output.get("external_actions_performed"), output.get("posting_performed"),
                    output.get("revenue_recognition_performed"), output.get("tax_liability_determined"),
                    output.get("inventory_or_cogs_modified"),
                )
            ) and all(
                briefing.get(field) is True for field in (
                    "revenue_claim_prohibited", "tax_liability_claim_prohibited",
                    "payment_settlement_claim_prohibited",
                )
            ),
        }
    if pipeline_id == "commerce.shipbob_fulfillment_close":
        batch = (result.get("connector_batches") or {}).get("shipbob.fulfillment") or {}
        source = batch.get("source") or {}
        quality = batch.get("quality") or {}
        counts = quality.get("dataset_counts") or {}
        output = (
            ((result.get("services") or {}).get(
                "fulfillment_and_return_evidence_summary"
            ) or {}).get("output") or {}
        )
        entity_id = str((result.get("lineage") or {}).get("entity_id") or "")
        start = str(source.get("interval_start") or "")
        end = str(source.get("interval_end") or "")
        period = start[:7] if re.fullmatch(r"20\d{2}-(?:0[1-9]|1[0-2])-01T00:00:00Z", start) else ""
        if period:
            year, month = (int(part) for part in period.split("-"))
            next_period = f"{year + (month == 12):04d}-{1 if month == 12 else month + 1:02d}"
            period_window = end == f"{next_period}-01T00:00:00Z"
        else:
            period_window = False
        briefing = result.get("founder_briefing") or {}
        return {
            "pipeline_ready": result.get("ready") is True,
            "order_count": counts.get("commerce.shipbob_orders"),
            "shipment_count": counts.get("commerce.shipbob_shipments"),
            "return_count": counts.get("commerce.shipbob_returns"),
            "return_item_count": counts.get("commerce.shipbob_return_items"),
            "unfulfilled_order_count": (
                (output.get("order_fulfillment") or {}).get("orders_without_shipments")
            ),
            "unprocessed_return_item_count": (
                output.get("unprocessed_return_item_count")
                if isinstance(output.get("unprocessed_return_item_count"), int)
                and not isinstance(output.get("unprocessed_return_item_count"), bool)
                else len(output.get("unprocessed_return_items") or [])
            ),
            "cross_window_return_reference_count": (
                output.get("cross_window_return_reference_count")
                if isinstance(output.get("cross_window_return_reference_count"), int)
                and not isinstance(output.get("cross_window_return_reference_count"), bool)
                else len(output.get("cross_window_return_references") or [])
            ),
            "network_fulfillment_read_performed": (
                result.get("network_access_performed") is True
                and source.get("kind") == "api"
                and source.get("name") == "shipbob.fulfillment"
                and source.get("network_access_performed") is True
            ),
            "monthly_half_open_window": period_window,
            "entity_scope_matched": (
                bool(entity_id)
                and entity_id == expected_entity_id
                and output.get("entity_id") == entity_id
            ),
            "customer_pii_excluded": (
                source.get("customer_identity_retained") is False
                and source.get("customer_address_retained") is False
                and source.get("raw_tracking_number_retained") is False
                and output.get("customer_pii_required") is False
            ),
            "raw_source_ids_excluded": source.get("raw_source_ids_retained") is False,
            "write_api_disabled": source.get("write_api_called") is False,
            "posting_and_inventory_actions_disabled": (
                result.get("external_actions_performed") is False
                and output.get("external_actions_performed") is False
                and output.get("posting_performed") is False
                and output.get("inventory_adjustment_performed") is False
                and briefing.get("revenue_claim_prohibited") is True
                and briefing.get("inventory_adjustment_claim_prohibited") is True
            ),
        }
    if pipeline_id == "paypal.transaction_close":
        batch = (result.get("connector_batches") or {}).get("paypal.transaction_activity") or {}
        source = batch.get("source") or {}
        quality = batch.get("quality") or {}
        output = (
            ((result.get("services") or {}).get("transaction_activity_summary") or {}).get("output")
            or {}
        )
        entity_id = str((result.get("lineage") or {}).get("entity_id") or "")
        start = str(source.get("interval_start") or "")
        end = str(source.get("interval_end") or "")
        period = start[:7] if re.fullmatch(r"20\d{2}-(?:0[1-9]|1[0-2])-01T00:00:00Z", start) else ""
        if period:
            year, month = (int(part) for part in period.split("-"))
            next_period = f"{year + (month == 12):04d}-{1 if month == 12 else month + 1:02d}"
            period_window = end == f"{next_period}-01T00:00:00Z"
        else:
            period_window = False
        return {
            "pipeline_ready": result.get("ready") is True,
            "transaction_count": quality.get("record_count"),
            "refund_candidate_count": output.get("refund_candidate_count"),
            "reversal_candidate_count": output.get("reversal_candidate_count"),
            "reference_review_required_count": output.get("reference_review_required_count"),
            "cross_currency_fee_count": output.get("cross_currency_fee_count"),
            "network_transaction_search_performed": (
                result.get("network_access_performed") is True
                and source.get("kind") == "api"
                and source.get("name") == "paypal.transaction_activity"
                and source.get("network_access_performed") is True
            ),
            "oauth_exchange_in_memory": (
                source.get("oauth_token_exchange_performed") is True
                and source.get("oauth_token_persisted") is False
            ),
            "monthly_half_open_window": period_window,
            "entity_scope_matched": (
                bool(entity_id)
                and entity_id == expected_entity_id
                and output.get("entity_id") == entity_id
            ),
            "transaction_info_only": (
                source.get("query_fields") == "transaction_info"
                and source.get("balance_affecting_records_only") is True
            ),
            "customer_pii_and_free_text_excluded": all(
                source.get(field) is False for field in (
                    "payer_identity_retained", "shipping_address_retained",
                    "cart_or_item_detail_retained", "free_text_retained",
                )
            ),
            "raw_source_ids_excluded": source.get("raw_source_ids_retained") is False,
            "business_write_posting_actions_disabled": all(
                item is False for item in (
                    source.get("business_write_api_called"), result.get("external_actions_performed"),
                    output.get("external_actions_performed"), output.get("posting_performed"),
                    output.get("revenue_recognition_performed"),
                    output.get("refund_accounting_performed"),
                    output.get("bank_reconciliation_performed"), output.get("cash_allocation_performed"),
                )
            ),
        }
    if pipeline_id == "finance.expense_evidence_review":
        output = (
            ((result.get("services") or {}).get("expense_evidence_review") or {}).get("output")
            or {}
        )
        batch_source = (result.get("batch") or {}).get("source") or {}
        return {
            "pipeline_ready": result.get("ready") is True,
            "expense_record_count": output.get("record_count"),
            "receipt_missing_count": output.get("receipt_missing_count"),
            "business_purpose_missing_count": output.get("business_purpose_missing_count"),
            "uncleared_count": output.get("uncleared_count"),
            "accounting_mapping_missing_count": output.get("accounting_mapping_missing_count"),
            "state_change_candidate_count": output.get("state_change_count"),
            "network_refetch_performed": (
                result.get("network_access_performed") is True
                and batch_source.get("network_access_performed") is True
                and batch_source.get("name") == "airwallex.expense_refetch"
            ),
            "webhook_refetch_basis": (
                batch_source.get("update_capture_basis")
                == "signed_webhook_then_read_only_refetch"
                and batch_source.get("webhook_context_validated") is True
                and isinstance(batch_source.get("webhook_context_count"), int)
                and batch_source.get("webhook_context_count") > 0
            ),
            "external_actions_disabled": all(
                item is False for item in (
                    result.get("external_actions_performed"),
                    result.get("expense_claims_created"), result.get("posting_performed"),
                    result.get("payment_performed"), output.get("external_actions_performed"),
                )
            ),
        }
    if pipeline_id == "finance.bank_statement_close":
        output = (
            ((result.get("services") or {}).get("bank_reconciliation_candidate") or {}).get(
                "output"
            )
            or {}
        )
        source = (result.get("batch") or {}).get("source") or {}
        rows = (
            ((result.get("batch") or {}).get("datasets") or {}).get(
                "finance.bank_transactions"
            )
            or []
        )
        accounts = output.get("accounts") or []
        if (
            not isinstance(rows, list) or any(not isinstance(item, dict) for item in rows)
            or not isinstance(accounts, list)
            or any(not isinstance(item, dict) for item in accounts)
        ):
            raise ConnectorShadowArtifactError("Wise Pipeline result controls are malformed")
        entity_id = str((result.get("lineage") or {}).get("entity_id") or "")
        period = str((result.get("lineage") or {}).get("period") or "")
        if re.fullmatch(r"20\d{2}-(?:0[1-9]|1[0-2])", period):
            year, month = (int(part) for part in period.split("-"))
            next_period = f"{year + (month == 12):04d}-{1 if month == 12 else month + 1:02d}"
            period_window = (
                source.get("interval_start") == f"{period}-01T00:00:00Z"
                and source.get("interval_end") == f"{next_period}-01T00:00:00Z"
            )
        else:
            period_window = False
        currency = str(source.get("currency") or "")
        scoped = (
            bool(rows) and bool(accounts)
            and entity_id == expected_entity_id
            and currency == expected_currency
            and output.get("entity_id") == entity_id
            and output.get("period") == period
            and all(
                item.get("entity_id") == entity_id
                and item.get("currency") == currency
                and str(item.get("transaction_date") or "")[:7] == period
                for item in rows
            )
            and all(item.get("currency") == currency for item in accounts)
        )
        return {
            "pipeline_ready": result.get("ready") is True,
            "bank_transaction_count": len(rows),
            "account_scope_count": len(accounts),
            "pending_transaction_count": output.get("pending_count"),
            "network_statement_performed": (
                result.get("network_access_performed") is True
                and source.get("kind") == "api"
                and source.get("name") == "wise.balance_statement"
                and source.get("network_access_performed") is True
            ),
            "monthly_half_open_window": period_window,
            "entity_currency_binding_matched": (
                scoped
                and source.get("entity_binding_verified") is True
                and bool(re.fullmatch(r"[0-9a-f]{16}", str(source.get("profile_binding_hash") or "")))
                and bool(re.fullmatch(r"[0-9a-f]{16}", str(source.get("balance_binding_hash") or "")))
            ),
            "business_profile_verified": source.get("entity_binding_verified") is True,
            "compact_english_statement": (
                source.get("statement_type") == "COMPACT"
                and source.get("statement_locale") == "en"
            ),
            "opening_closing_balance_controls_present": (
                source.get("opening_closing_balance_controls_present") is True
                or (
                    isinstance(source.get("opening_balance"), (int, float))
                    and not isinstance(source.get("opening_balance"), bool)
                    and isinstance(source.get("closing_balance"), (int, float))
                    and not isinstance(source.get("closing_balance"), bool)
                )
            ),
            "reconciliation_candidate_only": (
                output.get("output_status") == "candidate_reconciliation"
                and output.get("review_required") is True
                and (result.get("founder_briefing") or {}).get("candidate_only") is True
            ),
            "bank_balance_unconfirmed_without_review": (
                output.get("complete") is False
                and output.get("full_ledger_reconciliation_completed") is False
                and all(
                    item.get("confirmed") is False and item.get("review_current") is False
                    for item in accounts
                )
            ),
            "external_actions_disabled": (
                result.get("external_actions_performed") is False
                and (result.get("founder_briefing") or {}).get(
                    "posting_or_cash_allocation_performed"
                ) is False
            ),
        }
    if pipeline_id == "finance.trial_balance_review":
        output = (
            ((result.get("services") or {}).get("trial_balance_validation") or {}).get("output")
            or {}
        )
        source = (result.get("batch") or {}).get("source") or {}
        rows = (
            ((result.get("batch") or {}).get("datasets") or {}).get(
                "finance.trial_balance_lines"
            ) or []
        )
        summaries = output.get("summaries") or []
        if (
            not isinstance(rows, list) or any(not isinstance(item, dict) for item in rows)
            or not isinstance(summaries, list)
            or any(not isinstance(item, dict) for item in summaries)
        ):
            raise ConnectorShadowArtifactError("Xero Pipeline result controls are malformed")
        entity_id = str(output.get("entity_id") or "")
        period = str((result.get("lineage") or {}).get("period") or "")
        runtime_currency = str(source.get("base_currency") or "")
        scoped = (
            isinstance(rows, list) and bool(rows)
            and isinstance(summaries, list) and bool(summaries)
            and entity_id == expected_entity_id
            and runtime_currency == expected_currency
            and all(
                isinstance(item, dict)
                and item.get("entity_id") == entity_id
                and item.get("period") == period
                and item.get("currency") == runtime_currency
                for item in rows + summaries
            )
        )
        return {
            "pipeline_ready": result.get("ready") is True,
            "trial_balance_line_count": len(rows) if isinstance(rows, list) else None,
            "scope_count": len(summaries) if isinstance(summaries, list) else None,
            "balanced_scope_count": (
                sum(item.get("balanced") is True for item in summaries)
                if isinstance(summaries, list) else None
            ),
            "unbalanced_scope_count": (
                sum(item.get("balanced") is not True for item in summaries)
                if isinstance(summaries, list) else None
            ),
            "roll_forward_checked_scope_count": (
                sum(item.get("roll_forward_checked") is True for item in summaries)
                if isinstance(summaries, list) else None
            ),
            "network_snapshot_performed": (
                result.get("network_access_performed") is True
                and source.get("kind") == "api"
                and source.get("name") == "xero.trial_balance"
                and source.get("network_access_performed") is True
            ),
            "as_at_period_end": (
                bool(re.fullmatch(r"20\d{2}-(?:0[1-9]|1[0-2])", period))
                and source.get("as_at") == _period_end(period)
            ),
            "payments_only_disabled": source.get("payments_only") is False,
            "entity_currency_binding_matched": (
                scoped
                and bool(re.fullmatch(r"[0-9a-f]{16}", str(source.get("tenant_binding_hash") or "")))
                and bool(re.fullmatch(r"[0-9a-f]{16}", str(source.get("organisation_binding_hash") or "")))
            ),
            "point_in_time_snapshot": source.get("point_in_time_snapshot") is True,
            "opening_and_period_movements_absent": (
                source.get("opening_and_period_movements_provided") is False
            ),
            "ytd_columns_preserved_separately": (
                source.get("ytd_columns_preserved_separately") is True
            ),
            "external_actions_disabled": all(
                item is False for item in (
                    result.get("external_actions_performed"),
                    result.get("ledger_or_opening_balances_modified"),
                    result.get("posting_performed"), result.get("period_close_performed"),
                    output.get("ledger_or_opening_balances_modified"),
                    output.get("posting_performed"),
                )
            ),
        }
    services = result.get("services") or {}
    if pipeline_id == _DTC_SHOPIFY_STRIPE_MONTHLY_PIPELINE:
        batches = result.get("connector_batches") or {}
        shopify_batch = batches.get("shopify.monthly_order_evidence") or {}
        stripe_batch = batches.get("stripe.balance_transactions") or {}
        shopify_source = shopify_batch.get("source") or {}
        stripe_source = stripe_batch.get("source") or {}
        shopify_quality = shopify_batch.get("quality") or {}
        stripe_quality = stripe_batch.get("quality") or {}
        shopify_counts = shopify_quality.get("dataset_counts") or {}
        monthly_output = (
            (services.get("shopify_monthly_commerce_scope") or {}).get("output") or {}
        )
        processor_output = (
            (services.get("shopify_stripe_activity_reconciliation") or {}).get("output") or {}
        )
        monthly_rows = monthly_output.get("monthly_commerce_scope") or []
        processor_rows = processor_output.get("reconciliation") or []
        def nonnegative_int(value: Any) -> bool:
            return (
                isinstance(value, int)
                and not isinstance(value, bool)
                and value >= 0
            )

        def sum_row_count(rows: Any, field: str) -> int | None:
            if (
                not isinstance(rows, list)
                or any(
                    not isinstance(item, dict)
                    or not nonnegative_int(item.get(field))
                    for item in rows
                )
            ):
                return None
            return sum(item[field] for item in rows)

        processor_rows_valid = (
            isinstance(processor_rows, list)
            and all(isinstance(item, dict) for item in processor_rows)
        )
        lineage = result.get("lineage") or {}
        entity_id = str(lineage.get("entity_id") or "")
        period = str(lineage.get("period") or "")
        start = str(shopify_source.get("interval_start") or "")
        end = str(shopify_source.get("interval_end") or "")
        canonical_window = False
        close_capture = False
        try:
            start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
            end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
            observed_dt = datetime.fromisoformat(
                str(shopify_source.get("source_observed_at") or "").replace("Z", "+00:00")
            )
            year, month = (int(part) for part in period.split("-"))
            next_period = f"{year + (month == 12):04d}-{1 if month == 12 else month + 1:02d}"
            stripe_window = stripe_source.get("created_window") or {}
            canonical_window = (
                start == f"{period}-01T00:00:00Z"
                and end == f"{next_period}-01T00:00:00Z"
                and lineage.get("canonical_month_scope") is True
                and lineage.get("interval_start") == start
                and lineage.get("interval_end") == end
                and stripe_window.get("complete_bounds_declared") is True
                and stripe_window.get("semantics") == "half_open_unix_seconds"
                and stripe_window.get("gte") == int(start_dt.timestamp())
                and stripe_window.get("lt") == int(end_dt.timestamp())
            )
            close_capture = (
                start_dt.tzinfo is not None and end_dt.tzinfo is not None
                and observed_dt.tzinfo is not None
                and end_dt <= observed_dt <= end_dt + timedelta(hours=72)
                and shopify_source.get("close_capture_deadline_hours") == 72
            )
        except (TypeError, ValueError):
            pass
        refund_review_count = monthly_output.get("refund_review_count")
        if refund_review_count is None:
            reviews = monthly_output.get("refund_reviews") or []
            refund_review_count = len(reviews) if isinstance(reviews, list) else None
        created_order_count = sum_row_count(monthly_rows, "created_order_count")
        refund_event_count = sum_row_count(monthly_rows, "refund_event_count")
        return {
            "pipeline_ready": result.get("ready") is True,
            "shopify_order_count": shopify_counts.get("commerce.shopify_orders"),
            "shopify_transaction_count": shopify_counts.get("commerce.shopify_transactions"),
            "shopify_refund_count": shopify_counts.get("commerce.shopify_refunds"),
            "stripe_balance_transaction_count": stripe_quality.get("record_count"),
            "created_population_count": shopify_source.get("created_population_count"),
            "updated_population_count": shopify_source.get(
                "updated_since_month_start_population_count"
            ),
            "deduplicated_order_count": shopify_source.get("deduplicated_order_count"),
            "monthly_created_order_count": created_order_count,
            "monthly_refund_event_count": refund_event_count,
            "processor_link_matched_count": sum(
                item.get("status") == "matched" for item in processor_rows
            ) if processor_rows_valid else None,
            "processor_link_exception_count": sum(
                item.get("status") != "matched" for item in processor_rows
            ) if processor_rows_valid else None,
            "shopify_network_read_performed": (
                result.get("network_access_performed") is True
                and shopify_source.get("kind") == "api"
                and shopify_source.get("name") == "shopify.monthly_order_evidence"
                and shopify_source.get("network_access_performed") is True
                and nonnegative_int(shopify_source.get("created_page_count"))
                and shopify_source.get("created_page_count") >= 1
                and nonnegative_int(shopify_source.get("updated_page_count"))
                and shopify_source.get("updated_page_count") >= 1
            ),
            "stripe_network_read_performed": (
                result.get("network_access_performed") is True
                and stripe_source.get("kind") == "api"
                and stripe_source.get("name") == "stripe.balance_transactions"
                and stripe_source.get("network_access_performed") is True
            ),
            "canonical_month_half_open_window": canonical_window,
            "close_capture_within_72_hours": close_capture,
            "created_and_updated_population_contract": (
                nonnegative_int(shopify_source.get("created_population_count"))
                and nonnegative_int(
                    shopify_source.get("updated_since_month_start_population_count")
                )
                and nonnegative_int(shopify_source.get("deduplicated_order_count"))
                and shopify_source.get("updated_population_upper_bound_is_source_observed_at")
                is True
                and shopify_source.get("deduplicated_order_count")
                == shopify_counts.get("commerce.shopify_orders")
            ),
            "refund_processed_at_membership": (
                shopify_source.get("refund_event_membership_uses_processed_at") is True
                and monthly_output.get("order_and_refund_period_scope_aligned") is True
            ),
            "refund_component_and_transaction_reconciled": (
                monthly_output.get("ready") is True
                and not (monthly_output.get("blockers") or [])
                and refund_review_count == refund_event_count
            ),
            "entity_scope_matched": (
                bool(entity_id) and entity_id == expected_entity_id
                and monthly_output.get("entity_id") == entity_id
                and processor_output.get("entity_id") == entity_id
                and isinstance(monthly_rows, list) and bool(monthly_rows)
                and processor_rows_valid and bool(processor_rows)
                and lineage.get("processor_link_evidence_count") == len(processor_rows)
                and all(
                    isinstance(item, dict)
                    and item.get("entity_id") == entity_id
                    and item.get("period") == period
                    for item in monthly_rows
                )
            ),
            "candidate_only_no_external_actions": (
                result.get("external_actions_performed") is False
                and monthly_output.get("raw_source_records_returned") is False
                and monthly_output.get("revenue_recognition_performed") is False
                and monthly_output.get("posting_performed") is False
                and processor_output.get("candidate_only") is True
                and processor_output.get("revenue_recognition_performed") is False
                and processor_output.get("posting_performed") is False
                and (result.get("founder_briefing") or {}).get("candidate_only") is True
            ),
        }
    if pipeline_id == _STRIPE_PIPELINE:
        batches = result.get("connector_batches") or {}
        balance_batch = batches.get("stripe.balance_transactions") or {}
        payout_batch = batches.get("stripe.payouts") or {}
        payouts = (
            (services.get("payout_bank_reconciliation") or {}).get("output") or {}
        )
        payout_rows = payouts.get("reconciliation") or []
        if not isinstance(payout_rows, list):
            raise ConnectorShadowArtifactError(
                "Stripe Pipeline result is missing payout controls"
            )
        return {
            "pipeline_ready": result.get("ready") is True,
            "balance_transaction_count": (
                (balance_batch.get("quality") or {}).get("record_count")
            ),
            "payout_count": (payout_batch.get("quality") or {}).get("record_count"),
            "payout_bank_candidate_count": sum(
                row.get("reconciliation_status") in {
                    "high_confidence_candidate", "review_candidate",
                }
                for row in payout_rows
            ),
            "payout_bank_exception_count": len(payouts.get("exceptions") or []),
        }
    processor = (services.get("shopify_stripe_activity_reconciliation") or {}).get("output") or {}
    payouts = (services.get("stripe_payout_bank_reconciliation") or {}).get("output") or {}
    processor_rows = processor.get("reconciliation") or []
    payout_rows = payouts.get("reconciliation") or []
    if not isinstance(processor_rows, list) or not isinstance(payout_rows, list):
        raise ConnectorShadowArtifactError("Pipeline result is missing reconciliation controls")
    return {
        "pipeline_ready": result.get("ready") is True,
        "processor_link_matched_count": sum(row.get("status") == "matched" for row in processor_rows),
        "processor_link_exception_count": sum(row.get("status") != "matched" for row in processor_rows),
        "payout_bank_candidate_count": sum(
            row.get("reconciliation_status") in {"high_confidence_candidate", "review_candidate"}
            for row in payout_rows
        ),
        "payout_bank_exception_count": len(payouts.get("exceptions") or []),
    }


def write_stripe_shadow_observation(
    runtime: BoxRuntime,
    pipeline_result: dict[str, Any],
    output: str | Path,
) -> dict[str, Any]:
    """Persist only amount-, bank-reference- and raw-id-free Stripe close controls."""
    if not isinstance(pipeline_result, dict):
        raise ConnectorShadowArtifactError(
            "Stripe Shadow observation requires a Pipeline result"
        )
    pipeline = pipeline_result.get("pipeline") or {}
    if pipeline.get("pipeline_id") != _STRIPE_PIPELINE:
        raise ConnectorShadowArtifactError(
            "Stripe Shadow observation requires the stripe.daily_close Pipeline"
        )
    lineage = pipeline_result.get("lineage") or {}
    entity_id = str(lineage.get("entity_id") or "")
    runtime.require_entity(entity_id)
    batches = pipeline_result.get("connector_batches") or {}
    balance_batch = batches.get("stripe.balance_transactions") or {}
    payout_batch = batches.get("stripe.payouts") or {}
    expected_datasets = {
        "stripe.balance_transactions": "payments.stripe_balance_transactions",
        "stripe.payouts": "payments.stripe_payouts",
    }

    safe_source_fields = (
        "kind", "name", "network_access_performed", "api_version", "page_count",
        "retry_count", "rate_limit_count", "retry_delay_seconds_total",
        "retry_after_honored", "created_window",
    )
    safe_batches: dict[str, dict[str, Any]] = {}
    declared_windows: list[dict[str, Any]] = []
    for connector_id, batch in (
        ("stripe.balance_transactions", balance_batch),
        ("stripe.payouts", payout_batch),
    ):
        source = batch.get("source") or {}
        quality = batch.get("quality") or {}
        dataset_counts = quality.get("dataset_counts") or {}
        created_window = source.get("created_window") or {}
        if (
            source.get("kind") != "api"
            or source.get("name") != connector_id
            or source.get("network_access_performed") is not True
            or not re.fullmatch(
                r"20\d{2}-\d{2}-\d{2}(?:\.[a-z][a-z0-9_]*)?",
                str(source.get("api_version") or ""),
            )
            or any(
                not isinstance(source.get(field), int)
                or isinstance(source.get(field), bool)
                or source[field] < minimum
                for field, minimum in (
                    ("page_count", 1), ("retry_count", 0), ("rate_limit_count", 0),
                )
            )
            or not isinstance(source.get("retry_delay_seconds_total"), (int, float))
            or isinstance(source.get("retry_delay_seconds_total"), bool)
            or source["retry_delay_seconds_total"] < 0
            or not isinstance(source.get("retry_after_honored"), bool)
            or set(created_window) != {
                "gte", "lt", "semantics", "complete_bounds_declared",
            }
            or not isinstance(created_window.get("gte"), int)
            or isinstance(created_window.get("gte"), bool)
            or not isinstance(created_window.get("lt"), int)
            or isinstance(created_window.get("lt"), bool)
            or created_window["gte"] < 0
            or created_window["lt"] <= created_window["gte"]
            or created_window.get("semantics") != "half_open_unix_seconds"
            or created_window.get("complete_bounds_declared") is not True
        ):
            raise ConnectorShadowArtifactError(
                f"Stripe Shadow observation requires a complete real {connector_id} source window"
            )
        if (
            quality.get("ready") is not True
            or quality.get("rejected_count") != 0
            or not isinstance(quality.get("record_count"), int)
            or isinstance(quality.get("record_count"), bool)
            or quality.get("record_count") < 1
            or set(dataset_counts) != {expected_datasets[connector_id]}
            or any(
                not isinstance(count, int) or isinstance(count, bool) or count < 0
                for count in dataset_counts.values()
            )
            or quality.get("record_count") != sum(dataset_counts.values())
        ):
            raise ConnectorShadowArtifactError(
                f"Stripe Shadow observation requires a clean {connector_id} batch"
            )
        declared_windows.append(dict(created_window))
        safe_batches[connector_id] = {
            "source": {
                key: source[key] for key in safe_source_fields if key in source
            },
            "quality": {
                "ready": True,
                "record_count": quality["record_count"],
                "dataset_counts": dict(dataset_counts),
                "rejected_count": 0,
            },
        }
    if declared_windows[0] != declared_windows[1]:
        raise ConnectorShadowArtifactError(
            "Stripe Balance Transaction and Payout source windows must match exactly"
        )
    if (
        pipeline_result.get("ready") is not True
        or pipeline_result.get("blocked_at") is not None
        or pipeline_result.get("network_access_performed") is not True
        or pipeline_result.get("external_actions_performed") is not False
    ):
        raise ConnectorShadowArtifactError(
            "Stripe Shadow observation requires a clean read-only network Pipeline result"
        )

    services = pipeline_result.get("services") or {}
    balance_output = ((services.get("balance_activity_summary") or {}).get("output") or {})
    payout_output = (
        (services.get("payout_bank_reconciliation") or {}).get("output") or {}
    )
    payout_rows = payout_output.get("reconciliation") or []
    exceptions = payout_output.get("exceptions") or []
    allowed_statuses = {"high_confidence_candidate", "review_candidate"}
    bank_evidence_count = lineage.get("bank_evidence_count")
    if (
        balance_output.get("ready") is not True
        or balance_output.get("entity_id") != entity_id
        or balance_output.get("posting_performed") is not False
        or balance_output.get("revenue_recognition_performed") is not False
        or payout_output.get("ready") is not True
        or payout_output.get("ready_for_review") is not True
        or payout_output.get("entity_id") != entity_id
        or not isinstance(payout_rows, list)
        or not payout_rows
        or any(
            not isinstance(row, dict)
            or row.get("reconciliation_status") not in allowed_statuses
            for row in payout_rows
        )
        or len(payout_rows) != payout_batch["quality"]["record_count"]
        or not isinstance(exceptions, list)
        or exceptions
        or payout_output.get("candidate_only") is not True
        or payout_output.get("bank_reconciliation_completed") is not False
        or payout_output.get("posting_performed") is not False
        or payout_output.get("period_close_performed") is not False
        or not isinstance(bank_evidence_count, int)
        or isinstance(bank_evidence_count, bool)
        or bank_evidence_count < len(payout_rows)
        or (pipeline_result.get("founder_briefing") or {}).get("candidate_only") is not True
    ):
        raise ConnectorShadowArtifactError(
            "Stripe Shadow observation requires complete candidate-only payout controls"
        )

    observation_core = {
        "schema_version": 1,
        "artifact_type": "stripe_connector_shadow_observation",
        "runtime_fingerprint": runtime.snapshot()["fingerprint"],
        "private_pipeline_result_sha256": _hash(pipeline_result),
        "pipeline": {
            key: pipeline.get(key) for key in ("pipeline_id", "run_id", "executed_at")
        },
        "ready": True,
        "blocked_at": None,
        "lineage": {
            "entity_id": entity_id,
            "bank_evidence_count": bank_evidence_count,
        },
        "connector_batches": safe_batches,
        "services": {
            "balance_activity_summary": {"output": {
                "ready": True,
                "entity_id": entity_id,
                "posting_performed": False,
                "revenue_recognition_performed": False,
            }},
            "payout_bank_reconciliation": {"output": {
                "ready": True,
                "ready_for_review": True,
                "entity_id": entity_id,
                "reconciliation": [
                    {"reconciliation_status": row.get("reconciliation_status")}
                    for row in payout_rows
                ],
                "exceptions": [],
                "candidate_only": True,
                "bank_reconciliation_completed": False,
                "posting_performed": False,
                "period_close_performed": False,
            }},
        },
        "founder_briefing": {"candidate_only": True},
        "network_access_performed": True,
        "external_actions_performed": False,
        "posting_performed": False,
        "payment_performed": False,
        "bank_reconciliation_performed": False,
        "period_close_performed": False,
        "raw_source_ids_included": False,
        "bank_references_included": False,
        "financial_amounts_included": False,
        "private_source_evidence_required_separately": True,
    }
    observation = {
        **observation_core,
        "observation_fingerprint": _hash(observation_core),
    }
    destination = _write_private(output, observation)
    return {
        "output": str(destination),
        "observation_fingerprint": observation["observation_fingerprint"],
        "private_pipeline_result_sha256": observation["private_pipeline_result_sha256"],
        "entity_id": entity_id,
        "balance_transaction_count": balance_batch["quality"]["record_count"],
        "payout_count": payout_batch["quality"]["record_count"],
        "payout_bank_candidate_count": len(payout_rows),
        "pipeline_ready": True,
        "raw_source_values_returned": False,
        "bank_references_returned": False,
        "financial_amounts_returned": False,
        "external_actions_performed": False,
    }


def write_shopify_stripe_monthly_shadow_observation(
    runtime: BoxRuntime,
    pipeline_result: dict[str, Any],
    output: str | Path,
) -> dict[str, Any]:
    """Persist amount-, store- and raw-id-free controls from one real monthly close capture."""
    if not isinstance(pipeline_result, dict):
        raise ConnectorShadowArtifactError(
            "Shopify monthly Shadow observation requires a Pipeline result"
        )
    pipeline = pipeline_result.get("pipeline") or {}
    if pipeline.get("pipeline_id") != _DTC_SHOPIFY_STRIPE_MONTHLY_PIPELINE:
        raise ConnectorShadowArtifactError(
            "Shopify monthly Shadow observation requires the monthly close Pipeline"
        )
    lineage = pipeline_result.get("lineage") or {}
    entity_id = str(lineage.get("entity_id") or "")
    runtime.require_entity(entity_id)
    period = _period(lineage.get("period"))
    batches = pipeline_result.get("connector_batches") or {}
    shopify_batch = batches.get("shopify.monthly_order_evidence") or {}
    stripe_batch = batches.get("stripe.balance_transactions") or {}
    shopify_source = shopify_batch.get("source") or {}
    stripe_source = stripe_batch.get("source") or {}
    if not (
        pipeline_result.get("network_access_performed") is True
        and shopify_source.get("kind") == "api"
        and shopify_source.get("name") == "shopify.monthly_order_evidence"
        and shopify_source.get("network_access_performed") is True
        and stripe_source.get("kind") == "api"
        and stripe_source.get("name") == "stripe.balance_transactions"
        and stripe_source.get("network_access_performed") is True
    ):
        raise ConnectorShadowArtifactError(
            "Shopify monthly Shadow observation requires both real read-only network sources"
        )
    private_batches = {
        "shopify.monthly_order_evidence": shopify_batch,
        "stripe.balance_transactions": stripe_batch,
    }
    for connector_id, batch in private_batches.items():
        quality = batch.get("quality") or {}
        dataset_counts = quality.get("dataset_counts") or {}
        if (
            quality.get("ready") is not True
            or quality.get("rejected_count") != 0
            or not isinstance(quality.get("record_count"), int)
            or isinstance(quality.get("record_count"), bool)
            or quality.get("record_count") < 1
            or not isinstance(dataset_counts, dict)
            or any(
                not isinstance(count, int) or isinstance(count, bool) or count < 0
                for count in dataset_counts.values()
            )
            or quality.get("record_count") != sum(dataset_counts.values())
        ):
            raise ConnectorShadowArtifactError(
                f"Shopify monthly Shadow observation requires a clean {connector_id} batch"
            )
    actual_controls = _actual_controls(
        pipeline_result,
        _DTC_SHOPIFY_STRIPE_MONTHLY_PIPELINE,
        expected_entity_id=entity_id,
        expected_currency=runtime.entities.get(entity_id).functional_currency.upper(),
    )
    required_true = (
        "pipeline_ready", "shopify_network_read_performed",
        "stripe_network_read_performed", "canonical_month_half_open_window",
        "close_capture_within_72_hours", "created_and_updated_population_contract",
        "refund_processed_at_membership", "refund_component_and_transaction_reconciled",
        "entity_scope_matched", "candidate_only_no_external_actions",
    )
    if (
        any(actual_controls[item] is not True for item in required_true)
        or not isinstance(actual_controls["processor_link_matched_count"], int)
        or isinstance(actual_controls["processor_link_matched_count"], bool)
        or actual_controls["processor_link_matched_count"] < 1
        or actual_controls["processor_link_exception_count"] != 0
    ):
        raise ConnectorShadowArtifactError(
            "Shopify monthly Shadow observation requires a clean, reconciled close-capture result"
        )
    monthly_output = (
        ((pipeline_result.get("services") or {}).get(
            "shopify_monthly_commerce_scope"
        ) or {}).get("output") or {}
    )
    processor_output = (
        ((pipeline_result.get("services") or {}).get(
            "shopify_stripe_activity_reconciliation"
        ) or {}).get("output") or {}
    )
    monthly_rows = monthly_output.get("monthly_commerce_scope") or []
    processor_rows = processor_output.get("reconciliation") or []
    safe_shopify_source_fields = (
        "kind", "name", "network_access_performed", "api_version",
        "canonical_month_period", "interval_semantics", "interval_start", "interval_end",
        "source_observed_at", "close_capture_deadline_hours", "created_population_count",
        "updated_since_month_start_population_count", "deduplicated_order_count",
        "updated_population_upper_bound_is_source_observed_at",
        "refund_event_membership_uses_processed_at", "created_page_count",
        "updated_page_count", "retry_count", "rate_limit_count",
        "retry_delay_seconds_total", "retry_after_honored",
    )
    safe_stripe_source_fields = (
        "kind", "name", "network_access_performed", "api_version", "page_count",
        "retry_count", "created_window",
    )
    safe_shopify_source = {
        key: shopify_source[key] for key in safe_shopify_source_fields if key in shopify_source
    }
    safe_stripe_source = {
        key: stripe_source[key] for key in safe_stripe_source_fields if key in stripe_source
    }

    def safe_quality(batch: dict[str, Any]) -> dict[str, Any]:
        quality = batch["quality"]
        return {
            "ready": True,
            "record_count": quality["record_count"],
            "dataset_counts": dict(quality.get("dataset_counts") or {}),
            "rejected_count": 0,
        }

    observation_core = {
        "schema_version": 1,
        "artifact_type": "shopify_stripe_monthly_connector_shadow_observation",
        "runtime_fingerprint": runtime.snapshot()["fingerprint"],
        "private_pipeline_result_sha256": _hash(pipeline_result),
        "pipeline": {
            key: pipeline.get(key) for key in ("pipeline_id", "run_id", "executed_at")
        },
        "ready": True,
        "blocked_at": None,
        "lineage": {
            "entity_id": entity_id,
            "period": period,
            "canonical_month_scope": True,
            "interval_start": lineage.get("interval_start"),
            "interval_end": lineage.get("interval_end"),
            "processor_link_evidence_count": lineage.get("processor_link_evidence_count"),
        },
        "connector_batches": {
            "shopify.monthly_order_evidence": {
                "source": safe_shopify_source,
                "quality": safe_quality(shopify_batch),
            },
            "stripe.balance_transactions": {
                "source": safe_stripe_source,
                "quality": safe_quality(stripe_batch),
            },
        },
        "services": {
            "shopify_monthly_commerce_scope": {"output": {
                "ready": True,
                "entity_id": entity_id,
                "period": period,
                "monthly_commerce_scope": [{
                    "entity_id": item.get("entity_id"),
                    "period": item.get("period"),
                    "currency": item.get("currency"),
                    "created_order_count": item.get("created_order_count"),
                    "refund_event_count": item.get("refund_event_count"),
                } for item in monthly_rows],
                "refund_review_count": len(monthly_output.get("refund_reviews") or []),
                "blockers": [],
                "canonical_month_scope": True,
                "order_and_refund_period_scope_aligned": True,
                "historical_snapshot_contract": (
                    "close_capture_within_72_hours_after_month_end"
                ),
                "raw_source_records_returned": False,
                "revenue_recognition_performed": False,
                "posting_performed": False,
            }},
            "shopify_stripe_activity_reconciliation": {"output": {
                "ready": True,
                "entity_id": entity_id,
                "reconciliation": [{"status": item.get("status")} for item in processor_rows],
                "candidate_only": True,
                "revenue_recognition_performed": False,
                "posting_performed": False,
            }},
        },
        "founder_briefing": {
            "candidate_only": True,
            "tax_policy_review_required": True,
            "return_receipt_review_required": True,
            "margin_claim_prohibited": True,
        },
        "network_access_performed": True,
        "external_actions_performed": False,
        "posting_performed": False,
        "payment_performed": False,
        "revenue_recognition_performed": False,
        "tax_filing_performed": False,
        "raw_source_ids_included": False,
        "store_domain_included": False,
        "financial_amounts_included": False,
        "private_source_evidence_required_separately": True,
    }
    observation = {
        **observation_core,
        "observation_fingerprint": _hash(observation_core),
    }
    destination = _write_private(output, observation)
    return {
        "output": str(destination),
        "observation_fingerprint": observation["observation_fingerprint"],
        "private_pipeline_result_sha256": observation["private_pipeline_result_sha256"],
        "entity_id": entity_id,
        "sample_period": period,
        "shopify_record_count": shopify_batch["quality"]["record_count"],
        "stripe_balance_transaction_count": stripe_batch["quality"]["record_count"],
        "raw_source_values_returned": False,
        "store_domain_returned": False,
        "financial_amounts_returned": False,
        "external_actions_performed": False,
    }


def write_airwallex_shadow_observation(
    runtime: BoxRuntime,
    pipeline_result: dict[str, Any],
    output: str | Path,
) -> dict[str, Any]:
    """Persist the minimum assessor input from one real webhook refetch run."""
    if not isinstance(pipeline_result, dict):
        raise ConnectorShadowArtifactError("Airwallex Shadow observation requires a Pipeline result")
    if (pipeline_result.get("pipeline") or {}).get("pipeline_id") != "finance.expense_evidence_review":
        raise ConnectorShadowArtifactError("Airwallex Shadow observation requires the expense evidence Pipeline")
    source = (pipeline_result.get("batch") or {}).get("source") or {}
    if not (
        source.get("kind") == "api"
        and source.get("name") == "airwallex.expense_refetch"
        and source.get("network_access_performed") is True
        and source.get("update_capture_basis") == "signed_webhook_then_read_only_refetch"
        and source.get("webhook_context_validated") is True
        and isinstance(source.get("webhook_context_count"), int)
        and source.get("webhook_context_count") > 0
    ):
        raise ConnectorShadowArtifactError(
            "Airwallex Shadow observation requires a validated webhook-triggered network refetch"
        )
    batch = pipeline_result.get("batch") or {}
    quality = batch.get("quality") or {}
    if quality.get("ready") is not True or quality.get("rejected_count") != 0:
        raise ConnectorShadowArtifactError("Airwallex Shadow observation requires a clean Connector batch")
    datasets = batch.get("datasets") or {}
    rows = datasets.get("finance.expense_evidence") or []
    changes = datasets.get("finance.expense_evidence_state_changes") or []
    if not isinstance(rows, list) or not isinstance(changes, list) or not rows + changes:
        raise ConnectorShadowArtifactError("Airwallex Shadow observation requires at least one observed expense event")
    if (
        not isinstance(quality.get("record_count"), int)
        or isinstance(quality.get("record_count"), bool)
        or quality.get("record_count") != len(rows) + len(changes)
    ):
        raise ConnectorShadowArtifactError("Airwallex Shadow observation Connector counts are inconsistent")
    output_service = (
        ((pipeline_result.get("services") or {}).get("expense_evidence_review") or {}).get("output")
        or {}
    )
    entity_id = str(output_service.get("entity_id") or "")
    runtime.require_entity(entity_id)
    required_output_counts = {
        "record_count", "state_change_count", "receipt_missing_count",
        "business_purpose_missing_count", "uncleared_count",
        "accounting_mapping_missing_count",
    }
    if any(
        not isinstance(output_service.get(field), int)
        or isinstance(output_service.get(field), bool)
        or output_service.get(field) < 0
        for field in required_output_counts
    ) or (
        output_service.get("record_count") != len(rows)
        or output_service.get("state_change_count") != len(changes)
    ):
        raise ConnectorShadowArtifactError(
            "Airwallex Shadow observation service counts are incomplete or inconsistent"
        )
    if any(not isinstance(item, dict) or item.get("entity_id") != entity_id for item in rows + changes):
        raise ConnectorShadowArtifactError("Airwallex Shadow observation escaped one legal entity")
    if any(
        pipeline_result.get(field) is not False
        for field in (
            "external_actions_performed", "expense_claims_created",
            "posting_performed", "payment_performed",
        )
    ) or output_service.get("external_actions_performed") is not False:
        raise ConnectorShadowArtifactError("Airwallex Shadow observation requires external actions disabled")
    safe_source_fields = (
        "kind", "name", "network_access_performed", "api_version", "beta_api",
        "update_capture_basis", "complete_update_capture", "webhook_context_validated",
        "webhook_context_count", "webhook_context_fingerprint", "provider_absence_count",
        "page_count", "retry_count",
    )
    safe_source = {key: source[key] for key in safe_source_fields if key in source}
    safe_quality = {
        "ready": True,
        "record_count": quality.get("record_count"),
        "dataset_counts": dict(quality.get("dataset_counts") or {}),
        "rejected_count": 0,
    }
    safe_output_fields = (
        "entity_id", "record_count", "state_change_count", "receipt_missing_count",
        "business_purpose_missing_count", "uncleared_count",
        "accounting_mapping_missing_count", "external_actions_performed",
    )
    safe_output = {
        key: output_service[key] for key in safe_output_fields if key in output_service
    }
    pipeline = pipeline_result["pipeline"]
    observation_core = {
        "schema_version": 1,
        "artifact_type": "airwallex_connector_shadow_observation",
        "runtime_fingerprint": runtime.snapshot()["fingerprint"],
        "private_pipeline_result_sha256": _hash(pipeline_result),
        "pipeline": {
            key: pipeline.get(key) for key in ("pipeline_id", "run_id", "executed_at")
        },
        "ready": pipeline_result.get("ready") is True,
        "blocked_at": pipeline_result.get("blocked_at"),
        "batch": {
            "source": safe_source,
            "datasets": {
                "finance.expense_evidence": [{
                    "entity_id": item.get("entity_id"),
                    "created_at": item.get("created_at"),
                } for item in rows],
                "finance.expense_evidence_state_changes": [{
                    "entity_id": item.get("entity_id"),
                    "current_status": item.get("current_status"),
                    "updated_at": item.get("updated_at"),
                    "provider_absence_confirmed": item.get("provider_absence_confirmed") is True,
                    "candidate_only": item.get("candidate_only") is True,
                } for item in changes],
            },
        },
        "connector_batches": {
            "airwallex.approved_expenses": {
                "source": safe_source,
                "quality": safe_quality,
            },
        },
        "services": {
            "expense_evidence_review": {"output": safe_output},
        },
        "network_access_performed": pipeline_result.get("network_access_performed") is True,
        "external_actions_performed": False,
        "expense_claims_created": False,
        "posting_performed": False,
        "payment_performed": False,
        "raw_source_ids_included": False,
        "financial_amounts_included": False,
        "private_source_evidence_required_separately": True,
    }
    observation = {
        **observation_core,
        "observation_fingerprint": _hash(observation_core),
    }
    destination = _write_private(output, observation)
    return {
        "output": str(destination),
        "observation_fingerprint": observation["observation_fingerprint"],
        "private_pipeline_result_sha256": observation[
            "private_pipeline_result_sha256"
        ],
        "expense_record_count": len(rows),
        "state_change_candidate_count": len(changes),
        "raw_source_values_returned": False,
        "financial_amounts_returned": False,
        "external_actions_performed": False,
    }


def write_wise_shadow_observation(
    runtime: BoxRuntime,
    pipeline_result: dict[str, Any],
    output: str | Path,
) -> dict[str, Any]:
    """Persist amount- and account-reference-free controls from one real Wise statement."""
    if not isinstance(pipeline_result, dict):
        raise ConnectorShadowArtifactError("Wise Shadow observation requires a Pipeline result")
    pipeline = pipeline_result.get("pipeline") or {}
    if pipeline.get("pipeline_id") != "finance.bank_statement_close":
        raise ConnectorShadowArtifactError("Wise Shadow observation requires the bank statement Pipeline")
    batch = pipeline_result.get("batch") or {}
    source = batch.get("source") or {}
    if not (
        source.get("kind") == "api"
        and source.get("name") == "wise.balance_statement"
        and source.get("network_access_performed") is True
        and pipeline_result.get("network_access_performed") is True
    ):
        raise ConnectorShadowArtifactError(
            "Wise Shadow observation requires a real read-only network statement"
        )
    quality = batch.get("quality") or {}
    rows = ((batch.get("datasets") or {}).get("finance.bank_transactions") or [])
    if (
        quality.get("ready") is not True
        or quality.get("rejected_count") != 0
        or not isinstance(rows, list) or not rows
        or quality.get("record_count") != len(rows)
        or (quality.get("dataset_counts") or {}).get("finance.bank_transactions") != len(rows)
    ):
        raise ConnectorShadowArtifactError("Wise Shadow observation requires one clean non-empty batch")
    lineage = pipeline_result.get("lineage") or {}
    entity_id = str(lineage.get("entity_id") or "")
    period = str(lineage.get("period") or "")
    runtime.require_entity(entity_id)
    entity = runtime.entities.get(entity_id)
    currency = entity.functional_currency.upper()
    if not re.fullmatch(r"20\d{2}-(?:0[1-9]|1[0-2])", period):
        raise ConnectorShadowArtifactError("Wise Shadow observation requires one calendar month")
    year, month = (int(part) for part in period.split("-"))
    next_period = f"{year + (month == 12):04d}-{1 if month == 12 else month + 1:02d}"
    numeric_balances = all(
        isinstance(source.get(field), (int, float))
        and not isinstance(source.get(field), bool)
        for field in ("opening_balance", "closing_balance")
    )
    if (
        source.get("interval_start") != f"{period}-01T00:00:00Z"
        or source.get("interval_end") != f"{next_period}-01T00:00:00Z"
        or source.get("currency") != currency
        or source.get("entity_binding_verified") is not True
        or source.get("running_balance_validated") is not True
        or source.get("statement_type") != "COMPACT"
        or source.get("statement_locale") != "en"
        or not numeric_balances
        or not re.fullmatch(r"[0-9a-f]{16}", str(source.get("profile_binding_hash") or ""))
        or not re.fullmatch(r"[0-9a-f]{16}", str(source.get("balance_binding_hash") or ""))
    ):
        raise ConnectorShadowArtifactError(
            "Wise Shadow observation source scope, binding or balance controls are invalid"
        )
    if any(
        not isinstance(item, dict)
        or item.get("entity_id") != entity_id
        or item.get("currency") != currency
        or str(item.get("transaction_date") or "")[:7] != period
        for item in rows
    ):
        raise ConnectorShadowArtifactError("Wise Shadow observation rows escaped one entity-period-currency")
    service_output = (
        ((pipeline_result.get("services") or {}).get("bank_reconciliation_candidate") or {}).get(
            "output"
        )
        or {}
    )
    accounts = service_output.get("accounts") or []
    if (
        service_output.get("entity_id") != entity_id
        or service_output.get("period") != period
        or not isinstance(accounts, list) or not accounts
        or any(
            not isinstance(item, dict)
            or item.get("currency") != currency
            or not isinstance(item.get("matched"), int)
            or isinstance(item.get("matched"), bool)
            or not isinstance(item.get("pending"), int)
            or isinstance(item.get("pending"), bool)
            or item.get("review_current") is not False
            or item.get("confirmed") is not False
            for item in accounts
        )
        or service_output.get("pending_count") != sum(item["pending"] for item in accounts)
        or service_output.get("complete") is not False
        or service_output.get("full_ledger_reconciliation_completed") is not False
        or service_output.get("output_status") != "candidate_reconciliation"
        or service_output.get("review_required") is not True
    ):
        raise ConnectorShadowArtifactError(
            "Wise Shadow observation reconciliation candidate controls are invalid"
        )
    briefing = pipeline_result.get("founder_briefing") or {}
    if (
        pipeline_result.get("external_actions_performed") is not False
        or briefing.get("candidate_only") is not True
        or briefing.get("posting_or_cash_allocation_performed") is not False
        or briefing.get("bank_balance_confirmed") is not False
    ):
        raise ConnectorShadowArtifactError("Wise Shadow observation requires all write actions disabled")
    safe_source_fields = (
        "kind", "name", "network_access_performed", "api_version", "currency",
        "interval_start", "interval_end", "profile_binding_hash", "balance_binding_hash",
        "access_contract", "statement_type", "statement_locale", "entity_binding_verified",
        "running_balance_validated", "retry_count", "rate_limit_count",
        "retry_delay_seconds_total", "retry_after_honored",
    )
    safe_source = {key: source[key] for key in safe_source_fields if key in source}
    safe_source["opening_closing_balance_controls_present"] = True
    safe_quality = {
        "ready": True,
        "record_count": len(rows),
        "dataset_counts": {"finance.bank_transactions": len(rows)},
        "rejected_count": 0,
    }
    safe_accounts = [{
        "currency": item["currency"],
        "matched": item["matched"],
        "pending": item["pending"],
        "review_current": False,
        "confirmed": False,
    } for item in accounts]
    observation_core = {
        "schema_version": 1,
        "artifact_type": "wise_connector_shadow_observation",
        "runtime_fingerprint": runtime.snapshot()["fingerprint"],
        "private_pipeline_result_sha256": _hash(pipeline_result),
        "pipeline": {key: pipeline.get(key) for key in ("pipeline_id", "run_id", "executed_at")},
        "ready": pipeline_result.get("ready") is True,
        "blocked_at": pipeline_result.get("blocked_at"),
        "lineage": {"entity_id": entity_id, "period": period},
        "batch": {
            "source": safe_source,
            "datasets": {"finance.bank_transactions": [{
                "entity_id": entity_id,
                "transaction_date": item.get("transaction_date"),
                "currency": currency,
            } for item in rows]},
        },
        "connector_batches": {
            "wise.balance_statement": {"source": safe_source, "quality": safe_quality},
        },
        "services": {"bank_reconciliation_candidate": {"output": {
            "period": period,
            "entity_id": entity_id,
            "accounts": safe_accounts,
            "pending_count": service_output.get("pending_count"),
            "complete": False,
            "output_status": "candidate_reconciliation",
            "full_ledger_reconciliation_completed": False,
            "review_required": True,
        }}},
        "founder_briefing": {
            "candidate_only": True,
            "bank_balance_confirmed": False,
            "posting_or_cash_allocation_performed": False,
        },
        "network_access_performed": True,
        "external_actions_performed": False,
        "posting_performed": False,
        "payment_performed": False,
        "cash_allocation_performed": False,
        "period_close_performed": False,
        "raw_source_ids_included": False,
        "account_references_included": False,
        "counterparty_values_included": False,
        "financial_amounts_included": False,
        "private_source_evidence_required_separately": True,
    }
    observation = {**observation_core, "observation_fingerprint": _hash(observation_core)}
    destination = _write_private(output, observation)
    return {
        "output": str(destination),
        "observation_fingerprint": observation["observation_fingerprint"],
        "private_pipeline_result_sha256": observation["private_pipeline_result_sha256"],
        "bank_transaction_count": len(rows),
        "account_scope_count": len(accounts),
        "pipeline_ready": observation["ready"],
        "raw_source_values_returned": False,
        "financial_amounts_returned": False,
        "external_actions_performed": False,
    }


def write_xero_shadow_observation(
    runtime: BoxRuntime,
    pipeline_result: dict[str, Any],
    output: str | Path,
) -> dict[str, Any]:
    """Persist amount-free assessor input from one real Xero Trial Balance run."""
    if not isinstance(pipeline_result, dict):
        raise ConnectorShadowArtifactError("Xero Shadow observation requires a Pipeline result")
    pipeline = pipeline_result.get("pipeline") or {}
    if pipeline.get("pipeline_id") != "finance.trial_balance_review":
        raise ConnectorShadowArtifactError("Xero Shadow observation requires the Trial Balance Pipeline")
    source = (pipeline_result.get("batch") or {}).get("source") or {}
    if not (
        source.get("kind") == "api"
        and source.get("name") == "xero.trial_balance"
        and source.get("network_access_performed") is True
        and pipeline_result.get("network_access_performed") is True
    ):
        raise ConnectorShadowArtifactError(
            "Xero Shadow observation requires a real read-only network snapshot"
        )
    if source.get("payments_only") is not False:
        raise ConnectorShadowArtifactError(
            "Xero Shadow observation requires payments_only=false"
        )
    batch = pipeline_result.get("batch") or {}
    quality = batch.get("quality") or {}
    rows = ((batch.get("datasets") or {}).get("finance.trial_balance_lines") or [])
    if (
        quality.get("ready") is not True
        or quality.get("rejected_count") != 0
        or not isinstance(rows, list) or not rows
        or quality.get("record_count") != len(rows)
        or (quality.get("dataset_counts") or {}).get("finance.trial_balance_lines") != len(rows)
    ):
        raise ConnectorShadowArtifactError("Xero Shadow observation requires one clean non-empty Connector batch")
    service_output = (
        ((pipeline_result.get("services") or {}).get("trial_balance_validation") or {}).get("output")
        or {}
    )
    summaries = service_output.get("summaries") or []
    entity_id = str(service_output.get("entity_id") or "")
    period = str((pipeline_result.get("lineage") or {}).get("period") or "")
    runtime.require_entity(entity_id)
    entity = runtime.entities.get(entity_id)
    currency = entity.functional_currency.upper()
    if (
        not re.fullmatch(r"20\d{2}-(?:0[1-9]|1[0-2])", period)
        or source.get("as_at") != _period_end(period)
        or source.get("base_currency") != currency
        or not re.fullmatch(r"[0-9a-f]{16}", str(source.get("tenant_binding_hash") or ""))
        or not re.fullmatch(r"[0-9a-f]{16}", str(source.get("organisation_binding_hash") or ""))
        or source.get("point_in_time_snapshot") is not True
        or source.get("opening_and_period_movements_provided") is not False
        or source.get("ytd_columns_preserved_separately") is not True
    ):
        raise ConnectorShadowArtifactError(
            "Xero Shadow observation source scope, binding or snapshot contract is invalid"
        )
    if not isinstance(summaries, list) or not summaries or any(
        not isinstance(item, dict)
        or item.get("entity_id") != entity_id
        or item.get("period") != period
        or item.get("currency") != currency
        or not isinstance(item.get("line_count"), int)
        or isinstance(item.get("line_count"), bool)
        or item.get("line_count") < 1
        or not isinstance(item.get("balanced"), bool)
        or not isinstance(item.get("roll_forward_checked"), bool)
        or item.get("roll_forward_consistent") is not None
        for item in summaries
    ):
        raise ConnectorShadowArtifactError("Xero Shadow observation summaries are incomplete or out of scope")
    if sum(item["line_count"] for item in summaries) != len(rows) or any(
        not isinstance(item, dict)
        or item.get("entity_id") != entity_id
        or item.get("period") != period
        or item.get("currency") != currency
        for item in rows
    ):
        raise ConnectorShadowArtifactError("Xero Shadow observation row counts escaped one entity-period")
    if any(
        pipeline_result.get(field) is not False
        for field in (
            "external_actions_performed", "ledger_or_opening_balances_modified",
            "posting_performed", "period_close_performed",
        )
    ) or any(
        service_output.get(field) is not False
        for field in ("ledger_or_opening_balances_modified", "posting_performed")
    ):
        raise ConnectorShadowArtifactError("Xero Shadow observation requires all write actions disabled")
    safe_source_fields = (
        "kind", "name", "network_access_performed", "as_at", "payments_only",
        "tenant_binding_hash", "organisation_binding_hash", "base_currency",
        "point_in_time_snapshot", "opening_and_period_movements_provided",
        "ytd_columns_preserved_separately", "retry_count", "rate_limit_count",
        "retry_delay_seconds_total", "retry_after_honored",
    )
    safe_source = {key: source[key] for key in safe_source_fields if key in source}
    safe_quality = {
        "ready": True,
        "record_count": len(rows),
        "dataset_counts": {"finance.trial_balance_lines": len(rows)},
        "rejected_count": 0,
    }
    safe_summaries = [{
        key: item.get(key) for key in (
            "entity_id", "period", "currency", "line_count", "balanced",
            "roll_forward_checked", "roll_forward_consistent",
        )
    } for item in summaries]
    issues = service_output.get("issues") or []
    if not isinstance(issues, list) or any(not isinstance(item, dict) for item in issues):
        raise ConnectorShadowArtifactError("Xero Shadow observation issues must be a list")
    if (
        service_output.get("ready") is not (pipeline_result.get("ready") is True)
        or service_output.get("candidate_only") is not True
    ):
        raise ConnectorShadowArtifactError(
            "Xero Shadow observation Pipeline and service readiness are inconsistent"
        )
    safe_issues = [{
        "severity": str(item.get("severity") or "")[:40],
        "type": str(item.get("type") or "")[:100],
    } for item in issues if isinstance(item, dict)]
    observation_core = {
        "schema_version": 1,
        "artifact_type": "xero_connector_shadow_observation",
        "runtime_fingerprint": runtime.snapshot()["fingerprint"],
        "private_pipeline_result_sha256": _hash(pipeline_result),
        "pipeline": {
            key: pipeline.get(key) for key in ("pipeline_id", "run_id", "executed_at")
        },
        "ready": pipeline_result.get("ready") is True,
        "blocked_at": pipeline_result.get("blocked_at"),
        "lineage": {"entity_id": entity_id, "period": period},
        "batch": {
            "source": safe_source,
            "datasets": {"finance.trial_balance_lines": [{
                "entity_id": entity_id, "period": period, "currency": currency,
            } for _item in rows]},
        },
        "connector_batches": {
            "xero.trial_balance": {"source": safe_source, "quality": safe_quality},
        },
        "services": {"trial_balance_validation": {"output": {
            "ready": service_output.get("ready") is True,
            "entity_id": entity_id,
            "summaries": safe_summaries,
            "issues": safe_issues,
            "candidate_only": service_output.get("candidate_only") is True,
            "ledger_or_opening_balances_modified": False,
            "posting_performed": False,
        }}},
        "network_access_performed": True,
        "external_actions_performed": False,
        "ledger_or_opening_balances_modified": False,
        "posting_performed": False,
        "period_close_performed": False,
        "raw_source_ids_included": False,
        "financial_amounts_included": False,
        "private_source_evidence_required_separately": True,
    }
    observation = {**observation_core, "observation_fingerprint": _hash(observation_core)}
    destination = _write_private(output, observation)
    return {
        "output": str(destination),
        "observation_fingerprint": observation["observation_fingerprint"],
        "private_pipeline_result_sha256": observation["private_pipeline_result_sha256"],
        "trial_balance_line_count": len(rows),
        "scope_count": len(summaries),
        "pipeline_ready": observation["ready"],
        "raw_source_values_returned": False,
        "financial_amounts_returned": False,
        "external_actions_performed": False,
    }


def write_paypal_shadow_observation(
    runtime: BoxRuntime,
    pipeline_result: dict[str, Any],
    output: str | Path,
) -> dict[str, Any]:
    """Persist amount-, PII- and raw-id-free controls from one real PayPal month."""
    if not isinstance(pipeline_result, dict):
        raise ConnectorShadowArtifactError(
            "PayPal Shadow observation requires a Pipeline result"
        )
    pipeline = pipeline_result.get("pipeline") or {}
    if pipeline.get("pipeline_id") != "paypal.transaction_close":
        raise ConnectorShadowArtifactError(
            "PayPal Shadow observation requires the PayPal transaction Pipeline"
        )
    batch = (
        (pipeline_result.get("connector_batches") or {}).get(
            "paypal.transaction_activity"
        ) or {}
    )
    source = batch.get("source") or {}
    quality = batch.get("quality") or {}
    safe_source_fields = (
        "kind", "name", "api_contract", "environment", "interval_start",
        "interval_end", "api_end_inclusive", "page_count", "total_items",
        "network_access_performed", "oauth_token_exchange_performed",
        "retry_count", "rate_limit_count", "retry_delay_seconds_total",
        "retry_after_honored", "query_fields", "balance_affecting_records_only",
        "payer_identity_retained", "shipping_address_retained",
        "cart_or_item_detail_retained", "free_text_retained",
        "raw_source_ids_retained", "oauth_token_persisted",
        "business_write_api_called",
    )
    if set(source) != set(safe_source_fields) or not (
        source.get("kind") == "api"
        and source.get("name") == "paypal.transaction_activity"
        and source.get("api_contract") == "transaction-search-v1"
        and source.get("environment") == "production"
        and source.get("network_access_performed") is True
        and source.get("oauth_token_exchange_performed") is True
        and source.get("oauth_token_persisted") is False
        and source.get("query_fields") == "transaction_info"
        and source.get("balance_affecting_records_only") is True
        and all(
            source.get(field) is False for field in (
                "payer_identity_retained", "shipping_address_retained",
                "cart_or_item_detail_retained", "free_text_retained",
                "raw_source_ids_retained", "business_write_api_called",
            )
        )
    ):
        raise ConnectorShadowArtifactError(
            "PayPal Shadow observation requires a real minimized production Transaction Search"
        )
    start = str(source.get("interval_start") or "")
    end = str(source.get("interval_end") or "")
    period = start[:7] if re.fullmatch(
        r"20\d{2}-(?:0[1-9]|1[0-2])-01T00:00:00Z", start,
    ) else ""
    if period:
        year, month = (int(part) for part in period.split("-"))
        next_period = f"{year + (month == 12):04d}-{1 if month == 12 else month + 1:02d}"
        expected_end = f"{next_period}-01T00:00:00Z"
        expected_inclusive_end = (
            datetime.fromisoformat(expected_end.replace("Z", "+00:00"))
            - timedelta(microseconds=1)
        ).isoformat(timespec="microseconds").replace("+00:00", "Z")
    else:
        expected_end = ""
        expected_inclusive_end = ""
    if end != expected_end or source.get("api_end_inclusive") != expected_inclusive_end:
        raise ConnectorShadowArtifactError(
            "PayPal Shadow observation requires one exact UTC calendar month"
        )
    numeric_source_fields = (
        ("page_count", 1), ("total_items", 1), ("retry_count", 0),
        ("rate_limit_count", 0),
    )
    if (
        any(
            not isinstance(source.get(field), int)
            or isinstance(source.get(field), bool)
            or source[field] < minimum
            for field, minimum in numeric_source_fields
        )
        or not isinstance(source.get("retry_delay_seconds_total"), (int, float))
        or isinstance(source.get("retry_delay_seconds_total"), bool)
        or source["retry_delay_seconds_total"] < 0
        or not isinstance(source.get("retry_after_honored"), bool)
    ):
        raise ConnectorShadowArtifactError(
            "PayPal Shadow observation pagination and retry controls are invalid"
        )
    dataset_counts = quality.get("dataset_counts") or {}
    if (
        quality.get("ready") is not True
        or quality.get("rejected_count") != 0
        or quality.get("rejected_rows") != []
        or quality.get("duplicate_business_keys") != []
        or not isinstance(quality.get("record_count"), int)
        or isinstance(quality.get("record_count"), bool)
        or quality["record_count"] < 1
        or dataset_counts != {
            "payments.paypal_balance_activity": quality["record_count"]
        }
        or source.get("total_items") != quality["record_count"]
    ):
        raise ConnectorShadowArtifactError(
            "PayPal Shadow observation requires one clean non-empty transaction batch"
        )
    service_output = (
        ((pipeline_result.get("services") or {}).get(
            "transaction_activity_summary"
        ) or {}).get("output") or {}
    )
    lineage = pipeline_result.get("lineage") or {}
    entity_id = str(lineage.get("entity_id") or "")
    try:
        runtime.require_connector_entity("connector.paypal", entity_id)
        runtime.require_entity(entity_id)
    except Exception as exc:
        raise ConnectorShadowArtifactError(str(exc)) from exc
    count_fields = (
        "refund_candidate_count", "reversal_candidate_count",
        "reference_review_required_count", "cross_currency_fee_count",
    )
    if (
        pipeline_result.get("ready") is not True
        or pipeline_result.get("blocked_at") is not None
        or pipeline_result.get("network_access_performed") is not True
        or pipeline_result.get("external_actions_performed") is not False
        or pipeline_result.get("blockers") != []
        or lineage.get("accepted_record_count") != quality["record_count"]
        or service_output.get("ready") is not True
        or service_output.get("entity_id") != entity_id
        or service_output.get("transaction_count") != quality["record_count"]
        or any(
            not isinstance(service_output.get(field), int)
            or isinstance(service_output.get(field), bool)
            or service_output[field] < 0
            for field in count_fields
        )
        or service_output.get("duplicate_transaction_keys") != []
        or service_output.get("arithmetic_exception_keys") != []
        or service_output.get("blockers") != []
        or service_output.get("candidate_only") is not True
        or service_output.get("cross_currency_total_prohibited") is not True
        or any(
            service_output.get(field) is not False for field in (
                "revenue_recognition_performed", "refund_accounting_performed",
                "bank_reconciliation_performed", "cash_allocation_performed",
                "posting_performed", "external_actions_performed",
            )
        )
    ):
        raise ConnectorShadowArtifactError(
            "PayPal Shadow observation candidate and write-action controls are invalid"
        )
    briefing = pipeline_result.get("founder_briefing") or {}
    if (
        briefing.get("candidate_only") is not True
        or briefing.get("revenue_claim_prohibited") is not True
        or briefing.get("bank_receipt_claim_prohibited") is not True
        or briefing.get("transaction_count") != quality["record_count"]
        or any(briefing.get(field) != service_output.get(field) for field in count_fields[:3])
    ):
        raise ConnectorShadowArtifactError(
            "PayPal Shadow observation founder boundary is invalid"
        )
    safe_quality = {
        "ready": True,
        "record_count": quality["record_count"],
        "dataset_counts": dict(dataset_counts),
        "rejected_count": 0,
    }
    safe_output = {
        "ready": True,
        "entity_id": entity_id,
        "transaction_count": quality["record_count"],
        **{field: service_output[field] for field in count_fields},
        "candidate_only": True,
        "cross_currency_total_prohibited": True,
        "revenue_recognition_performed": False,
        "refund_accounting_performed": False,
        "bank_reconciliation_performed": False,
        "cash_allocation_performed": False,
        "posting_performed": False,
        "external_actions_performed": False,
    }
    observation_core = {
        "schema_version": 1,
        "artifact_type": "paypal_connector_shadow_observation",
        "runtime_fingerprint": runtime.snapshot()["fingerprint"],
        "private_pipeline_result_sha256": _hash(pipeline_result),
        "pipeline": {
            key: pipeline.get(key) for key in ("pipeline_id", "run_id", "executed_at")
        },
        "ready": True,
        "blocked_at": None,
        "lineage": {"entity_id": entity_id, "period": period},
        "connector_batches": {
            "paypal.transaction_activity": {
                "source": {key: source[key] for key in safe_source_fields},
                "quality": safe_quality,
            },
        },
        "services": {
            "transaction_activity_summary": {"output": safe_output},
        },
        "founder_briefing": {
            "transaction_count": quality["record_count"],
            **{field: service_output[field] for field in count_fields[:3]},
            "candidate_only": True,
            "revenue_claim_prohibited": True,
            "bank_receipt_claim_prohibited": True,
        },
        "network_access_performed": True,
        "external_actions_performed": False,
        "posting_performed": False,
        "revenue_recognition_performed": False,
        "refund_accounting_performed": False,
        "bank_reconciliation_performed": False,
        "cash_allocation_performed": False,
        "raw_source_ids_included": False,
        "customer_pii_or_free_text_included": False,
        "financial_amounts_included": False,
        "private_source_evidence_required_separately": True,
    }
    observation = {**observation_core, "observation_fingerprint": _hash(observation_core)}
    destination = _write_private(output, observation)
    return {
        "output": str(destination),
        "observation_fingerprint": observation["observation_fingerprint"],
        "private_pipeline_result_sha256": observation["private_pipeline_result_sha256"],
        "transaction_count": quality["record_count"],
        "refund_candidate_count": service_output["refund_candidate_count"],
        "reversal_candidate_count": service_output["reversal_candidate_count"],
        "pipeline_ready": True,
        "raw_source_values_returned": False,
        "customer_values_returned": False,
        "financial_amounts_returned": False,
        "external_actions_performed": False,
    }


def write_woocommerce_shadow_observation(
    runtime: BoxRuntime,
    pipeline_result: dict[str, Any],
    output: str | Path,
) -> dict[str, Any]:
    """Persist amount-, site-, customer-, product- and raw-id-free WooCommerce controls."""
    if not isinstance(pipeline_result, dict):
        raise ConnectorShadowArtifactError(
            "WooCommerce Shadow observation requires a Pipeline result"
        )
    pipeline = pipeline_result.get("pipeline") or {}
    if pipeline.get("pipeline_id") != "woocommerce.order_refund_close":
        raise ConnectorShadowArtifactError(
            "WooCommerce Shadow observation requires the order/refund Pipeline"
        )
    batch = (
        (pipeline_result.get("connector_batches") or {}).get(
            "woocommerce.order_refund_activity"
        ) or {}
    )
    source = batch.get("source") or {}
    quality = batch.get("quality") or {}
    safe_source_fields = (
        "kind", "name", "api_contract", "site_binding_sha256",
        "interval_start", "interval_end", "order_page_count",
        "refund_page_count", "order_total", "refund_total",
        "network_access_performed", "retry_count", "rate_limit_count",
        "retry_delay_seconds_total", "retry_after_honored",
        "read_only_key_required", "basic_auth_header_used",
        "query_string_credentials_used", "link_headers_followed",
        "customer_identity_retained", "address_retained",
        "customer_ip_or_user_agent_retained",
        "customer_note_or_metadata_retained",
        "product_identity_or_name_retained", "raw_source_ids_retained",
        "business_write_api_called",
    )
    if set(source) != set(safe_source_fields) or not (
        source.get("kind") == "api"
        and source.get("name") == "woocommerce.order_refund_activity"
        and source.get("api_contract") == "wc-rest-v3"
        and bool(re.fullmatch(
            r"[0-9a-f]{64}", str(source.get("site_binding_sha256") or "")
        ))
        and source.get("network_access_performed") is True
        and source.get("read_only_key_required") is True
        and source.get("basic_auth_header_used") is True
        and source.get("query_string_credentials_used") is False
        and source.get("link_headers_followed") is False
        and all(
            source.get(field) is False for field in (
                "customer_identity_retained", "address_retained",
                "customer_ip_or_user_agent_retained",
                "customer_note_or_metadata_retained",
                "product_identity_or_name_retained", "raw_source_ids_retained",
                "business_write_api_called",
            )
        )
    ):
        raise ConnectorShadowArtifactError(
            "WooCommerce Shadow observation requires a real minimized REST API v3 read"
        )
    start = str(source.get("interval_start") or "")
    end = str(source.get("interval_end") or "")
    period = start[:7] if re.fullmatch(
        r"20\d{2}-(?:0[1-9]|1[0-2])-01T00:00:00Z", start,
    ) else ""
    if period:
        year, month = (int(part) for part in period.split("-"))
        next_period = f"{year + (month == 12):04d}-{1 if month == 12 else month + 1:02d}"
        expected_end = f"{next_period}-01T00:00:00Z"
    else:
        expected_end = ""
    if end != expected_end:
        raise ConnectorShadowArtifactError(
            "WooCommerce Shadow observation requires one exact UTC calendar month"
        )
    numeric_source_fields = (
        ("order_page_count", 1), ("refund_page_count", 1),
        ("order_total", 1), ("refund_total", 0), ("retry_count", 0),
        ("rate_limit_count", 0),
    )
    if (
        any(
            not isinstance(source.get(field), int)
            or isinstance(source.get(field), bool)
            or source[field] < minimum
            for field, minimum in numeric_source_fields
        )
        or not isinstance(source.get("retry_delay_seconds_total"), (int, float))
        or isinstance(source.get("retry_delay_seconds_total"), bool)
        or source["retry_delay_seconds_total"] < 0
        or not isinstance(source.get("retry_after_honored"), bool)
    ):
        raise ConnectorShadowArtifactError(
            "WooCommerce Shadow observation pagination and retry controls are invalid"
        )
    dataset_counts = quality.get("dataset_counts") or {}
    order_count = dataset_counts.get("commerce.woocommerce_orders")
    refund_count = dataset_counts.get("commerce.woocommerce_refunds")
    if (
        quality.get("ready") is not True
        or quality.get("rejected_count") != 0
        or quality.get("rejected_rows") != []
        or quality.get("duplicate_business_keys") != []
        or not isinstance(quality.get("record_count"), int)
        or isinstance(quality.get("record_count"), bool)
        or not isinstance(order_count, int) or isinstance(order_count, bool)
        or order_count < 1
        or not isinstance(refund_count, int) or isinstance(refund_count, bool)
        or refund_count < 0
        or quality["record_count"] != order_count + refund_count
        or source.get("order_total") != order_count
        or source.get("refund_total") != refund_count
    ):
        raise ConnectorShadowArtifactError(
            "WooCommerce Shadow observation requires one clean non-empty order/refund batch"
        )
    service_output = (
        ((pipeline_result.get("services") or {}).get(
            "order_refund_activity_summary"
        ) or {}).get("output") or {}
    )
    lineage = pipeline_result.get("lineage") or {}
    entity_id = str(lineage.get("entity_id") or "")
    try:
        runtime.require_connector_entity("connector.woocommerce", entity_id)
        runtime.require_entity(entity_id)
    except Exception as exc:
        raise ConnectorShadowArtifactError(str(exc)) from exc
    count_fields = (
        "destination_review_required_count", "unpaid_or_unconfirmed_order_count",
    )
    if (
        pipeline_result.get("ready") is not True
        or pipeline_result.get("blocked_at") is not None
        or pipeline_result.get("network_access_performed") is not True
        or pipeline_result.get("external_actions_performed") is not False
        or pipeline_result.get("blockers") != []
        or lineage.get("accepted_record_count") != quality["record_count"]
        or service_output.get("ready") is not True
        or service_output.get("entity_id") != entity_id
        or service_output.get("order_count") != order_count
        or service_output.get("refund_event_count") != refund_count
        or any(
            not isinstance(service_output.get(field), int)
            or isinstance(service_output.get(field), bool)
            or service_output[field] < 0
            for field in count_fields
        )
        or any(service_output.get(field) != [] for field in (
            "duplicate_order_keys", "duplicate_refund_keys", "orphan_refund_keys",
            "arithmetic_exception_keys", "blockers",
        ))
        or service_output.get("candidate_only") is not True
        or service_output.get("cross_currency_total_prohibited") is not True
        or service_output.get("payment_settlement_inferred") is not False
        or any(
            service_output.get(field) is not False for field in (
                "revenue_recognition_performed", "tax_liability_determined",
                "inventory_or_cogs_modified", "posting_performed",
                "external_actions_performed",
            )
        )
    ):
        raise ConnectorShadowArtifactError(
            "WooCommerce Shadow observation candidate and write-action controls are invalid"
        )
    briefing = pipeline_result.get("founder_briefing") or {}
    if (
        briefing.get("candidate_only") is not True
        or briefing.get("revenue_claim_prohibited") is not True
        or briefing.get("tax_liability_claim_prohibited") is not True
        or briefing.get("payment_settlement_claim_prohibited") is not True
        or briefing.get("order_count") != order_count
        or briefing.get("refund_event_count") != refund_count
        or any(briefing.get(field) != service_output.get(field) for field in count_fields)
    ):
        raise ConnectorShadowArtifactError(
            "WooCommerce Shadow observation founder boundary is invalid"
        )
    safe_quality = {
        "ready": True,
        "record_count": quality["record_count"],
        "dataset_counts": dict(dataset_counts),
        "rejected_count": 0,
    }
    safe_output = {
        "ready": True,
        "entity_id": entity_id,
        "order_count": order_count,
        "refund_event_count": refund_count,
        "duplicate_order_keys": [],
        "duplicate_refund_keys": [],
        "orphan_refund_keys": [],
        "arithmetic_exception_keys": [],
        **{field: service_output[field] for field in count_fields},
        "candidate_only": True,
        "cross_currency_total_prohibited": True,
        "payment_settlement_inferred": False,
        "revenue_recognition_performed": False,
        "tax_liability_determined": False,
        "inventory_or_cogs_modified": False,
        "posting_performed": False,
        "external_actions_performed": False,
    }
    observation_core = {
        "schema_version": 1,
        "artifact_type": "woocommerce_connector_shadow_observation",
        "runtime_fingerprint": runtime.snapshot()["fingerprint"],
        "private_pipeline_result_sha256": _hash(pipeline_result),
        "pipeline": {
            key: pipeline.get(key) for key in ("pipeline_id", "run_id", "executed_at")
        },
        "ready": True,
        "blocked_at": None,
        "lineage": {"entity_id": entity_id, "period": period},
        "connector_batches": {
            "woocommerce.order_refund_activity": {
                "source": {key: source[key] for key in safe_source_fields},
                "quality": safe_quality,
            },
        },
        "services": {"order_refund_activity_summary": {"output": safe_output}},
        "founder_briefing": {
            "order_count": order_count,
            "refund_event_count": refund_count,
            **{field: service_output[field] for field in count_fields},
            "candidate_only": True,
            "revenue_claim_prohibited": True,
            "tax_liability_claim_prohibited": True,
            "payment_settlement_claim_prohibited": True,
        },
        "network_access_performed": True,
        "external_actions_performed": False,
        "posting_performed": False,
        "revenue_recognition_performed": False,
        "tax_liability_determined": False,
        "inventory_or_cogs_modified": False,
        "payment_settlement_inferred": False,
        "raw_source_ids_included": False,
        "site_origin_included": False,
        "customer_or_product_values_included": False,
        "financial_amounts_included": False,
        "private_source_evidence_required_separately": True,
    }
    observation = {**observation_core, "observation_fingerprint": _hash(observation_core)}
    destination = _write_private(output, observation)
    return {
        "output": str(destination),
        "observation_fingerprint": observation["observation_fingerprint"],
        "private_pipeline_result_sha256": observation["private_pipeline_result_sha256"],
        "order_count": order_count,
        "refund_event_count": refund_count,
        "pipeline_ready": True,
        "raw_source_values_returned": False,
        "site_origin_returned": False,
        "customer_or_product_values_returned": False,
        "financial_amounts_returned": False,
        "external_actions_performed": False,
    }


def write_shipbob_shadow_observation(
    runtime: BoxRuntime,
    pipeline_result: dict[str, Any],
    output: str | Path,
) -> dict[str, Any]:
    """Persist amount-, merchant-, customer-, inventory- and raw-id-free ShipBob controls."""
    if not isinstance(pipeline_result, dict):
        raise ConnectorShadowArtifactError(
            "ShipBob Shadow observation requires a Pipeline result"
        )
    pipeline = pipeline_result.get("pipeline") or {}
    if pipeline.get("pipeline_id") != "commerce.shipbob_fulfillment_close":
        raise ConnectorShadowArtifactError(
            "ShipBob Shadow observation requires the fulfillment Pipeline"
        )
    batch = (
        (pipeline_result.get("connector_batches") or {}).get("shipbob.fulfillment")
        or {}
    )
    source = batch.get("source") or {}
    quality = batch.get("quality") or {}
    safe_source_fields = (
        "kind", "name", "api_version", "environment", "interval_start",
        "interval_end", "order_page_count", "return_page_count",
        "network_access_performed", "retry_count", "rate_limit_count",
        "retry_delay_seconds_total", "retry_after_honored",
        "customer_identity_retained", "customer_address_retained",
        "raw_tracking_number_retained", "raw_source_ids_retained",
        "write_api_called",
    )
    if set(source) != set(safe_source_fields) or not (
        source.get("kind") == "api"
        and source.get("name") == "shipbob.fulfillment"
        and source.get("api_version") == "2026-07"
        and source.get("environment") == "production"
        and source.get("network_access_performed") is True
        and all(
            source.get(field) is False for field in (
                "customer_identity_retained", "customer_address_retained",
                "raw_tracking_number_retained", "raw_source_ids_retained",
                "write_api_called",
            )
        )
    ):
        raise ConnectorShadowArtifactError(
            "ShipBob Shadow observation requires a real minimized production API read"
        )
    start = str(source.get("interval_start") or "")
    end = str(source.get("interval_end") or "")
    period = start[:7] if re.fullmatch(
        r"20\d{2}-(?:0[1-9]|1[0-2])-01T00:00:00Z", start,
    ) else ""
    if period:
        year, month = (int(part) for part in period.split("-"))
        next_period = f"{year + (month == 12):04d}-{1 if month == 12 else month + 1:02d}"
        expected_end = f"{next_period}-01T00:00:00Z"
    else:
        expected_end = ""
    if end != expected_end:
        raise ConnectorShadowArtifactError(
            "ShipBob Shadow observation requires one exact UTC calendar month"
        )
    if (
        any(
            not isinstance(source.get(field), int)
            or isinstance(source.get(field), bool)
            or source[field] < minimum
            for field, minimum in (
                ("order_page_count", 1), ("return_page_count", 1),
                ("retry_count", 0), ("rate_limit_count", 0),
            )
        )
        or not isinstance(source.get("retry_delay_seconds_total"), (int, float))
        or isinstance(source.get("retry_delay_seconds_total"), bool)
        or source["retry_delay_seconds_total"] < 0
        or not isinstance(source.get("retry_after_honored"), bool)
    ):
        raise ConnectorShadowArtifactError(
            "ShipBob Shadow observation pagination and retry controls are invalid"
        )
    dataset_counts = quality.get("dataset_counts") or {}
    count_map = {
        "orders": dataset_counts.get("commerce.shipbob_orders"),
        "shipments": dataset_counts.get("commerce.shipbob_shipments"),
        "returns": dataset_counts.get("commerce.shipbob_returns"),
        "return_items": dataset_counts.get("commerce.shipbob_return_items"),
    }
    if (
        quality.get("ready") is not True
        or quality.get("rejected_count") != 0
        or quality.get("rejected_rows") != []
        or quality.get("duplicate_business_keys") != []
        or any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in count_map.values()
        )
        or count_map["orders"] < 1
        or quality.get("record_count") != sum(count_map.values())
    ):
        raise ConnectorShadowArtifactError(
            "ShipBob Shadow observation requires one clean non-empty fulfillment batch"
        )
    service_output = (
        ((pipeline_result.get("services") or {}).get(
            "fulfillment_and_return_evidence_summary"
        ) or {}).get("output") or {}
    )
    lineage = pipeline_result.get("lineage") or {}
    entity_id = str(lineage.get("entity_id") or "")
    try:
        runtime.require_connector_entity("connector.shipbob", entity_id)
        runtime.require_entity(entity_id)
    except Exception as exc:
        raise ConnectorShadowArtifactError(str(exc)) from exc
    order_fulfillment = service_output.get("order_fulfillment") or {}
    structural = service_output.get("structural_exceptions") or {}
    unprocessed_count = len(service_output.get("unprocessed_return_items") or [])
    cross_window_count = len(service_output.get("cross_window_return_references") or [])
    if (
        pipeline_result.get("ready") is not True
        or pipeline_result.get("blocked_at") is not None
        or pipeline_result.get("network_access_performed") is not True
        or pipeline_result.get("external_actions_performed") is not False
        or pipeline_result.get("blockers") != []
        or lineage.get("accepted_record_count") != quality.get("record_count")
        or service_output.get("ready") is not True
        or service_output.get("entity_id") != entity_id
        or service_output.get("counts") != count_map
        or service_output.get("blockers") != []
        or any(
            not isinstance(order_fulfillment.get(field), int)
            or isinstance(order_fulfillment.get(field), bool)
            or order_fulfillment[field] < 0
            for field in ("orders_with_shipments", "orders_without_shipments")
        )
        or order_fulfillment.get("orders_with_shipments")
        + order_fulfillment.get("orders_without_shipments") != count_map["orders"]
        or any(structural.get(field) != [] for field in (
            "missing_order_keys", "missing_shipment_keys", "missing_return_keys",
        ))
        or service_output.get("candidate_only") is not True
        or service_output.get("customer_pii_required") is not False
        or service_output.get("cross_currency_total_prohibited") is not True
        or any(
            service_output.get(field) is not False for field in (
                "revenue_recognition_performed", "inventory_adjustment_performed",
                "posting_performed", "external_actions_performed",
            )
        )
    ):
        raise ConnectorShadowArtifactError(
            "ShipBob Shadow observation candidate and write-action controls are invalid"
        )
    briefing = pipeline_result.get("founder_briefing") or {}
    if (
        briefing.get("counts") != count_map
        or briefing.get("orders_without_shipments")
        != order_fulfillment["orders_without_shipments"]
        or briefing.get("unprocessed_return_item_count") != unprocessed_count
        or briefing.get("cross_window_return_reference_count") != cross_window_count
        or briefing.get("candidate_only") is not True
        or briefing.get("revenue_claim_prohibited") is not True
        or briefing.get("inventory_adjustment_claim_prohibited") is not True
    ):
        raise ConnectorShadowArtifactError(
            "ShipBob Shadow observation founder boundary is invalid"
        )
    safe_quality = {
        "ready": True,
        "record_count": quality["record_count"],
        "dataset_counts": dict(dataset_counts),
        "rejected_count": 0,
    }
    safe_output = {
        "ready": True,
        "entity_id": entity_id,
        "counts": count_map,
        "order_fulfillment": {
            "orders_with_shipments": order_fulfillment["orders_with_shipments"],
            "orders_without_shipments": order_fulfillment["orders_without_shipments"],
        },
        "unprocessed_return_item_count": unprocessed_count,
        "cross_window_return_reference_count": cross_window_count,
        "candidate_only": True,
        "customer_pii_required": False,
        "cross_currency_total_prohibited": True,
        "revenue_recognition_performed": False,
        "inventory_adjustment_performed": False,
        "posting_performed": False,
        "external_actions_performed": False,
    }
    observation_core = {
        "schema_version": 1,
        "artifact_type": "shipbob_connector_shadow_observation",
        "runtime_fingerprint": runtime.snapshot()["fingerprint"],
        "private_pipeline_result_sha256": _hash(pipeline_result),
        "pipeline": {
            key: pipeline.get(key) for key in ("pipeline_id", "run_id", "executed_at")
        },
        "ready": True,
        "blocked_at": None,
        "lineage": {"entity_id": entity_id, "period": period},
        "connector_batches": {
            "shipbob.fulfillment": {
                "source": {key: source[key] for key in safe_source_fields},
                "quality": safe_quality,
            },
        },
        "services": {
            "fulfillment_and_return_evidence_summary": {"output": safe_output},
        },
        "founder_briefing": {
            "counts": count_map,
            "orders_without_shipments": order_fulfillment["orders_without_shipments"],
            "unprocessed_return_item_count": unprocessed_count,
            "cross_window_return_reference_count": cross_window_count,
            "candidate_only": True,
            "revenue_claim_prohibited": True,
            "inventory_adjustment_claim_prohibited": True,
        },
        "network_access_performed": True,
        "external_actions_performed": False,
        "posting_performed": False,
        "revenue_recognition_performed": False,
        "inventory_adjustment_performed": False,
        "raw_source_ids_included": False,
        "merchant_account_values_included": False,
        "customer_or_inventory_values_included": False,
        "financial_amounts_included": False,
        "private_source_evidence_required_separately": True,
    }
    observation = {**observation_core, "observation_fingerprint": _hash(observation_core)}
    destination = _write_private(output, observation)
    return {
        "output": str(destination),
        "observation_fingerprint": observation["observation_fingerprint"],
        "private_pipeline_result_sha256": observation["private_pipeline_result_sha256"],
        **{f"{key[:-1] if key.endswith('s') else key}_count": value for key, value in count_map.items()},
        "pipeline_ready": True,
        "raw_source_values_returned": False,
        "merchant_account_values_returned": False,
        "customer_or_inventory_values_returned": False,
        "financial_amounts_returned": False,
        "external_actions_performed": False,
    }


def write_amazon_seller_shadow_observation(
    runtime: BoxRuntime,
    pipeline_result: dict[str, Any],
    output: str | Path,
) -> dict[str, Any]:
    """Persist amount-, seller-, marketplace-, buyer-, product- and raw-id-free controls."""
    if not isinstance(pipeline_result, dict):
        raise ConnectorShadowArtifactError(
            "Amazon Seller Shadow observation requires a Pipeline result"
        )
    pipeline = pipeline_result.get("pipeline") or {}
    if pipeline.get("pipeline_id") != "amazon_seller.marketplace_close":
        raise ConnectorShadowArtifactError(
            "Amazon Seller Shadow observation requires the three-source marketplace Pipeline"
        )
    batch = (
        (pipeline_result.get("connector_batches") or {}).get(
            "amazon_seller.marketplace_evidence"
        ) or {}
    )
    source = batch.get("source") or {}
    quality = batch.get("quality") or {}
    private_source_fields = {
        "kind", "name", "api_contract", "region", "environment", "marketplace_id",
        "seller_binding_sha256", "interval_start", "interval_end",
        "canonical_month_period", "canonical_month_scope", "interval_semantics",
        "orders_time_basis", "orders_included_data", "inventory_observed_at",
        "inventory_observation_type", "transaction_status_filter",
        "order_page_count", "inventory_page_count", "transaction_page_count",
        "order_count", "inventory_count", "transaction_count", "retry_count",
        "rate_limit_count", "retry_delay_seconds_total", "retry_after_honored",
        "network_access_performed", "lwa_token_exchange_performed",
        "lwa_token_exchange_count", "response_links_followed",
        "orders_role_required", "inventory_role_required", "lwa_token_persisted",
        "aws_sigv4_used", "fixed_regional_endpoint_used", "response_urls_followed",
        "buyer_recipient_or_address_retained", "product_title_or_raw_identity_retained",
        "proceeds_expense_tax_payment_or_tracking_requested",
        "raw_seller_or_business_ids_retained", "business_write_api_called",
        "inventory_adjustment_performed",
    }
    if set(source) != private_source_fields or not (
        source.get("kind") == "api"
        and source.get("name") == "amazon_seller.marketplace_evidence"
        and source.get("api_contract")
        == "orders-v2026-01-01+fba-inventory-v1+finances-v2024-06-19"
        and source.get("region") in {"NA", "EU", "FE"}
        and source.get("environment") == "production"
        and bool(re.fullmatch(
            r"[A-Z0-9]{6,32}", str(source.get("marketplace_id") or "")
        ))
        and bool(re.fullmatch(
            r"[0-9a-f]{64}", str(source.get("seller_binding_sha256") or "")
        ))
        and source.get("canonical_month_scope") is True
        and source.get("interval_semantics") == "half_open_utc"
        and source.get("orders_time_basis") == "created"
        and source.get("orders_included_data") == ["FULFILLMENT"]
        and source.get("inventory_observation_type")
        == "current_at_fetch_not_historical_period_end"
        and source.get("transaction_status_filter") is None
        and source.get("network_access_performed") is True
        and source.get("lwa_token_exchange_performed") is True
        and source.get("lwa_token_exchange_count") == 1
        and source.get("lwa_token_persisted") is False
        and source.get("aws_sigv4_used") is False
        and source.get("fixed_regional_endpoint_used") is True
        and source.get("response_links_followed") is False
        and source.get("response_urls_followed") is False
        and isinstance(source.get("orders_role_required"), str)
        and isinstance(source.get("inventory_role_required"), str)
        and all(
            source.get(field) is False for field in (
                "buyer_recipient_or_address_retained",
                "product_title_or_raw_identity_retained",
                "proceeds_expense_tax_payment_or_tracking_requested",
                "raw_seller_or_business_ids_retained", "business_write_api_called",
                "inventory_adjustment_performed",
            )
        )
    ):
        raise ConnectorShadowArtifactError(
            "Amazon Seller Shadow observation requires a real minimized production three-source read"
        )
    start = str(source.get("interval_start") or "").replace(".000000Z", "Z")
    end = str(source.get("interval_end") or "").replace(".000000Z", "Z")
    period = start[:7] if re.fullmatch(
        r"20\d{2}-(?:0[1-9]|1[0-2])-01T00:00:00Z", start,
    ) else ""
    if period:
        year, month = (int(part) for part in period.split("-"))
        next_period = f"{year + (month == 12):04d}-{1 if month == 12 else month + 1:02d}"
        expected_end = f"{next_period}-01T00:00:00Z"
    else:
        expected_end = ""
    if (
        end != expected_end
        or source.get("canonical_month_period") != period
    ):
        raise ConnectorShadowArtifactError(
            "Amazon Seller Shadow observation requires one exact closed UTC calendar month"
        )
    try:
        observed_at = datetime.fromisoformat(
            str(source.get("inventory_observed_at") or "").replace("Z", "+00:00")
        )
        period_end = datetime.fromisoformat(end.replace("Z", "+00:00"))
        inventory_observation_valid = (
            observed_at.tzinfo is not None
            and observed_at >= period_end
            and observed_at <= datetime.now(timezone.utc) + timedelta(minutes=5)
        )
    except (TypeError, ValueError):
        inventory_observation_valid = False
    if not inventory_observation_valid:
        raise ConnectorShadowArtifactError(
            "Amazon Seller Shadow inventory observation timestamp is invalid"
        )
    if (
        any(
            not isinstance(source.get(field), int)
            or isinstance(source.get(field), bool)
            or source[field] < minimum
            for field, minimum in (
                ("order_page_count", 1), ("inventory_page_count", 1),
                ("transaction_page_count", 1), ("order_count", 1),
                ("inventory_count", 1), ("transaction_count", 1),
                ("retry_count", 0), ("rate_limit_count", 0),
            )
        )
        or not isinstance(source.get("retry_delay_seconds_total"), (int, float))
        or isinstance(source.get("retry_delay_seconds_total"), bool)
        or source["retry_delay_seconds_total"] < 0
        or not isinstance(source.get("retry_after_honored"), bool)
    ):
        raise ConnectorShadowArtifactError(
            "Amazon Seller Shadow pagination, source counts or retry controls are invalid"
        )
    dataset_counts = quality.get("dataset_counts") or {}
    count_map = {
        "orders": dataset_counts.get("commerce.amazon_seller_orders"),
        "inventory": dataset_counts.get("commerce.amazon_seller_inventory"),
        "transactions": dataset_counts.get("commerce.amazon_seller_transactions"),
    }
    if (
        quality.get("ready") is not True
        or quality.get("rejected_count") != 0
        or quality.get("rejected_rows") != []
        or quality.get("duplicate_business_keys") != []
        or any(
            not isinstance(value, int) or isinstance(value, bool) or value < 1
            for value in count_map.values()
        )
        or quality.get("record_count") != sum(count_map.values())
        or source.get("order_count") != count_map["orders"]
        or source.get("inventory_count") != count_map["inventory"]
        or source.get("transaction_count") != count_map["transactions"]
    ):
        raise ConnectorShadowArtifactError(
            "Amazon Seller Shadow observation requires one clean non-empty three-source batch"
        )
    service_output = (
        ((pipeline_result.get("services") or {}).get(
            "marketplace_evidence_reconciliation"
        ) or {}).get("output") or {}
    )
    lineage = pipeline_result.get("lineage") or {}
    entity_id = str(lineage.get("entity_id") or "")
    try:
        runtime.require_connector_entity("connector.amazon_seller", entity_id)
        runtime.require_entity(entity_id)
    except Exception as exc:
        raise ConnectorShadowArtifactError(str(exc)) from exc
    difference_lists = {
        "finance_without_order_count": "finance_without_order_keys",
        "shipped_order_without_finance_count": "shipped_order_without_finance_keys",
        "fba_order_sku_without_inventory_count": "fba_order_sku_without_inventory_keys",
        "inventory_sku_without_window_order_count": "inventory_sku_without_window_order_keys",
        "inventory_quantity_field_missing_count": "inventory_quantity_field_missing_keys",
        "unmatched_three_way_order_count": "unmatched_three_way_order_keys",
    }
    difference_counts = {
        output_name: len(service_output.get(list_name) or [])
        for output_name, list_name in difference_lists.items()
    }
    marketplace_id = str(source["marketplace_id"])
    marketplace_counts = service_output.get("marketplace_counts") or {}
    numeric_fields = (
        "eligible_three_way_order_count", "matched_three_way_order_count",
        "finance_order_reference_count", "order_item_count", "order_quantity",
    )
    if (
        pipeline_result.get("ready") is not True
        or pipeline_result.get("blocked_at") is not None
        or pipeline_result.get("network_access_performed") is not True
        or pipeline_result.get("external_actions_performed") is not False
        or pipeline_result.get("blockers") != []
        or lineage.get("accepted_record_count") != quality.get("record_count")
        or lineage.get("dataset_counts") != dataset_counts
        or lineage.get("period") != period
        or lineage.get("marketplace_id") != marketplace_id
        or service_output.get("ready") is not True
        or service_output.get("entity_id") != entity_id
        or service_output.get("period") != period
        or service_output.get("canonical_month_scope") is not True
        or service_output.get("marketplace_id") != marketplace_id
        or service_output.get("order_count") != count_map["orders"]
        or service_output.get("inventory_sku_count") != count_map["inventory"]
        or service_output.get("transaction_count") != count_map["transactions"]
        or marketplace_counts != {marketplace_id: quality["record_count"]}
        or any(
            not isinstance(service_output.get(field), int)
            or isinstance(service_output.get(field), bool)
            or service_output[field] < 0
            for field in numeric_fields
        )
        or service_output["matched_three_way_order_count"]
        > service_output["eligible_three_way_order_count"]
        or not isinstance(service_output.get("three_way_match_rate"), str)
        or any(service_output.get(field) != [] for field in (
            "duplicate_order_keys", "duplicate_inventory_keys", "blockers",
        ))
        or any(not isinstance(service_output.get(name), list) for name in difference_lists.values())
        or service_output.get("candidate_only") is not True
        or service_output.get("cross_source_difference_candidate_only") is not True
        or service_output.get("three_way_scope_match_is_not_completeness_claim") is not True
        or service_output.get("hashed_cross_source_keys_generated") is not True
        or service_output.get("hashed_cross_source_keys_human_reviewed") is not False
        or service_output.get("current_inventory_not_historical_period_end") is not True
        or service_output.get("order_or_financial_completeness_proven") is not False
        or any(
            service_output.get(field) is not False for field in (
                "inventory_valuation_or_cogs_performed", "revenue_recognition_performed",
                "tax_liability_determined", "settlement_or_bank_reconciliation_performed",
                "posting_or_inventory_adjustment_performed", "external_actions_performed",
            )
        )
    ):
        raise ConnectorShadowArtifactError(
            "Amazon Seller Shadow reconciliation and action controls are invalid"
        )
    briefing = pipeline_result.get("founder_briefing") or {}
    briefing_count_fields = (
        "finance_without_order_count", "shipped_order_without_finance_count",
        "fba_order_sku_without_inventory_count",
        "inventory_sku_without_window_order_count",
        "inventory_quantity_field_missing_count",
    )
    if (
        briefing.get("order_count") != count_map["orders"]
        or briefing.get("inventory_sku_count") != count_map["inventory"]
        or briefing.get("transaction_count") != count_map["transactions"]
        or briefing.get("period") != period
        or briefing.get("canonical_month_scope") is not True
        or briefing.get("marketplace_id") != marketplace_id
        or any(
            briefing.get(field) != difference_counts[field]
            for field in briefing_count_fields
        )
        or briefing.get("candidate_only") is not True
        or briefing.get("current_inventory_not_historical_period_end") is not True
        or briefing.get("order_or_financial_completeness_claim_prohibited") is not True
        or briefing.get("inventory_valuation_or_cogs_claim_prohibited") is not True
        or briefing.get("revenue_tax_settlement_claim_prohibited") is not True
    ):
        raise ConnectorShadowArtifactError(
            "Amazon Seller Shadow founder boundary is invalid"
        )
    safe_source_fields = (
        "kind", "name", "api_contract", "seller_binding_sha256",
        "interval_start", "interval_end", "canonical_month_period",
        "canonical_month_scope", "interval_semantics", "orders_time_basis",
        "orders_included_data", "inventory_observed_at", "inventory_observation_type",
        "transaction_status_filter", "order_page_count", "inventory_page_count",
        "transaction_page_count", "order_count", "inventory_count",
        "transaction_count", "retry_count", "rate_limit_count",
        "retry_delay_seconds_total", "retry_after_honored",
        "network_access_performed", "lwa_token_exchange_performed",
        "lwa_token_exchange_count", "response_links_followed",
        "orders_role_required", "inventory_role_required", "lwa_token_persisted",
        "aws_sigv4_used", "fixed_regional_endpoint_used", "response_urls_followed",
        "buyer_recipient_or_address_retained", "product_title_or_raw_identity_retained",
        "proceeds_expense_tax_payment_or_tracking_requested",
        "raw_seller_or_business_ids_retained", "business_write_api_called",
        "inventory_adjustment_performed", "regional_endpoint_class_valid",
    )
    safe_source = {
        key: source[key]
        for key in safe_source_fields
        if key != "regional_endpoint_class_valid"
    }
    safe_source["regional_endpoint_class_valid"] = True
    safe_quality = {
        "ready": True,
        "record_count": quality["record_count"],
        "dataset_counts": dict(dataset_counts),
        "rejected_count": 0,
    }
    safe_output = {
        "ready": True,
        "entity_id": entity_id,
        "period": period,
        "canonical_month_scope": True,
        "marketplace_scope_count": 1,
        "order_count": count_map["orders"],
        "inventory_sku_count": count_map["inventory"],
        "transaction_count": count_map["transactions"],
        "finance_order_reference_count": service_output["finance_order_reference_count"],
        "eligible_three_way_order_count": service_output["eligible_three_way_order_count"],
        "matched_three_way_order_count": service_output["matched_three_way_order_count"],
        "three_way_match_rate": service_output["three_way_match_rate"],
        **difference_counts,
        "candidate_only": True,
        "cross_source_difference_candidate_only": True,
        "three_way_scope_match_is_not_completeness_claim": True,
        "hashed_cross_source_keys_generated": True,
        "hashed_cross_source_keys_human_reviewed": False,
        "current_inventory_not_historical_period_end": True,
        "order_or_financial_completeness_proven": False,
        "inventory_valuation_or_cogs_performed": False,
        "revenue_recognition_performed": False,
        "tax_liability_determined": False,
        "settlement_or_bank_reconciliation_performed": False,
        "posting_or_inventory_adjustment_performed": False,
        "external_actions_performed": False,
    }
    observation_core = {
        "schema_version": 1,
        "artifact_type": "amazon_seller_connector_shadow_observation",
        "runtime_fingerprint": runtime.snapshot()["fingerprint"],
        "private_pipeline_result_sha256": _hash(pipeline_result),
        "pipeline": {
            key: pipeline.get(key) for key in ("pipeline_id", "run_id", "executed_at")
        },
        "ready": True,
        "blocked_at": None,
        "lineage": {"entity_id": entity_id, "period": period},
        "connector_batches": {
            "amazon_seller.marketplace_evidence": {
                "source": safe_source,
                "quality": safe_quality,
            },
        },
        "services": {"marketplace_evidence_reconciliation": {"output": safe_output}},
        "founder_briefing": {
            "order_count": count_map["orders"],
            "inventory_sku_count": count_map["inventory"],
            "transaction_count": count_map["transactions"],
            "period": period,
            "canonical_month_scope": True,
            "eligible_three_way_order_count": service_output["eligible_three_way_order_count"],
            "matched_three_way_order_count": service_output["matched_three_way_order_count"],
            "three_way_match_rate": service_output["three_way_match_rate"],
            **{field: difference_counts[field] for field in briefing_count_fields},
            "candidate_only": True,
            "current_inventory_not_historical_period_end": True,
            "order_or_financial_completeness_claim_prohibited": True,
            "inventory_valuation_or_cogs_claim_prohibited": True,
            "revenue_tax_settlement_claim_prohibited": True,
        },
        "network_access_performed": True,
        "external_actions_performed": False,
        "posting_or_inventory_adjustment_performed": False,
        "inventory_valuation_or_cogs_performed": False,
        "revenue_recognition_performed": False,
        "tax_liability_determined": False,
        "settlement_or_bank_reconciliation_performed": False,
        "raw_source_ids_included": False,
        "seller_region_or_marketplace_values_included": False,
        "buyer_product_or_inventory_values_included": False,
        "financial_amounts_included": False,
        "private_source_evidence_required_separately": True,
    }
    observation = {**observation_core, "observation_fingerprint": _hash(observation_core)}
    destination = _write_private(output, observation)
    return {
        "output": str(destination),
        "observation_fingerprint": observation["observation_fingerprint"],
        "private_pipeline_result_sha256": observation["private_pipeline_result_sha256"],
        "order_count": count_map["orders"],
        "inventory_sku_count": count_map["inventory"],
        "transaction_count": count_map["transactions"],
        "pipeline_ready": True,
        "raw_source_values_returned": False,
        "seller_region_or_marketplace_values_returned": False,
        "buyer_product_or_inventory_values_returned": False,
        "financial_amounts_returned": False,
        "external_actions_performed": False,
    }


def assess_connector_shadow_artifacts(
    runtime: BoxRuntime, baseline_json: str | Path, pipeline_result_json: str | Path,
    output: str | Path,
) -> dict[str, Any]:
    baseline = validate_connector_shadow_baseline(_read(baseline_json))
    result = _read(pipeline_result_json)
    if (
        isinstance(result, dict) and result.get("ok") is True
        and isinstance(result.get("result"), dict)
    ):
        result = result["result"]
    if not isinstance(result, dict):
        raise ConnectorShadowArtifactError("Pipeline result must be a JSON object")
    if result.get("artifact_type") == "stripe_connector_shadow_observation":
        expected_fields = {
            "schema_version", "artifact_type", "runtime_fingerprint",
            "private_pipeline_result_sha256", "pipeline", "ready", "blocked_at",
            "lineage", "connector_batches", "services", "founder_briefing",
            "network_access_performed", "external_actions_performed",
            "posting_performed", "payment_performed",
            "bank_reconciliation_performed", "period_close_performed",
            "raw_source_ids_included", "bank_references_included",
            "financial_amounts_included", "private_source_evidence_required_separately",
            "observation_fingerprint",
        }
        safe_source_fields = {
            "kind", "name", "network_access_performed", "api_version", "page_count",
            "retry_count", "rate_limit_count", "retry_delay_seconds_total",
            "retry_after_honored", "created_window",
        }
        observation_core = {
            key: value for key, value in result.items() if key != "observation_fingerprint"
        }
        pipeline_scope = result.get("pipeline") or {}
        lineage_scope = result.get("lineage") or {}
        batches = result.get("connector_batches") or {}
        balance_batch = batches.get("stripe.balance_transactions") or {}
        payout_batch = batches.get("stripe.payouts") or {}
        services = result.get("services") or {}
        balance_service = services.get("balance_activity_summary") or {}
        balance_output = balance_service.get("output") or {}
        payout_service = services.get("payout_bank_reconciliation") or {}
        payout_output = payout_service.get("output") or {}
        payout_rows = payout_output.get("reconciliation") or []
        exceptions = payout_output.get("exceptions") or []
        try:
            executed_at = datetime.fromisoformat(
                str(pipeline_scope.get("executed_at") or "").replace("Z", "+00:00")
            )
            executed_at_valid = executed_at.tzinfo is not None
        except (TypeError, ValueError):
            executed_at_valid = False

        source_contract = True
        quality_contract = True
        source_windows: list[dict[str, Any]] = []
        for connector_id, batch, dataset_id in (
            (
                "stripe.balance_transactions", balance_batch,
                "payments.stripe_balance_transactions",
            ),
            ("stripe.payouts", payout_batch, "payments.stripe_payouts"),
        ):
            source = batch.get("source") or {}
            quality = batch.get("quality") or {}
            dataset_counts = quality.get("dataset_counts") or {}
            window = source.get("created_window") or {}
            source_contract = source_contract and (
                set(batch) == {"source", "quality"}
                and set(source) == safe_source_fields
                and source.get("kind") == "api"
                and source.get("name") == connector_id
                and source.get("network_access_performed") is True
                and bool(re.fullmatch(
                    r"20\d{2}-\d{2}-\d{2}(?:\.[a-z][a-z0-9_]*)?",
                    str(source.get("api_version") or ""),
                ))
                and all(
                    isinstance(source.get(field), int)
                    and not isinstance(source.get(field), bool)
                    and source[field] >= minimum
                    for field, minimum in (
                        ("page_count", 1), ("retry_count", 0),
                        ("rate_limit_count", 0),
                    )
                )
                and isinstance(source.get("retry_delay_seconds_total"), (int, float))
                and not isinstance(source.get("retry_delay_seconds_total"), bool)
                and source["retry_delay_seconds_total"] >= 0
                and isinstance(source.get("retry_after_honored"), bool)
                and set(window) == {
                    "gte", "lt", "semantics", "complete_bounds_declared",
                }
                and isinstance(window.get("gte"), int)
                and not isinstance(window.get("gte"), bool)
                and isinstance(window.get("lt"), int)
                and not isinstance(window.get("lt"), bool)
                and window["gte"] >= 0
                and window["lt"] > window["gte"]
                and window.get("semantics") == "half_open_unix_seconds"
                and window.get("complete_bounds_declared") is True
            )
            quality_contract = quality_contract and (
                set(quality) == {
                    "ready", "record_count", "dataset_counts", "rejected_count",
                }
                and quality.get("ready") is True
                and isinstance(quality.get("record_count"), int)
                and not isinstance(quality.get("record_count"), bool)
                and quality.get("record_count") >= 1
                and set(dataset_counts) == {dataset_id}
                and isinstance(dataset_counts.get(dataset_id), int)
                and not isinstance(dataset_counts.get(dataset_id), bool)
                and dataset_counts[dataset_id] == quality.get("record_count")
                and quality.get("rejected_count") == 0
            )
            source_windows.append(window)
        candidate_count = len(payout_rows) if isinstance(payout_rows, list) else -1
        nested_contract_valid = (
            set(pipeline_scope) == {"pipeline_id", "run_id", "executed_at"}
            and pipeline_scope.get("pipeline_id") == _STRIPE_PIPELINE
            and bool(re.fullmatch(r"[0-9a-f]{24}", str(pipeline_scope.get("run_id") or "")))
            and executed_at_valid
            and set(lineage_scope) == {"entity_id", "bank_evidence_count"}
            and lineage_scope.get("entity_id") == baseline["entity_id"]
            and isinstance(lineage_scope.get("bank_evidence_count"), int)
            and not isinstance(lineage_scope.get("bank_evidence_count"), bool)
            and lineage_scope.get("bank_evidence_count") >= 1
            and set(batches) == {"stripe.balance_transactions", "stripe.payouts"}
            and source_contract
            and quality_contract
            and source_windows[0] == source_windows[1]
            and set(services) == {
                "balance_activity_summary", "payout_bank_reconciliation",
            }
            and set(balance_service) == {"output"}
            and set(balance_output) == {
                "ready", "entity_id", "posting_performed",
                "revenue_recognition_performed",
            }
            and balance_output == {
                "ready": True,
                "entity_id": baseline["entity_id"],
                "posting_performed": False,
                "revenue_recognition_performed": False,
            }
            and set(payout_service) == {"output"}
            and set(payout_output) == {
                "ready", "ready_for_review", "entity_id", "reconciliation",
                "exceptions", "candidate_only", "bank_reconciliation_completed",
                "posting_performed", "period_close_performed",
            }
            and payout_output.get("ready") is True
            and payout_output.get("ready_for_review") is True
            and payout_output.get("entity_id") == baseline["entity_id"]
            and isinstance(payout_rows, list) and bool(payout_rows)
            and all(
                isinstance(row, dict)
                and set(row) == {"reconciliation_status"}
                and row.get("reconciliation_status") in {
                    "high_confidence_candidate", "review_candidate",
                }
                for row in payout_rows
            )
            and isinstance(exceptions, list) and not exceptions
            and payout_output.get("candidate_only") is True
            and payout_output.get("bank_reconciliation_completed") is False
            and payout_output.get("posting_performed") is False
            and payout_output.get("period_close_performed") is False
            and candidate_count == payout_batch.get("quality", {}).get("record_count")
            and lineage_scope.get("bank_evidence_count") >= candidate_count
            and result.get("founder_briefing") == {"candidate_only": True}
        )
        if (
            set(result) != expected_fields
            or result.get("schema_version") != 1
            or result.get("runtime_fingerprint") != runtime.snapshot()["fingerprint"]
            or not re.fullmatch(
                r"[0-9a-f]{64}", str(result.get("private_pipeline_result_sha256") or "")
            )
            or not nested_contract_valid
            or result.get("ready") is not True
            or result.get("blocked_at") is not None
            or result.get("network_access_performed") is not True
            or result.get("raw_source_ids_included") is not False
            or result.get("bank_references_included") is not False
            or result.get("financial_amounts_included") is not False
            or result.get("private_source_evidence_required_separately") is not True
            or any(
                result.get(field) is not False for field in (
                    "external_actions_performed", "posting_performed", "payment_performed",
                    "bank_reconciliation_performed", "period_close_performed",
                )
            )
            or result.get("observation_fingerprint") != _hash(observation_core)
        ):
            raise ConnectorShadowArtifactError(
                "Stripe Shadow observation integrity or privacy contract is invalid"
            )
    if result.get("artifact_type") == "shopify_stripe_monthly_connector_shadow_observation":
        expected_fields = {
            "schema_version", "artifact_type", "runtime_fingerprint",
            "private_pipeline_result_sha256", "pipeline", "ready", "blocked_at",
            "lineage", "connector_batches", "services", "founder_briefing",
            "network_access_performed", "external_actions_performed",
            "posting_performed", "payment_performed", "revenue_recognition_performed",
            "tax_filing_performed", "raw_source_ids_included", "store_domain_included",
            "financial_amounts_included", "private_source_evidence_required_separately",
            "observation_fingerprint",
        }
        safe_shopify_source_fields = {
            "kind", "name", "network_access_performed", "api_version",
            "canonical_month_period", "interval_semantics", "interval_start", "interval_end",
            "source_observed_at", "close_capture_deadline_hours", "created_population_count",
            "updated_since_month_start_population_count", "deduplicated_order_count",
            "updated_population_upper_bound_is_source_observed_at",
            "refund_event_membership_uses_processed_at", "created_page_count",
            "updated_page_count", "retry_count", "rate_limit_count",
            "retry_delay_seconds_total", "retry_after_honored",
        }
        safe_stripe_source_fields = {
            "kind", "name", "network_access_performed", "api_version", "page_count",
            "retry_count", "created_window",
        }
        observation_core = {
            key: value for key, value in result.items() if key != "observation_fingerprint"
        }
        batches = result.get("connector_batches") or {}
        shopify_batch = batches.get("shopify.monthly_order_evidence") or {}
        stripe_batch = batches.get("stripe.balance_transactions") or {}
        shopify_source = shopify_batch.get("source") or {}
        stripe_source = stripe_batch.get("source") or {}
        shopify_quality = shopify_batch.get("quality") or {}
        stripe_quality = stripe_batch.get("quality") or {}
        services = result.get("services") or {}
        pipeline_scope = result.get("pipeline") or {}
        lineage_scope = result.get("lineage") or {}
        monthly_service = services.get("shopify_monthly_commerce_scope") or {}
        monthly_output = monthly_service.get("output") or {}
        processor_service = services.get("shopify_stripe_activity_reconciliation") or {}
        processor_output = processor_service.get("output") or {}
        monthly_rows = monthly_output.get("monthly_commerce_scope") or []
        processor_rows = processor_output.get("reconciliation") or []
        quality_contract = all(
            set(batch) == {"source", "quality"}
            and set(batch.get("quality") or {})
            == {"ready", "record_count", "dataset_counts", "rejected_count"}
            and batch["quality"].get("ready") is True
            and batch["quality"].get("rejected_count") == 0
            and isinstance(batch["quality"].get("record_count"), int)
            and not isinstance(batch["quality"].get("record_count"), bool)
            and batch["quality"].get("record_count") >= 1
            and isinstance(batch["quality"].get("dataset_counts"), dict)
            and bool(batch["quality"].get("dataset_counts"))
            and all(
                isinstance(count, int)
                and not isinstance(count, bool)
                and count >= 0
                for count in batch["quality"]["dataset_counts"].values()
            )
            and batch["quality"].get("record_count")
            == sum(batch["quality"]["dataset_counts"].values())
            for batch in (shopify_batch, stripe_batch)
        )
        try:
            executed_at = datetime.fromisoformat(
                str(pipeline_scope.get("executed_at") or "").replace("Z", "+00:00")
            )
            executed_at_valid = executed_at.tzinfo is not None
        except (TypeError, ValueError):
            executed_at_valid = False
        source_value_contract = (
            set(shopify_source) == safe_shopify_source_fields
            and shopify_source.get("kind") == "api"
            and shopify_source.get("name") == "shopify.monthly_order_evidence"
            and shopify_source.get("network_access_performed") is True
            and bool(re.fullmatch(r"20\d{2}-(?:0[1-9]|1[0-2])", str(
                shopify_source.get("api_version") or ""
            )))
            and shopify_source.get("interval_semantics")
            == "half_open_utc_calendar_month"
            and shopify_source.get("close_capture_deadline_hours") == 72
            and all(
                isinstance(shopify_source.get(field), int)
                and not isinstance(shopify_source.get(field), bool)
                and shopify_source[field] >= 0
                for field in (
                    "created_population_count",
                    "updated_since_month_start_population_count",
                    "deduplicated_order_count", "created_page_count",
                    "updated_page_count", "retry_count", "rate_limit_count",
                )
            )
            and isinstance(shopify_source.get("retry_delay_seconds_total"), (int, float))
            and not isinstance(shopify_source.get("retry_delay_seconds_total"), bool)
            and shopify_source["retry_delay_seconds_total"] >= 0
            and isinstance(shopify_source.get("retry_after_honored"), bool)
            and shopify_source.get(
                "updated_population_upper_bound_is_source_observed_at"
            ) is True
            and shopify_source.get("refund_event_membership_uses_processed_at") is True
            and set(stripe_source) == safe_stripe_source_fields
            and stripe_source.get("kind") == "api"
            and stripe_source.get("name") == "stripe.balance_transactions"
            and stripe_source.get("network_access_performed") is True
            and bool(re.fullmatch(r"20\d{2}-\d{2}-\d{2}(?:\.[a-z][a-z0-9_]*)?", str(
                stripe_source.get("api_version") or ""
            )))
            and all(
                isinstance(stripe_source.get(field), int)
                and not isinstance(stripe_source.get(field), bool)
                and stripe_source[field] >= 0
                for field in ("page_count", "retry_count")
            )
            and isinstance(stripe_source.get("created_window"), dict)
            and set(stripe_source["created_window"]) == {
                "gte", "lt", "semantics", "complete_bounds_declared",
            }
            and all(
                isinstance(stripe_source["created_window"].get(field), int)
                and not isinstance(stripe_source["created_window"].get(field), bool)
                for field in ("gte", "lt")
            )
            and stripe_source["created_window"].get("semantics")
            == "half_open_unix_seconds"
            and stripe_source["created_window"].get("complete_bounds_declared") is True
        )
        nested_contract_valid = (
            set(pipeline_scope) == {"pipeline_id", "run_id", "executed_at"}
            and pipeline_scope.get("pipeline_id") == _DTC_SHOPIFY_STRIPE_MONTHLY_PIPELINE
            and bool(re.fullmatch(r"[0-9a-f]{24}", str(pipeline_scope.get("run_id") or "")))
            and executed_at_valid
            and set(lineage_scope) == {
                "entity_id", "period", "canonical_month_scope", "interval_start",
                "interval_end", "processor_link_evidence_count",
            }
            and lineage_scope.get("entity_id") == baseline["entity_id"]
            and lineage_scope.get("period") == baseline["sample_period"]
            and lineage_scope.get("canonical_month_scope") is True
            and set(batches) == {
                "shopify.monthly_order_evidence", "stripe.balance_transactions",
            }
            and quality_contract
            and source_value_contract
            and set(shopify_quality.get("dataset_counts") or {}) == {
                "commerce.shopify_orders", "commerce.shopify_transactions",
                "commerce.shopify_refunds",
            }
            and set(stripe_quality.get("dataset_counts") or {})
            == {"payments.stripe_balance_transactions"}
            and set(services) == {
                "shopify_monthly_commerce_scope",
                "shopify_stripe_activity_reconciliation",
            }
            and set(monthly_service) == {"output"}
            and set(monthly_output) == {
                "ready", "entity_id", "period", "monthly_commerce_scope",
                "refund_review_count", "blockers", "canonical_month_scope",
                "order_and_refund_period_scope_aligned", "historical_snapshot_contract",
                "raw_source_records_returned", "revenue_recognition_performed",
                "posting_performed",
            }
            and isinstance(monthly_rows, list) and bool(monthly_rows)
            and all(
                isinstance(item, dict) and set(item) == {
                    "entity_id", "period", "currency", "created_order_count",
                    "refund_event_count",
                }
                and isinstance(item.get("entity_id"), str) and bool(item["entity_id"])
                and isinstance(item.get("period"), str)
                and bool(re.fullmatch(r"20\d{2}-(?:0[1-9]|1[0-2])", item["period"]))
                and isinstance(item.get("currency"), str)
                and bool(re.fullmatch(r"[A-Z]{3}", item["currency"]))
                and all(
                    isinstance(item.get(field), int)
                    and not isinstance(item.get(field), bool)
                    and item[field] >= 0
                    for field in ("created_order_count", "refund_event_count")
                )
                for item in monthly_rows
            )
            and isinstance(monthly_output.get("refund_review_count"), int)
            and not isinstance(monthly_output.get("refund_review_count"), bool)
            and monthly_output.get("refund_review_count") >= 0
            and set(processor_service) == {"output"}
            and set(processor_output) == {
                "ready", "entity_id", "reconciliation", "candidate_only",
                "revenue_recognition_performed", "posting_performed",
            }
            and isinstance(processor_rows, list) and bool(processor_rows)
            and all(isinstance(item, dict) and set(item) == {"status"} for item in processor_rows)
            and isinstance(
                lineage_scope.get("processor_link_evidence_count"), int,
            )
            and not isinstance(
                lineage_scope.get("processor_link_evidence_count"), bool,
            )
            and lineage_scope.get("processor_link_evidence_count") == len(processor_rows)
            and set(result.get("founder_briefing") or {}) == {
                "candidate_only", "tax_policy_review_required",
                "return_receipt_review_required", "margin_claim_prohibited",
            }
        )
        if (
            set(result) != expected_fields
            or result.get("schema_version") != 1
            or result.get("runtime_fingerprint") != runtime.snapshot()["fingerprint"]
            or not re.fullmatch(
                r"[0-9a-f]{64}", str(result.get("private_pipeline_result_sha256") or "")
            )
            or not nested_contract_valid
            or result.get("ready") is not True
            or result.get("blocked_at") is not None
            or result.get("network_access_performed") is not True
            or result.get("raw_source_ids_included") is not False
            or result.get("store_domain_included") is not False
            or result.get("financial_amounts_included") is not False
            or result.get("private_source_evidence_required_separately") is not True
            or any(
                result.get(field) is not False for field in (
                    "external_actions_performed", "posting_performed", "payment_performed",
                    "revenue_recognition_performed", "tax_filing_performed",
                )
            )
            or result.get("observation_fingerprint") != _hash(observation_core)
        ):
            raise ConnectorShadowArtifactError(
                "Shopify monthly Shadow observation integrity or privacy contract is invalid"
            )
    if result.get("artifact_type") == "airwallex_connector_shadow_observation":
        expected_fields = {
            "schema_version", "artifact_type", "runtime_fingerprint", "pipeline",
            "private_pipeline_result_sha256",
            "ready", "blocked_at", "batch", "connector_batches", "services",
            "network_access_performed", "external_actions_performed",
            "expense_claims_created", "posting_performed", "payment_performed",
            "raw_source_ids_included", "financial_amounts_included",
            "private_source_evidence_required_separately", "observation_fingerprint",
        }
        supplied_fingerprint = result.get("observation_fingerprint")
        observation_core = {
            key: value for key, value in result.items() if key != "observation_fingerprint"
        }
        if (
            set(result) != expected_fields
            or result.get("schema_version") != 1
            or result.get("runtime_fingerprint") != runtime.snapshot()["fingerprint"]
            or not re.fullmatch(
                r"[0-9a-f]{64}", str(result.get("private_pipeline_result_sha256") or "")
            )
            or result.get("raw_source_ids_included") is not False
            or result.get("financial_amounts_included") is not False
            or result.get("private_source_evidence_required_separately") is not True
            or supplied_fingerprint != _hash(observation_core)
        ):
            raise ConnectorShadowArtifactError(
                "Airwallex Shadow observation integrity or privacy contract is invalid"
            )
    if result.get("artifact_type") == "wise_connector_shadow_observation":
        expected_fields = {
            "schema_version", "artifact_type", "runtime_fingerprint", "pipeline",
            "private_pipeline_result_sha256", "ready", "blocked_at", "lineage",
            "batch", "connector_batches", "services", "founder_briefing",
            "network_access_performed", "external_actions_performed", "posting_performed",
            "payment_performed", "cash_allocation_performed", "period_close_performed",
            "raw_source_ids_included", "account_references_included",
            "counterparty_values_included", "financial_amounts_included",
            "private_source_evidence_required_separately", "observation_fingerprint",
        }
        safe_source_fields = {
            "kind", "name", "network_access_performed", "api_version", "currency",
            "interval_start", "interval_end", "profile_binding_hash", "balance_binding_hash",
            "access_contract", "statement_type", "statement_locale", "entity_binding_verified",
            "running_balance_validated", "opening_closing_balance_controls_present",
            "retry_count", "rate_limit_count", "retry_delay_seconds_total",
            "retry_after_honored",
        }
        observation_core = {
            key: value for key, value in result.items() if key != "observation_fingerprint"
        }
        batch = result.get("batch") or {}
        batch_source = batch.get("source") or {}
        rows = (batch.get("datasets") or {}).get("finance.bank_transactions") or []
        batches = result.get("connector_batches") or {}
        connector_batch = batches.get("wise.balance_statement") or {}
        quality = connector_batch.get("quality") or {}
        service_output = (
            ((result.get("services") or {}).get("bank_reconciliation_candidate") or {}).get(
                "output"
            )
            or {}
        )
        accounts = service_output.get("accounts") or []
        nested_contract_valid = (
            set(result.get("pipeline") or {}) == {"pipeline_id", "run_id", "executed_at"}
            and set(result.get("lineage") or {}) == {"entity_id", "period"}
            and set(batch) == {"source", "datasets"}
            and set(batch.get("datasets") or {}) == {"finance.bank_transactions"}
            and isinstance(rows, list) and bool(rows)
            and all(
                isinstance(item, dict)
                and set(item) == {"entity_id", "transaction_date", "currency"}
                for item in rows
            )
            and set(batches) == {"wise.balance_statement"}
            and set(connector_batch) == {"source", "quality"}
            and connector_batch.get("source") == batch_source
            and set(batch_source).issubset(safe_source_fields)
            and set(quality) == {"ready", "record_count", "dataset_counts", "rejected_count"}
            and set(quality.get("dataset_counts") or {}) == {"finance.bank_transactions"}
            and set(result.get("services") or {}) == {"bank_reconciliation_candidate"}
            and set((result.get("services") or {}).get("bank_reconciliation_candidate") or {}) == {"output"}
            and set(service_output) == {
                "period", "entity_id", "accounts", "pending_count", "complete",
                "output_status", "full_ledger_reconciliation_completed", "review_required",
            }
            and isinstance(accounts, list) and bool(accounts)
            and all(
                isinstance(item, dict)
                and set(item) == {
                    "currency", "matched", "pending", "review_current", "confirmed",
                }
                for item in accounts
            )
            and set(result.get("founder_briefing") or {}) == {
                "candidate_only", "bank_balance_confirmed",
                "posting_or_cash_allocation_performed",
            }
        )
        if (
            set(result) != expected_fields
            or result.get("schema_version") != 1
            or result.get("runtime_fingerprint") != runtime.snapshot()["fingerprint"]
            or not re.fullmatch(
                r"[0-9a-f]{64}", str(result.get("private_pipeline_result_sha256") or "")
            )
            or not nested_contract_valid
            or result.get("raw_source_ids_included") is not False
            or result.get("account_references_included") is not False
            or result.get("counterparty_values_included") is not False
            or result.get("financial_amounts_included") is not False
            or result.get("private_source_evidence_required_separately") is not True
            or any(
                result.get(field) is not False for field in (
                    "external_actions_performed", "posting_performed", "payment_performed",
                    "cash_allocation_performed", "period_close_performed",
                )
            )
            or result.get("observation_fingerprint") != _hash(observation_core)
        ):
            raise ConnectorShadowArtifactError(
                "Wise Shadow observation integrity or privacy contract is invalid"
            )
    if result.get("artifact_type") == "xero_connector_shadow_observation":
        expected_fields = {
            "schema_version", "artifact_type", "runtime_fingerprint", "pipeline",
            "private_pipeline_result_sha256", "ready", "blocked_at", "lineage",
            "batch", "connector_batches", "services", "network_access_performed",
            "external_actions_performed", "ledger_or_opening_balances_modified",
            "posting_performed", "period_close_performed", "raw_source_ids_included",
            "financial_amounts_included", "private_source_evidence_required_separately",
            "observation_fingerprint",
        }
        safe_source_fields = {
            "kind", "name", "network_access_performed", "as_at", "payments_only",
            "tenant_binding_hash", "organisation_binding_hash", "base_currency",
            "point_in_time_snapshot", "opening_and_period_movements_provided",
            "ytd_columns_preserved_separately", "retry_count", "rate_limit_count",
            "retry_delay_seconds_total", "retry_after_honored",
        }
        observation_core = {
            key: value for key, value in result.items() if key != "observation_fingerprint"
        }
        batch = result.get("batch") or {}
        batch_source = batch.get("source") or {}
        batches = result.get("connector_batches") or {}
        connector_batch = batches.get("xero.trial_balance") or {}
        quality = connector_batch.get("quality") or {}
        rows = (batch.get("datasets") or {}).get("finance.trial_balance_lines") or []
        service_output = (
            ((result.get("services") or {}).get("trial_balance_validation") or {}).get("output")
            or {}
        )
        summaries = service_output.get("summaries") or []
        issues = service_output.get("issues") or []
        nested_contract_valid = (
            set(result.get("pipeline") or {}) == {"pipeline_id", "run_id", "executed_at"}
            and set(result.get("lineage") or {}) == {"entity_id", "period"}
            and set(batch) == {"source", "datasets"}
            and set(batch.get("datasets") or {}) == {"finance.trial_balance_lines"}
            and isinstance(rows, list) and bool(rows)
            and all(
                isinstance(item, dict)
                and set(item) == {"entity_id", "period", "currency"}
                for item in rows
            )
            and set(batches) == {"xero.trial_balance"}
            and set(connector_batch) == {"source", "quality"}
            and connector_batch.get("source") == batch_source
            and set(batch_source).issubset(safe_source_fields)
            and set(quality) == {"ready", "record_count", "dataset_counts", "rejected_count"}
            and set(quality.get("dataset_counts") or {}) == {"finance.trial_balance_lines"}
            and set(result.get("services") or {}) == {"trial_balance_validation"}
            and set((result.get("services") or {}).get("trial_balance_validation") or {}) == {"output"}
            and set(service_output) == {
                "ready", "entity_id", "summaries", "issues", "candidate_only",
                "ledger_or_opening_balances_modified", "posting_performed",
            }
            and isinstance(summaries, list) and bool(summaries)
            and all(
                isinstance(item, dict) and set(item) == {
                    "entity_id", "period", "currency", "line_count", "balanced",
                    "roll_forward_checked", "roll_forward_consistent",
                }
                for item in summaries
            )
            and isinstance(issues, list)
            and all(
                isinstance(item, dict) and set(item) == {"severity", "type"}
                for item in issues
            )
        )
        if (
            set(result) != expected_fields
            or result.get("schema_version") != 1
            or result.get("runtime_fingerprint") != runtime.snapshot()["fingerprint"]
            or not re.fullmatch(
                r"[0-9a-f]{64}", str(result.get("private_pipeline_result_sha256") or "")
            )
            or not nested_contract_valid
            or result.get("raw_source_ids_included") is not False
            or result.get("financial_amounts_included") is not False
            or result.get("private_source_evidence_required_separately") is not True
            or any(
                result.get(field) is not False for field in (
                    "external_actions_performed", "ledger_or_opening_balances_modified",
                    "posting_performed", "period_close_performed",
                )
            )
            or result.get("observation_fingerprint") != _hash(observation_core)
        ):
            raise ConnectorShadowArtifactError(
                "Xero Shadow observation integrity or privacy contract is invalid"
            )
    if result.get("artifact_type") == "paypal_connector_shadow_observation":
        expected_fields = {
            "schema_version", "artifact_type", "runtime_fingerprint",
            "private_pipeline_result_sha256", "pipeline", "ready", "blocked_at",
            "lineage", "connector_batches", "services", "founder_briefing",
            "network_access_performed", "external_actions_performed",
            "posting_performed", "revenue_recognition_performed",
            "refund_accounting_performed", "bank_reconciliation_performed",
            "cash_allocation_performed", "raw_source_ids_included",
            "customer_pii_or_free_text_included", "financial_amounts_included",
            "private_source_evidence_required_separately", "observation_fingerprint",
        }
        safe_source_fields = {
            "kind", "name", "api_contract", "environment", "interval_start",
            "interval_end", "api_end_inclusive", "page_count", "total_items",
            "network_access_performed", "oauth_token_exchange_performed",
            "retry_count", "rate_limit_count", "retry_delay_seconds_total",
            "retry_after_honored", "query_fields", "balance_affecting_records_only",
            "payer_identity_retained", "shipping_address_retained",
            "cart_or_item_detail_retained", "free_text_retained",
            "raw_source_ids_retained", "oauth_token_persisted",
            "business_write_api_called",
        }
        observation_core = {
            key: value for key, value in result.items() if key != "observation_fingerprint"
        }
        pipeline_scope = result.get("pipeline") or {}
        lineage_scope = result.get("lineage") or {}
        batches = result.get("connector_batches") or {}
        connector_batch = batches.get("paypal.transaction_activity") or {}
        source = connector_batch.get("source") or {}
        quality = connector_batch.get("quality") or {}
        dataset_counts = quality.get("dataset_counts") or {}
        services = result.get("services") or {}
        service = services.get("transaction_activity_summary") or {}
        service_output = service.get("output") or {}
        briefing = result.get("founder_briefing") or {}
        period = baseline["sample_period"]
        year, month = (int(part) for part in period.split("-"))
        next_period = f"{year + (month == 12):04d}-{1 if month == 12 else month + 1:02d}"
        expected_end = f"{next_period}-01T00:00:00Z"
        expected_inclusive_end = (
            datetime.fromisoformat(expected_end.replace("Z", "+00:00"))
            - timedelta(microseconds=1)
        ).isoformat(timespec="microseconds").replace("+00:00", "Z")
        count_fields = (
            "refund_candidate_count", "reversal_candidate_count",
            "reference_review_required_count", "cross_currency_fee_count",
        )
        try:
            executed_at = datetime.fromisoformat(
                str(pipeline_scope.get("executed_at") or "").replace("Z", "+00:00")
            )
            executed_at_valid = executed_at.tzinfo is not None
        except (TypeError, ValueError):
            executed_at_valid = False
        source_contract = (
            set(source) == safe_source_fields
            and source.get("kind") == "api"
            and source.get("name") == "paypal.transaction_activity"
            and source.get("api_contract") == "transaction-search-v1"
            and source.get("environment") == "production"
            and source.get("interval_start") == f"{period}-01T00:00:00Z"
            and source.get("interval_end") == expected_end
            and source.get("api_end_inclusive") == expected_inclusive_end
            and source.get("network_access_performed") is True
            and source.get("oauth_token_exchange_performed") is True
            and source.get("oauth_token_persisted") is False
            and source.get("query_fields") == "transaction_info"
            and source.get("balance_affecting_records_only") is True
            and all(
                source.get(field) is False for field in (
                    "payer_identity_retained", "shipping_address_retained",
                    "cart_or_item_detail_retained", "free_text_retained",
                    "raw_source_ids_retained", "business_write_api_called",
                )
            )
            and all(
                isinstance(source.get(field), int)
                and not isinstance(source.get(field), bool)
                and source[field] >= minimum
                for field, minimum in (
                    ("page_count", 1), ("total_items", 1),
                    ("retry_count", 0), ("rate_limit_count", 0),
                )
            )
            and isinstance(source.get("retry_delay_seconds_total"), (int, float))
            and not isinstance(source.get("retry_delay_seconds_total"), bool)
            and source["retry_delay_seconds_total"] >= 0
            and isinstance(source.get("retry_after_honored"), bool)
        )
        quality_contract = (
            set(quality) == {
                "ready", "record_count", "dataset_counts", "rejected_count",
            }
            and quality.get("ready") is True
            and isinstance(quality.get("record_count"), int)
            and not isinstance(quality.get("record_count"), bool)
            and quality["record_count"] >= 1
            and dataset_counts == {
                "payments.paypal_balance_activity": quality["record_count"]
            }
            and quality.get("rejected_count") == 0
            and source.get("total_items") == quality.get("record_count")
        )
        service_fields = {
            "ready", "entity_id", "transaction_count", *count_fields,
            "candidate_only", "cross_currency_total_prohibited",
            "revenue_recognition_performed", "refund_accounting_performed",
            "bank_reconciliation_performed", "cash_allocation_performed",
            "posting_performed", "external_actions_performed",
        }
        service_contract = (
            set(services) == {"transaction_activity_summary"}
            and set(service) == {"output"}
            and set(service_output) == service_fields
            and service_output.get("ready") is True
            and service_output.get("entity_id") == baseline["entity_id"]
            and quality_contract
            and service_output.get("transaction_count") == quality["record_count"]
            and all(
                isinstance(service_output.get(field), int)
                and not isinstance(service_output.get(field), bool)
                and service_output[field] >= 0
                for field in count_fields
            )
            and service_output.get("candidate_only") is True
            and service_output.get("cross_currency_total_prohibited") is True
            and all(
                service_output.get(field) is False for field in (
                    "revenue_recognition_performed", "refund_accounting_performed",
                    "bank_reconciliation_performed", "cash_allocation_performed",
                    "posting_performed", "external_actions_performed",
                )
            )
        )
        briefing_contract = (
            set(briefing) == {
                "transaction_count", "refund_candidate_count",
                "reversal_candidate_count", "reference_review_required_count",
                "candidate_only", "revenue_claim_prohibited",
                "bank_receipt_claim_prohibited",
            }
            and briefing.get("transaction_count") == quality.get("record_count")
            and all(
                briefing.get(field) == service_output.get(field)
                for field in count_fields[:3]
            )
            and briefing.get("candidate_only") is True
            and briefing.get("revenue_claim_prohibited") is True
            and briefing.get("bank_receipt_claim_prohibited") is True
        )
        nested_contract_valid = (
            set(pipeline_scope) == {"pipeline_id", "run_id", "executed_at"}
            and pipeline_scope.get("pipeline_id") == "paypal.transaction_close"
            and bool(re.fullmatch(
                r"[0-9a-f]{24}", str(pipeline_scope.get("run_id") or "")
            ))
            and executed_at_valid
            and lineage_scope == {
                "entity_id": baseline["entity_id"], "period": period,
            }
            and set(batches) == {"paypal.transaction_activity"}
            and set(connector_batch) == {"source", "quality"}
            and source_contract
            and quality_contract
            and service_contract
            and briefing_contract
        )
        if (
            set(result) != expected_fields
            or result.get("schema_version") != 1
            or result.get("runtime_fingerprint") != runtime.snapshot()["fingerprint"]
            or not re.fullmatch(
                r"[0-9a-f]{64}", str(result.get("private_pipeline_result_sha256") or "")
            )
            or not nested_contract_valid
            or result.get("ready") is not True
            or result.get("blocked_at") is not None
            or result.get("network_access_performed") is not True
            or result.get("raw_source_ids_included") is not False
            or result.get("customer_pii_or_free_text_included") is not False
            or result.get("financial_amounts_included") is not False
            or result.get("private_source_evidence_required_separately") is not True
            or any(
                result.get(field) is not False for field in (
                    "external_actions_performed", "posting_performed",
                    "revenue_recognition_performed", "refund_accounting_performed",
                    "bank_reconciliation_performed", "cash_allocation_performed",
                )
            )
            or result.get("observation_fingerprint") != _hash(observation_core)
        ):
            raise ConnectorShadowArtifactError(
                "PayPal Shadow observation integrity or privacy contract is invalid"
            )
    if result.get("artifact_type") == "woocommerce_connector_shadow_observation":
        expected_fields = {
            "schema_version", "artifact_type", "runtime_fingerprint",
            "private_pipeline_result_sha256", "pipeline", "ready", "blocked_at",
            "lineage", "connector_batches", "services", "founder_briefing",
            "network_access_performed", "external_actions_performed",
            "posting_performed", "revenue_recognition_performed",
            "tax_liability_determined", "inventory_or_cogs_modified",
            "payment_settlement_inferred", "raw_source_ids_included",
            "site_origin_included", "customer_or_product_values_included",
            "financial_amounts_included", "private_source_evidence_required_separately",
            "observation_fingerprint",
        }
        safe_source_fields = {
            "kind", "name", "api_contract", "site_binding_sha256",
            "interval_start", "interval_end", "order_page_count",
            "refund_page_count", "order_total", "refund_total",
            "network_access_performed", "retry_count", "rate_limit_count",
            "retry_delay_seconds_total", "retry_after_honored",
            "read_only_key_required", "basic_auth_header_used",
            "query_string_credentials_used", "link_headers_followed",
            "customer_identity_retained", "address_retained",
            "customer_ip_or_user_agent_retained",
            "customer_note_or_metadata_retained",
            "product_identity_or_name_retained", "raw_source_ids_retained",
            "business_write_api_called",
        }
        observation_core = {
            key: value for key, value in result.items() if key != "observation_fingerprint"
        }
        pipeline_scope = result.get("pipeline") or {}
        lineage_scope = result.get("lineage") or {}
        batches = result.get("connector_batches") or {}
        connector_batch = batches.get("woocommerce.order_refund_activity") or {}
        source = connector_batch.get("source") or {}
        quality = connector_batch.get("quality") or {}
        dataset_counts = quality.get("dataset_counts") or {}
        services = result.get("services") or {}
        service = services.get("order_refund_activity_summary") or {}
        service_output = service.get("output") or {}
        briefing = result.get("founder_briefing") or {}
        period = baseline["sample_period"]
        year, month = (int(part) for part in period.split("-"))
        next_period = f"{year + (month == 12):04d}-{1 if month == 12 else month + 1:02d}"
        expected_end = f"{next_period}-01T00:00:00Z"
        try:
            executed_at = datetime.fromisoformat(
                str(pipeline_scope.get("executed_at") or "").replace("Z", "+00:00")
            )
            executed_at_valid = executed_at.tzinfo is not None
        except (TypeError, ValueError):
            executed_at_valid = False
        source_contract = (
            set(source) == safe_source_fields
            and source.get("kind") == "api"
            and source.get("name") == "woocommerce.order_refund_activity"
            and source.get("api_contract") == "wc-rest-v3"
            and bool(re.fullmatch(
                r"[0-9a-f]{64}", str(source.get("site_binding_sha256") or "")
            ))
            and source.get("interval_start") == f"{period}-01T00:00:00Z"
            and source.get("interval_end") == expected_end
            and source.get("network_access_performed") is True
            and source.get("read_only_key_required") is True
            and source.get("basic_auth_header_used") is True
            and source.get("query_string_credentials_used") is False
            and source.get("link_headers_followed") is False
            and all(
                source.get(field) is False for field in (
                    "customer_identity_retained", "address_retained",
                    "customer_ip_or_user_agent_retained",
                    "customer_note_or_metadata_retained",
                    "product_identity_or_name_retained", "raw_source_ids_retained",
                    "business_write_api_called",
                )
            )
            and all(
                isinstance(source.get(field), int)
                and not isinstance(source.get(field), bool)
                and source[field] >= minimum
                for field, minimum in (
                    ("order_page_count", 1), ("refund_page_count", 1),
                    ("order_total", 1), ("refund_total", 0),
                    ("retry_count", 0), ("rate_limit_count", 0),
                )
            )
            and isinstance(source.get("retry_delay_seconds_total"), (int, float))
            and not isinstance(source.get("retry_delay_seconds_total"), bool)
            and source["retry_delay_seconds_total"] >= 0
            and isinstance(source.get("retry_after_honored"), bool)
        )
        order_count = dataset_counts.get("commerce.woocommerce_orders")
        refund_count = dataset_counts.get("commerce.woocommerce_refunds")
        quality_contract = (
            set(quality) == {
                "ready", "record_count", "dataset_counts", "rejected_count",
            }
            and quality.get("ready") is True
            and isinstance(quality.get("record_count"), int)
            and not isinstance(quality.get("record_count"), bool)
            and isinstance(order_count, int) and not isinstance(order_count, bool)
            and order_count >= 1
            and isinstance(refund_count, int) and not isinstance(refund_count, bool)
            and refund_count >= 0
            and quality["record_count"] == order_count + refund_count
            and quality.get("rejected_count") == 0
            and source.get("order_total") == order_count
            and source.get("refund_total") == refund_count
        )
        count_fields = (
            "destination_review_required_count", "unpaid_or_unconfirmed_order_count",
        )
        service_fields = {
            "ready", "entity_id", "order_count", "refund_event_count",
            "duplicate_order_keys", "duplicate_refund_keys", "orphan_refund_keys",
            "arithmetic_exception_keys", *count_fields, "candidate_only",
            "cross_currency_total_prohibited", "payment_settlement_inferred",
            "revenue_recognition_performed", "tax_liability_determined",
            "inventory_or_cogs_modified", "posting_performed",
            "external_actions_performed",
        }
        service_contract = (
            set(services) == {"order_refund_activity_summary"}
            and set(service) == {"output"}
            and set(service_output) == service_fields
            and service_output.get("ready") is True
            and service_output.get("entity_id") == baseline["entity_id"]
            and quality_contract
            and service_output.get("order_count") == order_count
            and service_output.get("refund_event_count") == refund_count
            and all(
                isinstance(service_output.get(field), int)
                and not isinstance(service_output.get(field), bool)
                and service_output[field] >= 0
                for field in count_fields
            )
            and all(service_output.get(field) == [] for field in (
                "duplicate_order_keys", "duplicate_refund_keys", "orphan_refund_keys",
                "arithmetic_exception_keys",
            ))
            and service_output.get("candidate_only") is True
            and service_output.get("cross_currency_total_prohibited") is True
            and all(
                service_output.get(field) is False for field in (
                    "payment_settlement_inferred", "revenue_recognition_performed",
                    "tax_liability_determined", "inventory_or_cogs_modified",
                    "posting_performed", "external_actions_performed",
                )
            )
        )
        briefing_contract = (
            set(briefing) == {
                "order_count", "refund_event_count", *count_fields, "candidate_only",
                "revenue_claim_prohibited", "tax_liability_claim_prohibited",
                "payment_settlement_claim_prohibited",
            }
            and briefing.get("order_count") == order_count
            and briefing.get("refund_event_count") == refund_count
            and all(
                briefing.get(field) == service_output.get(field) for field in count_fields
            )
            and briefing.get("candidate_only") is True
            and briefing.get("revenue_claim_prohibited") is True
            and briefing.get("tax_liability_claim_prohibited") is True
            and briefing.get("payment_settlement_claim_prohibited") is True
        )
        nested_contract_valid = (
            set(pipeline_scope) == {"pipeline_id", "run_id", "executed_at"}
            and pipeline_scope.get("pipeline_id") == "woocommerce.order_refund_close"
            and bool(re.fullmatch(
                r"[0-9a-f]{24}", str(pipeline_scope.get("run_id") or "")
            ))
            and executed_at_valid
            and lineage_scope == {
                "entity_id": baseline["entity_id"], "period": period,
            }
            and set(batches) == {"woocommerce.order_refund_activity"}
            and set(connector_batch) == {"source", "quality"}
            and source_contract and quality_contract
            and service_contract and briefing_contract
        )
        if (
            set(result) != expected_fields
            or result.get("schema_version") != 1
            or result.get("runtime_fingerprint") != runtime.snapshot()["fingerprint"]
            or not re.fullmatch(
                r"[0-9a-f]{64}", str(result.get("private_pipeline_result_sha256") or "")
            )
            or not nested_contract_valid
            or result.get("ready") is not True
            or result.get("blocked_at") is not None
            or result.get("network_access_performed") is not True
            or result.get("raw_source_ids_included") is not False
            or result.get("site_origin_included") is not False
            or result.get("customer_or_product_values_included") is not False
            or result.get("financial_amounts_included") is not False
            or result.get("private_source_evidence_required_separately") is not True
            or any(
                result.get(field) is not False for field in (
                    "external_actions_performed", "posting_performed",
                    "revenue_recognition_performed", "tax_liability_determined",
                    "inventory_or_cogs_modified", "payment_settlement_inferred",
                )
            )
            or result.get("observation_fingerprint") != _hash(observation_core)
        ):
            raise ConnectorShadowArtifactError(
                "WooCommerce Shadow observation integrity or privacy contract is invalid"
            )
    if result.get("artifact_type") == "shipbob_connector_shadow_observation":
        expected_fields = {
            "schema_version", "artifact_type", "runtime_fingerprint",
            "private_pipeline_result_sha256", "pipeline", "ready", "blocked_at",
            "lineage", "connector_batches", "services", "founder_briefing",
            "network_access_performed", "external_actions_performed",
            "posting_performed", "revenue_recognition_performed",
            "inventory_adjustment_performed", "raw_source_ids_included",
            "merchant_account_values_included", "customer_or_inventory_values_included",
            "financial_amounts_included", "private_source_evidence_required_separately",
            "observation_fingerprint",
        }
        safe_source_fields = {
            "kind", "name", "api_version", "environment", "interval_start",
            "interval_end", "order_page_count", "return_page_count",
            "network_access_performed", "retry_count", "rate_limit_count",
            "retry_delay_seconds_total", "retry_after_honored",
            "customer_identity_retained", "customer_address_retained",
            "raw_tracking_number_retained", "raw_source_ids_retained",
            "write_api_called",
        }
        observation_core = {
            key: value for key, value in result.items() if key != "observation_fingerprint"
        }
        pipeline_scope = result.get("pipeline") or {}
        lineage_scope = result.get("lineage") or {}
        batches = result.get("connector_batches") or {}
        connector_batch = batches.get("shipbob.fulfillment") or {}
        source = connector_batch.get("source") or {}
        quality = connector_batch.get("quality") or {}
        dataset_counts = quality.get("dataset_counts") or {}
        services = result.get("services") or {}
        service = services.get("fulfillment_and_return_evidence_summary") or {}
        service_output = service.get("output") or {}
        briefing = result.get("founder_briefing") or {}
        period = baseline["sample_period"]
        year, month = (int(part) for part in period.split("-"))
        next_period = f"{year + (month == 12):04d}-{1 if month == 12 else month + 1:02d}"
        expected_end = f"{next_period}-01T00:00:00Z"
        try:
            executed_at = datetime.fromisoformat(
                str(pipeline_scope.get("executed_at") or "").replace("Z", "+00:00")
            )
            executed_at_valid = executed_at.tzinfo is not None
        except (TypeError, ValueError):
            executed_at_valid = False
        source_contract = (
            set(source) == safe_source_fields
            and source.get("kind") == "api"
            and source.get("name") == "shipbob.fulfillment"
            and source.get("api_version") == "2026-07"
            and source.get("environment") == "production"
            and source.get("interval_start") == f"{period}-01T00:00:00Z"
            and source.get("interval_end") == expected_end
            and source.get("network_access_performed") is True
            and all(
                source.get(field) is False for field in (
                    "customer_identity_retained", "customer_address_retained",
                    "raw_tracking_number_retained", "raw_source_ids_retained",
                    "write_api_called",
                )
            )
            and all(
                isinstance(source.get(field), int)
                and not isinstance(source.get(field), bool)
                and source[field] >= minimum
                for field, minimum in (
                    ("order_page_count", 1), ("return_page_count", 1),
                    ("retry_count", 0), ("rate_limit_count", 0),
                )
            )
            and isinstance(source.get("retry_delay_seconds_total"), (int, float))
            and not isinstance(source.get("retry_delay_seconds_total"), bool)
            and source["retry_delay_seconds_total"] >= 0
            and isinstance(source.get("retry_after_honored"), bool)
        )
        count_map = {
            "orders": dataset_counts.get("commerce.shipbob_orders"),
            "shipments": dataset_counts.get("commerce.shipbob_shipments"),
            "returns": dataset_counts.get("commerce.shipbob_returns"),
            "return_items": dataset_counts.get("commerce.shipbob_return_items"),
        }
        quality_contract = (
            set(quality) == {
                "ready", "record_count", "dataset_counts", "rejected_count",
            }
            and quality.get("ready") is True
            and all(
                isinstance(value, int) and not isinstance(value, bool) and value >= 0
                for value in count_map.values()
            )
            and count_map["orders"] >= 1
            and quality.get("record_count") == sum(count_map.values())
            and quality.get("rejected_count") == 0
        )
        order_fulfillment = service_output.get("order_fulfillment") or {}
        service_fields = {
            "ready", "entity_id", "counts", "order_fulfillment",
            "unprocessed_return_item_count", "cross_window_return_reference_count",
            "candidate_only", "customer_pii_required", "cross_currency_total_prohibited",
            "revenue_recognition_performed", "inventory_adjustment_performed",
            "posting_performed", "external_actions_performed",
        }
        service_contract = (
            set(services) == {"fulfillment_and_return_evidence_summary"}
            and set(service) == {"output"}
            and set(service_output) == service_fields
            and service_output.get("ready") is True
            and service_output.get("entity_id") == baseline["entity_id"]
            and service_output.get("counts") == count_map
            and set(order_fulfillment) == {
                "orders_with_shipments", "orders_without_shipments",
            }
            and all(
                isinstance(order_fulfillment.get(field), int)
                and not isinstance(order_fulfillment.get(field), bool)
                and order_fulfillment[field] >= 0
                for field in ("orders_with_shipments", "orders_without_shipments")
            )
            and sum(order_fulfillment.values()) == count_map["orders"]
            and all(
                isinstance(service_output.get(field), int)
                and not isinstance(service_output.get(field), bool)
                and service_output[field] >= 0
                for field in (
                    "unprocessed_return_item_count",
                    "cross_window_return_reference_count",
                )
            )
            and service_output.get("candidate_only") is True
            and service_output.get("customer_pii_required") is False
            and service_output.get("cross_currency_total_prohibited") is True
            and all(
                service_output.get(field) is False for field in (
                    "revenue_recognition_performed", "inventory_adjustment_performed",
                    "posting_performed", "external_actions_performed",
                )
            )
        )
        briefing_contract = (
            set(briefing) == {
                "counts", "orders_without_shipments",
                "unprocessed_return_item_count", "cross_window_return_reference_count",
                "candidate_only", "revenue_claim_prohibited",
                "inventory_adjustment_claim_prohibited",
            }
            and briefing.get("counts") == count_map
            and briefing.get("orders_without_shipments")
            == order_fulfillment.get("orders_without_shipments")
            and briefing.get("unprocessed_return_item_count")
            == service_output.get("unprocessed_return_item_count")
            and briefing.get("cross_window_return_reference_count")
            == service_output.get("cross_window_return_reference_count")
            and briefing.get("candidate_only") is True
            and briefing.get("revenue_claim_prohibited") is True
            and briefing.get("inventory_adjustment_claim_prohibited") is True
        )
        nested_contract_valid = (
            set(pipeline_scope) == {"pipeline_id", "run_id", "executed_at"}
            and pipeline_scope.get("pipeline_id") == "commerce.shipbob_fulfillment_close"
            and bool(re.fullmatch(
                r"[0-9a-f]{24}", str(pipeline_scope.get("run_id") or "")
            ))
            and executed_at_valid
            and lineage_scope == {
                "entity_id": baseline["entity_id"], "period": period,
            }
            and set(batches) == {"shipbob.fulfillment"}
            and set(connector_batch) == {"source", "quality"}
            and source_contract and quality_contract
            and service_contract and briefing_contract
        )
        if (
            set(result) != expected_fields
            or result.get("schema_version") != 1
            or result.get("runtime_fingerprint") != runtime.snapshot()["fingerprint"]
            or not re.fullmatch(
                r"[0-9a-f]{64}", str(result.get("private_pipeline_result_sha256") or "")
            )
            or not nested_contract_valid
            or result.get("ready") is not True
            or result.get("blocked_at") is not None
            or result.get("network_access_performed") is not True
            or result.get("raw_source_ids_included") is not False
            or result.get("merchant_account_values_included") is not False
            or result.get("customer_or_inventory_values_included") is not False
            or result.get("financial_amounts_included") is not False
            or result.get("private_source_evidence_required_separately") is not True
            or any(
                result.get(field) is not False for field in (
                    "external_actions_performed", "posting_performed",
                    "revenue_recognition_performed", "inventory_adjustment_performed",
                )
            )
            or result.get("observation_fingerprint") != _hash(observation_core)
        ):
            raise ConnectorShadowArtifactError(
                "ShipBob Shadow observation integrity or privacy contract is invalid"
            )
    if result.get("artifact_type") == "amazon_seller_connector_shadow_observation":
        expected_fields = {
            "schema_version", "artifact_type", "runtime_fingerprint",
            "private_pipeline_result_sha256", "pipeline", "ready", "blocked_at",
            "lineage", "connector_batches", "services", "founder_briefing",
            "network_access_performed", "external_actions_performed",
            "posting_or_inventory_adjustment_performed",
            "inventory_valuation_or_cogs_performed", "revenue_recognition_performed",
            "tax_liability_determined", "settlement_or_bank_reconciliation_performed",
            "raw_source_ids_included", "seller_region_or_marketplace_values_included",
            "buyer_product_or_inventory_values_included", "financial_amounts_included",
            "private_source_evidence_required_separately", "observation_fingerprint",
        }
        safe_source_fields = {
            "kind", "name", "api_contract", "seller_binding_sha256",
            "interval_start", "interval_end", "canonical_month_period",
            "canonical_month_scope", "interval_semantics", "orders_time_basis",
            "orders_included_data", "inventory_observed_at", "inventory_observation_type",
            "transaction_status_filter", "order_page_count", "inventory_page_count",
            "transaction_page_count", "order_count", "inventory_count",
            "transaction_count", "retry_count", "rate_limit_count",
            "retry_delay_seconds_total", "retry_after_honored",
            "network_access_performed", "lwa_token_exchange_performed",
            "lwa_token_exchange_count", "response_links_followed",
            "orders_role_required", "inventory_role_required", "lwa_token_persisted",
            "aws_sigv4_used", "fixed_regional_endpoint_used", "response_urls_followed",
            "buyer_recipient_or_address_retained", "product_title_or_raw_identity_retained",
            "proceeds_expense_tax_payment_or_tracking_requested",
            "raw_seller_or_business_ids_retained", "business_write_api_called",
            "inventory_adjustment_performed", "regional_endpoint_class_valid",
        }
        observation_core = {
            key: value for key, value in result.items() if key != "observation_fingerprint"
        }
        pipeline_scope = result.get("pipeline") or {}
        lineage_scope = result.get("lineage") or {}
        batches = result.get("connector_batches") or {}
        connector_batch = batches.get("amazon_seller.marketplace_evidence") or {}
        source = connector_batch.get("source") or {}
        quality = connector_batch.get("quality") or {}
        dataset_counts = quality.get("dataset_counts") or {}
        services = result.get("services") or {}
        service = services.get("marketplace_evidence_reconciliation") or {}
        service_output = service.get("output") or {}
        briefing = result.get("founder_briefing") or {}
        period = baseline["sample_period"]
        year, month = (int(part) for part in period.split("-"))
        next_period = f"{year + (month == 12):04d}-{1 if month == 12 else month + 1:02d}"
        expected_end = f"{next_period}-01T00:00:00Z"
        try:
            executed_at = datetime.fromisoformat(
                str(pipeline_scope.get("executed_at") or "").replace("Z", "+00:00")
            )
            observed_at = datetime.fromisoformat(
                str(source.get("inventory_observed_at") or "").replace("Z", "+00:00")
            )
            period_end = datetime.fromisoformat(expected_end.replace("Z", "+00:00"))
            timestamps_valid = (
                executed_at.tzinfo is not None and observed_at.tzinfo is not None
                and period_end <= observed_at <= datetime.now(timezone.utc) + timedelta(minutes=5)
            )
        except (TypeError, ValueError):
            timestamps_valid = False
        source_contract = (
            set(source) == safe_source_fields
            and source.get("kind") == "api"
            and source.get("name") == "amazon_seller.marketplace_evidence"
            and source.get("api_contract")
            == "orders-v2026-01-01+fba-inventory-v1+finances-v2024-06-19"
            and bool(re.fullmatch(
                r"[0-9a-f]{64}", str(source.get("seller_binding_sha256") or "")
            ))
            and str(source.get("interval_start") or "").replace(".000000Z", "Z")
            == f"{period}-01T00:00:00Z"
            and str(source.get("interval_end") or "").replace(".000000Z", "Z")
            == expected_end
            and source.get("canonical_month_period") == period
            and source.get("canonical_month_scope") is True
            and source.get("interval_semantics") == "half_open_utc"
            and source.get("orders_time_basis") == "created"
            and source.get("orders_included_data") == ["FULFILLMENT"]
            and source.get("inventory_observation_type")
            == "current_at_fetch_not_historical_period_end"
            and source.get("transaction_status_filter") is None
            and source.get("network_access_performed") is True
            and source.get("lwa_token_exchange_performed") is True
            and source.get("lwa_token_exchange_count") == 1
            and source.get("lwa_token_persisted") is False
            and source.get("regional_endpoint_class_valid") is True
            and source.get("aws_sigv4_used") is False
            and source.get("fixed_regional_endpoint_used") is True
            and source.get("response_links_followed") is False
            and source.get("response_urls_followed") is False
            and isinstance(source.get("orders_role_required"), str)
            and isinstance(source.get("inventory_role_required"), str)
            and all(
                source.get(field) is False for field in (
                    "buyer_recipient_or_address_retained",
                    "product_title_or_raw_identity_retained",
                    "proceeds_expense_tax_payment_or_tracking_requested",
                    "raw_seller_or_business_ids_retained", "business_write_api_called",
                    "inventory_adjustment_performed",
                )
            )
            and all(
                isinstance(source.get(field), int)
                and not isinstance(source.get(field), bool)
                and source[field] >= minimum
                for field, minimum in (
                    ("order_page_count", 1), ("inventory_page_count", 1),
                    ("transaction_page_count", 1), ("order_count", 1),
                    ("inventory_count", 1), ("transaction_count", 1),
                    ("retry_count", 0), ("rate_limit_count", 0),
                )
            )
            and isinstance(source.get("retry_delay_seconds_total"), (int, float))
            and not isinstance(source.get("retry_delay_seconds_total"), bool)
            and source["retry_delay_seconds_total"] >= 0
            and isinstance(source.get("retry_after_honored"), bool)
            and timestamps_valid
        )
        count_map = {
            "orders": dataset_counts.get("commerce.amazon_seller_orders"),
            "inventory": dataset_counts.get("commerce.amazon_seller_inventory"),
            "transactions": dataset_counts.get("commerce.amazon_seller_transactions"),
        }
        quality_contract = (
            set(quality) == {
                "ready", "record_count", "dataset_counts", "rejected_count",
            }
            and quality.get("ready") is True
            and all(
                isinstance(value, int) and not isinstance(value, bool) and value >= 1
                for value in count_map.values()
            )
            and quality.get("record_count") == sum(count_map.values())
            and quality.get("rejected_count") == 0
            and source.get("order_count") == count_map["orders"]
            and source.get("inventory_count") == count_map["inventory"]
            and source.get("transaction_count") == count_map["transactions"]
        )
        count_fields = {
            "finance_without_order_count", "shipped_order_without_finance_count",
            "fba_order_sku_without_inventory_count",
            "inventory_sku_without_window_order_count",
            "inventory_quantity_field_missing_count", "unmatched_three_way_order_count",
        }
        service_fields = {
            "ready", "entity_id", "period", "canonical_month_scope",
            "marketplace_scope_count", "order_count", "inventory_sku_count",
            "transaction_count", "finance_order_reference_count",
            "eligible_three_way_order_count", "matched_three_way_order_count",
            "three_way_match_rate", *count_fields, "candidate_only",
            "cross_source_difference_candidate_only",
            "three_way_scope_match_is_not_completeness_claim",
            "hashed_cross_source_keys_generated", "hashed_cross_source_keys_human_reviewed",
            "current_inventory_not_historical_period_end",
            "order_or_financial_completeness_proven",
            "inventory_valuation_or_cogs_performed", "revenue_recognition_performed",
            "tax_liability_determined", "settlement_or_bank_reconciliation_performed",
            "posting_or_inventory_adjustment_performed", "external_actions_performed",
        }
        service_contract = (
            set(services) == {"marketplace_evidence_reconciliation"}
            and set(service) == {"output"}
            and set(service_output) == service_fields
            and service_output.get("ready") is True
            and service_output.get("entity_id") == baseline["entity_id"]
            and service_output.get("period") == period
            and service_output.get("canonical_month_scope") is True
            and service_output.get("marketplace_scope_count") == 1
            and service_output.get("order_count") == count_map["orders"]
            and service_output.get("inventory_sku_count") == count_map["inventory"]
            and service_output.get("transaction_count") == count_map["transactions"]
            and all(
                isinstance(service_output.get(field), int)
                and not isinstance(service_output.get(field), bool)
                and service_output[field] >= 0
                for field in {
                    *count_fields, "finance_order_reference_count",
                    "eligible_three_way_order_count", "matched_three_way_order_count",
                }
            )
            and service_output.get("matched_three_way_order_count")
            <= service_output.get("eligible_three_way_order_count")
            and isinstance(service_output.get("three_way_match_rate"), str)
            and service_output.get("candidate_only") is True
            and service_output.get("cross_source_difference_candidate_only") is True
            and service_output.get("three_way_scope_match_is_not_completeness_claim") is True
            and service_output.get("hashed_cross_source_keys_generated") is True
            and service_output.get("hashed_cross_source_keys_human_reviewed") is False
            and service_output.get("current_inventory_not_historical_period_end") is True
            and service_output.get("order_or_financial_completeness_proven") is False
            and all(
                service_output.get(field) is False for field in (
                    "inventory_valuation_or_cogs_performed", "revenue_recognition_performed",
                    "tax_liability_determined", "settlement_or_bank_reconciliation_performed",
                    "posting_or_inventory_adjustment_performed", "external_actions_performed",
                )
            )
        )
        briefing_count_fields = count_fields - {"unmatched_three_way_order_count"}
        briefing_contract = (
            set(briefing) == {
                "order_count", "inventory_sku_count", "transaction_count", "period",
                "canonical_month_scope", "eligible_three_way_order_count",
                "matched_three_way_order_count", "three_way_match_rate",
                *briefing_count_fields, "candidate_only",
                "current_inventory_not_historical_period_end",
                "order_or_financial_completeness_claim_prohibited",
                "inventory_valuation_or_cogs_claim_prohibited",
                "revenue_tax_settlement_claim_prohibited",
            }
            and briefing.get("order_count") == count_map["orders"]
            and briefing.get("inventory_sku_count") == count_map["inventory"]
            and briefing.get("transaction_count") == count_map["transactions"]
            and briefing.get("period") == period
            and briefing.get("canonical_month_scope") is True
            and briefing.get("eligible_three_way_order_count")
            == service_output.get("eligible_three_way_order_count")
            and briefing.get("matched_three_way_order_count")
            == service_output.get("matched_three_way_order_count")
            and briefing.get("three_way_match_rate") == service_output.get("three_way_match_rate")
            and all(
                briefing.get(field) == service_output.get(field)
                for field in briefing_count_fields
            )
            and briefing.get("candidate_only") is True
            and briefing.get("current_inventory_not_historical_period_end") is True
            and briefing.get("order_or_financial_completeness_claim_prohibited") is True
            and briefing.get("inventory_valuation_or_cogs_claim_prohibited") is True
            and briefing.get("revenue_tax_settlement_claim_prohibited") is True
        )
        nested_contract_valid = (
            set(pipeline_scope) == {"pipeline_id", "run_id", "executed_at"}
            and pipeline_scope.get("pipeline_id") == "amazon_seller.marketplace_close"
            and bool(re.fullmatch(
                r"[0-9a-f]{24}", str(pipeline_scope.get("run_id") or "")
            ))
            and lineage_scope == {
                "entity_id": baseline["entity_id"], "period": period,
            }
            and set(batches) == {"amazon_seller.marketplace_evidence"}
            and set(connector_batch) == {"source", "quality"}
            and source_contract and quality_contract
            and service_contract and briefing_contract
        )
        if (
            set(result) != expected_fields
            or result.get("schema_version") != 1
            or result.get("runtime_fingerprint") != runtime.snapshot()["fingerprint"]
            or not re.fullmatch(
                r"[0-9a-f]{64}", str(result.get("private_pipeline_result_sha256") or "")
            )
            or not nested_contract_valid
            or result.get("ready") is not True
            or result.get("blocked_at") is not None
            or result.get("network_access_performed") is not True
            or result.get("raw_source_ids_included") is not False
            or result.get("seller_region_or_marketplace_values_included") is not False
            or result.get("buyer_product_or_inventory_values_included") is not False
            or result.get("financial_amounts_included") is not False
            or result.get("private_source_evidence_required_separately") is not True
            or any(
                result.get(field) is not False for field in (
                    "external_actions_performed", "posting_or_inventory_adjustment_performed",
                    "inventory_valuation_or_cogs_performed", "revenue_recognition_performed",
                    "tax_liability_determined", "settlement_or_bank_reconciliation_performed",
                )
            )
            or result.get("observation_fingerprint") != _hash(observation_core)
        ):
            raise ConnectorShadowArtifactError(
                "Amazon Seller Shadow observation integrity or privacy contract is invalid"
            )
    if result.get("pipeline", {}).get("pipeline_id") != baseline["pipeline_id"]:
        raise ConnectorShadowArtifactError("Pipeline result does not match the baseline pipeline")
    result_entity_id = result.get("lineage", {}).get("entity_id")
    if baseline["pipeline_id"] == "finance.expense_evidence_review":
        result_entity_id = (
            (((result.get("services") or {}).get("expense_evidence_review") or {}).get("output") or {}).get(
                "entity_id"
            )
        )
    if result_entity_id != baseline["entity_id"]:
        raise ConnectorShadowArtifactError("Pipeline result does not match the baseline entity")
    runtime.require_entity(baseline["entity_id"])
    snapshot = runtime.snapshot()
    if baseline["schema_version"] == 2:
        selected_pack_ids = _entity_selected_pack_ids(
            runtime, snapshot, baseline["entity_id"],
        )
        expected_profile = _resolved_connector_shadow_profile(
            baseline["pipeline_id"], selected_pack_ids=selected_pack_ids,
        )
        if (
            baseline["covered_pack_ids"] != expected_profile["covered_pack_ids"]
            or [
                (item["source_role"], item["connector_id"])
                for item in baseline["source_expectations"]
            ] != expected_profile["sources"]
        ):
            raise ConnectorShadowArtifactError(
                "real Connector Shadow baseline does not match the current Box Connector scope"
            )
    batches = result.get("connector_batches")
    if not isinstance(batches, dict):
        raise ConnectorShadowArtifactError("Pipeline result is missing connector_batches")
    source_results = []
    for expected in baseline["source_expectations"]:
        connector_id = expected["connector_id"]
        batch = batches.get(connector_id)
        actual_count = (batch.get("quality") or {}).get("record_count") if isinstance(batch, dict) else None
        period_scope_matched = True
        if connector_id == "shopify.monthly_order_evidence" and isinstance(batch, dict):
            source = batch.get("source") or {}
            period = baseline["sample_period"]
            year, month = (int(part) for part in period.split("-"))
            next_period = f"{year + (month == 12):04d}-{1 if month == 12 else month + 1:02d}"
            try:
                interval_end = datetime.fromisoformat(
                    str(source.get("interval_end") or "").replace("Z", "+00:00")
                )
                observed_at = datetime.fromisoformat(
                    str(source.get("source_observed_at") or "").replace("Z", "+00:00")
                )
                close_capture_matched = (
                    interval_end.tzinfo is not None and observed_at.tzinfo is not None
                    and interval_end <= observed_at <= interval_end + timedelta(hours=72)
                )
            except (TypeError, ValueError):
                close_capture_matched = False
            period_scope_matched = (
                source.get("canonical_month_period") == period
                and source.get("interval_start") == f"{period}-01T00:00:00Z"
                and source.get("interval_end") == f"{next_period}-01T00:00:00Z"
                and source.get("interval_semantics") == "half_open_utc_calendar_month"
                and source.get("close_capture_deadline_hours") == 72
                and close_capture_matched
            )
        elif (
            connector_id == "stripe.balance_transactions"
            and baseline["pipeline_id"] == _DTC_SHOPIFY_STRIPE_MONTHLY_PIPELINE
            and isinstance(batch, dict)
        ):
            source = batch.get("source") or {}
            period = baseline["sample_period"]
            year, month = (int(part) for part in period.split("-"))
            next_period = f"{year + (month == 12):04d}-{1 if month == 12 else month + 1:02d}"
            start = datetime.fromisoformat(f"{period}-01T00:00:00+00:00")
            end = datetime.fromisoformat(f"{next_period}-01T00:00:00+00:00")
            created_window = source.get("created_window") or {}
            period_scope_matched = (
                created_window.get("complete_bounds_declared") is True
                and created_window.get("semantics") == "half_open_unix_seconds"
                and created_window.get("gte") == int(start.timestamp())
                and created_window.get("lt") == int(end.timestamp())
            )
        elif (
            connector_id in {"stripe.balance_transactions", "stripe.payouts"}
            and baseline["pipeline_id"] == _STRIPE_PIPELINE
            and isinstance(batch, dict)
        ):
            source = batch.get("source") or {}
            period = baseline["sample_period"]
            year, month = (int(part) for part in period.split("-"))
            next_period = f"{year + (month == 12):04d}-{1 if month == 12 else month + 1:02d}"
            start = datetime.fromisoformat(f"{period}-01T00:00:00+00:00")
            end = datetime.fromisoformat(f"{next_period}-01T00:00:00+00:00")
            created_window = source.get("created_window") or {}
            period_scope_matched = (
                created_window.get("complete_bounds_declared") is True
                and created_window.get("semantics") == "half_open_unix_seconds"
                and created_window.get("gte") == int(start.timestamp())
                and created_window.get("lt") == int(end.timestamp())
            )
        elif connector_id == "wise.balance_statement" and isinstance(batch, dict):
            source = batch.get("source") or {}
            start = str(source.get("interval_start") or "")
            end = str(source.get("interval_end") or "")
            year, month = (int(part) for part in baseline["sample_period"].split("-"))
            next_period = f"{year + (month == 12):04d}-{1 if month == 12 else month + 1:02d}"
            period_scope_matched = (
                start == f"{baseline['sample_period']}-01T00:00:00Z"
                and end == f"{next_period}-01T00:00:00Z"
            )
        elif connector_id == "airwallex.approved_expenses" and isinstance(batch, dict):
            result_datasets = ((result.get("batch") or {}).get("datasets") or {})
            rows = result_datasets.get("finance.expense_evidence") or []
            state_changes = (
                result_datasets.get("finance.expense_evidence_state_changes") or []
            )
            period_scope_matched = (
                isinstance(rows, list) and isinstance(state_changes, list)
                and bool(rows or state_changes)
                and all(
                isinstance(row, dict)
                and row.get("entity_id") == baseline["entity_id"]
                and str(row.get("created_at") or "")[:7] == baseline["sample_period"]
                for row in rows
                )
                and all(
                    isinstance(row, dict)
                    and row.get("entity_id") == baseline["entity_id"]
                    and str(row.get("updated_at") or "")[:7] == baseline["sample_period"]
                    for row in state_changes
                )
            )
        elif connector_id == "xero.trial_balance" and isinstance(batch, dict):
            source = batch.get("source") or {}
            result_rows = (
                ((result.get("batch") or {}).get("datasets") or {}).get(
                    "finance.trial_balance_lines"
                ) or []
            )
            period_scope_matched = (
                source.get("as_at") == _period_end(baseline["sample_period"])
                and isinstance(result_rows, list) and bool(result_rows)
                and all(
                    isinstance(row, dict)
                    and row.get("entity_id") == baseline["entity_id"]
                    and row.get("period") == baseline["sample_period"]
                    for row in result_rows
                )
            )
        elif connector_id == "shipbob.fulfillment" and isinstance(batch, dict):
            source = batch.get("source") or {}
            start = str(source.get("interval_start") or "")
            end = str(source.get("interval_end") or "")
            period = baseline["sample_period"]
            year, month = (int(part) for part in period.split("-"))
            next_period = f"{year + (month == 12):04d}-{1 if month == 12 else month + 1:02d}"
            period_scope_matched = (
                start == f"{period}-01T00:00:00Z"
                and end == f"{next_period}-01T00:00:00Z"
            )
        elif connector_id == "paypal.transaction_activity" and isinstance(batch, dict):
            source = batch.get("source") or {}
            start = str(source.get("interval_start") or "")
            end = str(source.get("interval_end") or "")
            period = baseline["sample_period"]
            year, month = (int(part) for part in period.split("-"))
            next_period = f"{year + (month == 12):04d}-{1 if month == 12 else month + 1:02d}"
            period_scope_matched = (
                start == f"{period}-01T00:00:00Z"
                and end == f"{next_period}-01T00:00:00Z"
            )
        elif connector_id == "woocommerce.order_refund_activity" and isinstance(batch, dict):
            source = batch.get("source") or {}
            start = str(source.get("interval_start") or "")
            end = str(source.get("interval_end") or "")
            period = baseline["sample_period"]
            year, month = (int(part) for part in period.split("-"))
            next_period = f"{year + (month == 12):04d}-{1 if month == 12 else month + 1:02d}"
            period_scope_matched = (
                start == f"{period}-01T00:00:00Z"
                and end == f"{next_period}-01T00:00:00Z"
            )
        elif connector_id == "amazon_seller.transaction_activity" and isinstance(batch, dict):
            source = batch.get("source") or {}
            start = str(source.get("interval_start") or "").replace(".000000Z", "Z")
            end = str(source.get("interval_end") or "").replace(".000000Z", "Z")
            period = baseline["sample_period"]
            year, month = (int(part) for part in period.split("-"))
            next_period = f"{year + (month == 12):04d}-{1 if month == 12 else month + 1:02d}"
            period_scope_matched = (
                start == f"{period}-01T00:00:00Z"
                and end == f"{next_period}-01T00:00:00Z"
            )
        elif connector_id == "amazon_seller.marketplace_evidence" and isinstance(batch, dict):
            source = batch.get("source") or {}
            start = str(source.get("interval_start") or "").replace(".000000Z", "Z")
            end = str(source.get("interval_end") or "").replace(".000000Z", "Z")
            period = baseline["sample_period"]
            year, month = (int(part) for part in period.split("-"))
            next_period = f"{year + (month == 12):04d}-{1 if month == 12 else month + 1:02d}"
            period_scope_matched = (
                start == f"{period}-01T00:00:00Z"
                and end == f"{next_period}-01T00:00:00Z"
            )
        if (
            baseline["schema_version"] == 2
            and baseline["pipeline_id"] in {
                _DTC_SHOPIFY_STRIPE_PIPELINE, _DTC_SHOPIFY_STRIPE_MONTHLY_PIPELINE,
                _STRIPE_PIPELINE,
                "commerce.shipbob_fulfillment_close", "paypal.transaction_close",
                "woocommerce.order_refund_close",
                "amazon_seller.transaction_close",
                "amazon_seller.marketplace_close",
            }
        ):
            source = (batch.get("source") or {}) if isinstance(batch, dict) else {}
            period_scope_matched = (
                period_scope_matched
                and result.get("network_access_performed") is True
                and source.get("kind") == "api"
                and source.get("network_access_performed") is True
            )
        source_results.append({
            "source_role": expected["source_role"], "connector_id": connector_id,
            "expected_record_count": expected["expected_record_count"],
            "actual_record_count": actual_count,
            "matched": (
                actual_count == expected["expected_record_count"] and period_scope_matched
            ),
        })
    runtime.require_entity(baseline["entity_id"])
    expected_entity = runtime.entities.get(baseline["entity_id"])
    actual_controls = _actual_controls(
        result, baseline["pipeline_id"],
        expected_entity_id=baseline["entity_id"],
        expected_currency=expected_entity.functional_currency.upper(),
    )
    control_results = [{
        "control_id": item["control_id"], "expected_value": item["expected_value"],
        "actual_value": actual_controls[item["control_id"]],
        "matched": actual_controls[item["control_id"]] == item["expected_value"],
    } for item in baseline["control_expectations"]]
    assessment_schema_version = 2 if baseline["schema_version"] == 2 else 1
    core = {
        "schema_version": assessment_schema_version,
        "artifact_type": "connector_shadow_assessment",
        "runtime_fingerprint": snapshot["fingerprint"],
        "baseline_id": baseline["baseline_id"], "pipeline_id": baseline["pipeline_id"],
        "entity_id": baseline["entity_id"], "sample_period": baseline["sample_period"],
        "covered_pack_ids": list(baseline["covered_pack_ids"]),
        "baseline_prepared_by": baseline["prepared_by"],
        "baseline_sha256": _hash(baseline),
        "pipeline_result_sha256": (
            result.get("private_pipeline_result_sha256") or _hash(result)
        ),
        "source_results": source_results, "control_results": control_results,
        "passed": all(item["matched"] for item in source_results + control_results),
        "raw_source_values_included": False, "financial_amounts_included": False,
        "external_actions_performed": False,
    }
    if assessment_schema_version == 2:
        core.update({
            "sample_classification": baseline["sample_classification"],
            "source_independence_sha256": _hash(baseline["source_independence"]),
            "anonymization_sha256": _hash(baseline["anonymization"]),
            "real_sample_evidence": True,
        })
    assessment = {**core, "assessment_fingerprint": _hash(core), "review": None, "review_current": False}
    destination = _write_private(output, assessment)
    return {
        "output": str(destination), "assessment_fingerprint": assessment["assessment_fingerprint"],
        "source_count": len(source_results), "control_count": len(control_results),
        "passed": assessment["passed"], "review_current": False,
        "raw_source_values_returned": False, "external_actions_performed": False,
    }


def validate_connector_shadow_assessment(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("artifact_type") != "connector_shadow_assessment":
        raise ConnectorShadowArtifactError("Connector Shadow assessment is invalid")
    core = {key: item for key, item in value.items() if key not in {"assessment_fingerprint", "review", "review_current"}}
    legacy_core_fields = {
        "schema_version", "artifact_type", "runtime_fingerprint", "baseline_id",
        "pipeline_id", "entity_id", "sample_period", "covered_pack_ids",
        "baseline_prepared_by", "baseline_sha256", "pipeline_result_sha256",
        "source_results", "control_results", "passed", "raw_source_values_included",
        "financial_amounts_included", "external_actions_performed",
    }
    real_core_fields = legacy_core_fields | {
        "sample_classification", "source_independence_sha256",
        "anonymization_sha256", "real_sample_evidence",
    }
    if (
        value.get("schema_version") == 1 and set(core) != legacy_core_fields
    ) or (
        value.get("schema_version") == 2 and set(core) != real_core_fields
    ) or value.get("schema_version") not in {1, 2}:
        raise ConnectorShadowArtifactError("Connector Shadow assessment fields are invalid")
    if value.get("assessment_fingerprint") != _hash(core):
        raise ConnectorShadowArtifactError("Connector Shadow assessment fingerprint mismatch")
    if any(
        not re.fullmatch(r"[0-9a-f]{64}", str(value.get(field) or ""))
        for field in ("runtime_fingerprint", "baseline_sha256", "pipeline_result_sha256")
    ):
        raise ConnectorShadowArtifactError("Connector Shadow assessment hash is invalid")
    if value.get("schema_version") == 2:
        if (
            value.get("sample_classification") != "real_anonymized"
            or value.get("real_sample_evidence") is not True
            or any(
                not re.fullmatch(r"[0-9a-f]{64}", str(value.get(field) or ""))
                for field in ("source_independence_sha256", "anonymization_sha256")
            )
        ):
            raise ConnectorShadowArtifactError(
                "real Connector Shadow assessment evidence classification is invalid"
            )
    covered_pack_ids = value.get("covered_pack_ids")
    if (
        not isinstance(covered_pack_ids, list) or not covered_pack_ids
        or covered_pack_ids != sorted(set(covered_pack_ids))
        or any(not isinstance(item, str) or not PACK_PATTERN.fullmatch(item) for item in covered_pack_ids)
    ):
        raise ConnectorShadowArtifactError("Connector Shadow assessment Pack coverage is invalid")
    _actor(value.get("baseline_prepared_by"), "baseline_prepared_by")
    if not isinstance(value.get("passed"), bool):
        raise ConnectorShadowArtifactError("Connector Shadow assessment passed flag is invalid")
    if (
        value.get("raw_source_values_included") is not False
        or value.get("financial_amounts_included") is not False
        or value.get("external_actions_performed") is not False
    ):
        raise ConnectorShadowArtifactError("Connector Shadow assessment must not contain raw values")
    review = value.get("review")
    if value.get("review_current"):
        if not isinstance(review, dict) or review.get("assessment_fingerprint") != value["assessment_fingerprint"]:
            raise ConnectorShadowArtifactError("Connector Shadow review is not bound to the assessment")
        if set(review) != {
            "assessment_fingerprint", "decision", "actor", "rationale",
            "evidence_references", "reviewed_at", "review_id",
        }:
            raise ConnectorShadowArtifactError("Connector Shadow review fields are invalid")
        if review.get("decision") not in DECISIONS:
            raise ConnectorShadowArtifactError("Connector Shadow review decision is invalid")
        _actor(review.get("actor"), "Connector Shadow review actor")
        rationale = str(review.get("rationale") or "").strip()
        if not rationale or len(rationale) > 1000:
            raise ConnectorShadowArtifactError("Connector Shadow review rationale is invalid")
        (
            _real_evidence_references
            if value.get("schema_version") == 2 else _references
        )(review.get("evidence_references"), "Connector Shadow review evidence")
        try:
            reviewed_at = datetime.fromisoformat(
                str(review.get("reviewed_at") or "").replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise ConnectorShadowArtifactError("Connector Shadow reviewed_at must be ISO-8601") from exc
        if reviewed_at.tzinfo is None:
            raise ConnectorShadowArtifactError("Connector Shadow reviewed_at must include timezone")
        review_core = {key: item for key, item in review.items() if key != "review_id"}
        if review.get("review_id") != _hash(review_core)[:24]:
            raise ConnectorShadowArtifactError("Connector Shadow review fingerprint mismatch")
    elif review is not None:
        raise ConnectorShadowArtifactError("non-current Connector Shadow review must be null")
    return dict(value)


def review_connector_shadow_artifact(
    runtime: BoxRuntime, assessment_json: str | Path, output: str | Path, *,
    decision: str, actor: str, rationale: str, evidence_references: Iterable[str],
) -> dict[str, Any]:
    assessment = validate_connector_shadow_assessment(_read(assessment_json))
    if assessment["runtime_fingerprint"] != runtime.snapshot()["fingerprint"]:
        raise ConnectorShadowArtifactError("Connector Shadow assessment belongs to another Box")
    if decision not in DECISIONS:
        raise ConnectorShadowArtifactError("Connector Shadow review decision is invalid")
    actor = _actor(actor, "actor")
    if actor == assessment["baseline_prepared_by"]:
        raise ConnectorShadowArtifactError("Connector Shadow reviewer must differ from baseline preparer")
    rationale = str(rationale or "").strip()
    if not rationale or len(rationale) > 1000:
        raise ConnectorShadowArtifactError("rationale must be 1-1000 characters")
    refs = (
        _real_evidence_references
        if assessment.get("schema_version") == 2 else _references
    )(list(evidence_references), "evidence_references")
    review_core = {
        "assessment_fingerprint": assessment["assessment_fingerprint"],
        "decision": decision, "actor": actor, "rationale": rationale,
        "evidence_references": refs,
        "reviewed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    review = {**review_core, "review_id": _hash(review_core)[:24]}
    reviewed = {**assessment, "review": review, "review_current": True}
    validate_connector_shadow_assessment(reviewed)
    destination = _write_private(output, reviewed)
    return {
        "output": str(destination), "assessment_fingerprint": assessment["assessment_fingerprint"],
        "review_id": review["review_id"], "decision": decision, "review_current": True,
        "raw_source_values_returned": False, "external_actions_performed": False,
    }


def verify_connector_shadow_artifact(runtime: BoxRuntime, assessment_json: str | Path) -> dict[str, Any]:
    assessment = validate_connector_shadow_assessment(_read(assessment_json))
    if assessment["runtime_fingerprint"] != runtime.snapshot()["fingerprint"]:
        raise ConnectorShadowArtifactError("Connector Shadow assessment belongs to another Box")
    entity_id = assessment["entity_id"]
    runtime.require_entity(entity_id)
    unbound_connector_packs = sorted(
        pack_id for pack_id in assessment["covered_pack_ids"]
        if pack_id.startswith("connector.")
        and entity_id not in runtime.connector_entity_ids(pack_id)
    )
    if unbound_connector_packs:
        raise ConnectorShadowArtifactError(
            "Connector Shadow assessment covers a Connector not bound to its entity: "
            + ", ".join(unbound_connector_packs)
        )
    review = assessment.get("review") if assessment.get("review_current") else None
    return {
        "valid": True, "assessment_fingerprint": assessment["assessment_fingerprint"],
        "baseline_id": assessment["baseline_id"], "pipeline_id": assessment["pipeline_id"],
        "entity_id": assessment["entity_id"], "sample_period": assessment["sample_period"],
        "covered_pack_ids": list(assessment["covered_pack_ids"]),
        "baseline_prepared_by": assessment["baseline_prepared_by"],
        "baseline_sha256": assessment["baseline_sha256"],
        "pipeline_result_sha256": assessment["pipeline_result_sha256"],
        "source_count": len(assessment["source_results"]),
        "control_count": len(assessment["control_results"]), "passed": assessment["passed"],
        "sample_classification": assessment.get("sample_classification", "demonstration"),
        "real_sample_evidence": assessment.get("real_sample_evidence") is True,
        "source_independence_sha256": assessment.get("source_independence_sha256"),
        "anonymization_sha256": assessment.get("anonymization_sha256"),
        "review_current": bool(review), "decision": review.get("decision") if review else None,
        "review_id": review.get("review_id") if review else None,
        "review_actor": review.get("actor") if review else None,
        "reviewed_at": review.get("reviewed_at") if review else None,
        "review_rationale_sha256": (
            hashlib.sha256(str(review.get("rationale") or "").encode("utf-8")).hexdigest()
            if review else None
        ),
        "review_evidence_references": (
            list(review.get("evidence_references") or []) if review else []
        ),
        "raw_source_values_returned": False, "external_actions_performed": False,
    }
