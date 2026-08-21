from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any, Iterable


OPEN_DOCUMENT_STATUSES = {
    "待确认类型", "已识别待确认", "等待文字识别", "已提取待归档",
    "已解析待入账", "未识别到可用记录",
}


def plan_safe_document_automations(
    documents: Iterable[dict], *, fallback_period: str | None = None,
) -> list[dict]:
    """Plan preview-only document recognition; never plan a ledger commit.

    A known document type can be parsed automatically when its period is
    unambiguous. If the period is ambiguous, the action is intentionally left
    to the confirmation queue instead of guessing.
    """
    actions = []
    for document in documents:
        if document.get("recognition") or document.get("status") not in OPEN_DOCUMENT_STATUSES:
            continue
        entity_scope = document.get("entity_scope") or {}
        # 多主体资料在确认归属前不能自动进入结构化识别。即使识别本身
        # 不写账，也会影响采购/结算匹配，因此必须先确定法定主体边界。
        if entity_scope.get("status") != "confirmed" or not entity_scope.get("entity_id"):
            continue
        classification = document.get("classification") or {}
        document_type = str(classification.get("document_type") or "unknown")
        capability = str(classification.get("capability") or "")
        confidence = _number(classification.get("confidence"))
        if document_type == "unknown":
            continue
        if confidence < 0.75 and capability not in {"等待OCR", "等待文字提取", "证据归档"}:
            continue
        periods = sorted({period for value in classification.get("periods") or [] if (period := _period_from(value))})
        if fallback_period and fallback_period in periods:
            period = fallback_period
        elif len(periods) == 1:
            period = periods[0]
        elif not periods and fallback_period:
            period = fallback_period
        else:
            continue
        actions.append({
            "id": f"recognize:{document.get('id')}",
            "document_id": document.get("id"),
            "action": "recognize_preview",
            "document_type": document_type,
            "period": period,
            "entity_id": entity_scope.get("entity_id"),
            "reason": "仅生成结构化预览和证据，不写入正式台账",
            "commit_allowed": False,
        })
    return actions


