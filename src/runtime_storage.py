from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PRODUCT = "opc-finance-box"
CURRENT_LAYOUT_VERSION = 3
MANIFEST_NAME = "runtime-data-manifest.json"
BACKUP_MANIFEST_NAME = "runtime-data-backup.json"
RESTORE_RECEIPT_NAME = "runtime-data-restore-receipt.json"
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_FILE_BYTES = 256 * 1024 * 1024
MAX_TOTAL_BYTES = 1024 * 1024 * 1024
MAX_FILES = 10_000
ACTOR_PATTERN = re.compile(r"^[^\x00-\x1f\x7f]{1,80}$")

V1_STORE_CONTRACT = {
    "general_ledger": {"path": "ledger", "kind": "directory", "schema_version": 1},
    "agent_runtime": {"path": "agent_runtime", "kind": "directory", "schema_version": 1},
    "finance_inbox": {"path": "finance_inbox", "kind": "directory", "schema_version": 1},
    "pipeline_runs": {"path": "pipeline_runs", "kind": "directory", "schema_version": 1},
    "company_profile": {"path": "company_profile.json", "kind": "optional_file", "schema_version": 1},
    "import_templates": {"path": "import_templates.json", "kind": "optional_file", "schema_version": 1},
}
V2_STORE_CONTRACT = {
    **V1_STORE_CONTRACT,
    "connector_sync": {"path": "connector_sync", "kind": "directory", "schema_version": 1},
}
STORE_CONTRACT = {
    **V2_STORE_CONTRACT,
    "release_promotion": {
        "path": "release_promotion", "kind": "directory", "schema_version": 1,
    },
}
LAYOUT_STORE_CONTRACTS = {
    1: V1_STORE_CONTRACT,
    2: V2_STORE_CONTRACT,
    3: STORE_CONTRACT,
}


