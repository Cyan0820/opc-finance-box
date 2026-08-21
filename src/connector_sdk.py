from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable
import re

from .box_runtime import BoxRuntime, BoxRuntimeError


ConnectorHandler = Callable[[dict[str, Any], "ConnectorContext"], dict[str, Any]]


class ConnectorError(RuntimeError):
    """Raised when a connector violates its Pack, entity, evidence or batch contract."""


@dataclass(frozen=True)
class ConnectorSyncWindow:
    start_field: str
    end_field: str
    value_format: str
    max_incremental_days: int = 31
    max_backfill_days: int = 366
    incremental_overlap_seconds: int = 0


@dataclass(frozen=True)
class ConnectorDefinition:
    connector_id: str
    pack_id: str
    capability: str
    display_name: str
    dataset_types: tuple[str, ...]
    handler: ConnectorHandler
    business_keys: dict[str, tuple[str, ...]]
    credential_env: tuple[str, ...] = ()
    network_access: bool = False
    sync_window: ConnectorSyncWindow | None = None


@dataclass(frozen=True)
class ConnectorContext:
    runtime: BoxRuntime
    allowed_entity_ids: frozenset[str]


class ConnectorRegistry:
    def __init__(self):
        self._connectors: dict[str, ConnectorDefinition] = {}

    def register(self, connector: ConnectorDefinition) -> None:
        if connector.connector_id in self._connectors:
            raise ConnectorError(f"Duplicate connector id: {connector.connector_id}")
        if not connector.dataset_types or len(set(connector.dataset_types)) != len(connector.dataset_types):
            raise ConnectorError(f"Connector {connector.connector_id} requires unique dataset_types")
        if set(connector.business_keys) != set(connector.dataset_types):
            raise ConnectorError(
                f"Connector {connector.connector_id} business_keys must cover every dataset type"
            )
        if any(not fields for fields in connector.business_keys.values()):
            raise ConnectorError(f"Connector {connector.connector_id} requires business key fields")
        if len(set(connector.credential_env)) != len(connector.credential_env) or any(
            not re.fullmatch(r"OPC_[A-Z0-9_]+", name) for name in connector.credential_env
        ):
            raise ConnectorError(
                f"Connector {connector.connector_id} credential_env must contain unique OPC_ environment names"
            )
        if connector.network_access and not connector.credential_env:
            raise ConnectorError(f"Network connector {connector.connector_id} must declare credential_env")
        if connector.sync_window is not None:
            window = connector.sync_window
            if not connector.network_access:
                raise ConnectorError(
                    f"Connector {connector.connector_id} sync_window requires network_access"
                )
            if (
                not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", window.start_field)
                or not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", window.end_field)
                or window.start_field == window.end_field
                or window.value_format not in {"iso8601", "unix_seconds"}
                or not 1 <= window.max_incremental_days <= 90
                or not window.max_incremental_days <= window.max_backfill_days <= 3660
                or not isinstance(window.incremental_overlap_seconds, int)
                or isinstance(window.incremental_overlap_seconds, bool)
                or not 0 <= window.incremental_overlap_seconds <= window.max_incremental_days * 86400
            ):
                raise ConnectorError(
                    f"Connector {connector.connector_id} has an invalid sync_window contract"
                )
        self._connectors[connector.connector_id] = connector

    def catalog(self, runtime: BoxRuntime) -> list[dict[str, Any]]:
        snapshot = runtime.snapshot()
        selected_packs = {pack["id"] for pack in snapshot["packs"]}
        capabilities = set(snapshot["capabilities"])
        return [{
            "connector_id": connector.connector_id,
            "pack_id": connector.pack_id,
            "capability": connector.capability,
            "display_name": connector.display_name,
            "dataset_types": list(connector.dataset_types),
            "business_keys": {
                dataset: list(fields) for dataset, fields in connector.business_keys.items()
            },
            "credential_env": list(connector.credential_env),
            "network_access": connector.network_access,
            "entity_ids": sorted(runtime.connector_entity_ids(connector.pack_id)),
            "sync_window": (
                {
                    "start_field": connector.sync_window.start_field,
                    "end_field": connector.sync_window.end_field,
                    "value_format": connector.sync_window.value_format,
                    "max_incremental_days": connector.sync_window.max_incremental_days,
                    "max_backfill_days": connector.sync_window.max_backfill_days,
                    "incremental_overlap_seconds": connector.sync_window.incremental_overlap_seconds,
                }
                if connector.sync_window is not None else None
            ),
        } for connector in sorted(self._connectors.values(), key=lambda item: item.connector_id)
          if connector.pack_id in selected_packs and connector.capability in capabilities]

    def definitions(self) -> tuple[ConnectorDefinition, ...]:
        """Expose immutable definitions for Pack conformance tooling."""
        return tuple(sorted(self._connectors.values(), key=lambda item: item.connector_id))

    def definition(self, connector_id: str) -> ConnectorDefinition:
        try:
            return self._connectors[connector_id]
        except KeyError as exc:
            raise ConnectorError(f"Unknown connector: {connector_id}") from exc

    def dispatch(
        self,
        runtime: BoxRuntime,
        connector_id: str,
        request: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            connector = self._connectors[connector_id]
        except KeyError as exc:
            raise ConnectorError(f"Unknown connector: {connector_id}") from exc
        if not isinstance(request, dict):
            raise ConnectorError("connector request must be a JSON object")
        runtime.reload()
        snapshot = runtime.snapshot()
        selected_packs = {pack["id"] for pack in snapshot["packs"]}
        if connector.pack_id not in selected_packs:
            raise ConnectorError(f"Connector pack is not selected by this Box: {connector.pack_id}")
        try:
            runtime.require_capability(connector.capability)
        except BoxRuntimeError as exc:
            raise ConnectorError(str(exc)) from exc
        allowed_entity_ids = runtime.connector_entity_ids(connector.pack_id)
        requested_entity_id = request.get("default_entity_id")
        if requested_entity_id is not None:
            entity_id = str(requested_entity_id).strip()
            if not entity_id:
                raise ConnectorError("default_entity_id must be a non-empty legal entity id")
            try:
                runtime.require_connector_entity(connector.pack_id, entity_id)
            except BoxRuntimeError as exc:
                raise ConnectorError(
                    f"connector request requires a valid default_entity_id: {exc}"
                ) from exc
        context = ConnectorContext(runtime, allowed_entity_ids)
        raw_batch = connector.handler(dict(request), context)
        batch = self._validate_batch(connector, raw_batch, context.allowed_entity_ids)
        return {
            "connector": {
                "connector_id": connector.connector_id,
                "pack_id": connector.pack_id,
                "capability": connector.capability,
                "dataset_types": list(connector.dataset_types),
                "credential_env": list(connector.credential_env),
                "network_access": connector.network_access,
                "entity_ids": sorted(allowed_entity_ids),
                "executed_at": datetime.now(timezone.utc).isoformat(),
            },
            "batch": batch,
        }

    @staticmethod
    def _validate_batch(
        connector: ConnectorDefinition,
        raw_batch: Any,
        allowed_entity_ids: frozenset[str],
    ) -> dict[str, Any]:
        if not isinstance(raw_batch, dict):
            raise ConnectorError(f"Connector {connector.connector_id} must return a dictionary")
        batch_id = str(raw_batch.get("batch_id") or "").strip()
        if not batch_id:
            raise ConnectorError("connector batch requires batch_id")
        datasets = raw_batch.get("datasets")
        if not isinstance(datasets, dict):
            raise ConnectorError("connector batch requires datasets object")
        unknown_types = set(datasets) - set(connector.dataset_types)
        if unknown_types:
            raise ConnectorError(f"connector returned undeclared datasets: {sorted(unknown_types)}")

        normalized: dict[str, list[dict[str, Any]]] = {}
        rejected = list(raw_batch.get("rejected_rows") or [])
        duplicate_keys: list[str] = []
        seen: set[str] = set()
        record_count = 0
        for dataset_type in connector.dataset_types:
            rows = datasets.get(dataset_type, [])
            if not isinstance(rows, list):
                raise ConnectorError(f"dataset {dataset_type} must be a list")
            accepted: list[dict[str, Any]] = []
            for index, row in enumerate(rows):
                if not isinstance(row, dict):
                    rejected.append({
                        "dataset_type": dataset_type,
                        "row": index + 1,
                        "reason": "record must be an object",
                    })
                    continue
                entity_id = str(row.get("entity_id") or "")
                evidence = row.get("evidence")
                if not entity_id:
                    rejected.append({
                        "dataset_type": dataset_type,
                        "row": index + 1,
                        "reason": "missing entity_id",
                    })
                    continue
                if entity_id not in allowed_entity_ids:
                    rejected.append({
                        "dataset_type": dataset_type,
                        "row": index + 1,
                        "reason": f"unknown legal entity: {entity_id}",
                    })
                    continue
                if not isinstance(evidence, dict) or not str(evidence.get("source_file") or ""):
                    rejected.append({
                        "dataset_type": dataset_type,
                        "row": index + 1,
                        "reason": "missing source evidence",
                    })
                    continue
                evidence_batch = str(evidence.get("batch_id") or "")
                if evidence_batch != batch_id:
                    rejected.append({
                        "dataset_type": dataset_type,
                        "row": index + 1,
                        "reason": "evidence batch_id does not match connector batch",
                    })
                    continue
                key_fields = connector.business_keys[dataset_type]
                missing_keys = [field for field in key_fields if row.get(field) in (None, "")]
                if missing_keys:
                    rejected.append({
                        "dataset_type": dataset_type,
                        "row": index + 1,
                        "reason": f"missing business key fields: {', '.join(missing_keys)}",
                    })
                    continue
                key = "|".join([dataset_type, entity_id] + [str(row[field]) for field in key_fields])
                if key in seen:
                    duplicate_keys.append(key)
                    continue
                seen.add(key)
                accepted.append(dict(row))
            normalized[dataset_type] = accepted
            record_count += len(accepted)
        duplicate_keys.extend(str(item) for item in raw_batch.get("duplicate_business_keys") or [])
        duplicate_keys = sorted(set(duplicate_keys))
        return {
            "batch_id": batch_id,
            "source": dict(raw_batch.get("source") or {}),
            "datasets": normalized,
            "quality": {
                "ready": record_count > 0 and not rejected and not duplicate_keys,
                "record_count": record_count,
                "dataset_counts": {key: len(value) for key, value in normalized.items()},
                "rejected_count": len(rejected),
                "rejected_rows": rejected,
                "duplicate_business_keys": duplicate_keys,
            },
        }
