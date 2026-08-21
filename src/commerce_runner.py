from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from .box_api import build_box_context
from .box_runtime import BoxRuntime
from .commerce import build_commerce_analysis, build_return_inventory_reconciliation
from .commerce_import import parse_commerce_file
from .commerce_import_costs import build_import_landed_cost_candidates


REQUIRED_CAPABILITIES = (
    "commerce.order_to_cash",
    "commerce.product_margin",
    "commerce.return_inventory_reconciliation",
    "channel.dtc_order_import",
    "channel.dtc_payment_reconciliation",
)


def run_commerce_box(
    runtime: BoxRuntime,
    input_paths: Iterable[str | Path],
    *,
    default_entity_id: str | None = None,
    default_channel: str | None = None,
) -> dict[str, Any]:
    runtime.reload()
    for capability in REQUIRED_CAPABILITIES:
        runtime.require_capability(capability)
    entity_ids = runtime.entities.ids()
    if default_entity_id:
        runtime.require_entity(default_entity_id)
    elif len(entity_ids) == 1:
        default_entity_id = next(iter(entity_ids))

    batches = []
    orders = []
    settlements = []
    returns = []
    return_receipts = []
    import_costs = []
    for input_path in input_paths:
        batch = parse_commerce_file(
            input_path,
            default_entity_id=default_entity_id,
            default_channel=default_channel,
        )
        batches.append({
            "batch_id": batch["batch_id"],
            "source_file": batch["source_file"],
            "quality": batch["quality"],
        })
        orders.extend(batch["orders"])
        settlements.extend(batch["settlements"])
        returns.extend(batch["returns"])
        return_receipts.extend(batch["return_receipts"])
        import_costs.extend(batch["import_costs"])

    analysis = build_commerce_analysis(
        orders,
        settlements,
        allowed_entity_ids=entity_ids,
    )
    return_inventory = build_return_inventory_reconciliation(
        returns, return_receipts, order_rows=orders, allowed_entity_ids=entity_ids,
    )
    import_landed_cost = build_import_landed_cost_candidates(
        import_costs, allowed_entity_ids=entity_ids,
    )
    import_ready = bool(batches) and all(batch["quality"]["ready"] for batch in batches)
    return {
        "ready": (
            import_ready and analysis["ready"] and return_inventory["ready"]
            and import_landed_cost["ready"]
        ),
        "box": build_box_context(runtime),
        "imports": batches,
        "analysis": analysis,
        "return_inventory": return_inventory,
        "import_landed_cost": import_landed_cost,
        "counts": {
            "input_files": len(batches),
            "orders": len(orders),
            "settlements": len(settlements),
            "returns": len(returns),
            "return_receipts": len(return_receipts),
            "import_costs": len(import_costs),
        },
    }
