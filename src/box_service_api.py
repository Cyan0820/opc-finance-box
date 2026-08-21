from __future__ import annotations

from typing import Any

from .box_api import build_box_context
from .box_runtime import BoxRuntime
from .pack_services import PackServiceError, PackServiceRegistry


class BoxServiceRequestError(ValueError):
    """Raised when an HTTP-style Box service request has an invalid contract."""


def build_box_bootstrap(
    runtime: BoxRuntime,
    registry: PackServiceRegistry,
    *,
    scope: str = "management",
    entity_id: str | None = None,
    entity_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Stable bootstrap payload for any Workbench UI or Agent client."""
    return {
        "context": build_box_context(
            runtime,
            scope=scope,
            entity_id=entity_id,
            entity_ids=entity_ids,
        ),
        "services": registry.catalog(runtime),
    }


def dispatch_box_service_request(
    runtime: BoxRuntime,
    registry: PackServiceRegistry,
    request: dict[str, Any],
) -> dict[str, Any]:
    """Validate an untrusted JSON request before calling the Pack service registry."""
    if not isinstance(request, dict):
        raise BoxServiceRequestError("request body must be a JSON object")
    service_id = request.get("service_id")
    if not isinstance(service_id, str) or not service_id.strip():
        raise BoxServiceRequestError("service_id is required")
    payload = request.get("payload", {})
    if not isinstance(payload, dict):
        raise BoxServiceRequestError("payload must be a JSON object")
    entity_id = request.get("entity_id")
    if entity_id is not None and (not isinstance(entity_id, str) or not entity_id.strip()):
        raise BoxServiceRequestError("entity_id must be a non-empty string")
    entity_ids = request.get("entity_ids")
    if entity_ids is not None and (
        not isinstance(entity_ids, list)
        or not entity_ids
        or any(not isinstance(item, str) or not item.strip() for item in entity_ids)
        or len(set(entity_ids)) != len(entity_ids)
    ):
        raise BoxServiceRequestError("entity_ids must be a unique list of non-empty strings")
    approval = request.get("approval")
    if approval is not None and not isinstance(approval, dict):
        raise BoxServiceRequestError("approval must be a JSON object")
    try:
        return registry.dispatch(
            runtime,
            service_id,
            payload,
            entity_id=entity_id,
            entity_ids=entity_ids,
            approval=approval,
        )
    except PackServiceError:
        raise
