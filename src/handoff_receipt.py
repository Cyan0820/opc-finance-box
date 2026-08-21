from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path
from typing import Any

from .handoff_verify import BoxHandoffVerifyError, verify_box_candidate_bundle


class BrowserHandoffReceiptError(BoxHandoffVerifyError):
    """Raised when a browser Handoff receipt cannot be bound to its ZIP."""


RECEIPT_SCHEMA_VERSION = 1
MAX_RECEIPT_BYTES = 64 * 1024
RECEIPT_KEYS = {
    "schema_version",
    "verification_status",
    "filename",
    "size_bytes",
    "sha256",
    "runtime_fingerprint",
    "manifest_schema_version",
    "manifest_file_count",
    "browser_bytes_verified",
    "receipt_is_digital_signature",
    "archive_members_executed",
    "active_runtime_changed",
    "external_actions_performed",
}
HEX_SHA256 = re.compile(r"[0-9a-f]{64}")
BUNDLE_FILENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,250}\.zip")


def _read_private_receipt(receipt: str | Path) -> bytes:
    requested = Path(receipt).expanduser()
    if requested.suffix.lower() != ".json":
        raise BrowserHandoffReceiptError("browser Handoff receipt must use a .json suffix")
    if requested.is_symlink():
        raise BrowserHandoffReceiptError(
            "browser Handoff receipt must be an existing regular file"
        )
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(requested, flags)
    except OSError as exc:
        raise BrowserHandoffReceiptError(
            "browser Handoff receipt must be an existing regular file"
        ) from exc
    try:
        file_stat = os.fstat(descriptor)
        if (
            not stat.S_ISREG(file_stat.st_mode)
            or not 0 < file_stat.st_size <= MAX_RECEIPT_BYTES
            or file_stat.st_nlink != 1
        ):
            raise BrowserHandoffReceiptError(
                "browser Handoff receipt must be a private regular file"
            )
        if os.name != "nt" and (
            stat.S_IMODE(file_stat.st_mode) != 0o600
            or not stat.S_IMODE(file_stat.st_mode) & stat.S_IRUSR
        ):
            raise BrowserHandoffReceiptError(
                "browser Handoff receipt file must use owner-private mode 0600"
            )
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            body = stream.read(MAX_RECEIPT_BYTES + 1)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(body) != file_stat.st_size:
        raise BrowserHandoffReceiptError(
            "browser Handoff receipt size changed during verification"
        )
    return body


def _load_receipt(body: bytes) -> dict[str, Any]:
    try:
        receipt = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BrowserHandoffReceiptError(
            "browser Handoff receipt is not valid UTF-8 JSON"
        ) from exc
    if not isinstance(receipt, dict) or set(receipt) != RECEIPT_KEYS:
        raise BrowserHandoffReceiptError("browser Handoff receipt contract is invalid")
    if (
        receipt.get("schema_version") != RECEIPT_SCHEMA_VERSION
        or receipt.get("verification_status") != "passed"
        or receipt.get("browser_bytes_verified") is not True
        or receipt.get("receipt_is_digital_signature") is not False
        or receipt.get("archive_members_executed") is not False
        or receipt.get("active_runtime_changed") is not False
        or receipt.get("external_actions_performed") is not False
    ):
        raise BrowserHandoffReceiptError(
            "browser Handoff receipt safety boundary is invalid"
        )
    filename = receipt.get("filename")
    if not isinstance(filename, str) or BUNDLE_FILENAME.fullmatch(filename) is None:
        raise BrowserHandoffReceiptError("browser Handoff receipt filename is invalid")
    for field in ("sha256", "runtime_fingerprint"):
        value = receipt.get(field)
        if not isinstance(value, str) or HEX_SHA256.fullmatch(value) is None:
            raise BrowserHandoffReceiptError(
                f"browser Handoff receipt {field} is invalid"
            )
    for field in ("size_bytes", "manifest_schema_version", "manifest_file_count"):
        value = receipt.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise BrowserHandoffReceiptError(
                f"browser Handoff receipt {field} is invalid"
            )
    return receipt


def verify_browser_handoff_receipt(
    bundle: str | Path,
    receipt: str | Path,
    packs_root: str | Path,
) -> dict[str, Any]:
    """Formally verify a Handoff and bind its safe fields to a browser receipt.

    This proves that the portable receipt fields describe the verified ZIP. It cannot
    attest that a particular browser executed WebCrypto and is not a digital signature.
    """
    verification = verify_box_candidate_bundle(bundle, packs_root)
    receipt_body = _read_private_receipt(receipt)
    payload = _load_receipt(receipt_body)
    bundle_name = Path(bundle).expanduser().name
    expected = {
        "filename": bundle_name,
        "size_bytes": verification["size_bytes"],
        "sha256": verification["bundle_sha256"],
        "runtime_fingerprint": verification["runtime_fingerprint"],
        "manifest_schema_version": verification["manifest_schema_version"],
        "manifest_file_count": verification["manifest_file_count"],
    }
    mismatched = sorted(
        field for field, value in expected.items() if payload.get(field) != value
    )
    if mismatched:
        raise BrowserHandoffReceiptError(
            "browser Handoff receipt does not match the verified bundle: "
            + ", ".join(mismatched)
        )
    return {
        "schema_version": 1,
        "valid": True,
        "bundle_receipt_match": True,
        "bundle_sha256": verification["bundle_sha256"],
        "bundle_size_bytes": verification["size_bytes"],
        "runtime_fingerprint": verification["runtime_fingerprint"],
        "member_count": verification["member_count"],
        "manifest_schema_version": verification["manifest_schema_version"],
        "manifest_file_count": verification["manifest_file_count"],
        "receipt_sha256": hashlib.sha256(receipt_body).hexdigest(),
        "reproducible_with_installed_packs": verification[
            "reproducible_with_installed_packs"
        ],
        "archive_bytes_match_current_builder": verification[
            "archive_bytes_match_current_builder"
        ],
        "browser_bytes_verified_claimed": True,
        "browser_execution_attested": False,
        "receipt_is_digital_signature": False,
        "receipt_is_identity_attestation": False,
        "archive_extracted": False,
        "paths_returned": False,
        "secret_values_included": False,
        "financial_values_returned": False,
        "external_actions_performed": False,
        "active_runtime_changed": False,
    }
