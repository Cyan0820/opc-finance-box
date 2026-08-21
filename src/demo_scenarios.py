from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path

from .banking import BankTransaction, banking_payload, suggest_matches
from .business_flows import build_flow_overview, create_cash_allocation, create_expense_claim, create_payment_request
from .business_partner import build_bp_analysis
from .accounting_engine import create_accrual, create_asset_card, review_accounting_item
from .finance_ops import build_finance_ops
from .general_ledger import OpeningBalance
from .invoices import invoice_payload, roll_invoice_totals_to_purchases
from .payroll import PayrollRecord, payroll_payload
from .planning import PlanLine, build_planning_analysis, planning_payload
from .procurement import (
    PurchaseRecord, create_procurement_request, create_purchase_order_from_request,
    decide_procurement_request, procurement_budget_snapshot, procurement_payload,
    record_purchase_delivery,
)
from .reconcile import SettlementRecord, dashboard_payload
from .vendor_controls import create_vendor_bank_change, decide_vendor_bank_change


def load_demo_scenarios(path: str | Path) -> dict:
    path = Path(path)
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _identity(prefix: str, values: list) -> str:
    return hashlib.sha1(f"{prefix}|{'|'.join(map(str, values))}".encode("utf-8")).hexdigest()[:12]


def _settlements(rows: list[list], label: str) -> list[SettlementRecord]:
    output = []
    for index, row in enumerate(rows, 2):
        period, game, platform, channel, currency, gross, refunds, share_base, share_rate, settlement, tax, net, country = row
        record = SettlementRecord(
            id=_identity("settlement", row), source_file=f"{label}-结算对账.xlsx", source_sheet="结算对账", scope="国内" if label == "国服示例" else "海外",
            period=period, game=game, platform=platform, channel=channel, original_currency=currency, currency=currency,
            gross_original=gross, gross=gross, non_share=0, refunds=refunds, taxes=0,
            channel_cost=round(gross - refunds - share_base, 2), share_base=share_base, mix=None,
            share_rate=share_rate, fx_rate=1, settlement_amount=settlement, withholding_tax=tax,
            net_receivable=net, country=country, status="已核对", confidence=.99, anomalies=[],
            evidence={"row": index, "mapped_fields": ["period", "game", "platform", "channel", "currency", "gross", "refund", "share_base", "share_rate", "settlement", "withholding_tax", "net"]},
        )
        output.append(record)
    return output


def _purchases(rows: list[list], label: str) -> list[PurchaseRecord]:
    output = []
    for index, row in enumerate(rows, 2):
        po, order_date, project, vendor, item, quantity, unit_price, ordered, accepted, invoiced, paid, currency, tax_rate, note = row
        category = "广告投放" if "投放" in item else "软件与云服务" if any(x in item for x in ("云", "服务器")) else "素材制作"
        anomalies = ["疑似已发生未开票：月结需判断暂估"] if accepted and not invoiced else []
        output.append(PurchaseRecord(
            id=_identity("purchase", row), source_file=f"{label}-采购台账.xlsx", source_sheet="采购台账", source_row=index,
            po_number=po, order_date=order_date, project=project, vendor=vendor, category=category, item=item,
            quantity=quantity, unit_price=unit_price, ordered_amount=ordered, accepted_amount=accepted,
            invoice_amount=invoiced, paid_amount=paid, currency=currency, tax_rate=tax_rate,
            invoice_status="已开票" if invoiced else "未开票", payment_status="已付款" if paid >= (invoiced or 0) and paid else "未付款" if not paid else "部分付款",
            delivery_status="已验收" if accepted else "待确认", accounting_status="可生成应付凭证" if accepted else "缺少验收证据",
            status="异常" if anomalies else "待补证据", anomalies=anomalies, evidence={"note": note},
        ))
    return output


def _bank(rows: list[list], label: str) -> list[BankTransaction]:
    return [BankTransaction(
        id=_identity("bank", row), source_file=f"{label}-银行流水.xlsx", source_sheet="银行流水", source_row=index,
        transaction_date=row[0], transaction_id=row[1], account_masked=row[2], counterparty=row[3],
        counterparty_account_masked=row[4], summary=row[5], direction=row[6], currency=row[7], amount=row[8], balance=row[9], anomalies=[],
    ) for index, row in enumerate(rows, 2)]


