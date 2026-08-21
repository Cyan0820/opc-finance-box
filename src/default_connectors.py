from __future__ import annotations

from pathlib import Path
from typing import Any
from dataclasses import asdict
import hashlib

from .commerce import (
    CommerceDataError,
    CommerceOrder,
    CommerceReturn,
    CommerceReturnReceipt,
    CommerceSettlement,
)
from .commerce_import import parse_commerce_file
from .commerce_import_costs import CommerceImportCost, ImportCostDataError
from .bank_import import BankImportError, parse_bank_statement_file
from .accounting_import import AccountingImportError, parse_trial_balance_file
from .ledger_import import LedgerImportError, parse_general_ledger_file
from .connector_sdk import ConnectorContext, ConnectorDefinition, ConnectorError, ConnectorRegistry
from .reconcile import parse_workbook as parse_game_settlement_workbook


COMMERCE_DATASET_TYPES = (
    "commerce.orders", "commerce.settlements", "commerce.returns",
    "commerce.return_receipts",
    "commerce.import_costs",
)
COMMERCE_BUSINESS_KEYS = {
    "commerce.orders": ("order_id",),
    "commerce.settlements": ("settlement_id",),
    "commerce.returns": ("return_id", "sku"),
    "commerce.return_receipts": ("receipt_id",),
    "commerce.import_costs": ("entry_line_id",),
}


def _trial_balance_file_handler(
    request: dict[str, Any], context: ConnectorContext,
) -> dict[str, Any]:
    path_value = request.get("path")
    entity_id = request.get("default_entity_id")
    if not isinstance(path_value, str) or not path_value.strip():
        raise ConnectorError("trial balance connector requires path")
    if entity_id not in context.allowed_entity_ids:
        raise ConnectorError("trial balance connector requires a valid default_entity_id")
    try:
        parsed = parse_trial_balance_file(
            path_value,
            entity_id=str(entity_id),
            default_period=str(request.get("default_period") or ""),
            default_currency=str(request.get("default_currency") or ""),
        )
    except (AccountingImportError, OSError) as exc:
        raise ConnectorError(str(exc)) from exc
    return {
        "batch_id": parsed["batch_id"],
        "source": parsed["source"],
        "datasets": {"finance.trial_balance_lines": parsed["records"]},
        "rejected_rows": parsed["rejected_rows"],
    }


def _general_ledger_file_handler(
    request: dict[str, Any], context: ConnectorContext,
) -> dict[str, Any]:
    path_value = request.get("path")
    entity_id = request.get("default_entity_id")
    if not isinstance(path_value, str) or not path_value.strip():
        raise ConnectorError("general ledger connector requires path")
    if entity_id not in context.allowed_entity_ids:
        raise ConnectorError("general ledger connector requires a valid default_entity_id")
    try:
        parsed = parse_general_ledger_file(
            path_value,
            entity_id=str(entity_id),
            default_period=str(request.get("default_period") or ""),
            default_currency=str(request.get("default_currency") or ""),
        )
    except (LedgerImportError, OSError) as exc:
        raise ConnectorError(str(exc)) from exc
    return {
        "batch_id": parsed["batch_id"],
        "source": parsed["source"],
        "datasets": {"finance.general_ledger_lines": parsed["records"]},
        "rejected_rows": parsed["rejected_rows"],
    }


def _bank_statement_file_handler(
    request: dict[str, Any], context: ConnectorContext,
) -> dict[str, Any]:
    path_value = request.get("path")
    entity_id = request.get("default_entity_id")
    if not isinstance(path_value, str) or not path_value.strip():
        raise ConnectorError("bank statement connector requires path")
    if entity_id not in context.allowed_entity_ids:
        raise ConnectorError("bank statement connector requires a valid default_entity_id")
    try:
        parsed = parse_bank_statement_file(
            path_value,
            entity_id=str(entity_id),
            default_currency=str(request.get("default_currency") or ""),
            account_reference=str(request.get("account_reference") or ""),
        )
    except (BankImportError, OSError) as exc:
        raise ConnectorError(str(exc)) from exc
    return {
        "batch_id": parsed["batch_id"],
        "source": parsed["source"],
        "datasets": {"finance.bank_transactions": parsed["records"]},
        "rejected_rows": parsed["rejected_rows"],
    }


