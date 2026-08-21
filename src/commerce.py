from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Iterable


CENT = Decimal("0.01")


class CommerceDataError(ValueError):
    """Raised when commerce facts cannot be safely calculated."""


def _decimal(value: Any, field: str) -> Decimal:
    try:
        result = Decimal(str(value if value is not None else 0))
    except (InvalidOperation, ValueError) as exc:
        raise CommerceDataError(f"{field} must be numeric") from exc
    if not result.is_finite():
        raise CommerceDataError(f"{field} must be finite")
    return result.quantize(CENT, rounding=ROUND_HALF_UP)


def _money(value: Decimal) -> float:
    return float(value.quantize(CENT, rounding=ROUND_HALF_UP))


def _quantity(value: Any, field: str, *, positive: bool = False) -> Decimal:
    try:
        result = Decimal(str(value if value is not None else 0))
    except (InvalidOperation, ValueError) as exc:
        raise CommerceDataError(f"{field} must be numeric") from exc
    if not result.is_finite() or result < 0 or (positive and result == 0):
        qualifier = "positive" if positive else "non-negative"
        raise CommerceDataError(f"{field} must be a finite {qualifier} number")
    return result


def _period(value: Any) -> str:
    text = str(value or "")
    if len(text) != 7 or text[4] != "-" or not text[:4].isdigit() or text[5:] not in {
        f"{month:02d}" for month in range(1, 13)
    }:
        raise CommerceDataError(f"invalid period: {text}")
    return text


@dataclass(frozen=True)
class CommerceOrder:
    order_id: str
    entity_id: str
    period: str
    channel: str
    destination_country: str
    currency: str
    merchandise_gross_ex_tax: Decimal
    discounts_ex_tax: Decimal
    shipping_income_ex_tax: Decimal
    tax_collected: Decimal
    refunds_ex_tax: Decimal
    refunded_tax: Decimal
    cogs: Decimal
    fulfillment_cost: Decimal
    shipping_cost: Decimal
    evidence: dict[str, Any]

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> "CommerceOrder":
        required = ("order_id", "entity_id", "period", "channel", "destination_country", "currency")
        missing = [field for field in required if not str(row.get(field) or "").strip()]
        if missing:
            raise CommerceDataError(f"order missing required fields: {', '.join(missing)}")
        money_fields = (
            "merchandise_gross_ex_tax", "discounts_ex_tax", "shipping_income_ex_tax",
            "tax_collected", "refunds_ex_tax", "refunded_tax", "cogs",
            "fulfillment_cost", "shipping_cost",
        )
        values = {field: _decimal(row.get(field), field) for field in money_fields}
        if any(value < 0 for value in values.values()):
            raise CommerceDataError(f"order {row['order_id']} contains negative money fields")
        if values["discounts_ex_tax"] + values["refunds_ex_tax"] > values["merchandise_gross_ex_tax"]:
            raise CommerceDataError(f"order {row['order_id']} discounts and refunds exceed gross merchandise")
        return cls(
            order_id=str(row["order_id"]),
            entity_id=str(row["entity_id"]),
            period=_period(row["period"]),
            channel=str(row["channel"]),
            destination_country=str(row["destination_country"]).upper(),
            currency=str(row["currency"]).upper(),
            evidence=dict(row.get("evidence") or {}),
            **values,
        )

    def metrics(self) -> dict[str, Decimal]:
        customer_charge = (
            self.merchandise_gross_ex_tax - self.discounts_ex_tax
            + self.shipping_income_ex_tax + self.tax_collected
        )
        refunds_total = self.refunds_ex_tax + self.refunded_tax
        net_processor_inflow = customer_charge - refunds_total
        net_revenue_ex_tax = (
            self.merchandise_gross_ex_tax - self.discounts_ex_tax
            + self.shipping_income_ex_tax - self.refunds_ex_tax
        )
        contribution_before_channel_fees = (
            net_revenue_ex_tax - self.cogs - self.fulfillment_cost - self.shipping_cost
        )
        return {
            "customer_charge": customer_charge,
            "refunds_total": refunds_total,
            "net_processor_inflow": net_processor_inflow,
            "net_revenue_ex_tax": net_revenue_ex_tax,
            "tax_evidence_net": self.tax_collected - self.refunded_tax,
            "contribution_before_channel_fees": contribution_before_channel_fees,
        }


