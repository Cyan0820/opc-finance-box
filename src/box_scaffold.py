from __future__ import annotations

from copy import deepcopy
from typing import Any

from .box_config import BoxConfigError, PackCatalog, resolve_box


BUSINESS_ALIASES = {
    "game": "industry.game_studio",
    "game_studio": "industry.game_studio",
    "游戏": "industry.game_studio",
    "commerce": "industry.commerce",
    "ecommerce": "industry.commerce",
    "e-commerce": "industry.commerce",
    "dtc": "industry.commerce",
    "independent_store": "industry.commerce",
    "独立站": "industry.commerce",
    "电商": "industry.commerce",
}
CHANNEL_ALIASES = {
    "app_store": "channel.app_store",
    "google_play": "channel.google_play",
    "domestic_game": "channel.domestic_game_platforms",
    "domestic_game_platforms": "channel.domestic_game_platforms",
    "dtc": "channel.dtc_storefront",
    "dtc_storefront": "channel.dtc_storefront",
    "independent_store": "channel.dtc_storefront",
    "独立站": "channel.dtc_storefront",
    "marketplace": "channel.marketplace_commerce",
    "marketplace_commerce": "channel.marketplace_commerce",
    "第三方平台": "channel.marketplace_commerce",
}
CONNECTOR_ALIASES = {
    "file": "connector.file_import",
    "file_import": "connector.file_import",
    "shopify": "connector.shopify",
    "stripe": "connector.stripe",
    "xero": "connector.xero",
    "wise": "connector.wise",
    "airwallex": "connector.airwallex",
    "shipbob": "connector.shipbob",
    "paypal": "connector.paypal",
    "woocommerce": "connector.woocommerce",
    "amazon_seller": "connector.amazon_seller",
    "amazon": "connector.amazon_seller",
}
FEATURE_ALIASES = {
    "multi_entity": "feature.multi_entity",
    "shopify_stripe_order_to_cash": "feature.shopify_stripe_order_to_cash",
}
INTEGRATION_PRESETS = {
    "shopify": {
        "display_name": "Shopify 只读订单、交易与退款证据",
        "connectors": ["connector.shopify"],
        "features": [],
    },
    "stripe": {
        "display_name": "Stripe 余额活动、Payout 与银行到账证据",
        "connectors": ["connector.stripe"],
        "features": [],
    },
    "xero": {
        "display_name": "Xero 主体绑定只读 Trial Balance",
        "connectors": ["connector.xero"],
        "features": [],
    },
    "wise": {
        "display_name": "Wise Business 主体绑定只读余额账户流水",
        "connectors": ["connector.wise"],
        "features": [],
    },
    "airwallex": {
        "display_name": "Airwallex 企业卡已批准费用证据",
        "connectors": ["connector.airwallex"],
        "features": [],
    },
    "shipbob": {
        "display_name": "ShipBob 3PL 只读履约成本与退货处置证据",
        "connectors": ["connector.shipbob"],
        "features": [],
    },
    "paypal": {
        "display_name": "PayPal 只读交易、费用、退款与余额转出证据",
        "connectors": ["connector.paypal"],
        "features": [],
    },
    "woocommerce": {
        "display_name": "WooCommerce 只读订单、状态与退款证据",
        "connectors": ["connector.woocommerce"],
        "features": [],
    },
    "amazon_seller": {
        "display_name": "Amazon Seller SP-API 财务交易、费用与结算引用证据",
        "connectors": ["connector.amazon_seller"],
        "features": [],
    },
    "shopify_stripe": {
        "display_name": "Shopify + Stripe 完整订单到银行到账证据链",
        "connectors": ["connector.shopify", "connector.stripe"],
        "features": ["feature.shopify_stripe_order_to_cash"],
    },
    "shopify_stripe_xero": {
        "display_name": "Shopify + Stripe 订单到款 + Xero 期末试算",
        "connectors": ["connector.shopify", "connector.stripe", "connector.xero"],
        "features": ["feature.shopify_stripe_order_to_cash"],
    },
    "shopify_stripe_wise": {
        "display_name": "Shopify + Stripe 订单到款 + Wise 银行流水",
        "connectors": ["connector.shopify", "connector.stripe", "connector.wise"],
        "features": ["feature.shopify_stripe_order_to_cash"],
    },
    "shopify_stripe_wise_airwallex": {
        "display_name": "Shopify + Stripe + Wise 订单到银行 + Airwallex 企业卡费用",
        "connectors": [
            "connector.shopify", "connector.stripe", "connector.wise",
            "connector.airwallex",
        ],
        "features": ["feature.shopify_stripe_order_to_cash"],
    },
}
INTEGRATION_ALIASES = {
    "shopify+stripe": "shopify_stripe",
    "shopify-stripe": "shopify_stripe",
    "shopify_stripe_order_to_cash": "shopify_stripe",
    "shopify+stripe+xero": "shopify_stripe_xero",
    "shopify-stripe-xero": "shopify_stripe_xero",
    "shopify+stripe+wise": "shopify_stripe_wise",
    "shopify-stripe-wise": "shopify_stripe_wise",
    "shopify+stripe+wise+airwallex": "shopify_stripe_wise_airwallex",
    "shopify-stripe-wise-airwallex": "shopify_stripe_wise_airwallex",
}


