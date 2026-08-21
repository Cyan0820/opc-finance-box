from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from .handoff_verify import _safe_member_name
from .source_kit import (
    MANIFEST_NAME,
    MAX_ARCHIVE_BYTES,
    MAX_MANIFEST_BYTES,
    MAX_MEMBER_BYTES,
    MAX_MEMBER_COUNT,
    MAX_TOTAL_BYTES,
    SourceKitError,
    _read_archive,
    _verify_source_kit_body,
    build_source_kit_bundle,
)


class SourceKitUnpackError(ValueError):
    """Raised when a verified Source Kit cannot become a trusted fork workspace."""


RECEIPT_NAME = ".opc-source-kit-unpack-receipt.json"
RECEIPT_SCHEMA_VERSION = 1
RECEIPT_KEYS = {
    "schema_version",
    "product",
    "source_kit_schema_version",
    "installed_at",
    "actor",
    "archive_sha256",
    "archive_size_bytes",
    "content_fingerprint",
    "member_count",
    "manifest_file_count",
    "files",
    "tree_fingerprint",
    "archive_bytes_matched_current_builder",
    "archive_verified_before_unpack",
    "installed_source_reproducible",
    "destination_preexisted",
    "files_overwritten",
    "archive_members_executed",
    "source_archive_deleted_during_unpack",
    "git_repository_initialized",
    "dependencies_installed",
    "commands_executed",
    "credentials_persisted",
    "private_evidence_persisted",
    "financial_values_added",
    "external_actions_performed",
    "receipt_is_digital_signature",
    "receipt_payload_sha256",
}
FILE_RECORD_KEYS = {"path", "size_bytes", "sha256"}


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


def _sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _validate_actor(actor: Any) -> str:
    if not isinstance(actor, str):
        raise SourceKitUnpackError("Source Kit unpack actor must be a string")
    normalized = actor.strip()
    if (
        not normalized
        or normalized != actor
        or len(normalized) > 200
        or any(ord(character) < 32 for character in normalized)
    ):
        raise SourceKitUnpackError(
            "Source Kit unpack actor must be 1-200 visible characters without padding"
        )
    return normalized


def _new_destination(root: str | Path) -> Path:
    requested = Path(root).expanduser()
    if not requested.is_absolute() or requested.name in {"", ".", ".."}:
        raise SourceKitUnpackError("Source Kit unpack root must be a new absolute directory")
    parent = requested.parent.resolve()
    if parent.is_symlink() or not parent.is_dir():
        raise SourceKitUnpackError(
            "Source Kit unpack parent must be an existing real directory"
        )
    destination = parent / requested.name
    if destination.exists() or destination.is_symlink():
        raise SourceKitUnpackError(
            "Source Kit unpack root already exists; refusing to overwrite"
        )
    return destination


def _existing_destination(root: str | Path) -> Path:
    requested = Path(root).expanduser()
    if not requested.is_absolute() or requested.is_symlink():
        raise SourceKitUnpackError("Source Kit unpack root must be a real absolute directory")
    try:
        destination = requested.resolve(strict=True)
    except OSError as exc:
        raise SourceKitUnpackError(
            "Source Kit unpack root must be a real absolute directory"
        ) from exc
    if not destination.is_dir():
        raise SourceKitUnpackError("Source Kit unpack root must be a real absolute directory")
    return destination


def _ensure_private_directory(path: Path) -> None:
    if path.is_symlink() or not path.is_dir():
        raise SourceKitUnpackError("Source Kit workspace contains a non-directory path")
    if os.name != "nt" and stat.S_IMODE(path.stat().st_mode) != 0o700:
        raise SourceKitUnpackError("Source Kit workspace directories must use mode 0700")


def _create_parent_directories(root: Path, relative: PurePosixPath) -> None:
    current = root
    for part in relative.parts:
        current = current / part
        if current.exists() or current.is_symlink():
            _ensure_private_directory(current)
            continue
        os.mkdir(current, 0o700)
        if os.name != "nt":
            current.chmod(0o700)


