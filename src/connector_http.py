from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable


class ConnectorHttpError(RuntimeError):
    """Sanitized API transport failure; never includes headers, credentials, or response bodies."""


@dataclass(frozen=True)
class HttpRequest:
    url: str
    headers: dict[str, str]
    timeout_seconds: float
    max_response_bytes: int
    method: str = "GET"
    body: bytes | None = None


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: dict[str, str]
    body: bytes


HttpTransport = Callable[[HttpRequest], HttpResponse]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def urllib_transport(request: HttpRequest) -> HttpResponse:
    opener = urllib.request.build_opener(_NoRedirect)
    raw = urllib.request.Request(
        request.url, data=request.body, headers=request.headers, method=request.method,
    )
    try:
        response = opener.open(raw, timeout=request.timeout_seconds)
    except urllib.error.HTTPError as exc:
        response = exc
    body = response.read(request.max_response_bytes + 1)
    if len(body) > request.max_response_bytes:
        raise ConnectorHttpError("API response exceeds configured byte limit")
    return HttpResponse(int(response.status), dict(response.headers.items()), body)


def _https_endpoint(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ConnectorHttpError("Connector endpoint must be a fixed HTTPS URL without credentials or fragments")
    return urllib.parse.urlunsplit(parsed)


def _cursor_url(endpoint: str, cursor: str | None, cursor_parameter: str) -> str:
    if not cursor:
        return endpoint
    parsed = urllib.parse.urlsplit(endpoint)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    query = [(key, value) for key, value in query if key != cursor_parameter]
    query.append((cursor_parameter, cursor))
    return urllib.parse.urlunsplit(parsed._replace(query=urllib.parse.urlencode(query)))


def _retry_delay(headers: dict[str, str] | None, attempt: int) -> tuple[float, bool]:
    fallback = float(min(2 ** (attempt - 1), 4))
    retry_after = next(
        (value for key, value in (headers or {}).items() if key.lower() == "retry-after"), None,
    )
    if retry_after is None:
        return fallback, False
    try:
        parsed = float(str(retry_after).strip())
    except ValueError:
        return fallback, False
    if parsed < 0:
        return fallback, False
    return min(parsed, 30.0), True


def fetch_paginated_json(
    endpoint: str,
    *,
    bearer_token: str,
    source_name: str,
    start_cursor: str | None = None,
    cursor_parameter: str = "cursor",
    next_cursor_field: str = "next_cursor",
    max_pages: int = 50,
    max_attempts: int = 3,
    timeout_seconds: float = 20,
    max_response_bytes: int = 10 * 1024 * 1024,
    transport: HttpTransport = urllib_transport,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    endpoint = _https_endpoint(endpoint)
    if not bearer_token:
        raise ConnectorHttpError("Connector credential is missing")
    if not source_name.strip():
        raise ConnectorHttpError("source_name is required")
    if not 1 <= max_pages <= 100 or not 1 <= max_attempts <= 5:
        raise ConnectorHttpError("pagination/retry limits are outside supported bounds")
    if not 1 <= timeout_seconds <= 60 or not 1024 <= max_response_bytes <= 25 * 1024 * 1024:
        raise ConnectorHttpError("timeout or response byte limit is outside supported bounds")

    pages: list[dict[str, Any]] = []
    cursor = str(start_cursor) if start_cursor else None
    seen_cursors: set[str] = set()
    retry_count = 0
    rate_limit_count = 0
    retry_delay_seconds_total = 0.0
    retry_after_honored = False
    for page_number in range(1, max_pages + 1):
        url = _cursor_url(endpoint, cursor, cursor_parameter)
        response = None
        for attempt in range(1, max_attempts + 1):
            try:
                response = transport(HttpRequest(
                    url=url,
                    headers={"Accept": "application/json", "Authorization": f"Bearer {bearer_token}"},
                    timeout_seconds=float(timeout_seconds),
                    max_response_bytes=max_response_bytes,
                ))
            except (TimeoutError, OSError, ConnectorHttpError) as exc:
                if attempt == max_attempts:
                    raise ConnectorHttpError(
                        f"API transport failed after {max_attempts} attempts on page {page_number}"
                    ) from exc
                retry_count += 1
                delay, honored = _retry_delay(None, attempt)
                retry_delay_seconds_total += delay
                retry_after_honored = retry_after_honored or honored
                sleeper(delay)
                continue
            if response.status == 429 or 500 <= response.status <= 599:
                if attempt == max_attempts:
                    raise ConnectorHttpError(
                        f"API returned retryable status {response.status} after {max_attempts} attempts"
                    )
                retry_count += 1
                if response.status == 429:
                    rate_limit_count += 1
                delay, honored = _retry_delay(response.headers, attempt)
                retry_delay_seconds_total += delay
                retry_after_honored = retry_after_honored or honored
                sleeper(delay)
                continue
            break
        if response is None or response.status != 200:
            status = response.status if response is not None else "unknown"
            raise ConnectorHttpError(f"API returned non-success status {status}")
        try:
            payload = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ConnectorHttpError(f"API returned invalid JSON on page {page_number}") from exc
        if not isinstance(payload, dict):
            raise ConnectorHttpError(f"API page {page_number} must be a JSON object")
        pages.append(payload)
        next_cursor = payload.get(next_cursor_field)
        if next_cursor in (None, ""):
            cursor = None
            break
        cursor = str(next_cursor)
        if cursor in seen_cursors:
            raise ConnectorHttpError("API pagination cursor repeated")
        seen_cursors.add(cursor)
    if cursor is not None:
        raise ConnectorHttpError(f"API pagination exceeded max_pages={max_pages}")
    canonical = json.dumps(pages, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {
        "batch_id": hashlib.sha256(f"{source_name}|{canonical}".encode()).hexdigest()[:24],
        "source_name": source_name,
        "pages": pages,
        "page_count": len(pages),
        "retry_count": retry_count,
        "rate_limit_count": rate_limit_count,
        "retry_delay_seconds_total": retry_delay_seconds_total,
        "retry_after_honored": retry_after_honored,
        "network_access_performed": True,
    }


def fetch_stripe_list_json(
    endpoint: str,
    *,
    restricted_key: str,
    api_version: str,
    stripe_account: str | None = None,
    parameters: dict[str, str | int] | None = None,
    start_cursor: str | None = None,
    max_pages: int = 50,
    max_attempts: int = 3,
    timeout_seconds: float = 20,
    max_response_bytes: int = 10 * 1024 * 1024,
    transport: HttpTransport = urllib_transport,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Fetch a Stripe v1 list endpoint with bounded `starting_after` pagination.

    The endpoint and parameters are expected to be supplied by connector code, not
    end users. Errors are deliberately sanitized so credentials and response bodies
    never reach Box output or logs.
    """
    endpoint = _https_endpoint(endpoint)
    parsed_endpoint = urllib.parse.urlsplit(endpoint)
    if parsed_endpoint.hostname != "api.stripe.com" or parsed_endpoint.query:
        raise ConnectorHttpError("Stripe connector requires a fixed api.stripe.com endpoint")
    if not restricted_key:
        raise ConnectorHttpError("Stripe restricted credential is missing")
    if not re.fullmatch(r"rk_(?:test|live)_[A-Za-z0-9_]{1,256}", restricted_key):
        raise ConnectorHttpError("Stripe connector requires an rk_ restricted credential")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}\.[a-z]+", api_version):
        raise ConnectorHttpError("Stripe API version is invalid")
    if stripe_account is not None and not re.fullmatch(r"acct_[A-Za-z0-9]{8,128}", stripe_account):
        raise ConnectorHttpError("Stripe connected account binding is invalid")
    if not 1 <= max_pages <= 100 or not 1 <= max_attempts <= 5:
        raise ConnectorHttpError("pagination/retry limits are outside supported bounds")
    if not 1 <= timeout_seconds <= 60 or not 1024 <= max_response_bytes <= 25 * 1024 * 1024:
        raise ConnectorHttpError("timeout or response byte limit is outside supported bounds")

    query_parameters: dict[str, str] = {}
    for key, value in (parameters or {}).items():
        if not isinstance(key, str) or not key or key.lower() in {
            "authorization", "api_key", "key", "token", "starting_after", "ending_before",
        }:
            raise ConnectorHttpError("Stripe query parameters contain a forbidden field")
        if not isinstance(value, (str, int)) or isinstance(value, bool):
            raise ConnectorHttpError("Stripe query parameter values must be strings or integers")
        query_parameters[key] = str(value)

    pages: list[dict[str, Any]] = []
    cursor = str(start_cursor) if start_cursor else None
    seen_cursors: set[str] = set()
    retry_count = 0
    rate_limit_count = 0
    retry_delay_seconds_total = 0.0
    retry_after_honored = False
    for page_number in range(1, max_pages + 1):
        query = dict(query_parameters)
        if cursor:
            query["starting_after"] = cursor
        url = urllib.parse.urlunsplit(parsed_endpoint._replace(query=urllib.parse.urlencode(query)))
        response = None
        for attempt in range(1, max_attempts + 1):
            try:
                headers = {
                    "Accept": "application/json",
                    "Authorization": f"Bearer {restricted_key}",
                    "Stripe-Version": api_version,
                }
                if stripe_account is not None:
                    headers["Stripe-Account"] = stripe_account
                response = transport(HttpRequest(
                    url=url,
                    headers=headers,
                    timeout_seconds=float(timeout_seconds),
                    max_response_bytes=max_response_bytes,
                ))
            except (TimeoutError, OSError, ConnectorHttpError) as exc:
                if attempt == max_attempts:
                    raise ConnectorHttpError(
                        f"Stripe transport failed after {max_attempts} attempts on page {page_number}"
                    ) from exc
                retry_count += 1
                delay, honored = _retry_delay(None, attempt)
                retry_delay_seconds_total += delay
                retry_after_honored = retry_after_honored or honored
                sleeper(delay)
                continue
            if response.status == 429 or 500 <= response.status <= 599:
                if attempt == max_attempts:
                    raise ConnectorHttpError(
                        f"Stripe returned retryable status {response.status} after {max_attempts} attempts"
                    )
                retry_count += 1
                if response.status == 429:
                    rate_limit_count += 1
                delay, honored = _retry_delay(response.headers, attempt)
                retry_delay_seconds_total += delay
                retry_after_honored = retry_after_honored or honored
                sleeper(delay)
                continue
            break
        if response is None or response.status != 200:
            status = response.status if response is not None else "unknown"
            raise ConnectorHttpError(f"Stripe returned non-success status {status}")
        try:
            payload = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ConnectorHttpError(f"Stripe returned invalid JSON on page {page_number}") from exc
        if (
            not isinstance(payload, dict)
            or payload.get("object") != "list"
            or not isinstance(payload.get("data"), list)
            or not isinstance(payload.get("has_more"), bool)
        ):
            raise ConnectorHttpError(f"Stripe page {page_number} violates the list response contract")
        pages.append(payload)
        if not payload["has_more"]:
            cursor = None
            break
        data = payload["data"]
        if not data or not isinstance(data[-1], dict) or not str(data[-1].get("id") or ""):
            raise ConnectorHttpError("Stripe pagination requires a final object id")
        cursor = str(data[-1]["id"])
        if cursor in seen_cursors:
            raise ConnectorHttpError("Stripe pagination cursor repeated")
        seen_cursors.add(cursor)
    if cursor is not None:
        raise ConnectorHttpError(f"Stripe pagination exceeded max_pages={max_pages}")
    canonical = json.dumps(pages, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {
        "batch_id": hashlib.sha256(f"stripe|{api_version}|{canonical}".encode()).hexdigest()[:24],
        "pages": pages,
        "page_count": len(pages),
        "retry_count": retry_count,
        "rate_limit_count": rate_limit_count,
        "retry_delay_seconds_total": retry_delay_seconds_total,
        "retry_after_honored": retry_after_honored,
        "network_access_performed": True,
        "api_version": api_version,
    }


def fetch_shopify_graphql_orders(
    shop_domain: str,
    *,
    access_token: str,
    api_version: str,
    query: str,
    search_query: str,
    start_cursor: str | None = None,
    page_size: int = 100,
    max_pages: int = 50,
    max_attempts: int = 3,
    timeout_seconds: float = 20,
    max_response_bytes: int = 10 * 1024 * 1024,
    transport: HttpTransport = urllib_transport,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Fetch Shopify Admin GraphQL orders using a fixed versioned endpoint and bounded cursors."""
    domain = str(shop_domain or "").lower()
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.myshopify\.com", domain):
        raise ConnectorHttpError("Shopify shop_domain must be one store subdomain under myshopify.com")
    if not access_token:
        raise ConnectorHttpError("Shopify Admin credential is missing")
    if not re.fullmatch(r"\d{4}-(?:01|04|07|10)", api_version):
        raise ConnectorHttpError("Shopify API version is invalid")
    if not isinstance(query, str) or "query" not in query or "orders" not in query:
        raise ConnectorHttpError("Shopify GraphQL document is invalid")
    if not isinstance(search_query, str) or len(search_query) > 1000:
        raise ConnectorHttpError("Shopify order search query is invalid")
    if not 1 <= page_size <= 250 or not 1 <= max_pages <= 100 or not 1 <= max_attempts <= 5:
        raise ConnectorHttpError("Shopify pagination/retry limits are outside supported bounds")
    if not 1 <= timeout_seconds <= 60 or not 1024 <= max_response_bytes <= 25 * 1024 * 1024:
        raise ConnectorHttpError("timeout or response byte limit is outside supported bounds")

    endpoint = f"https://{domain}/admin/api/{api_version}/graphql.json"
    pages: list[dict[str, Any]] = []
    cursor = str(start_cursor) if start_cursor else None
    seen_cursors: set[str] = set()
    retry_count = 0
    rate_limit_count = 0
    retry_delay_seconds_total = 0.0
    retry_after_honored = False
    for page_number in range(1, max_pages + 1):
        body = json.dumps({
            "query": query,
            "variables": {"first": page_size, "after": cursor, "query": search_query},
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        response = None
        for attempt in range(1, max_attempts + 1):
            try:
                response = transport(HttpRequest(
                    url=endpoint,
                    headers={
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                        "X-Shopify-Access-Token": access_token,
                    },
                    timeout_seconds=float(timeout_seconds),
                    max_response_bytes=max_response_bytes,
                    method="POST",
                    body=body,
                ))
            except (TimeoutError, OSError, ConnectorHttpError) as exc:
                if attempt == max_attempts:
                    raise ConnectorHttpError(
                        f"Shopify transport failed after {max_attempts} attempts on page {page_number}"
                    ) from exc
                retry_count += 1
                delay, honored = _retry_delay(None, attempt)
                retry_delay_seconds_total += delay
                retry_after_honored = retry_after_honored or honored
                sleeper(delay)
                continue
            if response.status == 429 or 500 <= response.status <= 599:
                if attempt == max_attempts:
                    raise ConnectorHttpError(
                        f"Shopify returned retryable status {response.status} after {max_attempts} attempts"
                    )
                retry_count += 1
                if response.status == 429:
                    rate_limit_count += 1
                delay, honored = _retry_delay(response.headers, attempt)
                retry_delay_seconds_total += delay
                retry_after_honored = retry_after_honored or honored
                sleeper(delay)
                continue
            break
        if response is None or response.status != 200:
            status = response.status if response is not None else "unknown"
            raise ConnectorHttpError(f"Shopify returned non-success status {status}")
        response_version = next(
            (value for key, value in response.headers.items() if key.lower() == "x-shopify-api-version"),
            None,
        )
        if response_version is not None and response_version != api_version:
            raise ConnectorHttpError("Shopify fulfilled the request with a different API version")
        try:
            payload = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ConnectorHttpError(f"Shopify returned invalid JSON on page {page_number}") from exc
        if not isinstance(payload, dict) or payload.get("errors"):
            raise ConnectorHttpError(f"Shopify GraphQL returned errors on page {page_number}")
        orders = (payload.get("data") or {}).get("orders") if isinstance(payload.get("data"), dict) else None
        if not isinstance(orders, dict) or not isinstance(orders.get("nodes"), list):
            raise ConnectorHttpError(f"Shopify page {page_number} violates the orders connection contract")
        page_info = orders.get("pageInfo")
        if not isinstance(page_info, dict) or not isinstance(page_info.get("hasNextPage"), bool):
            raise ConnectorHttpError(f"Shopify page {page_number} has invalid pageInfo")
        pages.append(payload)
        if not page_info["hasNextPage"]:
            cursor = None
            break
        next_cursor = page_info.get("endCursor")
        if not isinstance(next_cursor, str) or not next_cursor:
            raise ConnectorHttpError("Shopify pagination requires endCursor")
        cursor = next_cursor
        if cursor in seen_cursors:
            raise ConnectorHttpError("Shopify pagination cursor repeated")
        seen_cursors.add(cursor)
    if cursor is not None:
        raise ConnectorHttpError(f"Shopify pagination exceeded max_pages={max_pages}")
    canonical = json.dumps(pages, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {
        "batch_id": hashlib.sha256(f"shopify|{domain}|{api_version}|{canonical}".encode()).hexdigest()[:24],
        "pages": pages,
        "page_count": len(pages),
        "retry_count": retry_count,
        "rate_limit_count": rate_limit_count,
        "retry_delay_seconds_total": retry_delay_seconds_total,
        "retry_after_honored": retry_after_honored,
        "network_access_performed": True,
        "api_version": api_version,
        "shop_domain": domain,
    }


def fetch_xero_accounting_json(
    resource: str,
    *,
    access_token: str,
    tenant_id: str,
    parameters: dict[str, str] | None = None,
    max_attempts: int = 3,
    timeout_seconds: float = 20,
    max_response_bytes: int = 10 * 1024 * 1024,
    transport: HttpTransport = urllib_transport,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Fetch one fixed Xero Accounting API resource with bounded retries.

    Connector code chooses the resource. User requests can provide only validated
    report parameters, never a URL, tenant header, or credential.
    """
    endpoints = {
        "organisation": "https://api.xero.com/api.xro/2.0/Organisation",
        "trial_balance": "https://api.xero.com/api.xro/2.0/Reports/TrialBalance",
    }
    try:
        endpoint = endpoints[resource]
    except KeyError as exc:
        raise ConnectorHttpError("Xero resource is not allowed") from exc
    if not access_token:
        raise ConnectorHttpError("Xero credential is missing")
    if not re.fullmatch(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
        tenant_id,
    ):
        raise ConnectorHttpError("Xero tenant binding is invalid")
    if not 1 <= max_attempts <= 5:
        raise ConnectorHttpError("Xero retry limit is outside supported bounds")
    if not 1 <= timeout_seconds <= 60 or not 1024 <= max_response_bytes <= 25 * 1024 * 1024:
        raise ConnectorHttpError("timeout or response byte limit is outside supported bounds")

    allowed_parameters = {"date", "paymentsOnly"} if resource == "trial_balance" else set()
    query: dict[str, str] = {}
    for key, value in (parameters or {}).items():
        if key not in allowed_parameters or not isinstance(value, str):
            raise ConnectorHttpError("Xero request parameters contain a forbidden field")
        query[key] = value
    if resource == "trial_balance":
        if not re.fullmatch(r"20\d{2}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])", query.get("date", "")):
            raise ConnectorHttpError("Xero Trial Balance Date must use YYYY-MM-DD")
        try:
            time.strptime(query["date"], "%Y-%m-%d")
        except ValueError as exc:
            raise ConnectorHttpError("Xero Trial Balance Date must be a real calendar date") from exc
        if query.get("paymentsOnly") not in {None, "true", "false"}:
            raise ConnectorHttpError("Xero paymentsOnly must be true or false")
    url = endpoint
    if query:
        url = f"{endpoint}?{urllib.parse.urlencode(query)}"

    retry_count = 0
    rate_limit_count = 0
    retry_delay_seconds_total = 0.0
    retry_after_honored = False
    response = None
    for attempt in range(1, max_attempts + 1):
        try:
            response = transport(HttpRequest(
                url=url,
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {access_token}",
                    "xero-tenant-id": tenant_id,
                },
                timeout_seconds=float(timeout_seconds),
                max_response_bytes=max_response_bytes,
            ))
        except (TimeoutError, OSError, ConnectorHttpError) as exc:
            if attempt == max_attempts:
                raise ConnectorHttpError(
                    f"Xero transport failed after {max_attempts} attempts for {resource}"
                ) from exc
            retry_count += 1
            delay, honored = _retry_delay(None, attempt)
            retry_delay_seconds_total += delay
            retry_after_honored = retry_after_honored or honored
            sleeper(delay)
            continue
        if response.status == 429 or 500 <= response.status <= 599:
            if attempt == max_attempts:
                raise ConnectorHttpError(
                    f"Xero returned retryable status {response.status} after {max_attempts} attempts"
                )
            retry_count += 1
            if response.status == 429:
                rate_limit_count += 1
            delay, honored = _retry_delay(response.headers, attempt)
            retry_delay_seconds_total += delay
            retry_after_honored = retry_after_honored or honored
            sleeper(delay)
            continue
        break
    if response is None or response.status != 200:
        status = response.status if response is not None else "unknown"
        raise ConnectorHttpError(f"Xero returned non-success status {status}")
    try:
        payload = json.loads(response.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConnectorHttpError(f"Xero returned invalid JSON for {resource}") from exc
    if not isinstance(payload, dict):
        raise ConnectorHttpError(f"Xero {resource} response must be a JSON object")
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {
        "request_id": hashlib.sha256(f"xero|{resource}|{tenant_id}|{canonical}".encode()).hexdigest()[:24],
        "resource": resource,
        "payload": payload,
        "retry_count": retry_count,
        "rate_limit_count": rate_limit_count,
        "retry_delay_seconds_total": retry_delay_seconds_total,
        "retry_after_honored": retry_after_honored,
        "network_access_performed": True,
    }


def fetch_wise_business_json(
    resource: str,
    *,
    access_token: str,
    profile_id: int,
    balance_id: int | None = None,
    parameters: dict[str, str] | None = None,
    max_attempts: int = 3,
    timeout_seconds: float = 20,
    max_response_bytes: int = 10 * 1024 * 1024,
    transport: HttpTransport = urllib_transport,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Fetch one fixed Wise Business resource using the pinned 2026Q3 contract.

    Profile and balance identifiers come from an operator-managed entity binding.
    The caller can supply only the statement's validated currency and time window;
    arbitrary URLs, account identifiers and authentication material are impossible.
    """
    if not access_token:
        raise ConnectorHttpError("Wise credential is missing")
    if isinstance(profile_id, bool) or not isinstance(profile_id, int) or profile_id <= 0:
        raise ConnectorHttpError("Wise profile binding is invalid")
    if balance_id is not None and (
        isinstance(balance_id, bool) or not isinstance(balance_id, int) or balance_id <= 0
    ):
        raise ConnectorHttpError("Wise balance binding is invalid")
    if not 1 <= max_attempts <= 5:
        raise ConnectorHttpError("Wise retry limit is outside supported bounds")
    if not 1 <= timeout_seconds <= 60 or not 1024 <= max_response_bytes <= 25 * 1024 * 1024:
        raise ConnectorHttpError("timeout or response byte limit is outside supported bounds")

    base = f"https://api.wise.com/2026Q3/profiles/{profile_id}"
    if resource == "profile":
        if balance_id is not None or parameters:
            raise ConnectorHttpError("Wise profile request contains forbidden fields")
        endpoint = base
        allowed_parameters: set[str] = set()
    elif resource == "balance":
        if balance_id is None or parameters:
            raise ConnectorHttpError("Wise balance request requires only a bound balance id")
        endpoint = f"{base}/balances/{balance_id}"
        allowed_parameters = set()
    elif resource == "balance_statement":
        if balance_id is None:
            raise ConnectorHttpError("Wise balance statement requires a bound balance id")
        endpoint = f"{base}/balance-statements/{balance_id}/statement.json"
        allowed_parameters = {
            "currency", "intervalStart", "intervalEnd", "type", "statementLocale",
        }
    else:
        raise ConnectorHttpError("Wise resource is not allowed")

    query: dict[str, str] = {}
    for key, value in (parameters or {}).items():
        if key not in allowed_parameters or not isinstance(value, str):
            raise ConnectorHttpError("Wise request parameters contain a forbidden field")
        query[key] = value
    if resource == "balance_statement":
        if set(query) != allowed_parameters:
            raise ConnectorHttpError("Wise balance statement parameters are incomplete")
        if not re.fullmatch(r"[A-Z]{3}", query["currency"]):
            raise ConnectorHttpError("Wise statement currency is invalid")
        if query["type"] != "COMPACT" or query["statementLocale"] != "en":
            raise ConnectorHttpError("Wise statement format must be COMPACT English JSON")
        for field in ("intervalStart", "intervalEnd"):
            try:
                parsed = time.strptime(query[field], "%Y-%m-%dT%H:%M:%SZ")
            except ValueError as exc:
                raise ConnectorHttpError(
                    f"Wise {field} must use UTC YYYY-MM-DDTHH:MM:SSZ"
                ) from exc
            if time.strftime("%Y-%m-%dT%H:%M:%SZ", parsed) != query[field]:
                raise ConnectorHttpError(f"Wise {field} is not canonical UTC")

    url = endpoint if not query else f"{endpoint}?{urllib.parse.urlencode(query)}"
    retry_count = 0
    rate_limit_count = 0
    retry_delay_seconds_total = 0.0
    retry_after_honored = False
    response = None
    for attempt in range(1, max_attempts + 1):
        try:
            response = transport(HttpRequest(
                url=url,
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {access_token}",
                },
                timeout_seconds=float(timeout_seconds),
                max_response_bytes=max_response_bytes,
            ))
        except (TimeoutError, OSError, ConnectorHttpError) as exc:
            if attempt == max_attempts:
                raise ConnectorHttpError(
                    f"Wise transport failed after {max_attempts} attempts for {resource}"
                ) from exc
            retry_count += 1
            delay, honored = _retry_delay(None, attempt)
            retry_delay_seconds_total += delay
            retry_after_honored = retry_after_honored or honored
            sleeper(delay)
            continue
        if response.status == 429 or 500 <= response.status <= 599:
            if attempt == max_attempts:
                raise ConnectorHttpError(
                    f"Wise returned retryable status {response.status} after {max_attempts} attempts"
                )
            retry_count += 1
            if response.status == 429:
                rate_limit_count += 1
            delay, honored = _retry_delay(response.headers, attempt)
            retry_delay_seconds_total += delay
            retry_after_honored = retry_after_honored or honored
            sleeper(delay)
            continue
        break
    if response is None or response.status != 200:
        status = response.status if response is not None else "unknown"
        if status == 403:
            raise ConnectorHttpError(
                "Wise access was denied; complete required SCA outside the Box or review the approved access contract"
            )
        raise ConnectorHttpError(f"Wise returned non-success status {status}")
    try:
        payload = json.loads(response.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConnectorHttpError(f"Wise returned invalid JSON for {resource}") from exc
    if not isinstance(payload, dict):
        raise ConnectorHttpError(f"Wise {resource} response must be a JSON object")
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {
        "request_id": hashlib.sha256(
            f"wise|2026Q3|{resource}|{profile_id}|{balance_id}|{canonical}".encode()
        ).hexdigest()[:24],
        "resource": resource,
        "payload": payload,
        "retry_count": retry_count,
        "rate_limit_count": rate_limit_count,
        "retry_delay_seconds_total": retry_delay_seconds_total,
        "retry_after_honored": retry_after_honored,
        "network_access_performed": True,
        "api_version": "2026Q3",
    }


def fetch_shipbob_fulfillment_pages(
    *,
    access_token: str,
    api_version: str,
    environment: str,
    interval_start: str,
    interval_end: str,
    channel_id: int | None = None,
    max_pages: int = 50,
    page_size: int = 100,
    max_attempts: int = 3,
    timeout_seconds: float = 20,
    max_response_bytes: int = 10 * 1024 * 1024,
    transport: HttpTransport = urllib_transport,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Read ShipBob order/shipment and return evidence from fixed 2026-07 endpoints.

    Pagination URLs are built locally instead of following response links. This keeps
    every request on the selected ShipBob host and makes the network boundary auditable.
    """
    if not access_token:
        raise ConnectorHttpError("ShipBob credential is missing")
    if api_version != "2026-07":
        raise ConnectorHttpError("ShipBob connector requires API version 2026-07")
    if channel_id is not None and (
        not isinstance(channel_id, int)
        or isinstance(channel_id, bool)
        or not 1 <= channel_id <= 2_147_483_647
    ):
        raise ConnectorHttpError("ShipBob channel_id must be a positive int32")
    hosts = {
        "production": "api.shipbob.com",
        "sandbox": "sandbox-api.shipbob.com",
    }
    try:
        host = hosts[environment]
    except KeyError as exc:
        raise ConnectorHttpError("ShipBob environment must be production or sandbox") from exc
    if not 1 <= max_pages <= 100 or not 1 <= page_size <= 250 or not 1 <= max_attempts <= 5:
        raise ConnectorHttpError("ShipBob pagination/retry limits are outside supported bounds")
    if not 1 <= timeout_seconds <= 60 or not 1024 <= max_response_bytes <= 25 * 1024 * 1024:
        raise ConnectorHttpError("ShipBob timeout or response byte limit is outside supported bounds")

    retry_count = 0
    rate_limit_count = 0
    retry_delay_seconds_total = 0.0
    retry_after_honored = False

    def request_json(resource: str, page: int) -> tuple[Any, dict[str, str]]:
        nonlocal retry_count, rate_limit_count
        nonlocal retry_delay_seconds_total, retry_after_honored
        if resource == "orders":
            path = "order"
            parameters = {
                "Page": page,
                "Limit": page_size,
                "StartDate": interval_start,
                "EndDate": interval_end,
                "SortOrder": "Oldest",
            }
        elif resource == "returns":
            path = "return"
            parameters = {
                "Cursor": page,
                "Limit": page_size,
                "StartDate": interval_start,
                "EndDate": interval_end,
                "SortOrder": "Asc",
            }
        else:  # pragma: no cover - internal call contract
            raise ConnectorHttpError("Unsupported ShipBob resource")
        url = f"https://{host}/{api_version}/{path}?{urllib.parse.urlencode(parameters)}"
        response = None
        for attempt in range(1, max_attempts + 1):
            try:
                headers = {
                    "Accept": "application/json",
                    "Authorization": f"Bearer {access_token}",
                }
                if channel_id is not None:
                    headers["shipbob_channel_id"] = str(channel_id)
                response = transport(HttpRequest(
                    url=url,
                    headers=headers,
                    timeout_seconds=float(timeout_seconds),
                    max_response_bytes=max_response_bytes,
                ))
            except (TimeoutError, OSError, ConnectorHttpError) as exc:
                if attempt == max_attempts:
                    raise ConnectorHttpError(
                        f"ShipBob transport failed after {max_attempts} attempts for {resource} page {page}"
                    ) from exc
                retry_count += 1
                delay, honored = _retry_delay(None, attempt)
                retry_delay_seconds_total += delay
                retry_after_honored = retry_after_honored or honored
                sleeper(delay)
                continue
            if response.status == 429 or 500 <= response.status <= 599:
                if attempt == max_attempts:
                    raise ConnectorHttpError(
                        f"ShipBob returned retryable status {response.status} after {max_attempts} attempts"
                    )
                retry_count += 1
                if response.status == 429:
                    rate_limit_count += 1
                delay, honored = _retry_delay(response.headers, attempt)
                retry_delay_seconds_total += delay
                retry_after_honored = retry_after_honored or honored
                sleeper(delay)
                continue
            break
        if response is None or response.status != 200:
            status = response.status if response is not None else "unknown"
            raise ConnectorHttpError(f"ShipBob returned non-success status {status} for {resource}")
        try:
            payload = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ConnectorHttpError(f"ShipBob returned invalid JSON for {resource} page {page}") from exc
        return payload, response.headers

    order_pages: list[list[dict[str, Any]]] = []
    for page in range(1, max_pages + 1):
        payload, headers = request_json("orders", page)
        if not isinstance(payload, list) or any(not isinstance(item, dict) for item in payload):
            raise ConnectorHttpError(f"ShipBob orders page {page} must be an object list")
        order_pages.append(payload)
        total_pages_value = next(
            (value for key, value in headers.items() if key.lower() == "total-pages"), None,
        )
        if total_pages_value is not None:
            try:
                total_pages = int(str(total_pages_value))
            except ValueError as exc:
                raise ConnectorHttpError("ShipBob Total-Pages response header is invalid") from exc
            if total_pages < page or total_pages > max_pages:
                raise ConnectorHttpError("ShipBob order pagination exceeds configured max_pages")
            if page == total_pages:
                break
        elif len(payload) < page_size:
            break
    else:
        raise ConnectorHttpError(f"ShipBob order pagination exceeded max_pages={max_pages}")

    return_pages: list[list[dict[str, Any]]] = []
    for page in range(1, max_pages + 1):
        payload, _headers = request_json("returns", page)
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list) or any(
            not isinstance(item, dict) for item in payload.get("items") or []
        ):
            raise ConnectorHttpError(f"ShipBob returns page {page} violates the pagination contract")
        items = payload["items"]
        return_pages.append(items)
        next_cursor_value = payload.get("next")
        if next_cursor_value in (None, ""):
            break
        if not isinstance(next_cursor_value, str):
            raise ConnectorHttpError("ShipBob returns pagination supplied an invalid next-page signal")
        if not items:
            raise ConnectorHttpError("ShipBob returns pagination supplied next with an empty page")
    else:
        raise ConnectorHttpError(f"ShipBob return pagination exceeded max_pages={max_pages}")

    canonical = json.dumps(
        {"orders": order_pages, "returns": return_pages},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return {
        "batch_id": hashlib.sha256(
            f"shipbob|{api_version}|{environment}|{interval_start}|{interval_end}|{canonical}".encode()
        ).hexdigest()[:24],
        "order_pages": order_pages,
        "return_pages": return_pages,
        "order_page_count": len(order_pages),
        "return_page_count": len(return_pages),
        "retry_count": retry_count,
        "rate_limit_count": rate_limit_count,
        "retry_delay_seconds_total": retry_delay_seconds_total,
        "retry_after_honored": retry_after_honored,
        "network_access_performed": True,
        "api_version": api_version,
        "environment": environment,
        "channel_header_used": channel_id is not None,
    }


def fetch_paypal_transaction_pages(
    *,
    client_id: str,
    client_secret: str,
    environment: str,
    interval_start: str,
    interval_end: str,
    page_size: int = 500,
    max_pages: int = 20,
    max_attempts: int = 3,
    timeout_seconds: float = 20,
    max_response_bytes: int = 10 * 1024 * 1024,
    transport: HttpTransport = urllib_transport,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Exchange OAuth credentials in memory and fetch PayPal balance-affecting activity.

    Only the fixed Transaction Search endpoint is used. HATEOAS links in responses
    are ignored; pagination is reconstructed locally from bounded integer pages.
    Credentials, access tokens and response bodies never enter returned metadata or
    sanitized errors.
    """
    if environment not in {"production", "sandbox"}:
        raise ConnectorHttpError("PayPal environment must be production or sandbox")
    if (
        not isinstance(client_id, str) or not client_id or len(client_id) > 512
        or ":" in client_id or any(ord(character) < 33 or ord(character) == 127 for character in client_id)
    ):
        raise ConnectorHttpError("PayPal client credential is missing or invalid")
    if (
        not isinstance(client_secret, str) or not client_secret or len(client_secret) > 2048
        or any(ord(character) < 33 or ord(character) == 127 for character in client_secret)
    ):
        raise ConnectorHttpError("PayPal secret credential is missing or invalid")
    if not 1 <= page_size <= 500 or not 1 <= max_pages <= 20 or not 1 <= max_attempts <= 5:
        raise ConnectorHttpError("PayPal pagination/retry limits are outside supported bounds")
    if not 1 <= timeout_seconds <= 60 or not 1024 <= max_response_bytes <= 25 * 1024 * 1024:
        raise ConnectorHttpError("PayPal timeout or response byte limit is outside supported bounds")
    timestamp_pattern = r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?(?:Z|[+-][0-9]{2}:[0-9]{2})"
    if not re.fullmatch(timestamp_pattern, interval_start) or not re.fullmatch(
        timestamp_pattern, interval_end,
    ):
        raise ConnectorHttpError("PayPal interval must use timezone-aware ISO-8601 timestamps with seconds")

    host = "api-m.paypal.com" if environment == "production" else "api-m.sandbox.paypal.com"
    retry_count = 0
    rate_limit_count = 0
    retry_delay_seconds_total = 0.0
    retry_after_honored = False

    def request_json(request: HttpRequest, *, operation: str) -> dict[str, Any]:
        nonlocal retry_count, rate_limit_count
        nonlocal retry_delay_seconds_total, retry_after_honored
        response = None
        for attempt in range(1, max_attempts + 1):
            try:
                response = transport(request)
            except (TimeoutError, OSError, ConnectorHttpError) as exc:
                if attempt == max_attempts:
                    raise ConnectorHttpError(
                        f"PayPal transport failed after {max_attempts} attempts for {operation}"
                    ) from exc
                retry_count += 1
                delay, honored = _retry_delay(None, attempt)
                retry_delay_seconds_total += delay
                retry_after_honored = retry_after_honored or honored
                sleeper(delay)
                continue
            if response.status == 429 or 500 <= response.status <= 599:
                if attempt == max_attempts:
                    raise ConnectorHttpError(
                        f"PayPal returned retryable status {response.status} after {max_attempts} attempts"
                    )
                retry_count += 1
                if response.status == 429:
                    rate_limit_count += 1
                delay, honored = _retry_delay(response.headers, attempt)
                retry_delay_seconds_total += delay
                retry_after_honored = retry_after_honored or honored
                sleeper(delay)
                continue
            break
        if response is None or response.status != 200:
            status = response.status if response is not None else "unknown"
            raise ConnectorHttpError(f"PayPal returned non-success status {status} for {operation}")
        try:
            payload = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ConnectorHttpError(f"PayPal returned invalid JSON for {operation}") from exc
        if not isinstance(payload, dict):
            raise ConnectorHttpError(f"PayPal {operation} response must be a JSON object")
        return payload

    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode("ascii")
    token_payload = request_json(HttpRequest(
        url=f"https://{host}/v1/oauth2/token",
        method="POST",
        body=b"grant_type=client_credentials",
        headers={
            "Accept": "application/json",
            "Accept-Language": "en_US",
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        timeout_seconds=float(timeout_seconds),
        max_response_bytes=max_response_bytes,
    ), operation="OAuth token exchange")
    access_token = token_payload.get("access_token")
    if (
        not isinstance(access_token, str) or not access_token or len(access_token) > 8192
        or str(token_payload.get("token_type") or "").lower() != "bearer"
    ):
        raise ConnectorHttpError("PayPal OAuth response does not contain a valid bearer token")

    pages: list[dict[str, Any]] = []
    total_items: int | None = None
    total_pages: int | None = None
    for page in range(1, max_pages + 1):
        query = urllib.parse.urlencode({
            "start_date": interval_start,
            "end_date": interval_end,
            "fields": "transaction_info",
            "balance_affecting_records_only": "Y",
            "page_size": page_size,
            "page": page,
        })
        payload = request_json(HttpRequest(
            url=f"https://{host}/v1/reporting/transactions?{query}",
            headers={
                "Accept": "application/json",
                "Accept-Language": "en_US",
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
                "PayPal-Enforce-ISO8601-Format": "true",
            },
            timeout_seconds=float(timeout_seconds),
            max_response_bytes=max_response_bytes,
        ), operation=f"transaction page {page}")
        rows = payload.get("transaction_details")
        current_total_items = payload.get("total_items")
        current_total_pages = payload.get("total_pages")
        if (
            not isinstance(rows, list) or any(not isinstance(item, dict) for item in rows)
            or not isinstance(current_total_items, int) or isinstance(current_total_items, bool)
            or not isinstance(current_total_pages, int) or isinstance(current_total_pages, bool)
            or current_total_items < 0 or current_total_pages < 0
        ):
            raise ConnectorHttpError(f"PayPal transaction page {page} violates the search response contract")
        if current_total_items > 10_000:
            raise ConnectorHttpError("PayPal transaction range exceeds 10,000 records; shorten the interval")
        if current_total_pages > max_pages:
            raise ConnectorHttpError("PayPal transaction pagination exceeds configured max_pages")
        if total_items is None:
            total_items, total_pages = current_total_items, current_total_pages
        elif (current_total_items, current_total_pages) != (total_items, total_pages):
            raise ConnectorHttpError("PayPal transaction pagination totals changed during the read")
        pages.append(payload)
        if current_total_pages == 0 or page >= current_total_pages:
            break
        if not rows:
            raise ConnectorHttpError("PayPal transaction pagination returned an empty intermediate page")
    else:
        raise ConnectorHttpError(f"PayPal transaction pagination exceeded max_pages={max_pages}")

    canonical = json.dumps(pages, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {
        "batch_id": hashlib.sha256(
            f"paypal|transaction-search-v1|{environment}|{interval_start}|{interval_end}|{canonical}".encode()
        ).hexdigest()[:24],
        "pages": pages,
        "page_count": len(pages),
        "total_items": total_items or 0,
        "retry_count": retry_count,
        "rate_limit_count": rate_limit_count,
        "retry_delay_seconds_total": retry_delay_seconds_total,
        "retry_after_honored": retry_after_honored,
        "network_access_performed": True,
        "oauth_token_exchange_performed": True,
        "api_contract": "transaction-search-v1",
        "environment": environment,
    }


def fetch_amazon_seller_transaction_pages(
    *,
    client_id: str,
    client_secret: str,
    refresh_token: str,
    region: str,
    environment: str,
    marketplace_id: str,
    posted_after: str,
    posted_before: str,
    transaction_status: str | None = None,
    max_pages: int = 20,
    max_attempts: int = 3,
    timeout_seconds: float = 20,
    max_response_bytes: int = 10 * 1024 * 1024,
    transport: HttpTransport = urllib_transport,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Fetch Amazon SP-API Finances v2024-06-19 transactions.

    LWA credentials are exchanged in memory. The SP-API host is selected from a
    closed regional map and every pagination request is rebuilt locally against
    the same listTransactions path; response links are never followed.
    """
    region = str(region or "").upper()
    environment = str(environment or "").lower()
    hosts = {
        "NA": "sellingpartnerapi-na.amazon.com",
        "EU": "sellingpartnerapi-eu.amazon.com",
        "FE": "sellingpartnerapi-fe.amazon.com",
    }
    if region not in hosts or environment not in {"production", "sandbox"}:
        raise ConnectorHttpError("Amazon Seller region/environment is outside the supported fixed endpoints")
    host = hosts[region]
    if environment == "sandbox":
        host = f"sandbox.{host}"
    credentials = (
        (client_id, 512, "client ID"),
        (client_secret, 2048, "client secret"),
        (refresh_token, 8192, "refresh token"),
    )
    for value, maximum, label in credentials:
        if (
            not isinstance(value, str) or not value or len(value) > maximum
            or any(ord(character) < 33 or ord(character) > 126 for character in value)
        ):
            raise ConnectorHttpError(f"Amazon Seller {label} is missing or invalid")
    if not re.fullmatch(r"[A-Z0-9]{6,32}", str(marketplace_id or "")):
        raise ConnectorHttpError("Amazon Seller marketplace ID is invalid")
    if transaction_status not in {None, "RELEASED", "DEFERRED", "DEFERRED_RELEASED"}:
        raise ConnectorHttpError("Amazon Seller transaction status is invalid")
    if not 1 <= max_pages <= 20 or not 1 <= max_attempts <= 5:
        raise ConnectorHttpError("Amazon Seller pagination/retry limits are outside supported bounds")
    if not 1 <= timeout_seconds <= 60 or not 1024 <= max_response_bytes <= 25 * 1024 * 1024:
        raise ConnectorHttpError("Amazon Seller timeout or response byte limit is outside supported bounds")
    timestamp_pattern = r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?(?:Z|[+-][0-9]{2}:[0-9]{2})"
    if not re.fullmatch(timestamp_pattern, posted_after) or not re.fullmatch(
        timestamp_pattern, posted_before,
    ):
        raise ConnectorHttpError("Amazon Seller interval must use timezone-aware ISO-8601 timestamps")
    try:
        start = datetime.fromisoformat(posted_after.replace("Z", "+00:00"))
        end = datetime.fromisoformat(posted_before.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ConnectorHttpError("Amazon Seller interval is invalid") from exc
    if start >= end or (end - start).total_seconds() > 180 * 86400:
        raise ConnectorHttpError("Amazon Seller interval must be positive and no longer than 180 days")

    retry_count = 0
    rate_limit_count = 0
    retry_delay_seconds_total = 0.0
    retry_after_honored = False

    def request_json(request: HttpRequest, *, operation: str) -> dict[str, Any]:
        nonlocal retry_count, rate_limit_count
        nonlocal retry_delay_seconds_total, retry_after_honored
        response = None
        for attempt in range(1, max_attempts + 1):
            try:
                response = transport(request)
            except (TimeoutError, OSError, ConnectorHttpError) as exc:
                if attempt == max_attempts:
                    raise ConnectorHttpError(
                        f"Amazon Seller transport failed after {max_attempts} attempts for {operation}"
                    ) from exc
                retry_count += 1
                delay, honored = _retry_delay(None, attempt)
                retry_delay_seconds_total += delay
                retry_after_honored = retry_after_honored or honored
                sleeper(delay)
                continue
            if response.status == 429 or 500 <= response.status <= 599:
                if attempt == max_attempts:
                    raise ConnectorHttpError(
                        f"Amazon Seller returned retryable status {response.status} after {max_attempts} attempts"
                    )
                retry_count += 1
                if response.status == 429:
                    rate_limit_count += 1
                delay, honored = _retry_delay(response.headers, attempt)
                retry_delay_seconds_total += delay
                retry_after_honored = retry_after_honored or honored
                sleeper(delay)
                continue
            break
        if response is None or response.status != 200:
            status = response.status if response is not None else "unknown"
            raise ConnectorHttpError(f"Amazon Seller returned non-success status {status} for {operation}")
        try:
            payload = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ConnectorHttpError(f"Amazon Seller returned invalid JSON for {operation}") from exc
        if not isinstance(payload, dict):
            raise ConnectorHttpError(f"Amazon Seller {operation} response must be a JSON object")
        return payload

    token_body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
        "client_secret": client_secret,
    }).encode("ascii")
    token_payload = request_json(HttpRequest(
        url="https://api.amazon.com/auth/o2/token",
        method="POST",
        body=token_body,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "OPC-Finance-Box/0.1",
        },
        timeout_seconds=float(timeout_seconds),
        max_response_bytes=max_response_bytes,
    ), operation="LWA token exchange")
    access_token = token_payload.get("access_token")
    if not isinstance(access_token, str) or not access_token or len(access_token) > 8192:
        raise ConnectorHttpError("Amazon Seller LWA response does not contain a valid access token")

    pages: list[dict[str, Any]] = []
    next_token: str | None = None
    seen_tokens: set[str] = set()
    total_items = 0
    for page in range(1, max_pages + 1):
        parameters: dict[str, str] = {
            "postedAfter": posted_after,
            "postedBefore": posted_before,
            "marketplaceId": marketplace_id,
        }
        if transaction_status:
            parameters["transactionStatus"] = transaction_status
        if next_token:
            parameters["nextToken"] = next_token
        query = urllib.parse.urlencode(parameters)
        now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        payload = request_json(HttpRequest(
            url=f"https://{host}/finances/2024-06-19/transactions?{query}",
            headers={
                "Accept": "application/json",
                "User-Agent": "OPC-Finance-Box/0.1",
                "x-amz-access-token": access_token,
                "x-amz-date": now,
            },
            timeout_seconds=float(timeout_seconds),
            max_response_bytes=max_response_bytes,
        ), operation=f"transaction page {page}")
        body = payload.get("payload")
        if not isinstance(body, dict):
            raise ConnectorHttpError(f"Amazon Seller transaction page {page} lacks payload")
        transactions = body.get("transactions")
        if not isinstance(transactions, list) or any(not isinstance(item, dict) for item in transactions):
            raise ConnectorHttpError(f"Amazon Seller transaction page {page} violates the response contract")
        if len(transactions) > 500:
            raise ConnectorHttpError(f"Amazon Seller transaction page {page} exceeds 500 records")
        total_items += len(transactions)
        if total_items > max_pages * 500:
            raise ConnectorHttpError("Amazon Seller transaction range exceeds configured pagination")
        pages.append(payload)
        raw_next = body.get("nextToken")
        if raw_next in (None, ""):
            next_token = None
            break
        if (
            not isinstance(raw_next, str) or len(raw_next) > 8192
            or any(ord(character) < 33 or ord(character) > 126 for character in raw_next)
        ):
            raise ConnectorHttpError("Amazon Seller supplied an invalid pagination token")
        if raw_next in seen_tokens:
            raise ConnectorHttpError("Amazon Seller pagination token repeated")
        seen_tokens.add(raw_next)
        next_token = raw_next
    else:
        raise ConnectorHttpError(f"Amazon Seller transaction pagination exceeded max_pages={max_pages}")

    canonical = json.dumps(pages, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {
        "batch_id": hashlib.sha256(
            f"amazon-seller|finances-2024-06-19|{region}|{marketplace_id}|{posted_after}|{posted_before}|{canonical}".encode()
        ).hexdigest()[:24],
        "pages": pages,
        "page_count": len(pages),
        "total_items": total_items,
        "retry_count": retry_count,
        "rate_limit_count": rate_limit_count,
        "retry_delay_seconds_total": retry_delay_seconds_total,
        "retry_after_honored": retry_after_honored,
        "network_access_performed": True,
        "lwa_token_exchange_performed": True,
        "lwa_token_exchange_count": 1,
        "response_links_followed": False,
        "api_contract": "finances-v2024-06-19",
        "region": region,
        "environment": environment,
        "marketplace_id": marketplace_id,
    }


def fetch_amazon_seller_marketplace_evidence_pages(
    *,
    client_id: str,
    client_secret: str,
    refresh_token: str,
    region: str,
    environment: str,
    marketplace_id: str,
    interval_start: str,
    interval_end: str,
    orders_time_basis: str = "created",
    transaction_status: str | None = None,
    max_order_pages: int = 20,
    max_inventory_pages: int = 20,
    max_transaction_pages: int = 20,
    max_attempts: int = 3,
    timeout_seconds: float = 20,
    max_response_bytes: int = 10 * 1024 * 1024,
    transport: HttpTransport = urllib_transport,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Fetch Orders 2026, current FBA Inventory and Finances with one LWA token.

    Every page is rebuilt locally against one fixed regional host. Orders requests
    include only FULFILLMENT; buyer, recipient, proceeds, expense, tax, payment,
    package and tracking datasets are deliberately not requested. Inventory is a
    current observation, while Orders and Finances use the bounded interval.
    """
    region = str(region or "").upper()
    environment = str(environment or "").lower()
    hosts = {
        "NA": "sellingpartnerapi-na.amazon.com",
        "EU": "sellingpartnerapi-eu.amazon.com",
        "FE": "sellingpartnerapi-fe.amazon.com",
    }
    if region not in hosts or environment not in {"production", "sandbox"}:
        raise ConnectorHttpError("Amazon Seller region/environment is outside the supported fixed endpoints")
    host = hosts[region]
    if environment == "sandbox":
        host = f"sandbox.{host}"
    for value, maximum, label in (
        (client_id, 512, "client ID"),
        (client_secret, 2048, "client secret"),
        (refresh_token, 8192, "refresh token"),
    ):
        if (
            not isinstance(value, str) or not value or len(value) > maximum
            or any(ord(character) < 33 or ord(character) > 126 for character in value)
        ):
            raise ConnectorHttpError(f"Amazon Seller {label} is missing or invalid")
    if not re.fullmatch(r"[A-Z0-9]{6,32}", str(marketplace_id or "")):
        raise ConnectorHttpError("Amazon Seller marketplace ID is invalid")
    if orders_time_basis not in {"created", "updated"}:
        raise ConnectorHttpError("Amazon Seller orders_time_basis must be created or updated")
    if transaction_status not in {None, "RELEASED", "DEFERRED", "DEFERRED_RELEASED"}:
        raise ConnectorHttpError("Amazon Seller transaction status is invalid")
    if not all(
        isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 20
        for value in (max_order_pages, max_inventory_pages, max_transaction_pages)
    ) or not 1 <= max_attempts <= 5:
        raise ConnectorHttpError("Amazon Seller pagination/retry limits are outside supported bounds")
    if not 1 <= timeout_seconds <= 60 or not 1024 <= max_response_bytes <= 25 * 1024 * 1024:
        raise ConnectorHttpError("Amazon Seller timeout or response byte limit is outside supported bounds")
    timestamp_pattern = r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?(?:Z|[+-][0-9]{2}:[0-9]{2})"
    if not re.fullmatch(timestamp_pattern, interval_start) or not re.fullmatch(
        timestamp_pattern, interval_end,
    ):
        raise ConnectorHttpError("Amazon Seller interval must use timezone-aware ISO-8601 timestamps")
    try:
        start = datetime.fromisoformat(interval_start.replace("Z", "+00:00"))
        end = datetime.fromisoformat(interval_end.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ConnectorHttpError("Amazon Seller interval is invalid") from exc
    if start >= end or (end - start).total_seconds() > 31 * 86400:
        raise ConnectorHttpError("Amazon Seller marketplace evidence interval must be positive and no longer than 31 days")

    retry_count = 0
    rate_limit_count = 0
    retry_delay_seconds_total = 0.0
    retry_after_honored = False

    def request_json(request: HttpRequest, *, operation: str) -> dict[str, Any]:
        nonlocal retry_count, rate_limit_count
        nonlocal retry_delay_seconds_total, retry_after_honored
        response = None
        for attempt in range(1, max_attempts + 1):
            try:
                response = transport(request)
            except (TimeoutError, OSError, ConnectorHttpError) as exc:
                if attempt == max_attempts:
                    raise ConnectorHttpError(
                        f"Amazon Seller transport failed after {max_attempts} attempts for {operation}"
                    ) from exc
                retry_count += 1
                delay, honored = _retry_delay(None, attempt)
                retry_delay_seconds_total += delay
                retry_after_honored = retry_after_honored or honored
                sleeper(delay)
                continue
            if response.status == 429 or 500 <= response.status <= 599:
                if attempt == max_attempts:
                    raise ConnectorHttpError(
                        f"Amazon Seller returned retryable status {response.status} after {max_attempts} attempts"
                    )
                retry_count += 1
                if response.status == 429:
                    rate_limit_count += 1
                delay, honored = _retry_delay(response.headers, attempt)
                retry_delay_seconds_total += delay
                retry_after_honored = retry_after_honored or honored
                sleeper(delay)
                continue
            break
        if response is None or response.status != 200:
            status = response.status if response is not None else "unknown"
            raise ConnectorHttpError(f"Amazon Seller returned non-success status {status} for {operation}")
        try:
            payload = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ConnectorHttpError(f"Amazon Seller returned invalid JSON for {operation}") from exc
        if not isinstance(payload, dict):
            raise ConnectorHttpError(f"Amazon Seller {operation} response must be a JSON object")
        return payload

    token_body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
        "client_secret": client_secret,
    }).encode("ascii")
    token_payload = request_json(HttpRequest(
        url="https://api.amazon.com/auth/o2/token",
        method="POST",
        body=token_body,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "OPC-Finance-Box/0.1",
        },
        timeout_seconds=float(timeout_seconds),
        max_response_bytes=max_response_bytes,
    ), operation="LWA token exchange")
    access_token = token_payload.get("access_token")
    if not isinstance(access_token, str) or not access_token or len(access_token) > 8192:
        raise ConnectorHttpError("Amazon Seller LWA response does not contain a valid access token")

    def sp_headers() -> dict[str, str]:
        return {
            "Accept": "application/json",
            "User-Agent": "OPC-Finance-Box/0.1",
            "x-amz-access-token": access_token,
            "x-amz-date": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        }

    order_pages: list[dict[str, Any]] = []
    order_token: str | None = None
    seen_order_tokens: set[str] = set()
    order_count = 0
    after_key = "createdAfter" if orders_time_basis == "created" else "lastUpdatedAfter"
    before_key = "createdBefore" if orders_time_basis == "created" else "lastUpdatedBefore"
    for page in range(1, max_order_pages + 1):
        parameters: list[tuple[str, str]] = [
            (after_key, interval_start), (before_key, interval_end),
            ("marketplaceIds", marketplace_id), ("maxResultsPerPage", "100"),
            ("includedData", "FULFILLMENT"),
        ]
        if order_token:
            parameters.append(("paginationToken", order_token))
        payload = request_json(HttpRequest(
            url=(
                f"https://{host}/orders/2026-01-01/orders?"
                + urllib.parse.urlencode(parameters)
            ),
            headers=sp_headers(), timeout_seconds=float(timeout_seconds),
            max_response_bytes=max_response_bytes,
        ), operation=f"Orders page {page}")
        rows = payload.get("orders")
        if not isinstance(rows, list) or any(not isinstance(item, dict) for item in rows):
            raise ConnectorHttpError(f"Amazon Seller Orders page {page} violates the response contract")
        if len(rows) > 100:
            raise ConnectorHttpError(f"Amazon Seller Orders page {page} exceeds 100 records")
        order_count += len(rows)
        if order_count > max_order_pages * 100:
            raise ConnectorHttpError("Amazon Seller Orders range exceeds configured pagination")
        order_pages.append(payload)
        pagination = payload.get("pagination") or {}
        if not isinstance(pagination, dict):
            raise ConnectorHttpError("Amazon Seller Orders pagination must be an object")
        raw_next = pagination.get("nextToken")
        if raw_next in (None, ""):
            order_token = None
            break
        if (
            not isinstance(raw_next, str) or len(raw_next) > 8192
            or any(ord(character) < 33 or ord(character) > 126 for character in raw_next)
        ):
            raise ConnectorHttpError("Amazon Seller Orders supplied an invalid pagination token")
        if raw_next in seen_order_tokens:
            raise ConnectorHttpError("Amazon Seller Orders pagination token repeated")
        seen_order_tokens.add(raw_next)
        order_token = raw_next
    else:
        raise ConnectorHttpError(f"Amazon Seller Orders pagination exceeded max_order_pages={max_order_pages}")

    inventory_pages: list[dict[str, Any]] = []
    inventory_token: str | None = None
    seen_inventory_tokens: set[str] = set()
    inventory_count = 0
    for page in range(1, max_inventory_pages + 1):
        parameters = [
            ("details", "true"), ("granularityType", "Marketplace"),
            ("granularityId", marketplace_id), ("marketplaceIds", marketplace_id),
        ]
        if inventory_token:
            parameters.append(("nextToken", inventory_token))
        payload = request_json(HttpRequest(
            url=(
                f"https://{host}/fba/inventory/v1/summaries?"
                + urllib.parse.urlencode(parameters)
            ),
            headers=sp_headers(), timeout_seconds=float(timeout_seconds),
            max_response_bytes=max_response_bytes,
        ), operation=f"FBA Inventory page {page}")
        body = payload.get("payload")
        if not isinstance(body, dict):
            raise ConnectorHttpError(f"Amazon Seller FBA Inventory page {page} lacks payload")
        granularity = body.get("granularity")
        rows = body.get("inventorySummaries")
        if (
            not isinstance(granularity, dict)
            or granularity.get("granularityType") != "Marketplace"
            or granularity.get("granularityId") != marketplace_id
            or not isinstance(rows, list)
            or any(not isinstance(item, dict) for item in rows)
        ):
            raise ConnectorHttpError(f"Amazon Seller FBA Inventory page {page} violates the response contract")
        if len(rows) > 1000:
            raise ConnectorHttpError(f"Amazon Seller FBA Inventory page {page} exceeds 1,000 records")
        inventory_count += len(rows)
        if inventory_count > max_inventory_pages * 1000:
            raise ConnectorHttpError("Amazon Seller FBA Inventory range exceeds configured pagination")
        inventory_pages.append(payload)
        pagination = payload.get("pagination") or {}
        if not isinstance(pagination, dict):
            raise ConnectorHttpError("Amazon Seller FBA Inventory pagination must be an object")
        raw_next = pagination.get("nextToken")
        if raw_next in (None, ""):
            inventory_token = None
            break
        if (
            not isinstance(raw_next, str) or len(raw_next) > 8192
            or any(ord(character) < 33 or ord(character) > 126 for character in raw_next)
        ):
            raise ConnectorHttpError("Amazon Seller FBA Inventory supplied an invalid pagination token")
        if raw_next in seen_inventory_tokens:
            raise ConnectorHttpError("Amazon Seller FBA Inventory pagination token repeated")
        seen_inventory_tokens.add(raw_next)
        inventory_token = raw_next
    else:
        raise ConnectorHttpError(
            f"Amazon Seller FBA Inventory pagination exceeded max_inventory_pages={max_inventory_pages}"
        )

    transaction_pages: list[dict[str, Any]] = []
    transaction_token: str | None = None
    seen_transaction_tokens: set[str] = set()
    transaction_count = 0
    for page in range(1, max_transaction_pages + 1):
        parameters = [
            ("postedAfter", interval_start), ("postedBefore", interval_end),
            ("marketplaceId", marketplace_id),
        ]
        if transaction_status:
            parameters.append(("transactionStatus", transaction_status))
        if transaction_token:
            parameters.append(("nextToken", transaction_token))
        payload = request_json(HttpRequest(
            url=(
                f"https://{host}/finances/2024-06-19/transactions?"
                + urllib.parse.urlencode(parameters)
            ),
            headers=sp_headers(), timeout_seconds=float(timeout_seconds),
            max_response_bytes=max_response_bytes,
        ), operation=f"Finances page {page}")
        body = payload.get("payload")
        rows = body.get("transactions") if isinstance(body, dict) else None
        if not isinstance(rows, list) or any(not isinstance(item, dict) for item in rows):
            raise ConnectorHttpError(f"Amazon Seller Finances page {page} violates the response contract")
        if len(rows) > 500:
            raise ConnectorHttpError(f"Amazon Seller Finances page {page} exceeds 500 records")
        transaction_count += len(rows)
        if transaction_count > max_transaction_pages * 500:
            raise ConnectorHttpError("Amazon Seller Finances range exceeds configured pagination")
        transaction_pages.append(payload)
        raw_next = body.get("nextToken")
        if raw_next in (None, ""):
            transaction_token = None
            break
        if (
            not isinstance(raw_next, str) or len(raw_next) > 8192
            or any(ord(character) < 33 or ord(character) > 126 for character in raw_next)
        ):
            raise ConnectorHttpError("Amazon Seller Finances supplied an invalid pagination token")
        if raw_next in seen_transaction_tokens:
            raise ConnectorHttpError("Amazon Seller Finances pagination token repeated")
        seen_transaction_tokens.add(raw_next)
        transaction_token = raw_next
    else:
        raise ConnectorHttpError(
            f"Amazon Seller Finances pagination exceeded max_transaction_pages={max_transaction_pages}"
        )

    canonical = json.dumps({
        "orders": order_pages, "inventory": inventory_pages,
        "transactions": transaction_pages,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {
        "batch_id": hashlib.sha256(
            f"amazon-seller|marketplace-evidence-v1|{region}|{marketplace_id}|{interval_start}|{interval_end}|{canonical}".encode()
        ).hexdigest()[:24],
        "order_pages": order_pages,
        "inventory_pages": inventory_pages,
        "transaction_pages": transaction_pages,
        "order_page_count": len(order_pages),
        "inventory_page_count": len(inventory_pages),
        "transaction_page_count": len(transaction_pages),
        "order_count": order_count,
        "inventory_count": inventory_count,
        "transaction_count": transaction_count,
        "retry_count": retry_count,
        "rate_limit_count": rate_limit_count,
        "retry_delay_seconds_total": retry_delay_seconds_total,
        "retry_after_honored": retry_after_honored,
        "network_access_performed": True,
        "lwa_token_exchange_performed": True,
        "lwa_token_exchange_count": 1,
        "response_links_followed": False,
        "api_contracts": [
            "orders-v2026-01-01-searchOrders",
            "fba-inventory-v1-getInventorySummaries",
            "finances-v2024-06-19-listTransactions",
        ],
        "orders_included_data": ["FULFILLMENT"],
        "inventory_observation_type": "current_at_fetch_not_historical_period_end",
        "region": region,
        "environment": environment,
        "marketplace_id": marketplace_id,
    }


def fetch_woocommerce_order_refund_pages(
    *,
    site_origin: str,
    consumer_key: str,
    consumer_secret: str,
    modified_after: str,
    modified_before: str,
    refund_after: str,
    refund_before: str,
    page_size: int = 100,
    max_pages: int = 100,
    max_attempts: int = 3,
    timeout_seconds: float = 20,
    max_response_bytes: int = 10 * 1024 * 1024,
    transport: HttpTransport = urllib_transport,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Fetch WooCommerce order change snapshots and refund events read-only.

    The store origin and credentials are operator configuration, never request URLs.
    Only fixed wc/v3 collection paths are used. Pagination is reconstructed from
    bounded integer pages and X-WP totals; Link URLs from the response are ignored.
    """
    parsed = urllib.parse.urlsplit(str(site_origin or ""))
    hostname = (parsed.hostname or "").lower()
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        is_ip = False
    else:
        is_ip = True
    try:
        configured_port = parsed.port
    except ValueError as exc:
        raise ConnectorHttpError("WooCommerce site origin contains an invalid port") from exc
    if (
        parsed.scheme != "https" or not hostname or is_ip or configured_port is not None
        or parsed.username or parsed.password or parsed.query or parsed.fragment
        or not re.fullmatch(
            r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+",
            hostname,
        )
        or hostname.endswith((".local", ".internal", ".localhost"))
    ):
        raise ConnectorHttpError(
            "WooCommerce site origin must be a public HTTPS domain without credentials, port, query or fragment"
        )
    site_path = parsed.path.rstrip("/")
    if site_path and not re.fullmatch(r"(?:/[A-Za-z0-9._~-]+)+", site_path):
        raise ConnectorHttpError("WooCommerce site origin path contains unsupported characters")
    origin = urllib.parse.urlunsplit(("https", hostname, site_path, "", ""))
    if (
        not isinstance(consumer_key, str)
        or not re.fullmatch(r"ck_[A-Za-z0-9]{8,128}", consumer_key)
        or not isinstance(consumer_secret, str)
        or not re.fullmatch(r"cs_[A-Za-z0-9]{8,256}", consumer_secret)
    ):
        raise ConnectorHttpError("WooCommerce read-only API credential is missing or invalid")
    if not 1 <= page_size <= 100 or not 1 <= max_pages <= 100 or not 1 <= max_attempts <= 5:
        raise ConnectorHttpError("WooCommerce pagination/retry limits are outside supported bounds")
    if not 1 <= timeout_seconds <= 60 or not 1024 <= max_response_bytes <= 25 * 1024 * 1024:
        raise ConnectorHttpError("WooCommerce timeout or response byte limit is outside supported bounds")
    timestamp_pattern = r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?(?:Z|[+-][0-9]{2}:[0-9]{2})"
    timestamps = (modified_after, modified_before, refund_after, refund_before)
    if any(not re.fullmatch(timestamp_pattern, value) for value in timestamps):
        raise ConnectorHttpError("WooCommerce date filters must use timezone-aware ISO-8601 timestamps")
    try:
        modified_start = datetime.fromisoformat(modified_after.replace("Z", "+00:00"))
        modified_end = datetime.fromisoformat(modified_before.replace("Z", "+00:00"))
        refund_start = datetime.fromisoformat(refund_after.replace("Z", "+00:00"))
        refund_end = datetime.fromisoformat(refund_before.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ConnectorHttpError("WooCommerce date filters are invalid") from exc
    if modified_start >= modified_end or refund_start >= refund_end:
        raise ConnectorHttpError("WooCommerce date filter starts must be earlier than ends")

    basic = base64.b64encode(f"{consumer_key}:{consumer_secret}".encode()).decode("ascii")
    retry_count = 0
    rate_limit_count = 0
    retry_delay_seconds_total = 0.0
    retry_after_honored = False

    def header_int(headers: dict[str, str], name: str, *, resource: str, page: int) -> int:
        raw = next((value for key, value in headers.items() if key.lower() == name.lower()), None)
        try:
            value = int(str(raw))
        except (TypeError, ValueError) as exc:
            raise ConnectorHttpError(
                f"WooCommerce {resource} page {page} lacks a valid {name} header"
            ) from exc
        if value < 0:
            raise ConnectorHttpError(f"WooCommerce {resource} page {page} has a negative {name}")
        return value

    def fetch_collection(resource: str, date_parameters: dict[str, str], orderby: str) -> tuple[list[list[dict[str, Any]]], int]:
        nonlocal retry_count, rate_limit_count
        nonlocal retry_delay_seconds_total, retry_after_honored
        pages: list[list[dict[str, Any]]] = []
        expected_total: int | None = None
        expected_pages: int | None = None
        for page in range(1, max_pages + 1):
            query = urllib.parse.urlencode({
                "context": "view", "page": page, "per_page": page_size,
                "order": "asc", "orderby": orderby,
                **date_parameters,
            })
            request = HttpRequest(
                url=f"{origin}/wp-json/wc/v3/{resource}?{query}",
                headers={"Accept": "application/json", "Authorization": f"Basic {basic}"},
                timeout_seconds=float(timeout_seconds),
                max_response_bytes=max_response_bytes,
            )
            response = None
            for attempt in range(1, max_attempts + 1):
                try:
                    response = transport(request)
                except (TimeoutError, OSError, ConnectorHttpError) as exc:
                    if attempt == max_attempts:
                        raise ConnectorHttpError(
                            f"WooCommerce transport failed after {max_attempts} attempts for {resource} page {page}"
                        ) from exc
                    retry_count += 1
                    delay, honored = _retry_delay(None, attempt)
                    retry_delay_seconds_total += delay
                    retry_after_honored = retry_after_honored or honored
                    sleeper(delay)
                    continue
                if response.status == 429 or 500 <= response.status <= 599:
                    if attempt == max_attempts:
                        raise ConnectorHttpError(
                            f"WooCommerce returned retryable status {response.status} after {max_attempts} attempts"
                        )
                    retry_count += 1
                    if response.status == 429:
                        rate_limit_count += 1
                    delay, honored = _retry_delay(response.headers, attempt)
                    retry_delay_seconds_total += delay
                    retry_after_honored = retry_after_honored or honored
                    sleeper(delay)
                    continue
                break
            if response is None or response.status != 200:
                status = response.status if response is not None else "unknown"
                raise ConnectorHttpError(
                    f"WooCommerce returned non-success status {status} for {resource} page {page}"
                )
            try:
                payload = json.loads(response.body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ConnectorHttpError(
                    f"WooCommerce returned invalid JSON for {resource} page {page}"
                ) from exc
            if not isinstance(payload, list) or any(not isinstance(item, dict) for item in payload):
                raise ConnectorHttpError(f"WooCommerce {resource} page {page} must be an object list")
            if len(payload) > page_size:
                raise ConnectorHttpError(f"WooCommerce {resource} page {page} exceeds per_page")
            current_total = header_int(response.headers, "X-WP-Total", resource=resource, page=page)
            current_pages = header_int(response.headers, "X-WP-TotalPages", resource=resource, page=page)
            if current_total > page_size * max_pages or current_pages > max_pages:
                raise ConnectorHttpError(
                    f"WooCommerce {resource} range exceeds configured pagination; shorten the interval"
                )
            if expected_total is None:
                expected_total, expected_pages = current_total, current_pages
            elif (current_total, current_pages) != (expected_total, expected_pages):
                raise ConnectorHttpError(f"WooCommerce {resource} pagination totals changed during the read")
            pages.append(payload)
            if current_pages == 0 or page >= current_pages:
                break
            if not payload:
                raise ConnectorHttpError(
                    f"WooCommerce {resource} pagination returned an empty intermediate page"
                )
        else:
            raise ConnectorHttpError(f"WooCommerce {resource} pagination exceeded max_pages={max_pages}")
        return pages, expected_total or 0

    order_pages, order_total = fetch_collection(
        "orders",
        {
            "modified_after": modified_after, "modified_before": modified_before,
            "dates_are_gmt": "true",
        },
        "modified",
    )
    refund_pages, refund_total = fetch_collection(
        "refunds", {"after": refund_after, "before": refund_before}, "date",
    )
    canonical = json.dumps(
        {"orders": order_pages, "refunds": refund_pages},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return {
        "batch_id": hashlib.sha256(
            f"woocommerce|wc-v3|{origin}|{modified_after}|{modified_before}|{refund_after}|{refund_before}|{canonical}".encode()
        ).hexdigest()[:24],
        "site_origin": origin,
        "order_pages": order_pages,
        "refund_pages": refund_pages,
        "order_page_count": len(order_pages),
        "refund_page_count": len(refund_pages),
        "order_total": order_total,
        "refund_total": refund_total,
        "retry_count": retry_count,
        "rate_limit_count": rate_limit_count,
        "retry_delay_seconds_total": retry_delay_seconds_total,
        "retry_after_honored": retry_after_honored,
        "network_access_performed": True,
        "api_contract": "wc-rest-v3",
    }
