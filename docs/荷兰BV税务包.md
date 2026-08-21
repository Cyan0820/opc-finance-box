# 荷兰 BV 税务设计包

`jurisdiction.nl_private_limited_company` 是荷兰 `besloten vennootschap`（BV）样板的 design 级税务 Pack。它用于整理 KVK 主体、Corporate Income Tax（VPB）、VAT 与年度财务报表的证据和人工日历，不是申报软件，也不构成荷兰税务意见。

## 精确范围

当前 Pack 只覆盖荷兰 BV。NV、foundation、association、cooperative、branch、sole trader 和外国主体不在同一合同内，必须选择或创建对应 Pack。

官方来源截至 2026-08-13 核对：

- Business.gov.nl / KVK 的 [Private limited company (BV) in the Netherlands](https://business.gov.nl/running-your-business/legal-forms-and-governance/private-limited-company-in-the-netherlands/)；
- Netherlands Tax Administration / Business.gov.nl 的 [Filing your corporate tax return](https://business.gov.nl/finance-and-taxes/filing-tax-returns/filing-your-corporate-tax-return-vpb-in-the-netherlands/)；
- Belastingdienst 的 [Filing a VAT return](https://www.belastingdienst.nl/wps/wcm/connect/bldcontenten/belastingdienst/business/vat/vat_in_the_netherlands/filing_vat_return_and_paying_vat/filing_a_vat_return/)；
- Business.gov.nl 的 [Filing financial statements](https://business.gov.nl/regulations/filing-financial-statements/)。

## 可执行服务

- `tax.nl_private_limited_company.registration_profile`：整理 BV、KVK、VPB 与 VAT 登记证据；
- `tax.nl_private_limited_company.evidence_checklist`：逐规则检查缺失、重复和未知证据 ID；
- `tax.nl_private_limited_company.build_calendar`：生成 VPB、VAT 和 KVK 财务报表人工配置任务。

例如：

```bash
python -m src.cli dispatch examples/boxes/nl_dtc_shopify_stripe_bv.json \
  examples/service_requests/nl_registration_profile.json

python -m src.cli dispatch examples/boxes/nl_dtc_shopify_stripe_bv.json \
  examples/service_requests/nl_tax_calendar.json
```

## 为什么日期保持人工配置

- calendar-year BV 的 VPB 通常在次年 6 月 1 日前申报，但 broken/short fiscal year、邀请函和延期会改变实际日期；
- 境内企业 VAT 一般在期间结束后 1 个月内申报，但登记、境内设立、月/季/年频率与账户日期必须确认；
- KVK 年度财务报表同时受“采纳后 8 天”和“财年后最多 12 个月”限制，还要确认 provisional statements 与 SBR 渠道。

因此 Pack 不把任何一个简化公式包装成确定截止日。缺少事实时 `candidate_due_date` 为 `null`，并给出明确的 `missing_configuration`。

## 安全边界

Pack 不接收 KVK number、RSIN、VAT number、BTW ID 或税号原值，只接收不含敏感值的证据引用。它不判断税务居民、fiscal unity、innovation box、KOR、OSS/IOSS、DGA 薪酬、工资税、股息预提、税率或税额，不生成可提交申报文件，也不申报、付款或访问外部系统。所有输出保留 `tax_advisor_review` 与 `tax_filing_release` 人工 gate。