def _number(value: Any) -> float:
    try:
        return round(float(value or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def _period_from(value: Any) -> str:
    text = str(value or "")[:7]
    if len(text) == 7 and text[4] == "-" and text[:4].isdigit() and text[5:].isdigit():
        month = int(text[5:])
        return text if 1 <= month <= 12 else ""
    return ""


def latest_period(datasets: dict[str, list[dict]], fallback: str | None = None) -> str:
    periods = set()
    fields = {
        # 预算预测可以包含未来月份，不能把未来预测期误当成当前月结账期。
        "settlements": "period", "payroll_rows": "period",
        "opening_balances": "period", "game_kpis": "period", "cash_allocations": "period",
        "payment_requests": "period", "accruals": "period", "purchases": "order_date",
        "collection_actions": "action_date",
        "bank_transactions": "transaction_date", "invoices": "invoice_date",
        "expense_claims": "claim_date", "asset_cards": "acquisition_date",
        "procurement_requests": "period", "purchase_deliveries": "delivery_date",
        "vendor_bank_changes": "requested_at",
    }
    for dataset, field in fields.items():
        for row in datasets.get(dataset) or []:
            if period := _period_from(row.get(field)):
                periods.add(period)
    if periods:
        return max(periods)
    return fallback or datetime.now().strftime("%Y-%m")


def _amount(value: Any, currency: Any = "CNY") -> dict | None:
    numeric = _number(value)
    if numeric <= 0:
        return None
    return {"value": numeric, "currency": str(currency or "CNY").upper()}


def _priority(*, blocked: bool = False, exception: bool = False, amount: float = 0, status: str = "") -> tuple[str, int]:
    score = 0
    if blocked:
        score += 100
    if exception:
        score += 60
    if "退回" in status or "失败" in status:
        score += 80
    if amount >= 1_000_000:
        score += 55
    elif amount >= 100_000:
        score += 35
    elif amount >= 10_000:
        score += 20
    if "税" in status or "申报" in status:
        score += 10
    return ("紧急" if score >= 100 else "高" if score >= 55 else "中" if score >= 20 else "普通", score)


def _item(
    *, item_id: str, domain: str, source_type: str, source_id: str, title: str,
    status: str, reason: str, recommendation: str, required_role: str,
    amount: dict | None = None, blockers: Iterable[str] = (), evidence: Iterable[Any] = (),
    decision: dict | None = None, exception: bool = False, metadata: dict | None = None,
) -> dict:
    blockers = [str(value) for value in blockers if str(value).strip()]
    priority, risk_score = _priority(
        blocked=bool(blockers) or status == "阻塞",
        exception=exception,
        amount=(amount or {}).get("value", 0),
        status=f"{status}{title}",
    )
    return {
        "id": item_id,
        "domain": domain,
        "source_type": source_type,
        "source_id": source_id,
        "title": title,
        "status": status,
        "priority": priority,
        "risk_score": risk_score,
        "amount": amount,
        "reason": reason,
        "blockers": blockers,
        "recommendation": recommendation,
        "required_role": required_role,
        "evidence": list(evidence)[:20],
        "decision": decision,
        "metadata": metadata or {},
    }


def build_confirmation_queue(
    goal: dict,
    datasets: dict[str, list[dict]],
    finance: dict,
    tax_delivery: dict | None = None,
    inbox_documents: Iterable[dict] = (),
) -> dict:
    """Build one decision queue without pretending blocked facts are approvable.

    Amounts remain currency-tagged per item. Summary totals are grouped by currency,
    which is essential for lean teams operating high-volume international games.
    """
    items: list[dict] = []
    period = str(goal.get("period") or "")

    for row in datasets.get("vendor_bank_changes") or []:
        if row.get("status") not in {"待批准", "阻塞"}:
            continue
        blocked = row.get("status") == "阻塞"
        items.append(_item(
            item_id=f"vendor-bank:{row.get('id')}", domain="采购", source_type="vendor_bank_change",
            source_id=str(row.get("id")), title=f"供应商收款账户 · {row.get('vendor') or '待确认供应商'}",
            status=str(row.get("status")), amount=None,
            reason=(row.get("blockers") or ["收款账户新增或变更会影响后续付款去向"])[0],
            blockers=row.get("blockers") or [],
            recommendation="使用主数据中已知联系人回拨，或核对银行证明；不要只回复变更邮件。",
            required_role="申请人以外的财务或资金复核人", evidence=row.get("evidence") or [],
            decision=None if blocked else {
                "endpoint": "/api/vendor-bank-change-decision", "method": "POST",
                "id_fields": {"change_id": row.get("id"), "entity_id": row.get("entity_id")},
                "choices": ["批准", "退回", "取消"],
            }, exception=blocked,
            metadata={
                "account_masked": row.get("account_masked"), "bank_name": row.get("bank_name"),
                "currency": row.get("currency"), "change_type": row.get("change_type"),
            },
        ))

    for action in goal.get("actions") or []:
        if action.get("status") not in {"等待确认", "已批准待执行", "阻塞"}:
            continue
        support = action.get("decision_support") or {}
        decision = None if action.get("status") == "阻塞" else {
            "endpoint": "/api/agent-decision", "method": "POST",
            "id_fields": {"goal_id": goal.get("id"), "action_id": action.get("id")},
            "choices": ["同意", "退回", "暂缓"],
        }
        items.append(_item(
            item_id=f"goal:{goal.get('id')}:{action.get('id')}", domain="月结",
            source_type="goal_action", source_id=str(action.get("id")), title=str(action.get("title")),
            status=str(action.get("status")),
            reason=(action.get("blockers") or [support.get("business_impact") or "影响本期财务完整性"])[0],
            blockers=action.get("blockers") or [], recommendation=support.get("agent_recommendation") or "请复核",
            required_role=(action.get("automation") or {}).get("required_role") or action.get("owner") or "财务负责人",
            evidence=action.get("evidence") or [], decision=decision,
            exception=action.get("status") == "阻塞", metadata={"period": period},
        ))

    for row in datasets.get("payment_requests") or []:
        if row.get("status") not in {"待批准", "阻塞"}:
            continue
        blocked = row.get("status") == "阻塞"
        items.append(_item(
            item_id=f"payment:{row.get('id')}", domain="付款", source_type="payment_request",
            source_id=str(row.get("id")), title=f"付款申请 · {row.get('purpose') or row.get('target_type') or '待说明'}",
            status=str(row.get("status")), amount=_amount(row.get("amount"), row.get("currency")),
            reason=(row.get("blockers") or ["付款会直接影响公司资金安全和现金头寸"])[0],
            blockers=row.get("blockers") or [], recommendation=row.get("agent_recommendation") or "核对收款方、业务事实和付款依据",
            required_role="与申请人相互独立的有权审批人",
            evidence=row.get("evidence") or [],
            decision=None if blocked else {
                "endpoint": "/api/payment-decision", "method": "POST",
                "id_fields": {"request_id": row.get("id")}, "choices": ["批准", "退回", "取消"],
            }, exception=blocked, metadata={"prepayment": bool(row.get("prepayment"))},
        ))

    for row in datasets.get("procurement_requests") or []:
        if row.get("status") not in {"待批准", "阻塞"}:
            continue
        blocked = row.get("status") == "阻塞"
        items.append(_item(
            item_id=f"procurement:{row.get('id')}", domain="采购", source_type="procurement_request",
            source_id=str(row.get("id")), title=f"采购申请 · {row.get('description') or row.get('category')}",
            status=str(row.get("status")), amount=_amount(row.get("amount"), row.get("currency")),
            reason=(row.get("blockers") or ["下单会占用预算并形成未来现金承诺"])[0],
            blockers=row.get("blockers") or [], recommendation=row.get("agent_recommendation") or "复核必要性、预算与寻源依据",
            required_role="与申请人相互独立的采购或财务审批人", evidence=row.get("evidence") or [],
            decision=None if blocked else {
                "endpoint": "/api/procurement-request-decision", "method": "POST",
                "id_fields": {"request_id": row.get("id"), "entity_id": row.get("entity_id")},
                "choices": ["批准", "退回", "取消"],
            }, exception=blocked,
            metadata={
                "sourcing_method": row.get("sourcing_method"),
                "selected_vendor": row.get("selected_vendor"),
                "warnings": row.get("warnings") or [],
                "budget_snapshot": row.get("budget_snapshot") or {},
            },
        ))

    for row in datasets.get("expense_claims") or []:
        if row.get("status") not in {"待审批", "阻塞"}:
            continue
        blocked = row.get("status") == "阻塞"
        items.append(_item(
            item_id=f"expense:{row.get('id')}", domain="费用", source_type="expense_claim",
            source_id=str(row.get("id")), title=f"费用报销 · {row.get('claimant') or '待识别报销人'}",
            status=str(row.get("status")), amount=_amount(row.get("amount"), row.get("currency")),
            reason=(row.get("blockers") or [f"归属项目：{row.get('project') or '待确认'}"])[0],
            blockers=row.get("blockers") or [], recommendation="核对业务用途、项目归属、票据和付款证明后决定",
            required_role="报销人直属负责人或费用审批人", evidence=row.get("evidence") or [],
            decision=None if blocked else {
                "endpoint": "/api/expense-decision", "method": "POST",
                "id_fields": {"claim_id": row.get("id")}, "choices": ["批准", "退回"],
            }, exception=blocked,
        ))

    milestone_purchase_ids = {
        str(row.get("purchase_id")) for row in datasets.get("purchase_deliveries") or []
        if row.get("purchase_id")
    }
    for row in datasets.get("purchase_deliveries") or []:
        if row.get("status") != "已交付待验收":
            continue
        items.append(_item(
            item_id=f"delivery-acceptance:{row.get('id')}", domain="采购验收",
            source_type="purchase_acceptance", source_id=str(row.get("id")),
            title=f"验收 · {row.get('milestone_title') or row.get('po_number') or '采购交付'}",
            status="已交付待验收", amount=_amount(row.get("delivered_amount"), row.get("currency")),
            reason="只有对真实交付事件验收后，成本、发票匹配和付款额度才会释放",
            blockers=[], recommendation=f"按“{row.get('acceptance_criteria') or '订单约定标准'}”核对交付物",
            required_role=str(row.get("acceptance_owner") or "实际接收交付的业务负责人"),
            evidence=row.get("evidence") or [], decision={
                "endpoint": "/api/purchase-acceptance", "method": "POST",
                "id_fields": {
                    "purchase_id": row.get("purchase_id"), "delivery_id": row.get("id"),
                    "entity_id": row.get("entity_id"), "period": period,
                },
                "choices": ["全部验收", "部分验收", "退回整改"],
            }, metadata={
                "vendor": row.get("vendor"), "po_number": row.get("po_number"),
                "milestone_id": row.get("milestone_id"), "delivery_id": row.get("id"),
                "acceptance_criteria": row.get("acceptance_criteria"),
            },
        ))

    for row in datasets.get("purchases") or []:
        if str(row.get("id")) in milestone_purchase_ids or row.get("milestones"):
            continue
        acceptance = row.get("acceptance_status") or ("已验收" if _number(row.get("accepted_amount")) else "待验收")
        if acceptance in {"已验收", "部分验收", "已退回整改", "部分验收，剩余退回整改"}:
            continue
        amount = _amount(row.get("ordered_amount"), row.get("currency"))
        items.append(_item(
            item_id=f"acceptance:{row.get('id')}", domain="采购验收", source_type="purchase_acceptance",
            source_id=str(row.get("id")), title=f"验收 · {row.get('item') or row.get('vendor') or row.get('po_number') or '采购交付'}",
            status=str(acceptance), amount=amount,
            reason="验收决定会控制成本确认、应付形成及是否允许付款",
            blockers=[], recommendation="由实际使用或接收成果的业务负责人核验数量、质量和完成进度",
            required_role="实际接收交付的业务负责人", evidence=row.get("acceptance_evidence") or [],
            decision={
                "endpoint": "/api/purchase-acceptance", "method": "POST",
                "id_fields": {"purchase_id": row.get("id"), "period": period},
                "choices": ["全部验收", "部分验收", "退回整改"],
            }, exception=bool(row.get("anomalies")), metadata={
                "vendor": row.get("vendor"), "po_number": row.get("po_number"),
                "warnings": row.get("anomalies") or [],
            },
        ))

    for dataset_name, domain, source_type in (
        ("asset_cards", "会计", "asset_card"), ("accruals", "会计", "accrual"),
        ("game_revenue_policies", "收入会计", "revenue_policy"),
    ):
        for row in datasets.get(dataset_name) or []:
            if row.get("status") not in {"待会计复核", "阻塞"}:
                continue
            blocked = row.get("status") == "阻塞"
            judgement = row.get("agent_judgement") or {}
            if dataset_name == "asset_cards":
                title, amount, endpoint, id_key = (
                    f"资产处理 · {row.get('name')}",
                    _amount(row.get("functional_cost") or row.get("cny_cost"), row.get("functional_currency") or "CNY"),
                    "/api/asset-review", "item_id",
                )
            elif dataset_name == "accruals":
                title, amount, endpoint, id_key = (
                    f"费用暂估 · {row.get('description')}",
                    _amount(row.get("functional_amount") or row.get("amount"), row.get("functional_currency") or "CNY"),
                    "/api/accrual-review", "item_id",
                )
            else:
                title, amount, endpoint, id_key = f"收入政策 · {row.get('game')} / {row.get('channel')}", None, "/api/game-revenue-policy-review", "policy_id"
            items.append(_item(
                item_id=f"{source_type}:{row.get('id')}", domain=domain, source_type=source_type,
                source_id=str(row.get("id")), title=title, status=str(row.get("status")), amount=amount,
                reason=(row.get("blockers") or [judgement.get("impact") or "会影响收入、成本、资产或期间利润"])[0],
                blockers=row.get("blockers") or [], recommendation=judgement.get("recommendation") or "按证据和会计政策复核",
                required_role="会计服务机构或财务负责人", evidence=row.get("evidence") or [],
                decision=None if blocked else {
                    "endpoint": endpoint, "method": "POST",
                    "id_fields": {id_key: row.get("id"), "entity_id": row.get("entity_id")},
                    "choices": ["批准", "退回"],
                }, exception=blocked,
            ))

    reviews = (datasets.get("tax_filing_reviews") or [])
    for form in (tax_delivery or {}).get("forms") or []:
        if form.get("review_status") == "已复核":
            continue
        blocked = bool(form.get("blocker_count"))
        items.append(_item(
            item_id=f"tax:{period}:{form.get('form_code')}", domain="税务", source_type="tax_form",
            source_id=str(form.get("form_code")), title=f"税务表单 · {form.get('name')}",
            status="阻塞" if blocked else str(form.get("review_status") or "未复核"),
            reason=("申报工作底稿仍有资料或口径阻塞项" if blocked else "Agent 已形成候选申报底稿，需有权人员复核口径"),
            blockers=[f"仍有 {form.get('blocker_count')} 个阻塞项"] if blocked else [],
            recommendation=form.get("agent_position") or "逐字段追溯来源并核对税会口径",
            required_role=form.get("review_role") or "税务服务机构或有权申报人", evidence=[],
            decision=None if blocked else {
                "endpoint": "/api/tax-form-review", "method": "POST",
                "id_fields": {"period": period, "form_code": form.get("form_code")},
                "choices": ["同意草稿", "退回修改", "确认不适用"],
            }, exception=blocked,
        ))

    voucher_reviews = (finance.get("period_state") or {}).get("voucher_reviews") or {}
    for voucher in finance.get("vouchers") or []:
        voucher_id = str(voucher.get("id") or "")
        if not voucher_id or voucher_id in voucher_reviews or voucher.get("status") == "阻塞":
            continue
        amount_value = max(
            sum(_number(line.get("debit")) for line in voucher.get("lines") or []),
            sum(_number(line.get("credit")) for line in voucher.get("lines") or []),
        )
        items.append(_item(
            item_id=f"voucher:{voucher_id}", domain="凭证", source_type="voucher",
            source_id=voucher_id, title=f"凭证复核 · {voucher.get('summary') or voucher_id}",
            status="待复核", amount=_amount(amount_value, "CNY"),
            reason="凭证复核决定会影响总账、报表和税务工作底稿",
            recommendation="核对业务事实、科目、期间、总额净额口径和借贷平衡",
            required_role=voucher.get("review_role") or "会计服务机构", evidence=voucher.get("source_evidence") or [],
            decision={
                "endpoint": "/api/voucher-review", "method": "POST",
                "id_fields": {"period": period, "voucher_id": voucher_id},
                "choices": ["接受", "退回", "忽略", "冲销"],
            }, exception=False,
        ))

    for document in inbox_documents:
        if document.get("status") not in OPEN_DOCUMENT_STATUSES:
            continue
        classification = document.get("classification") or {}
        document_type = str(classification.get("document_type") or "")
        recognition = document.get("recognition") or {}
        corrections = recognition.get("corrections") or []
        original_confirmed = any(item.get("confirmed_against_original") for item in corrections)
        blockers = []
        if document_type in {"invoice_document", "bank_statement_document"} and recognition and not original_confirmed:
            blockers.append("识别候选尚未由人工对照原件确认")
        if document_type in {"acceptance_evidence", "contract_commercial", "settlement_reconciliation_evidence"} and recognition and not document.get("business_links"):
            blockers.append("证据尚未关联到同一法律主体的采购/验收记录")
        if not (document.get("entity_scope") or {}).get("entity_id"):
            blockers.append("资料所属法律主体尚未确认")
        recommendation = "进入资料收件箱核对类型、主体和识别候选"
        if document_type in {"invoice_document", "bank_statement_document"} and recognition:
            recommendation = "进入资料收件箱逐项补正，并对照原件确认后再提交入台账"
        elif document_type in {"acceptance_evidence", "contract_commercial"} and recognition:
            recommendation = "把证据关联到采购/验收记录；证据本身不会自动形成验收结论"
        elif document_type == "settlement_reconciliation_evidence" and recognition:
            recommendation = "把核对底稿关联到同主体收入结算；底稿不会自动改写收入金额"
        items.append(_item(
            item_id=f"document:{document.get('id')}", domain="资料", source_type="inbox_document",
            source_id=str(document.get("id")), title=f"资料确认 · {document.get('original_filename')}",
            status="阻塞" if blockers else str(document.get("status")),
            reason=f"Agent 识别为{classification.get('label') or '待识别资料'}，置信度 {round(_number(classification.get('confidence')) * 100)}%",
            blockers=blockers, recommendation=recommendation,
            required_role="资料提供人或财务负责人", evidence=[document.get("sha256")],
            decision={
                "navigation_view": "documents", "method": "NAVIGATE",
                "id_fields": {"id": document.get("id")}, "choices": ["打开资料"],
            }, exception=classification.get("confidence", 0) < 0.75 or bool(blockers),
        ))

    items.sort(key=lambda value: (-value["risk_score"], -(value.get("amount") or {}).get("value", 0), value["title"]))
    totals: dict[str, float] = defaultdict(float)
    for item in items:
        if item.get("amount"):
            totals[item["amount"]["currency"]] += item["amount"]["value"]
    return {
        "items": items,
        "count": len(items),
        "blocked_count": sum(bool(item["blockers"]) or item["status"] == "阻塞" for item in items),
        "decision_ready_count": sum(bool(item.get("decision")) and item["status"] != "阻塞" for item in items),
        "amount_exposure_by_currency": [
            {"currency": currency, "value": round(value, 2)} for currency, value in sorted(totals.items())
        ],
        "control_note": "金额按币种分别展示，不直接相加；高流水不降低审批与职责分离要求。",
    }


def build_deliverable_register(goal: dict, finance: dict, tax_delivery: dict | None = None) -> dict:
    items = []
    for action in goal.get("actions") or []:
        for artifact in action.get("artifacts") or []:
            items.append({
                **artifact,
                "id": f"{action.get('id')}:{artifact.get('name')}",
                "action_id": action.get("id"),
                "action": action.get("title"),
                "owner": action.get("owner"),
                "period": goal.get("period"),
                "decision": action.get("latest_decision"),
                "blockers": action.get("blockers") or [],
            })
    if tax_delivery:
        items.append({
            "id": f"tax-delivery:{goal.get('period')}", "name": "税务申报交付包",
            "reference": f"api:/api/tax-delivery?period={goal.get('period')}",
            "status": "已完成" if tax_delivery.get("complete") else "草稿待复核",
            "evidence_state": "已取得申报结果" if tax_delivery.get("complete") else "不得视为已申报",
            "period": goal.get("period"), "action_id": "C14", "owner": "有权申报人",
            "decision": None, "blockers": [],
        })
    return {
        "items": items,
        "complete_count": sum(item.get("status") == "已完成" for item in items),
        "generated_count": sum(item.get("status") in {"已生成", "已完成"} for item in items),
        "draft_count": sum(item.get("status") != "已完成" for item in items),
        "evidence_note": "交付物必须同时保留来源、状态和人工决定；生成文件不等于已经复核、过账或申报。",
    }