def _write_private_file(path: Path, body: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise SourceKitUnpackError(
            "Source Kit unpack refused to overwrite or follow a destination file"
        ) from exc
    try:
        if os.name != "nt":
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _read_private_file(
    path: Path,
    *,
    maximum_bytes: int,
    expected_size: int | None = None,
) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise SourceKitUnpackError(
            "Source Kit workspace file must be an owner-private regular file"
        ) from exc
    try:
        file_stat = os.fstat(descriptor)
        if (
            not stat.S_ISREG(file_stat.st_mode)
            or file_stat.st_size < 0
            or file_stat.st_size > maximum_bytes
            or (expected_size is not None and file_stat.st_size != expected_size)
        ):
            raise SourceKitUnpackError(
                "Source Kit workspace file size or type is invalid"
            )
        if os.name != "nt" and (
            stat.S_IMODE(file_stat.st_mode) != 0o600 or file_stat.st_nlink != 1
        ):
            raise SourceKitUnpackError("Source Kit workspace files must use mode 0600")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            body = stream.read(maximum_bytes + 1)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(body) != file_stat.st_size:
        raise SourceKitUnpackError("Source Kit workspace file changed during verification")
    return body


def _fsync_directories(root: Path) -> None:
    if os.name == "nt":
        return
    directories = [root, *(path for path in root.rglob("*") if path.is_dir())]
    for directory in sorted(directories, key=lambda item: len(item.parts), reverse=True):
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _records(member_bodies: dict[str, bytes]) -> list[dict[str, Any]]:
    return [
        {"path": name, "size_bytes": len(body), "sha256": _sha256(body)}
        for name, body in sorted(member_bodies.items())
    ]


def _build_receipt(
    verification: dict[str, Any],
    member_bodies: dict[str, bytes],
    *,
    actor: str,
) -> dict[str, Any]:
    records = _records(member_bodies)
    payload = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "product": "opc-finance-box",
        "source_kit_schema_version": 1,
        "installed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
            "+00:00", "Z",
        ),
        "actor": actor,
        "archive_sha256": verification["sha256"],
        "archive_size_bytes": verification["size_bytes"],
        "content_fingerprint": verification["content_fingerprint"],
        "member_count": verification["member_count"],
        "manifest_file_count": verification["manifest_file_count"],
        "files": records,
        "tree_fingerprint": _sha256(_canonical_bytes(records)),
        "archive_bytes_matched_current_builder": verification[
            "archive_bytes_match_current_builder"
        ],
        "archive_verified_before_unpack": True,
        "installed_source_reproducible": True,
        "destination_preexisted": False,
        "files_overwritten": False,
        "archive_members_executed": False,
        "source_archive_deleted_during_unpack": False,
        "git_repository_initialized": False,
        "dependencies_installed": False,
        "commands_executed": False,
        "credentials_persisted": False,
        "private_evidence_persisted": False,
        "financial_values_added": False,
        "external_actions_performed": False,
        "receipt_is_digital_signature": False,
    }
    payload["receipt_payload_sha256"] = _sha256(_canonical_bytes(payload))
    return payload


