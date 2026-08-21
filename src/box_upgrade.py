from __future__ import annotations

from typing import Any


class BoxUpgradeError(ValueError):
    """Raised when an upgrade baseline is not a compiled Box contract."""


def _index(items: Any, key: str, field: str) -> dict[str, dict[str, Any]]:
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise BoxUpgradeError(f"{field} must be a list of objects")
    output: dict[str, dict[str, Any]] = {}
    for item in items:
        identity = item.get(key)
        if not isinstance(identity, str) or not identity:
            raise BoxUpgradeError(f"{field} item requires {key}")
        if identity in output:
            raise BoxUpgradeError(f"{field} contains duplicate {key}: {identity}")
        output[identity] = item
    return output


def _change(severity: str, category: str, identity: str, summary: str, **detail: Any) -> dict[str, Any]:
    return {
        "severity": severity,
        "category": category,
        "id": identity,
        "summary": summary,
        **detail,
    }


def compare_compiled_box(baseline: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """Compare two compiled contracts without changing runtime or external state."""
    if not isinstance(baseline, dict) or not isinstance(current, dict):
        raise BoxUpgradeError("baseline and current must be JSON objects")
    if baseline.get("schema_version") != 1 or current.get("schema_version") != 1:
        raise BoxUpgradeError("unsupported compiled Box schema_version")
    if not isinstance(baseline.get("lock"), dict) or not isinstance(current.get("lock"), dict):
        raise BoxUpgradeError("compiled Box requires lock")

    changes: list[dict[str, Any]] = []
    old_entities = _index(baseline.get("entities"), "id", "entities")
    new_entities = _index(current.get("entities"), "id", "entities")
    for entity_id in sorted(old_entities.keys() - new_entities.keys()):
        changes.append(_change("blocking", "entity_removed", entity_id, "法律主体从 Box 中移除"))
    for entity_id in sorted(new_entities.keys() - old_entities.keys()):
        changes.append(_change("review", "entity_added", entity_id, "新增法律主体，必须完成开户、期初、税务和权限配置"))
    entity_control_fields = (
        "jurisdiction", "functional_currency", "accounting_basis", "fiscal_year_end", "tax_pack",
    )
    for entity_id in sorted(old_entities.keys() & new_entities.keys()):
        for field in entity_control_fields:
            before, after = old_entities[entity_id].get(field), new_entities[entity_id].get(field)
            if before != after:
                changes.append(_change(
                    "blocking", "entity_control_changed", entity_id,
                    f"法律主体控制字段发生变化：{field}", field=field, before=before, after=after,
                ))
        before_regs = sorted(old_entities[entity_id].get("tax_registrations") or [])
        after_regs = sorted(new_entities[entity_id].get("tax_registrations") or [])
        if before_regs != after_regs:
            changes.append(_change(
                "review", "tax_registrations_changed", entity_id,
                "主体税务登记声明发生变化", before=before_regs, after=after_regs,
            ))

    old_packs = _index(baseline["lock"].get("packs"), "id", "lock.packs")
    new_packs = _index(current["lock"].get("packs"), "id", "lock.packs")
    for pack_id in sorted(old_packs.keys() - new_packs.keys()):
        changes.append(_change("blocking", "pack_removed", pack_id, "已锁定 Pack 被移除"))
    for pack_id in sorted(new_packs.keys() - old_packs.keys()):
        changes.append(_change("review", "pack_added", pack_id, "新增 Pack，需完成配置、控制与数据回归"))
    for pack_id in sorted(old_packs.keys() & new_packs.keys()):
        before, after = old_packs[pack_id], new_packs[pack_id]
        if before.get("version") != after.get("version"):
            changes.append(_change(
                "review", "pack_version_changed", pack_id, "Pack 版本发生变化",
                before=before.get("version"), after=after.get("version"),
            ))
        if before.get("status") != after.get("status"):
            changes.append(_change(
                "review", "pack_maturity_changed", pack_id, "Pack 成熟度标签发生变化",
                before=before.get("status"), after=after.get("status"),
            ))

    old_jurisdiction = baseline.get("jurisdiction_rules")
    new_jurisdiction = current.get("jurisdiction_rules")
    if old_jurisdiction is None and isinstance(new_jurisdiction, dict):
        changes.append(_change(
            "review", "jurisdiction_rule_lifecycle_added", "jurisdiction_rules",
            "新增税务规则来源复核周期，需配置当地复核责任人",
        ))
    elif isinstance(old_jurisdiction, dict) and isinstance(new_jurisdiction, dict):
        old_rules = _index(old_jurisdiction.get("entities") or [], "entity_id", "jurisdiction_rules.entities")
        new_rules = _index(new_jurisdiction.get("entities") or [], "entity_id", "jurisdiction_rules.entities")
        for entity_id in sorted(old_rules.keys() & new_rules.keys()):
            old_policy = old_rules[entity_id].get("review_policy")
            new_policy = new_rules[entity_id].get("review_policy")
            if old_policy is None and isinstance(new_policy, dict):
                changes.append(_change(
                    "review", "tax_review_policy_added", entity_id,
                    "新增税务规则时效与失效控制，需确认复核节奏",
                ))
            elif old_policy != new_policy:
                changes.append(_change(
                    "blocking", "tax_review_policy_changed", entity_id,
                    "税务规则时效或失效效果发生变化",
                    before=old_policy, after=new_policy,
                ))
            old_applicability_policy = old_rules[entity_id].get(
                "applicability_review_policy"
            )
            new_applicability_policy = new_rules[entity_id].get(
                "applicability_review_policy"
            )
            if old_applicability_policy is None and isinstance(
                new_applicability_policy, dict
            ):
                changes.append(_change(
                    "review", "tax_applicability_review_policy_added", entity_id,
                    "新增主体税务适用性签认时效控制，需重新签认事实截止日",
                ))
            elif old_applicability_policy != new_applicability_policy:
                changes.append(_change(
                    "blocking", "tax_applicability_review_policy_changed", entity_id,
                    "主体税务适用性签认时效或失效效果发生变化",
                    before=old_applicability_policy, after=new_applicability_policy,
                ))
            if old_rules[entity_id].get("verified_at") != new_rules[entity_id].get("verified_at"):
                changes.append(_change(
                    "review", "tax_rules_reverified", entity_id,
                    "税务规则官方来源复核日已更新",
                    before=old_rules[entity_id].get("verified_at"),
                    after=new_rules[entity_id].get("verified_at"),
                ))

    old_questionnaire = baseline.get("tax_applicability_questionnaire")
    new_questionnaire = current.get("tax_applicability_questionnaire")
    if old_questionnaire is None and isinstance(new_questionnaire, dict):
        changes.append(_change(
            "review", "tax_applicability_questionnaire_added", "tax_applicability_questionnaire",
            "新增逐主体税务 Pack 适用性问卷，需当地税务复核",
        ))
    old_applicability_schema = baseline.get("tax_applicability_artifact_schema")
    new_applicability_schema = current.get("tax_applicability_artifact_schema")
    if old_applicability_schema is None and isinstance(new_applicability_schema, dict):
        changes.append(_change(
            "review", "tax_applicability_review_contract_added",
            "tax_applicability_artifact_schema",
            "新增逐主体适用性工作底稿与独立签认契约",
        ))
    elif (
        isinstance(old_applicability_schema, dict)
        and isinstance(new_applicability_schema, dict)
        and old_applicability_schema != new_applicability_schema
    ):
        changes.append(_change(
            "blocking", "tax_applicability_review_contract_changed",
            "tax_applicability_artifact_schema",
            "税务适用性签认结构契约发生变化",
        ))
    old_artifact_security = baseline.get("tax_applicability_artifact_security_policy")
    new_artifact_security = current.get("tax_applicability_artifact_security_policy")
    if old_artifact_security is None and isinstance(new_artifact_security, dict):
        changes.append(_change(
            "review", "tax_applicability_artifact_security_policy_added",
            "tax_applicability_artifact_security_policy",
            "新增私有签认文件权限、符号链接与轮换目录控制",
        ))
    elif (
        isinstance(old_artifact_security, dict)
        and isinstance(new_artifact_security, dict)
        and old_artifact_security != new_artifact_security
    ):
        changes.append(_change(
            "blocking", "tax_applicability_artifact_security_policy_changed",
            "tax_applicability_artifact_security_policy",
            "私有签认文件或轮换目录安全策略发生变化",
        ))
    old_registry_receipt_schema = baseline.get(
        "tax_applicability_registry_receipt_schema"
    )
    new_registry_receipt_schema = current.get(
        "tax_applicability_registry_receipt_schema"
    )
    if old_registry_receipt_schema is None and isinstance(
        new_registry_receipt_schema, dict
    ):
        changes.append(_change(
            "review", "tax_applicability_registry_receipt_schema_added",
            "tax_applicability_registry_receipt_schema",
            "新增轮换目录内容指纹与受控激活收据契约",
        ))
    elif (
        isinstance(old_registry_receipt_schema, dict)
        and isinstance(new_registry_receipt_schema, dict)
        and old_registry_receipt_schema != new_registry_receipt_schema
    ):
        changes.append(_change(
            "blocking", "tax_applicability_registry_receipt_schema_changed",
            "tax_applicability_registry_receipt_schema",
            "轮换目录激活收据结构契约发生变化",
        ))

    old_pilot_plan = baseline.get("pilot_readiness_plan")
    new_pilot_plan = current.get("pilot_readiness_plan")
    if old_pilot_plan is None and isinstance(new_pilot_plan, dict):
        changes.append(_change(
            "review", "pilot_readiness_plan_added", "pilot_readiness_plan",
            "新增首家真实 OPC 数据接入与受限 Shadow Close 准入计划",
        ))
    elif (
        isinstance(old_pilot_plan, dict)
        and isinstance(new_pilot_plan, dict)
        and old_pilot_plan != new_pilot_plan
    ):
        changes.append(_change(
            "blocking", "pilot_readiness_plan_changed", "pilot_readiness_plan",
            "主体、资料域或网络 Connector 准入范围发生变化",
        ))
    old_production_plan = baseline.get("production_readiness_plan")
    new_production_plan = current.get("production_readiness_plan")
    if old_production_plan is None and isinstance(new_production_plan, dict):
        changes.append(_change(
            "review", "production_readiness_plan_added",
            "production_readiness_plan",
            "新增生产准备度与首客激活编排契约",
        ))
    elif (
        isinstance(old_production_plan, dict)
        and isinstance(new_production_plan, dict)
        and old_production_plan != new_production_plan
    ):
        changes.append(_change(
            "blocking", "production_readiness_plan_changed",
            "production_readiness_plan",
            "生产准备阶段、依赖、职责或命令合同发生变化",
        ))
    old_pilot_schema = baseline.get("pilot_readiness_artifact_schema")
    new_pilot_schema = current.get("pilot_readiness_artifact_schema")
    if old_pilot_schema is None and isinstance(new_pilot_schema, dict):
        changes.append(_change(
            "review", "pilot_readiness_artifact_schema_added",
            "pilot_readiness_artifact_schema",
            "新增私有首家公司准入工作底稿与独立复核契约",
        ))
    elif (
        isinstance(old_pilot_schema, dict)
        and isinstance(new_pilot_schema, dict)
        and old_pilot_schema != new_pilot_schema
    ):
        changes.append(_change(
            "blocking", "pilot_readiness_artifact_schema_changed",
            "pilot_readiness_artifact_schema",
            "首家公司准入签认结构契约发生变化",
        ))

    old_handoff_plan = baseline.get("pilot_data_handoff_plan")
    new_handoff_plan = current.get("pilot_data_handoff_plan")
    if old_handoff_plan is None and isinstance(new_handoff_plan, dict):
        changes.append(_change(
            "review", "pilot_data_handoff_plan_added", "pilot_data_handoff_plan",
            "新增首家真实 OPC 受控资料交接计划",
        ))
    elif (
        isinstance(old_handoff_plan, dict)
        and isinstance(new_handoff_plan, dict)
        and old_handoff_plan != new_handoff_plan
    ):
        changes.append(_change(
            "blocking", "pilot_data_handoff_plan_changed", "pilot_data_handoff_plan",
            "主体、资料域、传输或隐私控制契约发生变化",
        ))
    old_handoff_schema = baseline.get("pilot_data_handoff_artifact_schema")
    new_handoff_schema = current.get("pilot_data_handoff_artifact_schema")
    if old_handoff_schema is None and isinstance(new_handoff_schema, dict):
        changes.append(_change(
            "review", "pilot_data_handoff_artifact_schema_added",
            "pilot_data_handoff_artifact_schema",
            "新增私有资料交接工作底稿与独立访问复核契约",
        ))
    elif (
        isinstance(old_handoff_schema, dict)
        and isinstance(new_handoff_schema, dict)
        and old_handoff_schema != new_handoff_schema
    ):
        changes.append(_change(
            "blocking", "pilot_data_handoff_artifact_schema_changed",
            "pilot_data_handoff_artifact_schema",
            "首家公司资料交接签认结构契约发生变化",
        ))

    old_shadow_registration_schema = baseline.get(
        "pilot_shadow_run_registration_schema"
    )
    new_shadow_registration_schema = current.get(
        "pilot_shadow_run_registration_schema"
    )
    if old_shadow_registration_schema is None and isinstance(
        new_shadow_registration_schema, dict
    ):
        changes.append(_change(
            "review", "pilot_shadow_run_registration_schema_added",
            "pilot_shadow_run_registration_schema",
            "新增首家公司逐主体月结 Shadow Run 台账绑定契约",
        ))
    elif (
        isinstance(old_shadow_registration_schema, dict)
        and isinstance(new_shadow_registration_schema, dict)
        and old_shadow_registration_schema != new_shadow_registration_schema
    ):
        changes.append(_change(
            "blocking", "pilot_shadow_run_registration_schema_changed",
            "pilot_shadow_run_registration_schema",
            "首家公司 Shadow Run 登记结构或授权边界发生变化",
        ))

    old_shadow_observation_schema = baseline.get(
        "pilot_shadow_observation_artifact_schema"
    )
    new_shadow_observation_schema = current.get(
        "pilot_shadow_observation_artifact_schema"
    )
    if old_shadow_observation_schema is None and isinstance(
        new_shadow_observation_schema, dict
    ):
        changes.append(_change(
            "review", "pilot_shadow_observation_artifact_schema_added",
            "pilot_shadow_observation_artifact_schema",
            "新增首次 Shadow 观察、第四角色复核与源证据闭环契约",
        ))
    elif (
        isinstance(old_shadow_observation_schema, dict)
        and isinstance(new_shadow_observation_schema, dict)
        and old_shadow_observation_schema != new_shadow_observation_schema
    ):
        changes.append(_change(
            "blocking", "pilot_shadow_observation_artifact_schema_changed",
            "pilot_shadow_observation_artifact_schema",
            "首次 Shadow 观察结构、角色隔离或授权边界发生变化",
        ))

    old_shadow_series_schema = baseline.get(
        "pilot_shadow_series_artifact_schema"
    )
    new_shadow_series_schema = current.get(
        "pilot_shadow_series_artifact_schema"
    )
    if old_shadow_series_schema is None and isinstance(
        new_shadow_series_schema, dict
    ):
        changes.append(_change(
            "review", "pilot_shadow_series_artifact_schema_added",
            "pilot_shadow_series_artifact_schema",
            "新增连续 Shadow 期间重验、跨期趋势与独立复核契约",
        ))
    elif (
        isinstance(old_shadow_series_schema, dict)
        and isinstance(new_shadow_series_schema, dict)
        and old_shadow_series_schema != new_shadow_series_schema
    ):
        changes.append(_change(
            "blocking", "pilot_shadow_series_artifact_schema_changed",
            "pilot_shadow_series_artifact_schema",
            "连续 Shadow 期间结构、源证据绑定或授权边界发生变化",
        ))

    old_cfo_overlay = baseline.get("cfo_control_overlay")
    new_cfo_overlay = current.get("cfo_control_overlay")
    if old_cfo_overlay is None and isinstance(new_cfo_overlay, dict):
        changes.append(_change(
            "review", "cfo_control_overlay_added", "cfo_control_overlay",
            "新增 Pack 驱动的业务模型 CFO 控制覆盖层，需复核月度方法范围",
        ))
    elif (
        isinstance(old_cfo_overlay, dict)
        and isinstance(new_cfo_overlay, dict)
        and old_cfo_overlay != new_cfo_overlay
    ):
        changes.append(_change(
            "blocking", "cfo_control_overlay_changed", "cfo_control_overlay",
            "业务模型控制重点、数据源边界或创始人复盘问题发生变化",
        ))

    old_cfo_metrics = baseline.get("cfo_metric_catalog")
    new_cfo_metrics = current.get("cfo_metric_catalog")
    if old_cfo_metrics is None and isinstance(new_cfo_metrics, dict):
        changes.append(_change(
            "review", "cfo_metric_catalog_added", "cfo_metric_catalog",
            "新增 Pack 驱动的 CFO 指标定义，需复核公式、数据域与聚合边界",
        ))
    elif (
        isinstance(old_cfo_metrics, dict)
        and isinstance(new_cfo_metrics, dict)
        and old_cfo_metrics != new_cfo_metrics
    ):
        changes.append(_change(
            "blocking", "cfo_metric_catalog_changed", "cfo_metric_catalog",
            "CFO 指标公式、必需数据域、控制条件或聚合规则发生变化",
        ))

    old_capabilities = set(baseline.get("capabilities") or [])
    new_capabilities = set(current.get("capabilities") or [])
    for capability in sorted(old_capabilities - new_capabilities):
        changes.append(_change("blocking", "capability_removed", capability, "已用 capability 被移除"))
    for capability in sorted(new_capabilities - old_capabilities):
        changes.append(_change("review", "capability_added", capability, "新增 capability，需验证数据和控制边界"))

    old_services = _index(baseline.get("services"), "service_id", "services")
    new_services = _index(current.get("services"), "service_id", "services")
    for service_id in sorted(old_services.keys() - new_services.keys()):
        changes.append(_change("blocking", "service_removed", service_id, "已锁定 Service 被移除"))
    for service_id in sorted(new_services.keys() - old_services.keys()):
        changes.append(_change("review", "service_added", service_id, "新增 Service，需验证调用方与权限"))
    service_contract_fields = ("pack_id", "capability", "deterministic", "action_class", "entity_scope", "review_gate")
    for service_id in sorted(old_services.keys() & new_services.keys()):
        for field in service_contract_fields:
            before, after = old_services[service_id].get(field), new_services[service_id].get(field)
            if before != after:
                changes.append(_change(
                    "blocking", "service_contract_changed", service_id,
                    f"Service 控制契约发生变化：{field}", field=field, before=before, after=after,
                ))

    old_connectors = _index(baseline.get("connectors"), "connector_id", "connectors")
    new_connectors = _index(current.get("connectors"), "connector_id", "connectors")
    for connector_id in sorted(old_connectors.keys() - new_connectors.keys()):
        changes.append(_change("blocking", "connector_removed", connector_id, "已锁定 Connector 被移除"))
    for connector_id in sorted(new_connectors.keys() - old_connectors.keys()):
        changes.append(_change("review", "connector_added", connector_id, "新增 Connector，需通过 contract testkit"))
    for connector_id in sorted(old_connectors.keys() & new_connectors.keys()):
        for field in ("pack_id", "capability", "dataset_types", "business_keys", "credential_env", "network_access"):
            before, after = old_connectors[connector_id].get(field), new_connectors[connector_id].get(field)
            if before != after:
                changes.append(_change(
                    "blocking", "connector_contract_changed", connector_id,
                    f"Connector 契约发生变化：{field}", field=field, before=before, after=after,
                ))
        old_scope = set(old_connectors[connector_id].get("entity_ids") or old_entities)
        new_scope = set(new_connectors[connector_id].get("entity_ids") or new_entities)
        removed_scope = sorted(old_scope - new_scope)
        added_scope = sorted(new_scope - old_scope)
        if removed_scope:
            changes.append(_change(
                "blocking", "connector_entity_binding_reduced", connector_id,
                "Connector 不再绑定既有法律主体，需迁移调用方并重做 Shadow",
                removed_entity_ids=removed_scope, added_entity_ids=added_scope,
            ))
        elif added_scope:
            changes.append(_change(
                "review", "connector_entity_binding_expanded", connector_id,
                "Connector 新增法律主体绑定，需完成凭证隔离、映射和 Shadow 复核",
                added_entity_ids=added_scope,
            ))

    old_pipelines = _index(baseline.get("pipelines") or [], "pipeline_id", "pipelines")
    new_pipelines = _index(current.get("pipelines") or [], "pipeline_id", "pipelines")
    for pipeline_id in sorted(old_pipelines.keys() - new_pipelines.keys()):
        changes.append(_change("blocking", "pipeline_removed", pipeline_id, "已锁定 Pipeline 被移除"))
    for pipeline_id in sorted(new_pipelines.keys() - old_pipelines.keys()):
        changes.append(_change("review", "pipeline_added", pipeline_id, "新增 Pipeline，需完成重跑与部分失败演练"))
    for pipeline_id in sorted(old_pipelines.keys() & new_pipelines.keys()):
        for field in (
            "capability", "entity_scope", "required_connectors", "required_connectors_any",
            "optional_connectors",
            "required_services", "review_gates", "external_actions",
        ):
            before, after = old_pipelines[pipeline_id].get(field), new_pipelines[pipeline_id].get(field)
            if before != after:
                changes.append(_change(
                    "blocking", "pipeline_contract_changed", pipeline_id,
                    f"Pipeline 控制契约发生变化：{field}", field=field, before=before, after=after,
                ))

    old_run_policy = baseline.get("pipeline_run_policy")
    new_run_policy = current.get("pipeline_run_policy")
    if old_run_policy is None and isinstance(new_run_policy, dict):
        changes.append(_change(
            "review", "pipeline_run_policy_added", "pipeline_run_policy",
            "新增 Pipeline 运行台账与复核策略，需完成存储、备份和恢复验收",
        ))
    elif isinstance(old_run_policy, dict) and isinstance(new_run_policy, dict):
        for field in (
            "runtime_scope", "event_types", "review_decisions",
            "backup_and_restore",
            "release_candidate_rule", "release_candidate_is_external_authorization",
            "external_actions_performed", "posting_performed",
        ):
            before, after = old_run_policy.get(field), new_run_policy.get(field)
            if before != after:
                changes.append(_change(
                    "blocking", "pipeline_run_policy_changed", "pipeline_run_policy",
                    f"Pipeline 运行控制策略发生变化：{field}",
                    field=field, before=before, after=after,
                ))

    old_security_policy = baseline.get("runtime_security_policy")
    new_security_policy = current.get("runtime_security_policy")
    if old_security_policy is None and isinstance(new_security_policy, dict):
        changes.append(_change(
            "review", "runtime_security_policy_added", "runtime_security_policy",
            "新增运行时认证与职责分离策略，需完成 principal、token 和网络入口验收",
        ))
    elif isinstance(old_security_policy, dict) and isinstance(new_security_policy, dict):
        for field in (
            "server_binding_default", "non_loopback_requires_authentication",
            "authentication_modes", "role_policy", "route_classes",
            "authenticated_actor_source", "request_actor_override_allowed_when_authenticated",
            "public_api_paths", "response_cache_policy", "tls_included",
        ):
            before, after = old_security_policy.get(field), new_security_policy.get(field)
            if before != after:
                changes.append(_change(
                    "blocking", "runtime_security_policy_changed", "runtime_security_policy",
                    f"运行时安全控制策略发生变化：{field}",
                    field=field, before=before, after=after,
                ))

    old_runtime_data = baseline.get("runtime_data_contract")
    new_runtime_data = current.get("runtime_data_contract")
    if old_runtime_data is None and isinstance(new_runtime_data, dict):
        changes.append(_change(
            "review", "runtime_data_contract_added", "runtime_data_contract",
            "新增版本化运行数据契约，需完成预检、备份和恢复验收",
        ))
    elif isinstance(old_runtime_data, dict) and isinstance(new_runtime_data, dict):
        old_layout = old_runtime_data.get("layout") or {}
        new_layout = new_runtime_data.get("layout") or {}
        old_stores = old_layout.get("stores") or {}
        new_stores = new_layout.get("stores") or {}
        removed_or_changed = {
            name for name, contract in old_stores.items()
            if name not in new_stores or new_stores.get(name) != contract
        }
        if removed_or_changed:
            changes.append(_change(
                "blocking", "runtime_data_store_changed", "runtime_data_contract",
                "已受管运行数据 store 被移除或改变",
                stores=sorted(removed_or_changed),
            ))
        added_stores = sorted(set(new_stores) - set(old_stores))
        if (
            old_layout.get("current_version") != new_layout.get("current_version")
            or added_stores
        ):
            changes.append(_change(
                "review", "runtime_data_layout_changed", "runtime_data_contract",
                "运行数据 layout 需显式停服备份和迁移复核",
                before=old_layout.get("current_version"),
                after=new_layout.get("current_version"),
                added_stores=added_stores,
            ))

    old_promotion = baseline.get("stable_promotion_policy")
    new_promotion = current.get("stable_promotion_policy")
    if old_promotion is None and isinstance(new_promotion, dict):
        changes.append(_change(
            "review", "stable_promotion_policy_added", "stable_promotion_policy",
            "新增 stable candidate 证据、阈值与职责分离契约",
        ))
    elif isinstance(old_promotion, dict) and isinstance(new_promotion, dict):
        for field in (
            "minimum_controls", "ledger", "approval_effect",
            "pack_manifest_changed_automatically", "external_actions_performed",
        ):
            before, after = old_promotion.get(field), new_promotion.get(field)
            if before != after:
                changes.append(_change(
                    "blocking", "stable_promotion_policy_changed", "stable_promotion_policy",
                    f"Stable promotion 控制契约发生变化：{field}",
                    field=field, before=before, after=after,
                ))

    old_promotion_schema = baseline.get("stable_promotion_evidence_schema")
    new_promotion_schema = current.get("stable_promotion_evidence_schema")
    if old_promotion_schema is None and isinstance(new_promotion_schema, dict):
        changes.append(_change(
            "review", "stable_promotion_evidence_schema_added",
            "stable_promotion_evidence_schema",
            "新增 stable promotion 证据机器契约，需复核填写与验证工具",
        ))
    elif (
        isinstance(old_promotion_schema, dict)
        and isinstance(new_promotion_schema, dict)
        and old_promotion_schema != new_promotion_schema
    ):
        changes.append(_change(
            "blocking", "stable_promotion_evidence_schema_changed",
            "stable_promotion_evidence_schema",
            "Stable promotion 证据 schema 发生变化，必须迁移证据生成器与存量输入",
        ))

    old_promotion_templates = baseline.get("stable_promotion_evidence_templates")
    new_promotion_templates = current.get("stable_promotion_evidence_templates")
    if old_promotion_templates is None and isinstance(new_promotion_templates, dict):
        changes.append(_change(
            "review", "stable_promotion_evidence_templates_added",
            "stable_promotion_evidence_templates",
            "新增 Box/Pack 绑定证据模板，需确认占位符保持失败关闭",
        ))
    elif isinstance(old_promotion_templates, dict) and isinstance(new_promotion_templates, dict):
        for field in ("template_only", "assessment_ready", "evidence_schema"):
            before = old_promotion_templates.get(field)
            after = new_promotion_templates.get(field)
            if before != after:
                changes.append(_change(
                    "blocking", "stable_promotion_evidence_template_control_changed",
                    "stable_promotion_evidence_templates",
                    f"Stable promotion 证据模板控制字段发生变化：{field}",
                    field=field, before=before, after=after,
                ))

    old_workflows = _index(baseline.get("workflows"), "workflow_id", "workflows")
    new_workflows = _index(current.get("workflows"), "workflow_id", "workflows")
    for workflow_id in sorted(old_workflows.keys() - new_workflows.keys()):
        changes.append(_change("blocking", "workflow_removed", workflow_id, "已锁定工作流被移除"))
    for workflow_id in sorted(new_workflows.keys() - old_workflows.keys()):
        changes.append(_change("review", "workflow_added", workflow_id, "新增工作流，默认不得自动启用调度"))
    for workflow_id in sorted(old_workflows.keys() & new_workflows.keys()):
        for field in ("capability", "human_gate"):
            before, after = old_workflows[workflow_id].get(field), new_workflows[workflow_id].get(field)
            if before != after:
                changes.append(_change(
                    "blocking", "workflow_control_changed", workflow_id,
                    f"工作流控制字段发生变化：{field}", field=field, before=before, after=after,
                ))

    counts = {
        severity: sum(change["severity"] == severity for change in changes)
        for severity in ("blocking", "review", "info")
    }
    return {
        "schema_version": 1,
        "compatible": counts["blocking"] == 0,
        "unchanged": not changes,
        "requires_review": bool(changes),
        "baseline_fingerprint": baseline["lock"].get("runtime_fingerprint"),
        "current_fingerprint": current["lock"].get("runtime_fingerprint"),
        "counts": counts,
        "changes": changes,
        "recommended_action": (
            "可以继续使用当前 lock；未检测到契约变化。" if not changes
            else "存在 blocking 变化：先迁移配置/调用方并完成 shadow run，不要直接替换生产 Box lock。"
            if counts["blocking"] else "只有需复核变化：完成 Pack 回归、控制人确认和 shadow run 后再接受新 lock。"
        ),
    }


def build_upgrade_policy(compiled: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "baseline_fingerprint": compiled["lock"]["runtime_fingerprint"],
        "command": "opc-finance-box upgrade-check <box-config.json> <previous-box.lock.json>",
        "blocking_changes": [
            "法律主体移除或法定控制字段变化",
            "Pack、capability、Service、Connector、Pipeline 或工作流移除",
            "Service 的确定性、动作类别、主体范围或 review gate 变化",
            "Pipeline provider/主体/外部动作契约或工作流 capability/human gate 变化",
            "受管运行数据 store 被移除/改变，或 stable promotion 阈值、ledger、职责分离、证据 schema 契约变化",
        ],
        "review_changes": [
            "新增主体、Pack、capability、Service、Connector、Pipeline 或工作流",
            "Pack 版本/成熟度或主体税务登记变化",
            "运行数据 layout 版本或增量 store 变化，新增 stable promotion policy/schema/template",
        ],
        "acceptance": "所有变化先在 shadow run 中验证；本文件不自动批准升级。",
    }