def _commerce_file_handler(request: dict[str, Any], context: ConnectorContext) -> dict[str, Any]:
    path_value = request.get("path")
    if not isinstance(path_value, str) or not path_value.strip():
        raise ConnectorError("commerce file connector requires path")
    default_entity_id = request.get("default_entity_id")
    if default_entity_id is not None and default_entity_id not in context.allowed_entity_ids:
        raise ConnectorError(f"unknown default legal entity: {default_entity_id}")
    batch = parse_commerce_file(
        Path(path_value),
        record_type=request.get("record_type"),
        default_entity_id=default_entity_id,
        default_channel=request.get("default_channel"),
    )
    rejected = [
        {"dataset_type": f"commerce.{row.get('record_type')}", **row}
        for row in batch["quality"]["rejected_rows"]
    ]
    return {
        "batch_id": batch["batch_id"],
        "source": {"kind": "file", "name": batch["source_file"]},
        "datasets": {
            "commerce.orders": batch["orders"],
            "commerce.settlements": batch["settlements"],
            "commerce.returns": batch["returns"],
            "commerce.return_receipts": batch["return_receipts"],
            "commerce.import_costs": batch["import_costs"],
        },
        "rejected_rows": rejected,
        "duplicate_business_keys": batch["quality"]["duplicate_business_keys"],
    }


def _csv_commerce_handler(request: dict[str, Any], context: ConnectorContext) -> dict[str, Any]:
    path = str(request.get("path") or "")
    if Path(path).suffix.lower() != ".csv":
        raise ConnectorError("file.csv_commerce requires a .csv path")
    return _commerce_file_handler(request, context)


def _xlsx_commerce_handler(request: dict[str, Any], context: ConnectorContext) -> dict[str, Any]:
    path = str(request.get("path") or "")
    if Path(path).suffix.lower() != ".xlsx":
        raise ConnectorError("file.xlsx_commerce requires a .xlsx path")
    return _commerce_file_handler(request, context)


