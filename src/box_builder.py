from __future__ import annotations

import json
import tempfile
import hashlib
import io
import os
import zipfile
from copy import deepcopy
from pathlib import Path
from typing import Any

from .box_api import build_box_context
from .box_compiler import (
    build_pipeline_runtime_catalog, compile_box, write_compiled_box,
)
from .box_config import (
    SINGLE_CREDENTIAL_CONNECTOR_PACKS, PackCatalog, load_pack_catalog,
)
from .box_runtime import BoxRuntime
from .box_scaffold import create_box_config, list_box_options
from .deployment_assets import REQUIRED_ASSETS, verify_deployment_assets
from .resource_paths import find_resource_root


PRODUCT_PROFILES = (
    {
        "id": "game",
        "display_name": "游戏 OPC",
        "description": "渠道结算、合同分成、应收和经营复核样板。",
        "business_type": "game",
        "channels": ["app_store", "google_play", "domestic_game"],
        "allowed_integrations": ["wise", "xero"],
    },
    {
        "id": "dtc",
        "display_name": "独立站 OPC",
        "description": "订单、退款、履约、支付与目的地税务证据样板。",
        "business_type": "commerce",
        "channels": ["dtc"],
        "allowed_integrations": [
            "shopify", "stripe", "paypal", "woocommerce", "wise", "xero", "airwallex", "shipbob", "shopify_stripe",
            "shopify_stripe_wise", "shopify_stripe_xero",
            "shopify_stripe_wise_airwallex",
        ],
    },
    {
        "id": "marketplace",
        "display_name": "平台电商 OPC",
        "description": "平台费用、应收、订单与平台库存关账样板。",
        "business_type": "commerce",
        "channels": ["marketplace"],
        "allowed_integrations": ["amazon_seller", "paypal", "wise", "xero", "shipbob"],
    },
)


COUNTRY_STARTERS = {
    "AE": {"functional_currency": "AED", "accounting_basis": "IFRS"},
    "CN": {"functional_currency": "CNY", "accounting_basis": "PRC_GAAP"},
    "SG": {"functional_currency": "SGD", "accounting_basis": "SFRS"},
    "US": {"functional_currency": "USD", "accounting_basis": "US_GAAP"},
    "HK": {"functional_currency": "HKD", "accounting_basis": "HKFRS"},
    "GB": {"functional_currency": "GBP", "accounting_basis": "UK_GAAP"},
    "AU": {"functional_currency": "AUD", "accounting_basis": "AASB"},
    "CA": {"functional_currency": "CAD", "accounting_basis": "ASPE"},
    "NZ": {
        "functional_currency": "NZD",
        "accounting_basis": "NZ_GAAP",
        "fiscal_year_end": "03-31",
    },
    "IE": {"functional_currency": "EUR", "accounting_basis": "Irish_GAAP"},
    "NL": {"functional_currency": "EUR", "accounting_basis": "Dutch_GAAP"},
    "DE": {"functional_currency": "EUR", "accounting_basis": "German_GAAP"},
    "FR": {"functional_currency": "EUR", "accounting_basis": "French_GAAP"},
    "JP": {"functional_currency": "JPY", "accounting_basis": "JGAAP"},
    "KR": {"functional_currency": "KRW", "accounting_basis": "K_GAAP"},
}


CHANNEL_PACK_IDS = {
    "app_store": "channel.app_store",
    "google_play": "channel.google_play",
    "domestic_game": "channel.domestic_game_platforms",
    "dtc": "channel.dtc_storefront",
    "marketplace": "channel.marketplace_commerce",
}


SETUP_PHASES = {
    "runtime_security": (1, "保护运行入口"),
    "tax_readiness": (2, "确认主体与税务边界"),
    "tax_registration": (2, "确认主体与税务边界"),
    "connector_runtime": (3, "连接并影子核对数据源"),
    "control_owner": (4, "配置职责分离与有权人"),
    "pack_readiness": (5, "接受预览边界与升级计划"),
}

