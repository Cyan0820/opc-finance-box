from __future__ import annotations

from collections import Counter
from typing import Any

from .box_compiler import RUNTIME_CAPABILITIES, WORKFLOW_BLUEPRINTS
from .box_config import PackCatalog
from .connector_sdk import ConnectorRegistry
from .default_connectors import build_default_connector_registry
from .connector_extensions import load_connector_providers
from .default_services import build_default_service_registry
from .pack_services import PackServiceRegistry


def _dependency_closure(pack_id: str, catalog: PackCatalog) -> set[str]:
    output = {pack_id}
    pending = [pack_id]
    while pending:
        current = catalog.get(pending.pop())
        for dependency in current.requires:
            if dependency not in output:
                output.add(dependency)
                pending.append(dependency)
    return output


def audit_pack_catalog(
    catalog: PackCatalog,
    service_registry: PackServiceRegistry | None = None,
    connector_registry: ConnectorRegistry | None = None,
) -> dict[str, Any]:
    """Audit Pack declarations against registered code providers and review-gate contracts."""
    services = (service_registry or build_default_service_registry()).definitions()
    connectors = (
        connector_registry
        or load_connector_providers(build_default_connector_registry(), catalog)
    ).definitions()
    workflows_by_capability: dict[str, list[str]] = {}
    for workflow in WORKFLOW_BLUEPRINTS:
        workflows_by_capability.setdefault(workflow["capability"], []).append(workflow["workflow_id"])
    service_by_capability: dict[str, list[str]] = {}
    connector_by_capability: dict[str, list[str]] = {}
    for service in services:
        service_by_capability.setdefault(service.capability, []).append(service.service_id)
    for connector in connectors:
        connector_by_capability.setdefault(connector.capability, []).append(connector.connector_id)

    packs = []
    all_coverage = []
    contract_failures = []
    for pack in catalog.all():
        dependency_ids = _dependency_closure(pack.pack_id, catalog)
        available_gates = {
            gate for dependency_id in dependency_ids
            for gate in catalog.get(dependency_id).manual_review_gates
        }
        pack_services = [service for service in services if service.pack_id == pack.pack_id]
        pack_connectors = [connector for connector in connectors if connector.pack_id == pack.pack_id]
        gate_failures = []
        for service in pack_services:
            if service.review_gate and service.review_gate not in available_gates:
                gate_failures.append({
                    "provider": service.service_id,
                    "review_gate": service.review_gate,
                    "reason": "gate is not declared by Pack or its dependencies",
                })
        if gate_failures:
            contract_failures.append({"pack_id": pack.pack_id, "gate_failures": gate_failures})

        coverage = []
        for capability in pack.capabilities:
            providers = {
                "services": sorted(service_by_capability.get(capability, [])),
                "connectors": sorted(connector_by_capability.get(capability, [])),
                "workflows": sorted(workflows_by_capability.get(capability, [])),
                "runtime_guardrail": capability in RUNTIME_CAPABILITIES,
            }
            code_provider = bool(providers["services"] or providers["connectors"] or providers["runtime_guardrail"])
            status = "executable" if code_provider else "blueprint_only" if providers["workflows"] else "declared_only"
            item = {
                "pack_id": pack.pack_id,
                "capability": capability,
                "implementation_status": status,
                "providers": providers,
            }
            coverage.append(item)
            all_coverage.append(item)
        incomplete = [item for item in coverage if item["implementation_status"] != "executable"]
        packs.append({
            "pack_id": pack.pack_id,
            "kind": pack.kind,
            "version": pack.version,
            "status": pack.status,
            "dependencies": sorted(dependency_ids - {pack.pack_id}),
            "contract_valid": not gate_failures,
            "gate_failures": gate_failures,
            "capability_coverage": coverage,
            "complete_implementation": not incomplete,
            "incomplete_capabilities": [item["capability"] for item in incomplete],
            "stable_release_ready": (
                not gate_failures and not incomplete and pack.status == "stable"
            ),
        })
    status_counts = Counter(item["implementation_status"] for item in all_coverage)
    return {
        "contract_valid": not contract_failures,
        "complete_implementation": all(pack["complete_implementation"] for pack in packs),
        # Do not let an all-preview catalog pass through vacuous truth.
        "stable_release_ready": bool(packs) and all(
            pack["stable_release_ready"] for pack in packs
        ),
        "pack_count": len(packs),
        "capability_count": len(all_coverage),
        "coverage_counts": {
            key: status_counts.get(key, 0)
            for key in ("executable", "blueprint_only", "declared_only")
        },
        "contract_failures": contract_failures,
        "packs": packs,
    }
