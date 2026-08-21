from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from .box_builder import build_box_candidate_bundle
from .deployment_assets import REQUIRED_ASSETS


class BoxHandoffVerifyError(ValueError):
    """Raised when a Box handoff ZIP cannot be trusted."""


MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
MAX_MEMBER_BYTES = 16 * 1024 * 1024
MAX_TOTAL_UNCOMPRESSED_BYTES = 64 * 1024 * 1024
MAX_MEMBER_COUNT = 256
MAX_MANIFEST_BYTES = 1024 * 1024
MANIFEST_NAME = "bundle-manifest.json"
MANIFEST_KEYS = {
    "schema_version",
    "product",
    "runtime_fingerprint",
    "file_count",
    "files",
    "secret_values_included",
    "external_actions_performed",
    "active_runtime_changed",
    "deployment_assets_included",
    "activation_guide_included",
}
RECORD_KEYS = {"path", "size_bytes", "sha256"}
REQUIRED_MEMBERS = {
    "box-spec.json",
    "box.json",
    "setup-checklist.json",
    "HANDOFF.md",
    "ACTIVATION.md",
    "compiled/box.lock.json",
    "compiled/production-readiness-plan.json",
    "compiled/release-gates.json",
    *(f"deployment/{name}" for name in REQUIRED_ASSETS),
}


def _safe_member_name(name: str) -> bool:
    if not name or "\\" in name or "\x00" in name:
        return False
    path = PurePosixPath(name)
    return (
        not path.is_absolute()
        and path.as_posix() == name
        and all(part not in {"", ".", ".."} for part in path.parts)
    )


def _read_member(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    *,
    maximum_bytes: int = MAX_MEMBER_BYTES,
) -> bytes:
    if info.file_size > maximum_bytes:
        raise BoxHandoffVerifyError("Box handoff member exceeds the allowed size")
    with archive.open(info, "r") as stream:
        body = stream.read(maximum_bytes + 1)
    if len(body) > maximum_bytes or len(body) != info.file_size:
        raise BoxHandoffVerifyError("Box handoff member size is not trustworthy")
    return body


