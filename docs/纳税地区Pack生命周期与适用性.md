# 纳税地区 Pack 生命周期与适用性

地区 Pack 是版本化的财务方法与官方来源快照，不是永久有效的税务结论。每个 `rules.json` 必须声明规则核验日和复核策略：

```json
{
  "verified_at": "2026-08-13",
  "review_policy": {
    "max_age_days": 180,
    "warning_days_before_expiry": 30,
    "expiry_effect": "block_external_filing_and_calendar_release",
    "reverification_triggers": [
      "authority_source_change",
      "rule_effective_date_change",
      "pack_upgrade",
      "entity_applicability_change",
      "tax_registration_change"
    ]
  },
  "applicability_review_policy": {
    "max_age_days": 365,
    "warning_days_before_expiry": 30,
    "expiry_effect": "block_calendar_and_external_filing_release",
    "reverification_triggers": [
      "pack_upgrade",
      "entity_applicability_change",
      "tax_registration_change"
    ]
  }
}
```

`verified_at + max_age_days` 是最后有效日；到期前的 warning 窗口为 `review_due`，最后有效日之后为 `expired`。`expired` 会阻断税务日历发布和任何外部申报 release，但不会阻断只读内部演示、历史底稿查看或重新复核。`review_due` 会告警但保持日历可用，以便在过期前完成当地专业复核。

## 使用

用显式业务日期检查规则状态，避免 CI、回放和升级审计依赖机器当天日期：

```bash
python -m src.cli tax-rule-status examples/boxes/global_game_studio.json \
  --as-of 2026-08-14
python -m src.cli doctor examples/boxes/global_game_studio.json \
  --as-of 2026-08-14
```

`review_policy` 管官方规则来源快照，`applicability_review_policy` 管真实主体事实签认；两者相互独立，不能用一份“当前”替代另一份。`options` 和 Box Builder API 会为每个已安装地区返回两种策略。编译后的 `jurisdiction-rules.json` 锁定同一策略，`release-gates.json` 要求重新检查两条生命周期。

## 逐主体适用性问卷

编译会生成 `tax-applicability-questionnaire.json`。它按法律主体和所选地区 Pack 生成五组未回答问题：法律形式与 Pack 范围、税收居民与常设机构、直接税/间接税登记、财年与申报期间、跨境/集团/特殊制度。问题引用 Pack 官方来源，并要求税务登记确认或当地税务顾问复核。

问卷只是可填写模板：

- 不自动推断适用性、税率、申报义务或税额。
- 不要求录入税号、账号、凭证或其他原始标识；只保存私有证据引用。
- 每个主体独立回答和签认，不能用集团或另一主体的结论替代。
- 主体事实、登记、Pack 版本、规则生效日或官方来源变化后必须重新核验。

当前仓库内置 Pack 的日期只代表源码中来源快照的核验状态，不代表已获得真实公司的当地税务签认。外部申报能力仍以 Pack 的 `tax_readiness`、完整证据、review gate 和实际税局回执为准。

## 私有工作底稿与独立签认

编译问卷是公共的未回答模板，真实主体的回答应进入 repo 外或被 `.gitignore` 排除的私有目录：

```bash
opc-finance-box tax-applicability-init box.json \
  --entity sg_company --prepared-by tax-operator \
  --facts-as-of 2026-08-14 \
  --output evidence/sg-tax-workpaper.json
```

工作底稿按当前 Box、法律主体、Pack 版本和问卷合同绑定。只能修改每题的枚举 `answer` 与 `evidence_references`；证据引用必须使用 `evidence://`、`document://`、`workpaper://`、`registry://`、`advisor://` 或 `authority://` 的不透明引用，不得加入税号、账号、附件正文或任意扩展字段。

当地税务复核人完成独立复核后生成一份不可覆盖的新文件：

