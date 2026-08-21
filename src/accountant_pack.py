from __future__ import annotations

import csv
import hashlib
import io
import json
import zipfile
from datetime import datetime, timezone
from typing import Iterable


def _csv_bytes(headers: list[str], rows: Iterable[Iterable]) -> bytes:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(headers)
    writer.writerows(rows)
    return ("\ufeff" + output.getvalue()).encode("utf-8")


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")


def _fingerprint(payload: object) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _scope_audit_events(events: Iterable[dict], entity_id: str, period: str) -> list[dict]:
    scoped = []
    for event in events:
        detail = event.get("detail") or {}
        event_entity = str(detail.get("entity_id") or "")
        event_period = str(detail.get("period") or "")
        target = str(event.get("target") or "")
        if entity_id and event_entity and event_entity != entity_id:
            continue
        if period and event_period and event_period != period:
            continue
        if entity_id and not event_entity and not target.startswith(f"{entity_id}:"):
            continue
        if period and not event_period and period not in target:
            continue
        scoped.append(event)
    return scoped


def build_accountant_pack(
    finance: dict, period_state: dict, audit_events: list[dict],
    datasets: dict[str, list[dict]], company_profile: dict, *, entity: dict | None = None,
) -> bytes:
    """生成可直接交给会计/税务服务机构的只读复核包。"""
    period = finance["period"]
    entity = entity or {}
    entity_id = str(finance.get("entity_id") or company_profile.get("entity_id") or entity.get("id") or "").strip()
    if not entity_id:
        raise ValueError("外部会计复核包必须指定法律主体")
    entity_name = str(entity.get("name") or entity.get("legal_name") or company_profile.get("company_name") or entity_id)
    scoped_datasets = {}
    for name, records in datasets.items():
        scoped_datasets[name] = [
            row for row in records
            if str(row.get("entity_id") or "") == entity_id
        ]
    audit_events = _scope_audit_events(audit_events, entity_id, period)
    reviews = period_state.get("voucher_reviews") or {}
    assessment = finance.get("close_assessment") or {}
    manifest = {
        "package_type": "statutory_month_close_review",
        "entity_id": entity_id,
        "entity_name": entity_name,
        "jurisdiction": entity.get("jurisdiction"),
        "functional_currency": entity.get("functional_currency") or company_profile.get("base_currency"),
        "accounting_basis": entity.get("accounting_basis") or company_profile.get("accounting_standard"),
        "period": period,
        "period_status": period_state.get("status") or "开放",
        "reporting_basis": (finance.get("posting") or {}).get("reporting_basis"),
        "close_gate": assessment,
        "data_scope": "single_legal_entity",
        "books_must_remain_separate": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    manifest["content_fingerprint"] = _fingerprint({
        "entity_id": entity_id, "period": period,
        "period_state": period_state,
        "trial_balance": finance.get("posted_trial_balance") or finance.get("trial_balance"),
        "financial_statements": finance.get("posted_financial_statements") or finance.get("financial_statements"),
        "dataset_counts": {name: len(rows) for name, rows in sorted(scoped_datasets.items())},
    })
    memory = io.BytesIO()
    with zipfile.ZipFile(memory, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        summary = [
            f"# {period} 财务复核包",
            "",
            f"生成时间（UTC）：{datetime.now(timezone.utc).isoformat()}",
            f"法律主体：{entity_name}（{entity_id}）",
            f"纳税地区：{entity.get('jurisdiction') or '待配置'}",
            f"会计准则：{entity.get('accounting_basis') or company_profile.get('accounting_basis') or company_profile.get('accounting_standard') or '待配置'}",
            f"期间状态：{period_state.get('status') or '开放'}",
            f"月结进度：{finance['close']['completed']}/{finance['close']['total']}，阻塞 {finance['close']['blocked']} 项",
            f"凭证草稿：{len(finance['vouchers'])} 张，试算纳入 {len(finance['trial_balance']['included_vouchers'])} 张",
            "",
            "## 使用说明",
            "",
            "本包是 Agent 基于标准化业务台账生成的复核资料，不代表已经记账或申报。",
            "本包仅含上述法律主体的数据；集团管理合并不进入法定复核包。",
            "请优先检查 `凭证草稿.csv` 中的 Agent 判断、阻塞原因、证据需求和复核决定。",
            "提交法定申报前，由有权人员在申报系统内核对并确认；系统不处理法务意见。",
        ]
        archive.writestr("00_复核说明.md", "\n".join(summary).encode("utf-8"))
        archive.writestr("00_交付清单.json", _json_bytes(manifest))
        archive.writestr("01_公司财务档案.json", _json_bytes({
            **company_profile, "entity_id": entity_id, "company_name": entity_name,
            "jurisdiction": entity.get("jurisdiction"),
        }))
        archive.writestr("02_月结任务.json", _json_bytes(finance["close"]))
        archive.writestr("03_凭证草稿.csv", _csv_bytes(
            ["凭证编号", "日期", "类型", "摘要", "原币", "原币金额", "状态", "是否平衡", "借方", "贷方",
             "Agent建议", "影响", "阻塞/待确认", "证据", "复核决定", "复核人", "复核说明"],
            ([
                voucher.get("id"), voucher.get("date"), voucher.get("type"), voucher.get("summary"),
                voucher.get("original_currency"), voucher.get("original_amount"), voucher.get("status"),
                "是" if voucher.get("balanced") else "否",
                "；".join(f"{line.get('account')} {line.get('amount')} [{line.get('dimension')} ]" for line in voucher.get("debit") or []),
                "；".join(f"{line.get('account')} {line.get('amount')} [{line.get('dimension')} ]" for line in voucher.get("credit") or []),
                (voucher.get("judgement") or {}).get("agent_recommendation"),
                (voucher.get("judgement") or {}).get("impact"), "；".join(voucher.get("blockers") or []),
                "；".join(voucher.get("evidence") or []),
                (reviews.get(voucher.get("id")) or {}).get("decision", "未复核"),
                (reviews.get(voucher.get("id")) or {}).get("actor", ""),
                (reviews.get(voucher.get("id")) or {}).get("rationale", ""),
            ] for voucher in finance["vouchers"]),
        ))
        archive.writestr("04_科目试算.csv", _csv_bytes(
            ["科目", "借方", "贷方", "净额"],
            ([row.get("account"), row.get("debit"), row.get("credit"), row.get("net")]
             for row in finance["trial_balance"]["rows"]),
        ))
        archive.writestr("05_税务资料任务.csv", _csv_bytes(
            ["税种", "周期", "状态", "风险", "Agent交付", "缺失资料", "Agent建议", "复核角色"],
            ([item.get("tax"), item.get("frequency"), item.get("status"), item.get("risk"),
              item.get("agent_output"), "；".join(item.get("missing") or []),
              (item.get("decision_support") or {}).get("recommendation"), item.get("review_role")]
             for item in finance["tax_pack"]["items"]),
        ))
        archive.writestr("06_规则版本.json", _json_bytes(finance["sources"]))
        archive.writestr("07_审计日志.json", _json_bytes(audit_events))
        archive.writestr("08_数据集清单.csv", _csv_bytes(
            ["数据集", "记录数", "说明"],
            ([name, len(records), f"仅包含 {entity_id} 标准化台账；原始上传文件未打包"] for name, records in sorted(scoped_datasets.items())),
        ))
        archive.writestr("09_完整财务分析.json", _json_bytes(finance))
        archive.writestr("10_财务报表草稿.json", _json_bytes(finance.get("financial_statements") or {}))
        archive.writestr("11_已过账试算平衡.json", _json_bytes(finance.get("posted_trial_balance") or {}))
        archive.writestr("12_已过账财务报表.json", _json_bytes(finance.get("posted_financial_statements") or {}))
        archive.writestr("13_银行余额调节.json", _json_bytes(finance.get("bank_reconciliation") or {}))
        archive.writestr("14_税务申报工作区.json", _json_bytes((finance.get("tax_pack") or {}).get("returns_workspace") or {}))
    return memory.getvalue()