def _commerce_api_example_handler(request: dict[str, Any], context: ConnectorContext) -> dict[str, Any]:
    """Editable reference mapper; callers fetch/authenticate externally and pass a fictionalized payload."""
    payload = request.get("payload")
    if not isinstance(payload, dict):
        raise ConnectorError("API example connector requires payload object")
    batch_id = str(payload.get("batch_id") or "").strip()
    source_name = str(payload.get("source_name") or "").strip()
    if not batch_id or not source_name:
        raise ConnectorError("API payload requires batch_id and source_name")
    orders = []
    settlements = []
    returns = []
    return_receipts = []
    import_costs = []
    rejected = []
    for index, row in enumerate(payload.get("orders") or [], 1):
        if not isinstance(row, dict):
            rejected.append({"dataset_type": "commerce.orders", "row": index, "reason": "record must be object"})
            continue
        normalized = {
            "order_id": row.get("orderNumber"),
            "entity_id": row.get("entityId"),
            "period": str(row.get("processedAt") or "")[:7],
            "channel": row.get("store"),
            "destination_country": row.get("destinationCountry"),
            "currency": row.get("currency"),
            "merchandise_gross_ex_tax": row.get("subtotal", 0),
            "discounts_ex_tax": row.get("discounts", 0),
            "shipping_income_ex_tax": row.get("shippingIncome", 0),
            "tax_collected": row.get("tax", 0),
            "refunds_ex_tax": row.get("refund", 0),
            "refunded_tax": row.get("refundedTax", 0),
            "cogs": row.get("cost", 0),
            "fulfillment_cost": row.get("fulfillmentCost", 0),
            "shipping_cost": row.get("shippingCost", 0),
            "evidence": {
                "source_file": f"api:{source_name}",
                "source_sheet": "orders",
                "source_row": index,
                "batch_id": batch_id,
                "source_object_id": row.get("id") or row.get("orderNumber"),
            },
        }
        try:
            CommerceOrder.from_dict(normalized)
        except CommerceDataError as exc:
            rejected.append({"dataset_type": "commerce.orders", "row": index, "reason": str(exc)})
            continue
        orders.append(normalized)
    for index, row in enumerate(payload.get("payouts") or [], 1):
        if not isinstance(row, dict):
            rejected.append({"dataset_type": "commerce.settlements", "row": index, "reason": "record must be object"})
            continue
        normalized = {
            "settlement_id": row.get("payoutId"),
            "entity_id": row.get("entityId"),
            "period": str(row.get("period") or "")[:7],
            "channel": row.get("store"),
            "currency": row.get("currency"),
            "reported_order_inflow": row.get("orderInflow", 0),
            "channel_and_payment_fees": row.get("fees", 0),
            "tax_withheld_or_remitted": row.get("taxRemitted", 0),
            "other_adjustments": row.get("adjustments", 0),
            "payout": row.get("payout", 0),
            "evidence": {
                "source_file": f"api:{source_name}",
                "source_sheet": "payouts",
                "source_row": index,
                "batch_id": batch_id,
                "source_object_id": row.get("id") or row.get("payoutId"),
            },
        }
        try:
            CommerceSettlement.from_dict(normalized)
        except CommerceDataError as exc:
            rejected.append({"dataset_type": "commerce.settlements", "row": index, "reason": str(exc)})
            continue
        settlements.append(normalized)
    for index, row in enumerate(payload.get("returns") or [], 1):
        if not isinstance(row, dict):
            rejected.append({"dataset_type": "commerce.returns", "row": index, "reason": "record must be object"})
            continue
        normalized = {
            "return_id": row.get("returnId"),
            "order_id": row.get("orderNumber"),
            "entity_id": row.get("entityId"),
            "period": str(row.get("processedAt") or "")[:7],
            "channel": row.get("store"),
            "sku": row.get("sku"),
            "currency": row.get("currency"),
            "authorized_quantity": row.get("authorizedQuantity", 0),
            "refunded_quantity": row.get("refundedQuantity", 0),
            "refund_amount_ex_tax": row.get("refundAmountExTax", 0),
            "refunded_tax": row.get("refundedTax", 0),
            "evidence": {
                "source_file": f"api:{source_name}", "source_sheet": "returns",
                "source_row": index, "batch_id": batch_id,
                "source_object_id": row.get("id") or row.get("returnId"),
            },
        }
        try:
            CommerceReturn.from_dict(normalized)
        except CommerceDataError as exc:
            rejected.append({"dataset_type": "commerce.returns", "row": index, "reason": str(exc)})
            continue
        returns.append(normalized)
    for index, row in enumerate(payload.get("returnReceipts") or [], 1):
        if not isinstance(row, dict):
            rejected.append({
                "dataset_type": "commerce.return_receipts", "row": index,
                "reason": "record must be object",
            })
            continue
        normalized = {
            "receipt_id": row.get("receiptId"),
            "return_id": row.get("returnId"),
            "entity_id": row.get("entityId"),
            "period": str(row.get("receivedAt") or "")[:7],
            "sku": row.get("sku"),
            "warehouse": row.get("warehouse"),
            "received_quantity": row.get("receivedQuantity", 0),
            "disposition": row.get("disposition"),
            "evidence": {
                "source_file": f"api:{source_name}", "source_sheet": "returnReceipts",
                "source_row": index, "batch_id": batch_id,
                "source_object_id": row.get("id") or row.get("receiptId"),
            },
        }
        try:
            CommerceReturnReceipt.from_dict(normalized)
        except CommerceDataError as exc:
            rejected.append({
                "dataset_type": "commerce.return_receipts", "row": index,
                "reason": str(exc),
            })
            continue
        return_receipts.append(normalized)
    for index, row in enumerate(payload.get("importCosts") or [], 1):
        if not isinstance(row, dict):
            rejected.append({"dataset_type": "commerce.import_costs", "row": index, "reason": "record must be object"})
            continue
        normalized = {
            "entry_line_id": row.get("entryLineId"), "import_entry_id": row.get("importEntryId"),
            "entity_id": row.get("entityId"), "period": str(row.get("entryAt") or "")[:7],
            "sku": row.get("sku"), "warehouse": row.get("warehouse"),
            "origin_country": row.get("originCountry"),
            "destination_country": row.get("destinationCountry"), "currency": row.get("currency"),
            "quantity": row.get("quantity", 0), "declared_value": row.get("declaredValue", 0),
            "inbound_freight": row.get("inboundFreight", 0), "insurance": row.get("insurance", 0),
            "customs_duty": row.get("customsDuty", 0), "import_tax": row.get("importTax", 0),
            "brokerage": row.get("brokerage", 0),
            "evidence": {"source_file": f"api:{source_name}", "source_sheet": "importCosts",
                         "source_row": index, "batch_id": batch_id,
                         "source_object_id": row.get("id") or row.get("entryLineId")},
        }
        try:
            CommerceImportCost.from_dict(normalized)
        except ImportCostDataError as exc:
            rejected.append({"dataset_type": "commerce.import_costs", "row": index, "reason": str(exc)})
            continue
        import_costs.append(normalized)
    return {
        "batch_id": batch_id,
        "source": {"kind": "api_payload", "name": source_name, "network_access_performed": False},
        "datasets": {
            "commerce.orders": orders,
            "commerce.settlements": settlements,
            "commerce.returns": returns,
            "commerce.return_receipts": return_receipts,
            "commerce.import_costs": import_costs,
        },
        "rejected_rows": rejected,
    }


