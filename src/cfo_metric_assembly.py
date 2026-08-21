from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from typing import Any, TYPE_CHECKING

from .cfo_metric_catalog import build_cfo_metric_catalog
from .cfo_metric_evaluator import evaluate_cfo_metrics

if TYPE_CHECKING:  # pragma: no cover
    from .box_runtime import BoxRuntime


class CfoMetricAssemblyError(ValueError):
    """Raised when a trusted source result cannot satisfy the assembly contract."""


_PERIOD_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_CURRENCY_PATTERN = re.compile(r"^[A-Z]{3}$")
_SUPPORTED_SOURCES = {
    ("pipeline", "finance.bank_statement_close"),
    ("pipeline", "finance.month_close_control"),
    ("pipeline", "commerce.channel_close"),
    ("pipeline", "dtc.shopify_stripe_month_close"),
    ("pipeline", "marketplace.channel_close"),
    ("pipeline", "amazon_seller.marketplace_close"),
    ("service", "game.project_profitability"),
}
_KNOWN_BLOCKED_SOURCES = {
    ("pipeline", "game.channel_settlement_close"): (
        "blocked_source_contract",
        ["refund_fee_chargeback_components_not_separately_exposed"],
    ),
    ("pipeline", "dtc.shopify_stripe_daily_close"): (
        "blocked_source_contract",
        ["monthly_ex_tax_sales_scope_not_proven"],
    ),
}
_TRUSTED_EXECUTION_TOKEN = object()


