from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Iterable


PERIOD_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
DOMAIN_SPECS = {
    "settlements": {
        "name": "收入结算", "required": True, "declarable": False,
        "document_types": {"settlement", "settlement_reconciliation_evidence"},
        "why": "正向现金流游戏公司必须能解释本期平台、渠道、流水、分成和应收。",
    },
    "bank_transactions": {
        "name": "银行与真实现金", "required": True, "declarable": False,
        "document_types": {"bank_statement", "bank_statement_document"},
        "why": "没有银行事实就不能验证回款、付款、期末现金或现金跑道。",
    },
    "opening_balances": {
        "name": "期初余额", "required": True, "declarable": False,
        "document_types": {"opening_balance"},
        "why": "没有上一期已确认余额，资产负债表和跨期结转都不可靠。",
    },
    "purchases": {
        "name": "采购与验收", "required": True, "declarable": True,
        "document_types": {"purchase", "acceptance_evidence", "contract_commercial"},
        "why": "本期采购、外包、云服务和投放承诺会影响费用、应付和现金。",
    },
    "invoices": {
        "name": "发票与费用票据", "required": True, "declarable": True,
        "document_types": {"invoice_register", "invoice_document"},
        "why": "国内主体需核对票据、税额和未开票暂估；海外主体可声明确实不适用。",
    },
    "payroll_rows": {
        "name": "人员薪酬", "required": True, "declarable": True,
        "document_types": {"payroll"},
        "why": "自有人员成本通常是游戏项目的重要投入；纯外包团队可声明本期不适用。",
    },
    "plan_lines": {
        "name": "预算与90天预测", "required": False, "declarable": True,
        "document_types": {"planning"},
        "why": "没有目标和未来现金假设，仍可核算历史，但无法形成可靠资源建议。",
    },
    "game_kpis": {
        "name": "经营KPI", "required": False, "declarable": True,
        "document_types": set(),
        "why": "没有活跃、付费、留存和投放驱动，只能解释财务结果，不能解释经营原因。",
    },
}


def _period(value: Any) -> str:
    if isinstance(value, (date, datetime)):
        return value.strftime("%Y-%m")
    text = str(value or "")
    match = re.search(r"(20\d{2})[-/.年]?(1[0-2]|0?[1-9])(?!\d)", text)
    return f"{match.group(1)}-{int(match.group(2)):02d}" if match else ""


def _record_period(dataset: str, row: dict) -> str:
    fields = {
        "settlements": ("period",), "bank_transactions": ("transaction_date",),
        "opening_balances": ("period",), "purchases": ("order_date", "period"),
        "invoices": ("invoice_date", "period"), "payroll_rows": ("period",),
        "plan_lines": ("period",), "game_kpis": ("period",),
    }[dataset]
    return next((_period(row.get(field)) for field in fields if _period(row.get(field))), "")


def _record_matches_period(dataset: str, row: dict, period: str) -> bool:
    if _record_period(dataset, row) == period:
        return True
    if dataset != "purchases":
        return False
    if _period(row.get("accepted_at")) == period:
        return True
    if any(_period(event.get("decided_at") or event.get("created_at")) == period for event in row.get("acceptance_history") or []):
        return True
    order_period = _period(row.get("order_date"))
    acceptance_open = str(row.get("acceptance_status") or "待验收") not in {"已验收", "已关闭", "已取消"}
    payment_open = str(row.get("payment_status") or "待确认") not in {"已付款", "已关闭", "已取消"}
    return bool(order_period and order_period <= period and (acceptance_open or payment_open))


def _latest_declarations(declarations: Iterable[dict], entity_id: str, period: str) -> dict[str, dict]:
    result = {}
    for row in declarations:
        if row.get("entity_id") != entity_id or row.get("period") != period:
            continue
        domain = str(row.get("domain") or "")
        if domain not in DOMAIN_SPECS:
            continue
        if domain not in result or str(row.get("updated_at") or "") >= str(result[domain].get("updated_at") or ""):
            result[domain] = row
    return result