def _marketplace_api_example_handler(
    request: dict[str, Any], context: ConnectorContext,
) -> dict[str, Any]:
    """Editable normalized marketplace mapper with no network access or contract inference."""
    payload = request.get("payload")
    if not isinstance(payload, dict):
        raise ConnectorError("Marketplace API example requires payload object")
    batch_id = str(payload.get("batch_id") or "").strip()
    source_name = str(payload.get("source_name") or "").strip()
    if not batch_id or not source_name:
        raise ConnectorError("Marketplace API payload requires batch_id and source_name")
    default_entity_id = request.get("default_entity_id")
    if default_entity_id is not None and default_entity_id not in context.allowed_entity_ids:
        raise ConnectorError("Marketplace API payload requires a valid default_entity_id")
    datasets: dict[str, list[dict[str, Any]]] = {
        dataset_type: [] for dataset_type in COMMERCE_DATASET_TYPES
    }
    rejected = []
    for dataset_type, field, parser, business_key in (
        ("commerce.orders", "orders", CommerceOrder.from_dict, "order_id"),
        ("commerce.settlements", "settlements", CommerceSettlement.from_dict, "settlement_id"),
        ("commerce.returns", "returns", CommerceReturn.from_dict, "return_id"),
        (
            "commerce.return_receipts", "return_receipts",
            CommerceReturnReceipt.from_dict, "receipt_id",
        ),
        ("commerce.import_costs", "import_costs", CommerceImportCost.from_dict, "entry_line_id"),
    ):
        rows = payload.get(field) or []
        if not isinstance(rows, list):
            raise ConnectorError(f"Marketplace API payload {field} must be a list")
        for index, source in enumerate(rows, 1):
            if not isinstance(source, dict):
                rejected.append({
                    "dataset_type": dataset_type, "row": index, "reason": "record must be object",
                })
                continue
            row = dict(source)
            if default_entity_id and not row.get("entity_id"):
                row["entity_id"] = default_entity_id
            row["evidence"] = {
                "source_file": f"api:{source_name}",
                "source_sheet": field,
                "source_row": index,
                "batch_id": batch_id,
                "source_object_id": row.get(business_key),
            }
            try:
                parser(row)
            except (CommerceDataError, ImportCostDataError) as exc:
                rejected.append({
                    "dataset_type": dataset_type, "row": index, "reason": str(exc),
                })
                continue
            datasets[dataset_type].append(row)
    return {
        "batch_id": batch_id,
        "source": {
            "kind": "api_payload", "name": source_name, "network_access_performed": False,
        },
        "datasets": datasets,
        "rejected_rows": rejected,
    }


