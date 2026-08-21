from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Iterable


CENT = Decimal("0.01")


class ImportCostDataError(ValueError):
    """Raised when import-cost evidence cannot produce a safe candidate."""


def _number(value: Any, field: str, *, positive: bool = False) -> Decimal:
    try:
        result = Decimal(str(value if value is not None else 0))
    except (InvalidOperation, ValueError) as exc:
        raise ImportCostDataError(f"{field} must be numeric") from exc
    if not result.is_finite() or result < 0 or (positive and result == 0):
        qualifier = "positive" if positive else "non-negative"
        raise ImportCostDataError(f"{field} must be a finite {qualifier} number")
    return result


def _money(value: Decimal) -> float:
    return float(value.quantize(CENT, rounding=ROUND_HALF_UP))


@dataclass(frozen=True)
class CommerceImportCost:
    entry_line_id: str
    import_entry_id: str
    entity_id: str
    period: str
    sku: str
    warehouse: str
    origin_country: str
    destination_country: str
    currency: str
    quantity: Decimal
    declared_value: Decimal
    inbound_freight: Decimal
    insurance: Decimal
    customs_duty: Decimal
    import_tax: Decimal
    brokerage: Decimal
    evidence: dict[str, Any]

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> "CommerceImportCost":
        required = (
            "entry_line_id", "import_entry_id", "entity_id", "period", "sku", "warehouse",
            "origin_country", "destination_country", "currency",
        )
        missing = [field for field in required if not str(row.get(field) or "").strip()]
        if missing:
            raise ImportCostDataError(f"import cost missing required fields: {', '.join(missing)}")
        period = str(row["period"])
        if len(period) != 7 or period[4] != "-" or not period[:4].isdigit() or period[5:] not in {
            f"{month:02d}" for month in range(1, 13)
        }:
            raise ImportCostDataError(f"invalid period: {period}")
        for field in ("origin_country", "destination_country"):
            value = str(row[field]).upper()
            if len(value) != 2 or not value.isalpha():
                raise ImportCostDataError(f"{field} must be an ISO alpha-2 country code")
        evidence = row.get("evidence")
        if not isinstance(evidence, dict) or not evidence:
            raise ImportCostDataError(f"import cost {row['entry_line_id']} requires evidence")
        return cls(
            entry_line_id=str(row["entry_line_id"]), import_entry_id=str(row["import_entry_id"]),
            entity_id=str(row["entity_id"]), period=period, sku=str(row["sku"]),
            warehouse=str(row["warehouse"]),
            origin_country=str(row["origin_country"]).upper(),
            destination_country=str(row["destination_country"]).upper(),
            currency=str(row["currency"]).upper(),
            quantity=_number(row.get("quantity"), "quantity", positive=True),
            declared_value=_number(row.get("declared_value"), "declared_value"),
            inbound_freight=_number(row.get("inbound_freight"), "inbound_freight"),
            insurance=_number(row.get("insurance"), "insurance"),
            customs_duty=_number(row.get("customs_duty"), "customs_duty"),
            import_tax=_number(row.get("import_tax"), "import_tax"),
            brokerage=_number(row.get("brokerage"), "brokerage"),
            evidence=dict(evidence),
        )


def build_import_landed_cost_candidates(
    rows: Iterable[dict[str, Any]], *, allowed_entity_ids: set[str] | None = None,
) -> dict[str, Any]:
    entries = [CommerceImportCost.from_dict(row) for row in rows]
    keys = [(row.entity_id, row.entry_line_id) for row in entries]
    duplicates = sorted({key for key in keys if keys.count(key) > 1})
    issues: list[dict[str, Any]] = []
    if duplicates:
        issues.append({
            "severity": "blocking", "type": "duplicate_import_cost_line",
            "keys": ["|".join(key) for key in duplicates],
        })
    if allowed_entity_ids is not None:
        unknown = sorted({row.entity_id for row in entries} - allowed_entity_ids)
        if unknown:
            issues.append({
                "severity": "blocking", "type": "unknown_legal_entity", "entity_ids": unknown,
            })

    grouped: dict[tuple[str, str, str, str, str], dict[str, Decimal]] = defaultdict(
        lambda: defaultdict(Decimal)
    )
    routes: dict[tuple[str, str, str, str, str], set[tuple[str, str]]] = defaultdict(set)
    for row in entries:
        key = (row.entity_id, row.period, row.currency, row.warehouse, row.sku)
        values = grouped[key]
        for field in (
            "quantity", "declared_value", "inbound_freight", "insurance", "customs_duty",
            "import_tax", "brokerage",
        ):
            values[field] += getattr(row, field)
        routes[key].add((row.origin_country, row.destination_country))

    candidates = []
    for key, values in sorted(grouped.items()):
        inventory_cost_candidate = (
            values["declared_value"] + values["inbound_freight"] + values["insurance"]
            + values["customs_duty"] + values["brokerage"]
        )
        candidates.append({
            "entity_id": key[0], "period": key[1], "currency": key[2],
            "warehouse": key[3], "sku": key[4],
            "routes": [{"origin_country": route[0], "destination_country": route[1]}
                       for route in sorted(routes[key])],
            "quantity": float(values["quantity"]),
            "declared_value": _money(values["declared_value"]),
            "inbound_freight": _money(values["inbound_freight"]),
            "insurance": _money(values["insurance"]),
            "customs_duty": _money(values["customs_duty"]),
            "import_tax_evidence": _money(values["import_tax"]),
            "brokerage": _money(values["brokerage"]),
            "inventory_landed_cost_candidate": _money(inventory_cost_candidate),
            "unit_landed_cost_candidate": _money(inventory_cost_candidate / values["quantity"]),
            "status": "candidate_pending_import_landed_cost_policy",
        })
    return {
        "ready": not any(issue["severity"] in {"blocking", "high"} for issue in issues),
        "no_import_activity": not entries,
        "candidates": candidates,
        "issues": issues,
        "review_gate": "import_landed_cost_policy",
        "customs_classification_performed": False,
        "duty_rate_determined": False,
        "import_tax_recoverability_determined": False,
        "inventory_or_ledger_adjustment_performed": False,
        "guardrails": [
            "Declared value and imported charges are evidence, not a customs classification or duty-rate decision.",
            "Import tax is kept outside the inventory candidate until recoverability and accounting treatment are reviewed.",
            "The candidate never posts inventory value, tax, payable or general-ledger entries.",
        ],
    }
