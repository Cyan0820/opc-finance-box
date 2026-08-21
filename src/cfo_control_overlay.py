from __future__ import annotations

from collections.abc import Iterable
from typing import Any


class CfoControlOverlayError(ValueError):
    """Raised when a Box cannot be mapped to a safe CFO control overlay."""


_CORE_OBJECTIVES = (
    "cash_and_runway_visibility",
    "receivables_and_settlement_completeness",
    "period_cutoff_and_source_evidence",
    "tax_workpaper_readiness",
)

_CORE_REVIEW_QUESTIONS = (
    "what_changed_vs_prior_month",
    "which_cash_assumption_can_break_runway",
    "which_receivable_or_channel_is_most_concentrated",
    "which_exception_blocks_close",
)

_BUSINESS_MODELS: dict[str, dict[str, tuple[str, ...]]] = {
    "game_studio": {
        "objectives": (
            "platform_settlement_completeness",
            "refund_chargeback_and_deferred_revenue",
            "title_and_project_contribution_margin",
            "license_cloud_and_prepaid_period_release",
            "platform_and_title_concentration",
        ),
        "questions": (
            "which_platform_or_title_drives_concentration",
            "which_project_or_campaign_reduced_contribution",
            "which_license_or_cloud_cost_lacks_period_evidence",
        ),
    },
    "dtc_store": {
        "objectives": (
            "order_payment_refund_reconciliation",
            "returns_fulfillment_and_inventory_cutoff",
            "inventory_and_landed_cost_evidence",
            "processor_payout_completeness",
            "channel_and_product_contribution_margin",
        ),
        "questions": (
            "which_channel_product_or_refund_changed_margin",
            "do_orders_payments_refunds_and_payouts_share_scope",
            "which_inventory_or_landed_cost_assumption_is_unverified",
        ),
    },
    "marketplace_seller": {
        "objectives": (
            "order_finance_inventory_three_way_scope",
            "marketplace_fees_reserves_and_payouts",
            "returns_and_multiwarehouse_cutoff",
            "inventory_scope_and_valuation_evidence",
            "marketplace_concentration",
        ),
        "questions": (
            "which_marketplace_fee_reserve_or_return_changed_cash",
            "do_orders_finances_and_inventory_share_scope",
            "which_warehouse_or_inventory_cutoff_is_unsupported",
        ),
    },
}

_CONNECTOR_BOUNDARIES: dict[str, str] = {
    "connector.airwallex": (
        "airwallex_approved_expense_does_not_authorize_posting_or_payment"
    ),
    "connector.amazon_seller": (
        "amazon_current_inventory_is_not_historical_period_end"
    ),
    "connector.custom_api": (
        "custom_api_scope_and_completeness_require_independent_review"
    ),
    "connector.file_import": (
        "file_import_source_completeness_must_be_confirmed"
    ),
    "connector.paypal": (
        "paypal_balance_activity_does_not_prove_full_order_scope"
    ),
    "connector.shipbob": (
        "shipbob_fulfillment_does_not_authorize_inventory_accounting"
    ),
    "connector.shopify": (
        "shopify_order_evidence_does_not_prove_processor_settlement"
    ),
    "connector.stripe": (
        "stripe_processor_evidence_is_not_bank_or_ledger"
    ),
    "connector.wise": "wise_bank_evidence_is_not_general_ledger",
    "connector.woocommerce": (
        "woocommerce_orders_do_not_prove_processor_settlement"
    ),
    "connector.xero": (
        "xero_trial_balance_is_control_total_not_period_activity"
    ),
}


def _unique(items: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(items))


def _business_model_type_ids(pack_ids: frozenset[str]) -> list[str]:
    has_game = "industry.game_studio" in pack_ids
    has_commerce = "industry.commerce" in pack_ids
    if has_game == has_commerce:
        raise CfoControlOverlayError(
            "CFO control overlay requires exactly one supported industry Pack"
        )
    if has_game:
        return ["game_studio"]
    models = []
    if "channel.dtc_storefront" in pack_ids:
        models.append("dtc_store")
    if "channel.marketplace_commerce" in pack_ids:
        models.append("marketplace_seller")
    if not models:
        raise CfoControlOverlayError(
            "Commerce CFO control overlay requires a DTC or Marketplace channel"
        )
    return models


def build_cfo_control_overlay(
    enabled_pack_ids: Iterable[str], *, runtime_fingerprint: str | None = None,
) -> dict[str, Any]:
    """Build a value-free monthly control overlay from installed Pack contracts."""
    if isinstance(enabled_pack_ids, (str, bytes)):
        raise CfoControlOverlayError("enabled Pack IDs must be a sequence")
    normalized = []
    for value in enabled_pack_ids:
        if not isinstance(value, str) or not value:
            raise CfoControlOverlayError("enabled Pack IDs must be non-empty strings")
        normalized.append(value)
    pack_ids = frozenset(normalized)
    models = _business_model_type_ids(pack_ids)
    objectives = list(_CORE_OBJECTIVES)
    questions = list(_CORE_REVIEW_QUESTIONS)
    for model in models:
        objectives.extend(_BUSINESS_MODELS[model]["objectives"])
        questions.extend(_BUSINESS_MODELS[model]["questions"])

    connector_pack_ids = sorted(
        item for item in pack_ids if item.startswith("connector.")
    )
    uncovered = [
        item for item in connector_pack_ids if item not in _CONNECTOR_BOUNDARIES
    ]
    boundaries = [
        _CONNECTOR_BOUNDARIES[item]
        for item in connector_pack_ids
        if item in _CONNECTOR_BOUNDARIES
    ]
    if uncovered:
        boundaries.append(
            "unmapped_connector_requires_method_extension_before_shadow_use"
        )

    result: dict[str, Any] = {
        "schema_version": 1,
        "artifact_type": "cfo_business_control_overlay",
        "catalog_version": 1,
        "business_model_type_ids": models,
        "monthly_control_objective_type_ids": _unique(objectives),
        "source_boundary_type_ids": _unique(boundaries),
        "founder_review_question_type_ids": _unique(questions),
        "method_extension_required": bool(uncovered),
        "uncovered_connector_pack_count": len(uncovered),
        "generated_from_pack_contracts": True,
        "editable_compiled_contract": True,
        "financial_values_returned": False,
        "source_records_returned": False,
        "credentials_returned": False,
        "private_paths_returned": False,
        "authoritative_completion_inferred": False,
        "posting_payment_or_filing_authorized": False,
        "external_actions_performed": False,
    }
    if runtime_fingerprint is not None:
        if (
            not isinstance(runtime_fingerprint, str)
            or len(runtime_fingerprint) != 64
            or any(char not in "0123456789abcdef" for char in runtime_fingerprint)
        ):
            raise CfoControlOverlayError("runtime fingerprint is invalid")
        result["runtime_fingerprint"] = runtime_fingerprint
    return result
