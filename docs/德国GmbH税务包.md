# 德国 GmbH 税务设计包

`jurisdiction.de_limited_liability_company` 是德国 `Gesellschaft mit beschränkter Haftung`（GmbH）样板的 design 级税务 Pack。它用于整理 Handelsregister 主体、Körperschaftsteuer、Gewerbesteuer、VAT 与年度财务报表披露的证据和人工日历，不是申报软件，也不构成德国税务意见。

## 精确范围

当前 Pack 只覆盖德国 GmbH。UG（haftungsbeschränkt）、AG、GmbH & Co. KG、branch、partnership、sole trader 和外国主体不在同一合同内，必须选择或创建对应 Pack。

官方来源截至 2026-08-14 核对：

- BMWE 创业门户的 [Gesellschaft mit beschränkter Haftung (GmbH)](https://www.existenzgruendungsportal.de/SharedDocs/Expertenforum_Unterseiten/Rechtsformen/Gesellschaft-mit-beschraenkter-Haftung-GmbH/Gesellschaft-mit-beschraenkter-Haftung-GmbH?nn=f41c74ec-04ca-47bf-9309-80b8ceb79b27)；
- Bundesportal 的 [Pay corporate tax](https://verwaltung.bund.de/leistungsverzeichnis/EN/leistung/99102014002000/herausgeber/HH-S1000020010000011598/region/02)；
- Bundesportal 的 [Pre-notify sales tax](https://verwaltung.bund.de/leistungsverzeichnis/EN/leistung/99102021241000/herausgeber/NI-8676521/region/030000000000)；
- 德国联邦法律库的 [HGB § 325 Offenlegung](https://www.gesetze-im-internet.de/hgb/__325.html)；
- Unternehmensregister 的 [Submit for disclosure](https://www.unternehmensregister.de/en/howto/submit)。

## 可执行服务

- `tax.de_limited_liability_company.registration_profile`：整理 GmbH、商业登记、企业所得税、营业税与 VAT 登记证据；
- `tax.de_limited_liability_company.evidence_checklist`：逐规则检查缺失、重复和未知证据 ID；
- `tax.de_limited_liability_company.build_calendar`：生成 Körperschaftsteuer、Gewerbesteuer、VAT 预申报及财务报表披露的人工配置任务。

例如：

```bash
python -m src.cli dispatch examples/boxes/de_dtc_shopify_stripe_gmbh.json \
  examples/service_requests/de_registration_profile.json

python -m src.cli dispatch examples/boxes/de_dtc_shopify_stripe_gmbh.json \
  examples/service_requests/de_tax_calendar.json
```

## 为什么日期保持人工配置

- 企业所得税与营业税年度申报受税务期间、税务顾问代理、延期、主管税务机关/市镇和实际通知影响；
- VAT 预申报虽有一般的期间后第 10 日规则，但实际频率取决于设立年度、前期税额与账户状态，`Dauerfristverlängerung` 还会改变日期；
- HGB § 325 对一般资本公司规定了最迟期限，但公司规模、报表批准/审计、公开或存档范围及渠道仍需复核，不能把“最迟一年”误当成每个 GmbH 的唯一动作日期。

因此 Pack 不把简化公式包装成确定截止日。缺少主体事实时 `candidate_due_date` 为 `null`，并给出明确的 `missing_configuration`。

## 安全边界

Pack 不接收 Handelsregisternummer、Steuernummer、USt-IdNr. 或其他税务标识符原值，只接收不含敏感值的证据引用。它不判断德国税务居民、企业所得税率、Solidaritätszuschlag、营业税计税基础或地方 `Hebesatz`、VAT 登记义务/供应分类、OSS/IOSS、工资税或股息税，不生成可提交申报文件，也不申报、付款或访问外部系统。所有输出保留 `tax_advisor_review` 与 `tax_filing_release` 人工 gate。
