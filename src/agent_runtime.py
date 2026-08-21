from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PERIOD_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
VALID_GOAL_STATUSES = {"执行中", "等待确认", "阻塞", "已完成", "已取消"}
VALID_DECISIONS = {"同意", "退回", "暂缓"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _goal_id() -> str:
    return f"GOAL-{uuid.uuid4().hex[:12].upper()}"


def _event_id(goal_id: str, event_type: str) -> str:
    value = f"{goal_id}|{event_type}|{_now()}|{uuid.uuid4().hex}"
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:16]


def _validate_period(period: str) -> str:
    value = str(period or "").strip()
    if not PERIOD_PATTERN.fullmatch(value):
        raise ValueError("目标账期必须为 YYYY-MM")
    return value


def _approval_for(goal: dict, action_id: str) -> dict | None:
    return next(
        (item for item in reversed(goal.get("decisions") or []) if item.get("action_id") == action_id),
        None,
    )


def _action_policy(task: dict, context: dict | None = None) -> dict:
    context = context or {}
    task_id = task.get("id")
    if task_id == "S01":
        return {
            "level": "自动执行",
            "reason": "只同步 Shadow close 独立签认状态，不代替复核人作出签认。",
            "required_role": "独立复核人",
        }
    if task_id == "C15":
        return {
            "level": "仅授权人执行",
            "reason": "关账会冻结期间，后续修改必须走有审计记录的重开流程。",
            "required_role": "公司负责人或其授权人",
        }
    if task_id == "C10" and not context.get("has_foreign_currency"):
        return {
            "level": "自动执行",
            "reason": "本期没有外币余额或外币交易，无需发起折算口径确认。",
            "required_role": "Agent",
        }
    if task_id in {"C05", "C10", "C11", "C14"}:
        return {
            "level": "需确认",
            "reason": {
                "C05": "验收事实和成本归属需要业务负责人确认。",
                "C10": "汇率来源及外币期末计量口径需要财务确认。",
                "C11": "科目、总额净额口径及凭证过账需要会计复核。",
                "C14": "税务申报口径和最终提交需要税务服务机构或有权人员复核。",
            }[task_id],
            "required_role": task.get("owner") or "财务负责人",
        }
    return {
        "level": "自动执行",
        "reason": "Agent 可依据结构化业务事实和既定规则执行，并保留来源与计算记录。",
        "required_role": "Agent",
    }


def _action_artifacts(task_id: str, period: str, action_status: str) -> list[dict]:
    definitions = {
        "C02": ("渠道结算标准明细", "dataset:settlements"),
        "C03": ("收入结算勾稽结果", f"api:/api/finance-ops?period={period}"),
        "C04": ("发票归集与异常清单", "dataset:invoices"),
        "C05": ("采购、验收与应付台账", "dataset:purchases"),
        "C06": ("工资与个税计算底稿", "dataset:payroll_rows"),
        "C07": ("银行流水认领与余额调节", "dataset:bank_transactions"),
        "C08": ("应收应付勾稽结果", f"api:/api/finance-ops?period={period}"),
        "C10": ("外币折算底稿", f"api:/api/finance-ops?period={period}"),
        "C11": ("记账凭证草稿", f"api:/api/finance-ops?period={period}"),
        "C13": ("财务报表草稿", f"api:/api/finance-ops?period={period}"),
        "C14": ("税务申报工作底稿", f"api:/api/tax-return-workbook?period={period}"),
        "C15": ("已冻结期间", f"period:{period}"),
        "P01": ("90天资金安全预测", f"api:/api/planning?period={period}"),
        "A01": ("经营变化解释与管理建议", f"api:/api/bp?period={period}"),
        "S01": ("Shadow close 只读对比与签认", f"view:/shadow-close?period={period}"),
        "F01": ("首月上线资料覆盖检查", f"view:/onboarding?period={period}"),
    }
    if task_id not in definitions:
        return []
    name, reference = definitions[task_id]
    artifact_status = {
        "已完成": "已生成",
        "等待确认": "草稿待确认",
        "已批准待执行": "已批准待执行",
    }.get(action_status, "生成中")
    if task_id == "C15" and action_status == "已完成":
        artifact_status = "已完成"
    scope_notes = {
        "C06": "内部工资与个税计算已生成，仍需按扣缴申报链路复核。",
        "C11": "凭证草稿只有在复核并过账后才进入正式总账。",
        "C13": "内部财务报表草稿，需完成关账控制后才能作为定稿版本。",
        "C14": "税务工作底稿不等于已申报，申报结果以税务端回执为准。",
        "P01": "预测用于资金决策，不是银行账户余额或融资承诺。",
        "A01": "管理分析基于当前数据覆盖和已配置汇率，缺失数据会单独披露。",
        "S01": "只读验证不覆盖任一主体的台账、凭证、税表、银行或审批记录。",
    }
    return [{
        "name": name,
        "reference": reference,
        "status": artifact_status,
        "evidence_state": (
            "期间已冻结" if task_id == "C15" and action_status == "已完成"
            else "来源与计算已记录" if action_status == "已完成"
            else "待人工确认"
        ),
        "scope_note": scope_notes.get(task_id, "来源、计算过程和当前状态均保留可追溯记录。"),
    }]