SPEC_FIELDS = {
    "name", "business_type", "business_models", "channels", "integrations",
    "connectors", "features", "connector_bindings", "data_mode",
    "reporting_currency", "entities",
}
ENTITY_SPEC_FIELDS = {
    "id", "name", "tax_country", "jurisdiction", "tax_pack",
    "functional_currency", "accounting_basis", "fiscal_year_end", "tax_registrations",
}
CONNECTOR_BINDING_SPEC_FIELDS = {"connector_pack", "entity_ids"}
SECRET_KEY_MARKERS = (
    "secret", "token", "password", "apikey", "privatekey", "credential", "authorization",
)


def _validate_builder_spec_boundary(spec: Any) -> dict[str, Any]:
    if not isinstance(spec, dict):
        raise ValueError("Box Builder request must be a JSON object")
    unknown = sorted(set(spec) - SPEC_FIELDS)
    if unknown:
        raise ValueError("Box Builder spec contains unsupported fields: " + ", ".join(unknown))
    entities = spec.get("entities")
    if isinstance(entities, list):
        for index, entity in enumerate(entities):
            if not isinstance(entity, dict):
                continue
            unknown_entity = sorted(set(entity) - ENTITY_SPEC_FIELDS)
            if unknown_entity:
                raise ValueError(
                    f"entities[{index}] contains unsupported fields: " + ", ".join(unknown_entity)
                )
    connector_bindings = spec.get("connector_bindings")
    if isinstance(connector_bindings, list):
        for index, binding in enumerate(connector_bindings):
            if not isinstance(binding, dict):
                continue
            unknown_binding = sorted(set(binding) - CONNECTOR_BINDING_SPEC_FIELDS)
            if unknown_binding:
                raise ValueError(
                    f"connector_bindings[{index}] contains unsupported fields: "
                    + ", ".join(unknown_binding)
                )

    forbidden: list[str] = []
    def inspect(value: Any, path: str) -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                child = f"{path}.{key}" if path else str(key)
                normalized = "".join(character for character in str(key).lower() if character.isalnum())
                if any(marker in normalized for marker in SECRET_KEY_MARKERS):
                    forbidden.append(child)
                inspect(nested, child)
        elif isinstance(value, list):
            for index, nested in enumerate(value):
                inspect(nested, f"{path}[{index}]")
    inspect(spec, "spec")
    if forbidden:
        raise ValueError("Box Builder spec must not contain secret fields: " + ", ".join(forbidden))
    return spec


def _build_setup_checklist(tasks: list[dict[str, Any]]) -> dict[str, Any]:
    """Turn compiler tasks into an owner/evidence-oriented onboarding projection."""
    evidence_by_category = {
        "runtime_security": "认证策略引用、TLS/网络边界图和访问复核记录",
        "tax_readiness": "Tax Pack 适用性与成熟度的当地专业复核记录",
        "tax_registration": "法律主体登记凭证、税种登记状态和适用性结论",
        "connector_runtime": "环境变量名称已配置、provider contract test 与 shadow reconciliation 引用",
        "control_owner": "主审/替补 principal、职责分离检查和授权依据",
        "pack_readiness": "预览使用边界接受记录、升级负责人和目标版本",
    }
    groups: dict[tuple[int, str], list[dict[str, Any]]] = {}
    counts = {"blocking": 0, "required": 0, "advisory": 0}
    for raw in tasks:
        severity = str(raw.get("severity") or "warning")
        if severity.startswith("blocking"):
            level = "blocking"
        elif severity.startswith("required"):
            level = "required"
        else:
            level = "advisory"
        counts[level] += 1
        category = str(raw.get("category") or "other")
        phase_order, phase_name = SETUP_PHASES.get(category, (9, "其他上线任务"))
        item = {
            "task_id": raw.get("task_id"),
            "category": category,
            "level": level,
            "severity": severity,
            "owner_role": raw.get("owner_role"),
            "entity_id": raw.get("entity_id"),
            "summary": raw.get("summary"),
            "credential_env": list(raw.get("credential_env") or []),
            "completion_evidence": evidence_by_category.get(
                category, "完成记录、证据引用和负责人的明确确认",
            ),
            "status": "not_started",
            "secret_values_included": False,
        }
        groups.setdefault((phase_order, phase_name), []).append(item)
    grouped = [{
        "phase": phase_order,
        "display_name": phase_name,
        "tasks": sorted(items, key=lambda item: (item["level"], str(item["task_id"]))),
    } for (phase_order, phase_name), items in sorted(groups.items())]
    return {
        "schema_version": 1,
        "status": "not_started",
        "counts": {**counts, "total": len(tasks)},
        "owner_roles": sorted({
            str(task.get("owner_role")) for task in tasks if task.get("owner_role")
        }),
        "groups": grouped,
        "completion_is_release_approval": False,
        "control_note": (
            "Checklist completion requires evidence references; it never stores credential "
            "values and does not by itself authorize posting, payment or filing."
        ),
    }


