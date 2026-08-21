from __future__ import annotations

from collections.abc import Iterable
from copy import deepcopy
from typing import Any

from .cfo_control_overlay import (
    CfoControlOverlayError,
    build_cfo_control_overlay,
)


class CfoMetricCatalogError(ValueError):
    """Raised when a Box cannot be mapped to a safe CFO metric catalog."""


def _metric(
    metric_type_id: str,
    model_scope_type_id: str,
    value_type_id: str,
    operator_type_id: str,
    operand_type_ids: tuple[str, ...],
    required_data_domain_ids: tuple[str, ...],
    required_control_type_ids: tuple[str, ...],
    decision_use_type_id: str,
    aggregation_policy_type_id: str,
    *,
    scale: int | None = None,
    definition_version: int = 1,
    nonpositive_denominator_policy: str | None = None,
) -> dict[str, Any]:
    formula: dict[str, Any] = {
        "operator_type_id": operator_type_id,
        "operand_type_ids": list(operand_type_ids),
        "missing_operand_policy": "not_available",
    }
    if operator_type_id in {"safe_divide", "safe_divide_scaled"}:
        formula["zero_denominator_policy"] = "not_available"
    if scale is not None:
        formula["scale"] = scale
    if nonpositive_denominator_policy is not None:
        formula["nonpositive_denominator_policy"] = nonpositive_denominator_policy
    return {
        "metric_type_id": metric_type_id,
        "model_scope_type_id": model_scope_type_id,
        "definition_version": definition_version,
        "cadence_type_id": "monthly",
        "value_type_id": value_type_id,
        "formula": formula,
        "required_data_domain_ids": list(required_data_domain_ids),
        "required_control_type_ids": list(required_control_type_ids),
        "decision_use_type_id": decision_use_type_id,
        "aggregation_policy_type_id": aggregation_policy_type_id,
        "definition_only": True,
    }


_CORE_METRICS = (
    _metric(
        "cash_runway_months", "finance_core", "months", "safe_divide",
        ("unrestricted_cash_balance", "trailing_three_month_average_net_cash_burn"),
        ("bank_activity", "general_ledger"),
        (
            "cash_accounts_reconciled_to_ledger",
            "three_month_period_continuity_confirmed",
            "nonpositive_burn_returns_not_available",
        ),
        "decide_cash_preservation_or_growth_capacity",
        "recompute_after_explicit_currency_conversion",
        definition_version=2,
        nonpositive_denominator_policy="not_available",
    ),
    _metric(
        "overdue_receivable_ratio", "finance_core", "ratio", "safe_divide",
        ("overdue_receivable_balance", "gross_receivable_balance"),
        ("revenue_evidence", "general_ledger"),
        (
            "receivable_aging_reconciled_to_ledger",
            "credit_and_disputed_balances_reviewed",
        ),
        "decide_collection_and_concentration_response",
        "recompute_from_scoped_operands_never_average_ratios",
    ),
    _metric(
        "unreconciled_cash_item_count", "finance_core", "count", "count",
        ("unresolved_bank_reconciliation_items",),
        ("bank_activity", "general_ledger"),
        ("duplicate_and_timing_items_deduplicated", "entity_scope_confirmed"),
        "decide_close_blocker_escalation",
        "sum_counts_after_scope_deduplication",
    ),
    _metric(
        "close_blocker_count", "finance_core", "count", "count",
        ("unresolved_authoritative_close_blockers",),
        ("opening_trial_balance", "general_ledger", "bank_activity"),
        (
            "only_authoritative_verifier_blockers_counted",
            "operator_reports_do_not_clear_blockers",
        ),
        "decide_close_sequence_and_owner_escalation",
        "sum_counts_after_scope_deduplication",
    ),
)


