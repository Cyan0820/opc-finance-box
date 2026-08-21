from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


ALLOWED_PACK_KINDS = {"core", "industry", "channel", "jurisdiction", "connector", "feature"}
ALLOWED_PACK_STATUSES = {"experimental", "preview", "stable"}
ALLOWED_TAX_READINESS = {"design", "workpaper", "filing_assist"}
PACK_REFERENCE_FIELDS = {
    "core": "core",
    "business_models": "industry",
    "channels": "channel",
    "connectors": "connector",
    "features": "feature",
}
PACK_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")
ENTITY_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_-]*$")
JURISDICTION_PATTERN = re.compile(r"^[A-Z]{2}(?:-[A-Z0-9]{1,3})?$")
CURRENCY_PATTERN = re.compile(r"^[A-Z]{3}$")
FISCAL_YEAR_END_PATTERN = re.compile(r"^\d{2}-\d{2}$")
TAX_REVERIFICATION_TRIGGERS = {
    "authority_source_change", "rule_effective_date_change", "pack_upgrade",
    "entity_applicability_change", "tax_registration_change",
}
SINGLE_CREDENTIAL_CONNECTOR_PACKS = {
    "connector.shopify", "connector.stripe",
}


class BoxConfigError(ValueError):
    """Raised when a Box configuration or pack manifest is invalid."""


@dataclass(frozen=True)
class PackManifest:
    pack_id: str
    kind: str
    display_name: str
    version: str
    status: str
    path: Path
    capabilities: tuple[str, ...]
    requires: tuple[str, ...]
    conflicts: tuple[str, ...]
    manual_review_gates: tuple[str, ...]
    jurisdiction: dict[str, Any] | None
    rules_file: Path | None
    rules: dict[str, Any] | None
    raw: dict[str, Any]


