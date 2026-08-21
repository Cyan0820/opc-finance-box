from __future__ import annotations

import os
from typing import Any, Mapping

from .box_compiler import build_pipeline_runtime_catalog
from .box_runtime import BoxRuntime
from .default_connectors import build_box_connector_registry
from .connector_entity_credentials import (
    AMAZON_SELLER_BINDINGS_ENV,
    ConnectorEntityCredentialError,
    PAYPAL_BINDINGS_ENV,
    SHIPBOB_BINDINGS_ENV,
    WOOCOMMERCE_BINDINGS_ENV,
    access_credential_group,
)


PRIVATE_SHADOW_REQUEST_COMMANDS = {
    "connector.shopify": (
        "opc-finance-box shopify-monthly-shadow-request-init /absolute/BOX_CONFIG "
        "--entity ENTITY_ID --period YYYY-MM --output /absolute/private/shopify-request.json"
    ),
    "connector.stripe": (
        "opc-finance-box stripe-shadow-request-init /absolute/BOX_CONFIG "
        "--entity ENTITY_ID --period YYYY-MM --output /absolute/private/stripe-request.json"
    ),
    "connector.wise": (
        "opc-finance-box wise-shadow-request-init /absolute/BOX_CONFIG "
        "--entity ENTITY_ID --period YYYY-MM --output /absolute/private/wise-request.json"
    ),
    "connector.xero": (
        "opc-finance-box xero-shadow-request-init /absolute/BOX_CONFIG "
        "--entity ENTITY_ID --period YYYY-MM --output /absolute/private/xero-request.json"
    ),
    "connector.paypal": (
        "opc-finance-box paypal-shadow-request-init /absolute/BOX_CONFIG "
        "--entity ENTITY_ID --period YYYY-MM --output /absolute/private/paypal-request.json"
    ),
    "connector.woocommerce": (
        "opc-finance-box woocommerce-shadow-request-init /absolute/BOX_CONFIG "
        "--entity ENTITY_ID --period YYYY-MM --output /absolute/private/woocommerce-request.json"
    ),
    "connector.shipbob": (
        "opc-finance-box shipbob-shadow-request-init /absolute/BOX_CONFIG "
        "--entity ENTITY_ID --period YYYY-MM --output /absolute/private/shipbob-request.json"
    ),
    "connector.amazon_seller": (
        "opc-finance-box amazon-seller-shadow-request-init /absolute/BOX_CONFIG "
        "--entity ENTITY_ID --period YYYY-MM --marketplace-id MARKETPLACE_ID "
        "--output /absolute/private/amazon-seller-request.json"
    ),
}

PRIVATE_ACCESS_REQUEST_COMMANDS = {
    "connector.shopify": (
        "opc-finance-box connector-access-request-init /absolute/BOX_CONFIG "
        "--pack connector.shopify --entity ENTITY_ID "
        "--output /absolute/private/shopify-access-request.json"
    ),
    "connector.stripe": (
        "opc-finance-box connector-access-request-init /absolute/BOX_CONFIG "
        "--pack connector.stripe --entity ENTITY_ID "
        "--output /absolute/private/stripe-access-request.json"
    ),
    "connector.wise": (
        "opc-finance-box connector-access-request-init /absolute/BOX_CONFIG "
        "--pack connector.wise --entity ENTITY_ID "
        "--output /absolute/private/wise-access-request.json"
    ),
    "connector.xero": (
        "opc-finance-box connector-access-request-init /absolute/BOX_CONFIG "
        "--pack connector.xero --entity ENTITY_ID "
        "--output /absolute/private/xero-access-request.json"
    ),
    "connector.paypal": (
        "opc-finance-box connector-access-request-init /absolute/BOX_CONFIG "
        "--pack connector.paypal --entity ENTITY_ID "
        "--output /absolute/private/paypal-access-request.json"
    ),
    "connector.woocommerce": (
        "opc-finance-box connector-access-request-init /absolute/BOX_CONFIG "
        "--pack connector.woocommerce --entity ENTITY_ID "
        "--output /absolute/private/woocommerce-access-request.json"
    ),
    "connector.shipbob": (
        "opc-finance-box connector-access-request-init /absolute/BOX_CONFIG "
        "--pack connector.shipbob --entity ENTITY_ID "
        "--output /absolute/private/shipbob-access-request.json"
    ),
    "connector.amazon_seller": (
        "opc-finance-box connector-access-request-init /absolute/BOX_CONFIG "
        "--pack connector.amazon_seller --entity ENTITY_ID "
        "--output /absolute/private/amazon-seller-access-request.json"
    ),
}


