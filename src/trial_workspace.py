from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from .api_auth import load_api_auth_policy
from .box_runtime import BoxRuntime
from .handoff_unpack import (
    BoxHandoffUnpackError,
    _new_destination,
    _validate_actor,
    verify_unpacked_box_candidate,
)
from .runtime_storage import initialize_runtime_data, inspect_runtime_data
from .starter_workspace import initialize_box_starter_workspace


class TrialWorkspaceError(ValueError):
    """Raised when a local trial workspace is incomplete, unsafe or tampered."""


MANIFEST_NAME = "trial-workspace.json"
GUIDE_NAME = "START-HERE.md"
BOX_DIRECTORY = "box"
RUNTIME_DIRECTORY = "runtime-data"
TRIAL_WORKSPACE_ROOT_ENV = "OPC_FINANCE_TRIAL_ROOT"
MAX_MANIFEST_BYTES = 256 * 1024
MAX_SETUP_CHECKLIST_BYTES = 2 * 1024 * 1024
MANIFEST_KEYS = {
    "schema_version",
    "product",
    "artifact_type",
    "created_at",
    "created_by",
    "starter_id",
    "profile_id",
    "country_code",
    "jurisdiction_id",
    "tax_readiness",
    "selected_integrations",
    "box_workspace",
    "box_config",
    "runtime_data",
    "runtime_fingerprint",
    "box_receipt_sha256",
    "runtime_layout_version",
    "runtime_layout_id",
    "launch_policy",
    "control_boundary",
    "manifest_payload_sha256",
}