def _fingerprint(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _decimal(value: Any, label: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise CfoMetricAssemblyError(f"{label} must be a finite decimal")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise CfoMetricAssemblyError(f"{label} must be a finite decimal") from exc
    if not parsed.is_finite():
        raise CfoMetricAssemblyError(f"{label} must be a finite decimal")
    return parsed


def _decimal_text(value: Decimal) -> str:
    if value == 0:
        return "0"
    text = format(value.normalize(), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _source_collection(
    source_type_id: str,
    source_id: str,
    source_result: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "artifact_type": "cfo_metric_operand_assembly_collection",
        "assembly_contract_version": 1,
        "source_type_id": source_type_id,
        "source_id": source_id,
        "source_result_fingerprint": _fingerprint(source_result),
        "coverage_status": "executable",
        "coverage_blocker_type_ids": [],
        "assemblies": [],
        "assembly_count": 0,
        "source_result_trusted_at_assembly_time": True,
        "caller_supplied_source_result_accepted": False,
        "raw_source_records_returned": False,
        "credentials_returned": False,
        "private_paths_returned": False,
        "implicit_currency_conversion_performed": False,
        "human_controls_auto_confirmed": False,
        "authoritative_accounting_or_statutory_truth_inferred": False,
        "posting_payment_or_filing_authorized": False,
        "external_actions_performed": False,
    }


def _blocked_collection(
    source_type_id: str,
    source_id: str,
    source_result: dict[str, Any],
    status: str,
    blockers: list[str],
) -> dict[str, Any]:
    result = _source_collection(source_type_id, source_id, source_result)
    result["coverage_status"] = status
    result["coverage_blocker_type_ids"] = list(blockers)
    return result


def _provenance(
    operand_type_ids: list[str],
    *,
    source_type_id: str,
    source_id: str,
    source_result_fingerprint: str,
    source_field_type_id: str,
    derivation_type_id: str,
) -> list[dict[str, Any]]:
    return [{
        "operand_type_id": operand_type_id,
        "source_type_id": source_type_id,
        "source_id": source_id,
        "source_field_type_id": source_field_type_id,
        "derivation_type_id": derivation_type_id,
        "source_result_fingerprint": source_result_fingerprint,
    } for operand_type_id in operand_type_ids]


def _assembly(
    runtime: "BoxRuntime",
    *,
    source_type_id: str,
    source_id: str,
    source_result_fingerprint: str,
    entity_id: str,
    period: str,
    currency: str,
    metric_type_ids: list[str],
    operand_values: dict[str, Decimal],
    vector_operand_values: dict[str, list[Decimal]] | None = None,
    confirmed_control_type_ids: list[str],
    operand_provenance: list[dict[str, Any]],
    dimension_scope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if entity_id not in runtime.entities.ids():
        raise CfoMetricAssemblyError("metric source crossed the current Box entity scope")
    if not _PERIOD_PATTERN.fullmatch(period):
        raise CfoMetricAssemblyError("metric source period must use YYYY-MM")
    if not _CURRENCY_PATTERN.fullmatch(currency):
        raise CfoMetricAssemblyError("metric source currency must use an uppercase three-letter code")
    catalog = build_cfo_metric_catalog(
        {item["id"] for item in runtime.snapshot()["packs"]},
        runtime_fingerprint=runtime.snapshot()["fingerprint"],
    )
    definitions = {
        item["metric_type_id"]: item for item in catalog["metric_definitions"]
    }
    unknown = sorted(set(metric_type_ids) - set(definitions))
    if unknown:
        raise CfoMetricAssemblyError(
            "metric source mapped metrics not enabled by this Box: " + ", ".join(unknown)
        )
    allowed_operands = {
        operand_id for metric_type_id in metric_type_ids
        for operand_id in definitions[metric_type_id]["formula"]["operand_type_ids"]
    }
    vectors = vector_operand_values or {}
    if not set(operand_values) <= allowed_operands or not set(vectors) <= allowed_operands:
        raise CfoMetricAssemblyError("metric source produced operands outside selected metrics")
    if set(operand_values) & set(vectors):
        raise CfoMetricAssemblyError("metric source produced the same operand as scalar and vector")
    if any(not isinstance(values, list) or not values for values in vectors.values()):
        raise CfoMetricAssemblyError("metric source vector operands must be non-empty lists")
    allowed_controls = {
        control_id for metric_type_id in metric_type_ids
        for control_id in definitions[metric_type_id]["required_control_type_ids"]
    }
    if not set(confirmed_control_type_ids) <= allowed_controls:
        raise CfoMetricAssemblyError("metric source confirmed controls outside selected metrics")
    pending_controls = sorted(allowed_controls - set(confirmed_control_type_ids))
    canonical_operands = {
        key: _decimal_text(value) for key, value in sorted(operand_values.items())
    }
    canonical_vectors = {
        key: [_decimal_text(value) for value in values]
        for key, values in sorted(vectors.items())
    }
    request: dict[str, Any] = {
        "runtime_fingerprint": runtime.snapshot()["fingerprint"],
        "period": period,
        "currency": currency,
        "metric_type_ids": metric_type_ids,
        "operand_values": canonical_operands,
        "confirmed_control_type_ids": sorted(confirmed_control_type_ids),
    }
    if canonical_vectors:
        request["vector_operand_values"] = canonical_vectors
    if dimension_scope is not None:
        request["dimension_scope"] = dimension_scope
    functional_currency = runtime.entities.get(entity_id).functional_currency
    evaluation_preview = None
    evaluation_status = "blocked_source_currency_not_functional_currency"
    evaluation_blockers = ["explicit_fx_conversion_required"]
    if currency == functional_currency:
        evaluation_preview = evaluate_cfo_metrics(runtime, entity_id, request)
        evaluation_status = "evaluated_candidate"
        evaluation_blockers = []
    identity = {
        "source_result_fingerprint": source_result_fingerprint,
        "entity_id": entity_id,
        "period": period,
        "currency": currency,
        "dimension_scope": dimension_scope,
        "metric_type_ids": metric_type_ids,
    }
    return {
        "assembly_id": _fingerprint(identity)[:24],
        "entity_id": entity_id,
        "period": period,
        "currency": currency,
        "currency_basis_type_id": (
            "legal_entity_functional_currency"
            if currency == functional_currency else "source_currency_requires_explicit_fx"
        ),
        **({"dimension_scope": dimension_scope} if dimension_scope is not None else {}),
        "metric_type_ids": list(metric_type_ids),
        "operand_values": canonical_operands,
        "vector_operand_values": canonical_vectors,
        "confirmed_control_type_ids": sorted(confirmed_control_type_ids),
        "pending_control_type_ids": pending_controls,
        "operand_provenance": operand_provenance,
        "source_result_fingerprint": source_result_fingerprint,
        "evaluation_status": evaluation_status,
        "evaluation_blocker_type_ids": evaluation_blockers,
        "evaluation_request": request if currency == functional_currency else None,
        "evaluation_preview": evaluation_preview,
        "candidate_only": True,
        "human_review_still_required": bool(pending_controls),
        "raw_source_records_returned": False,
        "implicit_currency_conversion_performed": False,
        "authoritative_accounting_or_statutory_truth": False,
        "external_actions_performed": False,
    }


def _bank_assemblies(
    runtime: "BoxRuntime", source_type_id: str, source_id: str,
    source_result: dict[str, Any], source_fingerprint: str,
) -> list[dict[str, Any]]:
    if not source_result.get("ready"):
        return []
    lineage = source_result.get("lineage") or {}
    output = (
        ((source_result.get("services") or {}).get("bank_reconciliation_candidate") or {})
        .get("output") or {}
    )
    entity_id = str(lineage.get("entity_id") or "")
    period = str(lineage.get("period") or "")
    pending = _decimal(output.get("pending_count"), "bank pending_count")
    return [_assembly(
        runtime,
        source_type_id=source_type_id, source_id=source_id,
        source_result_fingerprint=source_fingerprint,
        entity_id=entity_id, period=period,
        currency=runtime.entities.get(entity_id).functional_currency,
        metric_type_ids=["unreconciled_cash_item_count"],
        operand_values={"unresolved_bank_reconciliation_items": pending},
        confirmed_control_type_ids=["entity_scope_confirmed"],
        operand_provenance=_provenance(
            ["unresolved_bank_reconciliation_items"], source_type_id=source_type_id,
            source_id=source_id, source_result_fingerprint=source_fingerprint,
            source_field_type_id="bank_reconciliation_pending_count",
            derivation_type_id="direct_deterministic_count",
        ),
    )]


def _month_close_assemblies(
    runtime: "BoxRuntime", source_type_id: str, source_id: str,
    source_result: dict[str, Any], source_fingerprint: str,
) -> list[dict[str, Any]]:
    services = source_result.get("services") or {}
    output = ((services.get("month_close_control") or {}).get("output") or {})
    lineage = source_result.get("lineage") or {}
    entity_id = str(lineage.get("entity_id") or output.get("entity_id") or "")
    period = str(lineage.get("period") or output.get("period") or "")
    issues = output.get("issues")
    if not isinstance(issues, list) or not entity_id or not period:
        return []
    return [_assembly(
        runtime,
        source_type_id=source_type_id, source_id=source_id,
        source_result_fingerprint=source_fingerprint,
        entity_id=entity_id, period=period,
        currency=runtime.entities.get(entity_id).functional_currency,
        metric_type_ids=["close_blocker_count"],
        operand_values={"unresolved_authoritative_close_blockers": Decimal(len(issues))},
        confirmed_control_type_ids=[
            "only_authoritative_verifier_blockers_counted",
            "operator_reports_do_not_clear_blockers",
        ],
        operand_provenance=_provenance(
            ["unresolved_authoritative_close_blockers"], source_type_id=source_type_id,
            source_id=source_id, source_result_fingerprint=source_fingerprint,
            source_field_type_id="authoritative_month_close_issue_population",
            derivation_type_id="deterministic_list_count",
        ),
    )]


def _commerce_assemblies(
    runtime: "BoxRuntime", source_type_id: str, source_id: str,
    source_result: dict[str, Any], source_fingerprint: str,
) -> list[dict[str, Any]]:
    if not source_result.get("ready"):
        return []
    services = source_result.get("services") or {}
    refunds = ((services.get("refund_summary") or {}).get("output") or {}).get(
        "refund_summary"
    ) or []
    reconciliations = (
        ((services.get("order_settlement_reconciliation") or {}).get("output") or {})
        .get("reconciliations") or []
    )
    returns_ready = bool(
        ((services.get("return_inventory_reconciliation") or {}).get("output") or {})
        .get("ready")
    )
    refund_buckets: dict[tuple[str, str, str], dict[str, Decimal]] = defaultdict(
        lambda: defaultdict(Decimal)
    )
    for index, row in enumerate(refunds):
        key = (str(row.get("entity_id") or ""), str(row.get("period") or ""), str(row.get("currency") or ""))
        for field in (
            "gross_order_sales_ex_tax_including_shipping",
            "discounts_and_refunds_ex_tax",
            "gross_merchandise_sales_ex_tax",
            "refunds_ex_tax",
        ):
            refund_buckets[key][field] += _decimal(row.get(field), f"refund_summary[{index}].{field}")
    contribution_buckets: dict[tuple[str, str, str], dict[str, Decimal]] = defaultdict(
        lambda: defaultdict(Decimal)
    )
    for index, row in enumerate(reconciliations):
        key = (str(row.get("entity_id") or ""), str(row.get("period") or ""), str(row.get("currency") or ""))
        contribution_buckets[key]["product_contribution"] += _decimal(
            row.get("contribution_after_channel_fees"),
            f"reconciliations[{index}].contribution_after_channel_fees",
        )
        contribution_buckets[key]["product_net_sales"] += _decimal(
            row.get("net_revenue_ex_tax"), f"reconciliations[{index}].net_revenue_ex_tax",
        )
    results = []
    for key in sorted(set(refund_buckets) | set(contribution_buckets)):
        refund_values = refund_buckets.get(key, {})
        contribution_values = contribution_buckets.get(key, {})
        required_refund_fields = {
            "gross_order_sales_ex_tax_including_shipping",
            "discounts_and_refunds_ex_tax",
            "gross_merchandise_sales_ex_tax",
            "refunds_ex_tax",
        }
        required_contribution_fields = {"product_contribution", "product_net_sales"}
        if set(refund_values) != required_refund_fields or set(contribution_values) != required_contribution_fields:
            raise CfoMetricAssemblyError(
                "commerce metric source did not produce a complete entity-period-currency scope"
            )
        derived_net_sales = (
            refund_values["gross_order_sales_ex_tax_including_shipping"]
            - refund_values["discounts_and_refunds_ex_tax"]
        )
        if abs(derived_net_sales - contribution_values["product_net_sales"]) > Decimal("0.01"):
            raise CfoMetricAssemblyError(
                "commerce net sales operands do not reconcile across trusted service stages"
            )
        operands = {
            **{name: value for name, value in refund_values.items()},
            **{name: value for name, value in contribution_values.items()},
        }
        metric_ids = [
            "dtc_net_sales", "dtc_refund_return_rate",
            "dtc_product_contribution_margin_ratio",
        ]
        confirmed = ["order_and_refund_period_scope_aligned", "fulfillment_cost_scope_confirmed"]
        if returns_ready:
            confirmed.append("return_authorization_and_receipt_scope_aligned")
        results.append(_assembly(
            runtime,
            source_type_id=source_type_id, source_id=source_id,
            source_result_fingerprint=source_fingerprint,
            entity_id=key[0], period=key[1], currency=key[2],
            metric_type_ids=metric_ids,
            operand_values=operands,
            confirmed_control_type_ids=confirmed,
            operand_provenance=[
                *_provenance(
                    list(refund_values), source_type_id=source_type_id, source_id=source_id,
                    source_result_fingerprint=source_fingerprint,
                    source_field_type_id="commerce_refund_scope_aggregate",
                    derivation_type_id="sum_across_channels_within_entity_period_currency",
                ),
                *_provenance(
                    list(contribution_values), source_type_id=source_type_id, source_id=source_id,
                    source_result_fingerprint=source_fingerprint,
                    source_field_type_id="commerce_order_settlement_scope_aggregate",
                    derivation_type_id="sum_across_channels_within_entity_period_currency",
                ),
            ],
        ))
    return results


def _marketplace_assemblies(
    runtime: "BoxRuntime", source_type_id: str, source_id: str,
    source_result: dict[str, Any], source_fingerprint: str,
) -> list[dict[str, Any]]:
    if not source_result.get("ready"):
        return []
    rows = (
        (((source_result.get("services") or {}).get("marketplace_fee_reconciliation") or {})
         .get("output") or {}).get("fee_reconciliation") or []
    )
    buckets: dict[tuple[str, str, str], dict[str, Any]] = {}
    for index, row in enumerate(rows):
        key = (
            str(row.get("entity_id") or ""),
            str(row.get("period") or ""),
            str(row.get("currency") or ""),
        )
        channel = str(row.get("channel") or "")
        if not all(key) or not channel:
            raise CfoMetricAssemblyError(
                f"marketplace fee row {index} requires entity, period, currency and channel"
            )
        bucket = buckets.setdefault(key, {
            "channels": {},
            "marketplace_fees": Decimal("0"),
            "marketplace_gross_merchandise_sales_ex_tax": Decimal("0"),
        })
        if channel in bucket["channels"]:
            raise CfoMetricAssemblyError(
                "marketplace metric source produced duplicate channel scope within one "
                "entity-period-currency population"
            )
        bucket["channels"][channel] = _decimal(
            row.get("net_revenue_ex_tax"), f"fee_reconciliation[{index}].net_revenue_ex_tax",
        )
        bucket["marketplace_fees"] += _decimal(
            row.get("channel_and_payment_fees"),
            f"fee_reconciliation[{index}].channel_and_payment_fees",
        )
        bucket["marketplace_gross_merchandise_sales_ex_tax"] += _decimal(
            row.get("gross_merchandise_sales_ex_tax"),
            f"fee_reconciliation[{index}].gross_merchandise_sales_ex_tax",
        )

    results = []
    for key, bucket in sorted(buckets.items()):
        channels = sorted(bucket["channels"])
        revenue_vector = [bucket["channels"][channel] for channel in channels]
        operands = {
            "marketplace_fees": bucket["marketplace_fees"],
            "marketplace_gross_merchandise_sales_ex_tax": (
                bucket["marketplace_gross_merchandise_sales_ex_tax"]
            ),
            "total_marketplace_net_revenue": sum(revenue_vector, Decimal("0")),
        }
        results.append(_assembly(
            runtime,
            source_type_id=source_type_id, source_id=source_id,
            source_result_fingerprint=source_fingerprint,
            entity_id=key[0], period=key[1], currency=key[2],
            dimension_scope={
                "dimension_type_id": "marketplace_population",
                "dimension_value_ids": channels,
            },
            metric_type_ids=[
                "marketplace_fee_rate", "marketplace_revenue_concentration_ratio",
            ],
            operand_values=operands,
            vector_operand_values={"net_revenue_by_marketplace": revenue_vector},
            confirmed_control_type_ids=[],
            operand_provenance=[
                *_provenance(
                    [
                        "marketplace_fees",
                        "marketplace_gross_merchandise_sales_ex_tax",
                    ],
                    source_type_id=source_type_id, source_id=source_id,
                    source_result_fingerprint=source_fingerprint,
                    source_field_type_id="marketplace_fee_reconciliation_population",
                    derivation_type_id=(
                        "sum_across_marketplaces_within_entity_period_currency"
                    ),
                ),
                *_provenance(
                    ["net_revenue_by_marketplace", "total_marketplace_net_revenue"],
                    source_type_id=source_type_id, source_id=source_id,
                    source_result_fingerprint=source_fingerprint,
                    source_field_type_id="marketplace_net_revenue_population",
                    derivation_type_id="vector_from_marketplace_net_revenue_scopes",
                ),
            ],
        ))
    return results


def _shopify_monthly_assemblies(
    runtime: "BoxRuntime", source_type_id: str, source_id: str,
    source_result: dict[str, Any], source_fingerprint: str,
) -> list[dict[str, Any]]:
    if not source_result.get("ready"):
        return []
    lineage = source_result.get("lineage") or {}
    if lineage.get("canonical_month_scope") is not True:
        return []
    output = (
        ((source_result.get("services") or {}).get("shopify_monthly_commerce_scope") or {})
        .get("output") or {}
    )
    if output.get("canonical_month_scope") is not True:
        return []
    rows = output.get("monthly_commerce_scope") or []
    results = []
    for index, row in enumerate(rows):
        operands = {
            field: _decimal(row.get(field), f"monthly_commerce_scope[{index}].{field}")
            for field in (
                "gross_order_sales_ex_tax_including_shipping",
                "discounts_and_refunds_ex_tax",
                "gross_merchandise_sales_ex_tax",
                "refunds_ex_tax",
            )
        }
        results.append(_assembly(
            runtime,
            source_type_id=source_type_id,
            source_id=source_id,
            source_result_fingerprint=source_fingerprint,
            entity_id=str(row.get("entity_id") or ""),
            period=str(row.get("period") or ""),
            currency=str(row.get("currency") or ""),
            metric_type_ids=["dtc_net_sales", "dtc_refund_return_rate"],
            operand_values=operands,
            confirmed_control_type_ids=["order_and_refund_period_scope_aligned"],
            operand_provenance=_provenance(
                list(operands), source_type_id=source_type_id, source_id=source_id,
                source_result_fingerprint=source_fingerprint,
                source_field_type_id="shopify_monthly_commerce_scope",
                derivation_type_id=(
                    "close_captured_created_orders_and_successful_processed_refunds"
                ),
            ),
        ))
    return results


def _amazon_marketplace_assemblies(
    runtime: "BoxRuntime", source_type_id: str, source_id: str,
    source_result: dict[str, Any], source_fingerprint: str,
) -> list[dict[str, Any]]:
    if not source_result.get("ready"):
        return []
    output = (
        (((source_result.get("services") or {}).get("marketplace_evidence_reconciliation") or {})
         .get("output") or {})
    )
    if not output.get("ready") or output.get("canonical_month_scope") is not True:
        return []
    entity_id = str(output.get("entity_id") or "")
    period = str(output.get("period") or "")
    marketplace_id = str(output.get("marketplace_id") or "")
    if not entity_id or not period or not marketplace_id:
        raise CfoMetricAssemblyError(
            "Amazon marketplace metric source requires entity, canonical month and marketplace"
        )
    return [_assembly(
        runtime,
        source_type_id=source_type_id, source_id=source_id,
        source_result_fingerprint=source_fingerprint,
        entity_id=entity_id, period=period,
        currency=runtime.entities.get(entity_id).functional_currency,
        dimension_scope={
            "dimension_type_id": "marketplace",
            "dimension_value_ids": [marketplace_id],
        },
        metric_type_ids=["marketplace_three_way_scope_match_rate"],
        operand_values={
            "orders_matched_across_orders_finances_and_inventory": _decimal(
                output.get("matched_three_way_order_count"),
                "marketplace evidence matched_three_way_order_count",
            ),
            "eligible_marketplace_orders": _decimal(
                output.get("eligible_three_way_order_count"),
                "marketplace evidence eligible_three_way_order_count",
            ),
        },
        confirmed_control_type_ids=["seller_marketplace_and_period_scope_identical"],
        operand_provenance=_provenance(
            [
                "orders_matched_across_orders_finances_and_inventory",
                "eligible_marketplace_orders",
            ],
            source_type_id=source_type_id, source_id=source_id,
            source_result_fingerprint=source_fingerprint,
            source_field_type_id="amazon_seller_three_way_order_population",
            derivation_type_id="hashed_fba_order_finance_inventory_scope_match",
        ),
    )]


def _game_profitability_assemblies(
    runtime: "BoxRuntime", source_type_id: str, source_id: str,
    source_result: dict[str, Any], source_fingerprint: str,
) -> list[dict[str, Any]]:
    if not source_result.get("ready"):
        return []
    results = []
    for index, row in enumerate(source_result.get("rows") or []):
        project_code = str(row.get("project_code") or "")
        if not project_code:
            raise CfoMetricAssemblyError(f"game profitability row {index} requires project_code")
        results.append(_assembly(
            runtime,
            source_type_id=source_type_id, source_id=source_id,
            source_result_fingerprint=source_fingerprint,
            entity_id=str(row.get("entity_id") or ""),
            period=str(row.get("period") or ""),
            currency=str(row.get("currency") or ""),
            dimension_scope={
                "dimension_type_id": "game_title",
                "dimension_value_ids": [project_code],
            },
            metric_type_ids=["game_title_contribution_margin_ratio"],
            operand_values={
                "title_contribution": _decimal(row.get("contribution"), f"rows[{index}].contribution"),
                "title_net_revenue": _decimal(row.get("revenue"), f"rows[{index}].revenue"),
            },
            confirmed_control_type_ids=["title_scope_confirmed"],
            operand_provenance=_provenance(
                ["title_contribution", "title_net_revenue"], source_type_id=source_type_id,
                source_id=source_id, source_result_fingerprint=source_fingerprint,
                source_field_type_id="game_project_profitability_row",
                derivation_type_id="direct_project_scope_mapping",
            ),
        ))
    return results


def _assemble_cfo_metric_source(
    runtime: "BoxRuntime",
    source_type_id: str,
    source_id: str,
    source_result: dict[str, Any],
    *,
    trusted_execution_token: object,
) -> dict[str, Any] | None:
    """Assemble source-bound metric candidates only inside a trusted execution boundary."""
    if trusted_execution_token is not _TRUSTED_EXECUTION_TOKEN:
        raise CfoMetricAssemblyError(
            "caller-supplied source results are not accepted for trusted metric assembly"
        )
    if not isinstance(source_result, dict):
        raise CfoMetricAssemblyError("metric source result must be an object")
    key = (source_type_id, source_id)
    if key not in _SUPPORTED_SOURCES:
        blocked = _KNOWN_BLOCKED_SOURCES.get(key)
        if blocked is None:
            return None
        return _blocked_collection(source_type_id, source_id, source_result, blocked[0], blocked[1])
    collection = _source_collection(source_type_id, source_id, source_result)
    source_fingerprint = collection["source_result_fingerprint"]
    if key == ("pipeline", "finance.bank_statement_close"):
        assemblies = _bank_assemblies(runtime, source_type_id, source_id, source_result, source_fingerprint)
    elif key == ("pipeline", "finance.month_close_control"):
        assemblies = _month_close_assemblies(runtime, source_type_id, source_id, source_result, source_fingerprint)
    elif key == ("pipeline", "commerce.channel_close"):
        assemblies = _commerce_assemblies(runtime, source_type_id, source_id, source_result, source_fingerprint)
    elif key == ("pipeline", "dtc.shopify_stripe_month_close"):
        assemblies = _shopify_monthly_assemblies(
            runtime, source_type_id, source_id, source_result, source_fingerprint,
        )
    elif key == ("pipeline", "marketplace.channel_close"):
        assemblies = _marketplace_assemblies(runtime, source_type_id, source_id, source_result, source_fingerprint)
    elif key == ("pipeline", "amazon_seller.marketplace_close"):
        assemblies = _amazon_marketplace_assemblies(
            runtime, source_type_id, source_id, source_result, source_fingerprint,
        )
    else:
        assemblies = _game_profitability_assemblies(runtime, source_type_id, source_id, source_result, source_fingerprint)
    collection["assemblies"] = assemblies
    collection["assembly_count"] = len(assemblies)
    if not assemblies:
        collection["coverage_status"] = "not_available_source_not_ready_or_empty"
        if key == ("pipeline", "amazon_seller.marketplace_close") and source_result.get("ready"):
            output = (
                (((source_result.get("services") or {})
                  .get("marketplace_evidence_reconciliation") or {}).get("output") or {})
            )
            collection["coverage_blocker_type_ids"] = [
                "canonical_month_scope_not_satisfied"
                if output.get("canonical_month_scope") is not True
                else "trusted_source_did_not_produce_scoped_rows"
            ]
        else:
            collection["coverage_blocker_type_ids"] = [
                "trusted_source_did_not_produce_scoped_rows"
            ]
    return collection


def attach_cfo_metric_assembly(
    runtime: "BoxRuntime", source_type_id: str, source_id: str, source_result: dict[str, Any],
) -> dict[str, Any]:
    """Attach a non-recursive assembly collection to an already produced trusted result."""
    collection = _assemble_cfo_metric_source(
        runtime, source_type_id, source_id, source_result,
        trusted_execution_token=_TRUSTED_EXECUTION_TOKEN,
    )
    if collection is not None:
        source_result["cfo_metric_operand_assembly"] = collection
    return source_result


def assemble_trusted_service_metric_source(
    runtime: "BoxRuntime", service_id: str, service_output: dict[str, Any],
) -> dict[str, Any] | None:
    """Internal Service-registry postprocessor; not an API for historical result ingestion."""
    return _assemble_cfo_metric_source(
        runtime, "service", service_id, service_output,
        trusted_execution_token=_TRUSTED_EXECUTION_TOKEN,
    )