def _invoices(rows: list[list], purchases: list[dict], label: str) -> dict:
    output = []
    for index, row in enumerate(rows, 2):
        number, date, invoice_type, seller, tax_id, buyer, item, amount, tax_rate, tax, total, po, project, verification, deduction, booking, status = row
        match = next((p for p in purchases if p.get("po_number") == po), None)
        output.append({
            "id": _identity("invoice", row), "source_file": f"{label}-发票台账.xlsx", "source_sheet": "发票台账", "source_row": index,
            "invoice_number": number, "invoice_code": "", "invoice_date": date, "invoice_type": invoice_type,
            "seller_name": seller, "seller_tax_id_masked": f"{tax_id[:4]}****{tax_id[-4:]}", "buyer_name": buyer,
            "item": item, "amount_ex_tax": amount, "tax_rate": tax_rate, "tax_amount": tax, "total_amount": total,
            "po_number": po, "project": project, "verification_status": verification, "deduction_status": deduction,
            "booking_status": booking, "duplicate_key": number, "status": "已匹配待入账" if match else "待匹配采购", "anomalies": [],
            "purchase_match": ({"purchase_id": match["id"], "po_number": po, "target": f"{match['vendor']} / {match['item']}", "score": 1, "difference": 0} if match else None),
        })
    return invoice_payload(output)


def _payroll(rows: list[list], label: str, period: str, jurisdiction: str = "CN", currency: str = "CNY") -> dict:
    output = []
    for index, row in enumerate(rows, 2):
        employee_id, name, department, project, gross, social, fund, special, other, cumulative_income, cumulative_deductions, tax_paid, net, rd_ratio = row
        current_tax = round(max(0, gross - social - fund - net), 2)
        output.append(PayrollRecord(
            id=_identity("payroll", row), source_file=f"{label}-工资表.xlsx", source_sheet="工资表", source_row=index, period=period,
            employee_masked=f"员工-{hashlib.sha1(employee_id.encode()).hexdigest()[:8]}", department=department, project=project,
            gross_salary=gross, social_security=social, housing_fund=fund, special_deduction=special,
            other_deduction=other, calculated_iit=current_tax if jurisdiction == "CN" else 0, declared_iit=current_tax, net_salary=net,
            rd_ratio=rd_ratio, rd_salary_candidate=round(gross * rd_ratio, 2), status="待复核", anomalies=[],
            currency=currency, jurisdiction=jurisdiction, employee_deductions=round(social + fund, 2),
            withholding_tax=current_tax, total_employer_cost=gross,
            payroll_basis="CN_CUMULATIVE_WITHHOLDING_CANDIDATE" if jurisdiction == "CN" else "IMPORTED_LOCAL_PAYROLL",
            statutory_calculation_status="system_candidate" if jurisdiction == "CN" else "imported_pending_local_review",
        ))
    payload = payroll_payload(output, period)
    for record in payload["records"]:
        if record.get("project") and float(record.get("rd_ratio") or 0) > 0:
            record.update({
                "allocation_evidence": [f"演示工时表 {period} · {record['id'][:6]}"],
                "allocation_evidence_type": "已批准月度工时表",
                "allocation_method": "显式研发工时比例",
                "activity_type": "研发活动",
            })
    return payload


def _opening(rows: list[list], label: str) -> list[dict]:
    category = {"1": "资产", "2": "负债", "3": "权益", "5": "成本费用"}
    return [asdict(OpeningBalance(
        id=_identity("opening", row), source_file=f"{label}-期初余额.xlsx", source_sheet="期初余额", source_row=index,
        period=row[0], account_code=row[1], account_name=row[2], account=f"{row[1]} {row[2]}", category=category.get(str(row[1])[:1], "待映射"),
        opening_debit=row[3], opening_credit=row[4], status="可用", anomalies=[],
    )) for index, row in enumerate(rows, 2)]