def _installed_product_profiles(catalog: PackCatalog) -> list[dict[str, Any]]:
    installed = {pack.pack_id for pack in catalog.all()}
    installed_presets = {
        item["id"] for item in list_box_options(catalog)["integration_presets"]
    }
    profiles = []
    for raw in PRODUCT_PROFILES:
        profile = deepcopy(raw)
        industry = (
            "industry.game_studio" if profile["business_type"] == "game"
            else "industry.commerce"
        )
        required = {
            industry, *(CHANNEL_PACK_IDS[channel] for channel in profile["channels"]),
        }
        if required <= installed:
            profile["allowed_integrations"] = [
                preset_id for preset_id in profile["allowed_integrations"]
                if preset_id in installed_presets
            ]
            profiles.append(profile)
    return profiles


def _installed_builder_jurisdictions(catalog: PackCatalog) -> list[dict[str, Any]]:
    scaffold_options = list_box_options(catalog)
    jurisdictions = []
    for item in scaffold_options["jurisdictions"]:
        country_code = str(item.get("country_code") or "")
        starter = COUNTRY_STARTERS.get(country_code, {})
        jurisdictions.append({
            **item,
            "starter": {
                "functional_currency": starter.get("functional_currency"),
                "accounting_basis": starter.get("accounting_basis"),
                "fiscal_year_end": starter.get("fiscal_year_end", "12-31"),
                "tax_registrations": [],
            },
            "requires_local_confirmation": True,
        })
    return sorted(jurisdictions, key=lambda item: (str(item["country_code"]), item["id"]))


def build_box_starter_catalog(catalog: PackCatalog) -> dict[str, Any]:
    """Build contract-checked starter specs for every installed product/country pair."""
    profiles = _installed_product_profiles(catalog)
    jurisdictions = _installed_builder_jurisdictions(catalog)
    entries: list[dict[str, Any]] = []
    unavailable: list[dict[str, Any]] = []
    for profile in profiles:
        for jurisdiction in jurisdictions:
            country_code = str(jurisdiction["country_code"])
            starter = jurisdiction["starter"]
            missing_fields = [
                field for field in ("functional_currency", "accounting_basis", "fiscal_year_end")
                if not str(starter.get(field) or "").strip()
            ]
            combination_id = f"{profile['id']}.{country_code.lower()}"
            if missing_fields:
                unavailable.append({
                    "id": combination_id,
                    "profile_id": profile["id"],
                    "jurisdiction_id": jurisdiction["id"],
                    "country_code": country_code,
                    "reason": "starter_defaults_incomplete",
                    "missing_fields": missing_fields,
                })
                continue
            entity_id = f"{country_code.lower()}_{profile['id']}_company"
            spec = {
                "name": f"{country_code} {profile['display_name']} Starter",
                "business_type": profile["business_type"],
                "channels": list(profile["channels"]),
                "integrations": [],
                "data_mode": "demo",
                "reporting_currency": starter["functional_currency"],
                "entities": [{
                    "id": entity_id,
                    "name": f"{country_code} {profile['display_name']} 经营主体（待确认）",
                    "tax_country": country_code,
                    "tax_pack": jurisdiction["id"],
                    "functional_currency": starter["functional_currency"],
                    "accounting_basis": starter["accounting_basis"],
                    "fiscal_year_end": starter["fiscal_year_end"],
                    "tax_registrations": [],
                }],
            }
            try:
                config = create_box_config(spec, catalog)
            except ValueError as error:
                unavailable.append({
                    "id": combination_id,
                    "profile_id": profile["id"],
                    "jurisdiction_id": jurisdiction["id"],
                    "country_code": country_code,
                    "reason": "pack_contract_rejected",
                    "error": str(error),
                })
                continue
            spec_bytes = json.dumps(
                spec, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            ).encode("utf-8")
            entries.append({
                "id": combination_id,
                "display_name": f"{profile['display_name']} · {country_code}",
                "profile_id": profile["id"],
                "jurisdiction_id": jurisdiction["id"],
                "country_code": country_code,
                "tax_readiness": jurisdiction.get("tax_readiness"),
                "allowed_integrations": list(profile["allowed_integrations"]),
                "starter_spec": spec,
                "starter_spec_sha256": hashlib.sha256(spec_bytes).hexdigest(),
                "resolved_pack_ids": [
                    config["core"], *config["business_models"], *config["channels"],
                    *config["connectors"], *config["features"],
                    *[entity["tax_pack"] for entity in config["entities"]],
                ],
                "contract_checked": True,
                "multi_entity_supported": "feature.multi_entity" in {
                    pack.pack_id for pack in catalog.all()
                },
                "filing_ready": False,
                "requires_local_confirmation": True,
            })
    eligible_count = len(profiles) * len(jurisdictions)
    return {
        "schema_version": 1,
        "profile_count": len(profiles),
        "jurisdiction_count": len(jurisdictions),
        "eligible_combination_count": eligible_count,
        "ready_combination_count": len(entries),
        "complete": len(entries) == eligible_count,
        "entries": entries,
        "unavailable_combinations": unavailable,
        "control_boundary": {
            "combinations_derived_from_installed_packs": True,
            "every_entry_contract_checked": all(item["contract_checked"] for item in entries),
            "starter_values_require_local_confirmation": True,
            "tax_registrations_default_to_empty": True,
            "external_actions_performed": False,
            "filing_readiness_inferred": False,
        },
    }


