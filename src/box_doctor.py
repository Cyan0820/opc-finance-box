from __future__ import annotations

import importlib.util
import hashlib
import shutil
import sys
from pathlib import Path
from typing import Any

from .box_compiler import compile_box
from .box_runtime import BoxRuntime
from .default_services import build_default_service_registry
from .pack_services import PackServiceRegistry
from .tax_pack_lifecycle import evaluate_tax_rule_lifecycle
from .tax_applicability_artifacts import (
    TaxApplicabilityArtifactError,
    verify_tax_applicability_review,
    verify_tax_applicability_registry_receipt,
)
from .pilot_readiness import build_pilot_readiness_status
from .pilot_data_handoff import build_pilot_data_handoff_status
from .pilot_shadow_run import build_pilot_shadow_run_status
from .pilot_shadow_observation import build_pilot_shadow_observation_status
from .pilot_shadow_series import build_pilot_shadow_series_status


def _check(
    check_id: str,
    status: str,
    summary: str,
    *,
    remediation: str = "",
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "check_id": check_id,
        "status": status,
        "summary": summary,
        "remediation": remediation,
        "details": details or {},
    }


def diagnose_box(
    runtime: BoxRuntime,
    registry: PackServiceRegistry | None = None,
    *,
    python_version: tuple[int, int, int] | None = None,
    dependency_probe: dict[str, bool] | None = None,
    executable_probe: dict[str, bool] | None = None,
    as_of: str | None = None,
    tax_applicability_review_paths: list[str | Path] | None = None,
    tax_applicability_review_dir: str | Path | None = None,
    tax_applicability_registry_receipt: str | Path | None = None,
    pilot_readiness_review: str | Path | None = None,
    pilot_data_handoff_review: str | Path | None = None,
    pilot_shadow_run_registration: str | Path | None = None,
    pilot_shadow_observation_review: str | Path | None = None,
    pilot_shadow_entity_reports: list[str | Path] | None = None,
    pilot_shadow_portfolio_review: str | Path | None = None,
    pilot_shadow_series_review: str | Path | None = None,
    pilot_shadow_series_evidence_root: str | Path | None = None,
    pipeline_runs_root: str | Path | None = None,
) -> dict[str, Any]:
    """Return a deterministic readiness report without changing external or ledger state."""
    selected_registry = registry or build_default_service_registry()
    compiled = compile_box(runtime, selected_registry)
    checks: list[dict[str, Any]] = []

    current_python = python_version or tuple(sys.version_info[:3])
    python_ok = current_python >= (3, 10, 0)
    checks.append(_check(
        "runtime.python",
        "pass" if python_ok else "blocker",
        f"Python {'.'.join(str(item) for item in current_python)} "
        + ("满足最低版本" if python_ok else "低于受支持的 3.10"),
        remediation="使用 Python 3.10+ 创建虚拟环境并重新安装项目。" if not python_ok else "",
    ))

    dependencies = dependency_probe or {
        package: importlib.util.find_spec(package) is not None
        for package in ("openpyxl", "pypdf")
    }
    missing_dependencies = sorted(package for package, available in dependencies.items() if not available)
    checks.append(_check(
        "runtime.dependencies",
        "pass" if not missing_dependencies else "blocker",
        "Python 运行依赖已安装" if not missing_dependencies else f"缺少运行依赖：{', '.join(missing_dependencies)}",
        remediation="运行 `python -m pip install -e .`。" if missing_dependencies else "",
        details={"dependencies": dependencies},
    ))

    snapshot = runtime.snapshot()
    checks.append(_check(
        "box.sources",
        "pass",
        f"Box 已解析 {len(snapshot['packs'])} 个 Pack、{len(snapshot['entities'])} 个法律主体",
        details={"fingerprint": snapshot["fingerprint"]},
    ))
    unstable = [pack for pack in snapshot["packs"] if pack["status"] != "stable"]
    checks.append(_check(
        "box.pack_maturity",
        "warning" if unstable else "pass",
        f"{len(unstable)} 个 Pack 尚未 stable" if unstable else "所有 Pack 均为 stable",
        remediation="上线前逐 Pack 完成兼容性、控制和数据回归验收。" if unstable else "",
        details={"packs": [{"id": pack["id"], "status": pack["status"]} for pack in unstable]},
    ))

    services = compiled["services"]
    workflows = compiled["workflows"]
    checks.append(_check(
        "box.executable_surface",
        "pass" if services and workflows else "blocker",
        f"已启用 {len(services)} 个服务、{len(workflows)} 个工作流",
        remediation="为所选 Pack 注册至少一个服务和工作流。" if not services or not workflows else "",
    ))
    declared_only = compiled["declared_only_capabilities"]
    checks.append(_check(
        "box.capability_coverage",
        "warning" if declared_only else "pass",
        (
            f"{len(declared_only)} 项 Pack capability 只有声明，尚无 service/connector 实现绑定"
            if declared_only else "所有声明 capability 均有可执行实现绑定"
        ),
        remediation="补充服务或 Connector 并注册契约测试；在此之前不要把声明能力展示为已完成。" if declared_only else "",
        details={"declared_only_capabilities": declared_only},
    ))

    uncertain_registrations = [
        task for task in compiled["setup_tasks"] if task["category"] == "tax_registration"
    ]
    checks.append(_check(
        "tax.registrations",
        "warning" if uncertain_registrations else "pass",
        (
            f"{len(uncertain_registrations)} 个主体存在待确认税务登记"
            if uncertain_registrations else "主体税务登记字段未发现待确认标记"
        ),
        remediation="由当地税务复核人确认登记状态后更新 Box 配置。" if uncertain_registrations else "",
        details={"tasks": uncertain_registrations},
    ))
    non_filing_entities = [
        entity for entity in snapshot["entities"] if entity.get("tax_readiness") != "filing_assist"
    ]
    checks.append(_check(
        "tax.filing_readiness",
        "warning" if non_filing_entities else "pass",
        (
            f"{len(non_filing_entities)} 个主体的税务包不是 filing_assist"
            if non_filing_entities else "所有主体税务包达到 filing_assist"
        ),
        remediation="保持外部申报禁用；使用当地复核人与实际申报系统完成提交。" if non_filing_entities else "",
        details={
            "entities": [
                {"id": entity["id"], "tax_readiness": entity.get("tax_readiness")}
                for entity in non_filing_entities
            ]
        },
    ))
    tax_lifecycle = evaluate_tax_rule_lifecycle(runtime, as_of=as_of)
    expired_rules = [
        item for item in tax_lifecycle["entities"] if item["status"] == "expired"
    ]
    review_due_rules = [
        item for item in tax_lifecycle["entities"] if item["status"] == "review_due"
    ]
    lifecycle_status = "warning" if expired_rules or review_due_rules else "pass"
    lifecycle_summary = (
        f"{len(expired_rules)} 个主体的税务规则来源复核已过期"
        if expired_rules else
        f"{len(review_due_rules)} 个主体的税务规则即将到期"
        if review_due_rules else "所有主体的税务规则来源在复核周期内"
    )
    checks.append(_check(
        "tax.rule_lifecycle",
        lifecycle_status,
        lifecycle_summary,
        remediation=(
            "由当地税务复核人重新检查官方来源、更新 verified_at/Pack 版本并跑 upgrade-check；"
            "expired 期间不得释放税务日历或外部申报。"
            if lifecycle_status == "warning" else ""
        ),
        details={"as_of": tax_lifecycle["as_of"], "entities": tax_lifecycle["entities"]},
    ))

    applicability_reviews = []
    invalid_reviews = []
    reviews_by_entity: dict[str, dict[str, Any]] = {}
    for index, path in enumerate(tax_applicability_review_paths or []):
        try:
            summary = verify_tax_applicability_review(runtime, path, as_of=as_of)
        except (TaxApplicabilityArtifactError, OSError, ValueError) as exc:
            invalid_reviews.append({
                "input_index": index,
                "error_sha256": hashlib.sha256(str(exc).encode("utf-8")).hexdigest(),
            })
            continue
        entity_id = summary["entity_id"]
        if entity_id in reviews_by_entity:
            invalid_reviews.append({
                "input_index": index,
                "error_sha256": hashlib.sha256(
                    f"duplicate:{entity_id}".encode("utf-8")
                ).hexdigest(),
            })
            continue
        reviews_by_entity[entity_id] = summary
        applicability_reviews.append(summary)
    configured_entity_ids = {item["id"] for item in snapshot["entities"]}
    approved_entity_ids = {
        entity_id for entity_id, summary in reviews_by_entity.items()
        if summary["applicability_gate_passed"]
        and summary["decision"] == "approved-in-scope"
        and summary["unanswered_count"] == 0
    }
    missing_applicability_reviews = sorted(configured_entity_ids - reviews_by_entity.keys())
    unapproved_applicability_reviews = sorted(
        reviews_by_entity.keys() - approved_entity_ids
    )
    review_due_applicability_reviews = sorted(
        entity_id for entity_id, summary in reviews_by_entity.items()
        if summary["lifecycle_status"] == "review_due"
    )
    expired_applicability_reviews = sorted(
        entity_id for entity_id, summary in reviews_by_entity.items()
        if summary["lifecycle_status"] == "expired"
    )
    applicability_complete = (
        not invalid_reviews
        and approved_entity_ids == configured_entity_ids
        and not unapproved_applicability_reviews
    )
    applicability_status = (
        "blocker" if invalid_reviews else
        "warning" if review_due_applicability_reviews or not applicability_complete else
        "pass"
    )
    checks.append(_check(
        "tax.applicability_reviews",
        applicability_status,
        (
            f"{len(review_due_applicability_reviews)} 个主体的适用性签认即将到期"
            if applicability_complete and review_due_applicability_reviews else
            "所有法律主体均有当前、独立且未过期的 Pack 适用性结论"
            if applicability_complete else
            f"{len(invalid_reviews)} 份税务适用性签认无效"
            if invalid_reviews else
            f"{len(missing_applicability_reviews)} 个主体缺少签认，"
            f"{len(unapproved_applicability_reviews)} 个主体尚未通过适用性门"
        ),
        remediation=(
            "逐主体以事实截止日运行 tax-applicability-init，完成证据引用后由不同的当地税务复核人"
            "运行 tax-applicability-review；再以 tax-applicability-verify 校验。"
            if not applicability_complete else
            "在失效日前以新的 facts_as_of 重新准备并独立签认。"
            if review_due_applicability_reviews else ""
        ),
        details={
            "required_entity_ids": sorted(configured_entity_ids),
            "approved_entity_ids": sorted(approved_entity_ids),
            "missing_entity_ids": missing_applicability_reviews,
            "unapproved_entity_ids": unapproved_applicability_reviews,
            "review_due_entity_ids": review_due_applicability_reviews,
            "expired_entity_ids": expired_applicability_reviews,
            "invalid_reviews": invalid_reviews,
            "reviews": applicability_reviews,
            "answers_returned": False,
            "review_rationales_returned": False,
            "evidence_references_returned": False,
        },
    ))

    registry_activation = None
    registry_activation_error = None
    if (
        tax_applicability_review_dir is None
        and tax_applicability_registry_receipt is not None
    ) or (
        tax_applicability_review_dir is not None
        and tax_applicability_registry_receipt is None
    ):
        registry_activation_error = hashlib.sha256(
            b"tax applicability registry requires both directory and receipt"
        ).hexdigest()
    elif (
        tax_applicability_review_dir is not None
        and tax_applicability_registry_receipt is not None
    ):
        try:
            registry_activation = verify_tax_applicability_registry_receipt(
                runtime,
                tax_applicability_review_dir,
                tax_applicability_registry_receipt,
                as_of=as_of,
            )
        except (TaxApplicabilityArtifactError, OSError, ValueError) as exc:
            registry_activation_error = hashlib.sha256(
                str(exc).encode("utf-8")
            ).hexdigest()
    registry_activation_ready = bool(
        registry_activation
        and registry_activation["registry_unchanged"]
        and registry_activation["ready_for_calendar_release"]
    )
    checks.append(_check(
        "tax.applicability_registry_activation",
        (
            "pass" if registry_activation_ready else
            "blocker" if registry_activation_error else "warning"
        ),
        (
            "税务适用性轮换目录与私有收据匹配"
            if registry_activation_ready else
            "税务适用性轮换目录或私有收据无效"
            if registry_activation_error else
            "尚未配置税务适用性轮换目录与私有收据"
        ),
        remediation=(
            "使用受控导入完成全主体目录，由独立 controller 运行 "
            "tax-applicability-registry-seal，再同时提供目录和收据。"
            if not registry_activation_ready else ""
        ),
        details={
            "configured": (
                tax_applicability_review_dir is not None
                or tax_applicability_registry_receipt is not None
            ),
            "valid": registry_activation_ready,
            **({"receipt_id": registry_activation["receipt_id"]}
               if registry_activation else {}),
            **({"error_sha256": registry_activation_error}
               if registry_activation_error else {}),
            "paths_returned": False,
            "private_review_contents_returned": False,
            "digital_signature_verified": False,
            "filing_authorization_granted": False,
        },
    ))

    pilot_status = build_pilot_readiness_status(
        runtime, pilot_readiness_review, as_of=as_of,
    )
    pilot_check_status = (
        "pass" if pilot_status["status"] == "current" else
        "warning" if pilot_status["status"] in {"missing", "review_due"} else
        "blocker"
    )
    checks.append(_check(
        "pilot.readiness_activation",
        pilot_check_status,
        (
            "首家公司准入签认当前有效"
            if pilot_status["status"] == "current" else
            "首家公司准入签认已进入复核窗口"
            if pilot_status["status"] == "review_due" else
            "尚未配置首家公司准入签认"
            if pilot_status["status"] == "missing" else
            "首家公司准入签认已过期或无效"
        ),
        remediation=(
            "运行 pilot-readiness-init，完成逐主体只读映射后由独立复核人运行 "
            "pilot-readiness-review；服务端仅挂载已签认的私有文件。"
            if pilot_status["status"] in {"missing", "invalid"} else
            "在 expires_at 前重新生成并独立签认当前 Box 的准入工作底稿。"
            if pilot_status["status"] in {"review_due", "expired"} else ""
        ),
        details={
            "configured": pilot_status["configured"],
            "valid": pilot_status["valid"],
            "status": pilot_status["status"],
            "ready_for_bounded_shadow": pilot_status["ready_for_bounded_shadow"],
            **({"period": pilot_status["period"]} if "period" in pilot_status else {}),
            **({"review_id": pilot_status["review_id"]} if "review_id" in pilot_status else {}),
            **({"review_due_at": pilot_status["review_due_at"]}
               if "review_due_at" in pilot_status else {}),
            **({"expires_at": pilot_status["expires_at"]}
               if "expires_at" in pilot_status else {}),
            **({"error_sha256": pilot_status["error_sha256"]}
               if "error_sha256" in pilot_status else {}),
            "paths_returned": False,
            "actors_returned": False,
            "evidence_references_returned": False,
            "credentials_returned": False,
            "financial_values_returned": False,
            "external_actions_performed": False,
        },
    ))

    handoff_status = build_pilot_data_handoff_status(
        runtime, pilot_data_handoff_review, pilot_readiness_review, as_of=as_of,
    )
    handoff_check_status = (
        "pass" if handoff_status["status"] == "current" else
        "warning" if handoff_status["status"] in {"missing", "review_due"} else
        "blocker"
    )
    checks.append(_check(
        "pilot.data_handoff_activation",
        handoff_check_status,
        (
            "首家公司资料交接签认当前有效"
            if handoff_status["status"] == "current" else
            "资料交接仍有效，但其 Pilot 准入已进入复核窗口"
            if handoff_status["status"] == "review_due" else
            "尚未配置首家公司资料交接签认"
            if handoff_status["status"] == "missing" else
            "资料交接签认或其 Pilot 准入绑定已过期或无效"
        ),
        remediation=(
            "在当前 Pilot 准入签认上运行 pilot-data-handoff-init，逐主体完成资料域、"
            "隐私与访问控制后由独立复核人运行 pilot-data-handoff-review。"
            if handoff_status["status"] in {"missing", "invalid"} else
            "在 Pilot expires_at 前重新完成准入与资料交接独立签认。"
            if handoff_status["status"] in {"review_due", "pilot_readiness_expired"}
            else ""
        ),
        details={
            "configured": handoff_status["configured"],
            "pilot_readiness_configured": handoff_status[
                "pilot_readiness_configured"
            ],
            "valid": handoff_status["valid"],
            "status": handoff_status["status"],
            "ready_for_controlled_data_intake": handoff_status[
                "ready_for_controlled_data_intake"
            ],
            "ready_for_bounded_shadow": handoff_status["ready_for_bounded_shadow"],
            **({"period": handoff_status["period"]}
               if "period" in handoff_status else {}),
            **({"review_id": handoff_status["review_id"]}
               if "review_id" in handoff_status else {}),
            **({"pilot_readiness_expires_at": handoff_status[
                "pilot_readiness_expires_at"
            ]} if "pilot_readiness_expires_at" in handoff_status else {}),
            **({"error_sha256": handoff_status["error_sha256"]}
               if "error_sha256" in handoff_status else {}),
            "paths_returned": False,
            "source_manifest_hash_values_returned": False,
            "actors_returned": False,
            "evidence_references_returned": False,
            "credentials_returned": False,
            "financial_values_returned": False,
            "external_actions_performed": False,
        },
    ))

    shadow_run_status = build_pilot_shadow_run_status(
        runtime,
        pilot_shadow_run_registration,
        pilot_data_handoff_review,
        pilot_readiness_review,
        pipeline_runs_root,
        as_of=as_of,
    )
    shadow_run_check_status = (
        "pass" if shadow_run_status["status"] == "current" else
        "warning" if shadow_run_status["status"] in {"missing", "review_due"} else
        "blocker"
    )
    checks.append(_check(
        "pilot.first_shadow_run_registration",
        shadow_run_check_status,
        (
            "首个逐主体月结 Shadow Run 登记当前有效"
            if shadow_run_status["status"] == "current" else
            "首个 Shadow Run 登记仍有效，但 Pilot 准入已进入复核窗口"
            if shadow_run_status["status"] == "review_due" else
            "尚未登记首个逐主体月结 Shadow Run"
            if shadow_run_status["status"] == "missing" else
            "首个 Shadow Run 登记、签认绑定或 Pipeline 台账无效"
        ),
        remediation=(
            "对每个法律主体运行 finance.month_close_control，完成全部独立复核 gate 后，"
            "由不同登记人运行 pilot-shadow-run-register。"
            if shadow_run_status["status"] in {"missing", "invalid"} else
            "在 Pilot expires_at 前重新完成准入、交接和逐主体 Shadow Run 登记。"
            if shadow_run_status["status"] == "review_due" else ""
        ),
        details={
            "configured": shadow_run_status["configured"],
            "handoff_configured": shadow_run_status["handoff_configured"],
            "pilot_readiness_configured": shadow_run_status[
                "pilot_readiness_configured"
            ],
            "pipeline_ledger_configured": shadow_run_status[
                "pipeline_ledger_configured"
            ],
            "valid": shadow_run_status["valid"],
            "status": shadow_run_status["status"],
            "ready_for_first_shadow_observation": shadow_run_status[
                "ready_for_first_shadow_observation"
            ],
            **({"period": shadow_run_status["period"]}
               if "period" in shadow_run_status else {}),
            **({"entity_count": shadow_run_status["entity_count"]}
               if "entity_count" in shadow_run_status else {}),
            **({"error_sha256": shadow_run_status["error_sha256"]}
               if "error_sha256" in shadow_run_status else {}),
            "paths_returned": False,
            "attempt_ids_returned": False,
            "result_fingerprints_returned": False,
            "actors_returned": False,
            "review_rationales_returned": False,
            "evidence_references_returned": False,
            "financial_values_returned": False,
            "external_actions_performed": False,
        },
    ))

    shadow_observation_status = build_pilot_shadow_observation_status(
        runtime,
        pilot_shadow_observation_review,
        pilot_shadow_run_registration,
        pilot_data_handoff_review,
        pilot_readiness_review,
        pipeline_runs_root,
        pilot_shadow_entity_reports or [],
        portfolio_review_path=pilot_shadow_portfolio_review,
        as_of=as_of,
    )
    shadow_observation_check_status = (
        "pass"
        if shadow_observation_status["status"] == "current"
        and shadow_observation_status["ready_for_next_shadow_period"]
        else "warning"
        if shadow_observation_status["status"] == "missing"
        else "blocker"
    )
    checks.append(_check(
        "pilot.first_shadow_observation_review",
        shadow_observation_check_status,
        (
            "首次 Shadow 观察已完成第四角色复核，可进入下一 Shadow 期间"
            if shadow_observation_check_status == "pass" else
            "尚未组装并独立复核首次 Shadow 观察"
            if shadow_observation_status["status"] == "missing" else
            "首次 Shadow 观察证据无效、已变化或被复核为需要修正"
        ),
        remediation=(
            "准备逐主体已签认 Shadow Close 报告；多主体 Box 再完成组合签认，然后依次运行 "
            "pilot-shadow-observation-assemble、pilot-shadow-observation-review 与 verify。"
            if shadow_observation_status["status"] == "missing" else
            "修复 system defect 或失效源证据，重新运行月结、逐主体签认、组合签认与第四角色复核。"
            if shadow_observation_check_status == "blocker" else ""
        ),
        details={
            key: value for key, value in shadow_observation_status.items()
            if key not in {
                "schema_version", "runtime_fingerprint", "status", "valid",
            }
        } | {
            "configured": shadow_observation_status["configured"],
            "valid": shadow_observation_status["valid"],
            "status": shadow_observation_status["status"],
        },
    ))

    shadow_series_status = build_pilot_shadow_series_status(
        runtime,
        pilot_shadow_series_review,
        pilot_shadow_series_evidence_root,
        pipeline_runs_root,
        as_of=as_of,
    )
    shadow_series_check_status = (
        "pass"
        if shadow_series_status["status"] == "current"
        and shadow_series_status[
            "eligible_to_prepare_stable_promotion_evidence"
        ]
        else "warning"
        if shadow_series_status["status"] == "missing"
        else "blocker"
    )
    checks.append(_check(
        "pilot.consecutive_shadow_series_review",
        shadow_series_check_status,
        (
            "连续 Shadow 期间已独立复核，可开始准备 stable 晋级证据"
            if shadow_series_check_status == "pass" else
            "尚未完成至少两个连续 Shadow 期间的独立复核"
            if shadow_series_status["status"] == "missing" else
            "连续 Shadow 期间证据无效、已变化或仍需修正"
        ),
        remediation=(
            "将每个已复核期间按 YYYY-MM 私有证据目录组织，依次运行 "
            "pilot-shadow-series-assemble、pilot-shadow-series-review 与 verify。"
            if shadow_series_status["status"] == "missing" else
            "修复缺期、失效源证据、系统缺陷或角色冲突，再重新组装连续期收据。"
            if shadow_series_check_status == "blocker" else ""
        ),
        details={
            key: value for key, value in shadow_series_status.items()
            if key not in {
                "schema_version", "runtime_fingerprint", "status", "valid",
            }
        } | {
            "configured": shadow_series_status["configured"],
            "valid": shadow_series_status["valid"],
            "status": shadow_series_status["status"],
        },
    ))

    external_disabled = compiled["deployment"]["external_actions_default"] == "disabled"
    checks.append(_check(
        "controls.external_actions",
        "pass" if external_disabled else "blocker",
        "付款、申报等外部动作默认禁用" if external_disabled else "外部动作未默认禁用",
        remediation="将外部动作默认状态恢复为 disabled。" if not external_disabled else "",
    ))
    missing_gate_owners = [
        task for task in compiled["setup_tasks"] if task["category"] == "control_owner"
    ]
    checks.append(_check(
        "controls.review_gate_owners",
        "warning" if missing_gate_owners else "pass",
        f"上线前需配置 {len(missing_gate_owners)} 个 review gate 的有权人",
        remediation="在身份与权限系统中配置主审、替补和职责分离。" if missing_gate_owners else "",
        details={"gate_task_ids": [task["task_id"] for task in missing_gate_owners]},
    ))

    executables = executable_probe or {
        executable: shutil.which(executable) is not None
        for executable in ("tesseract", "pdftoppm")
    }
    missing_ocr = sorted(name for name, available in executables.items() if not available)
    checks.append(_check(
        "optional.ocr",
        "warning" if missing_ocr else "pass",
        "扫描 PDF / 图片 OCR 可用" if not missing_ocr else f"可选 OCR 组件缺失：{', '.join(missing_ocr)}",
        remediation="需要扫描件识别时安装 Tesseract（chi_sim+eng）与 Poppler。" if missing_ocr else "",
        details={"executables": executables},
    ))

    counts = {
        status: sum(check["status"] == status for check in checks)
        for status in ("pass", "warning", "blocker")
    }
    return {
        "ready": counts["blocker"] == 0,
        "ready_for_internal_demo": counts["blocker"] == 0 and compiled["deployment"]["ready_for_internal_demo"],
        "ready_for_bounded_shadow": (
            counts["blocker"] == 0
            and pilot_status["ready_for_bounded_shadow"]
        ),
        "ready_for_controlled_data_intake": (
            counts["blocker"] == 0
            and handoff_status["ready_for_controlled_data_intake"]
        ),
        "ready_for_first_shadow_observation": (
            counts["blocker"] == 0
            and shadow_run_status["ready_for_first_shadow_observation"]
        ),
        "ready_for_next_shadow_period": (
            counts["blocker"] == 0
            and shadow_observation_status["ready_for_next_shadow_period"]
        ),
        "eligible_to_prepare_stable_promotion_evidence": (
            counts["blocker"] == 0
            and shadow_series_status[
                "eligible_to_prepare_stable_promotion_evidence"
            ]
        ),
        "ready_for_tax_calendar_release": (
            counts["blocker"] == 0
            and tax_lifecycle["calendar_release_allowed"]
            and applicability_complete
            and registry_activation_ready
            and not uncertain_registrations
        ),
        "ready_for_external_filing": (
            counts["blocker"] == 0 and compiled["deployment"]["ready_for_external_filing"]
            and tax_lifecycle["external_filing_release_allowed"]
            and applicability_complete and not uncertain_registrations
        ),
        "counts": counts,
        "checks": checks,
        "box": compiled["box"],
        "runtime_fingerprint": compiled["lock"]["runtime_fingerprint"],
    }