def _plans(rows: list[list], label: str) -> list[dict]:
    return [asdict(PlanLine(
        id=_identity("plan", row), source_file=f"{label}-预算预测.xlsx", source_sheet="预算预测", source_row=index,
        period=row[0], scenario=row[1], project=row[2], category=row[3], direction=row[4], amount=row[5], currency=row[6],
        probability=row[7], committed=row[8] == "是", note=row[9], status="可用", anomalies=[],
    )) for index, row in enumerate(rows, 2)]


def _kpis(rows: list[list], label: str) -> list[dict]:
    fields = ("period", "project_code", "channel", "region", "dau", "mau", "new_users", "payers", "installs", "gross_bookings", "marketing_spend", "retention_d1", "retention_d7", "retention_d30")
    return [{**dict(zip(fields, row)), "id": _identity("kpi", row), "status": "可用", "anomalies": [], "source_file": f"{label}-经营KPI.xlsx", "source_sheet": "经营KPI", "source_row": index} for index, row in enumerate(rows, 2)]


def _master_records(config: dict) -> list[dict]:
    names = sorted({row[1] for row in config["settlements"]})
    games = [{
        "id": _identity("game", [name]), "record_type": "game", "code": name, "name": name,
        "stage": "运营期", "owner": "项目负责人", "active": True, "anomalies": [], "status": "可用",
    } for name in names]
    channel_days = {
        "微信支付": 30, "App Store 中国区": 45, "硬核联盟": 60,
        "App Store": 45, "Google Play": 45, "AdMob": 30,
    }
    channels = sorted({row[3] for row in config["settlements"]})
    return games + [{
        "id": _identity("channel", [name]), "record_type": "channel", "code": name, "name": name,
        "payment_days": channel_days.get(name, 30), "active": True, "anomalies": [], "status": "可用",
    } for name in channels]


def _demo_collection_actions(settlements: list[dict], entity_id: str | None) -> list[dict]:
    target = next((row for row in settlements if row.get("period") == "2026-01"), None)
    if not target:
        return []
    amount = round(float(target.get("net_receivable") or 0) * 0.4, 2)
    return [{
        "id": f"COL-DEMO-{_identity('collection', [entity_id, target.get('id')]).upper()}",
        "entity_id": entity_id or "", "settlement_id": target.get("id"),
        "game": target.get("game"), "channel": target.get("channel"),
        "currency": target.get("currency") or "CNY", "outstanding_snapshot": target.get("net_receivable"),
        "action_type": "回款承诺", "action_date": "2026-02-25", "owner": "渠道运营负责人",
        "note": "演示：渠道已通过邮件确认预计付款日，财务保留原邮件引用。",
        "promised_date": "2026-03-15", "promised_amount": amount, "dispute_reason": "",
        "supersedes_action_id": None, "recorded_by": "财务BP", "recorded_at": "2026-02-25T08:00:00+00:00",
        "period": "2026-02", "idempotency_key": "",
    }]