def _business_impact(task: dict) -> str:
    impacts = {
        "收入": "影响收入完整性、渠道分成、应收账款及项目利润。",
        "成本费用": "影响项目成本、应付款和本期利润，遗漏会使利润虚高。",
        "资金": "影响真实现金余额、收付款认领及资金安全。",
        "薪酬": "影响人员成本、应付职工薪酬及个税底稿。",
        "外币": "影响外币资产负债的人民币价值及汇兑损益。",
        "税务": "影响申报底稿和税会差异；最终申报仍由有权人员确认。",
        "关账": "确认本期结果并冻结账期，是不可静默执行的关键控制点。",
        "经营分析": "影响对收入、获客、留存、项目回报和资源投入的管理判断。",
    }
    return impacts.get(task.get("stream"), "影响本期财务数据的完整性、准确性和可追溯性。")


def _derive_action(
    task: dict, period: str, goal: dict, period_state: dict, context: dict | None = None,
) -> dict:
    policy = _action_policy(task, context)
    decision = _approval_for(goal, task["id"])
    status = {
        "已完成": "已完成",
        "阻塞": "阻塞",
        "待处理": "可执行",
    }.get(task.get("status"), "待处理")

    if task["id"] == "C15" and period_state.get("status") == "已关账":
        status = "已完成"
    elif status == "可执行" and policy["level"] != "自动执行":
        status = "等待确认"
    elif task.get("status") == "已完成" and policy["level"] != "自动执行" and not decision:
        # 数据条件已满足不等于职业判断或授权已经完成。
        status = "等待确认"

    if decision:
        if decision.get("decision") == "同意" and status == "等待确认":
            status = "已批准待执行"
        elif decision.get("decision") == "退回":
            status = "阻塞"
        elif decision.get("decision") == "暂缓":
            status = "已暂缓"

    guidance = task.get("decision_support") or {}
    blockers = list(task.get("blockers") or [])
    if decision and decision.get("decision") == "退回" and decision.get("rationale"):
        blockers.append(f"人工退回：{decision['rationale']}")
    return {
        "id": task["id"],
        "title": task["name"],
        "stream": task.get("stream"),
        "owner": task.get("owner"),
        "status": status,
        "deadline": task.get("deadline"),
        "automation": policy,
        "blockers": blockers,
        "evidence_required": bool(task.get("evidence_required")),
        "evidence": [],
        "artifacts": _action_artifacts(task["id"], period, status) if status in {"已完成", "等待确认", "已批准待执行"} else [],
        "decision_support": {
            "plain_language": guidance.get("plain_language") or task["name"],
            "agent_recommendation": guidance.get("recommendation") or (
                "先补齐缺失的业务事实和原始证据，再继续执行；不建议凭金额或经验猜测。"
                if blockers else "按既定规则继续执行，并对异常项单独复核。"
            ),
            "confidence": guidance.get("confidence"),
            "business_impact": _business_impact(task),
            "questions": guidance.get("business_questions") or [],
            "options": guidance.get("options") or [],
        },
        "latest_decision": decision,
    }