def _build_provider_groups(
    runtime: BoxRuntime,
    connectors: list[dict[str, Any]],
    environment: Mapping[str, str],
) -> list[dict[str, Any]]:
    """Compress dataset-level connectors into founder-facing Pack diagnostics."""
    pack_metadata = {
        str(item.get("id")): item for item in runtime.snapshot().get("packs", [])
    }
    grouped: dict[str, list[dict[str, Any]]] = {}
    for connector in connectors:
        grouped.setdefault(str(connector["pack_id"]), []).append(connector)

    result = []
    for pack_id, members in sorted(grouped.items()):
        metadata = pack_metadata.get(pack_id, {})
        network_access = any(bool(item.get("network_access")) for item in members)
        credential_by_name: dict[str, bool] = {}
        for member in members:
            for item in member.get("credential_status") or []:
                name = str(item["env_name"])
                credential_by_name[name] = (
                    credential_by_name.get(name, True) and bool(item["configured"])
                )
        credential_status = [
            {"env_name": name, "configured": configured}
            for name, configured in sorted(credential_by_name.items())
        ]
        credentials_ready = all(item["configured"] for item in credential_status)
        entity_ids = sorted({
            str(entity_id)
            for member in members
            for entity_id in (member.get("entity_ids") or [])
        })
        if pack_id in {
            "connector.paypal", "connector.woocommerce", "connector.shipbob",
            "connector.amazon_seller",
        }:
            dynamic_status: dict[str, bool] = {}
            binding_env = {
                "connector.paypal": PAYPAL_BINDINGS_ENV,
                "connector.woocommerce": WOOCOMMERCE_BINDINGS_ENV,
                "connector.shipbob": SHIPBOB_BINDINGS_ENV,
                "connector.amazon_seller": AMAZON_SELLER_BINDINGS_ENV,
            }[pack_id]
            dynamic_status[binding_env] = bool(
                str(environment.get(binding_env) or "").strip()
            )
            entity_ready = []
            for entity_id in entity_ids:
                try:
                    resolved = access_credential_group(
                        pack_id, entity_id, environment,
                    )
                except ConnectorEntityCredentialError:
                    entity_ready.append(False)
                    continue
                entity_ready.append(bool(resolved["configured"]))
                for name in resolved["env_names"]:
                    dynamic_status[str(name)] = bool(
                        str(environment.get(str(name)) or "").strip()
                    )
            credential_status = [
                {"env_name": name, "configured": configured}
                for name, configured in sorted(dynamic_status.items())
            ]
            credentials_ready = bool(entity_ids) and all(entity_ready)
        entity_binding_ready = bool(entity_ids) and all(
            bool(member.get("entity_ids")) for member in members
        )
        access_command = PRIVATE_ACCESS_REQUEST_COMMANDS.get(pack_id)
        command = PRIVATE_SHADOW_REQUEST_COMMANDS.get(pack_id)
        if not network_access:
            diagnostic_status = "ready_for_versioned_file_sample"
            next_action = {
                "action_id": "prepare_versioned_file_sample",
                "display_name": "准备版本固定的脱敏文件样例与主体映射",
                "owner_role": "data_custodian",
                "command_template": None,
                "completion_evidence": (
                    "文件版本、法律主体、来源期间、业务键和字段映射由有权人确认"
                ),
            }
        elif not credentials_ready:
            diagnostic_status = "blocked_missing_credential_reference"
            next_action = {
                "action_id": "configure_credential_reference",
                "display_name": "在服务端密钥管理中配置最小只读凭证引用",
                "owner_role": "data_access_reviewer",
                "command_template": None,
                "completion_evidence": "只记录环境变量名、权限范围和独立批准，不复制凭证值",
            }
        elif access_command:
            diagnostic_status = "ready_to_initialize_private_access_probe_request"
            next_action = {
                "action_id": "initialize_private_access_probe_request",
                "display_name": "初始化主体与服务方账户绑定的私有只读权限探测请求",
                "owner_role": "data_access_reviewer",
                "command_template": access_command,
                "completion_evidence": (
                    "0600 私有请求经验证后，由有权人在 CLI 显式使用 --allow-network；"
                    "结果必须落盘为可重验的 0600 私有回执，不得包含密钥、"
                    "账户标识或原始响应"
                ),
            }
        elif command:
            diagnostic_status = "ready_to_initialize_private_shadow_request"
            next_action = {
                "action_id": "initialize_private_shadow_request",
                "display_name": "初始化主体与期间绑定的私有只读并行请求",
                "owner_role": "connector_shadow_controller",
                "command_template": command,
                "completion_evidence": "私有请求通过专用验证器且尚未访问外部数据源",
            }
        else:
            diagnostic_status = "manual_private_shadow_request_required"
            next_action = {
                "action_id": "review_connector_specific_runbook",
                "display_name": "查看该服务方的专用同步与并行验证手册",
                "owner_role": "connector_shadow_controller",
                "command_template": "opc-finance-box connector-sync-plan --help",
                "completion_evidence": "专用请求边界、主体、期间和只读权限已由有权人确认",
            }
        stages = [
            {
                "stage_id": "provider_contract",
                "status": "passed",
                "evidence": "已安装能力包、可执行提供方和数据集合同已通过运行时装载",
            },
            {
                "stage_id": "credential_reference",
                "status": (
                    "not_required" if not network_access
                    else "passed" if credentials_ready else "blocked"
                ),
                "evidence": "只检查环境变量是否非空，不返回或记录凭证值",
            },
            {
                "stage_id": "entity_binding",
                "status": "passed" if entity_binding_ready else "blocked",
                "evidence": "所有数据集适配器均绑定至少一个当前法律主体",
            },
            {
                "stage_id": "provider_access_probe",
                "status": (
                    "not_required" if not network_access
                    else "available"
                    if diagnostic_status == "ready_to_initialize_private_access_probe_request"
                    else "manual"
                    if diagnostic_status == "manual_private_shadow_request_required"
                    else "locked"
                ),
                "evidence": (
                    "需要显式授权的只读权限、最小权限与服务方账户绑定回执；"
                    "回执必须在 Shadow 前重新绑定当前 Box/请求/主体；"
                    "凭证存在本身不能通过本阶段"
                ),
            },
            {
                "stage_id": "private_shadow_request",
                "status": (
                    "available"
                    if diagnostic_status in {
                        "manual_private_shadow_request_required",
                        "ready_for_versioned_file_sample",
                    }
                    else "locked"
                ),
                "evidence": "尚未生成、读取或执行任何私有请求",
            },
            {
                "stage_id": "financial_reconciliation",
                "status": "locked",
                "evidence": "需要真实有界运行、来源计数和差异处置证据",
            },
            {
                "stage_id": "schedule_release",
                "status": "locked",
                "evidence": "需要独立复核、告警负责人和显式调度批准",
            },
        ]
        result.append({
            "pack_id": pack_id,
            "display_name": str(metadata.get("display_name") or pack_id),
            "pack_version": metadata.get("version"),
            "pack_status": metadata.get("status"),
            "network_access": network_access,
            "dataset_connector_count": len(members),
            "connector_ids": sorted(str(item["connector_id"]) for item in members),
            "dataset_types": sorted({
                str(dataset)
                for member in members
                for dataset in (member.get("dataset_types") or [])
            }),
            "used_by_pipelines": sorted({
                str(pipeline_id)
                for member in members
                for pipeline_id in (member.get("used_by_pipelines") or [])
            }),
            "entity_ids": entity_ids,
            "entity_binding_ready": entity_binding_ready,
            "credential_status": credential_status,
            "credentials_ready": credentials_ready,
            "diagnostic_status": diagnostic_status,
            "stages": stages,
            "next_action": next_action,
            "shadow_run_performed": False,
            "financial_reconciliation_completed": False,
            "schedule_released": False,
        })
    return result


