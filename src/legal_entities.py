from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Iterable


class EntityScopeError(ValueError):
    """Raised when a record or action has no valid legal-entity scope."""


@dataclass(frozen=True)
class LegalEntity:
    entity_id: str
    legal_name: str
    jurisdiction: str
    functional_currency: str
    accounting_basis: str
    fiscal_year_end: str
    tax_pack: str
    tax_registrations: tuple[str, ...]
    tax_readiness: str | None = None
    tax_rules_effective_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["tax_registrations"] = list(self.tax_registrations)
        return payload


class EntityRegistry:
    def __init__(self, entities: Iterable[LegalEntity], reporting_currency: str | None = None):
        self._entities: dict[str, LegalEntity] = {}
        for entity in entities:
            if entity.entity_id in self._entities:
                raise EntityScopeError(f"Duplicate legal entity: {entity.entity_id}")
            self._entities[entity.entity_id] = entity
        if not self._entities:
            raise EntityScopeError("At least one legal entity is required")
        self.reporting_currency = reporting_currency

    @classmethod
    def from_resolved_box(cls, resolved: dict[str, Any]) -> "EntityRegistry":
        entities = []
        for item in resolved.get("entities") or []:
            entities.append(LegalEntity(
                entity_id=str(item["id"]),
                legal_name=str(item["name"]),
                jurisdiction=str(item["jurisdiction"]),
                functional_currency=str(item["functional_currency"]),
                accounting_basis=str(item["accounting_basis"]),
                fiscal_year_end=str(item["fiscal_year_end"]),
                tax_pack=str(item["tax_pack"]),
                tax_registrations=tuple(item.get("tax_registrations") or []),
                tax_readiness=item.get("tax_readiness"),
                tax_rules_effective_at=item.get("tax_rules_effective_at"),
            ))
        return cls(entities, resolved.get("reporting_currency"))

    def get(self, entity_id: str) -> LegalEntity:
        try:
            return self._entities[entity_id]
        except KeyError as exc:
            raise EntityScopeError(f"Unknown legal entity: {entity_id}") from exc

    def all(self) -> list[LegalEntity]:
        return list(self._entities.values())

    def ids(self) -> set[str]:
        return set(self._entities)

    def statutory_scope(self, entity_id: str) -> dict[str, Any]:
        entity = self.get(entity_id)
        return {
            "scope": "statutory",
            "entity": entity.to_dict(),
            "currency": entity.functional_currency,
            "books_must_remain_separate": True,
        }

    def management_scope(self, entity_ids: Iterable[str] | None = None) -> dict[str, Any]:
        selected_ids = list(entity_ids) if entity_ids is not None else list(self._entities)
        if not selected_ids:
            raise EntityScopeError("Management scope requires at least one legal entity")
        selected = [self.get(entity_id) for entity_id in selected_ids]
        currencies = sorted({entity.functional_currency for entity in selected})
        if len(currencies) > 1 and not self.reporting_currency:
            raise EntityScopeError("Multi-currency management scope requires reporting_currency")
        return {
            "scope": "management",
            "entity_ids": selected_ids,
            "reporting_currency": self.reporting_currency or currencies[0],
            "requires_fx_translation": len(currencies) > 1,
            "requires_intercompany_elimination": len(selected_ids) > 1,
            "statutory_guardrail": "管理合并不改变各法律主体的法定账、税务、银行和审批记录。",
        }


def build_legacy_entity(profile: dict[str, Any], entity_id: str = "legacy_entity") -> LegalEntity:
    """Adapt the current single-company profile without pretending it is jurisdiction-neutral."""
    jurisdiction = str(profile.get("jurisdiction") or "CN").upper()
    accounting_standard = str(profile.get("accounting_standard") or "")
    basis = {
        "小企业会计准则": "PRC_SMALL_ENTERPRISE_AS",
        "企业会计准则": "PRC_GAAP",
    }.get(accounting_standard, accounting_standard or "UNCONFIGURED")
    registrations = []
    vat_type = str(profile.get("vat_taxpayer_type") or "")
    if vat_type and vat_type != "待配置":
        registrations.append(f"vat:{vat_type}")
    if profile.get("payroll_enabled"):
        registrations.append("individual_income_tax_withholding")
    tax_pack = "jurisdiction.cn_mainland" if jurisdiction == "CN" else ""
    return LegalEntity(
        entity_id=entity_id,
        legal_name=str(profile.get("company_name") or "未命名主体"),
        jurisdiction=jurisdiction,
        functional_currency=str(profile.get("base_currency") or "CNY").upper(),
        accounting_basis=basis,
        fiscal_year_end=str(profile.get("fiscal_year_end") or "12-31"),
        tax_pack=tax_pack,
        tax_registrations=tuple(registrations),
        tax_readiness="workpaper" if jurisdiction == "CN" else None,
    )


def entity_scope_quality(
    datasets: dict[str, list[dict[str, Any]]],
    registry: EntityRegistry,
    *,
    entity_field: str = "entity_id",
) -> dict[str, Any]:
    dataset_rows: dict[str, dict[str, Any]] = {}
    unassigned: list[dict[str, str]] = []
    unknown: list[dict[str, str]] = []
    known_ids = registry.ids()
    for dataset_name, rows in datasets.items():
        if not isinstance(rows, list):
            continue
        assigned_count = 0
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            record_id = str(row.get("id") or f"row-{index + 1}")
            entity_id = str(row.get(entity_field) or "")
            if not entity_id:
                unassigned.append({"dataset": dataset_name, "record_id": record_id})
            elif entity_id not in known_ids:
                unknown.append({"dataset": dataset_name, "record_id": record_id, "entity_id": entity_id})
            else:
                assigned_count += 1
        dataset_rows[dataset_name] = {"total": len(rows), "assigned": assigned_count}
    return {
        "ready": not unassigned and not unknown,
        "datasets": dataset_rows,
        "unassigned_count": len(unassigned),
        "unknown_count": len(unknown),
        "unassigned": unassigned,
        "unknown": unknown,
        "blocker": (
            "多主体运行前，所有法定账相关记录都必须归属一个已配置法律主体。"
            if unassigned or unknown else ""
        ),
    }
