from __future__ import annotations

import json
import copy
from pathlib import Path
from typing import Any

from .box_pipeline import dispatch_box_pipeline_request
from .box_runtime import BoxRuntime
from .box_service_api import dispatch_box_service_request
from .default_connectors import build_box_connector_registry
from .default_services import build_default_service_registry


class BoxEvalError(ValueError):
    """Raised when an eval suite is malformed or escapes its declared project root."""


def _safe_file(root: Path, value: Any, field: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise BoxEvalError(f"{field} must be a non-empty relative path")
    candidate = (root / value).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise BoxEvalError(f"{field} escapes project_root") from exc
    if not candidate.is_file():
        raise BoxEvalError(f"{field} does not exist: {value}")
    return candidate


def _json_object(path: Path, field: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BoxEvalError(f"{field} is not valid JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise BoxEvalError(f"{field} must contain a JSON object")
    return payload


def _values_at(value: Any, path: str) -> list[Any]:
    if not isinstance(path, str) or not path:
        raise BoxEvalError("assertion.path must be a non-empty dotted path")
    values = [value]
    for token in path.split("."):
        token = token.replace("~dot~", ".")
        next_values: list[Any] = []
        for current in values:
            if token == "*":
                if not isinstance(current, list):
                    raise BoxEvalError(f"path wildcard requires a list: {path}")
                next_values.extend(current)
            elif isinstance(current, dict) and token in current:
                next_values.append(current[token])
            elif isinstance(current, list) and token.isdigit() and int(token) < len(current):
                next_values.append(current[int(token)])
            else:
                raise BoxEvalError(f"path not found: {path}")
        values = next_values
    return values


def _evaluate_assertion(payload: dict[str, Any], assertion: dict[str, Any]) -> tuple[bool, list[Any]]:
    if not isinstance(assertion, dict):
        raise BoxEvalError("assertions must contain objects")
    path = assertion.get("path")
    operator = assertion.get("operator", "equals")
    values = _values_at(payload, path)
    expected = assertion.get("value")
    if operator == "equals":
        passed = len(values) == 1 and values[0] == expected
    elif operator == "all_equals":
        passed = bool(values) and all(value == expected for value in values)
    elif operator == "truthy":
        passed = len(values) == 1 and bool(values[0])
    elif operator == "falsey":
        passed = len(values) == 1 and not bool(values[0])
    elif operator == "contains":
        passed = len(values) == 1 and expected in values[0]
    elif operator == "length_gte":
        passed = len(values) == 1 and len(values[0]) >= int(expected)
    else:
        raise BoxEvalError(f"unsupported assertion operator: {operator}")
    return passed, values


def _assert_offline_pipeline_request(request: dict[str, Any]) -> None:
    pipeline_id = request.get("pipeline_id")
    payload = request.get("payload")
    if not isinstance(payload, dict):
        raise BoxEvalError("pipeline eval requires an object payload")
    connector_fields = {
        "finance.expense_evidence_review": ("connector_request",),
        "finance.bank_statement_close": ("connector_request",),
        "finance.trial_balance_review": ("connector_request",),
        "finance.accounting_close_review": (
            "general_ledger_connector_request", "trial_balance_connector_request",
        ),
        "finance.month_close_control": (
            "bank_connector_request", "general_ledger_connector_request",
            "trial_balance_connector_request",
        ),
        "finance.first_close_discovery": (
            "bank_connector_request", "general_ledger_connector_request",
            "trial_balance_connector_request",
        ),
        "finance.multi_entity_month_close_portfolio": (),
        "commerce.channel_close": ("connector_request",),
        "marketplace.channel_close": ("connector_request",),
        "stripe.daily_close": ("balance_request", "payout_request"),
        "dtc.shopify_stripe_daily_close": (
            "shopify_request", "stripe_balance_request", "stripe_payout_request",
        ),
        "dtc.shopify_stripe_month_close": (
            "shopify_monthly_request", "stripe_balance_request",
        ),
        "woocommerce.order_refund_close": ("woocommerce_request",),
        "amazon_seller.transaction_close": ("amazon_seller_request",),
        "amazon_seller.marketplace_close": ("amazon_seller_marketplace_request",),
    }.get(pipeline_id)
    if connector_fields is None:
        raise BoxEvalError(f"eval runner has no offline contract for pipeline: {pipeline_id}")
    for field in connector_fields:
        connector_request = payload.get(field)
        if pipeline_id == "finance.bank_statement_close":
            if payload.get("connector_id") != "file.bank_statement":
                raise BoxEvalError("finance.bank_statement_close eval requires file.bank_statement")
            path_value = connector_request.get("path") if isinstance(connector_request, dict) else None
            if (
                not isinstance(path_value, str) or not path_value
                or Path(path_value).is_absolute() or ".." in Path(path_value).parts
            ):
                raise BoxEvalError("bank statement eval requires a project-relative fixture path")
        elif pipeline_id == "finance.trial_balance_review":
            if payload.get("connector_id") != "file.trial_balance":
                raise BoxEvalError("finance.trial_balance_review eval requires file.trial_balance")
            path_value = connector_request.get("path") if isinstance(connector_request, dict) else None
            if (
                not isinstance(path_value, str) or not path_value
                or Path(path_value).is_absolute() or ".." in Path(path_value).parts
            ):
                raise BoxEvalError("trial balance eval requires a project-relative fixture path")
        elif pipeline_id in {
            "finance.accounting_close_review", "finance.month_close_control",
            "finance.first_close_discovery",
        }:
            connector_contract = {
                "bank_connector_request": ("bank_connector_id", "file.bank_statement"),
                "general_ledger_connector_request": (
                    "general_ledger_connector_id", "file.general_ledger",
                ),
                "trial_balance_connector_request": (
                    "trial_balance_connector_id", "file.trial_balance",
                ),
            }
            connector_field, expected_connector = connector_contract[field]
            if payload.get(connector_field) != expected_connector:
                raise BoxEvalError(
                    f"{pipeline_id} eval requires {expected_connector}"
                )
            path_value = connector_request.get("path") if isinstance(connector_request, dict) else None
            if (
                not isinstance(path_value, str) or not path_value
                or Path(path_value).is_absolute() or ".." in Path(path_value).parts
            ):
                raise BoxEvalError(
                    f"{pipeline_id} eval requires project-relative fixture paths"
                )
        elif pipeline_id in {"commerce.channel_close", "marketplace.channel_close"}:
            expected_connector = (
                "example.commerce_api_payload"
                if pipeline_id == "commerce.channel_close"
                else "example.marketplace_api_payload"
            )
            if payload.get("connector_id") != expected_connector:
                raise BoxEvalError(f"{pipeline_id} eval requires {expected_connector}")
            if not isinstance(connector_request, dict) or not isinstance(connector_request.get("payload"), dict):
                raise BoxEvalError(f"{pipeline_id} eval requires offline connector_request.payload")
        elif not isinstance(connector_request, dict) or connector_request.get("mode") != "fixture":
            raise BoxEvalError(f"eval runner requires {field}.mode=fixture")


def _resolve_offline_pipeline_fixture_paths(
    request: dict[str, Any], project_root: Path,
) -> dict[str, Any]:
    resolved = copy.deepcopy(request)
    pipeline_id = resolved.get("pipeline_id")
    payload = resolved.get("payload") or {}
    path_fields = {
        "finance.bank_statement_close": ("connector_request",),
        "finance.trial_balance_review": ("connector_request",),
        "finance.accounting_close_review": (
            "general_ledger_connector_request", "trial_balance_connector_request",
        ),
        "finance.month_close_control": (
            "bank_connector_request", "general_ledger_connector_request",
            "trial_balance_connector_request",
        ),
        "finance.first_close_discovery": (
            "bank_connector_request", "general_ledger_connector_request",
            "trial_balance_connector_request",
        ),
    }.get(pipeline_id, ())
    for field in path_fields:
        connector_request = payload.get(field)
        if not isinstance(connector_request, dict):  # guarded by offline contract
            continue
        connector_request["path"] = str(_safe_file(
            project_root, connector_request.get("path"),
            f"pipeline {pipeline_id}.{field}.path",
        ))
    return resolved


def run_box_eval_suite(
    suite_path: str | Path,
    packs_root: str | Path,
    *,
    project_root: str | Path | None = None,
) -> dict[str, Any]:
    suite_file = Path(suite_path).resolve()
    root = Path(project_root).resolve() if project_root is not None else suite_file.parent.parent.resolve()
    suite = _json_object(suite_file, "suite")
    if suite.get("schema_version") != 1:
        raise BoxEvalError("unsupported eval suite schema_version")
    cases = suite.get("cases")
    if not isinstance(cases, list) or not cases:
        raise BoxEvalError("suite.cases must be a non-empty list")
    registry = build_default_service_registry()
    results = []
    seen: set[str] = set()
    for index, case in enumerate(cases):
        if not isinstance(case, dict):
            raise BoxEvalError(f"cases[{index}] must be an object")
        case_id = str(case.get("id") or "").strip()
        if not case_id or case_id in seen:
            raise BoxEvalError(f"cases[{index}].id must be unique and non-empty")
        seen.add(case_id)
        assertions = case.get("assertions")
        if not isinstance(assertions, list) or not assertions:
            raise BoxEvalError(f"case {case_id} requires assertions")
        try:
            config_path = _safe_file(root, case.get("box_config"), f"case {case_id}.box_config")
            request_path = _safe_file(root, case.get("request"), f"case {case_id}.request")
            runtime = BoxRuntime(config_path, packs_root)
            request = _json_object(request_path, "request")
            case_type = case.get("type", "service")
            if case_type == "service":
                response = dispatch_box_service_request(runtime, registry, request)
                if response["service"]["action_class"] not in {"read", "draft"}:
                    raise BoxEvalError("eval runner refuses mutating or external services")
                provider_id = response["service"]["service_id"]
                action_class = response["service"]["action_class"]
            elif case_type == "connector":
                connector_id = str(case.get("connector_id") or "").strip()
                if not connector_id:
                    raise BoxEvalError(f"case {case_id} requires connector_id")
                if request.get("mode") != "fixture":
                    raise BoxEvalError("eval runner only permits offline connector fixture mode")
                response = build_box_connector_registry(runtime).dispatch(runtime, connector_id, request)
                provider_id = response["connector"]["connector_id"]
                action_class = "offline_fixture"
            elif case_type == "pipeline":
                _assert_offline_pipeline_request(request)
                request = _resolve_offline_pipeline_fixture_paths(request, root)
                response = dispatch_box_pipeline_request(runtime, request)
                if response.get("external_actions_performed") is not False:
                    raise BoxEvalError("eval runner refuses a pipeline with external actions")
                if response.get("network_access_performed") is not False:
                    raise BoxEvalError("offline pipeline eval unexpectedly performed network access")
                provider_id = response["pipeline"]["pipeline_id"]
                action_class = "offline_pipeline"
            else:
                raise BoxEvalError(f"case {case_id} has unsupported type: {case_type}")
            assertion_results = []
            for assertion in assertions:
                passed, actual = _evaluate_assertion(response, assertion)
                assertion_results.append({
                    "path": assertion.get("path"),
                    "operator": assertion.get("operator", "equals"),
                    "expected": assertion.get("value"),
                    "actual": actual,
                    "passed": passed,
                })
            passed = all(item["passed"] for item in assertion_results)
            results.append({
                "id": case_id, "passed": passed,
                "provider_id": provider_id,
                "action_class": action_class,
                "assertions": assertion_results,
            })
        except Exception as exc:
            results.append({"id": case_id, "passed": False, "error": str(exc), "assertions": []})
    passed_count = sum(item["passed"] for item in results)
    return {
        "schema_version": 1,
        "suite_id": suite.get("suite_id"),
        "passed": passed_count == len(results),
        "counts": {"total": len(results), "passed": passed_count, "failed": len(results) - passed_count},
        "external_actions_performed": False,
        "cases": results,
    }
