from __future__ import annotations

import hashlib
import json
import threading
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .box_config import PackCatalog, load_box_config, load_pack_catalog, resolve_box
from .legal_entities import EntityRegistry


class BoxRuntimeError(RuntimeError):
    """Raised when code requests a capability outside the configured Box."""


class BoxRuntime:
    """Thread-safe, read-only runtime view of a validated Box configuration."""

    def __init__(self, config_path: str | Path, packs_root: str | Path):
        self.config_path = Path(config_path)
        self.packs_root = Path(packs_root)
        self._lock = threading.RLock()
        self._fingerprint = ""
        self._resolved: dict[str, Any] = {}
        self._catalog: PackCatalog | None = None
        self._entities: EntityRegistry | None = None
        self._loaded_at = ""
        self.reload(force=True)

    def _source_fingerprint(self) -> str:
        digest = hashlib.sha256()
        paths = [self.config_path] + sorted(self.packs_root.rglob("*.json"))
        for path in paths:
            digest.update(str(path.relative_to(path.parent if path == self.config_path else self.packs_root)).encode())
            digest.update(path.read_bytes())
        return digest.hexdigest()

    def reload(self, *, force: bool = False) -> bool:
        fingerprint = self._source_fingerprint()
        with self._lock:
            if not force and fingerprint == self._fingerprint:
                return False
            catalog = load_pack_catalog(self.packs_root)
            resolved = resolve_box(load_box_config(self.config_path), catalog)
            entities = EntityRegistry.from_resolved_box(resolved)
            self._catalog = catalog
            self._resolved = resolved
            self._entities = entities
            self._fingerprint = fingerprint
            self._loaded_at = datetime.now(timezone.utc).isoformat()
            return True

    @property
    def entities(self) -> EntityRegistry:
        if self._entities is None:
            raise BoxRuntimeError("Box runtime is not loaded")
        return self._entities

    def has_capability(self, capability: str) -> bool:
        with self._lock:
            return capability in self._resolved.get("capabilities", [])

    def require_capability(self, capability: str) -> None:
        if not self.has_capability(capability):
            raise BoxRuntimeError(f"Capability is not enabled by this Box: {capability}")

    def require_entity(self, entity_id: str) -> None:
        try:
            self.entities.get(entity_id)
        except ValueError as exc:
            raise BoxRuntimeError(str(exc)) from exc

    def connector_entity_ids(self, connector_pack: str) -> frozenset[str]:
        """Return the legal entities a Connector Pack may access in this Box."""
        with self._lock:
            for binding in self._resolved.get("connector_bindings", []):
                if binding.get("connector_pack") == connector_pack:
                    return frozenset(str(item) for item in binding.get("entity_ids", []))
            # Channel-owned connectors are selected through their Channel Pack and
            # therefore do not have a top-level Connector Pack binding record.
            return frozenset(self.entities.ids())

    def require_connector_entity(self, connector_pack: str, entity_id: str) -> None:
        self.require_entity(entity_id)
        if entity_id not in self.connector_entity_ids(connector_pack):
            raise BoxRuntimeError(
                f"Connector Pack {connector_pack} is not bound to legal entity: {entity_id}"
            )

    def tax_rules(self, entity_id: str) -> dict[str, Any]:
        """Return the selected entity's versioned jurisdiction rules and pack controls."""
        with self._lock:
            entity = self.entities.get(entity_id)
            if self._catalog is None:
                raise BoxRuntimeError("Box runtime is not loaded")
            pack = self._catalog.get(entity.tax_pack)
            if pack.kind != "jurisdiction" or pack.rules is None:
                raise BoxRuntimeError(f"Entity has no loaded jurisdiction rules: {entity_id}")
            return {
                "pack_id": pack.pack_id,
                "pack_version": pack.version,
                "pack_status": pack.status,
                "jurisdiction": deepcopy(pack.jurisdiction),
                "manual_review_gates": list(pack.manual_review_gates),
                "rules": deepcopy(pack.rules),
            }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            pack_statuses: dict[str, int] = {}
            for pack in self._resolved.get("packs", []):
                status = str(pack.get("status") or "unknown")
                pack_statuses[status] = pack_statuses.get(status, 0) + 1
            entities = deepcopy(self._resolved.get("entities", []))
            tax_readiness = {entity["id"]: entity.get("tax_readiness") for entity in entities}
            return {
                "name": self._resolved.get("name"),
                "box_version": self._resolved.get("box_version"),
                "data_mode": self._resolved.get("data_mode"),
                "loaded_at": self._loaded_at,
                "fingerprint": self._fingerprint,
                "reporting_currency": self._resolved.get("reporting_currency"),
                "entities": entities,
                "connector_binding_mode": self._resolved.get("connector_binding_mode"),
                "connector_bindings": deepcopy(self._resolved.get("connector_bindings", [])),
                "packs": deepcopy(self._resolved.get("packs", [])),
                "pack_statuses": pack_statuses,
                "capabilities": list(self._resolved.get("capabilities", [])),
                "manual_review_gates": list(self._resolved.get("manual_review_gates", [])),
                "tax_readiness": tax_readiness,
                "warnings": list(self._resolved.get("warnings", [])),
                "guardrails": list(self._resolved.get("guardrails", [])),
                "production_ready": (
                    bool(self._resolved.get("packs"))
                    and all(pack.get("status") == "stable" for pack in self._resolved["packs"])
                    and all(value == "filing_assist" for value in tax_readiness.values())
                ),
            }

    def to_json(self) -> str:
        return json.dumps(self.snapshot(), ensure_ascii=False, indent=2)