_MODEL_METRICS: dict[str, tuple[dict[str, Any], ...]] = {
    "game_studio": (
        _metric(
            "game_platform_net_revenue", "game_studio", "currency", "subtract",
            ("platform_gross_settlement", "refunds_chargebacks_and_platform_fees"),
            ("channel_settlements", "revenue_evidence"),
            ("platform_statement_scope_confirmed", "settlement_currency_confirmed"),
            "decide_platform_and_title_revenue_response",
            "sum_only_within_currency_then_convert_explicitly",
        ),
        _metric(
            "game_refund_chargeback_rate", "game_studio", "ratio", "safe_divide",
            ("refunds_and_chargebacks", "platform_gross_settlement"),
            ("channel_settlements", "revenue_evidence"),
            ("refund_period_and_platform_scope_aligned",),
            "decide_product_quality_or_channel_risk_response",
            "recompute_from_scoped_operands_never_average_ratios",
        ),
        _metric(
            "game_deferred_revenue_closing_balance", "game_studio", "currency",
            "rollforward",
            ("opening_deferred_revenue", "current_period_deferrals", "current_period_releases"),
            ("channel_settlements", "revenue_evidence", "general_ledger"),
            ("approved_revenue_policy_bound", "release_evidence_period_matched"),
            "decide_revenue_cutoff_and_policy_review",
            "sum_only_within_currency_then_convert_explicitly",
        ),
        _metric(
            "game_title_contribution_margin_ratio", "game_studio", "ratio",
            "safe_divide",
            ("title_contribution", "title_net_revenue"),
            ("channel_settlements", "expense_evidence", "operating_kpis"),
            ("shared_cost_allocation_evidence_confirmed", "title_scope_confirmed"),
            "decide_title_investment_or_cost_reset",
            "recompute_from_scoped_operands_never_average_ratios",
        ),
        _metric(
            "game_platform_revenue_concentration_ratio", "game_studio", "ratio",
            "max_share",
            ("platform_net_revenue_by_platform", "total_platform_net_revenue"),
            ("channel_settlements",),
            ("complete_platform_population_confirmed",),
            "decide_platform_concentration_mitigation",
            "recompute_from_scoped_operands_never_average_ratios",
        ),
        _metric(
            "game_prepaid_release_evidence_coverage_ratio", "game_studio", "ratio",
            "safe_divide",
            ("prepaid_release_with_period_evidence", "total_prepaid_release_candidate"),
            ("expense_evidence", "general_ledger"),
            ("license_cloud_and_prepaid_policy_approved", "period_evidence_bound"),
            "decide_prepaid_release_or_hold",
            "recompute_from_scoped_operands_never_average_ratios",
        ),
    ),
    "dtc_store": (
        _metric(
            "dtc_net_sales", "dtc_store", "currency", "subtract",
            (
                "gross_order_sales_ex_tax_including_shipping",
                "discounts_and_refunds_ex_tax",
            ),
            ("orders", "refunds_and_returns", "revenue_evidence"),
            ("order_and_refund_period_scope_aligned", "tax_inclusive_policy_confirmed"),
            "decide_channel_product_and_pricing_response",
            "sum_only_within_currency_then_convert_explicitly",
            definition_version=2,
        ),
        _metric(
            "dtc_refund_return_rate", "dtc_store", "ratio", "safe_divide",
            ("refunds_ex_tax", "gross_merchandise_sales_ex_tax"),
            ("orders", "refunds_and_returns"),
            ("return_authorization_and_receipt_scope_aligned",),
            "decide_product_quality_and_return_policy_response",
            "recompute_from_scoped_operands_never_average_ratios",
            definition_version=2,
        ),
        _metric(
            "dtc_order_to_payout_reconciliation_coverage_ratio", "dtc_store", "ratio",
            "safe_divide",
            ("orders_bound_to_processor_and_payout_evidence", "eligible_paid_orders"),
            ("orders", "payments_and_settlements", "bank_activity"),
            ("order_processor_payout_scope_aligned", "timing_differences_classified"),
            "decide_settlement_gap_investigation",
            "recompute_from_scoped_operands_never_average_ratios",
        ),
        _metric(
            "dtc_product_contribution_margin_ratio", "dtc_store", "ratio",
            "safe_divide",
            ("product_contribution", "product_net_sales"),
            ("orders", "inventory", "fulfillment", "expense_evidence"),
            ("landed_cost_policy_confirmed", "fulfillment_cost_scope_confirmed"),
            "decide_product_assortment_and_acquisition_spend",
            "recompute_from_scoped_operands_never_average_ratios",
        ),
        _metric(
            "dtc_inventory_days_on_hand", "dtc_store", "days",
            "safe_divide_scaled",
            ("average_inventory_cost", "trailing_twelve_month_cost_of_goods_sold"),
            ("inventory", "orders", "general_ledger"),
            ("period_end_inventory_evidence_confirmed", "inventory_cost_basis_approved"),
            "decide_replenishment_and_working_capital_response",
            "recompute_from_scoped_operands_never_average_ratios",
            scale=365,
        ),
        _metric(
            "dtc_processor_payout_gap", "dtc_store", "currency", "absolute_difference",
            ("expected_processor_payout", "observed_bank_payout"),
            ("payments_and_settlements", "bank_activity"),
            ("processor_fee_refund_and_timing_bridge_confirmed",),
            "decide_processor_exception_escalation",
            "sum_only_within_currency_then_convert_explicitly",
        ),
    ),
    "marketplace_seller": (
        _metric(
            "marketplace_net_settlement_proceeds", "marketplace_seller", "currency",
            "subtract",
            ("marketplace_gross_sales", "fees_refunds_reserves_and_adjustments"),
            ("orders", "payments_and_settlements", "refunds_and_returns"),
            ("settlement_reference_completeness_confirmed", "transaction_layers_not_double_counted"),
            "decide_marketplace_cash_and_fee_response",
            "sum_only_within_currency_then_convert_explicitly",
        ),
        _metric(
            "marketplace_fee_rate", "marketplace_seller", "ratio", "safe_divide",
            ("marketplace_fees", "marketplace_gross_merchandise_sales_ex_tax"),
            ("payments_and_settlements", "orders"),
            ("fee_types_and_tax_treatment_reviewed",),
            "decide_fee_leakage_and_channel_economics_response",
            "recompute_from_scoped_operands_never_average_ratios",
            definition_version=2,
        ),
        _metric(
            "marketplace_reserve_to_sales_ratio", "marketplace_seller", "ratio",
            "safe_divide",
            ("marketplace_reserve_balance", "marketplace_gross_sales"),
            ("payments_and_settlements", "orders"),
            ("reserve_balance_period_and_currency_confirmed",),
            "decide_cash_reserve_and_liquidity_response",
            "recompute_from_scoped_operands_never_average_ratios",
        ),
        _metric(
            "marketplace_three_way_scope_match_rate", "marketplace_seller", "ratio",
            "safe_divide",
            ("orders_matched_across_orders_finances_and_inventory", "eligible_marketplace_orders"),
            ("orders", "payments_and_settlements", "inventory"),
            ("seller_marketplace_and_period_scope_identical", "hashed_cross_source_keys_reviewed"),
            "decide_three_way_scope_gap_investigation",
            "recompute_from_scoped_operands_never_average_ratios",
        ),
        _metric(
            "marketplace_inventory_scope_coverage_ratio", "marketplace_seller", "ratio",
            "safe_divide",
            ("inventory_units_with_warehouse_and_cost_scope", "total_inventory_units_in_scope"),
            ("inventory", "fulfillment"),
            ("current_inventory_not_used_as_historical_period_end", "warehouse_scope_confirmed"),
            "decide_inventory_evidence_or_valuation_hold",
            "recompute_from_scoped_operands_never_average_ratios",
        ),
        _metric(
            "marketplace_revenue_concentration_ratio", "marketplace_seller", "ratio",
            "max_share",
            ("net_revenue_by_marketplace", "total_marketplace_net_revenue"),
            ("orders", "payments_and_settlements"),
            ("complete_marketplace_population_confirmed",),
            "decide_marketplace_concentration_mitigation",
            "recompute_from_scoped_operands_never_average_ratios",
        ),
    ),
}


