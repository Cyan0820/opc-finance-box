from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any, Iterable

from .box_builder import build_box_candidate_bundle, build_box_starter_catalog
from .box_config import SINGLE_CREDENTIAL_CONNECTOR_PACKS, load_pack_catalog
from .box_scaffold import (
    CONNECTOR_ALIASES,
    INTEGRATION_ALIASES,
    INTEGRATION_PRESETS,
)
from .handoff_unpack import (
    BoxHandoffUnpackError,
    _new_destination,
    _unpack_box_candidate_body,
    _validate_actor,
)


class StarterWorkspaceError(ValueError):
    """Raised when an installed Starter cannot initialize a safe Box workspace."""


PROFILE_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
COUNTRY_PATTERN = re.compile(r"^[A-Z]{2}$")
ENTITY_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_-]*$")
CURRENCY_PATTERN = re.compile(r"^[A-Z]{3}$")


def _visible(value: str | None, field: str, *, maximum: int = 200) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise StarterWorkspaceError(f"{field} must be a string")
    normalized = value.strip()
    if (
        not normalized
        or normalized != value
        or len(normalized) > maximum
        or any(ord(character) < 32 for character in normalized)
    ):
        raise StarterWorkspaceError(
            f"{field} must be 1-{maximum} visible characters without padding"
        )
    return normalized


def _normalize_integrations(
    values: Iterable[str],
    *,
    allowed: set[str],
) -> list[str]:
    normalized: list[str] = []
    for raw in values:
        if not isinstance(raw, str) or not raw.strip() or raw.strip() != raw:
            raise StarterWorkspaceError(
                "integration selections must be non-empty strings without padding"
            )
        value = raw.lower()
        preset_id = INTEGRATION_ALIASES.get(value, value)
        if preset_id not in allowed:
            available = ", ".join(sorted(allowed)) or "none"
            raise StarterWorkspaceError(
                f"integration {raw} is not allowed for this profile; available: {available}"
            )
        if preset_id in normalized:
            raise StarterWorkspaceError(f"integration selection is duplicated: {preset_id}")
        normalized.append(preset_id)
    return normalized


