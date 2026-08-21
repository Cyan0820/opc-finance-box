from __future__ import annotations

import csv
import hashlib
import io
import json
import tempfile
import zipfile
from copy import deepcopy
from pathlib import Path
from typing import Any, Iterable

from .tax_export import build_tax_workbook


FORM_CONTRACTS = {
    "VAT-NATIONAL-GENERAL-2026-02": {
        "form_code": "VAT-RETURN",
        "version": "国家税务总局公告2026年第6号填报要求",
        "effective_from": "2026-02",
        "official_form_page": "https://12366.chinatax.gov.cn/bzds/009/009.html",
        "transport": "manual_entry_assist",
        "scope": "全国一般纳税人申报表及附列资料；属地电子税务局接口未开放时不生成上传文件",
    },
    "VAT-SH-PILOT-2026-06": {
        "form_code": "VAT-RETURN",
        "version": "上海市税务局公告2026年第1号增值税及附加税费申报表（试行）",
        "effective_from": "2026-06",
        "official_form_page": "https://shanghai.chinatax.gov.cn/zcfw/zcfgk/zzs/202606/t480523.html",
        "transport": "manual_entry_assist",
        "scope": "仅适用于收到主管税务机关通知的上海试点纳税人",
    },
    "A200000-2025-10": {
        "form_code": "A200000",
        "version": "国家税务总局公告2025年第17号",
        "effective_from": "2025-10",
        "official_form_page": "https://12366.chinatax.gov.cn/bzds/059/059.html",
        "transport": "manual_entry_assist",
        "scope": "查账征收居民企业月（季）度预缴A类表",
    },
    "A01103-2022-07": {
        "form_code": "A01103",
        "version": "国家税务总局公告2022年第14号",
        "effective_from": "2022-07",
        "official_form_page": "https://guangdong.chinatax.gov.cn/gdsw/zjfg/2022-06/30/content_dd4c241d7fee4cb390865fc256827322.shtml",
        "transport": "manual_entry_assist",
        "scope": "印花税税源明细及财产和行为税综合申报前置资料",
    },
    "IIT-WITHHOLD-CURRENT": {
        "form_code": "IIT-WITHHOLD",
        "version": "个人所得税扣缴申报表现行公开表样",
        "effective_from": "2019-01",
        "official_form_page": "https://12366.chinatax.gov.cn/bzds/068/068.html",
        "transport": "controlled_manual_entry_assist",
        "scope": "实名敏感数据只在受控申报端补齐，本包保持脱敏",
    },
    "FIN-STATEMENTS-LOCAL": {
        "form_code": "FIN-STATEMENTS",
        "version": "按主管税务机关核定报表种类与会计准则",
        "effective_from": "",
        "official_form_page": "https://12366.chinatax.gov.cn/",
        "transport": "manual_entry_assist",
        "scope": "财务报表报送种类和周期需按主体征管信息确认",
    },
}