_SOURCE_MAPPING_COVERAGE: tuple[dict[str, Any], ...] = (
    {
        "source_type_id": "pipeline",
        "source_id": "finance.bank_statement_close",
        "required_pack_ids": ["core.finance"],
        "coverage_status": "executable",
        "metric_type_ids": ["unreconciled_cash_item_count"],
        "auto_confirmed_control_type_ids": ["entity_scope_confirmed"],
        "human_control_type_ids": ["duplicate_and_timing_items_deduplicated"],
        "derivation_type_ids": ["direct_deterministic_count"],
    },
    {
        "source_type_id": "pipeline",
        "source_id": "finance.month_close_control",
        "required_pack_ids": ["core.finance"],
        "coverage_status": "executable",
        "metric_type_ids": ["close_blocker_count"],
        "auto_confirmed_control_type_ids": [
            "only_authoritative_verifier_blockers_counted",
            "operator_reports_do_not_clear_blockers",
        ],
        "human_control_type_ids": [],
        "derivation_type_ids": ["deterministic_list_count"],
    },
    {
        "source_type_id": "pipeline",
        "source_id": "commerce.channel_close",
        "required_pack_ids": ["core.finance", "channel.dtc_storefront"],
        "coverage_status": "executable",
        "metric_type_ids": [
            "dtc_net_sales", "dtc_refund_return_rate",
            "dtc_product_contribution_margin_ratio",
        ],
        "auto_confirmed_control_type_ids": [
            "order_and_refund_period_scope_aligned",
            "return_authorization_and_receipt_scope_aligned",
            "fulfillment_cost_scope_confirmed",
        ],
        "human_control_type_ids": [
            "tax_inclusive_policy_confirmed", "landed_cost_policy_confirmed",
        ],
        "derivation_type_ids": [
            "sum_across_channels_within_entity_period_currency",
        ],
    },
    {
        "source_type_id": "service",
        "source_id": "game.project_profitability",
        "required_pack_ids": ["core.finance", "industry.game_studio"],
        "coverage_status": "executable",
        "metric_type_ids": ["game_title_contribution_margin_ratio"],
        "auto_confirmed_control_type_ids": ["title_scope_confirmed"],
        "human_control_type_ids": ["shared_cost_allocation_evidence_confirmed"],
        "derivation_type_ids": ["direct_project_scope_mapping"],
    },
    {
        "source_type_id": "pipeline",
        "source_id": "game.channel_settlement_close",
        "required_pack_ids": ["core.finance", "industry.game_studio"],
        "coverage_status": "blocked_source_contract",
        "metric_type_ids": ["game_platform_net_revenue", "game_refund_chargeback_rate"],
        "coverage_blocker_type_ids": [
            "refund_fee_chargeback_components_not_separately_exposed",
        ],
    },
    {
        "source_type_id": "pipeline",
        "source_id": "dtc.shopify_stripe_daily_close",
        "required_pack_ids": ["core.finance", "channel.dtc_storefront"],
        "coverage_status": "blocked_source_contract",
        "metric_type_ids": ["dtc_net_sales", "dtc_refund_return_rate"],
        "coverage_blocker_type_ids": ["monthly_ex_tax_sales_scope_not_proven"],
    },
    {
        "source_type_id": "pipeline",
        "source_id": "dtc.shopify_stripe_month_close",
        "required_pack_ids": [
            "core.finance", "channel.dtc_storefront", "connector.shopify",
            "connector.stripe", "feature.shopify_stripe_order_to_cash",
        ],
        "coverage_status": "executable",
        "metric_type_ids": ["dtc_net_sales", "dtc_refund_return_rate"],
        "auto_confirmed_control_type_ids": ["order_and_refund_period_scope_aligned"],
        "human_control_type_ids": [
            "tax_inclusive_policy_confirmed",
            "return_authorization_and_receipt_scope_aligned",
        ],
        "derivation_type_ids": [
            "close_captured_shopify_created_and_updated_population",
            "successful_refund_component_reconciliation",
            "same_window_shopify_stripe_scope_proof",
        ],
    },
    {
        "source_type_id": "pipeline",
        "source_id": "marketplace.channel_close",
        "required_pack_ids": ["core.finance", "channel.marketplace_commerce"],
        "coverage_status": "executable",
        "metric_type_ids": ["marketplace_fee_rate", "marketplace_revenue_concentration_ratio"],
        "auto_confirmed_control_type_ids": [],
        "human_control_type_ids": [
            "fee_types_and_tax_treatment_reviewed",
            "complete_marketplace_population_confirmed",
        ],
        "derivation_type_ids": [
            "sum_across_marketplaces_within_entity_period_currency",
            "vector_from_marketplace_net_revenue_scopes",
        ],
        "blocked_metric_type_ids": [
            "marketplace_net_settlement_proceeds",
            "marketplace_reserve_to_sales_ratio",
            "marketplace_inventory_scope_coverage_ratio",
        ],
        "coverage_blocker_type_ids": [
            "reserve_and_adjustment_layers_not_separately_exposed",
            "historical_period_end_inventory_cost_scope_not_exposed",
        ],
    },
    {
        "source_type_id": "pipeline",
        "source_id": "amazon_seller.marketplace_close",
        "required_pack_ids": [
            "core.finance", "channel.marketplace_commerce", "connector.amazon_seller",
        ],
        "coverage_status": "executable",
        "metric_type_ids": ["marketplace_three_way_scope_match_rate"],
        "auto_confirmed_control_type_ids": [
            "seller_marketplace_and_period_scope_identical",
        ],
        "human_control_type_ids": ["hashed_cross_source_keys_reviewed"],
        "derivation_type_ids": ["hashed_fba_order_finance_inventory_scope_match"],
    },
)


