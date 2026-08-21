from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import stat
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from .box_runtime import BoxRuntime, BoxRuntimeError
from .connector_http import (
    ConnectorHttpError,
    HttpRequest,
    HttpResponse,
    HttpTransport,
    urllib_transport,
)
from .connector_entity_credentials import (
    AMAZON_SELLER_BINDINGS_ENV,
    ConnectorEntityCredentialError,
    PAYPAL_BINDINGS_ENV,
    SHIPBOB_BINDINGS_ENV,
    WOOCOMMERCE_BINDINGS_ENV,
    access_credential_group,
)


MAX_REQUEST_BYTES = 64 * 1024
MAX_RECEIPT_BYTES = 256 * 1024
MAX_RESPONSE_BYTES = 256 * 1024
DEFAULT_RECEIPT_MAXIMUM_AGE_DAYS = 30
SHOPIFY_API_VERSION = "2026-07"
STRIPE_API_VERSION = "2026-06-24.dahlia"
WISE_API_VERSION = "2026Q3"
SHOPIFY_TOKEN_ENV = "OPC_SHOPIFY_ADMIN_TOKEN"
STRIPE_KEY_ENV = "OPC_STRIPE_RESTRICTED_KEY"
WISE_TOKEN_ENV = "OPC_WISE_ACCESS_TOKEN"
WISE_BINDINGS_ENV = "OPC_WISE_ENTITY_BINDINGS_JSON"
XERO_TOKEN_ENV = "OPC_XERO_ACCESS_TOKEN"
XERO_BINDINGS_ENV = "OPC_XERO_ENTITY_BINDINGS_JSON"
SHIPBOB_API_VERSION = "2026-07"
AMAZON_SELLER_ORDERS_VERSION = "2026-01-01"
CREDENTIAL_ENV_NAMES = {
    "connector.shopify": (SHOPIFY_TOKEN_ENV,),
    "connector.stripe": (STRIPE_KEY_ENV,),
    "connector.wise": (WISE_TOKEN_ENV, WISE_BINDINGS_ENV),
    "connector.xero": (XERO_TOKEN_ENV, XERO_BINDINGS_ENV),
    "connector.paypal": (PAYPAL_BINDINGS_ENV,),
    "connector.woocommerce": (WOOCOMMERCE_BINDINGS_ENV,),
    "connector.shipbob": (SHIPBOB_BINDINGS_ENV,),
    "connector.amazon_seller": (AMAZON_SELLER_BINDINGS_ENV,),
}
SUPPORTED_PACKS = frozenset(CREDENTIAL_ENV_NAMES)
EXPECTED_CHECK_IDS = {
    "connector.shopify": {
        "provider_authentication", "api_version_pinned",
        "required_orders_read_scope", "no_write_scopes",
        "least_privilege_scope_set", "provider_account_binding",
    },
    "connector.stripe": {
        "restricted_key_type", "provider_account_binding",
        "balance_transactions_read", "payouts_read",
    },
    "connector.wise": {
        "provider_authentication", "provider_account_binding",
        "business_profile_type", "functional_currency_balance",
        "access_contract_eligible", "minimal_read_only_probe",
    },
    "connector.xero": {
        "provider_authentication", "provider_account_binding",
        "organisation_read_scope", "trial_balance_read_scope",
        "functional_currency_match", "read_only_probe",
    },
    "connector.paypal": {
        "provider_authentication", "provider_account_binding",
        "required_reporting_scope", "rest_app_binding",
        "reporting_balance_read_scope", "read_only_probe",
    },
    "connector.woocommerce": {
        "provider_authentication", "provider_account_binding",
        "orders_read_scope", "refunds_read_scope",
        "operator_declared_read_only_key", "read_only_probe",
    },
    "connector.shipbob": {
        "provider_authentication", "provider_account_binding",
        "required_read_scopes", "no_write_scopes",
        "least_privilege_scope_set", "read_only_probe",
    },
    "connector.amazon_seller": {
        "provider_authentication", "provider_account_binding",
        "marketplace_participations_read", "orders_read_scope",
        "inventory_read_scope", "finances_read_scope",
        "fixed_region_endpoint", "read_only_probe",
    },
}
_SHOP_DOMAIN = re.compile(
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.myshopify\.com"
)
_STRIPE_ACCOUNT_ID = re.compile(r"acct_[A-Za-z0-9]{8,128}")
_STRIPE_RESTRICTED_KEY = re.compile(r"rk_(test|live)_[A-Za-z0-9]{8,256}")
_SCOPE = re.compile(r"[a-z][a-z0-9_]{0,127}")
_ENV_REFERENCE = re.compile(r"OPC_[A-Z][A-Z0-9_]{2,123}")
_SECRET_FIELD = re.compile(
    r"(?:token|secret|password|authorization|api[_-]?key|credential)", re.I,
)
_SECRET_VALUE = re.compile(
    r"(?:sk_(?:live|test)_|rk_(?:live|test)_|shpat_|bearer\s+)", re.I,
)

SHOPIFY_SCOPE_QUERY = """
query FinanceConnectorAccessProbe {
  currentAppInstallation {
    accessScopes { handle }
  }
}
""".strip()


class ConnectorAccessProbeError(ValueError):
    """Raised when a private Connector access probe request is unsafe."""


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _binding_fingerprint(runtime: BoxRuntime, pack_id: str, value: str) -> str:
    material = {
        "runtime_fingerprint": runtime.snapshot()["fingerprint"],
        "pack_id": pack_id,
        "provider_account": value,
    }
    return f"sha256:{_fingerprint(material)}"


def _credential_fingerprint(runtime: BoxRuntime, pack_id: str, value: str) -> str:
    material = {
        "runtime_fingerprint": runtime.snapshot()["fingerprint"],
        "pack_id": pack_id,
        "credential_value": value,
    }
    return f"sha256:{_fingerprint(material)}"


def _credential_group_fingerprint(
    runtime: BoxRuntime,
    pack_id: str,
    values: Mapping[str, str],
    *,
    entity_id: str,
) -> str:
    credential_group: dict[str, Any] = dict(values)
    if pack_id in {"connector.wise", "connector.xero"}:
        bindings_env = (
            WISE_BINDINGS_ENV if pack_id == "connector.wise" else XERO_BINDINGS_ENV
        )
        try:
            bindings = json.loads(values[bindings_env])
        except (TypeError, ValueError) as exc:
            raise ConnectorAccessProbeError(
                "Connector access credential binding is invalid or has changed"
            ) from exc
        entity_binding = bindings.get(entity_id) if isinstance(bindings, dict) else None
        if not isinstance(entity_binding, dict):
            raise ConnectorAccessProbeError(
                "Connector access credential binding is invalid or has changed"
            )
        credential_group[bindings_env] = entity_binding
    material = {
        "runtime_fingerprint": runtime.snapshot()["fingerprint"],
        "pack_id": pack_id,
        "entity_id": entity_id,
        "credential_group": credential_group,
    }
    return f"sha256:{_fingerprint(material)}"


def _credential_group(
    pack_id: str,
    environment: Mapping[str, str],
    *,
    entity_id: str,
) -> tuple[tuple[str, ...], dict[str, str], bool]:
    if pack_id in {
        "connector.paypal", "connector.woocommerce", "connector.shipbob",
        "connector.amazon_seller",
    }:
        try:
            resolved = access_credential_group(pack_id, entity_id, environment)
        except ConnectorEntityCredentialError as exc:
            binding_env = CREDENTIAL_ENV_NAMES[pack_id][0]
            if str(environment.get(binding_env) or "").strip():
                raise ConnectorAccessProbeError(str(exc)) from exc
            return (binding_env,), {binding_env: ""}, False
        return (
            tuple(resolved["env_names"]),
            dict(resolved["fingerprint_values"]),
            bool(resolved["configured"]),
        )
    names = CREDENTIAL_ENV_NAMES[pack_id]
    values = {
        name: str(environment.get(name) or "").strip()
        for name in names
    }
    return names, values, all(values.values())