class PackCatalog:
    def __init__(self, packs: Iterable[PackManifest]):
        self._packs: dict[str, PackManifest] = {}
        for pack in packs:
            if pack.pack_id in self._packs:
                previous = self._packs[pack.pack_id]
                raise BoxConfigError(
                    f"Duplicate pack id {pack.pack_id}: {previous.path} and {pack.path}"
                )
            self._packs[pack.pack_id] = pack

    def get(self, pack_id: str) -> PackManifest:
        try:
            return self._packs[pack_id]
        except KeyError as exc:
            raise BoxConfigError(f"Unknown pack: {pack_id}") from exc

    def all(self) -> list[PackManifest]:
        return sorted(self._packs.values(), key=lambda item: item.pack_id)

    def __contains__(self, pack_id: str) -> bool:
        return pack_id in self._packs


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise BoxConfigError(f"Cannot read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise BoxConfigError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise BoxConfigError(f"Expected a JSON object in {path}")
    return payload


def _string_list(value: Any, field: str, path: Path) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise BoxConfigError(f"{path}: {field} must be a list of non-empty strings")
    if len(set(value)) != len(value):
        raise BoxConfigError(f"{path}: {field} contains duplicate values")
    return tuple(value)


def load_pack_manifest(path: str | Path) -> PackManifest:
    path = Path(path)
    payload = _read_json(path)
    required = ("id", "kind", "display_name", "version", "status")
    missing = [field for field in required if not str(payload.get(field) or "").strip()]
    if missing:
        raise BoxConfigError(f"{path}: missing required fields: {', '.join(missing)}")

    pack_id = str(payload["id"])
    if not PACK_ID_PATTERN.fullmatch(pack_id):
        raise BoxConfigError(f"{path}: invalid pack id {pack_id}")
    kind = str(payload["kind"])
    if kind not in ALLOWED_PACK_KINDS:
        raise BoxConfigError(f"{path}: invalid pack kind {kind}")
    status = str(payload["status"])
    if status not in ALLOWED_PACK_STATUSES:
        raise BoxConfigError(f"{path}: invalid pack status {status}")

    connector_provider = payload.get("connector_provider")
    if connector_provider is not None:
        if kind != "connector":
            raise BoxConfigError(f"{path}: only connector packs may declare connector_provider")
        if not isinstance(connector_provider, dict):
            raise BoxConfigError(f"{path}: connector_provider must be an object")
        module = str(connector_provider.get("module") or "")
        factory = str(connector_provider.get("factory") or "")
        if Path(module).name != module or Path(module).suffix != ".py":
            raise BoxConfigError(f"{path}: connector_provider.module must be one relative .py filename")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", factory):
            raise BoxConfigError(f"{path}: connector_provider.factory must be a Python identifier")
        if not (path.parent / module).is_file():
            raise BoxConfigError(f"{path}: connector provider module does not exist: {module}")

    jurisdiction = payload.get("jurisdiction")
    rules_file = None
    rules = None
    if kind == "jurisdiction":
        if not isinstance(jurisdiction, dict):
            raise BoxConfigError(f"{path}: jurisdiction packs require a jurisdiction object")
        code = str(jurisdiction.get("code") or "")
        if not JURISDICTION_PATTERN.fullmatch(code):
            raise BoxConfigError(f"{path}: invalid jurisdiction code {code}")
        readiness = str(jurisdiction.get("tax_readiness") or "")
        if readiness not in ALLOWED_TAX_READINESS:
            raise BoxConfigError(f"{path}: invalid tax_readiness {readiness}")
        if not str(jurisdiction.get("rules_effective_at") or ""):
            raise BoxConfigError(f"{path}: jurisdiction packs require rules_effective_at")
        rules_value = str(payload.get("rules_file") or "")
        if not rules_value:
            raise BoxConfigError(f"{path}: jurisdiction packs require rules_file")
        rules_file = path.parent / rules_value
        rules = load_jurisdiction_rules(rules_file, code)
    elif jurisdiction is not None:
        raise BoxConfigError(f"{path}: only jurisdiction packs may declare jurisdiction")

    return PackManifest(
        pack_id=pack_id,
        kind=kind,
        display_name=str(payload["display_name"]),
        version=str(payload["version"]),
        status=status,
        path=path,
        capabilities=_string_list(payload.get("capabilities"), "capabilities", path),
        requires=_string_list(payload.get("requires"), "requires", path),
        conflicts=_string_list(payload.get("conflicts"), "conflicts", path),
        manual_review_gates=_string_list(payload.get("manual_review_gates"), "manual_review_gates", path),
        jurisdiction=jurisdiction,
        rules_file=rules_file,
        rules=rules,
        raw=payload,
    )


def load_jurisdiction_rules(path: str | Path, expected_code: str | None = None) -> dict[str, Any]:
    path = Path(path)
    payload = _read_json(path)
    if payload.get("schema_version") != 1:
        raise BoxConfigError(f"{path}: schema_version must be 1")
    jurisdiction = str(payload.get("jurisdiction") or "")
    if not JURISDICTION_PATTERN.fullmatch(jurisdiction):
        raise BoxConfigError(f"{path}: invalid jurisdiction code {jurisdiction}")
    if expected_code and jurisdiction != expected_code:
        raise BoxConfigError(f"{path}: jurisdiction {jurisdiction} does not match {expected_code}")
    verified_at = str(payload.get("verified_at") or "")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", verified_at):
        raise BoxConfigError(f"{path}: verified_at must use YYYY-MM-DD")
    try:
        datetime.strptime(verified_at, "%Y-%m-%d")
    except ValueError as exc:
        raise BoxConfigError(f"{path}: verified_at must be a real calendar date") from exc
    review_policy = payload.get("review_policy")
    required_policy_fields = {
        "max_age_days", "warning_days_before_expiry", "expiry_effect",
        "reverification_triggers",
    }
    if not isinstance(review_policy, dict) or set(review_policy) != required_policy_fields:
        raise BoxConfigError(
            f"{path}: review_policy must contain exactly {sorted(required_policy_fields)}"
        )
    max_age_days = review_policy.get("max_age_days")
    warning_days = review_policy.get("warning_days_before_expiry")
    if (
        not isinstance(max_age_days, int) or isinstance(max_age_days, bool)
        or not 30 <= max_age_days <= 730
    ):
        raise BoxConfigError(f"{path}: review_policy.max_age_days must be 30-730")
    if (
        not isinstance(warning_days, int) or isinstance(warning_days, bool)
        or not 1 <= warning_days < max_age_days
    ):
        raise BoxConfigError(
            f"{path}: review_policy.warning_days_before_expiry must be below max_age_days"
        )
    if review_policy.get("expiry_effect") != "block_external_filing_and_calendar_release":
        raise BoxConfigError(f"{path}: review_policy.expiry_effect is unsupported")
    triggers = review_policy.get("reverification_triggers")
    if (
        not isinstance(triggers, list) or not triggers
        or len(triggers) != len(set(triggers))
        or any(item not in TAX_REVERIFICATION_TRIGGERS for item in triggers)
    ):
        raise BoxConfigError(f"{path}: review_policy.reverification_triggers is invalid")
    applicability_policy = payload.get("applicability_review_policy")
    if (
        not isinstance(applicability_policy, dict)
        or set(applicability_policy) != required_policy_fields
    ):
        raise BoxConfigError(
            f"{path}: applicability_review_policy must contain exactly "
            f"{sorted(required_policy_fields)}"
        )
    applicability_max_age = applicability_policy.get("max_age_days")
    applicability_warning_days = applicability_policy.get(
        "warning_days_before_expiry"
    )
    if (
        not isinstance(applicability_max_age, int)
        or isinstance(applicability_max_age, bool)
        or not 30 <= applicability_max_age <= 730
    ):
        raise BoxConfigError(
            f"{path}: applicability_review_policy.max_age_days must be 30-730"
        )
    if (
        not isinstance(applicability_warning_days, int)
        or isinstance(applicability_warning_days, bool)
        or not 1 <= applicability_warning_days < applicability_max_age
    ):
        raise BoxConfigError(
            f"{path}: applicability_review_policy.warning_days_before_expiry "
            "must be below max_age_days"
        )
    if (
        applicability_policy.get("expiry_effect")
        != "block_calendar_and_external_filing_release"
    ):
        raise BoxConfigError(
            f"{path}: applicability_review_policy.expiry_effect is unsupported"
        )
    applicability_triggers = applicability_policy.get("reverification_triggers")
    required_applicability_triggers = {
        "pack_upgrade", "entity_applicability_change", "tax_registration_change",
    }
    if (
        not isinstance(applicability_triggers, list)
        or len(applicability_triggers) != len(set(applicability_triggers))
        or any(
            item not in TAX_REVERIFICATION_TRIGGERS
            for item in applicability_triggers
        )
        or not required_applicability_triggers <= set(applicability_triggers)
    ):
        raise BoxConfigError(
            f"{path}: applicability_review_policy.reverification_triggers is invalid"
        )
    sources = payload.get("sources")
    rules = payload.get("rules")
    if not isinstance(sources, list) or not sources:
        raise BoxConfigError(f"{path}: at least one official source is required")
    if not isinstance(rules, list) or not rules:
        raise BoxConfigError(f"{path}: at least one rule is required")
    source_ids: set[str] = set()
    for index, source in enumerate(sources):
        if not isinstance(source, dict):
            raise BoxConfigError(f"{path}: sources[{index}] must be an object")
        source_id = str(source.get("id") or "")
        if not source_id or source_id in source_ids:
            raise BoxConfigError(f"{path}: source ids must be non-empty and unique")
        source_ids.add(source_id)
        if not str(source.get("authority") or "") or not str(source.get("title") or ""):
            raise BoxConfigError(f"{path}: source {source_id} requires authority and title")
        if not str(source.get("url") or "").startswith("https://"):
            raise BoxConfigError(f"{path}: source {source_id} requires an https URL")
    rule_ids: set[str] = set()
    for index, rule in enumerate(rules):
        if not isinstance(rule, dict):
            raise BoxConfigError(f"{path}: rules[{index}] must be an object")
        rule_id = str(rule.get("id") or "")
        if not rule_id or rule_id in rule_ids:
            raise BoxConfigError(f"{path}: rule ids must be non-empty and unique")
        rule_ids.add(rule_id)
        references = rule.get("source_ids")
        if not isinstance(references, list) or not references:
            raise BoxConfigError(f"{path}: rule {rule_id} requires source_ids")
        unknown = set(references) - source_ids
        if unknown:
            raise BoxConfigError(f"{path}: rule {rule_id} references unknown sources: {sorted(unknown)}")
        if rule.get("automation_level") not in {"calendar", "evidence", "workpaper"}:
            raise BoxConfigError(f"{path}: rule {rule_id} has invalid automation_level")
        if rule.get("human_review_required") is not True:
            raise BoxConfigError(f"{path}: rule {rule_id} must require human review")
        if rule.get("automation_level") == "calendar":
            _validate_calendar_schedule(rule, rule_id, path)
    return payload


def _validate_calendar_schedule(rule: dict[str, Any], rule_id: str, path: Path) -> None:
    schedule = rule.get("schedule")
    if not isinstance(schedule, dict):
        raise BoxConfigError(f"{path}: calendar rule {rule_id} requires a schedule")
    kind = schedule.get("kind")
    allowed_kinds = {
        "days_after_date", "months_after_date", "annual_fixed_after_date",
        "manual_configuration",
    }
    if kind not in allowed_kinds:
        raise BoxConfigError(f"{path}: calendar rule {rule_id} has invalid schedule kind")
    if not str(rule.get("review_gate") or "").strip():
        raise BoxConfigError(f"{path}: calendar rule {rule_id} requires review_gate")
    if kind in {"days_after_date", "months_after_date", "annual_fixed_after_date"}:
        if not str(schedule.get("anchor") or "").strip():
            raise BoxConfigError(f"{path}: calendar rule {rule_id} requires schedule.anchor")
    if kind == "days_after_date":
        if not isinstance(schedule.get("days"), int) or schedule["days"] < 1:
            raise BoxConfigError(
                f"{path}: calendar rule {rule_id} requires positive integer days"
            )
    elif kind == "months_after_date":
        if not isinstance(schedule.get("months"), int) or schedule["months"] < 1:
            raise BoxConfigError(f"{path}: calendar rule {rule_id} requires positive integer months")
    elif kind == "annual_fixed_after_date":
        month = schedule.get("month")
        day = schedule.get("day")
        year_offset = schedule.get("year_offset")
        if not isinstance(month, int) or not 1 <= month <= 12:
            raise BoxConfigError(f"{path}: calendar rule {rule_id} has invalid month")
        if not isinstance(day, int) or not 1 <= day <= 31:
            raise BoxConfigError(f"{path}: calendar rule {rule_id} has invalid day")
        if not isinstance(year_offset, int) or year_offset < 0:
            raise BoxConfigError(f"{path}: calendar rule {rule_id} has invalid year_offset")
    else:
        fields = schedule.get("required_fields")
        if not isinstance(fields, list) or not fields or any(not isinstance(item, str) or not item for item in fields):
            raise BoxConfigError(
                f"{path}: manual calendar rule {rule_id} requires non-empty required_fields"
            )
    registration_any = schedule.get("registration_any")
    if registration_any is not None and (
        not isinstance(registration_any, list)
        or not registration_any
        or any(not isinstance(item, str) or not item for item in registration_any)
    ):
        raise BoxConfigError(f"{path}: calendar rule {rule_id} has invalid registration_any")


def load_pack_catalog(root: str | Path) -> PackCatalog:
    root = Path(root)
    if not root.exists():
        raise BoxConfigError(f"Pack directory does not exist: {root}")
    paths = sorted(root.rglob("manifest.json"))
    if not paths:
        raise BoxConfigError(f"No pack manifests found under {root}")
    catalog = PackCatalog(load_pack_manifest(path) for path in paths)
    for pack in catalog.all():
        for dependency in pack.requires:
            if dependency not in catalog:
                raise BoxConfigError(f"{pack.path}: unknown dependency {dependency}")
        for conflict in pack.conflicts:
            if conflict not in catalog:
                raise BoxConfigError(f"{pack.path}: unknown conflict {conflict}")
    return catalog


def load_box_config(path: str | Path) -> dict[str, Any]:
    return _read_json(Path(path))


def _pack_references(config: dict[str, Any]) -> list[tuple[str, str]]:
    references: list[tuple[str, str]] = []
    core = config.get("core")
    if isinstance(core, str) and core:
        references.append((core, "core"))
    for field, expected_kind in PACK_REFERENCE_FIELDS.items():
        if field == "core":
            continue
        values = config.get(field, [])
        if isinstance(values, list):
            references.extend((value, expected_kind) for value in values if isinstance(value, str))
    for entity in config.get("entities", []) if isinstance(config.get("entities"), list) else []:
        if isinstance(entity, dict) and isinstance(entity.get("tax_pack"), str):
            references.append((entity["tax_pack"], "jurisdiction"))
    return references


def validate_box_config(config: dict[str, Any], catalog: PackCatalog) -> list[str]:
    errors: list[str] = []
    if config.get("box_version") != 1:
        errors.append("box_version must be 1")
    if not str(config.get("name") or "").strip():
        errors.append("name is required")
    if config.get("data_mode", "live") not in {"demo", "live"}:
        errors.append("data_mode must be demo or live")
    reporting_currency = config.get("reporting_currency")
    if reporting_currency is not None and not CURRENCY_PATTERN.fullmatch(str(reporting_currency)):
        errors.append("reporting_currency must be a 3-letter currency code")
    if not isinstance(config.get("core"), str) or not config.get("core"):
        errors.append("core must reference one core pack")

    for field in ("business_models", "channels", "connectors", "features"):
        value = config.get(field, [])
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            errors.append(f"{field} must be a list of pack ids")
        elif len(set(value)) != len(value):
            errors.append(f"{field} contains duplicate pack ids")
    if not config.get("business_models"):
        errors.append("at least one business model is required")

    entities = config.get("entities")
    if not isinstance(entities, list) or not entities:
        errors.append("at least one legal entity is required")
        entities = []
    seen_entity_ids: set[str] = set()
    for index, entity in enumerate(entities):
        label = f"entities[{index}]"
        if not isinstance(entity, dict):
            errors.append(f"{label} must be an object")
            continue
        entity_id = str(entity.get("id") or "")
        if not ENTITY_ID_PATTERN.fullmatch(entity_id):
            errors.append(f"{label}.id is invalid")
        elif entity_id in seen_entity_ids:
            errors.append(f"duplicate entity id: {entity_id}")
        seen_entity_ids.add(entity_id)
        if not str(entity.get("name") or "").strip():
            errors.append(f"{label}.name is required")
        jurisdiction = str(entity.get("jurisdiction") or "")
        if not JURISDICTION_PATTERN.fullmatch(jurisdiction):
            errors.append(f"{label}.jurisdiction is invalid")
        currency = str(entity.get("functional_currency") or "")
        if not CURRENCY_PATTERN.fullmatch(currency):
            errors.append(f"{label}.functional_currency must be a 3-letter currency code")
        if not str(entity.get("accounting_basis") or "").strip():
            errors.append(f"{label}.accounting_basis is required")
        registrations = entity.get("tax_registrations")
        if not isinstance(registrations, list):
            errors.append(f"{label}.tax_registrations must be a list")
        fiscal_year_end = str(entity.get("fiscal_year_end") or "")
        if not FISCAL_YEAR_END_PATTERN.fullmatch(fiscal_year_end):
            errors.append(f"{label}.fiscal_year_end must use MM-DD")
        else:
            try:
                # Leap year accepts 02-29 while still rejecting impossible month/day pairs.
                datetime.strptime(f"2000-{fiscal_year_end}", "%Y-%m-%d")
            except ValueError:
                errors.append(f"{label}.fiscal_year_end is not a valid month/day")
        tax_pack_id = entity.get("tax_pack")
        if not isinstance(tax_pack_id, str) or not tax_pack_id:
            errors.append(f"{label}.tax_pack is required")

    selected_connector_ids = {
        item for item in (config.get("connectors") or []) if isinstance(item, str)
    }
    connector_bindings = config.get("connector_bindings")
    if connector_bindings is None:
        unsafe_implicit = sorted(
            selected_connector_ids & SINGLE_CREDENTIAL_CONNECTOR_PACKS
        )
        if len(seen_entity_ids) > 1 and unsafe_implicit:
            errors.append(
                "multi-entity Box must declare complete connector_bindings for single-credential "
                "Connector Packs: " + ", ".join(unsafe_implicit)
            )
    elif not isinstance(connector_bindings, list) or not connector_bindings:
        errors.append("connector_bindings must be a non-empty complete list when provided")
    else:
        seen_connector_bindings: set[str] = set()
        for index, binding in enumerate(connector_bindings):
            label = f"connector_bindings[{index}]"
            if not isinstance(binding, dict) or set(binding) != {
                "connector_pack", "entity_ids",
            }:
                errors.append(f"{label} must contain only connector_pack and entity_ids")
                continue
            connector_pack = binding.get("connector_pack")
            if not isinstance(connector_pack, str) or not connector_pack.startswith("connector."):
                errors.append(f"{label}.connector_pack must reference a connector Pack")
                continue
            if connector_pack in seen_connector_bindings:
                errors.append(f"duplicate connector binding: {connector_pack}")
            seen_connector_bindings.add(connector_pack)
            if connector_pack not in selected_connector_ids:
                errors.append(f"{label}.connector_pack is not selected by connectors")
            bound_entity_ids = binding.get("entity_ids")
            if (
                not isinstance(bound_entity_ids, list)
                or not bound_entity_ids
                or any(not isinstance(item, str) or not item for item in bound_entity_ids)
                or len(set(bound_entity_ids)) != len(bound_entity_ids)
            ):
                errors.append(f"{label}.entity_ids must be a non-empty unique list")
                continue
            unknown_entity_ids = sorted(set(bound_entity_ids) - seen_entity_ids)
            if unknown_entity_ids:
                errors.append(
                    f"{label}.entity_ids contains unknown legal entities: "
                    + ", ".join(unknown_entity_ids)
                )
            if (
                connector_pack in SINGLE_CREDENTIAL_CONNECTOR_PACKS
                and len(bound_entity_ids) != 1
            ):
                errors.append(
                    f"{label} must bind {connector_pack} to exactly one entity because its "
                    "current provider uses one runtime credential"
                )
        missing_bindings = sorted(selected_connector_ids - seen_connector_bindings)
        extra_bindings = sorted(seen_connector_bindings - selected_connector_ids)
        if missing_bindings:
            errors.append(
                "connector_bindings must cover every selected Connector Pack; missing: "
                + ", ".join(missing_bindings)
            )
        if extra_bindings:
            errors.append(
                "connector_bindings contains unselected Connector Packs: "
                + ", ".join(extra_bindings)
            )

    selected_references = _pack_references(config)
    selected_ids = {pack_id for pack_id, _ in selected_references}
    for pack_id, expected_kind in selected_references:
        if pack_id not in catalog:
            errors.append(f"unknown {expected_kind} pack: {pack_id}")
            continue
        pack = catalog.get(pack_id)
        if pack.kind != expected_kind:
            errors.append(f"pack {pack_id} has kind {pack.kind}, expected {expected_kind}")

    for pack_id in sorted(selected_ids):
        if pack_id not in catalog:
            continue
        pack = catalog.get(pack_id)
        for dependency in pack.requires:
            if dependency not in selected_ids:
                errors.append(f"pack {pack_id} requires {dependency}")
        for conflict in pack.conflicts:
            if conflict in selected_ids:
                errors.append(f"pack {pack_id} conflicts with {conflict}")

    for index, entity in enumerate(entities):
        if not isinstance(entity, dict):
            continue
        tax_pack_id = entity.get("tax_pack")
        if not isinstance(tax_pack_id, str) or tax_pack_id not in catalog:
            continue
        tax_pack = catalog.get(tax_pack_id)
        if tax_pack.kind != "jurisdiction" or not tax_pack.jurisdiction:
            continue
        expected = str(entity.get("jurisdiction") or "")
        actual = str(tax_pack.jurisdiction.get("code") or "")
        if expected != actual:
            errors.append(
                f"entities[{index}] jurisdiction {expected} does not match tax pack {tax_pack_id} ({actual})"
            )
    return errors


def resolve_box(config: dict[str, Any], catalog: PackCatalog) -> dict[str, Any]:
    errors = validate_box_config(config, catalog)
    if errors:
        raise BoxConfigError("; ".join(errors))

    selected_ids = sorted({pack_id for pack_id, _ in _pack_references(config)})
    selected = [catalog.get(pack_id) for pack_id in selected_ids]
    capabilities = sorted({capability for pack in selected for capability in pack.capabilities})
    review_gates = sorted({gate for pack in selected for gate in pack.manual_review_gates})
    warnings = []
    for pack in selected:
        if pack.status != "stable":
            warnings.append(f"{pack.pack_id} is {pack.status}")
        if pack.kind == "jurisdiction" and pack.jurisdiction:
            readiness = pack.jurisdiction["tax_readiness"]
            if readiness != "filing_assist":
                warnings.append(
                    f"{pack.pack_id} tax readiness is {readiness}; it must not be presented as filing-ready"
                )

    entities = []
    for entity in config["entities"]:
        tax_pack = catalog.get(entity["tax_pack"])
        entities.append({
            "id": entity["id"],
            "name": entity["name"],
            "jurisdiction": entity["jurisdiction"],
            "functional_currency": entity["functional_currency"],
            "accounting_basis": entity["accounting_basis"],
            "fiscal_year_end": entity["fiscal_year_end"],
            "tax_registrations": list(entity["tax_registrations"]),
            "tax_pack": tax_pack.pack_id,
            "tax_readiness": tax_pack.jurisdiction["tax_readiness"] if tax_pack.jurisdiction else None,
            "tax_rules_effective_at": tax_pack.jurisdiction["rules_effective_at"] if tax_pack.jurisdiction else None,
            "tax_rules_verified_at": tax_pack.rules.get("verified_at") if tax_pack.rules else None,
            "tax_rule_count": len(tax_pack.rules.get("rules", [])) if tax_pack.rules else 0,
            "tax_source_count": len(tax_pack.rules.get("sources", [])) if tax_pack.rules else 0,
        })

    all_entity_ids = sorted(entity["id"] for entity in entities)
    raw_connector_bindings = config.get("connector_bindings")
    connector_bindings = (
        sorted(
            ({
                "connector_pack": item["connector_pack"],
                "entity_ids": sorted(item["entity_ids"]),
            } for item in raw_connector_bindings),
            key=lambda item: item["connector_pack"],
        )
        if isinstance(raw_connector_bindings, list)
        else [{
            "connector_pack": pack_id,
            "entity_ids": list(all_entity_ids),
        } for pack_id in sorted(config.get("connectors") or [])]
    )

    return {
        "box_version": config["box_version"],
        "name": config["name"],
        "reporting_currency": config.get("reporting_currency"),
        "data_mode": config.get("data_mode", "live"),
        "packs": [{
            "id": pack.pack_id,
            "kind": pack.kind,
            "display_name": pack.display_name,
            "version": pack.version,
            "status": pack.status,
        } for pack in selected],
        "entities": entities,
        "connector_binding_mode": (
            "explicit" if isinstance(raw_connector_bindings, list)
            else "implicit_all_entities"
        ),
        "connector_bindings": connector_bindings,
        "capabilities": capabilities,
        "manual_review_gates": review_gates,
        "warnings": warnings,
        "guardrails": [
            "Amounts, reconciliations, postings and tax calculations must use deterministic code.",
            "Every conclusion must retain source evidence, rules version and human decisions.",
            "Tax packs prepare and check work; filing requires the configured authorized reviewer.",
            "Legal-entity books remain separate even when management reporting is consolidated.",
            "Connector Packs may execute only for the legal entities in their resolved binding.",
        ],
    }


def resolve_box_file(config_path: str | Path, packs_root: str | Path) -> dict[str, Any]:
    return resolve_box(load_box_config(config_path), load_pack_catalog(packs_root))