def _game_settlement_file_handler(
    request: dict[str, Any],
    context: ConnectorContext,
    expected_channel: str,
) -> dict[str, Any]:
    path_value = request.get("path")
    entity_id = request.get("default_entity_id")
    if not isinstance(path_value, str) or Path(path_value).suffix.lower() != ".xlsx":
        raise ConnectorError("game settlement connector requires an .xlsx path")
    if entity_id not in context.allowed_entity_ids:
        raise ConnectorError("game settlement connector requires a valid default_entity_id")
    path = Path(path_value)
    batch_id = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    records = []
    rejected = []
    for index, parsed in enumerate(parse_game_settlement_workbook(path), 1):
        row = asdict(parsed)
        channel = str(row.get("channel") or "").lower().replace(" ", "")
        matches = (
            "appstore" in channel if expected_channel == "app_store"
            else "google" in channel if expected_channel == "google_play"
            else "appstore" not in channel and "google" not in channel
        )
        if not matches:
            rejected.append({
                "dataset_type": "game.settlements",
                "row": index,
                "reason": f"channel {row.get('channel')} does not match {expected_channel}",
            })
            continue
        row["entity_id"] = entity_id
        row["evidence"] = {
            **dict(row.get("evidence") or {}),
            "source_file": row.get("source_file") or path.name,
            "source_sheet": row.get("source_sheet"),
            "source_row": (row.get("evidence") or {}).get("row"),
            "batch_id": batch_id,
        }
        records.append(row)
    return {
        "batch_id": batch_id,
        "source": {"kind": "file", "name": path.name},
        "datasets": {"game.settlements": records},
        "rejected_rows": rejected,
    }


def _app_store_game_handler(request: dict[str, Any], context: ConnectorContext) -> dict[str, Any]:
    return _game_settlement_file_handler(request, context, "app_store")


def _google_play_game_handler(request: dict[str, Any], context: ConnectorContext) -> dict[str, Any]:
    return _game_settlement_file_handler(request, context, "google_play")


def _domestic_game_handler(request: dict[str, Any], context: ConnectorContext) -> dict[str, Any]:
    return _game_settlement_file_handler(request, context, "domestic")