def build_goal_snapshot(
    goal: dict,
    finance: dict,
    onboarding: dict,
    period_state: dict,
    business_flows: dict | None = None,
    planning: dict | None = None,
    analysis: dict | None = None,
    shadow_close_reports: list[dict] | None = None,
) -> dict:
    """用当前财务事实刷新目标；不把旧快照当成真实状态。"""
    period = _validate_period(goal.get("period"))
    context = {
        "has_foreign_currency": any(
            (voucher.get("original_currency") or "CNY") != "CNY"
            for voucher in finance.get("vouchers") or []
        ),
    }
    tasks = list(finance["close"]["tasks"])
    first_close = onboarding.get("first_close") or {}
    if first_close:
        first_close_blockers = list(first_close.get("blockers") or [])
        tasks.append({
            "id": "F01", "name": "完成首月上线资料覆盖门", "stream": "关账", "owner": "财务负责人",
            "status": "阻塞" if first_close_blockers else "已完成",
            "blockers": first_close_blockers, "deadline": "首月 Shadow Close 前",
            "evidence_required": True,
            "decision_support": {
                "plain_language": "确认每个必需财务域已有同主体正式台账，或有依据地声明本期不适用",
                "recommendation": (
                    "进入首次上线页，逐项处理候选资料或补充缺失数据；候选文件不能当作已入台账。"
                    if first_close_blockers else "首月必需资料覆盖已满足，可以进入 Shadow Close；这不代表法定申报已经完成。"
                ),
            },
        })
    if planning is not None:
        forecast = planning.get("forecast") or []
        planning_blockers = []
        if len(forecast) < 3:
            planning_blockers.append("未来90天现金流预测不足3个月")
        if planning.get("opening_cash_cny") in {None, 0}:
            planning_blockers.append("缺少经核对的期初现金")
        tasks.append({
            "id": "P01", "name": "更新90天资金安全预测", "stream": "资金", "owner": "Agent",
            "status": "阻塞" if planning_blockers else "已完成", "blockers": planning_blockers,
            "deadline": "持续滚动", "evidence_required": False,
            "decision_support": {
                "plain_language": "未来90天是否会跌破最低现金安全垫",
                "recommendation": (
                    f"预计在 {planning.get('buffer_breach_period')} 跌破安全垫，优先收回应收并延后非关键支出。"
                    if planning.get("buffer_breach_period") else "当前基准情景未触发安全垫预警；继续维护保守情景。"
                ),
            },
        })
    if business_flows is not None:
        receivables = business_flows.get("receivables") or {}
        missed_promises = int(receivables.get("missed_promise_count") or 0)
        disputed = int(receivables.get("disputed_count") or 0)
        overdue = int(receivables.get("overdue_count") or 0)
        collection_blockers = []
        if missed_promises:
            collection_blockers.append(f"{missed_promises}笔承诺回款已过期仍未结清")
        if disputed:
            collection_blockers.append(f"{disputed}笔应收仍在争议处理中")
        tasks.append({
            "id": "R01", "name": "推进逾期应收与回款承诺", "stream": "资金", "owner": "渠道运营负责人",
            "status": "阻塞" if collection_blockers else "待处理" if overdue else "已完成",
            "blockers": collection_blockers, "deadline": "持续滚动", "evidence_required": bool(overdue),
            "decision_support": {
                "plain_language": "真实逾期是否已有负责人、联系事实、争议原因和下一承诺日",
                "recommendation": (
                    "先更新失效承诺或解决争议；新记录追加留痕，并刷新现金情景，不覆盖原应收。"
                    if collection_blockers else "按渠道账期持续跟进；银行到账后用核销关闭余额。" if overdue
                    else "当前没有逾期应收，继续维护渠道账期和银行核销。"
                ),
            },
        })
    if analysis is not None:
        insight_rows = analysis.get("proactive_insights") or []
        analysis_blockers = [] if analysis.get("totals") else ["缺少可用于经营分析的收入与成本事实"]
        tasks.append({
            "id": "A01", "name": "解释经营变化并生成管理建议", "stream": "经营分析", "owner": "Agent",
            "status": "阻塞" if analysis_blockers else "已完成", "blockers": analysis_blockers,
            "deadline": "持续滚动", "evidence_required": False,
            "decision_support": {
                "plain_language": "本期为什么变化、需要关注什么、下一步做什么",
                "recommendation": (
                    str((insight_rows[0] if insight_rows else {}).get("recommendation") or "结合收入、项目投入、获客与留存变化形成管理建议。")
                ),
            },
        })
    if shadow_close_reports:
        unresolved = [item for item in shadow_close_reports if item.get("exception_count") or not item.get("review_current")]
        detail = []
        for item in unresolved:
            if item.get("exception_count"):
                detail.append(f"{item.get('entity_id')}有{item.get('exception_count')}项差异或缺项")
            elif not item.get("review_current"):
                detail.append(f"{item.get('entity_id')}尚未完成当前指纹的独立签认")
        tasks.append({
            "id": "S01", "name": f"验证{len(shadow_close_reports)}个主体的关账结果",
            "stream": "关账", "owner": "独立复核人",
            "status": "阻塞" if unresolved else "已完成", "blockers": detail,
            "deadline": "定稿前", "evidence_required": True,
            "decision_support": {
                "plain_language": "用人工已复核关账包独立验证 Agent 的总账、报表和税务候选值",
                "recommendation": "进入 Shadow close 逐主体解释差异并签认；不将人工数反向覆盖 Agent。" if unresolved else "所有已导入基准均已完成当前指纹的独立签认。",
            },
        })
    actions = [
        _derive_action(task, period, goal, period_state, context)
        for task in tasks
    ]
    completed = sum(action["status"] == "已完成" for action in actions)
    waiting = [action for action in actions if action["status"] in {"等待确认", "已批准待执行"}]
    blocked = [action for action in actions if action["status"] == "阻塞"]
    executable = [action for action in actions if action["status"] == "可执行"]

    if period_state.get("status") == "已关账":
        status = "已完成"
    elif blocked:
        status = "阻塞"
    elif waiting:
        status = "等待确认"
    else:
        status = "执行中"

    # 先暴露真正阻塞整套财务的事实，再处理可批准或可自动继续的事项。
    next_action = (blocked or waiting or executable or [None])[0]
    scale = {
        "settlement_records": finance.get("data_coverage", {}).get("settlement_records", 0),
        "purchase_records": finance.get("data_coverage", {}).get("purchase_records", 0),
        "bank_transactions": finance.get("data_coverage", {}).get("bank_transactions", 0),
        "invoice_records": finance.get("data_coverage", {}).get("invoices", 0),
        "payroll_records": finance.get("data_coverage", {}).get("payroll_records", 0),
    }
    business_flows = business_flows or {}
    goal.update({
        "status": status,
        "updated_at": _now(),
        "progress": {
            "completed": completed,
            "total": len(actions),
            "ratio": round(completed / len(actions), 4) if actions else 0,
            "waiting_confirmation": len(waiting),
            "blocked": len(blocked),
            "executable": len(executable),
        },
        "actions": actions,
        "next_focus": ({
            "action_id": next_action["id"],
            "title": next_action["title"],
            "status": next_action["status"],
            "why": (next_action.get("blockers") or [next_action["decision_support"]["business_impact"]])[0],
            "agent_recommendation": next_action["decision_support"]["agent_recommendation"],
        } if next_action else None),
        "readiness": {
            "onboarding_score": onboarding.get("readiness_score"),
            "onboarding_blockers": onboarding.get("blockers") or [],
            "data_volume": scale,
            "scale_note": "组织人数不作为交易量或营收能力上限；明细处理与汇总审批分层。",
        },
        "deliverables": [
            artifact
            for action in actions for artifact in action.get("artifacts") or []
            if action["status"] in {"已完成", "等待确认", "已批准待执行"}
        ],
        "operating_alerts": business_flows.get("alerts") or [],
        "business_flow_status": {
            "overdue_receivables": (business_flows.get("receivables") or {}).get("overdue_count", 0),
            "missed_collection_promises": (business_flows.get("receivables") or {}).get("missed_promise_count", 0),
            "disputed_receivables": (business_flows.get("receivables") or {}).get("disputed_count", 0),
            "pending_payments": (business_flows.get("payables") or {}).get("pending_payment_count", 0),
            "bank_unallocated": business_flows.get("bank_unallocated_count", 0),
            "pending_approvals": (
                (business_flows.get("payment_requests") or {}).get("pending_approval", 0)
                + (business_flows.get("expense_claims") or {}).get("pending_approval", 0)
            ),
        },
    })
    return goal


class AgentRuntimeStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.goals = self.root / "goals"
        self.events_file = self.root / "events.jsonl"
        self._lock = threading.RLock()

    def create(
        self,
        objective: str,
        period: str,
        actor: str = "财务工作台用户",
        *,
        data_mode: str = "live",
        origin: str = "user",
        demo_scenario: str = "domestic",
    ) -> dict:
        objective = str(objective or "").strip()
        if len(objective) < 4:
            raise ValueError("请用一句话说明要完成的财务目标")
        period = _validate_period(period)
        if data_mode not in {"live", "demo"}:
            raise ValueError("目标数据模式只能是 live 或 demo")
        if demo_scenario not in {"group", "domestic", "overseas"}:
            raise ValueError("演示场景只能是 group、domestic 或 overseas")
        now = _now()
        goal = {
            "id": _goal_id(),
            "type": "完成月度财务",
            "objective": objective[:500],
            "period": period,
            "status": "执行中",
            "created_by": str(actor or "财务工作台用户")[:80],
            "data_mode": data_mode,
            "origin": str(origin or "user")[:40],
            "demo_scenario": demo_scenario if data_mode == "demo" else None,
            "created_at": now,
            "updated_at": now,
            "actions": [],
            "decisions": [],
            "deliverables": [],
        }
        with self._lock:
            _atomic_write(self.goals / f"{goal['id']}.json", goal)
            self.append_event(goal["id"], "GOAL_CREATED", actor, {
                "objective": objective, "period": period, "data_mode": data_mode, "origin": origin,
                "demo_scenario": demo_scenario if data_mode == "demo" else None,
            })
        return goal

    def save(self, goal: dict) -> dict:
        goal_id = str(goal.get("id") or "")
        if not re.fullmatch(r"GOAL-[A-F0-9]{12}", goal_id):
            raise ValueError("目标编号无效")
        if goal.get("status") not in VALID_GOAL_STATUSES:
            raise ValueError("目标状态无效")
        goal["updated_at"] = _now()
        with self._lock:
            _atomic_write(self.goals / f"{goal_id}.json", goal)
        return goal

    def load(self, goal_id: str) -> dict:
        goal_id = str(goal_id or "").strip()
        if not re.fullmatch(r"GOAL-[A-F0-9]{12}", goal_id):
            raise ValueError("目标编号无效")
        path = self.goals / f"{goal_id}.json"
        if not path.exists():
            raise ValueError("目标不存在")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError("目标记录无法读取") from error
        return payload

    def list(self, limit: int = 100) -> list[dict]:
        if not self.goals.exists():
            return []
        items = []
        for path in self.goals.glob("GOAL-*.json"):
            try:
                items.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                continue
        items.sort(key=lambda item: item.get("updated_at") or "", reverse=True)
        return items[:max(1, min(int(limit), 500))]

    def decide(
        self,
        goal_id: str,
        action_id: str,
        decision: str,
        actor: str,
        rationale: str,
        evidence: list[str] | None = None,
    ) -> dict:
        if decision not in VALID_DECISIONS:
            raise ValueError("决定只能是同意、退回或暂缓")
        actor = str(actor or "").strip()
        if not actor:
            raise ValueError("请填写作出决定的人")
        rationale = str(rationale or "").strip()
        if len(rationale) < 4:
            raise ValueError("请简要说明决定依据")
        goal = self.load(goal_id)
        action = next((item for item in goal.get("actions") or [] if item.get("id") == action_id), None)
        if not action:
            raise ValueError("目标中不存在该动作")
        if action.get("automation", {}).get("level") == "自动执行":
            raise ValueError("自动动作无需人工批准；如有问题请退回对应业务证据")
        record = {
            "id": _event_id(goal_id, "DECISION"),
            "action_id": action_id,
            "decision": decision,
            "actor": actor[:80],
            "rationale": rationale[:1000],
            "evidence": [str(item)[:500] for item in (evidence or [])][:20],
            "timestamp": _now(),
        }
        goal.setdefault("decisions", []).append(record)
        self.save(goal)
        self.append_event(goal_id, "HUMAN_DECISION", actor, record)
        return record

    def append_event(self, goal_id: str, event_type: str, actor: str, detail: dict | None = None) -> dict:
        event = {
            "id": _event_id(goal_id, event_type),
            "goal_id": goal_id,
            "type": event_type,
            "actor": str(actor or "Agent")[:80],
            "timestamp": _now(),
            "detail": detail or {},
        }
        self.root.mkdir(parents=True, exist_ok=True)
        with self._lock:
            with self.events_file.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        return event

    def events(self, goal_id: str, limit: int = 200) -> list[dict]:
        if not self.events_file.exists():
            return []
        result = []
        for line in self.events_file.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("goal_id") == goal_id:
                result.append(event)
        return list(reversed(result[-max(1, min(int(limit), 1000)):]))
