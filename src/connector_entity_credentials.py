from __future__ import annotations

import json
import re
import urllib.parse
from typing import Any, Mapping


PAYPAL_BINDINGS_ENV = "OPC_PAYPAL_ENTITY_BINDINGS_JSON"
PAYPAL_LEGACY_CLIENT_ID_ENV = "OPC_PAYPAL_CLIENT_ID"
PAYPAL_LEGACY_CLIENT_SECRET_ENV = "OPC_PAYPAL_CLIENT_SECRET"
SHIPBOB_BINDINGS_ENV = "OPC_SHIPBOB_ENTITY_BINDINGS_JSON"
SHIPBOB_LEGACY_TOKEN_ENV = "OPC_SHIPBOB_ACCESS_TOKEN"
WOOCOMMERCE_BINDINGS_ENV = "OPC_WOOCOMMERCE_ENTITY_BINDINGS_JSON"
WOOCOMMERCE_LEGACY_SITE_ORIGIN_ENV = "OPC_WOOCOMMERCE_SITE_ORIGIN"
WOOCOMMERCE_LEGACY_CONSUMER_KEY_ENV = "OPC_WOOCOMMERCE_CONSUMER_KEY"
WOOCOMMERCE_LEGACY_CONSUMER_SECRET_ENV = "OPC_WOOCOMMERCE_CONSUMER_SECRET"
AMAZON_SELLER_BINDINGS_ENV = "OPC_AMAZON_SELLER_ENTITY_BINDINGS_JSON"
AMAZON_SELLER_LEGACY_CLIENT_ID_ENV = "OPC_AMAZON_SELLER_CLIENT_ID"
AMAZON_SELLER_LEGACY_CLIENT_SECRET_ENV = "OPC_AMAZON_SELLER_CLIENT_SECRET"
AMAZON_SELLER_LEGACY_REFRESH_TOKEN_ENV = "OPC_AMAZON_SELLER_REFRESH_TOKEN"
AMAZON_SELLER_LEGACY_REGION_ENV = "OPC_AMAZON_SELLER_REGION"
AMAZON_SELLER_LEGACY_SELLER_ID_ENV = "OPC_AMAZON_SELLER_ID"
AMAZON_SELLER_LEGACY_MARKETPLACE_IDS_ENV = (
    "OPC_AMAZON_SELLER_MARKETPLACE_IDS_JSON"
)

_ENV_ALIAS = re.compile(r"OPC_[A-Z][A-Z0-9_]{2,123}")
_PAYPAL_ACCOUNT_ID = re.compile(r"[2-9A-HJ-NP-Z]{13}")
_PAYPAL_APP_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{7,127}")
_AMAZON_IDENTIFIER = re.compile(r"[A-Z0-9]{6,32}")


class ConnectorEntityCredentialError(ValueError):
    """Raised when an entity-to-credential-reference binding is unsafe."""


def _mapping(raw: str, *, provider: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConnectorEntityCredentialError(
            f"{provider} entity credential binding must contain valid JSON"
        ) from exc
    if not isinstance(value, dict):
        raise ConnectorEntityCredentialError(
            f"{provider} entity credential binding must be a JSON object"
        )
    return value


def _alias(value: Any, *, provider: str, field: str) -> str:
    name = str(value or "")
    if not _ENV_ALIAS.fullmatch(name):
        raise ConnectorEntityCredentialError(
            f"{provider} {field} must name an OPC_ environment variable"
        )
    return name


def _value(environment: Mapping[str, str], name: str) -> str:
    return str(environment.get(name) or "").strip()


def _canonical_selected_binding(value: Mapping[str, Any]) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )


def _site_origin(value: Any) -> str:
    raw = str(value or "")
    parsed = urllib.parse.urlsplit(raw)
    hostname = (parsed.hostname or "").lower()
    try:
        configured_port = parsed.port
    except ValueError as exc:
        raise ConnectorEntityCredentialError(
            "WooCommerce site_origin contains an invalid port"
        ) from exc
    if (
        parsed.scheme != "https"
        or not hostname
        or configured_port is not None
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or not re.fullmatch(
            r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
            r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+",
            hostname,
        )
        or hostname.endswith((".local", ".internal", ".localhost"))
    ):
        raise ConnectorEntityCredentialError(
            "WooCommerce site_origin must be a public HTTPS domain"
        )
    path = parsed.path.rstrip("/")
    if path and not re.fullmatch(r"(?:/[A-Za-z0-9._~-]+)+", path):
        raise ConnectorEntityCredentialError(
            "WooCommerce site_origin path contains unsupported characters"
        )
    return urllib.parse.urlunsplit(("https", hostname, path, "", ""))