FIELD_TARGETS = {
    "VAT-SALES-CAND": "附列资料（一）：按发票类型、税率/征收率和计税方法拆分后填入销售额栏次",
    "VAT-OUTPUT": "主表及附列资料（一）：销项（应纳）税额",
    "VAT-INPUT": "附列资料（二）：本期进项税额明细",
    "VAT-PREPAID": "主表：本期已缴/预缴税额相关栏次",
    "VAT-PAYABLE": "主表：本期应补（退）税额",
    "SURTAX-CITY": "附加税费情况表：城市维护建设税",
    "SURTAX-EDU": "附加税费情况表：教育费附加",
    "SURTAX-LOCAL": "附加税费情况表：地方教育附加",
    "CIT-PROFIT-PERIOD": "仅作勾稽，不直接填报；A200000使用本年累计金额",
    "CIT-L01-REVENUE": "A200000第1行：营业收入（本年累计金额）",
    "CIT-L02-COST": "A200000第2行：营业成本（本年累计金额）",
    "CIT-L03-TAXES": "A200000第3行：税金及附加（本年累计金额）",
    "CIT-L04-SALES": "A200000第4行：销售费用（本年累计金额）",
    "CIT-L05-ADMIN": "A200000第5行：管理费用（本年累计金额）",
    "CIT-L06-RD": "A200000第6行：研发费用（本年累计金额）",
    "CIT-L07-FINANCE": "A200000第7行：财务费用（本年累计金额）",
    "CIT-L08-OTHER-INCOME": "A200000第8行：其他收益（本年累计金额）",
    "CIT-L15-OPERATING": "A200000第15行：营业利润（本年累计金额）",
    "CIT-L16-NONOP-INCOME": "A200000第16行：营业外收入（本年累计金额）",
    "CIT-L17-NONOP-EXPENSE": "A200000第17行：营业外支出（本年累计金额）",
    "CIT-PROFIT-YTD": "A200000第18行：利润总额（本年累计金额）",
    "CIT-ADJUST": "A200000第19行：特定业务计算的应纳税所得额",
    "CIT-TAXABLE": "A200000第25行：实际利润额/应纳税所得额",
    "CIT-RATE": "A200000第26行：税率",
    "CIT-PAYABLE": "A200000第32行：本期应补（退）所得税额",
    "CIT-PAYROLL-COST": "A200000附报事项：已计入成本费用的职工薪酬",
    "CIT-PAYROLL-PAID": "A200000附报事项：实际支付给职工的应付职工薪酬",
    "CIT-EXPORT-MODE": "A200000附报事项：出口方式",
    "STAMP-COUNT": "A01103税源明细行数核对；不直接作为计税金额",
    "IIT-PEOPLE": "个人所得税扣缴申报表：全员全额人员范围核对",
    "IIT-INCOME": "个人所得税扣缴申报表：本月（次）收入合计核对",
    "IIT-TAX": "个人所得税扣缴申报表：应补/退税额合计核对",
    "FS-ASSETS": "财务报表报送：资产负债表资产总计",
    "FS-LIAB-EQUITY": "财务报表报送：负债和所有者权益总计",
    "FS-REVENUE": "财务报表报送：利润表营业收入",
    "FS-PROFIT": "财务报表报送：利润表利润总额",
}