def build_first_close_readiness(
    *, entity_id: str, period: str, entity: dict | None,
    profile_gaps: list[dict], datasets: dict[str, list[dict]],
    documents: Iterable[dict] = (), declarations: Iterable[dict] = (),
    shadow_reports: Iterable[dict] = (), master_records: Iterable[dict] = (),
) -> dict:
    entity_id = str(entity_id or "").strip()
    period = str(period or "").strip()
    if not entity_id:
        raise ValueError("首月上线检查必须选择法律主体")
    if not PERIOD_PATTERN.fullmatch(period):
        raise ValueError("首月上线检查期间必须为 YYYY-MM")
    entity = entity or {}
    latest_declarations = _latest_declarations(
        declarations or datasets.get("onboarding_declarations") or [], entity_id, period,
    )
    documents = list(documents)
    items = []
    for domain, spec in DOMAIN_SPECS.items():
        records = [
            row for row in datasets.get(domain) or []
            if str(row.get("entity_id") or "") == entity_id and _record_matches_period(domain, row, period)
        ]
        candidate_documents = []
        for document in documents:
            scope = document.get("entity_scope") or {}
            classification = document.get("classification") or {}
            if scope.get("status") != "confirmed" or scope.get("entity_id") != entity_id:
                continue
            if classification.get("document_type") not in spec["document_types"]:
                continue
            doc_periods = set(classification.get("periods") or [])
            recognition_period = str((document.get("recognition") or {}).get("period") or "")
            if period not in doc_periods and recognition_period != period:
                continue
            if document.get("status") != "已入台账":
                candidate_documents.append(document)
        declaration = latest_declarations.get(domain)
        declared_na = bool(declaration and declaration.get("decision") == "本期不适用")
        if records:
            status, evidence, next_action = "完成", f"{len(records)} 条正式台账记录", "继续处理异常与勾稽"
        elif declaration and declaration.get("decision") == "撤销不适用":
            status, evidence, next_action = "阻塞", f"不适用声明已由 {declaration.get('actor') or '复核人'} 撤销", "上传该主体/期间资料并完成台账提交"
        elif declared_na and spec["declarable"]:
            status, evidence, next_action = "不适用", f"由 {declaration.get('actor') or '复核人'} 声明：{declaration.get('rationale')}", "保留声明证据；事实变化时撤销"
        elif candidate_documents:
            status, evidence, next_action = "候选待处理", f"{len(candidate_documents)} 份同主体候选资料，尚未进入正式台账", "进入资料收件箱完成识别、复核和提交"
        elif spec["required"]:
            status, evidence, next_action = "阻塞", "没有该主体/期间的正式记录或候选资料", "上传资料并确认主体；确实无业务时按允许范围声明不适用"
        else:
            status, evidence, next_action = "建议补充", "本期尚未取得数据", "补充后可提升预测与经营解释能力"
        items.append({
            "domain": domain, "name": spec["name"], "status": status,
            "required": spec["required"], "can_declare_not_applicable": spec["declarable"],
            "record_count": len(records), "candidate_document_count": len(candidate_documents),
            "candidate_document_ids": [item.get("id") for item in candidate_documents],
            "why": spec["why"], "evidence": evidence, "next_action": next_action,
            "declaration": declaration,
        })

    entity_facts = [
        entity.get("id") or entity_id, entity.get("name") or entity.get("legal_name"),
        entity.get("jurisdiction"), entity.get("functional_currency"), entity.get("accounting_basis"),
    ]
    legal_gaps = list(profile_gaps) if entity_id == "cn_studio" else []
    entity_name = str(entity.get("name") or entity.get("legal_name") or "")
    if not entity_name or any(token in entity_name for token in ("演示", "示例", "待确认")):
        legal_gaps.append({"field": "legal_name", "label": "真实主体法定名称", "reason": "当前仍是演示或待确认主体"})
    legal_complete = all(entity_facts) and not legal_gaps
    master_rows = [row for row in master_records if row.get("status") == "可用"]
    games = [row for row in master_rows if row.get("record_type") == "game"]
    channels = [row for row in master_rows if row.get("record_type") == "channel"]
    master_complete = bool(games and channels)
    shadow = next((row for row in shadow_reports if row.get("entity_id") == entity_id and row.get("period") == period), None)
    shadow_status = (
        "完成" if shadow and not shadow.get("exception_count") and shadow.get("review_current")
        else "待签认" if shadow else "未开始"
    )
    blockers = [item["name"] for item in items if item["required"] and item["status"] not in {"完成", "不适用"}]
    if not legal_complete:
        blockers.insert(0, "主体与核算规则")
    if not master_complete:
        blockers.append("游戏与渠道主数据")
    completed_required = sum(item["status"] in {"完成", "不适用"} for item in items if item["required"])
    total_required = sum(item["required"] for item in items) + 2
    completed_required += int(legal_complete) + int(master_complete)
    return {
        "entity_id": entity_id, "entity_name": entity.get("name") or entity.get("legal_name") or entity_id,
        "period": period, "readiness_score": round(completed_required / total_required * 100),
        "ready_for_shadow_close": not blockers,
        "ready_for_statutory_release": not blockers and shadow_status == "完成" and entity.get("tax_readiness") == "filing_assist",
        "blockers": blockers, "items": items,
        "entity_profile": {
            "status": "完成" if legal_complete else "阻塞", "gaps": legal_gaps,
            "evidence": "Box 主体事实和公司财务档案已具备" if legal_complete else "主体或税务基础事实仍不完整",
        },
        "master_data": {
            "status": "完成" if master_complete else "阻塞",
            "games": len(games), "channels": len(channels),
        },
        "shadow_close": {
            "status": shadow_status,
            "exception_count": (shadow or {}).get("exception_count"),
            "review_current": bool((shadow or {}).get("review_current")),
        },
        "tax_readiness": {
            "status": entity.get("tax_readiness") or "未配置",
            "release_ready": entity.get("tax_readiness") == "filing_assist",
            "note": "税务包成熟度不因资料齐全自动升级；实际申报仍需有权人和回执。",
        },
        "control_note": "候选资料不等于正式台账；不适用声明必须由复核人说明依据；Shadow Close 通过前不释放首月法定结果。",
    }


def make_not_applicable_declaration(
    *, entity_id: str, period: str, domain: str, decision: str,
    actor: str, rationale: str, evidence: Iterable[str] = (), now: str,
) -> dict:
    if domain not in DOMAIN_SPECS or not DOMAIN_SPECS[domain]["declarable"]:
        raise ValueError("该上线域不能声明不适用")
    if decision not in {"本期不适用", "撤销不适用"}:
        raise ValueError("不适用决定无效")
    if not PERIOD_PATTERN.fullmatch(str(period or "")):
        raise ValueError("期间必须为 YYYY-MM")
    if len(str(rationale or "").strip()) < 8:
        raise ValueError("请用至少8个字说明本期为何不适用或为何撤销")
    evidence = [str(item).strip()[:300] for item in evidence if str(item).strip()]
    if decision == "本期不适用" and not evidence:
        raise ValueError("声明本期不适用必须提供至少一项证据引用")
    return {
        "id": f"ONBOARDING-{entity_id}-{period}-{domain}",
        "entity_id": entity_id, "period": period, "domain": domain,
        "domain_name": DOMAIN_SPECS[domain]["name"], "decision": decision,
        "actor": str(actor or "")[:80], "rationale": str(rationale).strip()[:1000],
        "evidence": evidence, "updated_at": now,
    }