def build_demo_payload(config: dict, entity_id: str | None = None) -> dict:
    label = config["label"]
    entity_id = entity_id or {"国服示例": "cn_studio", "海外示例": "sg_publisher"}.get(label)
    profile = deepcopy(config["profile"])
    if entity_id:
        profile["entity_id"] = entity_id
    if entity_id == "sg_publisher":
        profile.update({
            "jurisdiction": "SG", "functional_currency": "USD",
            "accounting_basis": "SFRS", "tax_pack": "jurisdiction.sg",
            "tax_readiness": "design",
        })
    elif entity_id == "cn_studio":
        profile.update({
            "jurisdiction": "CN", "functional_currency": "CNY",
            "accounting_basis": "PRC_SMALL_ENTERPRISE_AS",
            "tax_pack": "jurisdiction.cn_mainland", "tax_readiness": "filing_assist",
        })
    settlement_records = _settlements(config["settlements"], label)
    settlement_payload = dashboard_payload(settlement_records)
    purchase_payload = procurement_payload(_purchases(config["purchases"], label))
    purchase_rows = purchase_payload["records"]
    bank_payload = banking_payload(suggest_matches(_bank(config["bank"], label), settlement_payload["records"], purchase_rows))
    invoice_data = _invoices(config["invoices"], purchase_rows, label)
    payroll_data = _payroll(
        config["payroll"], label, "2026-02", str(profile.get("jurisdiction") or "CN"),
        str(profile.get("functional_currency") or profile.get("base_currency") or "CNY"),
    )
    opening_rows = _opening(config["opening"], label)
    plan_rows = _plans(config["plans"], label)
    kpi_rows = _kpis(config["kpis"], label)
    datasets = {
        "settlements": settlement_payload["records"], "purchases": purchase_rows,
        "bank_transactions": bank_payload["transactions"], "invoices": invoice_data["records"],
        "payroll_rows": payroll_data["records"], "opening_balances": opening_rows,
        "plan_lines": plan_rows, "game_kpis": kpi_rows, "master_records": _master_records(config),
    }
    if entity_id:
        for records in datasets.values():
            for record in records:
                record["entity_id"] = entity_id
    purchase_rows = roll_invoice_totals_to_purchases(purchase_rows, invoice_data["records"])
    purchase_payload = procurement_payload(purchase_rows)
    datasets["purchases"] = purchase_rows
    payable_preview = build_flow_overview({
        **datasets, "cash_allocations": [], "payment_requests": [], "expense_claims": [],
    })["payables"]["rows"]
    payable_target = next((row for row in payable_preview if row.get("outstanding", 0) > 0 and row.get("verified_invoice_amount", 0) > 0), None)
    account_target = payable_target or next((row for row in payable_preview if row.get("outstanding", 0) > 0), None)
    vendor_bank_changes = []
    if entity_id and account_target and account_target.get("vendor"):
        bank_change = create_vendor_bank_change(
            entity_id=entity_id or "", vendor=str(account_target["vendor"]),
            beneficiary_name=str(account_target["vendor"]),
            bank_name="演示银行" if entity_id != "sg_publisher" else "Demo Commercial Bank",
            bank_country="CN" if entity_id != "sg_publisher" else "SG",
            currency=str(account_target.get("currency") or ("USD" if entity_id == "sg_publisher" else "CNY")),
            account_number=("6222020600009012" if entity_id != "sg_publisher" else "SG00DEMO00009012"),
            requester="采购经办", evidence=["演示：供应商盖章账户函", "演示：主数据联系人"],
        )
        vendor_bank_changes, bank_change = decide_vendor_bank_change(
            [bank_change], bank_change["id"], "批准", "资金复核",
            "演示：已回拨主数据联系人并核对银行证明", "回拨主数据联系人", "演示回拨记录 DEMO-01",
        )
    payment_requests = []
    if payable_target:
        payment = create_payment_request(
            "payable", payable_target,
            min(float(payable_target.get("outstanding") or 0), float(payable_target.get("verified_invoice_amount") or 0)),
            "制作人", purpose=f"支付{payable_target.get('item') or '供应商款项'}",
            evidence=["采购订单", "验收记录", "已查验发票"],
            vendor_bank_accounts=vendor_bank_changes,
            bank_account_id=vendor_bank_changes[0]["id"] if vendor_bank_changes else "",
            require_approved_vendor_account=bool(payable_target.get("vendor")),
        )
        payment.update({"period": "2026-02", "entity_id": entity_id or ""})
        payment_requests.append(payment)
    procurement_requests = []
    purchase_deliveries = []
    budget_line = next((row for row in plan_rows if row.get("direction") != "收入" and not row.get("anomalies")), None)
    if entity_id and budget_line:
        request_amount = round(max(1, min(float(budget_line.get("amount") or 0) * 0.25, float(budget_line.get("amount") or 0))), 2)
        if request_amount > 0:
            snapshot = procurement_budget_snapshot(
                plan_rows, [], entity_id=entity_id or "", project=str(budget_line.get("project") or "游戏项目"),
                category=str(budget_line.get("category") or "素材制作"), period=str(budget_line.get("period") or "2026-02"),
                currency=str(budget_line.get("currency") or ("USD" if entity_id == "sg_publisher" else "CNY")),
            )
            procurement_request = create_procurement_request(
                entity_id=entity_id or "", project=snapshot["project"], category=snapshot["category"],
                description="演示：新版本素材与商店页制作" if entity_id != "sg_publisher" else "Demo: global store creative production",
                amount=request_amount, currency=snapshot["currency"], period=snapshot["period"], needed_by="2026-03-20",
                requester="制作人", sourcing_method="竞争比价", selected_vendor="供应商A" if entity_id != "sg_publisher" else "Vendor A",
                quotes=[
                    {"vendor": "供应商A" if entity_id != "sg_publisher" else "Vendor A", "amount": request_amount, "currency": snapshot["currency"], "evidence": "演示报价A"},
                    {"vendor": "供应商B" if entity_id != "sg_publisher" else "Vendor B", "amount": round(request_amount * 1.08, 2), "currency": snapshot["currency"], "evidence": "演示报价B"},
                    {"vendor": "供应商C" if entity_id != "sg_publisher" else "Vendor C", "amount": round(request_amount * 1.12, 2), "currency": snapshot["currency"], "evidence": "演示报价C"},
                ], evidence=["演示需求说明", "演示三方报价"], budget_snapshot=snapshot,
            )
            procurement_request = decide_procurement_request(
                procurement_request, "批准", "财务负责人", "演示：预算、必要性和供应商报价均已核对",
            )
            procurement_request, demo_order = create_purchase_order_from_request(
                procurement_request,
                po_number="PO-DEMO-CN-001" if entity_id != "sg_publisher" else "PO-DEMO-SG-001",
                order_date="2026-03-01", actor="采购经办",
                item="新版本素材与商店页" if entity_id != "sg_publisher" else "Global store creative pack",
                evidence=["演示：双方订单确认"], milestones=[{
                    "title": "素材首批交付" if entity_id != "sg_publisher" else "Creative first delivery",
                    "amount": request_amount, "due_date": "2026-03-20",
                    "acceptance_criteria": "源文件、预览图和交付清单完整" if entity_id != "sg_publisher" else "Source files, previews and manifest complete",
                    "owner": "美术负责人" if entity_id != "sg_publisher" else "Creative Lead",
                }],
            )
            purchase_rows.append(demo_order)
            procurement_requests.append(procurement_request)
            purchase_deliveries.append(record_purchase_delivery(
                demo_order, milestone_id=demo_order["milestones"][0]["id"],
                delivered_amount=request_amount, delivery_date="2026-03-18",
                delivered_by="供应商交付人" if entity_id != "sg_publisher" else "Vendor Delivery Owner",
                evidence=["演示交付清单", "演示文件哈希"], existing_deliveries=[],
            ))
            purchase_payload = procurement_payload(purchase_rows)
    claim = create_expense_claim(
        "运营同学", "2026-02-25", 1280 if entity_id != "sg_publisher" else 240,
        "CNY" if entity_id != "sg_publisher" else "USD",
        "长安幻想录" if entity_id != "sg_publisher" else "Pixel Odyssey",
        "渠道差旅" if entity_id != "sg_publisher" else "Community operations",
        "参加渠道运营沟通并提交业务记录", ["电子发票", "行程单", "业务纪要"], "运营同学", entity_id or "",
    )
    claim["status"] = "已批准待付款"
    claim["approved_amount"] = claim["amount"]
    claim["approval_history"] = [{
        "decision": "批准", "actor": "财务负责人", "rationale": "示例：用途和证据已核对",
        "approved_amount": claim["amount"], "timestamp": "2026-02-26T00:00:00+00:00",
    }]
    functional_currency = profile.get("functional_currency") or "CNY"
    adapter_id = "sg-internal-ledger-v1" if entity_id == "sg_publisher" else "cn-small-enterprise-v1"
    asset = create_asset_card(
        "构建与测试工作站" if entity_id != "sg_publisher" else "Build and test workstation",
        "固定资产", "2026-01-10", 36000 if entity_id != "sg_publisher" else 6000,
        36, 0, "长安幻想录" if entity_id != "sg_publisher" else "Pixel Odyssey",
        "设备供应商" if entity_id != "sg_publisher" else "Studio Hardware Pte. Ltd.",
        ["采购订单", "发票", "验收记录"], "财务经办",
        currency=functional_currency, functional_currency=functional_currency,
        ledger_adapter_id=adapter_id,
    )
    asset["entity_id"] = entity_id or ""
    asset = review_accounting_item(asset, "批准", "会计服务机构", "示例：类别、成本和使用年限已核对")
    license_amount = 120000 if entity_id != "sg_publisher" else 18000
    license_currency = functional_currency
    license_project = "长安幻想录" if entity_id != "sg_publisher" else "星海远征"
    license_purchase = {
            "id": _identity("license-purchase", [entity_id, license_project, license_amount]),
            "entity_id": entity_id or "", "po_number": "DEMO-IP-CN" if entity_id != "sg_publisher" else "DEMO-IP-SG",
            "order_date": "2026-01-02", "project": license_project, "category": "IP授权",
            "vendor": "演示IP权利方", "item": "年度游戏IP授权", "currency": license_currency,
            "ordered_amount": license_amount, "accepted_amount": license_amount,
            "invoice_amount": license_amount, "paid_amount": license_amount,
            "acceptance_status": "已验收", "invoice_status": "已开票", "payment_status": "已付款",
            "status": "已完成", "anomalies": [],
            "contract_facts": {
                "cost_type": "游戏授权费" if entity_id != "sg_publisher" else "IP license",
                "contract_reference": "DEMO-LICENSE-CN-2026" if entity_id != "sg_publisher" else "DEMO-LICENSE-SG-2026",
                "contract_evidence": ["演示授权协议", "演示权利清单"],
                "service_start": "2026-01-01", "service_end": "2026-12-31",
                "period_evidence": [
                    {"period": month, "evidence": [f"演示：{month}授权权利有效确认"]}
                    for month in ("2026-01", "2026-02", "2026-03")
                ],
            },
            "cost_policy": {
                "status": "已批准", "approved_by": "会计服务机构",
                "classification": "递延", "allocation_method": "按服务期间直线释放",
                "cost_basis_amount": license_amount, "evidence": ["演示会计政策审批 DEMO-AP-01"],
            },
            "accepted_amount": license_amount, "invoice_amount": license_amount,
            "acceptance_history": [{"decision": "全部验收", "evidence": ["演示授权权利验收"]}],
            "payment_evidence": ["演示付款回单"],
        }
    purchase_rows.append(license_purchase)
    datasets["invoices"].append({
            "id": _identity("license-invoice", [entity_id, license_amount]), "entity_id": entity_id or "",
            "currency": license_currency, "total_amount": license_amount, "verification_status": "已查验",
            "anomalies": [], "purchase_match": {"purchase_id": license_purchase["id"]},
            "source_file": "演示授权费发票.xlsx", "source_sheet": "发票", "source_row": 2,
        })
    purchase_deliveries.append({
            "id": _identity("license-acceptance", [entity_id, license_purchase["id"]]),
            "entity_id": entity_id or "", "purchase_id": license_purchase["id"],
            "status": "已验收", "accepted_amount": license_amount,
            "acceptance_evidence": ["演示授权权利验收"], "period": "2026-01",
        })
    accrual = create_accrual(
        "2026-02", "二月美术外包已验收未开票" if entity_id != "sg_publisher" else "February art outsourcing accepted but not invoiced",
        28000 if entity_id != "sg_publisher" else 4200, "5602 管理费用" if entity_id != "sg_publisher" else "",
        "美术外包商" if entity_id != "sg_publisher" else "Global Art Vendor",
        "长安幻想录" if entity_id != "sg_publisher" else "Pixel Odyssey",
        ["合同", "交付清单", "验收记录"], "财务经办", currency=functional_currency,
        functional_currency=functional_currency, expense_role="cost_of_sales", ledger_adapter_id=adapter_id,
    )
    accrual["entity_id"] = entity_id or ""
    accrual = review_accounting_item(accrual, "批准", "会计服务机构", "示例：服务已完成且暂估金额有依据")
    if not payment_requests:
        payable_target = next((row for row in payable_preview if row.get("outstanding", 0) > 0), None)
        if payable_target:
            payment = create_payment_request(
                "payable", payable_target, float(payable_target.get("outstanding") or 0), "发行负责人",
                purpose=f"支付{payable_target.get('item') or '海外供应商款项'}",
                evidence=["采购订单", "验收记录"], prepayment=True,
                vendor_bank_accounts=vendor_bank_changes,
                bank_account_id=vendor_bank_changes[0]["id"] if vendor_bank_changes else "",
                require_approved_vendor_account=bool(payable_target.get("vendor")),
            )
            payment.update({"period": "2026-02", "entity_id": entity_id or ""})
            payment_requests.append(payment)
    datasets.update({
        "cash_allocations": [], "payment_requests": payment_requests, "expense_claims": [claim],
        "vendor_bank_changes": vendor_bank_changes, "procurement_requests": procurement_requests,
        "purchase_deliveries": purchase_deliveries,
        "asset_cards": [asset], "accruals": [accrual],
        "collection_actions": _demo_collection_actions(datasets["settlements"], entity_id),
    })
    business_flows = build_flow_overview(datasets, "2026-03-01")
    return {
        "scenario": label, "company_profile": profile, "settlements": settlement_payload,
        "procurement": purchase_payload, "banking": bank_payload, "invoices": invoice_data,
        "payroll": payroll_data, "opening_balances": opening_rows,
        "planning": build_planning_analysis(
            plan_rows, datasets["settlements"], purchase_rows, bank_payload["transactions"],
            payroll_data["records"], profile, "2026-02", "基准",
            datasets["collection_actions"], datasets["cash_allocations"],
        ),
        "bp": build_bp_analysis(datasets, profile, "2026-02", "基准"),
        "finance_ops": build_finance_ops(
            datasets["settlements"], "2026-02", purchase_rows, bank_payload["transactions"],
            invoice_data["records"], payroll_data["records"], profile, opening_rows,
            datasets["asset_cards"], datasets["accruals"], expense_claims=datasets["expense_claims"],
        ),
        "business_flows": business_flows,
        "download": f"/api/demo-workbook?scenario={'domestic' if label == '国服示例' else 'overseas'}",
        "datasets": datasets,
        "counts": {"settlements": len(datasets["settlements"]), "purchases": len(purchase_rows), "bank_transactions": len(bank_payload["transactions"]), "invoices": len(invoice_data["records"]), "payroll_rows": len(payroll_data["records"]), "plan_lines": len(plan_rows), "game_kpis": len(kpi_rows)},
    }


