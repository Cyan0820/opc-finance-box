from __future__ import annotations

from typing import Any

from .box_runtime import BoxRuntime
from .connector_sdk import ConnectorRegistry


def run_connector_contract_test(
    registry: ConnectorRegistry,
    runtime: BoxRuntime,
    connector_id: str,
    request: dict[str, Any],
    *,
    expected_minimum_counts: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Run a fixture twice to verify evidence, scope and idempotent normalized output."""
    first = registry.dispatch(runtime, connector_id, request)
    second = registry.dispatch(runtime, connector_id, request)
    first_batch = first["batch"]
    second_batch = second["batch"]
    expectations = expected_minimum_counts or {}
    count_failures = {
        dataset: {
            "expected_minimum": minimum,
            "actual": first_batch["quality"]["dataset_counts"].get(dataset, 0),
        }
        for dataset, minimum in expectations.items()
        if first_batch["quality"]["dataset_counts"].get(dataset, 0) < minimum
    }
    evidence_failures = []
    entity_failures = []
    allowed_entities = runtime.entities.ids()
    for dataset, rows in first_batch["datasets"].items():
        for index, row in enumerate(rows, 1):
            evidence = row.get("evidence") or {}
            if evidence.get("batch_id") != first_batch["batch_id"] or not evidence.get("source_file"):
                evidence_failures.append({"dataset": dataset, "row": index})
            if row.get("entity_id") not in allowed_entities:
                entity_failures.append({"dataset": dataset, "row": index, "entity_id": row.get("entity_id")})
    idempotent = (
        first_batch["batch_id"] == second_batch["batch_id"]
        and first_batch["datasets"] == second_batch["datasets"]
        and first_batch["quality"] == second_batch["quality"]
    )
    checks = {
        "batch_ready": first_batch["quality"]["ready"],
        "minimum_counts": not count_failures,
        "evidence_complete": not evidence_failures,
        "entity_scope_valid": not entity_failures,
        "idempotent_fixture": idempotent,
    }
    return {
        "passed": all(checks.values()),
        "connector_id": connector_id,
        "batch_id": first_batch["batch_id"],
        "checks": checks,
        "failures": {
            "minimum_counts": count_failures,
            "evidence": evidence_failures,
            "entity_scope": entity_failures,
        },
        "quality": first_batch["quality"],
    }