def resolve_paypal_entity_credentials(
    entity_id: str,
    environment: Mapping[str, str],
    *,
    legacy_environment: str | None = None,
    require_entity_binding: bool = False,
) -> dict[str, Any]:
    raw_bindings = _value(environment, PAYPAL_BINDINGS_ENV)
    if raw_bindings:
        bindings = _mapping(raw_bindings, provider="PayPal")
        selected = bindings.get(entity_id)
        if not isinstance(selected, dict) or set(selected) != {
            "environment", "app_id", "account_id",
            "client_id_env", "client_secret_env",
        }:
            raise ConnectorEntityCredentialError(
                "PayPal entity binding requires environment, app_id, account_id, "
                "client_id_env and client_secret_env"
            )
        provider_environment = str(selected.get("environment") or "")
        if provider_environment not in {"production", "sandbox"}:
            raise ConnectorEntityCredentialError(
                "PayPal entity binding environment must be production or sandbox"
            )
        if legacy_environment is not None and legacy_environment != provider_environment:
            raise ConnectorEntityCredentialError(
                "PayPal request environment does not match the entity credential binding"
            )
        app_id = str(selected.get("app_id") or "")
        account_id = str(selected.get("account_id") or "")
        if not _PAYPAL_APP_ID.fullmatch(app_id):
            raise ConnectorEntityCredentialError("PayPal app_id binding is invalid")
        if not _PAYPAL_ACCOUNT_ID.fullmatch(account_id):
            raise ConnectorEntityCredentialError("PayPal account_id binding is invalid")
        client_id_env = _alias(
            selected.get("client_id_env"), provider="PayPal", field="client_id_env",
        )
        client_secret_env = _alias(
            selected.get("client_secret_env"),
            provider="PayPal",
            field="client_secret_env",
        )
        if len({PAYPAL_BINDINGS_ENV, client_id_env, client_secret_env}) != 3:
            raise ConnectorEntityCredentialError(
                "PayPal credential environment aliases must be distinct"
            )
        values = {
            PAYPAL_BINDINGS_ENV: _canonical_selected_binding(selected),
            client_id_env: _value(environment, client_id_env),
            client_secret_env: _value(environment, client_secret_env),
        }
        names = (PAYPAL_BINDINGS_ENV, client_id_env, client_secret_env)
        return {
            "entity_binding_used": True,
            "entity_id": entity_id,
            "environment": provider_environment,
            "app_id": app_id,
            "account_id": account_id,
            "client_id": values[client_id_env],
            "client_secret": values[client_secret_env],
            "env_names": names,
            "fingerprint_values": values,
            "configured": all(values.values()),
        }
    if require_entity_binding:
        raise ConnectorEntityCredentialError(
            f"PayPal access requires {PAYPAL_BINDINGS_ENV} for the selected entity"
        )
    provider_environment = str(legacy_environment or "")
    if provider_environment not in {"production", "sandbox"}:
        raise ConnectorEntityCredentialError(
            "PayPal legacy credential resolution requires production or sandbox"
        )
    values = {
        PAYPAL_LEGACY_CLIENT_ID_ENV: _value(
            environment, PAYPAL_LEGACY_CLIENT_ID_ENV,
        ),
        PAYPAL_LEGACY_CLIENT_SECRET_ENV: _value(
            environment, PAYPAL_LEGACY_CLIENT_SECRET_ENV,
        ),
    }
    return {
        "entity_binding_used": False,
        "entity_id": entity_id,
        "environment": provider_environment,
        "app_id": None,
        "account_id": None,
        "client_id": values[PAYPAL_LEGACY_CLIENT_ID_ENV],
        "client_secret": values[PAYPAL_LEGACY_CLIENT_SECRET_ENV],
        "env_names": tuple(values),
        "fingerprint_values": values,
        "configured": all(values.values()),
    }


