# Shadow Close 试运行

Shadow Close 用第一家真实公司已复核的人工关账结果，独立验证 Agent 生成的总账、报表和税务候选值。它是 preview 进入 stable 前的必要验收门，不是将人工数倒灌回系统的工具。

## 运行边界

- 每份基准工作簿只允许一个 `entity_id` 和一个 `YYYY-MM` 期间。
- `cn_studio` 和 `sg_publisher` 分别导入、比较和签认；管理汇总不合并法定账、税务、银行或审批。
- 导入不覆盖台账、凭证、税表、银行流水或期间状态。
- 只比较人工基准已提供的域；没有税务定稿时可只导入总账与报表。
- CLI 与 Workbench 报告还绑定当前 Box `runtime_fingerprint`；签认同时绑定 runtime、`baseline_id + entity_id + period`、基准源指纹和 Agent 比较结果。Box 配置、Pack、主体、期间、基准或候选数据任一变化后旧签认自动失效。

## 建议顺序

1. 完成正式台账的主体确认、期初余额、结算、采购验收、发票、银行和工资导入。
2. 由会计或代账机构使用《智能财务工作台-Shadow-Close基准模板》填写已复核结果。
3. 在“财务执行明细 → 关账验证”选择主体和期间，导入一份基准。
4. 逐项区分映射、截止、口径、证据或系统缺陷，不直接改数消除差异。
5. 无差异由独立复核人签认“验证通过”；有差异时退回补数，或对每个差异分别记录分类、至少 12 个字的说明和证据引用后签认“接受差异”。
6. 关账前再刷新一次，确认签认仍对当前指纹有效。

## 可 fork 的 CLI 流程

Workbench 与 CLI 使用同一套解析、比较和指纹契约。先按当前 Box 生成空白模板；模板的 Checks 页会列出该 Box 允许的法律主体，输出路径存在时拒绝覆盖：

```bash
opc-finance-box shadow-close-template BOX.json \
  --output evidence/shadow-baseline.xlsx
```

将人工已复核基准填入工作簿。然后从本地 Workbench 的 `finance-ops` 取得同一 `entity_id`、同一 `period` 的确定性财务结果 JSON，做只读比较：

```bash
opc-finance-box shadow-close-compare BOX.json \
  evidence/shadow-baseline.xlsx evidence/finance-result.json \
  --output evidence/shadow-report.json
```

比较命令要求 `finance-result.json` 顶层同时包含 `entity_id`、`period` 和 `financial_statements`；主体必须存在于当前 Box，并与工作簿完全一致。报告含人工和 Agent 金额，因此以 `0600` 私有权限独占创建，stdout 只返回范围、计数和 SHA-256 指纹，不返回财务值，也不写台账。

无差异时由独立财务复核人签认：

```bash
opc-finance-box shadow-close-review BOX.json evidence/shadow-report.json \
  --decision passed \
  --actor independent-finance-reviewer \
  --rationale "已核对签字总账、报表及全部比较口径" \
  --evidence-reference audit://shadow-review \
  --output evidence/shadow-reviewed.json
```

在 CI 或交接前只校验、不回显财务明细：

```bash
opc-finance-box shadow-close-verify BOX.json evidence/shadow-reviewed.json
```

该命令会验证主体仍属于当前 Box、逐行金额与差异、明确容差、分域汇总、报告指纹以及当前签认范围；stdout 只返回安全摘要。

有差异时，`--decision accepted-differences` 必须同时提供 `--resolutions resolutions.json` 和至少一项 `--evidence-reference`。`resolutions.json` 可以是数组，或包含 `exception_resolutions` 数组的对象；每项必须精确包含：

```json
{
  "domain": "statement",
  "key": "IS_REVENUE",
  "classification": "cutoff",
  "rationale": "人工定稿使用历史截止口径，影响已经单独复核",
  "evidence_references": ["audit://difference-resolution/IS_REVENUE"]
}
```