def list_box_builder_options(catalog: PackCatalog) -> dict[str, Any]:
    """Return installed, product-oriented choices without inventing tax support."""
    scaffold_options = list_box_options(catalog)
    profiles = _installed_product_profiles(catalog)
    jurisdictions = _installed_builder_jurisdictions(catalog)

    return {
        "schema_version": 3,
        "profiles": profiles,
        "jurisdictions": jurisdictions,
        "integration_presets": scaffold_options["integration_presets"],
        "connector_binding_policy": {
            "schema_version": 1,
            "default_connector_pack": "connector.file_import",
            "complete_bindings_required_when_explicit": True,
            "single_credential_connector_packs": sorted(
                SINGLE_CREDENTIAL_CONNECTOR_PACKS
            ),
            "single_credential_max_entity_count": 1,
            "wrong_entity_dispatch_rejected_before_provider_call": True,
        },
        "handoff_download_policy": {
            "schema_version": 2,
            "digest_algorithm": "SHA-256",
            "digest_header": "X-OPC-Handoff-SHA256",
            "runtime_fingerprint_header": "X-OPC-Runtime-Fingerprint",
            "manifest_schema_header": "X-OPC-Manifest-Schema",
            "manifest_file_count_header": "X-OPC-Manifest-File-Count",
            "client_digest_required_before_download": True,
            "missing_or_mismatched_metadata_blocks_download": True,
            "receipt_schema_version": 1,
            "receipt_filename_suffix": ".browser-receipt.json",
            "recipient_verifier_command": "handoff-receipt-verify",
            "recipient_private_file_mode": "0600",
            "receipt_is_digital_signature": False,
        },
        "starter_catalog": build_box_starter_catalog(catalog),
        "control_boundary": {
            "only_installed_packs_listed": True,
            "starter_catalog_is_pack_driven": True,
            "tax_country_never_implies_filing_readiness": True,
            "starter_currency_and_accounting_basis_require_confirmation": True,
            "tax_registrations_default_to_empty": True,
            "browser_builder_can_emit_explicit_connector_bindings": True,
            "browser_bundle_bytes_must_be_verified_before_download": True,
            "portable_browser_receipt_must_be_formally_reverified": True,
        },
    }