class BoxScaffoldError(ValueError):
    """Raised when a simplified Box specification cannot form a valid product."""


def list_box_options(catalog: PackCatalog) -> dict[str, list[dict[str, Any]]]:
    options: dict[str, list[dict[str, Any]]] = {
        "business_models": [],
        "channels": [],
        "jurisdictions": [],
        "connectors": [],
        "features": [],
        "integration_presets": [],
    }
    key_by_kind = {
        "industry": "business_models",
        "channel": "channels",
        "jurisdiction": "jurisdictions",
        "connector": "connectors",
        "feature": "features",
    }
    for pack in catalog.all():
        key = key_by_kind.get(pack.kind)
        if not key:
            continue
        item = {
            "id": pack.pack_id,
            "display_name": pack.display_name,
            "version": pack.version,
            "status": pack.status,
            "requires": list(pack.requires),
        }
        if pack.kind == "jurisdiction":
            rules = pack.rules or {}
            item.update({
                "country_code": pack.jurisdiction["code"] if pack.jurisdiction else None,
                "tax_readiness": pack.jurisdiction["tax_readiness"] if pack.jurisdiction else None,
                "rules_effective_at": pack.jurisdiction["rules_effective_at"] if pack.jurisdiction else None,
                "rules_verified_at": rules.get("verified_at"),
                "review_policy": deepcopy(rules.get("review_policy")),
                "applicability_review_policy": deepcopy(
                    rules.get("applicability_review_policy")
                ),
            })
        options[key].append(item)
    installed = {pack.pack_id for pack in catalog.all()}
    for preset_id, preset in INTEGRATION_PRESETS.items():
        required = [*preset["connectors"], *preset["features"]]
        if set(required) <= installed:
            options["integration_presets"].append({
                "id": preset_id,
                "display_name": preset["display_name"],
                "connectors": list(preset["connectors"]),
                "features": list(preset["features"]),
            })
    return options


def _resolve_alias(value: str, aliases: dict[str, str], prefix: str) -> str:
    normalized = value.strip().lower()
    if normalized.startswith(prefix):
        return value.strip()
    try:
        return aliases[normalized]
    except KeyError as exc:
        raise BoxScaffoldError(f"Unsupported selection: {value}") from exc


def _jurisdiction_pack_by_code(catalog: PackCatalog) -> tuple[dict[str, str], set[str]]:
    mapping: dict[str, str] = {}
    duplicates: set[str] = set()
    for pack in catalog.all():
        if pack.kind != "jurisdiction" or not pack.jurisdiction:
            continue
        code = str(pack.jurisdiction["code"])
        if code in mapping:
            duplicates.add(code)
        mapping[code] = pack.pack_id
    return mapping, duplicates


