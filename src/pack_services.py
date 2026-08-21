from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from .box_runtime import BoxRuntime, BoxRuntimeError
from .cfo_metric_assembly import assemble_trusted_service_metric_source


ALLOWED_ACTION_CLASSES = {"read", "draft", "mutating", "external"}
ALLOWED_ENTITY_SCOPES = {"none", "statutory", "management"}
ServiceHandler = Callable[[dict[str, Any], "ServiceContext"], dict[str, Any]]


class PackServiceError(RuntimeError):
    """Raised when a Pack service violates runtime capability or control boundaries."""


@dataclass(frozen=True)
class ServiceDefinition:
    service_id: str
    pack_id: str
    capability: str
    display_name: str
    handler: ServiceHandler
    deterministic: bool
    action_class: str
    entity_scope: str
    review_gate: str | None = None


@dataclass(frozen=True)
class ServiceContext:
    runtime: BoxRuntime
    entity_id: str | None
    entity_ids: tuple[str, ...]
    scope: dict[str, Any] | None
    approval: dict[str, Any] | None


class PackServiceRegistry:
    def __init__(self):
        self._services: dict[str, ServiceDefinition] = {}

    def register(self, service: ServiceDefinition) -> None:
        if service.service_id in self._services:
            raise PackServiceError(f"Duplicate service id: {service.service_id}")
        if service.action_class not in ALLOWED_ACTION_CLASSES:
            raise PackServiceError(f"Invalid action class: {service.action_class}")
        if service.entity_scope not in ALLOWED_ENTITY_SCOPES:
            raise PackServiceError(f"Invalid entity scope: {service.entity_scope}")
        if service.action_class in {"mutating", "external"} and not service.review_gate:
            raise PackServiceError(
                f"Service {service.service_id} must declare a review gate for {service.action_class} actions"
            )
        self._services[service.service_id] = service

    def catalog(self, runtime: BoxRuntime) -> list[dict[str, Any]]:
        snapshot = runtime.snapshot()
        selected_packs = {pack["id"] for pack in snapshot["packs"]}
        enabled_capabilities = set(snapshot["capabilities"])
        return [{
            "service_id": service.service_id,
            "pack_id": service.pack_id,
            "capability": service.capability,
            "display_name": service.display_name,
            "deterministic": service.deterministic,
            "action_class": service.action_class,
            "entity_scope": service.entity_scope,
            "review_gate": service.review_gate,
        } for service in sorted(self._services.values(), key=lambda item: item.service_id)
          if service.pack_id in selected_packs and service.capability in enabled_capabilities]

    def definitions(self) -> tuple[ServiceDefinition, ...]:
        """Expose immutable definitions for Pack conformance tooling."""
        return tuple(sorted(self._services.values(), key=lambda item: item.service_id))

    def dispatch(
        self,
        runtime: BoxRuntime,
        service_id: str,
        payload: dict[str, Any],
        *,
        entity_id: str | None = None,
        entity_ids: list[str] | None = None,
        approval: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            service = self._services[service_id]
        except KeyError as exc:
            raise PackServiceError(f"Unknown service: {service_id}") from exc
        runtime.reload()
        snapshot = runtime.snapshot()
        selected_packs = {pack["id"] for pack in snapshot["packs"]}
        if service.pack_id not in selected_packs:
            raise PackServiceError(f"Service pack is not selected by this Box: {service.pack_id}")
        try:
            runtime.require_capability(service.capability)
        except BoxRuntimeError as exc:
            raise PackServiceError(str(exc)) from exc

        scope = None
        scoped_ids: tuple[str, ...] = ()
        if service.entity_scope == "statutory":
            if not entity_id:
                raise PackServiceError(f"Service {service_id} requires one entity_id")
            try:
                scope = runtime.entities.statutory_scope(entity_id)
            except ValueError as exc:
                raise PackServiceError(str(exc)) from exc
            scoped_ids = (entity_id,)
            entity_tax_pack = scope["entity"].get("tax_pack")
            if service.pack_id.startswith("jurisdiction.") and entity_tax_pack != service.pack_id:
                raise PackServiceError(
                    f"Service {service_id} belongs to {service.pack_id}, but entity {entity_id} uses {entity_tax_pack}"
                )
        elif service.entity_scope == "management":
            try:
                scope = runtime.entities.management_scope(entity_ids)
            except ValueError as exc:
                raise PackServiceError(str(exc)) from exc
            scoped_ids = tuple(scope["entity_ids"])

        if service.review_gate:
            if service.review_gate not in snapshot["manual_review_gates"]:
                raise PackServiceError(
                    f"Service review gate is not configured by this Box: {service.review_gate}"
                )
            if service.action_class in {"mutating", "external"}:
                self._validate_approval(service, approval)

        context = ServiceContext(
            runtime=runtime,
            entity_id=entity_id,
            entity_ids=scoped_ids,
            scope=scope,
            approval=approval,
        )
        output = service.handler(dict(payload), context)
        if not isinstance(output, dict):
            raise PackServiceError(f"Service {service_id} must return a dictionary")
        result = {
            "service": {
                "service_id": service.service_id,
                "pack_id": service.pack_id,
                "capability": service.capability,
                "deterministic": service.deterministic,
                "action_class": service.action_class,
                "entity_scope": service.entity_scope,
                "entity_ids": list(scoped_ids),
                "executed_at": datetime.now(timezone.utc).isoformat(),
                "approval": approval if service.action_class in {"mutating", "external"} else None,
            },
            "output": output,
        }
        metric_assembly = assemble_trusted_service_metric_source(
            runtime, service.service_id, output,
        )
        if metric_assembly is not None:
            result["cfo_metric_operand_assembly"] = metric_assembly
        return result

    @staticmethod
    def _validate_approval(service: ServiceDefinition, approval: dict[str, Any] | None) -> None:
        if not isinstance(approval, dict):
            raise PackServiceError(f"Service {service.service_id} requires approved review gate {service.review_gate}")
        if approval.get("gate") != service.review_gate or approval.get("decision") != "approved":
            raise PackServiceError(f"Approval does not satisfy review gate {service.review_gate}")
        if not str(approval.get("approved_by") or "").strip():
            raise PackServiceError("Approval requires approved_by")
        if not str(approval.get("approved_at") or "").strip():
            raise PackServiceError("Approval requires approved_at")