def preview_box_candidate(spec: dict[str, Any], packs_root: str | Path) -> dict[str, Any]:
    """Build a reproducible candidate without replacing the active server runtime."""
    spec = _validate_builder_spec_boundary(spec)
    if len(json.dumps(spec, ensure_ascii=False).encode("utf-8")) > 256 * 1024:
        raise ValueError("Box Builder request must not exceed 256 KiB")
    entities = spec.get("entities")
    if isinstance(entities, list) and len(entities) > 20:
        raise ValueError("Box Builder supports at most 20 legal entities per candidate")
    root = Path(packs_root)
    catalog = load_pack_catalog(root)
    config = create_box_config(spec, catalog)
    with tempfile.TemporaryDirectory(prefix="opc-box-preview-") as temp_dir:
        config_path = Path(temp_dir) / "box.json"
        config_path.write_text(
            json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
        runtime = BoxRuntime(config_path, root)
        context = build_box_context(runtime)
        compiled = compile_box(runtime)
        pipeline_catalog = build_pipeline_runtime_catalog(runtime)

    return {
        "schema_version": 1,
        "valid": True,
        "spec": deepcopy(spec),
        "config": config,
        "candidate": {
            "product": context["product"],
            "entities": context["entities"],
            "packs": context["packs"],
            "tax_readiness": context["tax_readiness"],
            "warnings": context["warnings"],
            "pipelines": pipeline_catalog["pipelines"],
            "request_template_count": len(
                pipeline_catalog["request_templates"]["templates"]
            ),
            "setup_tasks": compiled["setup_tasks"],
            "setup_checklist": _build_setup_checklist(compiled["setup_tasks"]),
            "release_gates": compiled["release_gates"],
            "runtime_fingerprint": pipeline_catalog["runtime_fingerprint"],
        },
        "control_boundary": {
            "active_runtime_changed": False,
            "persistent_files_written": False,
            "connector_dispatch_performed": False,
            "external_actions_performed": False,
            "secrets_included": False,
            "candidate_requires_explicit_save_and_restart": True,
        },
    }


def build_box_candidate_bundle(
    spec: dict[str, Any], packs_root: str | Path,
) -> tuple[bytes, str, dict[str, Any]]:
    """Return a deterministic, secret-free handoff bundle for one valid candidate."""
    preview = preview_box_candidate(spec, packs_root)
    root = Path(packs_root)
    with tempfile.TemporaryDirectory(prefix="opc-box-bundle-") as temp_dir:
        workspace = Path(temp_dir)
        config_path = workspace / "box.json"
        config_path.write_text(
            json.dumps(preview["config"], ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        compiled = compile_box(BoxRuntime(config_path, root))
        compiled_dir = workspace / "compiled"
        write_compiled_box(compiled, compiled_dir)
        files: dict[str, bytes] = {
            "box-spec.json": (json.dumps(preview["spec"], ensure_ascii=False, indent=2) + "\n").encode(),
            "box.json": config_path.read_bytes(),
            "setup-checklist.json": (
                json.dumps(preview["candidate"]["setup_checklist"], ensure_ascii=False, indent=2) + "\n"
            ).encode(),
        }
        for path in sorted(compiled_dir.rglob("*")):
            if path.is_file():
                files[f"compiled/{path.relative_to(compiled_dir).as_posix()}"] = path.read_bytes()
        deployment_root = find_resource_root() / "deployment"
        verify_deployment_assets(deployment_root)
        for name in REQUIRED_ASSETS:
            files[f"deployment/{name}"] = (deployment_root / name).read_bytes()
        files["HANDOFF.md"] = (
            "# OPC Finance Box Handoff\n\n"
            "此包是可编辑候选，不是发布、付款、过账或税务申报授权。\n\n"
            "```bash\n"
            "opc-finance-box handoff-verify /path/to/customer-handoff.zip\n"
            "opc-finance-box handoff-unpack /path/to/customer-handoff.zip \\\n"
            "  /absolute/new/handoff-workspace --actor HANDOFF_RECIPIENT\n"
            "opc-finance-box handoff-unpack-verify /absolute/new/handoff-workspace\n"
            "opc-finance-box validate box.json\n"
            "opc-finance-box compile box.json --output rebuilt\n"
            "opc-finance-box doctor box.json\n"
            "opc-finance-box deployment-assets-verify deployment\n"
            "opc-finance-box deployment-smoke box.json\n"
            "opc-finance-box upgrade-check box.json compiled/box.lock.json\n"
            "```\n\n"
            "真实首客接入请继续阅读 `ACTIVATION.md`；它会建立新的私有工作区和"
            "可恢复 Runbook，但不会自动执行命令或制造批准。\n\n"
            "`deployment/` 是经过控制检查的可编辑模板。Dockerfile 需要 OPC Finance Box "
            "完整源码作为 build context；请把本 Bundle 放入已 clone 的 starter repo，或先安装"
            "通过 distribution-verify 的 wheel 后使用 systemd 模板。\n\n"
            "先完成 `setup-checklist.json`、fixture/shadow close、职责分离和当地税务复核。\n"
        ).encode("utf-8")
        files["ACTIVATION.md"] = (
            "# First-customer private activation\n\n"
            "以下命令只创建私有目录、模板和非权威进度台账，不会创建通过结论。\n\n"
            "```bash\n"
            "opc-finance-box activation-init box.json /absolute/new/private-root \\\n"
            "  --period YYYY-MM --facts-as-of YYYY-MM-DD --prepared-by PREPARER\n"
            "opc-finance-box activation-workspace-verify box.json /absolute/new/private-root\n"
            "opc-finance-box activation-runbook-status box.json /absolute/new/private-root\n"
            "opc-finance-box activation-workspace-status box.json /absolute/new/private-root \\\n"
            "  --as-of YYYY-MM-DD\n"
            "```\n\n"
            "按 `/absolute/new/private-root/commands.json` 的顺序处理步骤。命令中的"
            " `REPLACE_WITH_...` 必须由操作者在执行副本中替换；不要改写原始命令合同。"
            "所有 review 决定默认失败关闭，只有真实证据支持时才能改变。\n\n"
            "可用 `activation-runbook-record` 记录 `reported-complete`、"
            "`reported-failed`、`blocked` 或 `deferred`，并用"
            " `activation-runbook-verify` 校验追加式 hash chain。Runbook 只帮助恢复进度；"
            "它永不替代税务、Connector、Shadow Close、连续期间或 stable promotion 的"
            "权威 verifier，也不授权入账、付款、关账或申报。\n"
        ).encode("utf-8")
        file_records = [{
            "path": name,
            "size_bytes": len(body),
            "sha256": hashlib.sha256(body).hexdigest(),
        } for name, body in sorted(files.items())]
        manifest = {
            "schema_version": 2,
            "product": "opc-finance-box",
            "runtime_fingerprint": preview["candidate"]["runtime_fingerprint"],
            "file_count": len(file_records),
            "files": file_records,
            "secret_values_included": False,
            "external_actions_performed": False,
            "active_runtime_changed": False,
            "deployment_assets_included": True,
            "activation_guide_included": True,
        }
        files["bundle-manifest.json"] = (
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8")
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for name, body in sorted(files.items()):
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                archive.writestr(info, body)
    fingerprint = preview["candidate"]["runtime_fingerprint"]
    return output.getvalue(), f"opc-finance-box-{fingerprint[:12]}.zip", manifest


def write_box_candidate_bundle(
    spec: dict[str, Any],
    packs_root: str | Path,
    output: str | Path,
) -> dict[str, Any]:
    """Write one deterministic handoff ZIP without overwriting an existing file."""
    body, suggested_filename, manifest = build_box_candidate_bundle(spec, packs_root)
    requested = Path(output).expanduser()
    destination = requested.parent.resolve() / requested.name
    if destination.suffix.lower() != ".zip":
        raise ValueError("Box handoff output must use a .zip suffix")
    parent = destination.parent
    if parent.is_symlink() or not parent.is_dir():
        raise ValueError("Box handoff output parent must be an existing real directory")
    try:
        descriptor = os.open(
            destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600,
        )
    except FileExistsError as exc:
        raise ValueError("Box handoff output already exists; refusing to overwrite") from exc
    try:
        if os.name != "nt":
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    return {
        "written": True,
        "output": str(destination),
        "suggested_filename": suggested_filename,
        "size_bytes": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
        "runtime_fingerprint": manifest["runtime_fingerprint"],
        "file_count": manifest["file_count"],
        "activation_guide_included": True,
        "secret_values_included": False,
        "external_actions_performed": False,
        "active_runtime_changed": False,
    }