def form_fingerprint(form: dict, contract_id: str) -> str:
    payload = {
        "contract_id": contract_id,
        "form_code": form.get("form_code"),
        "version": form.get("version"),
        "fields": [
            {"code": field.get("code"), "value": field.get("value"), "status": field.get("status")}
            for field in form.get("fields") or []
        ],
        "blockers": sorted(map(str, form.get("blockers") or [])),
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def form_fingerprint_from_workspace(workspace: dict, form_code: str) -> tuple[str, str]:
    form = next((item for item in workspace.get("returns") or [] if item.get("form_code") == form_code), None)
    if not form:
        raise ValueError("当前税务工作底稿不存在该申报表")
    contract_id, _, _ = select_form_contract(form, workspace)
    return contract_id, form_fingerprint(form, contract_id)


def select_form_contract(form: dict, workspace: dict) -> tuple[str, dict, list[str]]:
    code = form.get("form_code")
    period = str(workspace.get("period") or "")
    blockers: list[str] = []
    if code == "VAT-RETURN":
        filing_profile = workspace.get("filing_profile") or {}
        city = str(filing_profile.get("registered_city") or "")
        pilot = str(filing_profile.get("shanghai_vat_pilot_status") or "待确认")
        if "上海" in city and period >= "2026-06":
            if pilot == "已纳入试点":
                contract_id = "VAT-SH-PILOT-2026-06"
            elif pilot == "未纳入试点":
                contract_id = "VAT-NATIONAL-GENERAL-2026-02"
            else:
                contract_id = "VAT-NATIONAL-GENERAL-2026-02"
                blockers.append("上海2026年增值税申报试点身份未确认，不能锁定全国表或试行表")
        else:
            contract_id = "VAT-NATIONAL-GENERAL-2026-02"
    else:
        contract_id = {
            "A200000": "A200000-2025-10",
            "A01103": "A01103-2022-07",
            "IIT-WITHHOLD": "IIT-WITHHOLD-CURRENT",
            "FIN-STATEMENTS": "FIN-STATEMENTS-LOCAL",
        }[code]
    contract = deepcopy(FORM_CONTRACTS[contract_id])
    if contract["effective_from"] and period < contract["effective_from"]:
        blockers.append(f"当前期间早于表单契约 {contract_id} 生效期")
    return contract_id, contract, blockers


def build_filing_assist(workspace: dict, reviews: Iterable[dict] = ()) -> dict:
    reviews = list(reviews)
    entity_id = str(workspace.get("entity_id") or "")
    forms = []
    for form in workspace.get("returns") or []:
        contract_id, contract, contract_blockers = select_form_contract(form, workspace)
        form["filing_contract"] = {
            "contract_id": contract_id, **deepcopy(contract),
            "blockers": list(contract_blockers),
        }
        fingerprint = form_fingerprint(form, contract_id)
        review = next((item for item in reviews if
                       item.get("entity_id", "") == entity_id
                       and item.get("period") == workspace.get("period")
                       and item.get("form_code") == form.get("form_code")), None)
        reviewed_fingerprint = (review or {}).get("form_fingerprint")
        review_current = bool(review and review.get("status") == "已复核" and reviewed_fingerprint == fingerprint)
        blockers = list(form.get("blockers") or []) + contract_blockers
        mappings = [{
            "field_code": field.get("code"), "field_name": field.get("name"),
            "candidate_value": field.get("value"), "candidate_status": field.get("status"),
            "official_target": FIELD_TARGETS.get(field.get("code"), "待建立官方栏次映射"),
            "source": field.get("source"),
        } for field in form.get("fields") or []]
        unmapped = sum(row["official_target"] == "待建立官方栏次映射" for row in mappings)
        release_ready = not blockers and not unmapped and review_current
        forms.append({
            "form_code": form.get("form_code"), "name": form.get("name"),
            "contract_id": contract_id, "contract": contract,
            "form_fingerprint": fingerprint, "field_mappings": mappings,
            "blockers": blockers, "unmapped_field_count": unmapped,
            "review_status": (review or {}).get("status") or "未复核",
            "review_id": (review or {}).get("id"),
            "review_current": review_current,
            "submission_status": ((review or {}).get("submission") or {}).get("status") or "未提交",
            "release_status": "可释放至人工申报端" if release_ready else "阻塞",
            "release_ready": release_ready,
            "external_submission_enabled": False,
            "direct_upload_file": None,
        })
    return {
        "entity_id": entity_id, "entity_name": workspace.get("company_name"),
        "period": workspace.get("period"), "forms": forms,
        "summary": {
            "form_count": len(forms),
            "contract_locked": sum(not any("不能锁定" in blocker for blocker in form["blockers"]) for form in forms),
            "ready_for_release": sum(form["release_ready"] for form in forms),
            "blocked": sum(not form["release_ready"] for form in forms),
            "direct_upload_ready": 0,
        },
        "release_boundary": "本包用于按官方表样逐栏复核和人工申报录入；未取得属地电子税务局导入规范及回执前，不生成或声称可直接上传。",
    }


def build_filing_assist_package(workspace: dict, assist: dict) -> bytes:
    with tempfile.TemporaryDirectory(prefix="opc-tax-filing-assist-") as temp_dir:
        temp = Path(temp_dir)
        workbook = build_tax_workbook(workspace, temp / "01_税务申报工作底稿.xlsx")
        manifest = {
            "package_type": "tax_filing_assist",
            "entity_id": assist["entity_id"], "entity_name": assist["entity_name"],
            "period": assist["period"], "summary": assist["summary"],
            "release_boundary": assist["release_boundary"],
            "forms": [{key: form[key] for key in (
                "form_code", "name", "contract_id", "form_fingerprint", "blockers",
                "review_status", "submission_status", "release_status", "external_submission_enabled",
            )} for form in assist["forms"]],
        }
        mapping = io.StringIO()
        writer = csv.writer(mapping)
        writer.writerow(["表单", "契约", "内部字段", "字段名称", "候选值", "状态", "官方目标栏次", "数据来源"])
        for form in assist["forms"]:
            for row in form["field_mappings"]:
                writer.writerow([
                    form["form_code"], form["contract_id"], row["field_code"], row["field_name"],
                    row["candidate_value"], row["candidate_status"], row["official_target"], row["source"],
                ])
        instructions = (
            "税务申报辅助包\n\n"
            "1. 先查看 manifest.json 的表单契约、阻塞项和指纹。\n"
            "2. 在工作底稿中完成数据与证据复核，再由有权人批准。\n"
            "3. 对照官方空表或电子税务局逐栏录入；本包不模拟电子税务局上传格式。\n"
            "4. 实际提交后，将申报流水号、回执和缴款凭证回写工作台。\n"
        )
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.write(workbook, workbook.name)
            archive.writestr("00_使用说明.txt", instructions)
            archive.writestr("02_表单契约与释放检查.json", json.dumps(manifest, ensure_ascii=False, indent=2))
            archive.writestr("03_字段到官方栏次映射.csv", "\ufeff" + mapping.getvalue())
            archive.writestr("04_回执登记模板.csv", "\ufeff表单,申报状态,申报流水号/回执号,操作人,回执文件,缴款凭证,备注\n")
        return output.getvalue()