分类只能是 `mapping`、`cutoff`、`accounting_policy`、`source_evidence`、`timing`、`foreign_exchange`、`accepted_scope` 或 `system_defect`。每个当前差异必须且只能处置一次；`system_defect` 即使完成解释也不能进入 stable candidate。

签认命令在写文件前重算报告内部计数和指纹，并要求报告 runtime 与当前 Box 完全一致；手工改动 Box、主体、期间、金额、容差、差异状态或比较行后，旧报告会被拒绝。输出的 `shadow-reviewed.json` 可作为 stable promotion evidence 的一个 `shadow_close_reports` 元素，但仍需另外完成样本代表性、自动门、恢复演练、阈值批准和发布复核。

## 多主体组合验收

多主体 Box 必须先对每个已配置法律主体分别完成上述比较和当前签认，再运行带 `--verify-source-runs` 的 `finance.multi_entity_month_close_portfolio`，将命令结果保存为 `portfolio-result.json`。直接保存 CLI stdout 也可以，组装器会安全读取其 `result` 对象。

```bash
opc-finance-box shadow-close-portfolio-assemble BOX.json \
  --entity-report evidence/cn-shadow-reviewed.json \
  --entity-report evidence/sg-shadow-reviewed.json \
  --portfolio-result evidence/portfolio-result.json \
  --output evidence/portfolio-shadow-manifest.json

opc-finance-box shadow-close-portfolio-review BOX.json \
  evidence/portfolio-shadow-manifest.json \
  --decision passed \
  --actor portfolio-independent-reviewer \
  --rationale "已核对所有主体签认、来源运行台账、汇率底稿和组合范围" \
  --evidence-reference audit://portfolio-shadow-review \
  --output evidence/portfolio-shadow-reviewed.json

opc-finance-box shadow-close-portfolio-verify BOX.json \
  evidence/portfolio-shadow-reviewed.json
```

组装器要求全部已配置主体恰好出现一次、期间一致、逐主体签认仍对当前指纹有效、组合全部就绪、来源运行已通过防篡改台账核验，并保留候选、预抵销、无过账、无关账、无申报边界。组合复核人必须不同于每一位逐主体复核人。`passed` 只允许所有主体零差异且均为“验证通过”；存在已接受差异时只能选择 `accepted-differences` 或 `needs-correction`。

`portfolio-shadow-manifest.json` 和复核后的文件都以 `0600`、拒绝覆盖方式创建。它们只持久化主体报告指纹、复核 ID、计数、组合结果指纹、来源 attempt ID 和台账链头，不保存人工值、Agent 值或管理组合金额。验证通过仍只是多主体 Shadow Close 验收证据，不执行抵销、不生成法定合并报表、不自动修改 Pack 成熟度。

## 差异分类

| 状态 | 含义 | 建议处理 |
|---|---|---|
| 一致 | 差异在绝对或百分比容差内 | 保留来源，等待独立签认 |
| 需解释 | 双方都有金额但超过容差 | 查口径、截止、汇率、映射与更新时点 |
| Agent 缺项 | 人工基准有值，Agent 无法形成候选值 | 补原始业务事实或修复规则，不猜数 |
| 人工基准缺项 | Agent 有结果，基准未提供对应行 | 确认该行是否应进入基准范围 |

## 第一家公司验收证据

- 人工基准工作簿及其来源、复核人和定稿时点。
- 导入指纹、对比指纹、差异清单、修复记录和最终签认。
- 黑名单用例：跨主体、跨期间、重复键、借贷同时有余额、无证据接受差异。
- 一次完整的备份、空目标恢复和重启后签认有效性复查。

完成一家真实公司的脱敏试运行并由财务专业人士签认后，还必须通过 [Stable 晋级证据与签认](Stable晋级证据与签认.md) 的覆盖阈值、自动门、演练和三方职责分离，才能形成 Finance Core `stable_candidate_approved` 证据。