def _contains_secret(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            _SECRET_FIELD.search(str(key)) is not None or _contains_secret(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_secret(item) for item in value)
    return isinstance(value, str) and _SECRET_VALUE.search(value) is not None


def _ensure_pack_entity(runtime: BoxRuntime, pack_id: str, entity_id: str) -> None:
    try:
        runtime.reload()
        runtime.require_connector_entity(pack_id, entity_id)
        if pack_id == "connector.shopify":
            runtime.require_capability("connector.shopify_orders")
        elif pack_id == "connector.stripe":
            runtime.require_capability("connector.stripe_balance_transactions")
            runtime.require_capability("connector.stripe_payouts")
        elif pack_id == "connector.wise":
            runtime.require_capability("connector.wise_balance_statement")
        elif pack_id == "connector.xero":
            runtime.require_capability("connector.xero_trial_balance")
        elif pack_id == "connector.paypal":
            runtime.require_capability("connector.paypal_transaction_activity")
        elif pack_id == "connector.woocommerce":
            runtime.require_capability("connector.woocommerce_order_refund_activity")
        elif pack_id == "connector.shipbob":
            runtime.require_capability("connector.shipbob_fulfillment_evidence")
        elif pack_id == "connector.amazon_seller":
            runtime.require_capability("connector.amazon_seller_marketplace_evidence")
        else:
            raise ConnectorAccessProbeError(
                "Connector access probe Pack is unsupported"
            )
    except BoxRuntimeError as exc:
        raise ConnectorAccessProbeError(str(exc)) from exc


def _request_template(pack_id: str, entity_id: str) -> dict[str, Any]:
    if pack_id == "connector.shopify":
        binding = {
            "mode": "store_domain",
            "shop_domain": "REPLACE_WITH_PRIVATE_STORE.myshopify.com",
        }
    elif pack_id == "connector.stripe":
        binding = {
            "mode": "own_account",
            "account_id": "acct_REPLACE_WITH_PRIVATE_ACCOUNT_ID",
        }
    elif pack_id in {
        "connector.wise", "connector.xero", "connector.paypal",
        "connector.woocommerce", "connector.shipbob", "connector.amazon_seller",
    }:
        binding = {"mode": "entity_environment_binding"}
    else:
        raise ConnectorAccessProbeError(
            "Connector access probe Pack is unsupported"
        )
    return {
        "schema_version": 1,
        "pack_id": pack_id,
        "entity_id": entity_id,
        "account_binding": binding,
    }


def _private_destination(output: str | Path) -> Path:
    raw = Path(output).expanduser()
    if not raw.is_absolute():
        raise ConnectorAccessProbeError("Connector access request output must be an absolute path")
    destination = raw.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    return destination


def _write_private_artifact(
    output: str | Path,
    value: dict[str, Any],
    *,
    artifact_label: str,
) -> Path:
    destination = _private_destination(output)
    try:
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise ConnectorAccessProbeError(f"{artifact_label} output already exists") from exc
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    except Exception:
        try:
            destination.unlink()
        except OSError:
            pass
        raise
    if os.name != "nt":
        os.chmod(destination, 0o600)
    return destination


def initialize_connector_access_request(
    runtime: BoxRuntime,
    *,
    pack_id: str,
    entity_id: str,
    output: str | Path,
) -> dict[str, Any]:
    """Write a mode-0600, intentionally incomplete provider binding request."""
    if pack_id not in SUPPORTED_PACKS:
        raise ConnectorAccessProbeError(
            "Connector access probe Pack is unsupported"
        )
    _ensure_pack_entity(runtime, pack_id, entity_id)
    request = _request_template(pack_id, entity_id)
    destination = _write_private_artifact(
        output, request, artifact_label="Connector access request",
    )
    return {
        "written": True,
        "pack_id": pack_id,
        "entity_id": entity_id,
        "template_fingerprint": _fingerprint(request),
        "template_only": True,
        "ready_for_network_probe": False,
        "operator_edits_required": (
            ["account_binding.shop_domain"]
            if pack_id == "connector.shopify" else
            ["account_binding.account_id"]
            if pack_id == "connector.stripe" else []
        ),
        "credentials_included": False,
        "provider_account_returned": False,
        "network_access_performed": False,
        "external_actions_performed": False,
    }


def read_private_connector_access_request(path: str | Path) -> dict[str, Any]:
    raw = Path(path).expanduser()
    if not raw.is_absolute():
        raise ConnectorAccessProbeError("Connector access request must use an absolute path")
    try:
        raw_metadata = raw.lstat()
    except OSError as exc:
        raise ConnectorAccessProbeError("Connector access request is unavailable") from exc
    if stat.S_ISLNK(raw_metadata.st_mode) or not stat.S_ISREG(raw_metadata.st_mode):
        raise ConnectorAccessProbeError(
            "Connector access request must be a regular non-symlink file"
        )
    source = raw.resolve()
    metadata = source.stat()
    if os.name != "nt" and stat.S_IMODE(metadata.st_mode) != 0o600:
        raise ConnectorAccessProbeError("Connector access request must use mode 0600")
    if not 0 < metadata.st_size <= MAX_REQUEST_BYTES:
        raise ConnectorAccessProbeError(
            "Connector access request must be between 1 byte and 64 KiB"
        )
    try:
        request = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConnectorAccessProbeError(
            "Connector access request must contain valid UTF-8 JSON"
        ) from exc
    if not isinstance(request, dict):
        raise ConnectorAccessProbeError("Connector access request must be a JSON object")
    return request


def validate_connector_access_request(
    runtime: BoxRuntime,
    request: dict[str, Any],
) -> dict[str, Any]:
    """Validate a private binding without returning provider account identifiers."""
    if not isinstance(request, dict) or set(request) != {
        "schema_version", "pack_id", "entity_id", "account_binding",
    }:
        raise ConnectorAccessProbeError("Connector access request fields are invalid")
    if request.get("schema_version") != 1:
        raise ConnectorAccessProbeError("Connector access request schema_version must be 1")
    pack_id = request.get("pack_id")
    entity_id = request.get("entity_id")
    if pack_id not in SUPPORTED_PACKS:
        raise ConnectorAccessProbeError(
            "Connector access probe Pack is unsupported"
        )
    if not isinstance(entity_id, str) or not entity_id:
        raise ConnectorAccessProbeError("Connector access request entity_id is required")
    if _contains_secret(request):
        raise ConnectorAccessProbeError(
            "Credentials and credential-like values are prohibited in Connector access requests"
        )
    _ensure_pack_entity(runtime, str(pack_id), entity_id)
    binding = request.get("account_binding")
    if pack_id == "connector.shopify":
        if not isinstance(binding, dict) or set(binding) != {"mode", "shop_domain"}:
            raise ConnectorAccessProbeError("Shopify account_binding fields are invalid")
        domain = str(binding.get("shop_domain") or "").lower()
        if binding.get("mode") != "store_domain" or not _SHOP_DOMAIN.fullmatch(domain):
            raise ConnectorAccessProbeError(
                "Shopify account_binding requires one store domain under myshopify.com"
            )
        provider_account = domain
        binding_mode = "store_domain"
    elif pack_id == "connector.stripe":
        if not isinstance(binding, dict) or set(binding) != {"mode", "account_id"}:
            raise ConnectorAccessProbeError("Stripe account_binding fields are invalid")
        account_id = str(binding.get("account_id") or "")
        if binding.get("mode") not in {"own_account", "connected_account"}:
            raise ConnectorAccessProbeError(
                "Stripe account_binding mode must be own_account or connected_account"
            )
        if not _STRIPE_ACCOUNT_ID.fullmatch(account_id):
            raise ConnectorAccessProbeError("Stripe account_binding.account_id is invalid")
        provider_account = account_id
        binding_mode = str(binding["mode"])
    else:
        if binding != {"mode": "entity_environment_binding"}:
            provider = {
                "connector.wise": "Wise",
                "connector.xero": "Xero",
                "connector.paypal": "PayPal",
                "connector.woocommerce": "WooCommerce",
                "connector.shipbob": "ShipBob",
                "connector.amazon_seller": "Amazon Seller",
            }[str(pack_id)]
            raise ConnectorAccessProbeError(
                f"{provider} account_binding must use entity_environment_binding"
            )
        provider_account = entity_id
        binding_mode = "entity_environment_binding"
    return {
        "valid": True,
        "pack_id": str(pack_id),
        "entity_id": entity_id,
        "binding_mode": binding_mode,
        "provider_account_fingerprint": _binding_fingerprint(
            runtime, str(pack_id), provider_account,
        ),
        "request_fingerprint": _fingerprint(request),
        "credentials_included": False,
        "provider_account_returned": False,
        "network_access_performed": False,
        "external_actions_performed": False,
    }


def verify_private_connector_access_request(
    runtime: BoxRuntime,
    path: str | Path,
) -> dict[str, Any]:
    return validate_connector_access_request(
        runtime, read_private_connector_access_request(path),
    )


def _retry_delay(headers: Mapping[str, str], attempt: int) -> float:
    value = next(
        (item for key, item in headers.items() if key.lower() == "retry-after"), None,
    )
    if value is not None:
        try:
            return min(max(float(str(value).strip()), 0.0), 30.0)
        except ValueError:
            pass
    return float(min(2 ** (attempt - 1), 4))


def _send_probe_request(
    request: HttpRequest,
    *,
    provider: str,
    transport: HttpTransport,
    sleeper: Callable[[float], None],
) -> HttpResponse:
    response: HttpResponse | None = None
    for attempt in range(1, 4):
        try:
            response = transport(request)
        except (TimeoutError, OSError, ConnectorHttpError) as exc:
            if attempt == 3:
                raise ConnectorAccessProbeError(
                    f"{provider} access probe transport failed after 3 attempts"
                ) from exc
            sleeper(_retry_delay({}, attempt))
            continue
        if response.status == 429 or 500 <= response.status <= 599:
            if attempt == 3:
                raise ConnectorAccessProbeError(
                    f"{provider} access probe returned a retryable failure after 3 attempts"
                )
            sleeper(_retry_delay(response.headers, attempt))
            continue
        return response
    raise ConnectorAccessProbeError(f"{provider} access probe did not produce a response")


def _json_object(response: HttpResponse, *, provider: str) -> dict[str, Any] | None:
    if response.status != 200:
        return None
    if len(response.body) > MAX_RESPONSE_BYTES:
        raise ConnectorAccessProbeError(f"{provider} access probe response is too large")
    try:
        payload = json.loads(response.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConnectorAccessProbeError(
            f"{provider} access probe returned invalid JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise ConnectorAccessProbeError(
            f"{provider} access probe response must be a JSON object"
        )
    return payload


def _json_array(response: HttpResponse, *, provider: str) -> list[Any] | None:
    if response.status != 200:
        return None
    if len(response.body) > MAX_RESPONSE_BYTES:
        raise ConnectorAccessProbeError(f"{provider} access probe response is too large")
    try:
        payload = json.loads(response.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConnectorAccessProbeError(
            f"{provider} access probe returned invalid JSON"
        ) from exc
    if not isinstance(payload, list):
        raise ConnectorAccessProbeError(
            f"{provider} access probe response must be a JSON array"
        )
    return payload


def _check(check_id: str, passed: bool, evidence: str) -> dict[str, Any]:
    return {
        "check_id": check_id,
        "required": True,
        "passed": bool(passed),
        "status": "passed" if passed else "blocked",
        "evidence": evidence,
    }


def _shopify_probe(
    request: dict[str, Any],
    *,
    token: str,
    transport: HttpTransport,
    sleeper: Callable[[float], None],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    domain = str(request["account_binding"]["shop_domain"]).lower()
    endpoint = f"https://{domain}/admin/api/{SHOPIFY_API_VERSION}/graphql.json"
    body = _canonical({"query": SHOPIFY_SCOPE_QUERY})
    response = _send_probe_request(
        HttpRequest(
            url=endpoint,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-Shopify-Access-Token": token,
            },
            timeout_seconds=20,
            max_response_bytes=MAX_RESPONSE_BYTES,
            method="POST",
            body=body,
        ),
        provider="Shopify",
        transport=transport,
        sleeper=sleeper,
    )
    payload = _json_object(response, provider="Shopify")
    authenticated = payload is not None and not payload.get("errors")
    response_version = next(
        (value for key, value in response.headers.items()
         if key.lower() == "x-shopify-api-version"),
        None,
    )
    version_pinned = response_version in {None, SHOPIFY_API_VERSION}
    scopes: list[str] = []
    if authenticated:
        installation = (payload.get("data") or {}).get("currentAppInstallation")
        raw_scopes = installation.get("accessScopes") if isinstance(installation, dict) else None
        if isinstance(raw_scopes, list):
            for item in raw_scopes:
                handle = item.get("handle") if isinstance(item, dict) else None
                if not isinstance(handle, str) or not _SCOPE.fullmatch(handle):
                    scopes = []
                    authenticated = False
                    break
                scopes.append(handle)
        else:
            authenticated = False
    scope_set = set(scopes)
    required_scope = "read_orders" in scope_set
    no_write_scopes = not any(scope.startswith("write_") for scope in scope_set)
    allowed_scopes = {"read_orders", "read_all_orders"}
    least_privilege = bool(scope_set) and scope_set <= allowed_scopes
    checks = [
        _check("provider_authentication", authenticated, "fixed read-only GraphQL query succeeded"),
        _check("api_version_pinned", authenticated and version_pinned, "requested API version was not changed"),
        _check("required_orders_read_scope", authenticated and required_scope, "read_orders is granted"),
        _check("no_write_scopes", authenticated and no_write_scopes, "no write_* scope is granted"),
        _check("least_privilege_scope_set", authenticated and least_privilege, "only required or historical order read scopes are granted"),
        _check("provider_account_binding", authenticated, "credential succeeded only against the privately bound store endpoint"),
    ]
    return checks, {
        "api_version": SHOPIFY_API_VERSION,
        "credential_type": "admin_access_token",
        "environment_mode": "provider_managed",
        "granted_scope_count": len(scope_set),
        "scope_set_fingerprint": (
            f"sha256:{hashlib.sha256('|'.join(sorted(scope_set)).encode()).hexdigest()}"
            if scope_set else None
        ),
        "scope_names_returned": False,
    }


def _stripe_request(
    url: str,
    *,
    key: str,
    connected_account: str | None,
) -> HttpRequest:
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {key}",
        "Stripe-Version": STRIPE_API_VERSION,
    }
    if connected_account:
        headers["Stripe-Account"] = connected_account
    return HttpRequest(
        url=url,
        headers=headers,
        timeout_seconds=20,
        max_response_bytes=MAX_RESPONSE_BYTES,
    )


def _stripe_probe(
    request: dict[str, Any],
    *,
    key: str,
    transport: HttpTransport,
    sleeper: Callable[[float], None],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    key_match = _STRIPE_RESTRICTED_KEY.fullmatch(key)
    if not key_match:
        raise ConnectorAccessProbeError(
            "Stripe access probe requires an rk_test_ or rk_live_ restricted key"
        )
    account_id = str(request["account_binding"]["account_id"])
    binding_mode = str(request["account_binding"]["mode"])
    account_endpoint = (
        "https://api.stripe.com/v1/account"
        if binding_mode == "own_account"
        else f"https://api.stripe.com/v1/accounts/{account_id}"
    )
    account_response = _send_probe_request(
        _stripe_request(
            account_endpoint,
            key=key,
            connected_account=None,
        ),
        provider="Stripe",
        transport=transport,
        sleeper=sleeper,
    )
    account_payload = _json_object(account_response, provider="Stripe")
    account_bound = bool(
        account_payload
        and account_payload.get("object") == "account"
        and account_payload.get("id") == account_id
    )
    connected_account = account_id if binding_mode == "connected_account" else None
    resource_checks = []
    for check_id, endpoint in (
        ("balance_transactions_read", "https://api.stripe.com/v1/balance_transactions?limit=1"),
        ("payouts_read", "https://api.stripe.com/v1/payouts?limit=1"),
    ):
        response = _send_probe_request(
            _stripe_request(endpoint, key=key, connected_account=connected_account),
            provider="Stripe",
            transport=transport,
            sleeper=sleeper,
        )
        payload = _json_object(response, provider="Stripe")
        passed = bool(
            payload
            and payload.get("object") == "list"
            and isinstance(payload.get("data"), list)
            and isinstance(payload.get("has_more"), bool)
        )
        resource_checks.append(_check(
            check_id,
            passed,
            "fixed list endpoint accepted limit=1 without retaining source records",
        ))
    checks = [
        _check("restricted_key_type", True, "credential uses an rk_ restricted-key prefix"),
        _check("provider_account_binding", account_bound, "private expected account returned the matching Account object"),
        *resource_checks,
    ]
    return checks, {
        "api_version": STRIPE_API_VERSION,
        "credential_type": "restricted_api_key",
        "environment_mode": str(key_match.group(1)),
        "connected_account_header_used": binding_mode == "connected_account",
    }


def _positive_integer(value: Any, *, field: str) -> int:
    if isinstance(value, bool):
        raise ConnectorAccessProbeError(f"{field} must be a positive integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ConnectorAccessProbeError(f"{field} must be a positive integer") from exc
    if parsed <= 0 or str(parsed) != str(value):
        raise ConnectorAccessProbeError(f"{field} must be a canonical positive integer")
    return parsed


def _private_json_mapping(value: str, *, provider: str) -> dict[str, Any]:
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ConnectorAccessProbeError(
            f"{provider} entity binding configuration is invalid JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise ConnectorAccessProbeError(
            f"{provider} entity binding configuration must be an object"
        )
    return payload


def _wise_probe(
    runtime: BoxRuntime,
    request: dict[str, Any],
    *,
    credentials: Mapping[str, str],
    transport: HttpTransport,
    sleeper: Callable[[float], None],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    entity_id = str(request["entity_id"])
    entity = runtime.entities.get(entity_id)
    currency = entity.functional_currency.upper()
    bindings = _private_json_mapping(
        credentials[WISE_BINDINGS_ENV], provider="Wise",
    )
    selected = bindings.get(entity_id)
    if not isinstance(selected, dict) or set(selected) != {
        "profile_id", "business_name", "access_contract", "balances",
    }:
        raise ConnectorAccessProbeError(
            "Wise entity binding requires profile_id, business_name, access_contract and balances"
        )
    profile_id = _positive_integer(selected.get("profile_id"), field="Wise profile_id")
    business_name = str(selected.get("business_name") or "").strip()
    if not business_name or len(business_name) > 200:
        raise ConnectorAccessProbeError("Wise business_name binding is invalid")
    access_contract = str(selected.get("access_contract") or "")
    eligible_contract = access_contract in {
        "personal_token_eligible", "wise_partner_approved",
    }
    personal_jurisdictions = {"US", "CA", "AU", "NZ", "SG", "MY"}
    eligible_jurisdiction = (
        access_contract == "wise_partner_approved"
        or (
            access_contract == "personal_token_eligible"
            and entity.jurisdiction.upper() in personal_jurisdictions
        )
    )
    balances = selected.get("balances")
    balance = balances.get(currency) if isinstance(balances, dict) else None
    if not isinstance(balance, dict) or set(balance) != {
        "balance_id", "account_reference_masked",
    }:
        raise ConnectorAccessProbeError(
            "Wise entity binding is missing the functional-currency balance"
        )
    balance_id = _positive_integer(balance.get("balance_id"), field="Wise balance_id")
    masked = str(balance.get("account_reference_masked") or "").strip()
    if (
        not masked or len(masked) > 80
        or re.search(r"(?<!\d)\d{9,}(?!\d)", masked)
    ):
        raise ConnectorAccessProbeError(
            "Wise account_reference_masked must not expose a full account number"
        )
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {credentials[WISE_TOKEN_ENV]}",
    }
    responses = []
    for endpoint in (
        f"https://api.wise.com/{WISE_API_VERSION}/profiles/{profile_id}",
        f"https://api.wise.com/{WISE_API_VERSION}/profiles/{profile_id}/balances/{balance_id}",
    ):
        responses.append(_json_object(_send_probe_request(
            HttpRequest(
                url=endpoint,
                headers=headers,
                timeout_seconds=20,
                max_response_bytes=MAX_RESPONSE_BYTES,
            ),
            provider="Wise",
            transport=transport,
            sleeper=sleeper,
        ), provider="Wise"))
    profile_payload, balance_payload = responses
    authenticated = profile_payload is not None and balance_payload is not None
    profile_bound = bool(
        authenticated
        and profile_payload.get("id") == profile_id
        and str(profile_payload.get("type") or "").upper() == "BUSINESS"
        and str(profile_payload.get("businessName") or "").strip() == business_name
    )
    balance_bound = bool(
        authenticated
        and balance_payload.get("id") == balance_id
        and str(balance_payload.get("currency") or "").upper() == currency
        and str(balance_payload.get("type") or "").upper() in {"STANDARD", "SAVINGS"}
    )
    checks = [
        _check("provider_authentication", authenticated, "two fixed Wise Business read endpoints succeeded"),
        _check("provider_account_binding", profile_bound and balance_bound, "profile and balance responses matched the private entity binding"),
        _check("business_profile_type", profile_bound, "bound profile is a named BUSINESS profile"),
        _check("functional_currency_balance", balance_bound, "bound balance uses the Box entity functional currency"),
        _check("access_contract_eligible", eligible_contract and eligible_jurisdiction, "entity uses an eligible personal-token or approved partner contract"),
        _check("minimal_read_only_probe", authenticated, "only profile and balance metadata endpoints were read; no statement was requested"),
    ]
    return checks, {
        "api_version": WISE_API_VERSION,
        "credential_type": "business_access_token",
        "environment_mode": "provider_managed",
        "entity_binding_source": "server_environment",
        "profile_and_balance_identifiers_returned": False,
        "financial_values_requested": False,
    }


_UUID = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def _xero_probe(
    runtime: BoxRuntime,
    request: dict[str, Any],
    *,
    credentials: Mapping[str, str],
    as_at: str,
    transport: HttpTransport,
    sleeper: Callable[[float], None],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    entity_id = str(request["entity_id"])
    entity = runtime.entities.get(entity_id)
    bindings = _private_json_mapping(
        credentials[XERO_BINDINGS_ENV], provider="Xero",
    )
    selected = bindings.get(entity_id)
    if not isinstance(selected, dict) or set(selected) != {
        "tenant_id", "organisation_id",
    }:
        raise ConnectorAccessProbeError(
            "Xero entity binding requires tenant_id and organisation_id"
        )
    tenant_id = str(selected.get("tenant_id") or "")
    organisation_id = str(selected.get("organisation_id") or "")
    if not _UUID.fullmatch(tenant_id) or not _UUID.fullmatch(organisation_id):
        raise ConnectorAccessProbeError("Xero entity binding contains an invalid identifier")
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {credentials[XERO_TOKEN_ENV]}",
        "xero-tenant-id": tenant_id,
    }
    organisation_payload = _json_object(_send_probe_request(
        HttpRequest(
            url="https://api.xero.com/api.xro/2.0/Organisation",
            headers=headers,
            timeout_seconds=20,
            max_response_bytes=MAX_RESPONSE_BYTES,
        ),
        provider="Xero",
        transport=transport,
        sleeper=sleeper,
    ), provider="Xero")
    report_payload = _json_object(_send_probe_request(
        HttpRequest(
            url=(
                "https://api.xero.com/api.xro/2.0/Reports/TrialBalance"
                f"?date={as_at}&paymentsOnly=false"
            ),
            headers=headers,
            timeout_seconds=20,
            max_response_bytes=MAX_RESPONSE_BYTES,
        ),
        provider="Xero",
        transport=transport,
        sleeper=sleeper,
    ), provider="Xero")
    organisations = (
        organisation_payload.get("Organisations")
        if isinstance(organisation_payload, dict) else None
    )
    organisation = (
        organisations[0]
        if isinstance(organisations, list) and len(organisations) == 1
        and isinstance(organisations[0], dict) else None
    )
    account_bound = bool(
        organisation
        and organisation.get("OrganisationID") == organisation_id
    )
    currency_bound = bool(
        organisation
        and str(organisation.get("BaseCurrency") or "").upper()
        == entity.functional_currency.upper()
    )
    reports = (
        report_payload.get("Reports")
        if isinstance(report_payload, dict) else None
    )
    report_read = bool(
        isinstance(reports, list) and len(reports) == 1
        and isinstance(reports[0], dict)
        and isinstance(reports[0].get("Rows"), list)
    )
    authenticated = organisation_payload is not None and report_payload is not None
    checks = [
        _check("provider_authentication", authenticated, "fixed Xero Accounting read endpoints succeeded"),
        _check("provider_account_binding", account_bound, "Organisation response matched the private tenant-to-entity binding"),
        _check("organisation_read_scope", account_bound, "accounting.settings.read Organisation endpoint succeeded"),
        _check("trial_balance_read_scope", report_read, "accounting.reports.trialbalance.read endpoint succeeded"),
        _check("functional_currency_match", currency_bound, "Organisation base currency matches the Box entity"),
        _check("read_only_probe", authenticated, "only Organisation and Trial Balance GET endpoints were used"),
    ]
    return checks, {
        "api_version": "2.0",
        "credential_type": "oauth_access_token",
        "environment_mode": "provider_managed",
        "entity_binding_source": "server_environment",
        "organisation_identifier_returned": False,
        "trial_balance_values_retained": False,
        "probe_as_at": as_at,
    }


def _paypal_probe(
    runtime: BoxRuntime,
    request: dict[str, Any],
    *,
    credentials: Mapping[str, str],
    observed_at: str,
    transport: HttpTransport,
    sleeper: Callable[[float], None],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    entity = runtime.entities.get(str(request["entity_id"]))
    selected = _private_json_mapping(
        credentials[PAYPAL_BINDINGS_ENV], provider="PayPal",
    )
    if set(selected) != {
        "environment", "app_id", "account_id", "client_id_env",
        "client_secret_env",
    }:
        raise ConnectorAccessProbeError("PayPal selected entity binding is invalid")
    provider_environment = str(selected["environment"])
    host = (
        "api-m.paypal.com"
        if provider_environment == "production"
        else "api-m.sandbox.paypal.com"
    )
    client_id_env = str(selected["client_id_env"])
    client_secret_env = str(selected["client_secret_env"])
    basic = base64.b64encode(
        f"{credentials[client_id_env]}:{credentials[client_secret_env]}".encode()
    ).decode("ascii")
    token_payload = _json_object(_send_probe_request(
        HttpRequest(
            url=f"https://{host}/v1/oauth2/token",
            method="POST",
            body=b"grant_type=client_credentials",
            headers={
                "Accept": "application/json",
                "Accept-Language": "en_US",
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout_seconds=20,
            max_response_bytes=MAX_RESPONSE_BYTES,
        ),
        provider="PayPal",
        transport=transport,
        sleeper=sleeper,
    ), provider="PayPal")
    access_token = token_payload.get("access_token") if token_payload else None
    token_authenticated = bool(
        token_payload
        and isinstance(access_token, str)
        and access_token
        and len(access_token) <= 8192
        and str(token_payload.get("token_type") or "").lower() == "bearer"
    )
    raw_scopes = str(token_payload.get("scope") or "") if token_payload else ""
    scope_set = {item for item in raw_scopes.split() if item}
    required_scope = "https://uri.paypal.com/services/reporting/search/read"
    scope_granted = required_scope in scope_set
    app_bound = bool(
        token_authenticated and token_payload.get("app_id") == selected["app_id"]
    )
    balances_payload = None
    if token_authenticated:
        query = urllib.parse.urlencode({
            "as_of_time": observed_at,
            "currency_code": entity.functional_currency.upper(),
        })
        balances_payload = _json_object(_send_probe_request(
            HttpRequest(
                url=f"https://{host}/v1/reporting/balances?{query}",
                headers={
                    "Accept": "application/json",
                    "Accept-Language": "en_US",
                    "Authorization": f"Bearer {access_token}",
                    "PayPal-Enforce-ISO8601-Format": "true",
                },
                timeout_seconds=20,
                max_response_bytes=MAX_RESPONSE_BYTES,
            ),
            provider="PayPal",
            transport=transport,
            sleeper=sleeper,
        ), provider="PayPal")
    balances = balances_payload.get("balances") if balances_payload else None
    balance_read = bool(
        isinstance(balances, list)
        and all(isinstance(item, dict) for item in balances)
    )
    account_bound = bool(
        balances_payload
        and balances_payload.get("account_id") == selected["account_id"]
    )
    checks = [
        _check("provider_authentication", token_authenticated, "fixed OAuth client-credentials exchange succeeded"),
        _check("provider_account_binding", account_bound, "reporting balance response matched the private entity account binding"),
        _check("required_reporting_scope", token_authenticated and scope_granted, "reporting/search read scope is present in the OAuth grant"),
        _check("rest_app_binding", app_bound, "OAuth response matched the privately bound REST app"),
        _check("reporting_balance_read_scope", balance_read and account_bound, "functional-currency filtered reporting balance GET succeeded"),
        _check("read_only_probe", token_authenticated and balance_read, "only OAuth token exchange and one reporting balance GET were used"),
    ]
    return checks, {
        "api_contract": "Transaction Search v1",
        "credential_type": "oauth_client_credentials",
        "environment_mode": provider_environment,
        "entity_binding_source": "server_environment_aliases",
        "granted_scope_count": len(scope_set),
        "scope_names_returned": False,
        "app_and_account_identifiers_returned": False,
        "balance_values_requested": True,
        "balance_values_retained": False,
        "probe_currency": entity.functional_currency.upper(),
    }


def _woocommerce_probe(
    request: dict[str, Any],
    *,
    credentials: Mapping[str, str],
    transport: HttpTransport,
    sleeper: Callable[[float], None],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    selected = _private_json_mapping(
        credentials[WOOCOMMERCE_BINDINGS_ENV], provider="WooCommerce",
    )
    if set(selected) != {
        "site_origin", "key_permission", "consumer_key_env", "consumer_secret_env",
    }:
        raise ConnectorAccessProbeError(
            "WooCommerce selected entity binding is invalid"
        )
    origin = str(selected["site_origin"]).rstrip("/")
    key_env = str(selected["consumer_key_env"])
    secret_env = str(selected["consumer_secret_env"])
    basic = base64.b64encode(
        f"{credentials[key_env]}:{credentials[secret_env]}".encode()
    ).decode("ascii")
    reads: dict[str, bool] = {}
    for resource in ("orders", "refunds"):
        query = urllib.parse.urlencode({
            "context": "view", "page": 1, "per_page": 1, "_fields": "id",
        })
        response = _send_probe_request(
            HttpRequest(
                url=f"{origin}/wp-json/wc/v3/{resource}?{query}",
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Basic {basic}",
                },
                timeout_seconds=20,
                max_response_bytes=MAX_RESPONSE_BYTES,
            ),
            provider="WooCommerce",
            transport=transport,
            sleeper=sleeper,
        )
        payload = _json_array(response, provider="WooCommerce")
        reads[resource] = bool(
            payload is not None
            and len(payload) <= 1
            and all(
                isinstance(item, dict)
                and set(item) <= {"id"}
                and isinstance(item.get("id"), int)
                and not isinstance(item.get("id"), bool)
                for item in payload
            )
        )
    authenticated = all(reads.values())
    declared_read_only = selected.get("key_permission") == "read"
    checks = [
        _check("provider_authentication", authenticated, "fixed authenticated wc/v3 collection GETs succeeded"),
        _check("provider_account_binding", authenticated, "credentials succeeded only against the privately bound site origin"),
        _check("orders_read_scope", reads["orders"], "orders collection accepted context=view, per_page=1 and _fields=id"),
        _check("refunds_read_scope", reads["refunds"], "refunds collection accepted context=view, per_page=1 and _fields=id"),
        _check("operator_declared_read_only_key", declared_read_only, "private entity binding declares the WooCommerce key permission as read"),
        _check("read_only_probe", authenticated, "only two bounded GET requests were used and no source row was retained"),
    ]
    return checks, {
        "api_contract": "wc-rest-v3",
        "credential_type": "consumer_key_pair",
        "environment_mode": "production_https",
        "entity_binding_source": "server_environment_aliases",
        "site_origin_returned": False,
        "source_ids_retained": False,
        "financial_values_requested": False,
        "write_permission_provider_verified": False,
    }


def _shipbob_probe(
    request: dict[str, Any],
    *,
    credentials: Mapping[str, str],
    transport: HttpTransport,
    sleeper: Callable[[float], None],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    selected = _private_json_mapping(
        credentials[SHIPBOB_BINDINGS_ENV], provider="ShipBob",
    )
    if set(selected) != {"environment", "channel_id", "token_env"}:
        raise ConnectorAccessProbeError("ShipBob selected entity binding is invalid")
    provider_environment = str(selected["environment"])
    host = (
        "api.shipbob.com"
        if provider_environment == "production"
        else "sandbox-api.shipbob.com"
    )
    channel_id = _positive_integer(
        selected["channel_id"], field="ShipBob channel_id",
    )
    token_env = str(selected["token_env"])
    query = urllib.parse.urlencode({"RecordsPerPage": 50})
    payload = _json_object(_send_probe_request(
        HttpRequest(
            url=f"https://{host}/{SHIPBOB_API_VERSION}/channel?{query}",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {credentials[token_env]}",
            },
            timeout_seconds=20,
            max_response_bytes=MAX_RESPONSE_BYTES,
        ),
        provider="ShipBob",
        transport=transport,
        sleeper=sleeper,
    ), provider="ShipBob")
    items = payload.get("items") if payload else None
    channels_valid = bool(
        isinstance(items, list)
        and len(items) <= 50
        and all(isinstance(item, dict) for item in items)
    )
    selected_channel = next(
        (
            item for item in (items or [])
            if isinstance(item, dict) and item.get("id") == channel_id
        ),
        None,
    )
    raw_scopes = selected_channel.get("scopes") if selected_channel else None
    scopes_valid = bool(
        isinstance(raw_scopes, list)
        and raw_scopes
        and len(raw_scopes) <= 100
        and all(isinstance(item, str) and _SCOPE.fullmatch(item) for item in raw_scopes)
        and len(raw_scopes) == len(set(raw_scopes))
    )
    scope_set = set(raw_scopes or []) if scopes_valid else set()
    required = {
        "channels_read", "orders_read", "fulfillments_read", "returns_read",
    }
    required_read = required <= scope_set
    no_write = not any(scope.endswith("_write") for scope in scope_set)
    least_privilege = scope_set == required
    account_bound = bool(channels_valid and selected_channel and scopes_valid)
    authenticated = payload is not None and channels_valid
    checks = [
        _check("provider_authentication", authenticated, "fixed ShipBob Channels GET succeeded"),
        _check("provider_account_binding", account_bound, "Channels response contained the privately bound channel"),
        _check("required_read_scopes", account_bound and required_read, "bound channel grants channels, orders, fulfillments and returns read scopes"),
        _check("no_write_scopes", account_bound and no_write, "bound channel grants no *_write scope"),
        _check("least_privilege_scope_set", account_bound and least_privilege, "bound channel grants exactly the four required read scopes"),
        _check("read_only_probe", authenticated, "only one bounded Channels GET was used and channel details were discarded"),
    ]
    return checks, {
        "api_version": SHIPBOB_API_VERSION,
        "credential_type": "personal_or_oauth_access_token",
        "environment_mode": provider_environment,
        "entity_binding_source": "server_environment_aliases",
        "granted_scope_count": len(scope_set),
        "scope_names_returned": False,
        "channel_identifier_returned": False,
        "financial_values_requested": False,
    }


def _amazon_seller_probe(
    request: dict[str, Any],
    *,
    credentials: Mapping[str, str],
    observed_at: str,
    transport: HttpTransport,
    sleeper: Callable[[float], None],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    selected = _private_json_mapping(
        credentials[AMAZON_SELLER_BINDINGS_ENV], provider="Amazon Seller",
    )
    if set(selected) != {
        "environment", "region", "seller_id", "marketplace_ids",
        "client_id_env", "client_secret_env", "refresh_token_env",
    }:
        raise ConnectorAccessProbeError(
            "Amazon Seller selected entity binding is invalid"
        )
    provider_environment = str(selected["environment"])
    region = str(selected["region"])
    hosts = {
        "NA": "sellingpartnerapi-na.amazon.com",
        "EU": "sellingpartnerapi-eu.amazon.com",
        "FE": "sellingpartnerapi-fe.amazon.com",
    }
    host = hosts.get(region)
    if host is None:
        raise ConnectorAccessProbeError("Amazon Seller selected region is invalid")
    if provider_environment == "sandbox":
        host = f"sandbox.{host}"
    marketplace_ids = selected.get("marketplace_ids")
    if (
        not isinstance(marketplace_ids, list)
        or not marketplace_ids
        or len(marketplace_ids) > 100
        or any(
            not isinstance(item, str) or not re.fullmatch(r"[A-Z0-9]{6,32}", item)
            for item in marketplace_ids
        )
        or marketplace_ids != sorted(set(marketplace_ids))
    ):
        raise ConnectorAccessProbeError(
            "Amazon Seller selected marketplace binding is invalid"
        )
    client_id_env = str(selected["client_id_env"])
    client_secret_env = str(selected["client_secret_env"])
    refresh_token_env = str(selected["refresh_token_env"])
    token_body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": credentials[refresh_token_env],
        "client_id": credentials[client_id_env],
        "client_secret": credentials[client_secret_env],
    }).encode("ascii")
    token_payload = _json_object(_send_probe_request(
        HttpRequest(
            url="https://api.amazon.com/auth/o2/token",
            method="POST",
            body=token_body,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "OPC-Finance-Box/0.1",
            },
            timeout_seconds=20,
            max_response_bytes=MAX_RESPONSE_BYTES,
        ),
        provider="Amazon Seller",
        transport=transport,
        sleeper=sleeper,
    ), provider="Amazon Seller")
    access_token = token_payload.get("access_token") if token_payload else None
    authenticated = bool(
        isinstance(access_token, str) and access_token and len(access_token) <= 8192
    )

    def sp_get(path: str, *, provider_label: str) -> dict[str, Any] | None:
        if not authenticated:
            return None
        return _json_object(_send_probe_request(
            HttpRequest(
                url=f"https://{host}{path}",
                headers={
                    "Accept": "application/json",
                    "User-Agent": "OPC-Finance-Box/0.1",
                    "x-amz-access-token": str(access_token),
                    "x-amz-date": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
                },
                timeout_seconds=20,
                max_response_bytes=MAX_RESPONSE_BYTES,
            ),
            provider=provider_label,
            transport=transport,
            sleeper=sleeper,
        ), provider=provider_label)

    participations_payload = sp_get(
        "/sellers/v1/marketplaceParticipations",
        provider_label="Amazon Seller",
    )
    participations = (
        participations_payload.get("payload")
        if participations_payload else None
    )
    participation_by_marketplace: dict[str, bool] = {}
    if isinstance(participations, list) and len(participations) <= 100:
        for item in participations:
            marketplace = item.get("marketplace") if isinstance(item, dict) else None
            participation = item.get("participation") if isinstance(item, dict) else None
            marketplace_id = (
                marketplace.get("id") if isinstance(marketplace, dict) else None
            )
            if (
                isinstance(marketplace_id, str)
                and re.fullmatch(r"[A-Z0-9]{6,32}", marketplace_id)
                and isinstance(participation, dict)
            ):
                participation_by_marketplace[marketplace_id] = bool(
                    participation.get("isParticipating") is True
                    and participation.get("isSuspended") is False
                )
    marketplaces_bound = all(
        participation_by_marketplace.get(item) is True for item in marketplace_ids
    )
    probe_marketplace = marketplace_ids[0]
    observed = _timestamp(observed_at, field="observed_at")
    window_end = observed - timedelta(minutes=5)
    window_start = window_end - timedelta(minutes=5)
    start = window_start.isoformat().replace("+00:00", "Z")
    end = window_end.isoformat().replace("+00:00", "Z")
    orders_query = urllib.parse.urlencode({
        "createdAfter": start,
        "createdBefore": end,
        "marketplaceIds": probe_marketplace,
        "maxResultsPerPage": 1,
        "includedData": "FULFILLMENT",
    })
    orders_payload = sp_get(
        f"/orders/{AMAZON_SELLER_ORDERS_VERSION}/orders?{orders_query}",
        provider_label="Amazon Seller",
    )
    orders_read = bool(
        orders_payload is not None
        and isinstance(orders_payload.get("orders"), list)
        and len(orders_payload["orders"]) <= 1
    )
    inventory_query = urllib.parse.urlencode({
        "details": "false",
        "granularityType": "Marketplace",
        "granularityId": probe_marketplace,
        "marketplaceIds": probe_marketplace,
        "startDateTime": observed_at,
    })
    inventory_payload = sp_get(
        f"/fba/inventory/v1/summaries?{inventory_query}",
        provider_label="Amazon Seller",
    )
    inventory_body = (
        inventory_payload.get("payload") if inventory_payload else None
    )
    granularity = (
        inventory_body.get("granularity")
        if isinstance(inventory_body, dict) else None
    )
    inventory_read = bool(
        isinstance(inventory_body, dict)
        and isinstance(inventory_body.get("inventorySummaries"), list)
        and isinstance(granularity, dict)
        and granularity.get("granularityType") == "Marketplace"
        and granularity.get("granularityId") == probe_marketplace
    )
    finances_query = urllib.parse.urlencode({
        "postedAfter": start,
        "postedBefore": end,
        "marketplaceId": probe_marketplace,
    })
    finances_payload = sp_get(
        f"/finances/2024-06-19/transactions?{finances_query}",
        provider_label="Amazon Seller",
    )
    finances_body = finances_payload.get("payload") if finances_payload else None
    finances_read = bool(
        isinstance(finances_body, dict)
        and isinstance(finances_body.get("transactions"), list)
    )
    fixed_endpoint = all(
        region_host in host
        for region_host in [hosts[region]]
    )
    checks = [
        _check("provider_authentication", authenticated, "fixed LWA refresh-token exchange succeeded"),
        _check("provider_account_binding", marketplaces_bound, "Sellers response contained every privately bound active marketplace"),
        _check("marketplace_participations_read", bool(participation_by_marketplace), "Sellers marketplace participations GET succeeded"),
        _check("orders_read_scope", orders_read, "bounded Orders FULFILLMENT request succeeded"),
        _check("inventory_read_scope", inventory_read, "future-start current FBA Inventory request succeeded"),
        _check("finances_read_scope", finances_read, "bounded Finances transaction request succeeded"),
        _check("fixed_region_endpoint", fixed_endpoint, "all SP-API reads used the closed regional endpoint"),
        _check("read_only_probe", authenticated and orders_read and inventory_read and finances_read, "one token exchange and four fixed GETs were used; no source row was retained"),
    ]
    return checks, {
        "api_contracts": [
            "sellers-v1-getMarketplaceParticipations",
            "orders-v2026-01-01-searchOrders",
            "fba-inventory-v1-getInventorySummaries",
            "finances-v2024-06-19-listTransactions",
        ],
        "credential_type": "lwa_refresh_token_exchange",
        "environment_mode": provider_environment,
        "region": region,
        "entity_binding_source": "server_environment_aliases",
        "bound_marketplace_count": len(marketplace_ids),
        "seller_id_provider_verified": False,
        "provider_identifiers_returned": False,
        "source_records_retained": False,
        "financial_values_requested": True,
        "financial_values_retained": False,
    }


def run_connector_access_probe(
    runtime: BoxRuntime,
    request_path: str | Path,
    *,
    allow_network: bool,
    environ: Mapping[str, str] | None = None,
    transport: HttpTransport = urllib_transport,
    sleeper: Callable[[float], None] = time.sleep,
    observed_at: str | None = None,
) -> dict[str, Any]:
    """Perform an explicitly authorized, read-only, secret-free access probe."""
    request = read_private_connector_access_request(request_path)
    validated = validate_connector_access_request(runtime, request)
    pack_id = validated["pack_id"]
    environment = os.environ if environ is None else environ
    env_names, credentials, credentials_configured = _credential_group(
        pack_id, environment, entity_id=validated["entity_id"],
    )
    base = {
        "schema_version": 2,
        "artifact_type": "opc_finance_box_connector_access_probe",
        "runtime_fingerprint": runtime.snapshot()["fingerprint"],
        "request_fingerprint": validated["request_fingerprint"],
        "pack_id": pack_id,
        "entity_id": validated["entity_id"],
        "provider_account_binding": {
            "mode": validated["binding_mode"],
            "fingerprint": validated["provider_account_fingerprint"],
            "verified": False,
        },
        "credential_reference": {
            "env_names": list(env_names),
            "configured": credentials_configured,
        },
        "network_authorized_by_operator": bool(allow_network),
    }
    if not allow_network:
        return {
            **base,
            "observed_at": None,
            "status": "network_authorization_required",
            "checks": [],
            "summary": {
                "required_check_count": 0,
                "passed_check_count": 0,
                "blocked_check_count": 0,
                "ready_for_private_shadow_request": False,
            },
            "next_action": "rerun with --allow-network after an authorized operator approves the bounded read-only probe",
            "control_boundary": _control_boundary(network_performed=False),
        }
    if not credentials_configured:
        return {
            **base,
            "observed_at": None,
            "status": "blocked_missing_credential_reference",
            "checks": [],
            "summary": {
                "required_check_count": 0,
                "passed_check_count": 0,
                "blocked_check_count": 1,
                "ready_for_private_shadow_request": False,
            },
            "next_action": (
                "configure every required credential reference in a server-side "
                "secret manager or environment"
            ),
            "control_boundary": _control_boundary(network_performed=False),
        }
    timestamp = observed_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    if pack_id == "connector.shopify":
        checks, provider = _shopify_probe(
            request, token=credentials[SHOPIFY_TOKEN_ENV],
            transport=transport, sleeper=sleeper,
        )
    elif pack_id == "connector.stripe":
        checks, provider = _stripe_probe(
            request, key=credentials[STRIPE_KEY_ENV],
            transport=transport, sleeper=sleeper,
        )
    elif pack_id == "connector.wise":
        checks, provider = _wise_probe(
            runtime, request, credentials=credentials,
            transport=transport, sleeper=sleeper,
        )
    elif pack_id == "connector.xero":
        checks, provider = _xero_probe(
            runtime, request, credentials=credentials,
            as_at=_timestamp(timestamp, field="observed_at").date().isoformat(),
            transport=transport, sleeper=sleeper,
        )
    elif pack_id == "connector.paypal":
        checks, provider = _paypal_probe(
            runtime,
            request,
            credentials=credentials,
            observed_at=timestamp,
            transport=transport,
            sleeper=sleeper,
        )
    elif pack_id == "connector.woocommerce":
        checks, provider = _woocommerce_probe(
            request,
            credentials=credentials,
            transport=transport,
            sleeper=sleeper,
        )
    elif pack_id == "connector.shipbob":
        checks, provider = _shipbob_probe(
            request,
            credentials=credentials,
            transport=transport,
            sleeper=sleeper,
        )
    else:
        checks, provider = _amazon_seller_probe(
            request,
            credentials=credentials,
            observed_at=timestamp,
            transport=transport,
            sleeper=sleeper,
        )
    ready = all(item["passed"] for item in checks)
    binding_verified = next(
        item["passed"] for item in checks if item["check_id"] == "provider_account_binding"
    )
    result = {
        **base,
        "observed_at": timestamp,
        "status": "passed" if ready else "blocked_provider_access",
        "provider": provider,
        "checks": checks,
        "summary": {
            "required_check_count": len(checks),
            "passed_check_count": sum(1 for item in checks if item["passed"]),
            "blocked_check_count": sum(1 for item in checks if not item["passed"]),
            "ready_for_private_shadow_request": ready,
        },
        "next_action": (
            "initialize a private entity-and-period Shadow request; this probe does not authorize dispatch"
            if ready
            else "reduce excess access or grant only the missing read permissions, then rerun the probe"
        ),
        "control_boundary": _control_boundary(network_performed=True),
    }
    result["credential_reference"]["fingerprint"] = _credential_group_fingerprint(
        runtime, pack_id, credentials, entity_id=validated["entity_id"],
    )
    result["provider_account_binding"]["verified"] = binding_verified
    return result


def _control_boundary(*, network_performed: bool) -> dict[str, bool]:
    return {
        "network_access_performed": network_performed,
        "read_only_requests_only": True,
        "credential_values_returned": False,
        "provider_account_identifiers_returned": False,
        "raw_provider_responses_returned": False,
        "source_records_returned": False,
        "financial_values_returned": False,
        "payments_created": False,
        "refunds_created": False,
        "accounting_entries_posted": False,
        "tax_filings_submitted": False,
        "shadow_request_dispatched": False,
        "financial_reconciliation_inferred": False,
        "schedule_released": False,
    }


def write_connector_access_probe_receipt(
    runtime: BoxRuntime,
    request_path: str | Path,
    output: str | Path,
    *,
    allow_network: bool,
    environ: Mapping[str, str] | None = None,
    transport: HttpTransport = urllib_transport,
    sleeper: Callable[[float], None] = time.sleep,
    observed_at: str | None = None,
) -> dict[str, Any]:
    """Run an authorized probe and persist a private, secret-free receipt."""
    if not allow_network:
        raise ConnectorAccessProbeError(
            "Connector access receipt creation requires explicit network authorization"
        )
    result = run_connector_access_probe(
        runtime,
        request_path,
        allow_network=True,
        environ=environ,
        transport=transport,
        sleeper=sleeper,
        observed_at=observed_at,
    )
    receipt = _receipt_from_probe_result(result)
    _write_private_artifact(
        output, receipt, artifact_label="Connector access probe receipt",
    )
    return _receipt_write_summary(receipt)


def _receipt_from_probe_result(result: dict[str, Any]) -> dict[str, Any]:
    receipt = {
        **result,
        "artifact_type": "opc_finance_box_connector_access_probe_receipt",
        "receipt_is_digital_signature": False,
    }
    receipt["receipt_fingerprint"] = _fingerprint(receipt)
    return receipt


def _receipt_write_summary(receipt: dict[str, Any]) -> dict[str, Any]:
    return {
        "written": True,
        "schema_version": receipt["schema_version"],
        "artifact_type": receipt["artifact_type"],
        "pack_id": receipt["pack_id"],
        "entity_id": receipt["entity_id"],
        "status": receipt["status"],
        "ready_for_private_shadow_request": receipt["summary"][
            "ready_for_private_shadow_request"
        ],
        "receipt_fingerprint": receipt["receipt_fingerprint"],
        "receipt_is_digital_signature": False,
        "credentials_returned": False,
        "provider_account_returned": False,
        "financial_values_returned": False,
    }


def read_private_connector_access_probe_receipt(path: str | Path) -> dict[str, Any]:
    raw = Path(path).expanduser()
    if not raw.is_absolute():
        raise ConnectorAccessProbeError("Connector access receipt must use an absolute path")
    try:
        raw_metadata = raw.lstat()
    except OSError as exc:
        raise ConnectorAccessProbeError("Connector access receipt is unavailable") from exc
    if stat.S_ISLNK(raw_metadata.st_mode) or not stat.S_ISREG(raw_metadata.st_mode):
        raise ConnectorAccessProbeError(
            "Connector access receipt must be a regular non-symlink file"
        )
    source = raw.resolve()
    metadata = source.stat()
    if os.name != "nt" and stat.S_IMODE(metadata.st_mode) != 0o600:
        raise ConnectorAccessProbeError("Connector access receipt must use mode 0600")
    if not 0 < metadata.st_size <= MAX_RECEIPT_BYTES:
        raise ConnectorAccessProbeError(
            "Connector access receipt must be between 1 byte and 256 KiB"
        )
    try:
        receipt = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConnectorAccessProbeError(
            "Connector access receipt must contain valid UTF-8 JSON"
        ) from exc
    if not isinstance(receipt, dict):
        raise ConnectorAccessProbeError("Connector access receipt must be a JSON object")
    return receipt


def _timestamp(value: Any, *, field: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ConnectorAccessProbeError(f"Connector access receipt {field} is required")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ConnectorAccessProbeError(
            f"Connector access receipt {field} must be an ISO-8601 timestamp"
        ) from exc
    if parsed.tzinfo is None:
        raise ConnectorAccessProbeError(
            f"Connector access receipt {field} must include a timezone"
        )
    return parsed.astimezone(timezone.utc)


def _receipt_credential_env_names(
    pack_id: str,
    receipt_schema: int,
    credential_reference: Any,
) -> tuple[str, ...]:
    if receipt_schema == 1:
        return CREDENTIAL_ENV_NAMES[pack_id]
    if pack_id not in {
        "connector.paypal", "connector.woocommerce", "connector.shipbob",
        "connector.amazon_seller",
    }:
        return CREDENTIAL_ENV_NAMES[pack_id]
    names = (
        credential_reference.get("env_names")
        if isinstance(credential_reference, dict) else None
    )
    expected_binding_env = CREDENTIAL_ENV_NAMES[pack_id][0]
    expected_length = {
        "connector.paypal": 3,
        "connector.woocommerce": 3,
        "connector.shipbob": 2,
        "connector.amazon_seller": 4,
    }[pack_id]
    if (
        not isinstance(names, list)
        or len(names) != expected_length
        or names[0] != expected_binding_env
        or any(not isinstance(name, str) or not _ENV_REFERENCE.fullmatch(name) for name in names)
        or len(set(names)) != expected_length
    ):
        raise ConnectorAccessProbeError(
            "Connector access receipt dynamic credential references are invalid"
        )
    return tuple(names)


def _validate_private_connector_access_probe_receipt_contract(
    runtime: BoxRuntime,
    request_path: str | Path,
    receipt_path: str | Path,
) -> tuple[
    dict[str, Any], dict[str, Any], dict[str, Any], str, datetime,
    tuple[str, ...],
]:
    """Validate immutable receipt evidence without claiming current credential or age."""
    request = read_private_connector_access_request(request_path)
    validated = validate_connector_access_request(runtime, request)
    receipt = read_private_connector_access_probe_receipt(receipt_path)
    expected_receipt_fields = {
        "schema_version", "artifact_type", "runtime_fingerprint",
        "request_fingerprint", "pack_id", "entity_id",
        "provider_account_binding", "credential_reference",
        "network_authorized_by_operator", "observed_at", "status", "provider",
        "checks", "summary", "next_action", "control_boundary",
        "receipt_is_digital_signature", "receipt_fingerprint",
    }
    if set(receipt) != expected_receipt_fields:
        raise ConnectorAccessProbeError("Connector access receipt fields are invalid")
    fingerprint = receipt.get("receipt_fingerprint")
    body = dict(receipt)
    body.pop("receipt_fingerprint", None)
    if not isinstance(fingerprint, str) or fingerprint != _fingerprint(body):
        raise ConnectorAccessProbeError("Connector access receipt fingerprint is invalid")
    receipt_schema = receipt.get("schema_version")
    if (
        receipt_schema not in {1, 2}
        or receipt.get("artifact_type") != (
        "opc_finance_box_connector_access_probe_receipt"
        )
        or (
            receipt_schema == 1
            and validated["pack_id"] not in {"connector.shopify", "connector.stripe"}
        )
    ):
        raise ConnectorAccessProbeError("Connector access receipt schema or artifact type is invalid")
    if receipt.get("receipt_is_digital_signature") is not False:
        raise ConnectorAccessProbeError(
            "Connector access receipt must declare that it is not a digital signature"
        )
    runtime_fingerprint = runtime.snapshot()["fingerprint"]
    exact_bindings = {
        "runtime_fingerprint": runtime_fingerprint,
        "request_fingerprint": validated["request_fingerprint"],
        "pack_id": validated["pack_id"],
        "entity_id": validated["entity_id"],
    }
    for field, expected in exact_bindings.items():
        if receipt.get(field) != expected:
            raise ConnectorAccessProbeError(
                f"Connector access receipt {field} does not match the current request"
            )
    if receipt.get("status") != "passed":
        raise ConnectorAccessProbeError("Connector access receipt did not pass all required checks")
    if receipt.get("next_action") != (
        "initialize a private entity-and-period Shadow request; this probe does not authorize dispatch"
    ):
        raise ConnectorAccessProbeError("Connector access receipt next action is invalid")
    if receipt.get("network_authorized_by_operator") is not True:
        raise ConnectorAccessProbeError(
            "Connector access receipt lacks explicit operator network authorization"
        )
    binding = receipt.get("provider_account_binding")
    if binding != {
        "mode": validated["binding_mode"],
        "fingerprint": validated["provider_account_fingerprint"],
        "verified": True,
    }:
        raise ConnectorAccessProbeError(
            "Connector access receipt provider-account binding is invalid"
        )
    credential_reference = receipt.get("credential_reference")
    env_names = _receipt_credential_env_names(
        validated["pack_id"], int(receipt_schema), credential_reference,
    )
    legacy_reference_valid = (
        receipt_schema == 1
        and isinstance(credential_reference, dict)
        and set(credential_reference) == {"env_name", "configured", "fingerprint"}
        and credential_reference.get("env_name") == env_names[0]
        and credential_reference.get("configured") is True
        and isinstance(credential_reference.get("fingerprint"), str)
        and re.fullmatch(r"sha256:[a-f0-9]{64}", credential_reference["fingerprint"])
        is not None
    )
    group_reference_valid = (
        receipt_schema == 2
        and isinstance(credential_reference, dict)
        and set(credential_reference) == {"env_names", "configured", "fingerprint"}
        and credential_reference.get("env_names") == list(env_names)
        and credential_reference.get("configured") is True
        and isinstance(credential_reference.get("fingerprint"), str)
        and re.fullmatch(r"sha256:[a-f0-9]{64}", credential_reference["fingerprint"])
        is not None
    )
    if not legacy_reference_valid and not group_reference_valid:
        raise ConnectorAccessProbeError(
            "Connector access receipt credential reference contract is invalid"
        )
    expected_check_ids = EXPECTED_CHECK_IDS[validated["pack_id"]]
    checks = receipt.get("checks")
    if not isinstance(checks, list) or len(checks) != len(expected_check_ids):
        raise ConnectorAccessProbeError("Connector access receipt required checks are invalid")
    check_ids: set[str] = set()
    for item in checks:
        if (
            not isinstance(item, dict)
            or set(item) != {"check_id", "required", "passed", "status", "evidence"}
            or item.get("required") is not True
            or item.get("passed") is not True
            or item.get("status") != "passed"
            or not isinstance(item.get("evidence"), str)
            or not item["evidence"]
        ):
            raise ConnectorAccessProbeError("Connector access receipt contains a failed check")
        check_ids.add(str(item.get("check_id") or ""))
    if check_ids != expected_check_ids:
        raise ConnectorAccessProbeError("Connector access receipt check set is invalid")
    expected_summary = {
        "required_check_count": len(expected_check_ids),
        "passed_check_count": len(expected_check_ids),
        "blocked_check_count": 0,
        "ready_for_private_shadow_request": True,
    }
    if receipt.get("summary") != expected_summary:
        raise ConnectorAccessProbeError("Connector access receipt summary is invalid")
    if receipt.get("control_boundary") != _control_boundary(network_performed=True):
        raise ConnectorAccessProbeError("Connector access receipt control boundary is invalid")
    observed = _timestamp(receipt.get("observed_at"), field="observed_at")
    provider = receipt.get("provider")
    if not isinstance(provider, dict):
        raise ConnectorAccessProbeError("Connector access receipt provider evidence is invalid")
    if validated["pack_id"] == "connector.shopify":
        if (
            set(provider) != {
                "api_version", "credential_type", "environment_mode",
                "granted_scope_count", "scope_set_fingerprint",
                "scope_names_returned",
            }
            or provider.get("api_version") != SHOPIFY_API_VERSION
            or provider.get("credential_type") != "admin_access_token"
            or provider.get("environment_mode") != "provider_managed"
            or not isinstance(provider.get("granted_scope_count"), int)
            or isinstance(provider.get("granted_scope_count"), bool)
            or not 1 <= provider["granted_scope_count"] <= 2
            or not isinstance(provider.get("scope_set_fingerprint"), str)
            or not re.fullmatch(r"sha256:[a-f0-9]{64}", provider["scope_set_fingerprint"])
            or provider.get("scope_names_returned") is not False
        ):
            raise ConnectorAccessProbeError("Shopify access receipt evidence is invalid")
    elif validated["pack_id"] == "connector.stripe" and (
        set(provider) != {
            "api_version", "credential_type", "environment_mode",
            "connected_account_header_used",
        }
        or provider.get("api_version") != STRIPE_API_VERSION
        or provider.get("credential_type") != "restricted_api_key"
        or provider.get("environment_mode") not in {"test", "live"}
        or provider.get("connected_account_header_used")
        is not (validated["binding_mode"] == "connected_account")
    ):
        raise ConnectorAccessProbeError("Stripe access receipt evidence is invalid")
    elif validated["pack_id"] == "connector.wise" and (
        provider != {
            "api_version": WISE_API_VERSION,
            "credential_type": "business_access_token",
            "environment_mode": "provider_managed",
            "entity_binding_source": "server_environment",
            "profile_and_balance_identifiers_returned": False,
            "financial_values_requested": False,
        }
    ):
        raise ConnectorAccessProbeError("Wise access receipt evidence is invalid")
    elif validated["pack_id"] == "connector.xero" and (
        set(provider) != {
            "api_version", "credential_type", "environment_mode",
            "entity_binding_source", "organisation_identifier_returned",
            "trial_balance_values_retained", "probe_as_at",
        }
        or provider.get("api_version") != "2.0"
        or provider.get("credential_type") != "oauth_access_token"
        or provider.get("environment_mode") != "provider_managed"
        or provider.get("entity_binding_source") != "server_environment"
        or provider.get("organisation_identifier_returned") is not False
        or provider.get("trial_balance_values_retained") is not False
        or not isinstance(provider.get("probe_as_at"), str)
        or not re.fullmatch(r"20\d{2}-\d{2}-\d{2}", provider["probe_as_at"])
        or provider.get("probe_as_at") != observed.date().isoformat()
    ):
        raise ConnectorAccessProbeError("Xero access receipt evidence is invalid")
    elif validated["pack_id"] == "connector.paypal" and (
        set(provider) != {
            "api_contract", "credential_type", "environment_mode",
            "entity_binding_source", "granted_scope_count",
            "scope_names_returned", "app_and_account_identifiers_returned",
            "balance_values_requested", "balance_values_retained",
            "probe_currency",
        }
        or provider.get("api_contract") != "Transaction Search v1"
        or provider.get("credential_type") != "oauth_client_credentials"
        or provider.get("environment_mode") not in {"production", "sandbox"}
        or provider.get("entity_binding_source") != "server_environment_aliases"
        or not isinstance(provider.get("granted_scope_count"), int)
        or isinstance(provider.get("granted_scope_count"), bool)
        or provider["granted_scope_count"] < 1
        or provider.get("scope_names_returned") is not False
        or provider.get("app_and_account_identifiers_returned") is not False
        or provider.get("balance_values_requested") is not True
        or provider.get("balance_values_retained") is not False
        or not isinstance(provider.get("probe_currency"), str)
        or not re.fullmatch(r"[A-Z]{3}", provider["probe_currency"])
    ):
        raise ConnectorAccessProbeError("PayPal access receipt evidence is invalid")
    elif validated["pack_id"] == "connector.woocommerce" and provider != {
        "api_contract": "wc-rest-v3",
        "credential_type": "consumer_key_pair",
        "environment_mode": "production_https",
        "entity_binding_source": "server_environment_aliases",
        "site_origin_returned": False,
        "source_ids_retained": False,
        "financial_values_requested": False,
        "write_permission_provider_verified": False,
    }:
        raise ConnectorAccessProbeError(
            "WooCommerce access receipt evidence is invalid"
        )
    elif validated["pack_id"] == "connector.shipbob" and (
        set(provider) != {
            "api_version", "credential_type", "environment_mode",
            "entity_binding_source", "granted_scope_count",
            "scope_names_returned", "channel_identifier_returned",
            "financial_values_requested",
        }
        or provider.get("api_version") != SHIPBOB_API_VERSION
        or provider.get("credential_type") != "personal_or_oauth_access_token"
        or provider.get("environment_mode") not in {"production", "sandbox"}
        or provider.get("entity_binding_source") != "server_environment_aliases"
        or provider.get("granted_scope_count") != 4
        or provider.get("scope_names_returned") is not False
        or provider.get("channel_identifier_returned") is not False
        or provider.get("financial_values_requested") is not False
    ):
        raise ConnectorAccessProbeError("ShipBob access receipt evidence is invalid")
    elif validated["pack_id"] == "connector.amazon_seller" and (
        set(provider) != {
            "api_contracts", "credential_type", "environment_mode", "region",
            "entity_binding_source", "bound_marketplace_count",
            "seller_id_provider_verified", "provider_identifiers_returned",
            "source_records_retained", "financial_values_requested",
            "financial_values_retained",
        }
        or provider.get("api_contracts") != [
            "sellers-v1-getMarketplaceParticipations",
            "orders-v2026-01-01-searchOrders",
            "fba-inventory-v1-getInventorySummaries",
            "finances-v2024-06-19-listTransactions",
        ]
        or provider.get("credential_type") != "lwa_refresh_token_exchange"
        or provider.get("environment_mode") not in {"production", "sandbox"}
        or provider.get("region") not in {"NA", "EU", "FE"}
        or provider.get("entity_binding_source") != "server_environment_aliases"
        or not isinstance(provider.get("bound_marketplace_count"), int)
        or isinstance(provider.get("bound_marketplace_count"), bool)
        or not 1 <= provider["bound_marketplace_count"] <= 100
        or provider.get("seller_id_provider_verified") is not False
        or provider.get("provider_identifiers_returned") is not False
        or provider.get("source_records_retained") is not False
        or provider.get("financial_values_requested") is not True
        or provider.get("financial_values_retained") is not False
    ):
        raise ConnectorAccessProbeError(
            "Amazon Seller access receipt evidence is invalid"
        )
    serialized = json.dumps(receipt, ensure_ascii=False, sort_keys=True)
    provider_account = request["account_binding"].get(
        "shop_domain"
        if validated["pack_id"] == "connector.shopify"
        else "account_id"
        if validated["pack_id"] == "connector.stripe"
        else None
    )
    if provider_account and str(provider_account) in serialized:
        raise ConnectorAccessProbeError(
            "Connector access receipt exposes a private provider-account identifier"
        )
    if _SECRET_VALUE.search(serialized):
        raise ConnectorAccessProbeError("Connector access receipt exposes a credential value")
    return request, validated, receipt, fingerprint, observed, env_names


def verify_private_connector_access_probe_receipt_contract(
    runtime: BoxRuntime,
    request_path: str | Path,
    receipt_path: str | Path,
) -> dict[str, Any]:
    """Verify static receipt integrity without treating it as current access."""
    _, validated, receipt, fingerprint, _, _ = (
        _validate_private_connector_access_probe_receipt_contract(
            runtime, request_path, receipt_path,
        )
    )
    return {
        "valid_static_contract": True,
        "schema_version": receipt["schema_version"],
        "artifact_type": receipt["artifact_type"],
        "pack_id": validated["pack_id"],
        "entity_id": validated["entity_id"],
        "binding_mode": validated["binding_mode"],
        "observed_at": receipt["observed_at"],
        "receipt_fingerprint": fingerprint,
        "receipt_is_digital_signature": False,
        "current_credential_binding_verified": False,
        "freshness_verified": False,
        "ready_for_private_shadow_request": False,
        "credentials_returned": False,
        "provider_account_returned": False,
        "financial_values_returned": False,
        "network_access_performed": False,
        "external_actions_performed": False,
    }


def verify_private_connector_access_probe_receipt(
    runtime: BoxRuntime,
    request_path: str | Path,
    receipt_path: str | Path,
    *,
    as_of: str | None = None,
    maximum_age_days: int = DEFAULT_RECEIPT_MAXIMUM_AGE_DAYS,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Verify that a current passed receipt matches one private request and runtime."""
    if (
        not isinstance(maximum_age_days, int)
        or isinstance(maximum_age_days, bool)
        or not 1 <= maximum_age_days <= 365
    ):
        raise ConnectorAccessProbeError(
            "Connector access receipt maximum_age_days must be an integer from 1 to 365"
        )
    _, validated, receipt, fingerprint, observed, env_names = (
        _validate_private_connector_access_probe_receipt_contract(
            runtime, request_path, receipt_path,
        )
    )
    environment = os.environ if environ is None else environ
    _, current_credentials, credentials_configured = _credential_group(
        validated["pack_id"],
        environment,
        entity_id=validated["entity_id"],
    )
    if not credentials_configured:
        raise ConnectorAccessProbeError(
            "Connector access receipt requires the currently bound credential reference"
        )
    primary_credential = current_credentials[env_names[0]]
    if validated["pack_id"] == "connector.stripe" and not _STRIPE_RESTRICTED_KEY.fullmatch(
        primary_credential
    ):
        raise ConnectorAccessProbeError(
            "Connector access receipt requires the current Stripe rk_ restricted key"
        )
    expected_reference = (
        {
            "env_name": env_names[0],
            "configured": True,
            "fingerprint": _credential_fingerprint(
                runtime, validated["pack_id"], primary_credential,
            ),
        }
        if receipt["schema_version"] == 1 else
        {
            "env_names": list(env_names),
            "configured": True,
            "fingerprint": _credential_group_fingerprint(
                runtime,
                validated["pack_id"],
                current_credentials,
                entity_id=validated["entity_id"],
            ),
        }
    )
    if receipt.get("credential_reference") != expected_reference:
        raise ConnectorAccessProbeError(
            "Connector access receipt credential binding is invalid or has changed"
        )
    if (
        validated["pack_id"] == "connector.stripe"
        and receipt["provider"]["environment_mode"]
        != ("test" if primary_credential.startswith("rk_test_") else "live")
    ):
        raise ConnectorAccessProbeError(
            "Stripe access receipt environment does not match the current restricted key"
        )
    clock = (
        _timestamp(as_of, field="as_of")
        if as_of is not None
        else datetime.now(timezone.utc)
    )
    if observed > clock + timedelta(minutes=5):
        raise ConnectorAccessProbeError("Connector access receipt is dated in the future")
    if observed < clock - timedelta(days=maximum_age_days):
        raise ConnectorAccessProbeError(
            f"Connector access receipt is older than {maximum_age_days} days"
        )
    return {
        "valid": True,
        "schema_version": receipt["schema_version"],
        "artifact_type": receipt["artifact_type"],
        "pack_id": validated["pack_id"],
        "entity_id": validated["entity_id"],
        "binding_mode": validated["binding_mode"],
        "observed_at": receipt["observed_at"],
        "maximum_age_days": maximum_age_days,
        "receipt_fingerprint": fingerprint,
        "receipt_is_digital_signature": False,
        "ready_for_private_shadow_request": True,
        "credentials_returned": False,
        "provider_account_returned": False,
        "financial_values_returned": False,
        "network_access_performed": False,
        "external_actions_performed": False,
    }


def renew_connector_access_probe_receipt(
    runtime: BoxRuntime,
    request_path: str | Path,
    receipt_path: str | Path,
    *,
    allow_network: bool,
    environ: Mapping[str, str] | None = None,
    transport: HttpTransport = urllib_transport,
    sleeper: Callable[[float], None] = time.sleep,
    observed_at: str | None = None,
) -> dict[str, Any]:
    """Atomically replace a bound receipt after retaining its exact prior bytes."""
    if not allow_network:
        raise ConnectorAccessProbeError(
            "Connector access receipt renewal requires explicit network authorization"
        )
    raw_receipt = Path(receipt_path).expanduser()
    if not raw_receipt.is_absolute():
        raise ConnectorAccessProbeError(
            "Connector access receipt renewal requires an absolute receipt path"
        )
    try:
        raw_metadata = raw_receipt.lstat()
    except OSError as exc:
        raise ConnectorAccessProbeError(
            "Connector access receipt is unavailable"
        ) from exc
    if stat.S_ISLNK(raw_metadata.st_mode) or not stat.S_ISREG(raw_metadata.st_mode):
        raise ConnectorAccessProbeError(
            "Connector access receipt renewal requires a regular non-symlink file"
        )
    current = raw_receipt.resolve()
    marker = current.with_name(f".{current.name}.renewal-lock")
    marker_descriptor: int | None = None
    marker_created = False
    temporary: Path | None = None
    archive: Path | None = None
    archive_created = False
    try:
        try:
            marker_descriptor = os.open(
                marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600,
            )
        except FileExistsError as exc:
            raise ConnectorAccessProbeError(
                "Connector access receipt renewal is already in progress"
            ) from exc
        marker_created = True
        os.close(marker_descriptor)
        marker_descriptor = None
        if os.name != "nt":
            os.chmod(marker, 0o600)

        _, validated, old_receipt, old_fingerprint, _, _ = (
            _validate_private_connector_access_probe_receipt_contract(
                runtime, request_path, current,
            )
        )
        original_metadata = current.stat()
        archive = current.with_name(
            f"{current.stem}--superseded-{old_fingerprint[:12]}{current.suffix}"
        )
        if archive.exists() or archive.is_symlink():
            raise ConnectorAccessProbeError(
                "Connector access receipt superseded archive already exists"
            )

        result = run_connector_access_probe(
            runtime,
            request_path,
            allow_network=True,
            environ=environ,
            transport=transport,
            sleeper=sleeper,
            observed_at=observed_at,
        )
        if (
            result.get("status") != "passed"
            or result.get("summary", {}).get("ready_for_private_shadow_request") is not True
        ):
            raise ConnectorAccessProbeError(
                "Connector access receipt renewal probe did not pass all required checks"
            )
        new_receipt = _receipt_from_probe_result(result)
        new_fingerprint = new_receipt["receipt_fingerprint"]
        if new_fingerprint == old_fingerprint:
            raise ConnectorAccessProbeError(
                "Connector access receipt renewal must produce a new observation"
            )
        temporary = current.with_name(
            f".{current.name}.renewing-{new_fingerprint[:12]}.tmp"
        )
        _write_private_artifact(
            temporary,
            new_receipt,
            artifact_label="Connector access receipt renewal temporary",
        )
        verify_private_connector_access_probe_receipt(
            runtime,
            request_path,
            temporary,
            as_of=result["observed_at"],
            environ=environ,
        )
        _, _, _, latest_fingerprint, _, _ = (
            _validate_private_connector_access_probe_receipt_contract(
                runtime, request_path, current,
            )
        )
        latest_metadata = current.stat()
        if (
            latest_metadata.st_dev != original_metadata.st_dev
            or latest_metadata.st_ino != original_metadata.st_ino
            or latest_metadata.st_size != original_metadata.st_size
            or latest_metadata.st_mtime_ns != original_metadata.st_mtime_ns
            or latest_fingerprint != old_fingerprint
        ):
            raise ConnectorAccessProbeError(
                "Connector access receipt changed during renewal"
            )
        response = {
            **_receipt_write_summary(new_receipt),
            "renewed": True,
            "superseded_receipt_retained": True,
            "superseded_receipt_fingerprint": old_fingerprint,
            "renewal_atomic": True,
            "credential_rotation_supported": True,
            "archive_path_returned": False,
            "external_actions_performed": False,
        }
        os.link(current, archive)
        archive_created = True
        os.replace(temporary, current)
        temporary = None
        return response
    except Exception:
        if archive_created and archive is not None:
            try:
                archive.unlink()
            except OSError:
                pass
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass
        raise
    finally:
        if marker_descriptor is not None:
            os.close(marker_descriptor)
        if marker_created:
            try:
                marker.unlink()
            except OSError:
                pass