```bash
opc-finance-box tax-applicability-review box.json evidence/sg-tax-workpaper.json \
  --decision approved-in-scope \
  --actor sg-local-tax-reviewer \
  --rationale "已核对法律形式、居民身份、登记、期间及特殊制度" \
  --evidence-reference advisor://sg-review/memo-2026 \
  --output evidence/sg-tax-review.json

opc-finance-box tax-applicability-verify box.json evidence/sg-tax-review.json \
  --as-of 2026-08-14
```

`approved-in-scope` 只接受五组问题全部回答、逐题有证据且没有未解决范围；`confirmed-out-of-scope` 和 `needs-correction` 都不会通过适用性 release gate。复核人必须不同于准备人。验证命令只返回主体、Pack、指纹、决定、`facts_as_of`、提醒日、失效日和生命周期，不返回回答、复核理由或证据引用内容。默认策略从事实截止日起 365 天有效、提前 30 天进入 `review_due`，最后有效日之后进入 `expired`；`review_due` 仍可释放但必须安排复核，`expired` 会关闭日历与外部申报 release gate。

`doctor` 可重复接收每个主体的签认文件，并单独报告 `ready_for_tax_calendar_release`：

```bash
opc-finance-box doctor box.json --as-of 2026-08-14 \
  --tax-applicability-review evidence/cn-tax-review.json \
  --tax-applicability-review evidence/sg-tax-review.json

opc-finance-box tax-applicability-portfolio-verify box.json \
  evidence/cn-tax-review.json evidence/sg-tax-review.json \
  --as-of 2026-08-14

opc-finance-box tax-applicability-import box.json \
  evidence/cn-tax-review.json \
  --review-dir /absolute/private/tax-applicability-reviews \
  --as-of 2026-08-14

opc-finance-box tax-applicability-status box.json \
  --review-dir /absolute/private/tax-applicability-reviews \
  --as-of 2026-08-14

opc-finance-box tax-applicability-registry-seal box.json \
  --review-dir /absolute/private/tax-applicability-reviews \
  --actor tax-registry-controller --as-of 2026-08-14 \
  --output evidence/tax-applicability-registry-receipt.json

opc-finance-box tax-applicability-registry-verify box.json \
  evidence/tax-applicability-registry-receipt.json \
  --review-dir /absolute/private/tax-applicability-reviews \
  --as-of 2026-08-14

opc-finance-box tax-applicability-alerts box.json \
  --review-dir /absolute/private/tax-applicability-reviews \
  --receipt evidence/tax-applicability-registry-receipt.json \
  --as-of 2026-08-14
```

Portfolio verifier 要求 Box 中每个主体恰好一份未过期的 `approved-in-scope` 签认，并分别汇总 `current` / `review_due` / `expired`。税务日历 release 还同时要求规则生命周期未过期且不存在待确认登记。它不代表税额已计算、申报已授权或税局已接收。

轮换目录只接受严格的 `<entity_id>.json` 文件，额外目录项会令 registry 不洁净，缺失主体会列为 `missing`。`tax-applicability-import` 在导入前验证当前 Box/Pack 和目录清洁度，按签认内主体 ID 独占写入，已有目标时拒绝覆盖；新一轮应建立新的私有目录，完成全主体 portfolio/status 验证后由独立 registry controller 生成内容指纹收据。运行时同时验证目录与收据，从而发现状态检查后发生的合法文件替换；收据不复制私有内容，不是数字签名，也不授予申报权。`tax-applicability-alerts` 把缺失、无效、临期、过期、目录污染和收据异常转成稳定 alert ID 的安全候选；编译出的 daily job 默认关闭，不发送通知，部署者必须配置时区、接收人、去重和升级责任人后才能启用。检查、导入、封存、验证和告警结果都不会返回文件路径、回答、理由或证据引用。私有工件拒绝符号链接；在 POSIX 上，任何 group/other 权限位都会被判为 `invalid`，因此复制、挂载或备份恢复后必须重新核对 `0600` 权限。