def _canonical(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _payload_sha256(payload: dict[str, Any]) -> str:
    unsigned = dict(payload)
    unsigned.pop("manifest_payload_sha256", None)
    return hashlib.sha256(_canonical(unsigned)).hexdigest()


def _private_write(path: Path, body: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise TrialWorkspaceError("trial workspace refuses to overwrite or follow files") from exc
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


def _guide() -> bytes:
    return (
        "# OPC Finance Box · 五分钟本地试用\n\n"
        "这个目录把不可变、可复验的 `box/` 与可变的 `runtime-data/` 分开。"
        "不要把凭证写入本目录的 JSON、Markdown 或 Box 配置。\n\n"
        "```bash\n"
        "opc-finance-box trial-verify /absolute/TRIAL_ROOT\n"
        "opc-finance-box trial-onboarding /absolute/TRIAL_ROOT\n"
        "opc-finance-box trial-run /absolute/TRIAL_ROOT\n"
        "```\n\n"
        "启动后访问 `http://127.0.0.1:8765`。默认只允许本机回环访问；"
        "需要非回环监听时必须另行配置 API token 或角色策略文件，并自行提供 TLS、"
        "反向代理和网络访问控制。按 `Ctrl-C` 停止。\n\n"
        "试用模式只展示候选、控制和演示数据，不访问 Connector 网络、不配置凭证、"
        "不认定税务适用性，也不授权过账、付款、报税或其他外部动作。准备接入真实数据时，"
        "先运行 `trial-onboarding` 查看压缩后的五段旅程、优先阻塞项和命令模板，再从"
        " `box/box.json` 另行创建首客 Activation Workspace。\n"
    ).encode("utf-8")


def _existing_root(root: str | Path) -> Path:
    requested = Path(root).expanduser()
    if not requested.is_absolute() or requested.is_symlink():
        raise TrialWorkspaceError("trial workspace root must be an absolute real directory")
    try:
        resolved = requested.resolve(strict=True)
    except OSError as exc:
        raise TrialWorkspaceError("trial workspace root is missing") from exc
    if not resolved.is_dir():
        raise TrialWorkspaceError("trial workspace root must be a directory")
    if os.name != "nt" and stat.S_IMODE(resolved.stat().st_mode) != 0o700:
        raise TrialWorkspaceError("trial workspace root must use mode 0700")
    return resolved


def _read_private_file(path: Path, *, maximum: int) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise TrialWorkspaceError("trial workspace control file must be a regular file") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise TrialWorkspaceError("trial workspace control file must be a regular file")
        if metadata.st_size < 1 or metadata.st_size > maximum:
            raise TrialWorkspaceError("trial workspace control file has an invalid size")
        if os.name != "nt" and stat.S_IMODE(metadata.st_mode) != 0o600:
            raise TrialWorkspaceError("trial workspace control files must use mode 0600")
        chunks: list[bytes] = []
        remaining = metadata.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        body = b"".join(chunks)
        if len(body) != metadata.st_size or os.read(descriptor, 1):
            raise TrialWorkspaceError("trial workspace control file changed while being read")
        return body
    finally:
        os.close(descriptor)


def _load_manifest(root: Path) -> dict[str, Any]:
    try:
        payload = json.loads(
            _read_private_file(root / MANIFEST_NAME, maximum=MAX_MANIFEST_BYTES).decode("utf-8")
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TrialWorkspaceError("trial workspace manifest is invalid JSON") from exc
    if not isinstance(payload, dict) or set(payload) != MANIFEST_KEYS:
        raise TrialWorkspaceError("trial workspace manifest fields are invalid")
    if (
        payload.get("schema_version") != 1
        or payload.get("product") != "opc-finance-box"
        or payload.get("artifact_type") != "opc_finance_box_local_trial_workspace"
    ):
        raise TrialWorkspaceError("trial workspace manifest contract is unsupported")
    expected_hash = _payload_sha256(payload)
    if payload.get("manifest_payload_sha256") != expected_hash:
        raise TrialWorkspaceError("trial workspace manifest fingerprint is invalid")
    return payload


def initialize_trial_workspace(
    *,
    profile: str,
    country: str,
    packs_root: str | Path,
    destination_root: str | Path,
    actor: str,
    integrations: Iterable[str] = (),
    name: str | None = None,
    entity_id: str | None = None,
    entity_name: str | None = None,
) -> dict[str, Any]:
    """Create a complete local demo wrapper around one immutable Starter workspace."""
    try:
        normalized_actor = _validate_actor(actor)
        destination = _new_destination(destination_root)
    except BoxHandoffUnpackError as exc:
        raise TrialWorkspaceError(str(exc)) from exc
    try:
        os.mkdir(destination, 0o700)
        if os.name != "nt":
            destination.chmod(0o700)
        starter = initialize_box_starter_workspace(
            profile=profile,
            country=country,
            packs_root=packs_root,
            destination_root=destination / BOX_DIRECTORY,
            actor=normalized_actor,
            integrations=integrations,
            name=name,
            entity_id=entity_id,
            entity_name=entity_name,
            data_mode="demo",
        )
        runtime = initialize_runtime_data(
            destination / RUNTIME_DIRECTORY,
            actor=normalized_actor,
        )
        manifest: dict[str, Any] = {
            "schema_version": 1,
            "product": "opc-finance-box",
            "artifact_type": "opc_finance_box_local_trial_workspace",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "created_by": normalized_actor,
            "starter_id": starter["starter_id"],
            "profile_id": starter["profile_id"],
            "country_code": starter["country_code"],
            "jurisdiction_id": starter["jurisdiction_id"],
            "tax_readiness": starter["tax_readiness"],
            "selected_integrations": starter["selected_integrations"],
            "box_workspace": BOX_DIRECTORY,
            "box_config": f"{BOX_DIRECTORY}/box.json",
            "runtime_data": RUNTIME_DIRECTORY,
            "runtime_fingerprint": starter["runtime_fingerprint"],
            "box_receipt_sha256": starter["workspace_receipt_sha256"],
            "runtime_layout_version": runtime["current_layout_version"],
            "runtime_layout_id": runtime["layout_id"],
            "launch_policy": {
                "default_host": "127.0.0.1",
                "default_port": 8765,
                "anonymous_access_loopback_only": True,
                "non_loopback_requires_authentication": True,
                "browser_opened_automatically": False,
            },
            "control_boundary": {
                "demo_mode": True,
                "credentials_persisted": False,
                "connector_network_dispatch_performed": False,
                "financial_values_added": False,
                "tax_applicability_determined": False,
                "posting_payment_or_filing_authorized": False,
                "external_actions_performed": False,
            },
        }
        manifest["manifest_payload_sha256"] = _payload_sha256(manifest)
        _private_write(destination / GUIDE_NAME, _guide())
        _private_write(destination / MANIFEST_NAME, _canonical(manifest))
        descriptor = os.open(destination, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except TrialWorkspaceError:
        raise
    except (OSError, ValueError) as exc:
        raise TrialWorkspaceError(
            "trial workspace initialization failed; the incomplete directory was preserved"
        ) from exc
    verified = verify_trial_workspace(destination, packs_root)
    return {
        "schema_version": 1,
        "initialized": True,
        "starter_id": verified["starter_id"],
        "profile_id": verified["profile_id"],
        "country_code": verified["country_code"],
        "jurisdiction_id": verified["jurisdiction_id"],
        "tax_readiness": verified["tax_readiness"],
        "selected_integrations": verified["selected_integrations"],
        "runtime_fingerprint": verified["runtime_fingerprint"],
        "runtime_layout_version": verified["runtime_layout_version"],
        "workspace_verified": True,
        "ready_to_run_locally": True,
        "default_url": "http://127.0.0.1:8765",
        "box_workspace_immutable": True,
        "runtime_data_separate": True,
        "credentials_persisted": False,
        "connector_network_dispatch_performed": False,
        "external_actions_performed": False,
        "destination_path_returned": False,
        "actor_returned": False,
    }


def verify_trial_workspace(
    root: str | Path,
    packs_root: str | Path,
) -> dict[str, Any]:
    """Reverify the immutable Box and versioned mutable data layout of one local trial."""
    destination = _existing_root(root)
    expected_names = {BOX_DIRECTORY, RUNTIME_DIRECTORY, GUIDE_NAME, MANIFEST_NAME}
    if {item.name for item in destination.iterdir()} != expected_names:
        raise TrialWorkspaceError("trial workspace root contains missing or unexpected entries")
    manifest = _load_manifest(destination)
    if _read_private_file(destination / GUIDE_NAME, maximum=64 * 1024) != _guide():
        raise TrialWorkspaceError("trial workspace guide was modified")
    if (
        manifest["box_workspace"] != BOX_DIRECTORY
        or manifest["box_config"] != f"{BOX_DIRECTORY}/box.json"
        or manifest["runtime_data"] != RUNTIME_DIRECTORY
    ):
        raise TrialWorkspaceError("trial workspace paths are invalid")
    try:
        box = verify_unpacked_box_candidate(destination / BOX_DIRECTORY, packs_root)
    except (BoxHandoffUnpackError, OSError, ValueError) as exc:
        raise TrialWorkspaceError("trial Box workspace verification failed") from exc
    runtime = inspect_runtime_data(destination / RUNTIME_DIRECTORY)
    if runtime.get("state") != "ready" or not runtime.get("compatible"):
        raise TrialWorkspaceError("trial runtime data layout is not ready")
    config = destination / manifest["box_config"]
    snapshot = BoxRuntime(config, Path(packs_root)).snapshot()
    try:
        spec = json.loads(
            _read_private_file(
                destination / BOX_DIRECTORY / "box-spec.json", maximum=MAX_MANIFEST_BYTES,
            ).decode("utf-8")
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TrialWorkspaceError("trial Box specification is invalid") from exc
    pack_ids = {str(item.get("id") or "") for item in snapshot.get("packs", [])}
    if "industry.game_studio" in pack_ids:
        actual_profile = "game"
    elif "industry.commerce" in pack_ids and "channel.dtc_storefront" in pack_ids:
        actual_profile = "dtc"
    elif "industry.commerce" in pack_ids and "channel.marketplace_commerce" in pack_ids:
        actual_profile = "marketplace"
    else:
        raise TrialWorkspaceError("trial Box does not match a supported product profile")
    entities = snapshot.get("entities") or []
    if len(entities) != 1:
        raise TrialWorkspaceError("trial workspace requires exactly one legal entity")
    entity = entities[0]
    actual_country = str(entity.get("jurisdiction") or "")
    actual_jurisdiction = str(entity.get("tax_pack") or "")
    actual_tax_readiness = str(entity.get("tax_readiness") or "")
    actual_integrations = spec.get("integrations")
    if not isinstance(actual_integrations, list) or any(
        not isinstance(item, str) for item in actual_integrations
    ):
        raise TrialWorkspaceError("trial Box integration selections are invalid")
    if (
        box["runtime_fingerprint"] != manifest["runtime_fingerprint"]
        or snapshot["fingerprint"] != manifest["runtime_fingerprint"]
        or box["receipt_sha256"] != manifest["box_receipt_sha256"]
        or runtime["current_layout_version"] != manifest["runtime_layout_version"]
        or runtime["layout_id"] != manifest["runtime_layout_id"]
        or snapshot["data_mode"] != "demo"
        or manifest["profile_id"] != actual_profile
        or manifest["country_code"] != actual_country
        or manifest["starter_id"] != f"{actual_profile}.{actual_country.lower()}"
        or manifest["jurisdiction_id"] != actual_jurisdiction
        or manifest["tax_readiness"] != actual_tax_readiness
        or manifest["selected_integrations"] != actual_integrations
    ):
        raise TrialWorkspaceError("trial workspace binding does not match its live components")
    boundary = manifest.get("control_boundary")
    if boundary != {
        "demo_mode": True,
        "credentials_persisted": False,
        "connector_network_dispatch_performed": False,
        "financial_values_added": False,
        "tax_applicability_determined": False,
        "posting_payment_or_filing_authorized": False,
        "external_actions_performed": False,
    }:
        raise TrialWorkspaceError("trial workspace control boundary is invalid")
    return {
        "schema_version": 1,
        "valid": True,
        "starter_id": manifest["starter_id"],
        "profile_id": manifest["profile_id"],
        "country_code": manifest["country_code"],
        "jurisdiction_id": manifest["jurisdiction_id"],
        "tax_readiness": manifest["tax_readiness"],
        "selected_integrations": manifest["selected_integrations"],
        "runtime_fingerprint": manifest["runtime_fingerprint"],
        "runtime_layout_version": manifest["runtime_layout_version"],
        "default_url": "http://127.0.0.1:8765",
        "box_workspace_immutable": True,
        "runtime_data_separate": True,
        "credentials_returned": False,
        "financial_values_returned": False,
        "paths_returned": False,
        "actor_returned": False,
        "network_access_performed": False,
        "external_actions_performed": False,
    }


def _load_trial_setup_checklist(destination: Path) -> dict[str, Any]:
    path = destination / BOX_DIRECTORY / "setup-checklist.json"
    try:
        checklist = json.loads(
            _read_private_file(path, maximum=MAX_SETUP_CHECKLIST_BYTES).decode("utf-8")
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TrialWorkspaceError("trial setup checklist is invalid JSON") from exc
    if not isinstance(checklist, dict) or checklist.get("schema_version") != 1:
        raise TrialWorkspaceError("trial setup checklist contract is unsupported")
    groups = checklist.get("groups")
    counts = checklist.get("counts")
    if not isinstance(groups, list) or not isinstance(counts, dict):
        raise TrialWorkspaceError("trial setup checklist structure is invalid")
    recomputed = {"blocking": 0, "required": 0, "advisory": 0, "total": 0}
    seen_tasks: set[str] = set()
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("tasks"), list):
            raise TrialWorkspaceError("trial setup checklist group is invalid")
        if not isinstance(group.get("phase"), int) or not str(
            group.get("display_name") or ""
        ).strip():
            raise TrialWorkspaceError("trial setup checklist phase is invalid")
        for task in group["tasks"]:
            if not isinstance(task, dict):
                raise TrialWorkspaceError("trial setup checklist task is invalid")
            task_id = str(task.get("task_id") or "")
            level = str(task.get("level") or "")
            if not task_id or task_id in seen_tasks or level not in {
                "blocking", "required", "advisory",
            }:
                raise TrialWorkspaceError("trial setup checklist task contract is invalid")
            if task.get("secret_values_included") is not False:
                raise TrialWorkspaceError("trial setup checklist must not contain secret values")
            seen_tasks.add(task_id)
            recomputed[level] += 1
            recomputed["total"] += 1
    if counts != recomputed:
        raise TrialWorkspaceError("trial setup checklist counts are inconsistent")
    return checklist


def _starter_init_command(verified: dict[str, Any]) -> str:
    arguments = [
        "opc-finance-box", "starter-init", "/absolute/new/editable-box",
        "--profile", verified["profile_id"],
        "--country", verified["country_code"],
    ]
    for integration in verified["selected_integrations"]:
        arguments.extend(("--integration", integration))
    arguments.extend(("--data-mode", "live", "--actor", "BOX_OWNER"))
    return " ".join(arguments)


def build_trial_onboarding_plan(
    root: str | Path,
    packs_root: str | Path,
) -> dict[str, Any]:
    """Compress a verified trial's release controls into a founder-facing next-step plan."""
    destination = _existing_root(root)
    verified = verify_trial_workspace(destination, packs_root)
    checklist = _load_trial_setup_checklist(destination)
    phase_summaries: list[dict[str, Any]] = []
    priority_tasks: list[dict[str, Any]] = []
    for group in checklist["groups"]:
        levels = {"blocking": 0, "required": 0, "advisory": 0}
        roles: set[str] = set()
        credential_env_names: set[str] = set()
        for task in group["tasks"]:
            level = str(task["level"])
            levels[level] += 1
            if task.get("owner_role"):
                roles.add(str(task["owner_role"]))
            credential_env_names.update(
                str(item) for item in (task.get("credential_env") or []) if item
            )
            if level == "blocking" or str(task.get("severity") or "") in {
                "required_before_network_exposure",
            }:
                priority_tasks.append({
                    "task_id": task["task_id"],
                    "phase": group["phase"],
                    "level": level,
                    "owner_role": task.get("owner_role"),
                    "entity_id": task.get("entity_id"),
                    "summary": task.get("summary"),
                    "completion_evidence": task.get("completion_evidence"),
                    "credential_env_names": sorted(
                        str(item) for item in (task.get("credential_env") or []) if item
                    ),
                    "status": "not_started",
                })
        phase_summaries.append({
            "phase": group["phase"],
            "display_name": group["display_name"],
            "task_count": len(group["tasks"]),
            "blocking_count": levels["blocking"],
            "required_count": levels["required"],
            "advisory_count": levels["advisory"],
            "owner_roles": sorted(roles),
            "credential_env_names": sorted(credential_env_names),
            "status": "not_started",
        })

    journey = [
        {
            "stage_order": 1,
            "stage_id": "explore_local_demo",
            "display_name": "运行本地演示工作台",
            "status": "ready",
            "why": "不可变工作台与可变运行数据已经分别复验，可在本机演示。",
            "command_templates": [
                "opc-finance-box trial-verify /absolute/TRIAL_ROOT",
                "opc-finance-box trial-run /absolute/TRIAL_ROOT",
            ],
        },
        {
            "stage_order": 2,
            "stage_id": "fork_and_configure_box",
            "display_name": "建立自己的可编辑工作台",
            "status": "available",
            "why": "沿用已选行业、纳税地区和集成，但新建正式候选，不改写试用快照。",
            "command_templates": [_starter_init_command(verified)],
        },
        {
            "stage_order": 3,
            "stage_id": "review_setup_contract",
            "display_name": "确认主体、税务、数据源和控制人",
            "status": "blocked",
            "why": "上线清单尚无任何权威完成证据；演示可运行不等于真实业务就绪。",
            "command_templates": [
                "opc-finance-box validate /absolute/new/editable-box/box.json",
                "opc-finance-box compile /absolute/new/editable-box/box.json --output /absolute/new/editable-box/rebuilt",
                "opc-finance-box doctor /absolute/new/editable-box/box.json --as-of YYYY-MM-DD",
            ],
        },
        {
            "stage_order": 4,
            "stage_id": "initialize_private_activation",
            "display_name": "初始化首客私有激活工作区",
            "status": "locked",
            "why": "先完成可编辑工作台与初始配置复核，再把真实证据放入独立私有目录。",
            "command_templates": [
                "opc-finance-box activation-init /absolute/new/editable-box/box.json /absolute/new/private-activation --period YYYY-MM --facts-as-of YYYY-MM-DD --prepared-by ACTIVATION_PREPARER",
                "opc-finance-box activation-workspace-verify /absolute/new/editable-box/box.json /absolute/new/private-activation",
            ],
        },
        {
            "stage_order": 5,
            "stage_id": "bounded_shadow_and_promotion",
            "display_name": "真实只读并行验证与连续月结",
            "status": "locked",
            "why": "数据连接器、资料交接、逐主体观察和连续期间证据必须依次通过权威验证。",
            "command_templates": [
                "opc-finance-box activation-workspace-status /absolute/new/editable-box/box.json /absolute/new/private-activation --as-of YYYY-MM-DD",
            ],
        },
    ]
    return {
        "schema_version": 1,
        "artifact_type": "opc_finance_box_trial_onboarding_plan",
        "valid": True,
        "current_stage_id": "explore_local_demo",
        "next_action_id": "run_local_demo",
        "starter": {
            "starter_id": verified["starter_id"],
            "profile_id": verified["profile_id"],
            "country_code": verified["country_code"],
            "jurisdiction_id": verified["jurisdiction_id"],
            "tax_readiness": verified["tax_readiness"],
            "selected_integrations": verified["selected_integrations"],
        },
        "summary": {
            "demo_ready": True,
            "production_ready": False,
            "setup_task_count": checklist["counts"]["total"],
            "blocking_setup_task_count": checklist["counts"]["blocking"],
            "required_setup_task_count": checklist["counts"]["required"],
            "advisory_setup_task_count": checklist["counts"]["advisory"],
            "setup_phase_count": len(phase_summaries),
            "journey_stage_count": len(journey),
        },
        "journey": journey,
        "setup_phases": phase_summaries,
        "priority_setup_tasks": priority_tasks,
        "control_boundary": {
            "trial_workspace_verified": True,
            "setup_evidence_recorded": False,
            "tax_applicability_determined": False,
            "connector_credentials_inspected": False,
            "commands_are_templates_only": True,
            "commands_executed": False,
            "paths_returned": False,
            "actors_returned": False,
            "financial_values_returned": False,
            "production_readiness_inferred": False,
            "posting_payment_or_filing_authorized": False,
            "external_actions_performed": False,
        },
    }


def _is_loopback(host: str) -> bool:
    normalized = str(host or "").strip().lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def run_trial_workbench(
    root: str | Path,
    packs_root: str | Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    auth_file: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Verify and run one trial without modifying its immutable Box workspace."""
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise TrialWorkspaceError("trial workbench port must be an integer from 1 to 65535")
    normalized_host = str(host or "").strip()
    if not normalized_host or len(normalized_host) > 253:
        raise TrialWorkspaceError("trial workbench host is invalid")
    destination = _existing_root(root)
    verified = verify_trial_workspace(destination, packs_root)
    environment = dict(os.environ if environ is None else environ)
    selected_auth_file: Path | None = None
    if auth_file is not None:
        selected_auth_file = Path(auth_file).expanduser()
        if not selected_auth_file.is_absolute():
            raise TrialWorkspaceError("trial auth policy must use an absolute path")
        try:
            selected_auth_file = selected_auth_file.resolve(strict=True)
            load_api_auth_policy(policy_path=selected_auth_file, legacy_token="")
        except (OSError, ValueError) as exc:
            raise TrialWorkspaceError("trial auth policy is invalid") from exc
        if environment.get("OPC_FINANCE_API_TOKEN"):
            raise TrialWorkspaceError("configure either a trial auth policy or API token, not both")
        environment["OPC_FINANCE_API_AUTH_FILE"] = str(selected_auth_file)
    has_auth = bool(
        environment.get("OPC_FINANCE_API_TOKEN")
        or environment.get("OPC_FINANCE_API_AUTH_FILE")
    )
    if not _is_loopback(normalized_host) and not has_auth:
        raise TrialWorkspaceError("non-loopback trial workbench binding requires authentication")
    packs = Path(packs_root).expanduser().resolve()
    module_root = Path(__file__).resolve().parent.parent
    existing_pythonpath = environment.get("PYTHONPATH")
    environment.update({
        "PYTHONPATH": (
            str(module_root)
            if not existing_pythonpath
            else str(module_root) + os.pathsep + existing_pythonpath
        ),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
        "OPC_FINANCE_BOX_CONFIG": str(destination / BOX_DIRECTORY / "box.json"),
        "OPC_FINANCE_PACKS_ROOT": str(packs),
        "OPC_FINANCE_DATA_DIR": str(destination / RUNTIME_DIRECTORY),
        TRIAL_WORKSPACE_ROOT_ENV: str(destination),
        "OPC_FINANCE_HOST": normalized_host,
        "OPC_FINANCE_PORT": str(port),
    })
    process = subprocess.Popen(
        [sys.executable, "-m", "src.server"],
        cwd=destination / RUNTIME_DIRECTORY,
        env=environment,
    )
    interrupted = False
    try:
        exit_code = process.wait()
    except KeyboardInterrupt:
        interrupted = True
        try:
            exit_code = process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                exit_code = process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                exit_code = process.wait(timeout=10)
    if exit_code != 0:
        raise TrialWorkspaceError(
            f"trial workbench exited with non-zero status {exit_code}"
        )
    post_run = verify_trial_workspace(destination, packs)
    if post_run["runtime_fingerprint"] != verified["runtime_fingerprint"]:
        raise TrialWorkspaceError("trial Box changed while the workbench was running")
    return {
        "schema_version": 1,
        "stopped": True,
        "exit_code": 0,
        "stopped_by_operator": interrupted,
        "host": normalized_host,
        "port": port,
        "runtime_fingerprint": verified["runtime_fingerprint"],
        "box_workspace_modified": False,
        "post_stop_workspace_verified": True,
        "browser_opened_automatically": False,
        "credentials_returned": False,
        "external_actions_performed": False,
    }
