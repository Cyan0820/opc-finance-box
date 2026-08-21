from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path

from .box_config import PackCatalog
from .connector_sdk import ConnectorError, ConnectorRegistry


def load_connector_providers(
    registry: ConnectorRegistry,
    catalog: PackCatalog,
    *,
    selected_pack_ids: set[str] | None = None,
) -> ConnectorRegistry:
    """Load explicitly declared local providers from trusted installed Pack directories."""
    for pack in catalog.all():
        if pack.kind != "connector" or (selected_pack_ids is not None and pack.pack_id not in selected_pack_ids):
            continue
        declaration = pack.raw.get("connector_provider")
        if declaration is None:
            continue
        module_path = (pack.path.parent / declaration["module"]).resolve()
        try:
            module_path.relative_to(pack.path.parent.resolve())
        except ValueError as exc:
            raise ConnectorError(f"Connector provider escapes Pack directory: {pack.pack_id}") from exc
        module_name = "_opc_connector_" + hashlib.sha256(str(module_path).encode()).hexdigest()[:16]
        spec = importlib.util.spec_from_file_location(module_name, module_path)
        if spec is None or spec.loader is None:
            raise ConnectorError(f"Cannot load connector provider: {module_path}")
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        except Exception as exc:
            raise ConnectorError(f"Connector provider import failed for {pack.pack_id}: {exc}") from exc
        factory = getattr(module, declaration["factory"], None)
        if not callable(factory):
            raise ConnectorError(
                f"Connector provider factory is not callable: {pack.pack_id}.{declaration['factory']}"
            )
        before = {item.connector_id for item in registry.definitions()}
        factory(registry)
        added = [item for item in registry.definitions() if item.connector_id not in before]
        if not added:
            raise ConnectorError(f"Connector provider registered no connectors: {pack.pack_id}")
        for connector in added:
            if connector.pack_id != pack.pack_id:
                raise ConnectorError(
                    f"Provider {pack.pack_id} registered connector for another Pack: {connector.connector_id}"
                )
            if connector.capability not in pack.capabilities:
                raise ConnectorError(
                    f"Provider {pack.pack_id} used undeclared capability: {connector.capability}"
                )
    return registry
