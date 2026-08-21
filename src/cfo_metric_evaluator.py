from __future__ import annotations

import hashlib
import json
import re
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any

from .box_runtime import BoxRuntime
from .cfo_metric_catalog import build_cfo_metric_catalog


class CfoMetricEvaluationError(ValueError):
    """Raised when a CFO metric evaluation request violates its safe contract."""


_PERIOD_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_CURRENCY_PATTERN = re.compile(r"^[A-Z]{3}$")
_DECIMAL_PATTERN = re.compile(r"^-?(0|[1-9]\d*)(\.\d+)?$")
_DIMENSION_TYPE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_MAX_ABSOLUTE_OPERAND = Decimal("1e18")
_SUPPORTED_OPERATORS = {
    "subtract",
    "safe_divide",
    "safe_divide_scaled",
    "count",
    "rollforward",
    "max_share",
    "absolute_difference",
}
_ALLOWED_REQUEST_KEYS = {
    "runtime_fingerprint",
    "period",
    "currency",
    "metric_type_ids",
    "operand_values",
    "vector_operand_values",
    "confirmed_control_type_ids",
    "dimension_scope",
}


def _dimension_scope(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {
        "dimension_type_id", "dimension_value_ids",
    }:
        raise CfoMetricEvaluationError(
            "dimension_scope must contain only dimension_type_id and dimension_value_ids"
        )
    dimension_type_id = value.get("dimension_type_id")
    dimension_value_ids = value.get("dimension_value_ids")
    if (
        not isinstance(dimension_type_id, str)
        or not _DIMENSION_TYPE_PATTERN.fullmatch(dimension_type_id)
    ):
        raise CfoMetricEvaluationError("dimension_scope.dimension_type_id is invalid")
    if (
        not isinstance(dimension_value_ids, list)
        or not dimension_value_ids
        or len(dimension_value_ids) > 100
        or any(
            not isinstance(item, str) or not item.strip() or len(item) > 128
            or any(ord(char) < 32 for char in item)
            for item in dimension_value_ids
        )
        or len(dimension_value_ids) != len(set(dimension_value_ids))
    ):
        raise CfoMetricEvaluationError(
            "dimension_scope.dimension_value_ids must be a unique non-empty list of safe IDs"
        )
    return {
        "dimension_type_id": dimension_type_id,
        "dimension_value_ids": list(dimension_value_ids),
    }


def _parse_decimal(value: Any, label: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise CfoMetricEvaluationError(f"{label} must be a finite decimal number")
    if isinstance(value, str) and not _DECIMAL_PATTERN.fullmatch(value):
        raise CfoMetricEvaluationError(
            f"{label} must use a canonical decimal string without exponent notation"
        )
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise CfoMetricEvaluationError(f"{label} must be a finite decimal number") from exc
    if not parsed.is_finite():
        raise CfoMetricEvaluationError(f"{label} must be a finite decimal number")
    if abs(parsed) > _MAX_ABSOLUTE_OPERAND:
        raise CfoMetricEvaluationError(f"{label} exceeds the supported absolute value")
    return parsed


def _decimal_text(value: Decimal) -> str:
    if value == 0:
        return "0"
    text = format(value.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _fingerprint(payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _evaluate_formula(
    metric: dict[str, Any],
    scalar_values: dict[str, Decimal],
    vector_values: dict[str, tuple[Decimal, ...]],
) -> tuple[str, Decimal | None, str | None, dict[str, Any]]:
    formula = metric["formula"]
    operator = formula["operator_type_id"]
    operand_ids = formula["operand_type_ids"]
    scalar_snapshot = {
        operand_id: _decimal_text(scalar_values[operand_id])
        for operand_id in operand_ids if operand_id in scalar_values
    }
    vector_snapshot = {
        operand_id: {
            "item_count": len(vector_values[operand_id]),
            "total": _decimal_text(sum(vector_values[operand_id], Decimal("0"))),
            "maximum": _decimal_text(max(vector_values[operand_id])),
        }
        for operand_id in operand_ids if operand_id in vector_values
    }
    snapshot = {
        "scalar_operand_values": scalar_snapshot,
        "vector_operand_summaries": vector_snapshot,
        "raw_source_records_returned": False,
    }

    if operator == "max_share":
        vector_id, denominator_id = operand_ids
        missing = [
            operand_id for operand_id in operand_ids
            if operand_id not in vector_values and operand_id not in scalar_values
        ]
        if missing:
            return "not_available_missing_operands", None, None, snapshot
        vector = vector_values[vector_id]
        denominator = scalar_values[denominator_id]
        if any(item < 0 for item in vector) or denominator < 0:
            return "blocked_inconsistent_operands", None, "negative_share_operand", snapshot
        if denominator == 0:
            return "not_available_zero_denominator", None, None, snapshot
        vector_total = sum(vector, Decimal("0"))
        tolerance = max(Decimal("0.01"), abs(denominator) * Decimal("0.000001"))
        if abs(vector_total - denominator) > tolerance:
            return "blocked_inconsistent_operands", None, "vector_total_mismatch", snapshot
        return "available", max(vector) / denominator, None, snapshot

    missing = [operand_id for operand_id in operand_ids if operand_id not in scalar_values]
    if missing:
        return "not_available_missing_operands", None, None, snapshot
    values = [scalar_values[operand_id] for operand_id in operand_ids]

    if operator == "subtract":
        value = values[0] - values[1]
    elif operator in {"safe_divide", "safe_divide_scaled"}:
        denominator = values[1]
        if denominator == 0:
            return "not_available_zero_denominator", None, None, snapshot
        if formula.get("nonpositive_denominator_policy") == "not_available" and denominator < 0:
            return "not_available_nonpositive_denominator", None, None, snapshot
        value = values[0] / denominator
        if operator == "safe_divide_scaled":
            value *= Decimal(str(formula["scale"]))
    elif operator == "count":
        value = values[0]
        if value < 0 or value != value.to_integral_value():
            return "blocked_inconsistent_operands", None, "count_must_be_nonnegative_integer", snapshot
    elif operator == "rollforward":
        value = values[0] + values[1] - values[2]
    elif operator == "absolute_difference":
        value = abs(values[0] - values[1])
    else:  # pragma: no cover - protected by catalog validation below
        raise CfoMetricEvaluationError(f"unsupported metric operator: {operator}")
    return "available", value, None, snapshot


def evaluate_cfo_metrics(
    runtime: BoxRuntime,
    entity_id: str,
    request: dict[str, Any],
) -> dict[str, Any]:
    """Evaluate Pack-selected CFO metrics without inference, FX, or arbitrary code."""
    if not isinstance(request, dict):
        raise CfoMetricEvaluationError("CFO metric request must be a JSON object")
    unknown_keys = sorted(set(request) - _ALLOWED_REQUEST_KEYS)
    if unknown_keys:
        raise CfoMetricEvaluationError(
            "CFO metric request contains unsupported keys: " + ", ".join(unknown_keys)
        )

    snapshot = runtime.snapshot()
    expected_fingerprint = snapshot["fingerprint"]
    if request.get("runtime_fingerprint") != expected_fingerprint:
        raise CfoMetricEvaluationError(
            "runtime_fingerprint must match the currently loaded Box runtime"
        )
    period = request.get("period")
    if not isinstance(period, str) or not _PERIOD_PATTERN.fullmatch(period):
        raise CfoMetricEvaluationError("period must use YYYY-MM")
    currency = request.get("currency")
    if not isinstance(currency, str) or not _CURRENCY_PATTERN.fullmatch(currency):
        raise CfoMetricEvaluationError("currency must be an uppercase three-letter code")
    entity = runtime.entities.get(entity_id)
    if currency != entity.functional_currency:
        raise CfoMetricEvaluationError(
            "currency must equal the legal entity functional currency; "
            "implicit FX conversion is not permitted"
        )
    dimension_scope = _dimension_scope(request.get("dimension_scope"))

    selected_pack_ids = {item["id"] for item in snapshot["packs"]}
    catalog = build_cfo_metric_catalog(
        selected_pack_ids, runtime_fingerprint=expected_fingerprint,
    )
    definitions = catalog["metric_definitions"]
    definition_by_id = {item["metric_type_id"]: item for item in definitions}
    selected_metric_ids = request.get("metric_type_ids")
    if selected_metric_ids is None:
        selected_metric_ids = [item["metric_type_id"] for item in definitions]
    if (
        not isinstance(selected_metric_ids, list)
        or not selected_metric_ids
        or any(not isinstance(item, str) or not item for item in selected_metric_ids)
        or len(selected_metric_ids) != len(set(selected_metric_ids))
    ):
        raise CfoMetricEvaluationError(
            "metric_type_ids must be a unique non-empty list when provided"
        )
    unknown_metrics = sorted(set(selected_metric_ids) - set(definition_by_id))
    if unknown_metrics:
        raise CfoMetricEvaluationError(
            "metric_type_ids are not enabled by this Box: " + ", ".join(unknown_metrics)
        )

    active_definitions = [definition_by_id[item] for item in selected_metric_ids]
    unsupported_operators = sorted({
        item["formula"]["operator_type_id"] for item in active_definitions
        if item["formula"]["operator_type_id"] not in _SUPPORTED_OPERATORS
    })
    if unsupported_operators:
        raise CfoMetricEvaluationError(
            "metric catalog contains unsupported operators: " + ", ".join(unsupported_operators)
        )
    vector_operand_ids = {
        item["formula"]["operand_type_ids"][0] for item in active_definitions
        if item["formula"]["operator_type_id"] == "max_share"
    }
    allowed_operand_ids = {
        operand_id for item in active_definitions
        for operand_id in item["formula"]["operand_type_ids"]
    }
    scalar_operand_ids = allowed_operand_ids - vector_operand_ids

    raw_scalars = request.get("operand_values", {})
    raw_vectors = request.get("vector_operand_values", {})
    if not isinstance(raw_scalars, dict) or not isinstance(raw_vectors, dict):
        raise CfoMetricEvaluationError(
            "operand_values and vector_operand_values must be JSON objects"
        )
    unknown_scalar_ids = sorted(set(raw_scalars) - scalar_operand_ids)
    unknown_vector_ids = sorted(set(raw_vectors) - vector_operand_ids)
    if unknown_scalar_ids:
        raise CfoMetricEvaluationError(
            "scalar operands are not used by the selected metrics: " + ", ".join(unknown_scalar_ids)
        )
    if unknown_vector_ids:
        raise CfoMetricEvaluationError(
            "vector operands are not used by the selected metrics: " + ", ".join(unknown_vector_ids)
        )
    scalar_values = {
        operand_id: _parse_decimal(value, f"operand_values.{operand_id}")
        for operand_id, value in raw_scalars.items()
    }
    vector_values: dict[str, tuple[Decimal, ...]] = {}
    for operand_id, values in raw_vectors.items():
        if not isinstance(values, list) or not values:
            raise CfoMetricEvaluationError(
                f"vector_operand_values.{operand_id} must be a non-empty list"
            )
        vector_values[operand_id] = tuple(
            _parse_decimal(value, f"vector_operand_values.{operand_id}[{index}]")
            for index, value in enumerate(values)
        )

    confirmed_controls = request.get("confirmed_control_type_ids", [])
    if (
        not isinstance(confirmed_controls, list)
        or any(not isinstance(item, str) or not item for item in confirmed_controls)
        or len(confirmed_controls) != len(set(confirmed_controls))
    ):
        raise CfoMetricEvaluationError(
            "confirmed_control_type_ids must be a unique list of non-empty strings"
        )
    allowed_controls = {
        control_id for item in active_definitions
        for control_id in item["required_control_type_ids"]
    }
    unknown_controls = sorted(set(confirmed_controls) - allowed_controls)
    if unknown_controls:
        raise CfoMetricEvaluationError(
            "confirmed controls are not used by the selected metrics: "
            + ", ".join(unknown_controls)
        )
    confirmed_control_set = set(confirmed_controls)

    results = []
    status_counts: dict[str, int] = {}
    with localcontext() as decimal_context:
        decimal_context.prec = 38
        for metric in active_definitions:
            missing_controls = sorted(
                set(metric["required_control_type_ids"]) - confirmed_control_set
            )
            status, value, reason, operand_snapshot = _evaluate_formula(
                metric, scalar_values, vector_values,
            )
            if missing_controls:
                status = "blocked_missing_controls"
                value = None
                reason = None
            missing_operands = [
                operand_id for operand_id in metric["formula"]["operand_type_ids"]
                if operand_id not in scalar_values and operand_id not in vector_values
            ]
            result: dict[str, Any] = {
                "metric_type_id": metric["metric_type_id"],
                "definition_version": metric["definition_version"],
                "model_scope_type_id": metric["model_scope_type_id"],
                "value_type_id": metric["value_type_id"],
                "status": status,
                "missing_operand_type_ids": missing_operands,
                "missing_control_type_ids": missing_controls,
                "operator_type_id": metric["formula"]["operator_type_id"],
                "operand_snapshot": operand_snapshot,
                "decision_use_type_id": metric["decision_use_type_id"],
                "authoritative_accounting_or_statutory_truth": False,
            }
            if reason is not None:
                result["block_reason_type_id"] = reason
            if value is not None:
                if metric["value_type_id"] == "count":
                    result["value"] = int(value)
                    result["value_serialization"] = "integer"
                else:
                    result["value"] = _decimal_text(value)
                    result["value_serialization"] = "decimal_string"
                if metric["value_type_id"] == "currency":
                    result["currency"] = currency
            results.append(result)
            status_counts[status] = status_counts.get(status, 0) + 1

    canonical_input = {
        "runtime_fingerprint": expected_fingerprint,
        "entity_id": entity_id,
        "period": period,
        "currency": currency,
        "metric_type_ids": selected_metric_ids,
        "operand_values": {
            key: _decimal_text(value) for key, value in sorted(scalar_values.items())
        },
        "vector_operand_values": {
            key: [_decimal_text(value) for value in values]
            for key, values in sorted(vector_values.items())
        },
        "confirmed_control_type_ids": sorted(confirmed_control_set),
    }
    if dimension_scope is not None:
        canonical_input["dimension_scope"] = dimension_scope
    return {
        "schema_version": 1,
        "artifact_type": "cfo_metric_evaluation",
        "evaluation_contract_version": 1,
        "catalog_version": catalog["catalog_version"],
        "input_fingerprint": _fingerprint(canonical_input),
        "runtime_fingerprint": expected_fingerprint,
        "entity_id": entity_id,
        "period": period,
        "currency": currency,
        "currency_basis_type_id": "legal_entity_functional_currency",
        **({"dimension_scope": dimension_scope} if dimension_scope is not None else {}),
        "metric_results": results,
        "metric_result_count": len(results),
        "status_counts": dict(sorted(status_counts.items())),
        "all_metrics_available": status_counts.get("available", 0) == len(results),
        "formula_allowlist_enforced": True,
        "arbitrary_expression_execution_permitted": False,
        "missing_inputs_inferred_or_filled_with_zero": False,
        "implicit_currency_conversion_performed": False,
        "source_records_returned": False,
        "credentials_returned": False,
        "private_paths_returned": False,
        "authoritative_accounting_or_statutory_truth_inferred": False,
        "posting_payment_or_filing_authorized": False,
        "external_actions_performed": False,
    }
