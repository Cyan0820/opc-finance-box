from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping

from .box_runtime import BoxRuntime, BoxRuntimeError


BOX_CONFIG_ENV = "OPC_FINANCE_BOX_CONFIG"
PACKS_ROOT_ENV = "OPC_FINANCE_PACKS_ROOT"


def load_default_box_runtime(
    project_root: str | Path,
    environ: Mapping[str, str] | None = None,
) -> BoxRuntime:
    root = Path(project_root)
    values = os.environ if environ is None else environ
    config_value = values.get(BOX_CONFIG_ENV)
    packs_value = values.get(PACKS_ROOT_ENV)
    config_path = Path(config_value) if config_value else root / "examples" / "boxes" / "global_game_studio.json"
    packs_root = Path(packs_value) if packs_value else root / "packs"
    if not config_path.is_absolute():
        config_path = root / config_path
    if not packs_root.is_absolute():
        packs_root = root / packs_root
    return BoxRuntime(config_path, packs_root)


def _capability_groups(capabilities: list[str]) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for capability in capabilities:
        namespace = capability.split(".", 1)[0]
        grouped.setdefault(namespace, []).append(capability)
    return {key: sorted(values) for key, values in sorted(grouped.items())}


def _workbench_profile(packs: list[dict[str, Any]]) -> dict[str, Any]:
    """Describe the reference UI that matches the selected Pack composition."""
    pack_ids = {str(pack.get("id") or "") for pack in packs}
    industry_ids = sorted(pack_id for pack_id in pack_ids if pack_id.startswith("industry."))
    channel_ids = sorted(pack_id for pack_id in pack_ids if pack_id.startswith("channel."))
    if "industry.game_studio" in pack_ids:
        profile, label, reference, demo = "game_studio", "游戏 OPC Box", "game_agent", "game_global"
    elif "industry.commerce" in pack_ids and "channel.marketplace_commerce" in pack_ids:
        profile, label, reference, demo = "commerce_marketplace", "电商平台 OPC Box", "box_control", None
    elif "industry.commerce" in pack_ids and "channel.dtc_storefront" in pack_ids:
        profile, label, reference, demo = "commerce_dtc", "独立站 OPC Box", "box_control", None
    else:
        profile, label, reference, demo = "finance_core", "OPC Finance Box", "box_control", None
    return {
        "profile": profile,
        "label": label,
        "reference_workbench": reference,
        "demo_dataset": demo,
        "industry_pack_ids": industry_ids,
        "channel_pack_ids": channel_ids,
    }


def build_box_context(
    runtime: BoxRuntime,
    *,
    scope: str = "management",
    entity_id: str | None = None,
    entity_ids: list[str] | None = None,
) -> dict[str, Any]:
    runtime.reload()
    snapshot = runtime.snapshot()
    workbench = _workbench_profile(snapshot["packs"])
    if scope == "statutory":
        if not entity_id:
            raise BoxRuntimeError("statutory scope requires entity_id")
        scope_context = runtime.entities.statutory_scope(entity_id)
    elif scope == "management":
        scope_context = runtime.entities.management_scope(entity_ids)
    else:
        raise BoxRuntimeError(f"Unsupported Box scope: {scope}")

    return {
        "product": {
            "name": snapshot["name"],
            "box_version": snapshot["box_version"],
            "data_mode": snapshot["data_mode"],
            "production_ready": snapshot["production_ready"],
            "workbench": workbench,
        },
        "scope": scope_context,
        "entities": snapshot["entities"],
        "packs": snapshot["packs"],
        "capability_groups": _capability_groups(snapshot["capabilities"]),
        "manual_review_gates": snapshot["manual_review_gates"],
        "tax_readiness": snapshot["tax_readiness"],
        "warnings": snapshot["warnings"],
        "guardrails": snapshot["guardrails"],
        "runtime": {
            "loaded_at": snapshot["loaded_at"],
            "fingerprint": snapshot["fingerprint"],
        },
    }