def build_default_connector_registry() -> ConnectorRegistry:
    registry = ConnectorRegistry()
    registry.register(ConnectorDefinition(
        connector_id="file.general_ledger",
        pack_id="connector.file_import",
        capability="connector.general_ledger_import",
        display_name="CSV / XLSX 会计系统总账明细只读导入",
        dataset_types=("finance.general_ledger_lines",),
        handler=_general_ledger_file_handler,
        business_keys={"finance.general_ledger_lines": ("journal_line_id",)},
    ))
    registry.register(ConnectorDefinition(
        connector_id="file.trial_balance",
        pack_id="connector.file_import",
        capability="connector.trial_balance_import",
        display_name="CSV / XLSX 会计系统试算平衡只读导入",
        dataset_types=("finance.trial_balance_lines",),
        handler=_trial_balance_file_handler,
        business_keys={"finance.trial_balance_lines": ("line_id",)},
    ))
    registry.register(ConnectorDefinition(
        connector_id="file.bank_statement",
        pack_id="connector.file_import",
        capability="connector.bank_statement_import",
        display_name="CSV / XLSX 银行对账单只读导入",
        dataset_types=("finance.bank_transactions",),
        handler=_bank_statement_file_handler,
        business_keys={"finance.bank_transactions": ("bank_transaction_id",)},
    ))
    registry.register(ConnectorDefinition(
        connector_id="file.commerce",
        pack_id="connector.file_import",
        capability="connector.commerce_standard_import",
        display_name="CSV / XLSX Commerce 标准导入",
        dataset_types=COMMERCE_DATASET_TYPES,
        handler=_commerce_file_handler,
        business_keys=COMMERCE_BUSINESS_KEYS,
    ))
    registry.register(ConnectorDefinition(
        connector_id="file.csv_commerce",
        pack_id="connector.file_import",
        capability="connector.csv",
        display_name="CSV Commerce 标准导入",
        dataset_types=COMMERCE_DATASET_TYPES,
        handler=_csv_commerce_handler,
        business_keys=COMMERCE_BUSINESS_KEYS,
    ))
    registry.register(ConnectorDefinition(
        connector_id="file.xlsx_commerce",
        pack_id="connector.file_import",
        capability="connector.xlsx",
        display_name="XLSX Commerce 标准导入",
        dataset_types=COMMERCE_DATASET_TYPES,
        handler=_xlsx_commerce_handler,
        business_keys=COMMERCE_BUSINESS_KEYS,
    ))
    registry.register(ConnectorDefinition(
        connector_id="file.marketplace_commerce",
        pack_id="channel.marketplace_commerce",
        capability="channel.marketplace_order_import",
        display_name="第三方电商平台标准订单/结算文件导入",
        dataset_types=COMMERCE_DATASET_TYPES,
        handler=_commerce_file_handler,
        business_keys=COMMERCE_BUSINESS_KEYS,
    ))
    registry.register(ConnectorDefinition(
        connector_id="example.commerce_api_payload",
        pack_id="connector.custom_api",
        capability="connector.custom_api_commerce_example",
        display_name="可编辑 Commerce API Payload 示例",
        dataset_types=COMMERCE_DATASET_TYPES,
        handler=_commerce_api_example_handler,
        business_keys=COMMERCE_BUSINESS_KEYS,
    ))
    registry.register(ConnectorDefinition(
        connector_id="example.marketplace_api_payload",
        pack_id="channel.marketplace_commerce",
        capability="channel.marketplace_order_import",
        display_name="可编辑 Marketplace 标准 API Payload 示例",
        dataset_types=COMMERCE_DATASET_TYPES,
        handler=_marketplace_api_example_handler,
        business_keys=COMMERCE_BUSINESS_KEYS,
    ))
    for connector_id, pack_id, capability, display_name, handler in (
        ("file.app_store_settlements", "channel.app_store", "channel.app_store_settlement_import", "App Store 游戏结算 XLSX 导入", _app_store_game_handler),
        ("file.google_play_settlements", "channel.google_play", "channel.google_play_settlement_import", "Google Play 游戏结算 XLSX 导入", _google_play_game_handler),
        ("file.domestic_game_settlements", "channel.domestic_game_platforms", "channel.domestic_game_settlement_import", "中国游戏渠道结算 XLSX 导入", _domestic_game_handler),
    ):
        registry.register(ConnectorDefinition(
            connector_id=connector_id,
            pack_id=pack_id,
            capability=capability,
            display_name=display_name,
            dataset_types=("game.settlements",),
            handler=handler,
            business_keys={"game.settlements": ("id",)},
        ))
    return registry


def build_box_connector_registry(runtime) -> ConnectorRegistry:
    from .box_config import load_pack_catalog
    from .connector_extensions import load_connector_providers

    registry = build_default_connector_registry()
    selected = {pack["id"] for pack in runtime.snapshot()["packs"]}
    return load_connector_providers(
        registry, load_pack_catalog(runtime.packs_root), selected_pack_ids=selected,
    )
