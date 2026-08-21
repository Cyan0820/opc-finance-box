# 法国 SASU 税务设计包

`jurisdiction.fr_single_member_simplified_joint_stock_company` 是法国 `société par actions simplifiée unipersonnelle`（SASU）样板的 design 级税务 Pack。它用于整理 RNE/商业登记、利润税制、VAT 与年度账目证据和人工日历，不是申报软件，也不构成法国税务意见。

## 精确范围

当前 Pack 只覆盖法国 SASU，即只有一个股东的 SAS。SAS、EURL、SARL、SA、branch、sole trader 和外国主体不在同一合同内，必须选择或创建对应 Pack。

官方来源截至 2026-08-14 核对：

- Entreprendre Service Public 的 [SASU：ce qu'il faut savoir](https://entreprendre.service-public.gouv.fr/vosdroits/F37383?lang=fr)；
- Entreprendre Service Public 的 [Fiscalité de la SASU](https://entreprendre.service-public.gouv.fr/vosdroits/F36215)；
- Entreprendre Service Public / Ministère de la Justice 的 [Dépôt des comptes annuels d'une société](https://entreprendre.service-public.gouv.fr/vosdroits/F31214)。

## 可执行服务

- `tax.fr_single_member_simplified_joint_stock_company.registration_profile`：整理 SASU、RNE、利润税制与 VAT 登记证据；
- `tax.fr_single_member_simplified_joint_stock_company.evidence_checklist`：逐规则检查缺失、重复和未知证据 ID；
- `tax.fr_single_member_simplified_joint_stock_company.build_calendar`：生成利润申报、IS 付款、VAT 和年度账目提交的人工配置任务。

例如：

```bash
python -m src.cli dispatch examples/boxes/fr_dtc_shopify_stripe_sasu.json \
  examples/service_requests/fr_registration_profile.json

python -m src.cli dispatch examples/boxes/fr_dtc_shopify_stripe_sasu.json \
  examples/service_requests/fr_tax_calendar.json
```

## 为什么日期保持人工配置

- SASU 默认适用 IS，但满足条件时可能临时选择 IR；利润申报文件和日期还取决于 réel simplifié/normal、财年结束、首个财年以及 EFI/EDI 渠道；
- IS 预缴和余额受财年结束、首个财年、前期税额、免预缴情形与专业账户日期影响；
- VAT 可能是 franchise en base、réel simplifié 或 réel normal，申报可以是月、季或年，实际日期来自专业账户；
- SAS/SASU 的账目批准期限由股东在章程或决定中确定，常见 6 个月不是统一法定锚点；批准后线下与电子提交窗口也不同。

因此 Pack 不把任何简化公式包装成确定截止日。缺少主体事实时 `candidate_due_date` 为 `null`，并给出明确的 `missing_configuration`。

## 安全边界

Pack 不接收 SIREN、SIRET、税号或 VAT 号原值，只接收不含敏感值的证据引用。它不判断法国税务居民、IS/IR 税制、税率或优惠税率、IS 预缴金额、VAT 登记义务/制度/供应分类、CFE/CVAE、OSS/IOSS、工资和社会缴费、个人股息税或跨境税，不生成可提交申报文件，也不申报、付款或访问外部系统。所有输出保留 `tax_advisor_review` 与 `tax_filing_release` 人工 gate。