@dataclass(frozen=True)
class CommerceSettlement:
    settlement_id: str
    entity_id: str
    period: str
    channel: str
    currency: str
    reported_order_inflow: Decimal
    channel_and_payment_fees: Decimal
    tax_withheld_or_remitted: Decimal
    other_adjustments: Decimal
    payout: Decimal
    evidence: dict[str, Any]

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> "CommerceSettlement":
        required = ("settlement_id", "entity_id", "period", "channel", "currency")
        missing = [field for field in required if not str(row.get(field) or "").strip()]
        if missing:
            raise CommerceDataError(f"settlement missing required fields: {', '.join(missing)}")
        money_fields = (
            "reported_order_inflow", "channel_and_payment_fees",
            "tax_withheld_or_remitted", "other_adjustments", "payout",
        )
        values = {field: _decimal(row.get(field), field) for field in money_fields}
        if any(values[field] < 0 for field in (
            "reported_order_inflow", "channel_and_payment_fees", "tax_withheld_or_remitted", "payout"
        )):
            raise CommerceDataError(f"settlement {row['settlement_id']} contains invalid negative amounts")
        return cls(
            settlement_id=str(row["settlement_id"]),
            entity_id=str(row["entity_id"]),
            period=_period(row["period"]),
            channel=str(row["channel"]),
            currency=str(row["currency"]).upper(),
            evidence=dict(row.get("evidence") or {}),
            **values,
        )


@dataclass(frozen=True)
class CommerceReturn:
    return_id: str
    order_id: str
    entity_id: str
    period: str
    channel: str
    sku: str
    currency: str
    authorized_quantity: Decimal
    refunded_quantity: Decimal
    refund_amount_ex_tax: Decimal
    refunded_tax: Decimal
    evidence: dict[str, Any]

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> "CommerceReturn":
        required = (
            "return_id", "order_id", "entity_id", "period", "channel", "sku", "currency",
        )
        missing = [field for field in required if not str(row.get(field) or "").strip()]
        if missing:
            raise CommerceDataError(f"return missing required fields: {', '.join(missing)}")
        authorized = _quantity(
            row.get("authorized_quantity"), "authorized_quantity", positive=True,
        )
        refunded = _quantity(row.get("refunded_quantity"), "refunded_quantity")
        if refunded > authorized:
            raise CommerceDataError(
                f"return {row['return_id']} refunded quantity exceeds authorized quantity"
            )
        refund_amount = _decimal(row.get("refund_amount_ex_tax"), "refund_amount_ex_tax")
        refunded_tax = _decimal(row.get("refunded_tax"), "refunded_tax")
        if refund_amount < 0 or refunded_tax < 0:
            raise CommerceDataError(f"return {row['return_id']} contains negative refund amounts")
        evidence = row.get("evidence")
        if not isinstance(evidence, dict) or not evidence:
            raise CommerceDataError(f"return {row['return_id']} requires evidence")
        return cls(
            return_id=str(row["return_id"]),
            order_id=str(row["order_id"]),
            entity_id=str(row["entity_id"]),
            period=_period(row["period"]),
            channel=str(row["channel"]),
            sku=str(row["sku"]),
            currency=str(row["currency"]).upper(),
            authorized_quantity=authorized,
            refunded_quantity=refunded,
            refund_amount_ex_tax=refund_amount,
            refunded_tax=refunded_tax,
            evidence=dict(evidence),
        )


RETURN_DISPOSITIONS = frozenset({"restockable", "damaged", "inspection_pending"})