def build_cfo_metric_catalog(
    enabled_pack_ids: Iterable[str], *, runtime_fingerprint: str | None = None,
) -> dict[str, Any]:
    """Build value-free metric definitions from the Box's installed Pack set."""
    enabled = set(enabled_pack_ids)
    try:
        overlay = build_cfo_control_overlay(enabled)
    except CfoControlOverlayError as exc:
        raise CfoMetricCatalogError(str(exc)) from exc
    models = list(overlay["business_model_type_ids"])
    metrics = [deepcopy(item) for item in _CORE_METRICS]
    for model in models:
        metrics.extend(deepcopy(item) for item in _MODEL_METRICS[model])
    metric_ids = [item["metric_type_id"] for item in metrics]
    if len(metric_ids) != len(set(metric_ids)):
        raise CfoMetricCatalogError("CFO metric catalog contains duplicate metric IDs")

    source_mappings = [
        deepcopy(item) for item in _SOURCE_MAPPING_COVERAGE
        if set(item["required_pack_ids"]) <= enabled
    ]
    result: dict[str, Any] = {
        "schema_version": 1,
        "artifact_type": "cfo_metric_catalog",
        "catalog_version": 5,
        "business_model_type_ids": models,
        "metric_definitions": metrics,
        "metric_count": len(metrics),
        "source_mapping_definitions": source_mappings,
        "source_mapping_count": len(source_mappings),
        "missing_input_policy": "not_available_never_zero_or_inferred",
        "currency_policy": "preserve_source_currency_until_explicit_conversion",
        "ratio_aggregation_policy": "recompute_from_scoped_operands_never_average",
        "legal_entity_policy": "calculate_per_legal_entity_before_management_view",
        "connector_selection_does_not_reduce_required_data_domains": True,
        "generated_from_pack_contracts": True,
        "editable_compiled_contract": True,
        "evaluation_contract": {
            "service_id": "core.evaluate_cfo_metrics",
            "capability": "finance.cfo_metrics",
            "entity_scope": "statutory",
            "cadence_type_id": "monthly",
            "currency_basis_type_id": "legal_entity_functional_currency",
            "current_box_binding_required": True,
            "formula_allowlist_enforced": True,
            "arbitrary_expression_execution_permitted": False,
            "implicit_currency_conversion_permitted": False,
            "partial_evaluation_returns_explicit_statuses": True,
        },
        "deterministic_evaluator_available": True,
        "trusted_source_operand_assembly_available": any(
            item["coverage_status"] == "executable" for item in source_mappings
        ),
        "caller_supplied_source_results_accepted_for_assembly": False,
        "metric_values_returned": False,
        "formula_evaluated": False,
        "source_records_returned": False,
        "credentials_returned": False,
        "private_paths_returned": False,
        "authoritative_accounting_or_statutory_truth_inferred": False,
        "posting_payment_or_filing_authorized": False,
        "external_actions_performed": False,
    }
    if runtime_fingerprint is not None:
        if (
            not isinstance(runtime_fingerprint, str)
            or len(runtime_fingerprint) != 64
            or any(char not in "0123456789abcdef" for char in runtime_fingerprint)
        ):
            raise CfoMetricCatalogError("runtime fingerprint is invalid")
        result["runtime_fingerprint"] = runtime_fingerprint
    return result