def _hash_member(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with archive.open(info, "r") as stream:
        while True:
            chunk = stream.read(64 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_MEMBER_BYTES:
                raise BoxHandoffVerifyError("Box handoff member exceeds the allowed size")
            digest.update(chunk)
    if size != info.file_size:
        raise BoxHandoffVerifyError("Box handoff member size is not trustworthy")
    return size, digest.hexdigest()


def _load_json_object(body: bytes, field: str) -> dict[str, Any]:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BoxHandoffVerifyError(f"Box handoff {field} is not valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise BoxHandoffVerifyError(f"Box handoff {field} must be a JSON object")
    return payload


def _validate_archive_members(archive: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    infos = archive.infolist()
    if not infos or len(infos) > MAX_MEMBER_COUNT:
        raise BoxHandoffVerifyError("Box handoff has an invalid member count")
    members: dict[str, zipfile.ZipInfo] = {}
    total_uncompressed = 0
    for info in infos:
        if info.filename in members:
            raise BoxHandoffVerifyError("Box handoff contains duplicate members")
        if info.is_dir() or not _safe_member_name(info.filename):
            raise BoxHandoffVerifyError("Box handoff contains an unsafe member path")
        if info.flag_bits & 0x1:
            raise BoxHandoffVerifyError("Box handoff contains an encrypted member")
        if info.flag_bits & ~0x800 or info.extra or info.comment:
            raise BoxHandoffVerifyError("Box handoff member metadata is not canonical")
        if info.compress_type != zipfile.ZIP_DEFLATED:
            raise BoxHandoffVerifyError("Box handoff uses an unexpected compression method")
        mode = info.external_attr >> 16
        if (
            info.create_system != 3
            or stat.S_IFMT(mode) != stat.S_IFREG
            or stat.S_IMODE(mode) != 0o644
            or info.date_time != (1980, 1, 1, 0, 0, 0)
        ):
            raise BoxHandoffVerifyError("Box handoff member metadata is not deterministic")
        if info.file_size > MAX_MEMBER_BYTES:
            raise BoxHandoffVerifyError("Box handoff member exceeds the allowed size")
        total_uncompressed += info.file_size
        if total_uncompressed > MAX_TOTAL_UNCOMPRESSED_BYTES:
            raise BoxHandoffVerifyError("Box handoff uncompressed size exceeds the allowed limit")
        members[info.filename] = info
    if MANIFEST_NAME not in members:
        raise BoxHandoffVerifyError("Box handoff manifest is missing")
    return members


def _validate_archive_envelope(body: bytes, archive: zipfile.ZipFile) -> None:
    """Reject prepended/appended polyglot data, ZIP comments and non-canonical envelopes."""
    infos = archive.infolist()
    if (
        len(body) < 22
        or body[-22:-18] != b"PK\x05\x06"
        or body[-2:] != b"\x00\x00"
        or archive.comment
        or not infos
        or min(info.header_offset for info in infos) != 0
    ):
        raise BoxHandoffVerifyError("Box handoff ZIP envelope is not canonical")


def _validate_manifest(
    archive: zipfile.ZipFile,
    members: dict[str, zipfile.ZipInfo],
) -> dict[str, Any]:
    manifest = _load_json_object(
        _read_member(archive, members[MANIFEST_NAME], maximum_bytes=MAX_MANIFEST_BYTES),
        "manifest",
    )
    if set(manifest) != MANIFEST_KEYS:
        raise BoxHandoffVerifyError("Box handoff manifest contract is invalid")
    if (
        manifest.get("schema_version") != 2
        or manifest.get("product") != "opc-finance-box"
        or manifest.get("secret_values_included") is not False
        or manifest.get("external_actions_performed") is not False
        or manifest.get("active_runtime_changed") is not False
        or manifest.get("deployment_assets_included") is not True
        or manifest.get("activation_guide_included") is not True
    ):
        raise BoxHandoffVerifyError("Box handoff manifest safety boundary is invalid")
    fingerprint = manifest.get("runtime_fingerprint")
    if not isinstance(fingerprint, str) or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None:
        raise BoxHandoffVerifyError("Box handoff runtime fingerprint is invalid")
    records = manifest.get("files")
    file_count = manifest.get("file_count")
    if (
        not isinstance(records, list)
        or not isinstance(file_count, int)
        or isinstance(file_count, bool)
        or file_count != len(records)
    ):
        raise BoxHandoffVerifyError("Box handoff manifest file count is invalid")
    paths: list[str] = []
    for record in records:
        if not isinstance(record, dict) or set(record) != RECORD_KEYS:
            raise BoxHandoffVerifyError("Box handoff manifest file record is invalid")
        path = record.get("path")
        size = record.get("size_bytes")
        sha256 = record.get("sha256")
        if not isinstance(path, str) or not _safe_member_name(path) or path == MANIFEST_NAME:
            raise BoxHandoffVerifyError("Box handoff manifest contains an unsafe member path")
        if (
            not isinstance(size, int)
            or isinstance(size, bool)
            or not 0 <= size <= MAX_MEMBER_BYTES
            or not isinstance(sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", sha256) is None
        ):
            raise BoxHandoffVerifyError("Box handoff manifest digest record is invalid")
        paths.append(path)
    if paths != sorted(paths) or len(paths) != len(set(paths)):
        raise BoxHandoffVerifyError("Box handoff manifest paths are not unique and sorted")
    if set(paths) | {MANIFEST_NAME} != set(members):
        raise BoxHandoffVerifyError("Box handoff members do not match the manifest")
    if not REQUIRED_MEMBERS.issubset(paths):
        raise BoxHandoffVerifyError("Box handoff is missing required product members")
    for record in records:
        size, sha256 = _hash_member(archive, members[record["path"]])
        if size != record["size_bytes"] or sha256 != record["sha256"]:
            raise BoxHandoffVerifyError("Box handoff member does not match the manifest")
    return manifest


def _read_private_bundle(bundle: str | Path) -> bytes:
    """Read one owner-private regular ZIP through a no-follow descriptor."""
    requested = Path(bundle).expanduser()
    if requested.suffix.lower() != ".zip":
        raise BoxHandoffVerifyError("Box handoff input must use a .zip suffix")
    if requested.is_symlink():
        raise BoxHandoffVerifyError("Box handoff must be an existing regular file")
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(requested, flags)
    except OSError as exc:
        raise BoxHandoffVerifyError("Box handoff must be an existing regular file") from exc
    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise BoxHandoffVerifyError("Box handoff must be an existing regular file")
        if not 0 < file_stat.st_size <= MAX_ARCHIVE_BYTES:
            raise BoxHandoffVerifyError("Box handoff archive size is outside the allowed limit")
        if os.name != "nt" and (
            stat.S_IMODE(file_stat.st_mode) & 0o077
            or not stat.S_IMODE(file_stat.st_mode) & stat.S_IRUSR
        ):
            raise BoxHandoffVerifyError("Box handoff file must be owner-private and readable")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            body = stream.read(MAX_ARCHIVE_BYTES + 1)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(body) != file_stat.st_size:
        raise BoxHandoffVerifyError("Box handoff archive size changed during verification")
    return body


def _verify_bundle_body(
    body: bytes,
    packs_root: str | Path,
) -> tuple[dict[str, Any], dict[str, bytes]]:
    """Verify trusted-size bytes and return private member bodies to in-package callers."""
    if not isinstance(body, bytes) or not 0 < len(body) <= MAX_ARCHIVE_BYTES:
        raise BoxHandoffVerifyError("Box handoff archive size is outside the allowed limit")
    try:
        with zipfile.ZipFile(io.BytesIO(body), "r") as archive:
            _validate_archive_envelope(body, archive)
            members = _validate_archive_members(archive)
            manifest = _validate_manifest(archive, members)
            member_bodies = {
                name: _read_member(archive, info) for name, info in members.items()
            }
            spec = _load_json_object(member_bodies["box-spec.json"], "Box specification")
            compiled_lock = _load_json_object(
                member_bodies["compiled/box.lock.json"], "compiled lock",
            )
    except (zipfile.BadZipFile, RuntimeError) as exc:
        if isinstance(exc, BoxHandoffVerifyError):
            raise
        raise BoxHandoffVerifyError("Box handoff is not a valid ZIP archive") from exc
    lock_fingerprint = (compiled_lock.get("lock") or {}).get("runtime_fingerprint")
    if lock_fingerprint != manifest["runtime_fingerprint"]:
        raise BoxHandoffVerifyError("Box handoff runtime fingerprint binding is invalid")
    try:
        expected_body, _, expected_manifest = build_box_candidate_bundle(spec, packs_root)
    except (OSError, ValueError) as exc:
        raise BoxHandoffVerifyError(
            "Box handoff cannot be reproduced with the installed Pack catalog"
        ) from exc
    if expected_manifest != manifest:
        raise BoxHandoffVerifyError(
            "Box handoff does not reproduce with the installed Pack catalog"
        )
    try:
        with (
            zipfile.ZipFile(io.BytesIO(expected_body), "r") as expected_archive,
            zipfile.ZipFile(io.BytesIO(body), "r") as observed_archive,
        ):
            expected_members = {
                info.filename: info for info in expected_archive.infolist()
            }
            observed_members = {
                info.filename: info for info in observed_archive.infolist()
            }
            if set(expected_members) != set(observed_members):
                raise BoxHandoffVerifyError(
                    "Box handoff does not reproduce with the installed Pack catalog"
                )
            for name, expected_info in expected_members.items():
                if _read_member(expected_archive, expected_info) != _read_member(
                    observed_archive, observed_members[name]
                ):
                    raise BoxHandoffVerifyError(
                        "Box handoff does not reproduce with the installed Pack catalog"
                    )
    except (zipfile.BadZipFile, RuntimeError) as exc:
        if isinstance(exc, BoxHandoffVerifyError):
            raise
        raise BoxHandoffVerifyError(
            "Box handoff cannot be reproduced with the installed Pack catalog"
        ) from exc
    return {
        "schema_version": 1,
        "valid": True,
        "bundle_sha256": hashlib.sha256(body).hexdigest(),
        "size_bytes": len(body),
        "runtime_fingerprint": manifest["runtime_fingerprint"],
        "member_count": len(members),
        "manifest_file_count": manifest["file_count"],
        "manifest_schema_version": manifest["schema_version"],
        "reproducible_with_installed_packs": True,
        "archive_bytes_match_current_builder": expected_body == body,
        "archive_extracted": False,
        "paths_returned": False,
        "secret_values_included": False,
        "financial_values_returned": False,
        "external_actions_performed": False,
        "active_runtime_changed": False,
    }, member_bodies


def verify_box_candidate_bundle(
    bundle: str | Path,
    packs_root: str | Path,
) -> dict[str, Any]:
    """Verify a private handoff without extracting it or trusting its own manifest."""
    result, _ = _verify_bundle_body(_read_private_bundle(bundle), packs_root)
    return result
