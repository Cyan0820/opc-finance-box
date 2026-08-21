from __future__ import annotations


def assess_close(finance: dict, period_state: dict, profile_gaps: list[dict]) -> dict:
    """单一关账判定入口，供 API、测试和未来工作流复用。"""
    reviews = period_state.get("voucher_reviews") or {}
    eligible = [
        voucher for voucher in finance.get("vouchers") or []
        if voucher.get("balanced") and voucher.get("status") != "阻塞"
    ]
    blockers = []
    if finance.get("close", {}).get("blocked"):
        blockers.append(f"月结任务仍有 {finance['close']['blocked']} 项阻塞")
    if not finance.get("trial_balance", {}).get("balanced"):
        blockers.append(f"试算差额为 {finance['trial_balance'].get('difference')}")
    unreviewed = [
        voucher["id"] for voucher in eligible
        if reviews.get(voucher["id"], {}).get("decision") != "接受"
    ]
    if unreviewed:
        blockers.append(f"仍有 {len(unreviewed)} 张可记账凭证未接受")
    if finance.get("posting", {}).get("enabled"):
        posted_ids = set(finance.get("posted_trial_balance", {}).get("source_voucher_ids") or [])
        unposted = [voucher["id"] for voucher in eligible if voucher["id"] not in posted_ids]
        if unposted:
            blockers.append(f"仍有 {len(unposted)} 张已具备条件的凭证尚未过账")
    else:
        unposted = []
    returned = [
        voucher_id for voucher_id, review in reviews.items()
        if review.get("decision") == "退回"
    ]
    if returned:
        blockers.append(f"仍有 {len(returned)} 张凭证处于退回状态")
    if profile_gaps:
        blockers.append(f"公司财务档案仍缺 {len(profile_gaps)} 项关键配置")
    return {
        "can_close": not blockers,
        "blockers": blockers,
        "eligible_voucher_ids": [voucher["id"] for voucher in eligible],
        "unreviewed_voucher_ids": unreviewed,
        "unposted_voucher_ids": unposted,
        "recommendation": (
            "关键勾稽、凭证复核和公司档案已满足关账门槛；建议负责人确认本月经营结果后关账。"
            if not blockers else
            "建议按阻塞顺序补证据：先补原始业务数据，再复核凭证，最后校验税务档案。不建议强制跳过。"
        ),
    }