def _select_starter(
    profile: str,
    country: str,
    packs_root: str | Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(profile, str) or PROFILE_PATTERN.fullmatch(profile) is None:
        raise StarterWorkspaceError("profile must be an installed Starter profile id")
    if not isinstance(country, str):
        raise StarterWorkspaceError("country must be a two-letter installed tax country")
    country_code = country.upper()
    if COUNTRY_PATTERN.fullmatch(country_code) is None:
        raise StarterWorkspaceError("country must be a two-letter installed tax country")
    catalog = load_pack_catalog(Path(packs_root))
    starters = build_box_starter_catalog(catalog)
    starter_id = f"{profile}.{country_code.lower()}"
    matches = [item for item in starters["entries"] if item["id"] == starter_id]
    if len(matches) != 1:
        profiles = sorted({item["profile_id"] for item in starters["entries"]})
        countries = sorted({item["country_code"] for item in starters["entries"]})
        raise StarterWorkspaceError(
            f"installed Starter is unavailable: {starter_id}; profiles: "
            f"{', '.join(profiles) or 'none'}; countries: {', '.join(countries) or 'none'}"
        )
    return matches[0], starters


def _materialize_starter_spec(
    *,
    spec: dict[str, Any],
    packs_root: str | Path,
    destination: Path,
    actor: str,
) -> tuple[dict[str, Any], dict[str, Any], str, int]:
    try:
        body, _, manifest = build_box_candidate_bundle(spec, packs_root)
        installed = _unpack_box_candidate_body(
            body,
            packs_root,
            destination,
            actor=actor,
            source_bundle_retained=False,
        )
    except (BoxHandoffUnpackError, OSError, ValueError) as exc:
        raise StarterWorkspaceError(str(exc)) from exc
    spec_body = json.dumps(
        spec, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    compiled_file_count = sum(
        1 for record in manifest["files"]
        if str(record.get("path") or "").startswith("compiled/")
    )
    return installed, manifest, hashlib.sha256(spec_body).hexdigest(), compiled_file_count


def _entity_selections(values: Iterable[str], profile: str) -> list[dict[str, str]]:
    selections: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for raw in values:
        if not isinstance(raw, str) or not raw or raw.strip() != raw:
            raise StarterWorkspaceError(
                "entity selections must use COUNTRY or COUNTRY=entity_id without padding"
            )
        country_raw, separator, custom_id = raw.partition("=")
        country = country_raw.upper()
        if COUNTRY_PATTERN.fullmatch(country) is None:
            raise StarterWorkspaceError(
                "entity selections must use COUNTRY or COUNTRY=entity_id"
            )
        entity_id = custom_id if separator else f"{country.lower()}_{profile}_company"
        if (
            not entity_id
            or ENTITY_ID_PATTERN.fullmatch(entity_id) is None
            or len(entity_id) > 128
        ):
            raise StarterWorkspaceError(
                "entity selector id must start with a lowercase letter and contain only "
                "lowercase letters, digits, underscores or hyphens"
            )
        if entity_id in seen_ids:
            raise StarterWorkspaceError(f"entity selector id is duplicated: {entity_id}")
        seen_ids.add(entity_id)
        selections.append({"country_code": country, "entity_id": entity_id})
    if len(selections) < 2:
        raise StarterWorkspaceError("starter-compose requires at least two legal entities")
    if len(selections) > 20:
        raise StarterWorkspaceError("starter-compose supports at most 20 legal entities")
    return selections


def _entity_name_overrides(
    values: Iterable[str], *, entity_ids: set[str],
) -> dict[str, str]:
    overrides: dict[str, str] = {}
    for raw in values:
        if not isinstance(raw, str) or raw.strip() != raw:
            raise StarterWorkspaceError(
                "entity name overrides must use entity_id=visible name without padding"
            )
        entity_id, separator, raw_name = raw.partition("=")
        if not separator or entity_id not in entity_ids:
            raise StarterWorkspaceError(
                "entity name override must reference one selected entity_id"
            )
        if entity_id in overrides:
            raise StarterWorkspaceError(
                f"entity name override is duplicated: {entity_id}"
            )
        overrides[entity_id] = _visible(raw_name, "entity_name") or ""
    return overrides


def _entity_integration_selections(
    values: Iterable[str], *, entity_ids: set[str], allowed: set[str],
) -> list[dict[str, str]]:
    selections: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for raw in values:
        if not isinstance(raw, str) or raw.strip() != raw:
            raise StarterWorkspaceError(
                "entity integration selections must use entity_id=integration without padding"
            )
        entity_id, separator, integration = raw.partition("=")
        if not separator or entity_id not in entity_ids:
            raise StarterWorkspaceError(
                "entity integration must reference one selected entity_id"
            )
        preset_id = _normalize_integrations([integration], allowed=allowed)[0]
        scope = (entity_id, preset_id)
        if scope in seen:
            raise StarterWorkspaceError(
                f"entity integration selection is duplicated: {entity_id}={preset_id}"
            )
        seen.add(scope)
        selections.append({"entity_id": entity_id, "integration": preset_id})
    return selections


def _reporting_currency(value: str | None, functional_currencies: set[str]) -> tuple[str, bool]:
    if value is None:
        if len(functional_currencies) != 1:
            raise StarterWorkspaceError(
                "reporting_currency is required when legal entities use multiple currencies"
            )
        return next(iter(functional_currencies)), False
    if not isinstance(value, str) or value.strip() != value:
        raise StarterWorkspaceError("reporting_currency must be a 3-letter currency code")
    normalized = value.upper()
    if CURRENCY_PATTERN.fullmatch(normalized) is None:
        raise StarterWorkspaceError("reporting_currency must be a 3-letter currency code")
    return normalized, True


def initialize_box_starter_workspace(
    *,
    profile: str,
    country: str,
    packs_root: str | Path,
    destination_root: str | Path,
    actor: str,
    integrations: Iterable[str] = (),
    name: str | None = None,
    entity_id: str | None = None,
    entity_name: str | None = None,
    data_mode: str = "demo",
) -> dict[str, Any]:
    """Select an installed product/country Starter and materialize a standard Box workspace."""
    try:
        normalized_actor = _validate_actor(actor)
        destination = _new_destination(destination_root)
    except BoxHandoffUnpackError as exc:
        raise StarterWorkspaceError(str(exc)) from exc
    starter, catalog_summary = _select_starter(profile, country, packs_root)
    if data_mode not in {"demo", "live"}:
        raise StarterWorkspaceError("data_mode must be demo or live")
    selected_integrations = _normalize_integrations(
        integrations,
        allowed=set(starter["allowed_integrations"]),
    )
    custom_name = _visible(name, "name")
    custom_entity_name = _visible(entity_name, "entity_name")
    if entity_id is not None and (
        not isinstance(entity_id, str)
        or ENTITY_ID_PATTERN.fullmatch(entity_id) is None
        or len(entity_id) > 128
    ):
        raise StarterWorkspaceError(
            "entity_id must start with a lowercase letter and contain only lowercase letters, "
            "digits, underscores or hyphens"
        )
    spec = deepcopy(starter["starter_spec"])
    spec["integrations"] = selected_integrations
    spec["data_mode"] = data_mode
    if custom_name is not None:
        spec["name"] = custom_name
    if entity_id is not None:
        spec["entities"][0]["id"] = entity_id
    if custom_entity_name is not None:
        spec["entities"][0]["name"] = custom_entity_name
    installed, manifest, spec_sha256, compiled_file_count = _materialize_starter_spec(
        spec=spec,
        packs_root=packs_root,
        destination=destination,
        actor=normalized_actor,
    )
    return {
        "schema_version": 1,
        "initialized": True,
        "starter_id": starter["id"],
        "profile_id": starter["profile_id"],
        "country_code": starter["country_code"],
        "jurisdiction_id": starter["jurisdiction_id"],
        "tax_readiness": starter["tax_readiness"],
        "selected_integrations": selected_integrations,
        "starter_catalog_spec_sha256": starter["starter_spec_sha256"],
        "initialized_spec_sha256": spec_sha256,
        "runtime_fingerprint": manifest["runtime_fingerprint"],
        "compiled_file_count": compiled_file_count,
        "workspace_file_count": installed["installed_file_count"],
        "workspace_directory_count": installed["directory_count"],
        "workspace_receipt_sha256": installed["receipt_sha256"],
        "workspace_verified": installed["installed_tree_verified"],
        "source_bundle_materialized": False,
        "source_bundle_retained": False,
        "profile_count": catalog_summary["profile_count"],
        "jurisdiction_count": catalog_summary["jurisdiction_count"],
        "available_starter_count": catalog_summary["ready_combination_count"],
        "requires_local_confirmation": True,
        "tax_registrations_default_to_empty": True,
        "filing_ready": False,
        "credentials_persisted": False,
        "financial_values_added": False,
        "archive_members_executed": False,
        "destination_path_returned": False,
        "actor_returned": False,
        "external_actions_performed": False,
        "active_runtime_changed": False,
    }


def initialize_multi_entity_starter_workspace(
    *,
    profile: str,
    entities: Iterable[str],
    packs_root: str | Path,
    destination_root: str | Path,
    actor: str,
    integrations: Iterable[str] = (),
    entity_integrations: Iterable[str] = (),
    entity_names: Iterable[str] = (),
    reporting_currency: str | None = None,
    name: str | None = None,
    data_mode: str = "demo",
) -> dict[str, Any]:
    """Compose 2-20 installed same-profile Starters into one verified Box workspace."""
    try:
        normalized_actor = _validate_actor(actor)
        destination = _new_destination(destination_root)
    except BoxHandoffUnpackError as exc:
        raise StarterWorkspaceError(str(exc)) from exc
    if not isinstance(profile, str) or PROFILE_PATTERN.fullmatch(profile) is None:
        raise StarterWorkspaceError("profile must be an installed Starter profile id")
    selections = _entity_selections(entities, profile)
    if data_mode not in {"demo", "live"}:
        raise StarterWorkspaceError("data_mode must be demo or live")

    selected_starters: list[dict[str, Any]] = []
    catalog_summary: dict[str, Any] | None = None
    for selection in selections:
        starter, current_catalog = _select_starter(
            profile, selection["country_code"], packs_root,
        )
        selected_starters.append(starter)
        catalog_summary = current_catalog
    if catalog_summary is None:
        raise StarterWorkspaceError("starter-compose could not resolve an installed catalog")
    allowed_integrations = set.intersection(
        *(set(starter["allowed_integrations"]) for starter in selected_starters)
    )
    selected_global_integrations = _normalize_integrations(
        integrations, allowed=allowed_integrations,
    )
    entity_ids = {selection["entity_id"] for selection in selections}
    selected_entity_integrations = _entity_integration_selections(
        entity_integrations, entity_ids=entity_ids, allowed=allowed_integrations,
    )
    for preset_id in selected_global_integrations:
        unsafe = set(INTEGRATION_PRESETS[preset_id]["connectors"]) & (
            SINGLE_CREDENTIAL_CONNECTOR_PACKS
        )
        if unsafe:
            raise StarterWorkspaceError(
                "multi-entity global integration cannot select single-credential Connector Packs; "
                "use --entity-integration entity_id=preset for: " + ", ".join(sorted(unsafe))
            )
    redundant = sorted({
        f"{item['entity_id']}={item['integration']}"
        for item in selected_entity_integrations
        if item["integration"] in selected_global_integrations
    })
    if redundant:
        raise StarterWorkspaceError(
            "entity integration is already selected globally: " + ", ".join(redundant)
        )
    selected_integrations = list(dict.fromkeys([
        *selected_global_integrations,
        *(item["integration"] for item in selected_entity_integrations),
    ]))
    overrides = _entity_name_overrides(
        entity_names,
        entity_ids={selection["entity_id"] for selection in selections},
    )
    custom_name = _visible(name, "name")

    composed_entities: list[dict[str, Any]] = []
    entity_summaries: list[dict[str, Any]] = []
    for selection, starter in zip(selections, selected_starters, strict=True):
        entity = deepcopy(starter["starter_spec"]["entities"][0])
        entity["id"] = selection["entity_id"]
        if selection["entity_id"] in overrides:
            entity["name"] = overrides[selection["entity_id"]]
        composed_entities.append(entity)
        entity_summaries.append({
            "entity_id": entity["id"],
            "country_code": starter["country_code"],
            "jurisdiction_id": starter["jurisdiction_id"],
            "functional_currency": entity["functional_currency"],
            "tax_readiness": starter["tax_readiness"],
        })
    functional_currencies = {
        str(entity["functional_currency"]) for entity in composed_entities
    }
    normalized_reporting_currency, reporting_currency_explicit = _reporting_currency(
        reporting_currency, functional_currencies,
    )
    template = deepcopy(selected_starters[0]["starter_spec"])
    template.update({
        "name": custom_name or f"{profile} OPC Multi-entity Starter",
        "integrations": selected_integrations,
        "data_mode": data_mode,
        "reporting_currency": normalized_reporting_currency,
        "entities": composed_entities,
    })
    if selected_entity_integrations:
        all_entity_ids = sorted(entity_ids)
        connector_scopes: dict[str, set[str]] = {}
        for raw_connector in template.get("connectors") or ["file_import"]:
            connector_pack = CONNECTOR_ALIASES.get(
                str(raw_connector).lower(), str(raw_connector),
            )
            connector_scopes.setdefault(connector_pack, set()).update(all_entity_ids)
        for preset_id in selected_global_integrations:
            for connector_pack in INTEGRATION_PRESETS[preset_id]["connectors"]:
                connector_scopes.setdefault(connector_pack, set()).update(all_entity_ids)
        for selection in selected_entity_integrations:
            for connector_pack in INTEGRATION_PRESETS[selection["integration"]]["connectors"]:
                connector_scopes.setdefault(connector_pack, set()).add(selection["entity_id"])
        unsafe_multi_scope = sorted(
            connector_pack for connector_pack, scope in connector_scopes.items()
            if connector_pack in SINGLE_CREDENTIAL_CONNECTOR_PACKS and len(scope) != 1
        )
        if unsafe_multi_scope:
            raise StarterWorkspaceError(
                "single-credential Connector Pack must bind to exactly one entity: "
                + ", ".join(unsafe_multi_scope)
            )
        template["connector_bindings"] = [{
            "connector_pack": connector_pack,
            "entity_ids": sorted(scope),
        } for connector_pack, scope in sorted(connector_scopes.items())]
    installed, manifest, spec_sha256, compiled_file_count = _materialize_starter_spec(
        spec=template,
        packs_root=packs_root,
        destination=destination,
        actor=normalized_actor,
    )
    return {
        "schema_version": 1,
        "initialized": True,
        "composition_type": "same_profile_multi_entity",
        "profile_id": profile,
        "starter_ids": [starter["id"] for starter in selected_starters],
        "entity_count": len(entity_summaries),
        "entities": entity_summaries,
        "selected_integrations": selected_integrations,
        "global_integrations": selected_global_integrations,
        "entity_integrations": selected_entity_integrations,
        "connector_binding_mode": (
            "explicit" if selected_entity_integrations else "implicit_all_entities"
        ),
        "connector_bindings": deepcopy(template.get("connector_bindings") or []),
        "reporting_currency": normalized_reporting_currency,
        "reporting_currency_explicit": reporting_currency_explicit,
        "cross_currency": len(functional_currencies) > 1,
        "initialized_spec_sha256": spec_sha256,
        "runtime_fingerprint": manifest["runtime_fingerprint"],
        "compiled_file_count": compiled_file_count,
        "workspace_file_count": installed["installed_file_count"],
        "workspace_directory_count": installed["directory_count"],
        "workspace_receipt_sha256": installed["receipt_sha256"],
        "workspace_verified": installed["installed_tree_verified"],
        "source_bundle_materialized": False,
        "source_bundle_retained": False,
        "profile_count": catalog_summary["profile_count"],
        "jurisdiction_count": catalog_summary["jurisdiction_count"],
        "available_starter_count": catalog_summary["ready_combination_count"],
        "multi_entity_feature_selected": True,
        "entity_books_separate": True,
        "management_reporting_pre_elimination": True,
        "cross_currency_aggregation_authorized": False,
        "fx_rates_added": False,
        "requires_local_confirmation": True,
        "tax_registrations_default_to_empty": True,
        "filing_ready": False,
        "credentials_persisted": False,
        "financial_values_added": False,
        "archive_members_executed": False,
        "destination_path_returned": False,
        "actor_returned": False,
        "external_actions_performed": False,
        "active_runtime_changed": False,
    }