def build_group_demo_payload(scenarios: dict) -> dict:
    """Build a management package across the domestic and overseas entities."""
    domestic = scenarios.get("domestic")
    overseas = scenarios.get("overseas")
    if not domestic or not overseas:
        raise ValueError("全球管理汇总示例需要国服和海外两套主体数据")
    domestic_payload = build_demo_payload(domestic, "cn_studio")
    overseas_payload = build_demo_payload(overseas, "sg_publisher")
    profile = deepcopy(domestic["profile"])
    domestic_cash = domestic["profile"].get("cash_planning") or {}
    overseas_cash = overseas["profile"].get("cash_planning") or {}
    profile.update({
        "company_name": "全球游戏项目组合（演示）",
        "credit_code": "GROUP-MANAGEMENT-VIEW",
        "reporting_scope": "group_management",
        "cross_border_business": True,
        "legal_entities": [
            {"key": "domestic", "entity_id": "cn_studio", "name": domestic["profile"]["company_name"], "market": "国服", "base_currency": "CNY"},
            {"key": "overseas", "entity_id": "sg_publisher", "name": overseas["profile"]["company_name"], "market": "海外", "base_currency": "USD"},
        ],
        "cash_planning": {
            "opening_cash_cny": float(domestic_cash.get("opening_cash_cny") or 0) + float(overseas_cash.get("opening_cash_cny") or 0),
            "minimum_buffer_cny": float(domestic_cash.get("minimum_buffer_cny") or 0) + float(overseas_cash.get("minimum_buffer_cny") or 0),
            "forecast_months": max(int(domestic_cash.get("forecast_months") or 0), int(overseas_cash.get("forecast_months") or 0)),
        },
        "fx_policy": deepcopy(overseas["profile"].get("fx_policy") or {}),
        "external_accountant": {"provider": "按主体分别复核", "contact": "国服/海外服务机构", "email": ""},
    })
    combined = {
        "label": "全球管理汇总示例",
        "company": "全球游戏项目组合",
        "filename": "智能财务工作台-全球管理汇总示例.xlsx",
        "profile": profile,
    }
    for name in ("settlements", "purchases", "bank", "invoices", "payroll", "opening", "plans", "kpis"):
        combined[name] = deepcopy(domestic.get(name) or []) + deepcopy(overseas.get(name) or [])
    payload = build_demo_payload(combined)
    # Preserve statutory ownership in the management package. Records stay intact;
    # elimination is an overlay, never a destructive merge.
    payload["datasets"] = {
        name: deepcopy(domestic_payload["datasets"].get(name) or []) + deepcopy(overseas_payload["datasets"].get(name) or [])
        for name in payload["datasets"]
    }
    payload["settlements"]["records"] = payload["datasets"]["settlements"]
    payload["procurement"]["records"] = payload["datasets"]["purchases"]
    payload["banking"]["transactions"] = payload["datasets"]["bank_transactions"]
    payload["invoices"]["records"] = payload["datasets"]["invoices"]
    payload["payroll"]["records"] = payload["datasets"]["payroll_rows"]
    payload["opening_balances"] = payload["datasets"]["opening_balances"]
    same_game = {
        ("cn_studio", "长安幻想录"): ("GAME-GLOBAL-001", "星海远征 / 长安幻想录"),
        ("sg_publisher", "星海远征"): ("GAME-GLOBAL-001", "星海远征 / 长安幻想录"),
    }
    for record in payload["datasets"]["settlements"]:
        identity = same_game.get((record.get("entity_id"), record.get("game")))
        if identity:
            record["management_game_id"], record["management_game_name"] = identity
    for record in payload["datasets"]["master_records"]:
        identity = same_game.get((record.get("entity_id"), record.get("name")))
        if identity:
            record["management_game_id"], record["management_game_name"] = identity
    demo_allocations = []
    for entity_id in ("cn_studio", "sg_publisher"):
        transaction = next((row for row in payload["datasets"]["bank_transactions"] if (
            row.get("entity_id") == entity_id and row.get("direction") == "收入"
            and (row.get("suggested_match") or {}).get("target_id")
        )), None)
        target_id = (transaction.get("suggested_match") or {}).get("target_id") if transaction else ""
        target = next((row for row in payload["datasets"]["settlements"] if (
            row.get("entity_id") == entity_id and row.get("id") == target_id
        )), None)
        if transaction and target:
            allocation = create_cash_allocation(
                transaction, "receivable", target,
                min(float(transaction.get("amount") or 0), float(target.get("net_receivable") or 0)),
                demo_allocations, "演示资金复核人", note="演示：已核对渠道、币种、金额和银行摘要",
            )
            allocation["id"] = _identity("group-demo-allocation", [entity_id, transaction["id"], target["id"]])
            allocation["period"] = str(transaction.get("transaction_date") or "")[:7]
            demo_allocations.append(allocation)
    payload["datasets"]["cash_allocations"] = demo_allocations
    payload["business_flows"] = build_flow_overview(payload["datasets"], "2026-03-01")
    payload["bp"] = build_bp_analysis(payload["datasets"], profile, "2026-02", "基准")
    payload.update({
        "scenario": "全球管理汇总示例",
        "scope_mode": "group",
        "download": "/api/demo-workbook?scenario=group",
        "entities": profile["legal_entities"],
        "entity_workspaces": [
            {
                "key": key,
                "entity_id": "cn_studio" if key == "domestic" else "sg_publisher",
                "name": config["profile"]["company_name"],
                "market": "国服" if key == "domestic" else "海外",
                "settlement_records": base["counts"]["settlements"],
                "currencies": sorted(base["settlements"]["summary"]["currencies"]),
                "revenue_cny": base["bp"]["totals"].get("revenue") or 0,
                "confirmation_scope": "主体账、税务、银行与审批均按此主体处理",
            }
            for key, config, base in (
                ("domestic", domestic, domestic_payload), ("overseas", overseas, overseas_payload),
            )
        ],
        "statutory_guardrail": "全球管理汇总用于项目和经营管理；凭证、法定报表、税务申报、银行付款及审批必须回到各法律主体。",
        "elimination_policy": {
            "status": "待配置内部往来规则",
            "principle": "主体间分成、授权费、借款和代收代付在全球管理口径中抵销，原始主体记录不删除。",
        },
    })
    return payload