@dataclass(frozen=True)
class CommerceReturnReceipt:
    receipt_id: str
    return_id: str
    entity_id: str
    period: str
    sku: str
    warehouse: str
    received_quantity: Decimal
    disposition: str
    evidence: dict[str, Any]

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> "CommerceReturnReceipt":
        required = (
            "receipt_id", "return_id", "entity_id", "period", "sku", "warehouse",
            "disposition",
        )
        missing = [field for field in required if not str(row.get(field) or "").strip()]
        if missing:
            raise CommerceDataError(
                f"return receipt missing required fields: {', '.join(missing)}"
            )
        disposition = str(row["disposition"]).strip().lower()
        if disposition not in RETURN_DISPOSITIONS:
            raise CommerceDataError(
                f"return receipt {row['receipt_id']} disposition must be one of: "
                + ", ".join(sorted(RETURN_DISPOSITIONS))
            )
        evidence = row.get("evidence")
        if not isinstance(evidence, dict) or not evidence:
            raise CommerceDataError(f"return receipt {row['receipt_id']} requires evidence")
        return cls(
            receipt_id=str(row["receipt_id"]),
            return_id=str(row["return_id"]),
            entity_id=str(row["entity_id"]),
            period=_period(row["period"]),
            sku=str(row["sku"]),
            warehouse=str(row["warehouse"]),
            received_quantity=_quantity(
                row.get("received_quantity"), "received_quantity", positive=True,
            ),
            disposition=disposition,
            evidence=dict(evidence),
        )