def _string_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise BoxScaffoldError(f"{field} must be a list of non-empty strings")
    return [item.strip() for item in value]


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def create_box_config(spec: dict[str, Any], catalog: PackCatalog) -> dict[str, Any]:
    """Turn a human-oriented product specification into the strict Box configuration."""
    if not isinstance(spec, dict):
        raise BoxScaffoldError("spec must be a JSON object")
    name = str(spec.get("name") or "").strip()
    if not name:
        raise BoxScaffoldError("name is required")
    business_values = spec.get("business_models")
    if business_values is None:
        business_values = [spec.get("business_type")]
    if not isinstance(business_values, list) or not business_values or any(
        not isinstance(item, str) or not item for item in business_values
    ):
        raise BoxScaffoldError("business_type or business_models is required")
    business_models = _unique([
        _resolve_alias(item, BUSINESS_ALIASES, "industry.") for item in business_values
    ])

    channel_values = _string_list(spec.get("channels") or [], "channels")
    channels = _unique([
        _resolve_alias(item, CHANNEL_ALIASES, "channel.") for item in channel_values
    ])

    entity_specs = spec.get("entities")
    if not isinstance(entity_specs, list) or not entity_specs:
        raise BoxScaffoldError("at least one entity is required")
    tax_packs, duplicate_country_codes = _jurisdiction_pack_by_code(catalog)
    entities = []
    for index, raw in enumerate(entity_specs):
        if not isinstance(raw, dict):
            raise BoxScaffoldError(f"entities[{index}] must be an object")
        country = str(raw.get("tax_country") or raw.get("jurisdiction") or "").upper()
        explicit_tax_pack = raw.get("tax_pack")
        if explicit_tax_pack:
            tax_pack = str(explicit_tax_pack)
            if tax_pack not in catalog:
                raise BoxScaffoldError(f"Unknown tax pack: {tax_pack}")
            selected = catalog.get(tax_pack)
            if selected.kind != "jurisdiction" or not selected.jurisdiction:
                raise BoxScaffoldError(f"Not a jurisdiction pack: {tax_pack}")
            if country and country != selected.jurisdiction["code"]:
                raise BoxScaffoldError(f"Entity country {country} does not match {tax_pack}")
            country = selected.jurisdiction["code"]
        else:
            if country in duplicate_country_codes:
                choices = sorted(
                    pack.pack_id for pack in catalog.all()
                    if pack.kind == "jurisdiction" and pack.jurisdiction
                    and pack.jurisdiction["code"] == country
                )
                raise BoxScaffoldError(
                    f"Multiple tax packs exist for {country}; set entities[{index}].tax_pack to one of: "
                    + ", ".join(choices)
                )
            try:
                tax_pack = tax_packs[country]
            except KeyError as exc:
                supported = ", ".join(sorted(tax_packs)) or "none"
                raise BoxScaffoldError(
                    f"No installed tax pack for {country or 'missing country'}; available: {supported}"
                ) from exc
        entities.append({
            "id": raw.get("id"),
            "name": raw.get("name"),
            "jurisdiction": country,
            "functional_currency": str(raw.get("functional_currency") or "").upper(),
            "accounting_basis": raw.get("accounting_basis"),
            "fiscal_year_end": raw.get("fiscal_year_end", "12-31"),
            "tax_pack": tax_pack,
            "tax_registrations": deepcopy(raw.get("tax_registrations") or []),
        })

    currencies = {entity["functional_currency"] for entity in entities}
    reporting_currency = spec.get("reporting_currency")
    if not reporting_currency and len(currencies) == 1:
        reporting_currency = next(iter(currencies))
    if not reporting_currency and len(currencies) > 1:
        raise BoxScaffoldError("reporting_currency is required for multi-currency entities")

    connector_values = _string_list(
        spec.get("connectors", ["file_import"]), "connectors",
    )
    connectors = [_resolve_alias(value, CONNECTOR_ALIASES, "connector.") for value in connector_values]
    feature_values = _string_list(spec.get("features") or [], "features")
    features = [_resolve_alias(value, FEATURE_ALIASES, "feature.") for value in feature_values]
    integration_values = _string_list(spec.get("integrations") or [], "integrations")
    for value in integration_values:
        normalized = value.strip().lower()
        preset_id = INTEGRATION_ALIASES.get(normalized, normalized)
        try:
            preset = INTEGRATION_PRESETS[preset_id]
        except KeyError as exc:
            raise BoxScaffoldError(
                f"Unsupported integration preset: {value}; available: "
                + ", ".join(sorted(INTEGRATION_PRESETS))
            ) from exc
        connectors.extend(preset["connectors"])
        features.extend(preset["features"])
    connectors = _unique(connectors)
    features = _unique(features)
    if len(entities) > 1 and "feature.multi_entity" not in features:
        features.append("feature.multi_entity")

    connector_bindings = spec.get("connector_bindings")
    resolved_bindings: list[dict[str, Any]] | None = None
    if connector_bindings is not None:
        if not isinstance(connector_bindings, list) or not connector_bindings:
            raise BoxScaffoldError(
                "connector_bindings must be a non-empty complete list when provided"
            )
        resolved_bindings = []
        for index, binding in enumerate(connector_bindings):
            if not isinstance(binding, dict) or set(binding) != {
                "connector_pack", "entity_ids",
            }:
                raise BoxScaffoldError(
                    f"connector_bindings[{index}] must contain only connector_pack and entity_ids"
                )
            connector_pack = _resolve_alias(
                str(binding.get("connector_pack") or ""),
                CONNECTOR_ALIASES,
                "connector.",
            )
            entity_ids = _string_list(
                binding.get("entity_ids"), f"connector_bindings[{index}].entity_ids",
            )
            resolved_bindings.append({
                "connector_pack": connector_pack,
                "entity_ids": _unique(entity_ids),
            })

    config = {
        "box_version": 1,
        "name": name,
        "data_mode": spec.get("data_mode", "live"),
        "reporting_currency": str(reporting_currency).upper() if reporting_currency else None,
        "core": "core.finance",
        "business_models": business_models,
        "channels": channels,
        "connectors": connectors,
        "features": features,
        "entities": entities,
    }
    if resolved_bindings is not None:
        config["connector_bindings"] = resolved_bindings
    try:
        resolve_box(config, catalog)
    except BoxConfigError as exc:
        raise BoxScaffoldError(str(exc)) from exc
    return config