def resolve_woocommerce_entity_credentials(
    entity_id: str,
    environment: Mapping[str, str],
    *,
    require_entity_binding: bool = False,
) -> dict[str, Any]:
    raw_bindings = _value(environment, WOOCOMMERCE_BINDINGS_ENV)
    if raw_bindings:
        bindings = _mapping(raw_bindings, provider="WooCommerce")
        selected = bindings.get(entity_id)
        if not isinstance(selected, dict) or set(selected) != {
            "site_origin", "key_permission", "consumer_key_env",
            "consumer_secret_env",
        }:
            raise ConnectorEntityCredentialError(
                "WooCommerce entity binding requires site_origin, key_permission, "
                "consumer_key_env and consumer_secret_env"
            )
        site_origin = _site_origin(selected.get("site_origin"))
        if selected.get("key_permission") != "read":
            raise ConnectorEntityCredentialError(
                "WooCommerce entity binding key_permission must be read"
            )
        consumer_key_env = _alias(
            selected.get("consumer_key_env"),
            provider="WooCommerce",
            field="consumer_key_env",
        )
        consumer_secret_env = _alias(
            selected.get("consumer_secret_env"),
            provider="WooCommerce",
            field="consumer_secret_env",
        )
        if len({
            WOOCOMMERCE_BINDINGS_ENV, consumer_key_env, consumer_secret_env,
        }) != 3:
            raise ConnectorEntityCredentialError(
                "WooCommerce credential environment aliases must be distinct"
            )
        canonical_binding = dict(selected)
        canonical_binding["site_origin"] = site_origin
        values = {
            WOOCOMMERCE_BINDINGS_ENV: _canonical_selected_binding(canonical_binding),
            consumer_key_env: _value(environment, consumer_key_env),
            consumer_secret_env: _value(environment, consumer_secret_env),
        }
        names = (
            WOOCOMMERCE_BINDINGS_ENV, consumer_key_env, consumer_secret_env,
        )
        return {
            "entity_binding_used": True,
            "entity_id": entity_id,
            "site_origin": site_origin,
            "key_permission": "read",
            "consumer_key": values[consumer_key_env],
            "consumer_secret": values[consumer_secret_env],
            "env_names": names,
            "fingerprint_values": values,
            "configured": all(values.values()),
        }
    if require_entity_binding:
        raise ConnectorEntityCredentialError(
            f"WooCommerce access requires {WOOCOMMERCE_BINDINGS_ENV} for the selected entity"
        )
    values = {
        WOOCOMMERCE_LEGACY_SITE_ORIGIN_ENV: _value(
            environment, WOOCOMMERCE_LEGACY_SITE_ORIGIN_ENV,
        ),
        WOOCOMMERCE_LEGACY_CONSUMER_KEY_ENV: _value(
            environment, WOOCOMMERCE_LEGACY_CONSUMER_KEY_ENV,
        ),
        WOOCOMMERCE_LEGACY_CONSUMER_SECRET_ENV: _value(
            environment, WOOCOMMERCE_LEGACY_CONSUMER_SECRET_ENV,
        ),
    }
    site_origin = (
        _site_origin(values[WOOCOMMERCE_LEGACY_SITE_ORIGIN_ENV])
        if values[WOOCOMMERCE_LEGACY_SITE_ORIGIN_ENV] else ""
    )
    return {
        "entity_binding_used": False,
        "entity_id": entity_id,
        "site_origin": site_origin,
        "key_permission": None,
        "consumer_key": values[WOOCOMMERCE_LEGACY_CONSUMER_KEY_ENV],
        "consumer_secret": values[WOOCOMMERCE_LEGACY_CONSUMER_SECRET_ENV],
        "env_names": tuple(values),
        "fingerprint_values": values,
        "configured": all(values.values()),
    }


def _canonical_positive_integer(value: Any, *, provider: str, field: str) -> int:
    if isinstance(value, bool):
        raise ConnectorEntityCredentialError(
            f"{provider} {field} must be a canonical positive integer"
        )
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ConnectorEntityCredentialError(
            f"{provider} {field} must be a canonical positive integer"
        ) from exc
    if parsed <= 0 or parsed > 2_147_483_647 or str(parsed) != str(value):
        raise ConnectorEntityCredentialError(
            f"{provider} {field} must be a canonical positive integer"
        )
    return parsed