class RuntimeStorageError(RuntimeError):
    """Raised when runtime data cannot be safely initialized, backed up or restored."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _actor(value: Any) -> str:
    actor = str(value or "").strip()
    if not ACTOR_PATTERN.fullmatch(actor):
        raise RuntimeStorageError("actor must be 1-80 printable characters")
    return actor


def _canonical(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )


def _safe_root(value: str | Path, *, label: str) -> Path:
    root = Path(value).expanduser().resolve()
    anchor = Path(root.anchor).resolve()
    if root == anchor or root == Path.home().resolve():
        raise RuntimeStorageError(f"{label} must not be a filesystem root or home directory")
    return root


def _private_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _ensure_private_parent_tree(base: Path, relative_file: str) -> Path:
    current = base
    os.chmod(current, 0o700)
    parts = Path(relative_file).parts
    for part in parts[:-1]:
        current = current / part
        current.mkdir(exist_ok=True, mode=0o700)
        if current.is_symlink() or not current.is_dir():
            raise RuntimeStorageError(f"backup path is not a real directory: {part}")
        os.chmod(current, 0o700)
    return base / relative_file


def _scan_files(root: Path, *, hashes: bool) -> list[dict[str, Any]]:
    if root.is_symlink() or not root.is_dir():
        raise RuntimeStorageError("runtime data root must be a real directory")
    records: list[dict[str, Any]] = []
    total_bytes = 0
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise RuntimeStorageError(f"cannot inspect runtime path: {path.name}") from exc
        relative = path.relative_to(root).as_posix()
        if stat.S_ISLNK(metadata.st_mode):
            raise RuntimeStorageError(f"runtime data must not contain symlinks: {relative}")
        if stat.S_ISDIR(metadata.st_mode):
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise RuntimeStorageError(f"runtime data must contain regular files only: {relative}")
        if metadata.st_size > MAX_FILE_BYTES:
            raise RuntimeStorageError(f"runtime file exceeds 256 MiB: {relative}")
        total_bytes += metadata.st_size
        if total_bytes > MAX_TOTAL_BYTES:
            raise RuntimeStorageError("runtime data exceeds the supported 1 GiB backup size")
        if len(records) >= MAX_FILES:
            raise RuntimeStorageError("runtime data exceeds the supported 10000 file count")
        record: dict[str, Any] = {
            "path": relative,
            "size_bytes": metadata.st_size,
        }
        if hashes:
            try:
                body = path.read_bytes()
            except OSError as exc:
                raise RuntimeStorageError(f"cannot read runtime file: {relative}") from exc
            if len(body) != metadata.st_size:
                raise RuntimeStorageError(f"runtime file changed while being scanned: {relative}")
            record["sha256"] = hashlib.sha256(body).hexdigest()
        records.append(record)
    return records


def _inventory_fingerprint(records: list[dict[str, Any]]) -> str:
    return hashlib.sha256(_canonical(records)).hexdigest()


def _load_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        if path.stat().st_size > MAX_MANIFEST_BYTES:
            raise RuntimeStorageError(f"{label} exceeds 1 MiB")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except RuntimeStorageError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeStorageError(f"{label} is missing or invalid") from exc
    if not isinstance(payload, dict):
        raise RuntimeStorageError(f"{label} must be a JSON object")
    return payload


def _validate_layout_manifest(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("schema_version") != 1 or payload.get("product") != PRODUCT:
        raise RuntimeStorageError("unsupported runtime data manifest")
    version = payload.get("layout_version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise RuntimeStorageError("runtime data manifest has an invalid layout_version")
    layout_id = payload.get("layout_id")
    try:
        uuid.UUID(str(layout_id))
    except (ValueError, AttributeError) as exc:
        raise RuntimeStorageError("runtime data manifest has an invalid layout_id") from exc
    expected_contract = LAYOUT_STORE_CONTRACTS.get(version)
    if expected_contract is None and version <= CURRENT_LAYOUT_VERSION:
        raise RuntimeStorageError("runtime data manifest uses an unsupported historical layout")
    if expected_contract is not None and payload.get("stores") != expected_contract:
        raise RuntimeStorageError("runtime data manifest store contract does not match this product")
    if expected_contract is None and not isinstance(payload.get("stores"), dict):
        raise RuntimeStorageError("future runtime data manifest has an invalid store contract")
    return payload


def inspect_runtime_data(root: str | Path) -> dict[str, Any]:
    root = _safe_root(root, label="runtime data root")
    if not root.exists():
        return {
            "schema_version": 1,
            "state": "absent",
            "root": str(root),
            "compatible": False,
            "initialization_required": True,
            "adoption_required": False,
            "current_layout_version": None,
            "target_layout_version": CURRENT_LAYOUT_VERSION,
            "file_count": 0,
            "total_bytes": 0,
            "external_actions_performed": False,
        }
    records = _scan_files(root, hashes=False)
    total_bytes = sum(record["size_bytes"] for record in records)
    manifest_path = root / MANIFEST_NAME
    private_permissions = stat.S_IMODE(root.stat().st_mode) & 0o077 == 0
    if not manifest_path.exists():
        return {
            "schema_version": 1,
            "state": "uninitialized",
            "root": str(root),
            "compatible": False,
            "initialization_required": True,
            "adoption_required": bool(records),
            "current_layout_version": None,
            "target_layout_version": CURRENT_LAYOUT_VERSION,
            "file_count": len(records),
            "total_bytes": total_bytes,
            "private_permissions": private_permissions,
            "external_actions_performed": False,
        }
    manifest = _validate_layout_manifest(_load_json(manifest_path, label="runtime data manifest"))
    version = manifest["layout_version"]
    compatible = version == CURRENT_LAYOUT_VERSION and private_permissions
    state = (
        "future_layout_blocked"
        if version > CURRENT_LAYOUT_VERSION
        else "migration_required" if version < CURRENT_LAYOUT_VERSION
        else "ready" if private_permissions else "unsafe_permissions"
    )
    return {
        "schema_version": 1,
        "state": state,
        "root": str(root),
        "compatible": compatible,
        "initialization_required": False,
        "adoption_required": False,
        "current_layout_version": version,
        "target_layout_version": CURRENT_LAYOUT_VERSION,
        "layout_id": manifest["layout_id"],
        "file_count": len(records),
        "total_bytes": total_bytes,
        "private_permissions": private_permissions,
        "stores": manifest["stores"],
        "external_actions_performed": False,
    }


def runtime_upgrade_preflight(root: str | Path) -> dict[str, Any]:
    inspection = inspect_runtime_data(root)
    state = inspection["state"]
    if state == "ready":
        decision = "no_change"
        compatible = True
        backup_required = False
    elif state == "absent":
        decision = "initialize_new_layout"
        compatible = False
        backup_required = False
    elif state == "uninitialized":
        decision = "adopt_legacy_layout" if inspection["adoption_required"] else "initialize_new_layout"
        compatible = False
        backup_required = bool(inspection["adoption_required"])
    elif state == "unsafe_permissions":
        decision = "fix_root_permissions_before_start"
        compatible = False
        backup_required = False
    elif state == "migration_required":
        decision = "offline_migration_required"
        compatible = False
        backup_required = True
    else:
        decision = "blocked_by_newer_layout"
        compatible = False
        backup_required = True
    return {
        "schema_version": 1,
        "compatible": compatible,
        "decision": decision,
        "current_layout_version": inspection["current_layout_version"],
        "target_layout_version": CURRENT_LAYOUT_VERSION,
        "backup_required_before_change": backup_required,
        "service_stop_required_before_change": state not in {"absent", "ready"},
        "automatic_in_place_migration_available": False,
        "restore_requires_new_target": True,
        "inspection": inspection,
        "external_actions_performed": False,
    }


def initialize_runtime_data(
    root: str | Path,
    *,
    actor: str,
    adopt_existing: bool = False,
) -> dict[str, Any]:
    root = _safe_root(root, label="runtime data root")
    actor = _actor(actor)
    if root.exists() and root.is_symlink():
        raise RuntimeStorageError("runtime data root must not be a symlink")
    if root.exists() and not root.is_dir():
        raise RuntimeStorageError("runtime data root must be a directory")
    if (root / MANIFEST_NAME).exists():
        inspection = inspect_runtime_data(root)
        if not inspection["compatible"]:
            raise RuntimeStorageError("runtime data layout is not compatible with this product version")
        return {"initialized": False, "already_initialized": True, **inspection}
    existing_records = _scan_files(root, hashes=True) if root.exists() else []
    if existing_records and not adopt_existing:
        raise RuntimeStorageError("runtime data root is not empty; rerun with explicit legacy adoption")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    for contract in STORE_CONTRACT.values():
        if contract["kind"] == "directory":
            directory = root / contract["path"]
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(directory, 0o700)
    manifest = {
        "schema_version": 1,
        "product": PRODUCT,
        "layout_version": CURRENT_LAYOUT_VERSION,
        "layout_id": str(uuid.uuid4()),
        "created_at": _now(),
        "created_by": actor,
        "adopted_existing_data": bool(existing_records),
        "adoption_file_count": len(existing_records),
        "adoption_inventory_sha256": _inventory_fingerprint(existing_records),
        "stores": STORE_CONTRACT,
        "migration_policy": {
            "backup_before_layout_change": True,
            "in_place_automatic_migration": False,
            "restore_requires_new_target": True,
            "http_restore_enabled": False,
        },
    }
    _private_write(root / MANIFEST_NAME, _canonical(manifest))
    _fsync_directory(root)
    inspection = inspect_runtime_data(root)
    return {
        "initialized": True,
        "already_initialized": False,
        "adopted_existing_data": bool(existing_records),
        **inspection,
    }


def backup_runtime_data(
    root: str | Path,
    destination: str | Path,
    *,
    actor: str,
    service_stopped_confirmed: bool,
) -> dict[str, Any]:
    root = _safe_root(root, label="runtime data root")
    destination = _safe_root(destination, label="backup destination")
    actor = _actor(actor)
    if not service_stopped_confirmed:
        raise RuntimeStorageError("offline backup requires explicit confirmation that the service is stopped")
    inspection = inspect_runtime_data(root)
    if inspection["state"] not in {"ready", "migration_required"}:
        raise RuntimeStorageError(
            "runtime data must use a supported initialized layout before backup"
        )
    if destination.exists():
        raise RuntimeStorageError("backup destination already exists; backups never overwrite")
    if not destination.parent.is_dir():
        raise RuntimeStorageError("backup destination parent must already exist")
    if destination.is_relative_to(root):
        raise RuntimeStorageError("backup destination must be outside the runtime data root")
    records = _scan_files(root, hashes=True)
    backup_id = uuid.uuid4().hex
    destination.mkdir(mode=0o700)
    os.chmod(destination, 0o700)
    payload_root = destination / "payload"
    payload_root.mkdir(mode=0o700)
    for record in records:
        source = root / record["path"]
        body = source.read_bytes()
        if len(body) != record["size_bytes"] or hashlib.sha256(body).hexdigest() != record["sha256"]:
            raise RuntimeStorageError(f"runtime file changed during offline backup: {record['path']}")
        _private_write(_ensure_private_parent_tree(payload_root, record["path"]), body)
    after_records = _scan_files(root, hashes=True)
    if after_records != records:
        raise RuntimeStorageError("runtime data changed during offline backup; partial backup was left for inspection")
    manifest = {
        "schema_version": 1,
        "product": PRODUCT,
        "backup_id": backup_id,
        "created_at": _now(),
        "created_by": actor,
        "source_layout_version": inspection["current_layout_version"],
        "source_layout_id": inspection["layout_id"],
        "payload_directory": "payload",
        "file_count": len(records),
        "total_bytes": sum(record["size_bytes"] for record in records),
        "inventory_sha256": _inventory_fingerprint(records),
        "files": records,
        "service_stopped_confirmed": True,
        "contains_sensitive_runtime_data": True,
        "encrypted_by_tool": False,
        "restore_requires_new_target": True,
        "external_actions_performed": False,
    }
    _private_write(destination / BACKUP_MANIFEST_NAME, _canonical(manifest))
    _fsync_directory(destination)
    return {
        "valid": True,
        "backup_id": backup_id,
        "backup_path": str(destination),
        "layout_version": inspection["current_layout_version"],
        "file_count": manifest["file_count"],
        "total_bytes": manifest["total_bytes"],
        "inventory_sha256": manifest["inventory_sha256"],
        "contains_sensitive_runtime_data": True,
        "encrypted_by_tool": False,
        "restore_requires_new_target": True,
        "external_actions_performed": False,
    }


def verify_runtime_backup(backup: str | Path) -> dict[str, Any]:
    backup = _safe_root(backup, label="runtime backup")
    if backup.is_symlink() or not backup.is_dir():
        raise RuntimeStorageError("runtime backup must be a real directory")
    for path in (backup, *sorted(backup.rglob("*"), key=lambda item: item.as_posix())):
        metadata = path.lstat()
        relative = "." if path == backup else path.relative_to(backup).as_posix()
        if stat.S_ISLNK(metadata.st_mode):
            raise RuntimeStorageError(f"runtime backup must not contain symlinks: {relative}")
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            raise RuntimeStorageError(f"runtime backup permissions are not private: {relative}")
    manifest = _load_json(backup / BACKUP_MANIFEST_NAME, label="runtime backup manifest")
    if manifest.get("schema_version") != 1 or manifest.get("product") != PRODUCT:
        raise RuntimeStorageError("unsupported runtime backup manifest")
    if manifest.get("payload_directory") != "payload" or manifest.get("restore_requires_new_target") is not True:
        raise RuntimeStorageError("runtime backup has an unsafe restore contract")
    records = manifest.get("files")
    if not isinstance(records, list) or any(not isinstance(record, dict) for record in records):
        raise RuntimeStorageError("runtime backup file inventory is invalid")
    payload_root = backup / "payload"
    actual = _scan_files(payload_root, hashes=True)
    if actual != records:
        raise RuntimeStorageError("runtime backup payload does not match its inventory")
    if manifest.get("file_count") != len(actual):
        raise RuntimeStorageError("runtime backup file count mismatch")
    total_bytes = sum(record["size_bytes"] for record in actual)
    if manifest.get("total_bytes") != total_bytes:
        raise RuntimeStorageError("runtime backup byte count mismatch")
    inventory_sha256 = _inventory_fingerprint(actual)
    if manifest.get("inventory_sha256") != inventory_sha256:
        raise RuntimeStorageError("runtime backup inventory fingerprint mismatch")
    layout_manifest_record = next((record for record in actual if record["path"] == MANIFEST_NAME), None)
    if layout_manifest_record is None:
        raise RuntimeStorageError("runtime backup is missing its layout manifest")
    layout_manifest = _validate_layout_manifest(
        _load_json(payload_root / MANIFEST_NAME, label="backed-up runtime data manifest")
    )
    if manifest.get("source_layout_id") != layout_manifest["layout_id"]:
        raise RuntimeStorageError("runtime backup layout identity mismatch")
    if manifest.get("source_layout_version") != layout_manifest["layout_version"]:
        raise RuntimeStorageError("runtime backup layout version mismatch")
    return {
        "valid": True,
        "backup_id": manifest.get("backup_id"),
        "backup_path": str(backup),
        "layout_version": layout_manifest["layout_version"],
        "layout_id": layout_manifest["layout_id"],
        "file_count": len(actual),
        "total_bytes": total_bytes,
        "inventory_sha256": inventory_sha256,
        "contains_sensitive_runtime_data": True,
        "encrypted_by_tool": False,
        "restore_requires_new_target": True,
        "external_actions_performed": False,
    }


def migrate_runtime_data(
    root: str | Path,
    backup: str | Path,
    *,
    actor: str,
    service_stopped_confirmed: bool,
) -> dict[str, Any]:
    root = _safe_root(root, label="runtime data root")
    actor = _actor(actor)
    if not service_stopped_confirmed:
        raise RuntimeStorageError(
            "offline migration requires explicit confirmation that workbench and scheduler are stopped"
        )
    inspection = inspect_runtime_data(root)
    if inspection["state"] != "migration_required":
        raise RuntimeStorageError("runtime data does not require a supported migration")
    source_version = inspection["current_layout_version"]
    source_contract = LAYOUT_STORE_CONTRACTS.get(source_version)
    if source_contract is None or source_version >= CURRENT_LAYOUT_VERSION:
        raise RuntimeStorageError("no explicit migration path is available for this layout")
    verified = verify_runtime_backup(backup)
    if (
        verified["layout_version"] != inspection["current_layout_version"]
        or verified["layout_id"] != inspection["layout_id"]
    ):
        raise RuntimeStorageError("verified backup does not belong to this runtime data layout")
    backup_manifest = _load_json(
        _safe_root(backup, label="runtime backup") / BACKUP_MANIFEST_NAME,
        label="runtime backup manifest",
    )
    before_records = _scan_files(root, hashes=True)
    if before_records != backup_manifest.get("files"):
        raise RuntimeStorageError(
            "runtime data changed after the verified backup; create a new stopped-service backup"
        )
    new_stores = []
    for name, contract in STORE_CONTRACT.items():
        if name in source_contract:
            continue
        if contract["kind"] != "directory":
            raise RuntimeStorageError("explicit migration supports only additive directory stores")
        directory = root / contract["path"]
        directory.mkdir(mode=0o700)
        os.chmod(directory, 0o700)
        new_stores.append(name)
    old_manifest = _validate_layout_manifest(
        _load_json(root / MANIFEST_NAME, label="runtime data manifest")
    )
    migrated = dict(old_manifest)
    migrated.update({
        "layout_version": CURRENT_LAYOUT_VERSION,
        "stores": STORE_CONTRACT,
        "migrated_at": _now(),
        "migrated_by": actor,
        "migration": {
            "from_layout_version": source_version,
            "to_layout_version": CURRENT_LAYOUT_VERSION,
            "verified_backup_id": verified["backup_id"],
            "verified_backup_inventory_sha256": verified["inventory_sha256"],
            "service_stopped_confirmed": True,
            "data_files_rewritten": False,
            "new_stores_initialized": new_stores,
        },
        "migration_policy": {
            "backup_before_layout_change": True,
            "in_place_automatic_migration": False,
            "explicit_offline_migration": True,
            "restore_requires_new_target": True,
            "http_restore_enabled": False,
        },
    })
    _private_write(root / MANIFEST_NAME, _canonical(migrated))
    _fsync_directory(root)
    after = inspect_runtime_data(root)
    if not after["compatible"]:
        raise RuntimeStorageError("runtime data migration did not produce a compatible layout")
    return {
        "migrated": True,
        "root": str(root),
        "from_layout_version": source_version,
        "to_layout_version": CURRENT_LAYOUT_VERSION,
        "layout_id": after["layout_id"],
        "verified_backup_id": verified["backup_id"],
        "data_files_rewritten": False,
        "new_stores_initialized": new_stores,
        "service_stopped_confirmed": True,
        "external_actions_performed": False,
    }


def restore_runtime_backup(
    backup: str | Path,
    target: str | Path,
    *,
    actor: str,
) -> dict[str, Any]:
    backup = _safe_root(backup, label="runtime backup")
    target = _safe_root(target, label="restore target")
    actor = _actor(actor)
    verified = verify_runtime_backup(backup)
    if target.exists():
        raise RuntimeStorageError("restore target must not exist; restore never overwrites or merges")
    if not target.parent.is_dir():
        raise RuntimeStorageError("restore target parent must already exist")
    if target.is_relative_to(backup):
        raise RuntimeStorageError("restore target must be outside the backup directory")
    manifest = _load_json(backup / BACKUP_MANIFEST_NAME, label="runtime backup manifest")
    records = manifest["files"]
    target.mkdir(mode=0o700)
    os.chmod(target, 0o700)
    payload_root = backup / "payload"
    for record in records:
        body = (payload_root / record["path"]).read_bytes()
        if hashlib.sha256(body).hexdigest() != record["sha256"]:
            raise RuntimeStorageError(f"runtime backup changed during restore: {record['path']}")
        _private_write(_ensure_private_parent_tree(target, record["path"]), body)
    restored_records = _scan_files(target, hashes=True)
    if restored_records != records:
        raise RuntimeStorageError("restored runtime data does not match the verified backup")
    receipt = {
        "schema_version": 1,
        "product": PRODUCT,
        "backup_id": verified["backup_id"],
        "restored_at": _now(),
        "restored_by": actor,
        "layout_version": verified["layout_version"],
        "layout_id": verified["layout_id"],
        "inventory_sha256": verified["inventory_sha256"],
        "target_was_new": True,
        "merge_performed": False,
        "overwrite_performed": False,
        "external_actions_performed": False,
    }
    _private_write(target / RESTORE_RECEIPT_NAME, _canonical(receipt))
    _fsync_directory(target)
    return {
        "restored": True,
        "target_path": str(target),
        "file_count": verified["file_count"],
        **receipt,
    }