def _receipt_body(receipt: dict[str, Any]) -> bytes:
    return (json.dumps(receipt, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def unpack_source_kit_bundle(
    bundle: str | Path,
    destination_root: str | Path,
    *,
    actor: str,
    project_root: str | Path | None = None,
) -> dict[str, Any]:
    """Verify one archive body, then materialize those exact bytes into a new root."""
    actor = _validate_actor(actor)
    destination = _new_destination(destination_root)
    try:
        body = _read_archive(bundle)
        verification, member_bodies, _ = _verify_source_kit_body(
            body, project_root=project_root,
        )
    except SourceKitError as exc:
        raise SourceKitUnpackError(str(exc)) from exc
    if RECEIPT_NAME in member_bodies:
        raise SourceKitUnpackError("Source Kit collides with the workspace receipt")
    try:
        os.mkdir(destination, 0o700)
    except OSError as exc:
        raise SourceKitUnpackError(
            "Source Kit unpack root could not be created without overwrite"
        ) from exc
    if os.name != "nt":
        destination.chmod(0o700)
    receipt_path = destination / RECEIPT_NAME
    try:
        for name, member_body in sorted(member_bodies.items()):
            if not _safe_member_name(name):
                raise SourceKitUnpackError("verified Source Kit contains an unsafe path")
            relative = PurePosixPath(name)
            _create_parent_directories(destination, relative.parent)
            _write_private_file(destination.joinpath(*relative.parts), member_body)
        receipt = _build_receipt(verification, member_bodies, actor=actor)
        receipt_body = _receipt_body(receipt)
        _write_private_file(receipt_path, receipt_body)
        _fsync_directories(destination)
        verified = verify_unpacked_source_kit(destination, project_root=project_root)
    except Exception as exc:
        if receipt_path.is_file() and not receipt_path.is_symlink():
            receipt_path.unlink()
        if isinstance(exc, SourceKitUnpackError):
            raise
        raise SourceKitUnpackError(
            "Source Kit unpack did not complete; destination has no valid receipt"
        ) from exc
    return {
        "schema_version": 1,
        "unpacked": True,
        "archive_sha256": verification["sha256"],
        "content_fingerprint": verification["content_fingerprint"],
        "extracted_member_count": verification["member_count"],
        "installed_file_count": verification["member_count"] + 1,
        "directory_count": verified["directory_count"],
        "receipt_sha256": _sha256(receipt_body),
        "receipt_written_last": True,
        "installed_tree_verified": True,
        "source_archive_retained": True,
        "files_overwritten": False,
        "archive_members_executed": False,
        "git_repository_initialized": False,
        "dependencies_installed": False,
        "commands_executed": False,
        "destination_path_returned": False,
        "actor_returned": False,
        "credentials_returned": False,
        "private_evidence_returned": False,
        "financial_values_returned": False,
        "receipt_is_digital_signature": False,
        "external_actions_performed": False,
        "active_runtime_changed": False,
    }


def _load_receipt(path: Path) -> tuple[dict[str, Any], bytes]:
    body = _read_private_file(path, maximum_bytes=MAX_MANIFEST_BYTES)
    if not body:
        raise SourceKitUnpackError("Source Kit workspace receipt size is invalid")
    try:
        receipt = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SourceKitUnpackError("Source Kit workspace receipt is not valid JSON") from exc
    if not isinstance(receipt, dict) or set(receipt) != RECEIPT_KEYS:
        raise SourceKitUnpackError("Source Kit workspace receipt contract is invalid")
    return receipt, body


def _validate_receipt(receipt: dict[str, Any]) -> list[dict[str, Any]]:
    false_fields = (
        "destination_preexisted",
        "files_overwritten",
        "archive_members_executed",
        "source_archive_deleted_during_unpack",
        "git_repository_initialized",
        "dependencies_installed",
        "commands_executed",
        "credentials_persisted",
        "private_evidence_persisted",
        "financial_values_added",
        "external_actions_performed",
        "receipt_is_digital_signature",
    )
    if (
        receipt.get("schema_version") != RECEIPT_SCHEMA_VERSION
        or receipt.get("product") != "opc-finance-box"
        or receipt.get("source_kit_schema_version") != 1
        or receipt.get("archive_verified_before_unpack") is not True
        or receipt.get("installed_source_reproducible") is not True
        or any(receipt.get(field) is not False for field in false_fields)
    ):
        raise SourceKitUnpackError("Source Kit workspace receipt safety boundary is invalid")
    _validate_actor(receipt.get("actor"))
    installed_at_value = receipt.get("installed_at")
    if not isinstance(installed_at_value, str):
        raise SourceKitUnpackError("Source Kit workspace receipt timestamp is invalid")
    try:
        installed_at = datetime.fromisoformat(installed_at_value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise SourceKitUnpackError("Source Kit workspace receipt timestamp is invalid") from exc
    if installed_at.tzinfo is None:
        raise SourceKitUnpackError("Source Kit workspace receipt timestamp is invalid")
    for key in (
        "archive_sha256", "content_fingerprint", "tree_fingerprint",
        "receipt_payload_sha256",
    ):
        if not isinstance(receipt.get(key), str) or re.fullmatch(
            r"[0-9a-f]{64}", receipt[key],
        ) is None:
            raise SourceKitUnpackError("Source Kit workspace receipt fingerprint is invalid")
    for key in ("archive_size_bytes", "member_count", "manifest_file_count"):
        value = receipt.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise SourceKitUnpackError("Source Kit workspace receipt count is invalid")
    if (
        receipt["archive_size_bytes"] > MAX_ARCHIVE_BYTES
        or receipt["member_count"] > MAX_MEMBER_COUNT
        or receipt["manifest_file_count"] >= receipt["member_count"]
        or not isinstance(receipt.get("archive_bytes_matched_current_builder"), bool)
    ):
        raise SourceKitUnpackError("Source Kit workspace receipt count is invalid")
    records = receipt.get("files")
    if not isinstance(records, list) or len(records) != receipt["member_count"]:
        raise SourceKitUnpackError("Source Kit workspace receipt file count is invalid")
    paths: list[str] = []
    total = 0
    for record in records:
        if not isinstance(record, dict) or set(record) != FILE_RECORD_KEYS:
            raise SourceKitUnpackError("Source Kit workspace file record is invalid")
        path = record.get("path")
        size = record.get("size_bytes")
        digest = record.get("sha256")
        if (
            not isinstance(path, str)
            or not _safe_member_name(path)
            or path == RECEIPT_NAME
            or not isinstance(size, int)
            or isinstance(size, bool)
            or not 0 <= size <= MAX_MEMBER_BYTES
            or not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        ):
            raise SourceKitUnpackError("Source Kit workspace file record is invalid")
        paths.append(path)
        total += size
        if total > MAX_TOTAL_BYTES:
            raise SourceKitUnpackError("Source Kit workspace content exceeds the limit")
    if paths != sorted(paths) or len(paths) != len(set(paths)):
        raise SourceKitUnpackError("Source Kit workspace file paths are invalid")
    if _sha256(_canonical_bytes(records)) != receipt["tree_fingerprint"]:
        raise SourceKitUnpackError("Source Kit workspace tree fingerprint is invalid")
    unsigned = {
        key: value for key, value in receipt.items() if key != "receipt_payload_sha256"
    }
    if _sha256(_canonical_bytes(unsigned)) != receipt["receipt_payload_sha256"]:
        raise SourceKitUnpackError("Source Kit workspace receipt fingerprint is invalid")
    return records


def _inspect_tree(
    root: Path,
    records: list[dict[str, Any]],
) -> tuple[dict[str, bytes], int]:
    expected_files = {record["path"] for record in records} | {RECEIPT_NAME}
    expected_directories = {
        PurePosixPath(path).parent.as_posix()
        for path in expected_files
        if PurePosixPath(path).parent.as_posix() != "."
    }
    expected_directories |= {
        parent.as_posix()
        for path in tuple(expected_directories)
        for parent in PurePosixPath(path).parents
        if parent.as_posix() != "."
    }
    actual_files: set[str] = set()
    actual_directories: set[str] = set()
    for current, directories, files in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        _ensure_private_directory(current_path)
        for name in directories:
            path = current_path / name
            if path.is_symlink():
                raise SourceKitUnpackError("Source Kit workspace contains a symbolic link")
            _ensure_private_directory(path)
            actual_directories.add(path.relative_to(root).as_posix())
            if len(actual_directories) > MAX_MEMBER_COUNT:
                raise SourceKitUnpackError("Source Kit workspace tree exceeds the limit")
        for name in files:
            path = current_path / name
            if path.is_symlink():
                raise SourceKitUnpackError("Source Kit workspace contains a symbolic link")
            if not path.is_file():
                raise SourceKitUnpackError("Source Kit workspace contains a non-regular file")
            actual_files.add(path.relative_to(root).as_posix())
            if len(actual_files) > MAX_MEMBER_COUNT + 1:
                raise SourceKitUnpackError("Source Kit workspace tree exceeds the limit")
    if actual_files != expected_files or actual_directories != expected_directories:
        raise SourceKitUnpackError("Source Kit workspace tree does not match its receipt")
    bodies: dict[str, bytes] = {}
    for record in records:
        path = root.joinpath(*PurePosixPath(record["path"]).parts)
        body = _read_private_file(
            path, maximum_bytes=MAX_MEMBER_BYTES, expected_size=record["size_bytes"],
        )
        if _sha256(body) != record["sha256"]:
            raise SourceKitUnpackError("Source Kit workspace file does not match its receipt")
        bodies[record["path"]] = body
    return bodies, len(actual_directories) + 1


def verify_unpacked_source_kit(
    root: str | Path,
    *,
    project_root: str | Path | None = None,
) -> dict[str, Any]:
    """Reverify a pristine fork workspace without requiring its source ZIP."""
    destination = _existing_destination(root)
    _ensure_private_directory(destination)
    receipt, receipt_body = _load_receipt(destination / RECEIPT_NAME)
    records = _validate_receipt(receipt)
    member_bodies, directory_count = _inspect_tree(destination, records)
    manifest_body = member_bodies.get(MANIFEST_NAME)
    if manifest_body is None or len(manifest_body) > MAX_MANIFEST_BYTES:
        raise SourceKitUnpackError("Source Kit workspace manifest is missing or invalid")
    try:
        manifest = json.loads(manifest_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SourceKitUnpackError("Source Kit workspace manifest is invalid") from exc
    if (
        not isinstance(manifest, dict)
        or manifest.get("content_fingerprint") != receipt["content_fingerprint"]
        or manifest.get("file_count") != receipt["manifest_file_count"]
    ):
        raise SourceKitUnpackError("Source Kit workspace manifest binding is invalid")
    try:
        expected_body, _, expected_manifest = build_source_kit_bundle(project_root)
        with zipfile.ZipFile(io.BytesIO(expected_body), "r") as archive:
            expected_bodies = {
                info.filename: archive.read(info) for info in archive.infolist()
            }
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        raise SourceKitUnpackError(
            "Source Kit workspace cannot be reproduced from installed source assets"
        ) from exc
    if expected_manifest != manifest or expected_bodies != member_bodies:
        raise SourceKitUnpackError(
            "Source Kit workspace does not reproduce from installed source assets"
        )
    return {
        "schema_version": 1,
        "valid": True,
        "archive_sha256": receipt["archive_sha256"],
        "receipt_sha256": _sha256(receipt_body),
        "content_fingerprint": receipt["content_fingerprint"],
        "member_count": receipt["member_count"],
        "installed_file_count": receipt["member_count"] + 1,
        "directory_count": directory_count,
        "installed_source_reproducible": True,
        "source_archive_sha_matches_current_builder": (
            _sha256(expected_body) == receipt["archive_sha256"]
        ),
        "source_archive_required": False,
        "pristine_workspace_required": True,
        "paths_returned": False,
        "actor_returned": False,
        "credentials_returned": False,
        "private_evidence_returned": False,
        "financial_values_returned": False,
        "receipt_is_digital_signature": False,
        "archive_members_executed": False,
        "external_actions_performed": False,
        "active_runtime_changed": False,
    }