def resolve_shipbob_entity_credentials(
    entity_id: str,
    environment: Mapping[str, str],
    *,
    legacy_environment: str | None = None,
    require_entity_binding: bool = False,
) -> dict[str, Any]:
    raw_bindings = _value(environment, SHIPBOB_BINDINGS_ENV)
    if raw_bindings:
        bindings = _mapping(raw_bindings, provider="ShipBob")
        selected = bindings.get(entity_id)
        if not isinstance(selected, dict) or set(selected) != {
            "environment", "channel_id", "token_env",
        }:
            raise ConnectorEntityCredentialError(
                "ShipBob entity binding requires environment, channel_id and token_env"
            )
        provider_environment = str(selected.get("environment") or "")
        if provider_environment not in {"production", "sandbox"}:
            raise ConnectorEntityCredentialError(
                "ShipBob entity binding environment must be production or sandbox"
            )
        if legacy_environment is not None and legacy_environment != provider_environment:
            raise ConnectorEntityCredentialError(
                "ShipBob request environment does not match the entity credential binding"
            )
        channel_id = _canonical_positive_integer(
            selected.get("channel_id"), provider="ShipBob", field="channel_id",
        )
        token_env = _alias(
            selected.get("token_env"), provider="ShipBob", field="token_env",
        )
        if token_env == SHIPBOB_BINDINGS_ENV:
            raise ConnectorEntityCredentialError(
                "ShipBob credential environment aliases must be distinct"
            )
        canonical_binding = dict(selected)
        canonical_binding["channel_id"] = channel_id
        values = {
            SHIPBOB_BINDINGS_ENV: _canonical_selected_binding(canonical_binding),
            token_env: _value(environment, token_env),
        }
        return {
            "entity_binding_used": True,
            "entity_id": entity_id,
            "environment": provider_environment,
            "channel_id": channel_id,
            "access_token": values[token_env],
            "env_names": (SHIPBOB_BINDINGS_ENV, token_env),
            "fingerprint_values": values,
            "configured": all(values.values()),
        }
    if require_entity_binding:
        raise ConnectorEntityCredentialError(
            f"ShipBob access requires {SHIPBOB_BINDINGS_ENV} for the selected entity"
        )
    provider_environment = str(legacy_environment or "")
    if provider_environment not in {"production", "sandbox"}:
        raise ConnectorEntityCredentialError(
            "ShipBob legacy credential resolution requires production or sandbox"
        )
    values = {
        SHIPBOB_LEGACY_TOKEN_ENV: _value(environment, SHIPBOB_LEGACY_TOKEN_ENV),
    }
    return {
        "entity_binding_used": False,
        "entity_id": entity_id,
        "environment": provider_environment,
        "channel_id": None,
        "access_token": values[SHIPBOB_LEGACY_TOKEN_ENV],
        "env_names": tuple(values),
        "fingerprint_values": values,
        "configured": all(values.values()),
    }


def _amazon_marketplace_ids(value: Any) -> list[str]:
    if (
        not isinstance(value, list)
        or not value
        or len(value) > 100
        or any(
            not isinstance(item, str) or not _AMAZON_IDENTIFIER.fullmatch(item)
            for item in value
        )
        or len(value) != len(set(value))
    ):
        raise ConnectorEntityCredentialError(
            "Amazon Seller marketplace_ids must contain unique provider identifiers"
        )
    return sorted(value)


