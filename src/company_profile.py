from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any


DEFAULT_PROFILE = {
    "entity_id": "cn_studio",
    "company_name": "示例游戏公司",
    "credit_code": "",
    "registered_city": "",
    "base_currency": "CNY",
    "accounting_standard": "小企业会计准则",
    "fiscal_year_end": "12-31",
    "close_target_days": 7,
    "vat_taxpayer_type": "待配置",
    "vat_filing_frequency": "待配置",
    "cit_filing_frequency": "季度",
    "cit_collection_method": "查账征收",
    "micro_enterprise_candidate": "待核验",
    "payroll_enabled": True,
    "asset_policy": {"material_assets_present": "待确认", "monthly_attestation": {}},
    "cross_border_business": True,
    "external_accountant": {"provider": "", "contact": "", "email": ""},
    "cash_planning": {"opening_cash_cny": None, "minimum_buffer_cny": 100000, "forecast_months": 12},
    "fx_policy": {"source": "", "month_end_rates": {}},
    "tax_policy": {"cross_border_reviews": {}, "shanghai_vat_pilot_status": "待确认"},
    "review_policy": {
        "auto_post_enabled": False,
        "high_confidence_threshold": 0.92,
        "materiality_cny": 1000,
        "company_head_final_close": True,
    },
}


ALLOWED_ACCOUNTING_STANDARDS = {"小企业会计准则", "企业会计准则"}
ALLOWED_VAT_TYPES = {"待配置", "小规模纳税人", "一般纳税人"}
ALLOWED_FREQUENCIES = {"待配置", "月度", "季度"}
ALLOWED_SHANGHAI_VAT_PILOT = {"待确认", "已纳入试点", "未纳入试点", "不适用"}


def load_profile(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    if not path.exists():
        return deepcopy(DEFAULT_PROFILE)
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return deepcopy(DEFAULT_PROFILE)
    profile = deepcopy(DEFAULT_PROFILE)
    for key, value in stored.items():
        if isinstance(value, dict) and isinstance(profile.get(key), dict):
            profile[key].update(value)
        else:
            profile[key] = value
    return profile


def validate_profile(profile: dict[str, Any]) -> list[str]:
    errors = []
    if not str(profile.get("company_name") or "").strip():
        errors.append("公司名称不能为空")
    if profile.get("base_currency") != "CNY":
        errors.append("当前中国境内主体版本的记账本位币必须配置为CNY")
    if profile.get("accounting_standard") not in ALLOWED_ACCOUNTING_STANDARDS:
        errors.append("会计准则配置无效")
    if profile.get("vat_taxpayer_type") not in ALLOWED_VAT_TYPES:
        errors.append("增值税纳税人类型配置无效")
    if profile.get("vat_filing_frequency") not in ALLOWED_FREQUENCIES:
        errors.append("增值税申报周期配置无效")
    pilot_status = (profile.get("tax_policy") or {}).get("shanghai_vat_pilot_status", "待确认")
    if pilot_status not in ALLOWED_SHANGHAI_VAT_PILOT:
        errors.append("上海增值税试点身份配置无效")
    try:
        if int(profile.get("close_target_days")) not in range(1, 31):
            errors.append("关账目标天数应在1至30天之间")
    except (TypeError, ValueError):
        errors.append("关账目标天数必须是整数")
    try:
        threshold = float((profile.get("review_policy") or {}).get("high_confidence_threshold"))
        if not 0.5 <= threshold <= 1:
            errors.append("高置信阈值应在0.5至1之间")
    except (TypeError, ValueError):
        errors.append("高置信阈值必须是数字")
    return errors


def save_profile(path: str | Path, profile: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(DEFAULT_PROFILE)
    for key, value in profile.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key].update(value)
        else:
            merged[key] = value
    errors = validate_profile(merged)
    if errors:
        raise ValueError("；".join(errors))
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    return merged


def profile_gaps(profile: dict[str, Any]) -> list[dict[str, str]]:
    gaps = []
    checks = [
        ("credit_code", "统一社会信用代码", "开票、纳税申报和主体校验需要"),
        ("registered_city", "注册及主管税务地区", "印花税期限等属地规则需要"),
        ("vat_taxpayer_type", "增值税纳税人类型", "决定小规模或一般计税路径"),
        ("vat_filing_frequency", "增值税申报周期", "决定月度或季度申报任务"),
    ]
    for key, name, reason in checks:
        value = profile.get(key)
        if not value or value == "待配置":
            gaps.append({"field": key, "name": name, "reason": reason})
    if "上海" in str(profile.get("registered_city") or "") and (
        (profile.get("tax_policy") or {}).get("shanghai_vat_pilot_status", "待确认") == "待确认"
    ):
        gaps.append({
            "field": "tax_policy.shanghai_vat_pilot_status",
            "name": "上海增值税申报试点身份",
            "reason": "2026年6月起部分上海纳税人使用试行表，需以主管税务机关通知确认",
        })
    accountant = profile.get("external_accountant") or {}
    if not accountant.get("provider"):
        gaps.append({"field": "external_accountant.provider", "name": "会计/税务服务机构", "reason": "需明确申报复核和异常升级联系人"})
    return gaps
