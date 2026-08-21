from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any


TOKEN_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
PRINCIPAL_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.@-]{0,79}$")
ROLES = {"reader", "operator", "reviewer", "admin"}
ROLE_GRANTS = {
    "reader": {"reader"},
    "operator": {"reader", "operator"},
    "reviewer": {"reader", "reviewer"},
    "admin": set(ROLES),
}


class ApiAuthError(ValueError):
    """Raised when API principal configuration is unsafe or invalid."""


@dataclass(frozen=True)
class ApiPrincipal:
    principal_id: str
    roles: tuple[str, ...]

    def allows(self, required_role: str) -> bool:
        return any(required_role in ROLE_GRANTS[role] for role in self.roles)

    def public_dict(self) -> dict[str, Any]:
        return {"principal_id": self.principal_id, "roles": list(self.roles)}


@dataclass(frozen=True)
class ApiAuthPolicy:
    mode: str
    principals: tuple[tuple[ApiPrincipal, str], ...]
    source: str

    def authenticate(self, bearer_token: str) -> ApiPrincipal | None:
        if not bearer_token:
            return None
        try:
            supplied_hash = hash_token(bearer_token)
        except ApiAuthError:
            return None
        matched: ApiPrincipal | None = None
        for principal, expected_hash in self.principals:
            if hmac.compare_digest(supplied_hash, expected_hash):
                matched = principal
        return matched

    def public_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "mode": self.mode,
            "source": self.source,
            "principals": [principal.public_dict() for principal, _ in self.principals],
            "raw_tokens_present": False,
        }


def hash_token(token: str) -> str:
    value = str(token or "")
    if len(value) < 32:
        raise ApiAuthError("API bearer tokens must contain at least 32 characters")
    if any(ord(char) < 33 or ord(char) > 126 for char in value):
        raise ApiAuthError("API bearer tokens must contain printable non-space ASCII characters")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _load_file_policy(path_value: str | Path) -> ApiAuthPolicy:
    path = Path(path_value).expanduser().resolve()
    try:
        file_stat = path.stat()
    except OSError as exc:
        raise ApiAuthError(f"API auth policy cannot be read: {path}") from exc
    if not stat.S_ISREG(file_stat.st_mode):
        raise ApiAuthError("API auth policy must be a regular file")
    if stat.S_IMODE(file_stat.st_mode) & 0o077:
        raise ApiAuthError("API auth policy permissions must not allow group or other access")
    if file_stat.st_size > 256 * 1024:
        raise ApiAuthError("API auth policy exceeds 256 KiB")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ApiAuthError("API auth policy must be valid UTF-8 JSON") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ApiAuthError("API auth policy requires schema_version 1")
    if set(payload) != {"schema_version", "principals"}:
        raise ApiAuthError("API auth policy contains unsupported fields")
    raw_principals = payload.get("principals")
    if not isinstance(raw_principals, list) or not raw_principals or len(raw_principals) > 100:
        raise ApiAuthError("API auth policy requires 1-100 principals")
    principals: list[tuple[ApiPrincipal, str]] = []
    principal_ids: set[str] = set()
    hashes: set[str] = set()
    for index, item in enumerate(raw_principals):
        if not isinstance(item, dict):
            raise ApiAuthError(f"principals[{index}] must be an object")
        if set(item) != {"principal_id", "token_sha256", "roles"}:
            raise ApiAuthError(
                f"principals[{index}] must contain only principal_id, token_sha256 and roles"
            )
        principal_id = item.get("principal_id")
        if not isinstance(principal_id, str) or not PRINCIPAL_PATTERN.fullmatch(principal_id):
            raise ApiAuthError(f"principals[{index}].principal_id is invalid")
        if principal_id in principal_ids:
            raise ApiAuthError(f"duplicate API principal_id: {principal_id}")
        token_hash = item.get("token_sha256")
        if not isinstance(token_hash, str) or not TOKEN_HASH_PATTERN.fullmatch(token_hash):
            raise ApiAuthError(f"principals[{index}].token_sha256 must be 64 lowercase hex characters")
        if token_hash in hashes:
            raise ApiAuthError("the same token hash cannot be assigned to multiple principals")
        raw_roles = item.get("roles")
        if not isinstance(raw_roles, list) or not raw_roles or any(
            not isinstance(role, str) or role not in ROLES for role in raw_roles
        ):
            raise ApiAuthError(f"principals[{index}].roles contains an unsupported role")
        roles = tuple(sorted(set(raw_roles)))
        principal_ids.add(principal_id)
        hashes.add(token_hash)
        principals.append((ApiPrincipal(principal_id, roles), token_hash))
    return ApiAuthPolicy("role_policy", tuple(principals), str(path))


def load_api_auth_policy(
    *,
    legacy_token: str | None = None,
    policy_path: str | Path | None = None,
) -> ApiAuthPolicy | None:
    selected_token = legacy_token if legacy_token is not None else os.environ.get("OPC_FINANCE_API_TOKEN")
    selected_path = policy_path if policy_path is not None else os.environ.get("OPC_FINANCE_API_AUTH_FILE")
    if selected_token and selected_path:
        raise ApiAuthError("configure either OPC_FINANCE_API_TOKEN or OPC_FINANCE_API_AUTH_FILE, not both")
    if selected_path:
        return _load_file_policy(selected_path)
    if selected_token:
        return ApiAuthPolicy(
            "legacy_admin_token",
            ((ApiPrincipal("legacy_api_admin", ("admin",)), hash_token(selected_token)),),
            "OPC_FINANCE_API_TOKEN",
        )
    return None