def resolve_amazon_seller_entity_credentials(
    entity_id: str,
    environment: Mapping[str, str],
    *,
    legacy_environment: str | None = None,
    require_entity_binding: bool = False,
) -> dict[str, Any]:
    raw_bindings = _value(environment, AMAZON_SELLER_BINDINGS_ENV)
    if raw_bindings:
        bindings = _mapping(raw_bindings, provider="Amazon Seller")
        selected = bindings.get(entity_id)
        if not isinstance(selected, dict) or set(selected) != {
            "environment", "region", "seller_id", "marketplace_ids",
            "client_id_env", "client_secret_env", "refresh_token_env",
        }:
            raise ConnectorEntityCredentialError(
                "Amazon Seller entity binding requires environment, region, seller_id, "
                "marketplace_ids, client_id_env, client_secret_env and refresh_token_env"
            )
        provider_environment = str(selected.get("environment") or "")
        if provider_environment not in {"production", "sandbox"}:
            raise ConnectorEntityCredentialError(
                "Amazon Seller entity binding environment must be production or sandbox"
            )
        if legacy_environment is not None and legacy_environment != provider_environment:
            raise ConnectorEntityCredentialError(
                "Amazon Seller request environment does not match the entity credential binding"
            )
        region = str(selected.get("region") or "")
        if region not in {"NA", "EU", "FE"}:
            raise ConnectorEntityCredentialError(
                "Amazon Seller entity binding region must be NA, EU or FE"
            )
        seller_id = str(selected.get("seller_id") or "")
        if not _AMAZON_IDENTIFIER.fullmatch(seller_id):
            raise ConnectorEntityCredentialError(
                "Amazon Seller seller_id binding is invalid"
            )
        marketplace_ids = _amazon_marketplace_ids(selected.get("marketplace_ids"))
        aliases = (
            _alias(
                selected.get("client_id_env"),
                provider="Amazon Seller", field="client_id_env",
            ),
            _alias(
                selected.get("client_secret_env"),
                provider="Amazon Seller", field="client_secret_env",
            ),
            _alias(
                selected.get("refresh_token_env"),
                provider="Amazon Seller", field="refresh_token_env",
            ),
        )
        if len({AMAZON_SELLER_BINDINGS_ENV, *aliases}) != 4:
            raise ConnectorEntityCredentialError(
                "Amazon Seller credential environment aliases must be distinct"
            )
        canonical_binding = dict(selected)
        canonical_binding["marketplace_ids"] = marketplace_ids
        values = {
            AMAZON_SELLER_BINDINGS_ENV: _canonical_selected_binding(canonical_binding),
            **{name: _value(environment, name) for name in aliases},
        }
        return {
            "entity_binding_used": True,
            "entity_id": entity_id,
            "environment": provider_environment,
            "region": region,
            "seller_id": seller_id,
            "marketplace_ids": marketplace_ids,
            "client_id": values[aliases[0]],
            "client_secret": values[aliases[1]],
            "refresh_token": values[aliases[2]],
            "env_names": (AMAZON_SELLER_BINDINGS_ENV, *aliases),
            "fingerprint_values": values,
            "configured": all(values.values()),
        }
    if require_entity_binding:
        raise ConnectorEntityCredentialError(
            f"Amazon Seller access requires {AMAZON_SELLER_BINDINGS_ENV} for the selected entity"
        )
    provider_environment = str(legacy_environment or "")
    if provider_environment not in {"production", "sandbox"}:
        raise ConnectorEntityCredentialError(
            "Amazon Seller legacy credential resolution requires production or sandbox"
        )
    raw_marketplaces = _value(
        environment, AMAZON_SELLER_LEGACY_MARKETPLACE_IDS_ENV,
    )
    try:
        marketplace_ids = _amazon_marketplace_ids(json.loads(raw_marketplaces))
    except (json.JSONDecodeError, ConnectorEntityCredentialError):
        marketplace_ids = []
    values = {
        name: _value(environment, name)
        for name in (
            AMAZON_SELLER_LEGACY_CLIENT_ID_ENV,
            AMAZON_SELLER_LEGACY_CLIENT_SECRET_ENV,
            AMAZON_SELLER_LEGACY_REFRESH_TOKEN_ENV,
            AMAZON_SELLER_LEGACY_REGION_ENV,
            AMAZON_SELLER_LEGACY_SELLER_ID_ENV,
            AMAZON_SELLER_LEGACY_MARKETPLACE_IDS_ENV,
        )
    }
    return {
        "entity_binding_used": False,
        "entity_id": entity_id,
        "environment": provider_environment,
        "region": values[AMAZON_SELLER_LEGACY_REGION_ENV].upper(),
        "seller_id": values[AMAZON_SELLER_LEGACY_SELLER_ID_ENV],
        "marketplace_ids": marketplace_ids,
        "client_id": values[AMAZON_SELLER_LEGACY_CLIENT_ID_ENV],
        "client_secret": values[AMAZON_SELLER_LEGACY_CLIENT_SECRET_ENV],
        "refresh_token": values[AMAZON_SELLER_LEGACY_REFRESH_TOKEN_ENV],
        "env_names": tuple(values),
        "fingerprint_values": values,
        "configured": (
            all(values.values())
            and values[AMAZON_SELLER_LEGACY_REGION_ENV].upper() in {"NA", "EU", "FE"}
            and bool(marketplace_ids)
            and _AMAZON_IDENTIFIER.fullmatch(
                values[AMAZON_SELLER_LEGACY_SELLER_ID_ENV]
            ) is not None
        ),
    }


def access_credential_group(
    pack_id: str,
    entity_id: str,
    environment: Mapping[str, str],
) -> dict[str, Any]:
    if pack_id == "connector.paypal":
        return resolve_paypal_entity_credentials(
            entity_id, environment, require_entity_binding=True,
        )
    if pack_id == "connector.woocommerce":
        return resolve_woocommerce_entity_credentials(
            entity_id, environment, require_entity_binding=True,
        )
    if pack_id == "connector.shipbob":
        return resolve_shipbob_entity_credentials(
            entity_id, environment, require_entity_binding=True,
        )
    if pack_id == "connector.amazon_seller":
        return resolve_amazon_seller_entity_credentials(
            entity_id, environment, require_entity_binding=True,
        )
    raise ConnectorEntityCredentialError(
        "Connector does not use an entity credential alias binding"
    )


def access_credentials_configured(
    pack_id: str,
    entity_id: str,
    environment: Mapping[str, str],
) -> bool:
    try:
        return bool(access_credential_group(pack_id, entity_id, environment)["configured"])
    except ConnectorEntityCredentialError:
        return False