def build_connector_onboarding(
    runtime: BoxRuntime,
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Project secret-free credential and shadow readiness for Pipeline connectors."""
    environment = os.environ if environ is None else environ
    connectors = build_box_connector_registry(runtime).catalog(runtime)
    pipeline_catalog = build_pipeline_runtime_catalog(runtime)
    used_by: dict[str, list[str]] = {}
    for pipeline in pipeline_catalog["pipelines"]:
        if pipeline.get("implementation_status") != "executable":
            continue
        for connector_id in [
            *(pipeline.get("required_connectors") or []),
            *(pipeline.get("required_connectors_any") or []),
        ]:
            used_by.setdefault(connector_id, []).append(pipeline["pipeline_id"])

    def project(connector: dict[str, Any]) -> dict[str, Any]:
        credential_env = list(connector.get("credential_env") or [])
        credential_status = [{
            "env_name": name,
            "configured": bool(str(environment.get(name) or "").strip()),
        } for name in credential_env]
        network_access = bool(connector.get("network_access"))
        credentials_ready = all(item["configured"] for item in credential_status)
        pack_id = str(connector.get("pack_id") or "")
        entity_ids = sorted({
            str(entity_id) for entity_id in (connector.get("entity_ids") or [])
        })
        if pack_id in {
            "connector.paypal", "connector.woocommerce", "connector.shipbob",
            "connector.amazon_seller",
        }:
            binding_env = {
                "connector.paypal": PAYPAL_BINDINGS_ENV,
                "connector.woocommerce": WOOCOMMERCE_BINDINGS_ENV,
                "connector.shipbob": SHIPBOB_BINDINGS_ENV,
                "connector.amazon_seller": AMAZON_SELLER_BINDINGS_ENV,
            }[pack_id]
            dynamic_status = {
                binding_env: bool(str(environment.get(binding_env) or "").strip()),
            }
            entity_ready = []
            for entity_id in entity_ids:
                try:
                    resolved = access_credential_group(
                        pack_id, entity_id, environment,
                    )
                except ConnectorEntityCredentialError:
                    entity_ready.append(False)
                    continue
                entity_ready.append(bool(resolved["configured"]))
                for name in resolved["env_names"]:
                    dynamic_status[str(name)] = bool(
                        str(environment.get(str(name)) or "").strip()
                    )
            credential_status = [
                {"env_name": name, "configured": configured}
                for name, configured in sorted(dynamic_status.items())
            ]
            credentials_ready = bool(entity_ids) and all(entity_ready)
        if not network_access:
            readiness = "ready_for_fixture_or_shadow"
        elif credentials_ready:
            readiness = "credential_ready_for_bounded_shadow"
        else:
            readiness = "blocked_missing_credential_reference"
        steps = [
            {
                "step": "provider_contract",
                "required_evidence": "provider contract test result and fixture version",
            },
        ]
        if network_access:
            steps.append({
                "step": "credential_reference",
                "required_evidence": "secret manager/environment variable names configured; values never copied",
            })
        steps.extend([
            {
                "step": "entity_and_source_mapping",
                "required_evidence": "explicit legal entity, source account/store and dataset mapping approval",
            },
            {
                "step": "bounded_shadow_run",
                "required_evidence": "date-bounded read-only run id, source counts and retry/page summary",
            },
            {
                "step": "checkpoint_and_backfill_control",
                "required_evidence": (
                    "committed time-window checkpoint, named reviewer, backfill proof that production "
                    "checkpoint did not advance, and resolved quarantine queue"
                ),
            },
            {
                "step": "financial_reconciliation",
                "required_evidence": "source totals, normalized totals, rejected rows and unexplained differences",
            },
            {
                "step": "schedule_release",
                "required_evidence": "named operator/reviewer, alert owner and explicit schedule approval",
            },
        ])
        return {
            **connector,
            "used_by_pipelines": sorted(set(used_by.get(connector["connector_id"], []))),
            "credential_status": credential_status,
            "credentials_ready": credentials_ready,
            "readiness": readiness,
            "shadow_run_performed": False,
            "schedule_installed": False,
            "secret_values_included": False,
            "onboarding_steps": steps,
        }

    projected = [project(connector) for connector in connectors]
    pipeline_connectors = [
        connector for connector in projected if connector["used_by_pipelines"]
    ]
    unreferenced = [
        connector for connector in projected if not connector["used_by_pipelines"]
    ]
    provider_groups = _build_provider_groups(
        runtime, pipeline_connectors, environment,
    )
    required_env: dict[str, bool] = {}
    for connector in pipeline_connectors:
        for status in connector["credential_status"]:
            required_env[status["env_name"]] = (
                required_env.get(status["env_name"], True) and status["configured"]
            )
    return {
        "schema_version": 3,
        "artifact_type": "opc_finance_box_connector_preflight",
        "runtime_fingerprint": runtime.snapshot()["fingerprint"],
        "summary": {
            "pipeline_connector_count": len(pipeline_connectors),
            "network_connector_count": sum(
                1 for connector in pipeline_connectors if connector["network_access"]
            ),
            "blocked_connector_count": sum(
                1 for connector in pipeline_connectors
                if connector["readiness"].startswith("blocked_")
            ),
            "provider_group_count": len(provider_groups),
            "network_provider_group_count": sum(
                1 for group in provider_groups if group["network_access"]
            ),
            "blocked_provider_group_count": sum(
                1 for group in provider_groups
                if group["diagnostic_status"].startswith("blocked_")
            ),
            "actionable_provider_group_count": sum(
                1 for group in provider_groups
                if group["diagnostic_status"] in {
                    "ready_to_initialize_private_access_probe_request",
                    "ready_to_initialize_private_shadow_request",
                    "manual_private_shadow_request_required",
                    "ready_for_versioned_file_sample",
                }
            ),
            "required_env": [
                {"env_name": name, "configured": configured}
                for name, configured in sorted(required_env.items())
            ],
        },
        "provider_groups": provider_groups,
        "pipeline_connectors": pipeline_connectors,
        "available_unreferenced_connectors": unreferenced,
        "control_boundary": {
            "credential_values_returned": False,
            "connector_dispatched": False,
            "network_access_performed": False,
            "provider_access_probe_performed": False,
            "provider_access_inferred_from_credentials": False,
            "shadow_run_inferred_from_credentials": False,
            "schedule_installed": False,
            "unreferenced_connectors_hidden_from_primary_workflow": True,
            "provider_groups_are_pack_level_projections": True,
            "commands_are_templates_only": True,
            "commands_executed": False,
            "private_requests_read_or_written": False,
            "entity_or_account_access_validated_externally": False,
            "financial_reconciliation_inferred": False,
        },
    }
