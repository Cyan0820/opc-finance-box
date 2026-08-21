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

from .box_builder import build_box_candidate_bundle
from .handoff_verify import (
    BoxHandoffVerifyError,
    MAX_ARCHIVE_BYTES,
    MAX_MEMBER_BYTES,
    MAX_MEMBER_COUNT,
    MAX_TOTAL_UNCOMPRESSED_BYTES,
    _load_json_object,
    _read_private_bundle,
    _safe_member_name,
    _verify_bundle_body,
)


class BoxHandoffUnpackError(ValueError):
    """Raised when a verified handoff cannot be safely materialized or reverified."""


RECEIPT_NAME = "handoff-unpack-receipt.json"
RECEIPT_SCHEMA_VERSION = 1
MAX_RECEIPT_BYTES = 2 * 1024 * 1024
RECEIPT_KEYS = {
    "schema_version",
    "product",
    "installed_at",
    "actor",
    "bundle_sha256",
    "bundle_size_bytes",
    "runtime_fingerprint",
    "member_count",
    "manifest_file_count",
    "files",
    "content_fingerprint",
    "source_archive_bytes_matched_current_builder",
    "archive_verified_before_unpack",
    "installed_pack_reproducible",
    "destination_preexisted",
    "files_overwritten",
    "archive_members_executed",
    "source_bundle_deleted_during_unpack",
    "credentials_persisted",
    "financial_values_added",
    "authoritative_financial_evidence",
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


def _validate_actor(actor: str) -> str:
    if not isinstance(actor, str):
        raise BoxHandoffUnpackError("handoff unpack actor must be a string")
    normalized = actor.strip()
    if (
        not normalized
        or normalized != actor
        or len(normalized) > 200
        or any(ord(character) < 32 for character in normalized)
    ):
        raise BoxHandoffUnpackError(
            "handoff unpack actor must be 1-200 visible characters without padding"
        )
    return normalized


def _new_destination(root: str | Path) -> Path:
    requested = Path(root).expanduser()
    if not requested.is_absolute() or requested.name in {"", ".", ".."}:
        raise BoxHandoffUnpackError("handoff unpack root must be a new absolute directory")
    parent = requested.parent.resolve()
    if parent.is_symlink() or not parent.is_dir():
        raise BoxHandoffUnpackError("handoff unpack parent must be an existing real directory")
    destination = parent / requested.name
    if destination.exists() or destination.is_symlink():
        raise BoxHandoffUnpackError("handoff unpack root already exists; refusing to overwrite")
    return destination


def _existing_destination(root: str | Path) -> Path:
    requested = Path(root).expanduser()
    if not requested.is_absolute():
        raise BoxHandoffUnpackError("handoff unpack root must be an absolute directory")
    if requested.is_symlink():
        raise BoxHandoffUnpackError("handoff unpack root must be a real directory")
    try:
        destination = requested.resolve(strict=True)
    except OSError as exc:
        raise BoxHandoffUnpackError("handoff unpack root must be a real directory") from exc
    if not destination.is_dir():
        raise BoxHandoffUnpackError("handoff unpack root must be a real directory")
    return destination


def _ensure_private_directory(path: Path) -> None:
    if path.is_symlink() or not path.is_dir():
        raise BoxHandoffUnpackError("handoff unpack encountered a non-directory path")
    if os.name != "nt" and stat.S_IMODE(path.stat().st_mode) != 0o700:
        raise BoxHandoffUnpackError("handoff unpack directories must use mode 0700")


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
        raise BoxHandoffUnpackError(
            "handoff unpack refused to overwrite or follow a destination file"
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


def _read_private_installed_file(
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
        raise BoxHandoffUnpackError(
            "handoff unpack file must be an owner-private regular file"
        ) from exc
    try:
        file_stat = os.fstat(descriptor)
        if (
            not stat.S_ISREG(file_stat.st_mode)
            or file_stat.st_size < 0
            or file_stat.st_size > maximum_bytes
        ):
            raise BoxHandoffUnpackError("handoff unpack file size or type is invalid")
        if expected_size is not None and file_stat.st_size != expected_size:
            raise BoxHandoffUnpackError("handoff unpack file does not match its receipt")
        if os.name != "nt" and (
            stat.S_IMODE(file_stat.st_mode) != 0o600 or file_stat.st_nlink != 1
        ):
            raise BoxHandoffUnpackError("handoff unpack files must use mode 0600")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            body = stream.read(maximum_bytes + 1)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(body) != file_stat.st_size:
        raise BoxHandoffUnpackError("handoff unpack file changed during verification")
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


def _file_records(member_bodies: dict[str, bytes]) -> list[dict[str, Any]]:
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
    records = _file_records(member_bodies)
    payload = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "product": "opc-finance-box",
        "installed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
            "+00:00", "Z",
        ),
        "actor": actor,
        "bundle_sha256": verification["bundle_sha256"],
        "bundle_size_bytes": verification["size_bytes"],
        "runtime_fingerprint": verification["runtime_fingerprint"],
        "member_count": verification["member_count"],
        "manifest_file_count": verification["manifest_file_count"],
        "files": records,
        "content_fingerprint": _sha256(_canonical_bytes(records)),
        "source_archive_bytes_matched_current_builder": verification[
            "archive_bytes_match_current_builder"
        ],
        "archive_verified_before_unpack": True,
        "installed_pack_reproducible": True,
        "destination_preexisted": False,
        "files_overwritten": False,
        "archive_members_executed": False,
        "source_bundle_deleted_during_unpack": False,
        "credentials_persisted": False,
        "financial_values_added": False,
        "authoritative_financial_evidence": False,
        "external_actions_performed": False,
        "receipt_is_digital_signature": False,
    }
    payload["receipt_payload_sha256"] = _sha256(_canonical_bytes(payload))
    return payload


def _receipt_body(receipt: dict[str, Any]) -> bytes:
    return (json.dumps(receipt, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def unpack_box_candidate_bundle(
    bundle: str | Path,
    packs_root: str | Path,
    destination_root: str | Path,
    *,
    actor: str,
) -> dict[str, Any]:
    """Verify once, then materialize those exact bytes into a new private fork root."""
    actor = _validate_actor(actor)
    destination = _new_destination(destination_root)
    try:
        body = _read_private_bundle(bundle)
    except BoxHandoffVerifyError as exc:
        raise BoxHandoffUnpackError(str(exc)) from exc
    return _unpack_box_candidate_body(
        body,
        packs_root,
        destination,
        actor=actor,
        source_bundle_retained=True,
    )


def _unpack_box_candidate_body(
    body: bytes,
    packs_root: str | Path,
    destination: Path,
    *,
    actor: str,
    source_bundle_retained: bool,
) -> dict[str, Any]:
    """Materialize one already-read candidate body after full in-memory verification."""
    try:
        verification, member_bodies = _verify_bundle_body(body, packs_root)
    except BoxHandoffVerifyError as exc:
        raise BoxHandoffUnpackError(str(exc)) from exc
    if RECEIPT_NAME in member_bodies:
        raise BoxHandoffUnpackError("handoff bundle collides with the install receipt")
    try:
        os.mkdir(destination, 0o700)
    except OSError as exc:
        raise BoxHandoffUnpackError(
            "handoff unpack root could not be created without overwrite"
        ) from exc
    if os.name != "nt":
        destination.chmod(0o700)
    receipt_path = destination / RECEIPT_NAME
    try:
        for name, member_body in sorted(member_bodies.items()):
            relative = PurePosixPath(name)
            if not _safe_member_name(name):
                raise BoxHandoffUnpackError("verified handoff contains an unsafe member path")
            _create_parent_directories(destination, relative.parent)
            _write_private_file(destination.joinpath(*relative.parts), member_body)
        receipt = _build_receipt(verification, member_bodies, actor=actor)
        receipt_body = _receipt_body(receipt)
        _write_private_file(receipt_path, receipt_body)
        _fsync_directories(destination)
        verified = verify_unpacked_box_candidate(destination, packs_root)
    except Exception as exc:
        if receipt_path.is_file() and not receipt_path.is_symlink():
            receipt_path.unlink()
        if isinstance(exc, BoxHandoffUnpackError):
            raise
        raise BoxHandoffUnpackError(
            "handoff unpack did not complete; destination has no valid receipt"
        ) from exc
    return {
        "schema_version": 1,
        "unpacked": True,
        "bundle_sha256": verification["bundle_sha256"],
        "runtime_fingerprint": verification["runtime_fingerprint"],
        "extracted_member_count": verification["member_count"],
        "installed_file_count": verification["member_count"] + 1,
        "directory_count": verified["directory_count"],
        "receipt_sha256": _sha256(receipt_body),
        "content_fingerprint": receipt["content_fingerprint"],
        "receipt_written_last": True,
        "installed_tree_verified": True,
        "source_bundle_retained": source_bundle_retained,
        "source_bundle_deleted_during_unpack": False,
        "files_overwritten": False,
        "archive_members_executed": False,
        "destination_path_returned": False,
        "actor_returned": False,
        "financial_values_returned": False,
        "credentials_returned": False,
        "authoritative_financial_evidence": False,
        "external_actions_performed": False,
        "active_runtime_changed": False,
    }


def _load_receipt(path: Path) -> tuple[dict[str, Any], bytes]:
    if path.is_symlink() or not path.is_file():
        raise BoxHandoffUnpackError("handoff unpack receipt is missing")
    body = _read_private_installed_file(path, maximum_bytes=MAX_RECEIPT_BYTES)
    if not body:
        raise BoxHandoffUnpackError("handoff unpack receipt size is invalid")
    try:
        receipt = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BoxHandoffUnpackError("handoff unpack receipt is not valid JSON") from exc
    if not isinstance(receipt, dict) or set(receipt) != RECEIPT_KEYS:
        raise BoxHandoffUnpackError("handoff unpack receipt contract is invalid")
    return receipt, body


def _validate_receipt(receipt: dict[str, Any]) -> list[dict[str, Any]]:
    if (
        receipt.get("schema_version") != RECEIPT_SCHEMA_VERSION
        or receipt.get("product") != "opc-finance-box"
        or receipt.get("archive_verified_before_unpack") is not True
        or receipt.get("installed_pack_reproducible") is not True
        or receipt.get("destination_preexisted") is not False
        or receipt.get("files_overwritten") is not False
        or receipt.get("archive_members_executed") is not False
        or receipt.get("source_bundle_deleted_during_unpack") is not False
        or receipt.get("credentials_persisted") is not False
        or receipt.get("financial_values_added") is not False
        or receipt.get("authoritative_financial_evidence") is not False
        or receipt.get("external_actions_performed") is not False
        or receipt.get("receipt_is_digital_signature") is not False
    ):
        raise BoxHandoffUnpackError("handoff unpack receipt safety boundary is invalid")
    _validate_actor(receipt.get("actor"))
    installed_at_value = receipt.get("installed_at")
    if not isinstance(installed_at_value, str):
        raise BoxHandoffUnpackError("handoff unpack receipt timestamp is invalid")
    try:
        installed_at = datetime.fromisoformat(installed_at_value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise BoxHandoffUnpackError("handoff unpack receipt timestamp is invalid") from exc
    if installed_at.tzinfo is None:
        raise BoxHandoffUnpackError("handoff unpack receipt timestamp is invalid")
    for key in ("bundle_sha256", "runtime_fingerprint", "content_fingerprint", "receipt_payload_sha256"):
        if not isinstance(receipt.get(key), str) or re.fullmatch(
            r"[0-9a-f]{64}", receipt[key],
        ) is None:
            raise BoxHandoffUnpackError("handoff unpack receipt fingerprint is invalid")
    for key in ("bundle_size_bytes", "member_count", "manifest_file_count"):
        value = receipt.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise BoxHandoffUnpackError("handoff unpack receipt count is invalid")
    if (
        receipt["bundle_size_bytes"] > MAX_ARCHIVE_BYTES
        or receipt["member_count"] > MAX_MEMBER_COUNT
        or receipt["manifest_file_count"] >= receipt["member_count"]
    ):
        raise BoxHandoffUnpackError("handoff unpack receipt count exceeds the allowed limit")
    if not isinstance(receipt.get("source_archive_bytes_matched_current_builder"), bool):
        raise BoxHandoffUnpackError("handoff unpack receipt archive comparison is invalid")
    records = receipt.get("files")
    if not isinstance(records, list) or len(records) != receipt["member_count"]:
        raise BoxHandoffUnpackError("handoff unpack receipt file count is invalid")
    paths: list[str] = []
    total_size = 0
    for record in records:
        if not isinstance(record, dict) or set(record) != FILE_RECORD_KEYS:
            raise BoxHandoffUnpackError("handoff unpack receipt file record is invalid")
        path = record.get("path")
        size = record.get("size_bytes")
        sha256 = record.get("sha256")
        if (
            not isinstance(path, str)
            or not _safe_member_name(path)
            or path == RECEIPT_NAME
            or not isinstance(size, int)
            or isinstance(size, bool)
            or not 0 <= size <= MAX_MEMBER_BYTES
            or not isinstance(sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", sha256) is None
        ):
            raise BoxHandoffUnpackError("handoff unpack receipt file record is invalid")
        paths.append(path)
        total_size += size
        if total_size > MAX_TOTAL_UNCOMPRESSED_BYTES:
            raise BoxHandoffUnpackError(
                "handoff unpack receipt content exceeds the allowed limit"
            )
    if paths != sorted(paths) or len(paths) != len(set(paths)):
        raise BoxHandoffUnpackError("handoff unpack receipt file paths are invalid")
    if _sha256(_canonical_bytes(records)) != receipt["content_fingerprint"]:
        raise BoxHandoffUnpackError("handoff unpack content fingerprint is invalid")
    unsigned = {key: value for key, value in receipt.items() if key != "receipt_payload_sha256"}
    if _sha256(_canonical_bytes(unsigned)) != receipt["receipt_payload_sha256"]:
        raise BoxHandoffUnpackError("handoff unpack receipt payload fingerprint is invalid")
    return records


def _inspect_private_tree(
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
                raise BoxHandoffUnpackError("handoff unpack tree contains a symbolic link")
            _ensure_private_directory(path)
            actual_directories.add(path.relative_to(root).as_posix())
            if len(actual_directories) > MAX_MEMBER_COUNT:
                raise BoxHandoffUnpackError("handoff unpack tree exceeds the allowed limit")
        for name in files:
            path = current_path / name
            if path.is_symlink():
                raise BoxHandoffUnpackError("handoff unpack tree contains a symbolic link")
            if not path.is_file():
                raise BoxHandoffUnpackError("handoff unpack tree contains a non-regular file")
            file_stat = path.stat()
            if os.name != "nt" and (
                stat.S_IMODE(file_stat.st_mode) != 0o600 or file_stat.st_nlink != 1
            ):
                raise BoxHandoffUnpackError("handoff unpack files must use mode 0600")
            actual_files.add(path.relative_to(root).as_posix())
            if len(actual_files) > MAX_MEMBER_COUNT + 1:
                raise BoxHandoffUnpackError("handoff unpack tree exceeds the allowed limit")
    if actual_files != expected_files or actual_directories != expected_directories:
        raise BoxHandoffUnpackError("handoff unpack tree does not match its receipt")
    record_by_path = {record["path"]: record for record in records}
    bodies: dict[str, bytes] = {}
    for relative, record in record_by_path.items():
        path = root.joinpath(*PurePosixPath(relative).parts)
        body = _read_private_installed_file(
            path, maximum_bytes=MAX_MEMBER_BYTES, expected_size=record["size_bytes"],
        )
        if len(body) != record["size_bytes"] or _sha256(body) != record["sha256"]:
            raise BoxHandoffUnpackError("handoff unpack file does not match its receipt")
        bodies[relative] = body
    return bodies, len(actual_directories) + 1


def verify_unpacked_box_candidate(
    root: str | Path,
    packs_root: str | Path,
) -> dict[str, Any]:
    """Reverify a materialized handoff from private files and its non-signing receipt."""
    destination = _existing_destination(root)
    _ensure_private_directory(destination)
    receipt, receipt_body = _load_receipt(destination / RECEIPT_NAME)
    records = _validate_receipt(receipt)
    member_bodies, directory_count = _inspect_private_tree(destination, records)
    try:
        spec = _load_json_object(member_bodies["box-spec.json"], "Box specification")
        manifest = _load_json_object(
            member_bodies["bundle-manifest.json"], "manifest",
        )
        compiled_lock = _load_json_object(
            member_bodies["compiled/box.lock.json"], "compiled lock",
        )
    except (KeyError, BoxHandoffVerifyError) as exc:
        raise BoxHandoffUnpackError("handoff unpack required product member is invalid") from exc
    if (
        manifest.get("runtime_fingerprint") != receipt["runtime_fingerprint"]
        or (compiled_lock.get("lock") or {}).get("runtime_fingerprint")
        != receipt["runtime_fingerprint"]
        or manifest.get("file_count") != receipt["manifest_file_count"]
    ):
        raise BoxHandoffUnpackError("handoff unpack runtime binding is invalid")
    try:
        expected_body, _, expected_manifest = build_box_candidate_bundle(spec, packs_root)
        with zipfile.ZipFile(io.BytesIO(expected_body), "r") as archive:
            expected_bodies = {
                info.filename: archive.read(info) for info in archive.infolist()
            }
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        raise BoxHandoffUnpackError(
            "handoff unpack cannot be reproduced with the installed Pack catalog"
        ) from exc
    if expected_manifest != manifest or expected_bodies != member_bodies:
        raise BoxHandoffUnpackError(
            "handoff unpack does not reproduce with the installed Pack catalog"
        )
    return {
        "schema_version": 1,
        "valid": True,
        "runtime_fingerprint": receipt["runtime_fingerprint"],
        "bundle_sha256": receipt["bundle_sha256"],
        "receipt_sha256": _sha256(receipt_body),
        "content_fingerprint": receipt["content_fingerprint"],
        "member_count": receipt["member_count"],
        "installed_file_count": receipt["member_count"] + 1,
        "directory_count": directory_count,
        "installed_pack_reproducible": True,
        "source_bundle_sha_matches_current_builder": (
            _sha256(expected_body) == receipt["bundle_sha256"]
        ),
        "source_bundle_required": False,
        "paths_returned": False,
        "actor_returned": False,
        "financial_values_returned": False,
        "credentials_returned": False,
        "receipt_is_digital_signature": False,
        "authoritative_financial_evidence": False,
        "archive_members_executed": False,
        "external_actions_performed": False,
        "active_runtime_changed": False,
    }