def build_return_inventory_reconciliation(
    return_rows: Iterable[dict[str, Any]],
    receipt_rows: Iterable[dict[str, Any]],
    *,
    order_rows: Iterable[dict[str, Any]] = (),
    allowed_entity_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Reconcile refund facts to physical receipts without posting or changing inventory."""
    returns = [CommerceReturn.from_dict(row) for row in return_rows]
    receipts = [CommerceReturnReceipt.from_dict(row) for row in receipt_rows]
    orders = [CommerceOrder.from_dict(row) for row in order_rows]
    issues: list[dict[str, Any]] = []

    return_keys = [(row.entity_id, row.return_id, row.sku) for row in returns]
    receipt_keys = [(row.entity_id, row.receipt_id) for row in receipts]
    duplicate_returns = sorted({key for key in return_keys if return_keys.count(key) > 1})
    duplicate_receipts = sorted({key for key in receipt_keys if receipt_keys.count(key) > 1})
    if duplicate_returns:
        issues.append({
            "severity": "blocking", "type": "duplicate_return_business_key",
            "keys": ["|".join(key) for key in duplicate_returns],
        })
    if duplicate_receipts:
        issues.append({
            "severity": "blocking", "type": "duplicate_return_receipt_id",
            "keys": ["|".join(key) for key in duplicate_receipts],
        })

    actual_entities = (
        {row.entity_id for row in returns}
        | {row.entity_id for row in receipts}
        | {row.entity_id for row in orders}
    )
    if allowed_entity_ids is not None:
        unknown = sorted(actual_entities - allowed_entity_ids)
        if unknown:
            issues.append({
                "severity": "blocking", "type": "unknown_legal_entity", "entity_ids": unknown,
            })

    order_index = {(row.entity_id, row.order_id): row for row in orders}
    if orders:
        for row in returns:
            order = order_index.get((row.entity_id, row.order_id))
            if order is None:
                issues.append({
                    "severity": "blocking", "type": "orphan_return",
                    "return_id": row.return_id, "order_id": row.order_id,
                    "entity_id": row.entity_id, "sku": row.sku,
                })
            elif order.channel != row.channel or order.currency != row.currency:
                issues.append({
                    "severity": "blocking", "type": "return_order_dimension_mismatch",
                    "return_id": row.return_id, "order_id": row.order_id,
                    "entity_id": row.entity_id, "sku": row.sku,
                })
        return_amounts: dict[tuple[str, str], dict[str, Decimal]] = defaultdict(
            lambda: defaultdict(Decimal)
        )
        for row in returns:
            totals = return_amounts[(row.entity_id, row.order_id)]
            totals["refund_amount_ex_tax"] += row.refund_amount_ex_tax
            totals["refunded_tax"] += row.refunded_tax
        for order in orders:
            key = (order.entity_id, order.order_id)
            has_return_detail = key in return_amounts
            detail = return_amounts[key]
            if (
                detail["refund_amount_ex_tax"] != order.refunds_ex_tax
                or detail["refunded_tax"] != order.refunded_tax
            ):
                issues.append({
                    "severity": "high",
                    "type": "return_refund_amount_mismatch",
                    "entity_id": order.entity_id,
                    "order_id": order.order_id,
                    "currency": order.currency,
                    "order_refunds_ex_tax": _money(order.refunds_ex_tax),
                    "return_refunds_ex_tax": _money(detail["refund_amount_ex_tax"]),
                    "order_refunded_tax": _money(order.refunded_tax),
                    "return_refunded_tax": _money(detail["refunded_tax"]),
                    "policy_exception_required": not has_return_detail,
                })

    return_index = {(row.entity_id, row.return_id, row.sku): row for row in returns}
    receipts_by_return: dict[tuple[str, str, str], list[CommerceReturnReceipt]] = defaultdict(list)
    for receipt in receipts:
        key = (receipt.entity_id, receipt.return_id, receipt.sku)
        if key not in return_index:
            issues.append({
                "severity": "blocking", "type": "orphan_return_receipt",
                "receipt_id": receipt.receipt_id, "return_id": receipt.return_id,
                "entity_id": receipt.entity_id, "sku": receipt.sku,
            })
            continue
        receipts_by_return[key].append(receipt)
        if receipt.period < return_index[key].period:
            issues.append({
                "severity": "high", "type": "receipt_precedes_return_authorization",
                "receipt_id": receipt.receipt_id, "return_id": receipt.return_id,
                "entity_id": receipt.entity_id, "sku": receipt.sku,
            })

    warehouse_totals: dict[tuple[str, str, str, str], Decimal] = defaultdict(Decimal)
    for receipt in receipts:
        warehouse_totals[
            (receipt.entity_id, receipt.warehouse, receipt.sku, receipt.disposition)
        ] += receipt.received_quantity

    reconciliations = []
    restock_candidates = []
    for row in sorted(returns, key=lambda item: (item.entity_id, item.return_id, item.sku)):
        key = (row.entity_id, row.return_id, row.sku)
        matched = receipts_by_return.get(key, [])
        received = sum((item.received_quantity for item in matched), Decimal(0))
        dispositions: dict[str, Decimal] = defaultdict(Decimal)
        for item in matched:
            dispositions[item.disposition] += item.received_quantity
            if item.disposition == "restockable":
                restock_candidates.append({
                    "entity_id": item.entity_id,
                    "return_id": item.return_id,
                    "receipt_id": item.receipt_id,
                    "sku": item.sku,
                    "warehouse": item.warehouse,
                    "quantity": float(item.received_quantity),
                    "candidate_status": "requires_inventory_review",
                })

        status = "reconciled"
        issue_type = None
        if received > row.authorized_quantity:
            status, issue_type = "over_received", "over_received"
        elif row.refunded_quantity > received:
            status, issue_type = "refunded_without_receipt", "refunded_without_receipt"
        elif received > row.refunded_quantity:
            status, issue_type = "received_not_refunded", "received_not_refunded"
        elif received < row.authorized_quantity:
            status = "awaiting_return" if received == 0 else "open_authorization"
        elif dispositions.get("inspection_pending", Decimal(0)):
            status = "inspection_pending"

        result_row = {
            "entity_id": row.entity_id,
            "return_id": row.return_id,
            "order_id": row.order_id,
            "period": row.period,
            "channel": row.channel,
            "sku": row.sku,
            "currency": row.currency,
            "authorized_quantity": float(row.authorized_quantity),
            "refunded_quantity": float(row.refunded_quantity),
            "received_quantity": float(received),
            "open_quantity": float(max(row.authorized_quantity - received, Decimal(0))),
            "refund_amount_ex_tax": _money(row.refund_amount_ex_tax),
            "refunded_tax": _money(row.refunded_tax),
            "receipt_count": len(matched),
            "disposition_quantities": {
                name: float(dispositions.get(name, Decimal(0)))
                for name in sorted(RETURN_DISPOSITIONS)
            },
            "status": status,
        }
        reconciliations.append(result_row)
        if issue_type:
            issues.append({
                "severity": "high", "type": issue_type,
                "entity_id": row.entity_id, "return_id": row.return_id, "sku": row.sku,
                "authorized_quantity": result_row["authorized_quantity"],
                "refunded_quantity": result_row["refunded_quantity"],
                "received_quantity": result_row["received_quantity"],
            })
        elif status in {"awaiting_return", "open_authorization", "inspection_pending"}:
            issues.append({
                "severity": "warning", "type": status,
                "entity_id": row.entity_id, "return_id": row.return_id, "sku": row.sku,
            })

    return {
        "ready": not any(issue["severity"] in {"blocking", "high"} for issue in issues),
        "no_return_activity": not returns and not receipts,
        "reconciliations": reconciliations,
        "warehouse_disposition_summary": [{
            "entity_id": key[0], "warehouse": key[1], "sku": key[2],
            "disposition": key[3], "received_quantity": float(quantity),
        } for key, quantity in sorted(warehouse_totals.items())],
        "restock_candidates": restock_candidates,
        "issues": issues,
        "inventory_adjustment_performed": False,
        "refund_posting_performed": False,
        "review_gate": "return_disposition_review",
        "guardrails": [
            "Refunded quantity without matching warehouse receipt remains a high-risk exception, including returnless-refund cases.",
            "Order-level refund amounts must equal return-detail refund amounts; returnless refunds require an explicit reviewed policy exception.",
            "Restockable receipts produce candidates only; no inventory quantity, value or ledger entry is changed.",
            "Warehouse disposition does not infer customs duty, tax recovery or inventory valuation treatment.",
        ],
    }


def _key(row: CommerceOrder | CommerceSettlement) -> tuple[str, str, str, str]:
    return row.entity_id, row.period, row.channel, row.currency


def build_commerce_analysis(
    order_rows: Iterable[dict[str, Any]],
    settlement_rows: Iterable[dict[str, Any]],
    *,
    tolerance: float = 0.01,
    allowed_entity_ids: set[str] | None = None,
) -> dict[str, Any]:
    orders = [CommerceOrder.from_dict(row) for row in order_rows]
    settlements = [CommerceSettlement.from_dict(row) for row in settlement_rows]
    duplicate_order_ids = sorted({
        order.order_id for order in orders if sum(item.order_id == order.order_id for item in orders) > 1
    })
    duplicate_settlement_ids = sorted({
        row.settlement_id for row in settlements
        if sum(item.settlement_id == row.settlement_id for item in settlements) > 1
    })
    tolerance_value = _decimal(tolerance, "tolerance")

    order_groups: dict[tuple[str, str, str, str], dict[str, Decimal]] = defaultdict(
        lambda: defaultdict(Decimal)
    )
    destinations: dict[tuple[str, str, str, str], dict[str, Decimal]] = defaultdict(
        lambda: defaultdict(Decimal)
    )
    for order in orders:
        metrics = order.metrics()
        group = order_groups[_key(order)]
        group["order_count"] += Decimal(1)
        for name, value in metrics.items():
            group[name] += value
        group["gross_merchandise_sales_ex_tax"] += order.merchandise_gross_ex_tax
        group["discounts_ex_tax"] += order.discounts_ex_tax
        group["shipping_income_ex_tax"] += order.shipping_income_ex_tax
        group["refunds_ex_tax"] += order.refunds_ex_tax
        group["cogs"] += order.cogs
        group["fulfillment_cost"] += order.fulfillment_cost
        group["shipping_cost"] += order.shipping_cost
        destination = destinations[(order.entity_id, order.destination_country, order.period, order.currency)]
        destination["order_count"] += Decimal(1)
        destination["net_revenue_ex_tax"] += metrics["net_revenue_ex_tax"]
        destination["tax_evidence_net"] += metrics["tax_evidence_net"]

    settlement_groups: dict[tuple[str, str, str, str], dict[str, Decimal]] = defaultdict(
        lambda: defaultdict(Decimal)
    )
    for settlement in settlements:
        group = settlement_groups[_key(settlement)]
        group["settlement_count"] += Decimal(1)
        for field in (
            "reported_order_inflow", "channel_and_payment_fees",
            "tax_withheld_or_remitted", "other_adjustments", "payout",
        ):
            group[field] += getattr(settlement, field)

    reconciliations = []
    issues = []
    all_keys = sorted(set(order_groups) | set(settlement_groups))
    for key in all_keys:
        order_values = order_groups[key]
        settlement_values = settlement_groups[key]
        order_to_reported_difference = (
            settlement_values["reported_order_inflow"] - order_values["net_processor_inflow"]
        )
        calculated_payout = (
            settlement_values["reported_order_inflow"]
            - settlement_values["channel_and_payment_fees"]
            - settlement_values["tax_withheld_or_remitted"]
            + settlement_values["other_adjustments"]
        )
        payout_difference = settlement_values["payout"] - calculated_payout
        contribution_after_channel_fees = (
            order_values["contribution_before_channel_fees"]
            - settlement_values["channel_and_payment_fees"]
        )
        net_revenue = order_values["net_revenue_ex_tax"]
        margin = (
            contribution_after_channel_fees / net_revenue
            if net_revenue else None
        )
        status = "已核对"
        if not order_values["order_count"] or not settlement_values["settlement_count"]:
            status = "缺少订单或结算"
        elif abs(order_to_reported_difference) > tolerance_value or abs(payout_difference) > tolerance_value:
            status = "存在差异"
        row = {
            "entity_id": key[0], "period": key[1], "channel": key[2], "currency": key[3],
            "order_count": int(order_values["order_count"]),
            "settlement_count": int(settlement_values["settlement_count"]),
            "gross_merchandise_sales_ex_tax": _money(
                order_values["gross_merchandise_sales_ex_tax"]
            ),
            "discounts_ex_tax": _money(order_values["discounts_ex_tax"]),
            "shipping_income_ex_tax": _money(order_values["shipping_income_ex_tax"]),
            "refunds_ex_tax": _money(order_values["refunds_ex_tax"]),
            "net_revenue_ex_tax": _money(net_revenue),
            "tax_evidence_net": _money(order_values["tax_evidence_net"]),
            "net_processor_inflow": _money(order_values["net_processor_inflow"]),
            "reported_order_inflow": _money(settlement_values["reported_order_inflow"]),
            "order_to_reported_difference": _money(order_to_reported_difference),
            "channel_and_payment_fees": _money(settlement_values["channel_and_payment_fees"]),
            "tax_withheld_or_remitted": _money(settlement_values["tax_withheld_or_remitted"]),
            "reported_payout": _money(settlement_values["payout"]),
            "calculated_payout": _money(calculated_payout),
            "payout_difference": _money(payout_difference),
            "contribution_after_channel_fees": _money(contribution_after_channel_fees),
            "contribution_margin": round(float(margin), 4) if margin is not None else None,
            "status": status,
        }
        reconciliations.append(row)
        if status != "已核对":
            issues.append({
                "severity": "high" if status == "存在差异" else "blocking",
                "type": "commerce_reconciliation",
                "key": {"entity_id": key[0], "period": key[1], "channel": key[2], "currency": key[3]},
                "status": status,
                "order_to_reported_difference": row["order_to_reported_difference"],
                "payout_difference": row["payout_difference"],
            })

    if duplicate_order_ids:
        issues.append({"severity": "blocking", "type": "duplicate_order_id", "ids": duplicate_order_ids})
    if duplicate_settlement_ids:
        issues.append({"severity": "blocking", "type": "duplicate_settlement_id", "ids": duplicate_settlement_ids})
    if allowed_entity_ids is not None:
        actual_entity_ids = {row.entity_id for row in orders} | {row.entity_id for row in settlements}
        unknown_entity_ids = sorted(actual_entity_ids - allowed_entity_ids)
        if unknown_entity_ids:
            issues.append({
                "severity": "blocking",
                "type": "unknown_legal_entity",
                "entity_ids": unknown_entity_ids,
            })

    destination_rows = [{
        "entity_id": key[0], "destination_country": key[1], "period": key[2], "currency": key[3],
        "order_count": int(values["order_count"]),
        "net_revenue_ex_tax": _money(values["net_revenue_ex_tax"]),
        "tax_evidence_net": _money(values["tax_evidence_net"]),
        "tax_status": "交易目的地与已收税额证据；不等同于应纳税额或申报结论",
    } for key, values in sorted(destinations.items())]

    return {
        "ready": not any(issue["severity"] in {"blocking", "high"} for issue in issues),
        "reconciliations": reconciliations,
        "destination_summary": destination_rows,
        "issues": issues,
        "guardrails": [
            "不同法律主体、期间、渠道和币种分别核对，不直接混加。",
            "目的地与已收税额仅作为间接税判断证据，不自动推导纳税义务。",
            "商品成本、履约成本和渠道费用进入贡献利润，但不替代法定会计收入政策。",
        ],
    }
