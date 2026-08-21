# 连续 Shadow 期间复核

首次观察复核只能证明一个期间。连续期间复核会重新验证 2–24 个连续月份的完整 Pilot 私有证据，把每期登记、Pipeline 台账、逐主体 Shadow Close 报告、组合签认和最终观察复核绑定成一份无金额收据。

通过后的含义仅是“可以开始准备 stable 晋级证据”。它不会直接运行 `promotion-assess`，不会修改 Pack 状态，也不授权过账、付款、法定关账或申报。进入 `promotion-assess` 时必须再次提供该 reviewed receipt、严格期间证据根和 Pipeline runs 根；评估器会重新验证而不是信任旧的 verify 输出。

## 1. 归档一个已复核期间

不要手工复制或改名拼装期间目录。先创建一个真实、非符号链接且权限为 `0700` 的空证据根，然后对每个已复核期间运行：

```bash
opc-finance-box pilot-shadow-period-archive BOX.json \
  private/current/reviewed-observation.json \
  private/current/shadow-run-registration.json \
  private/current/data-handoff-review.json \
  private/current/pilot-readiness-review.json \
  --entity-report private/current/entity-reports/cn_dtc_company.json \
  --evidence-root /absolute/private/shadow-series-periods \
  --runs-root /absolute/private/pipeline-runs \
  --as-of 2026-08-31
```

多主体 Box 对 `--entity-report` 重复传入每个主体，并必须增加 `--portfolio-review private/current/portfolio-review.json`。命令先用当前 Pipeline 台账重新验证完整观察链，再从已验证内容确定期间和主体，最后独占创建 `YYYY-MM/` 与 `entity-reports/`。源文件按原字节复制到私有归档，文件为 `0600`、目录为 `0700`；归档完成后会对复制件再次执行同一观察验证。已有月份拒绝覆盖，任何写入或复验失败都会清理本次新目录。

CLI 只返回期间、主体数、文件数和布尔边界，不返回源路径、内容哈希、人员、证据引用或财务值。该命令复制的是私有财务证据，不会修改法定账，也不会执行过账、付款、关账或外部申报。

归档结果允许进入下一期时，不要复制首期全部目录。在首客 Activation Workspace 中运行 `pilot-shadow-next-period-init`，它会重验完整归档链并从最新期间推导下一自然月，再生成该月独立的 readiness、handoff、逐主体 Shadow、可选 portfolio、observation 和归档命令。详见 [连续 Shadow 下一期间工作区](连续Shadow下一期间工作区.md)。

## 私有目录结构

准备一个只读根目录，每个期间只能使用 `YYYY-MM` 目录名。期间必须是连续自然月，不能缺月；根目录和期间目录均拒绝符号链接和额外条目。

单主体布局：

```text
private/shadow-series-periods/
├── 2026-08/
│   ├── reviewed-observation.json
│   ├── shadow-run-registration.json
│   ├── data-handoff-review.json
│   ├── pilot-readiness-review.json
│   └── entity-reports/
│       └── cn_dtc_company.json
└── 2026-09/
    ├── reviewed-observation.json
    ├── shadow-run-registration.json
    ├── data-handoff-review.json
    ├── pilot-readiness-review.json
    └── entity-reports/
        └── cn_dtc_company.json
```

多主体每个期间还必须包含 `portfolio-review.json`，且 `entity-reports/` 中只能有当前 Box 每个主体恰好一个 `<entity_id>.json`。所有 JSON 文件在 POSIX 上必须为 `0600`。Pipeline 运行账本继续通过独立的 `--runs-root` 提供，无需复制进每个期间目录。

每个期间需要自己的当前 Pilot 准入、资料交接、Shadow Run 登记和观察复核。不能把 8 月的准入或交接文件改名后当作 9 月证据；运行时会重新验证其中的期间、主体、内容指纹和台账记录。

## 2. 组装连续期收据

```bash
opc-finance-box pilot-shadow-series-assemble BOX.json \
  /absolute/private/shadow-series-periods \
  --runs-root /absolute/private/pipeline-runs \
  --as-of 2026-09-30 \
  --output private/pilot-shadow-series-receipt.json
```

组装时会对每个期间重新执行首次观察的完整验证。前一期间如果没有放行下一期间，后面却又出现新期间，组装会失败关闭。最后一个期间可以是 `needs-correction`，但此时连续期候选也只能是 `needs_correction`。

收据只保存：

- Box 指纹、精确主体集合和连续期间；
- 每期比较、匹配、差异与系统缺陷计数；
- 每期私有证据内容的聚合 SHA-256；
- 观察复核决定和职责分离所需的角色标识；
- 相邻期间的计数变化和差异趋势。

收据不保存财务金额、文件路径、Pipeline attempt ID、复核理由或证据引用。输出使用独占创建，不覆盖已有文件。

## 3. 独立连续性复核

全部期间均可继续时：

```bash
opc-finance-box pilot-shadow-series-review BOX.json \
  private/pilot-shadow-series-receipt.json \
  --decision approved-for-promotion-evidence \
  --actor independent-continuity-reviewer \
  --rationale "已复核连续月份范围、源证据重验和跨期差异趋势" \
  --evidence-reference audit://pilot/2026-09/continuity-review \
  --output private/pilot-shadow-series-reviewed.json
```

连续性复核人必须不同于所有期间的登记人、Pipeline 复核人、逐主体 Shadow 复核人、组合复核人和观察复核人。若最后一期含系统缺陷或被观察复核为需要修正，只能选择 `needs-correction`。

## 4. 持续验证

```bash
opc-finance-box pilot-shadow-series-verify BOX.json \
  private/pilot-shadow-series-reviewed.json \
  /absolute/private/shadow-series-periods \
  --runs-root /absolute/private/pipeline-runs \
  --as-of 2026-09-30
```

任何期间文件内容变化、观察复核失效、登记替换、Pipeline 当前 gate 被后续驳回、缺少主体报告、组合签认变化、目录增加文件或月份不连续，都会使既有连续期复核失效。

## 5. Doctor 与 Workbench

生产环境只读挂载：

- `OPC_PILOT_SHADOW_SERIES_REVIEW`：已独立复核的连续期收据；
- `OPC_PILOT_SHADOW_SERIES_EVIDENCE_ROOT`：上述严格期间目录；
- `OPC_FINANCE_PIPELINE_RUNS_ROOT`：当前 Pipeline 运行账本。

Doctor 增加 `pilot.consecutive_shadow_series_review`。`GET /api/box/pilot-shadow-series` 只返回主体数、期间范围、计数、连续性和“可准备晋级证据”布尔值；浏览器不能提交私有路径，也不能读取内容指纹、角色或金额。

## 与 stable 晋级的边界

`eligible_to_prepare_stable_promotion_evidence=true` 仍然不是 `stable_candidate`。下一步仍需按目标 Pack 准备完整的代表性脱敏样本、逐主体原始 Shadow 报告、多主体组合签认、网络 Connector 来源 Shadow、全部自动门、备份/回滚/权限/故障演练和独立发布复核，再运行 `promotion-assess`。

stable 证据 JSON 必须填写：

```json
{
  "pilot_shadow_series": {
    "reviewed_receipt_path": "/absolute/private/pilot-shadow-series-reviewed.json",
    "period_evidence_root": "/absolute/private/shadow-series-periods",
    "pipeline_runs_root": "/absolute/private/pipeline-runs"
  }
}
```

`sample.periods` 必须与收据期间完全一致且至少两个自然月；`shadow_close_reports` 和多主体组合签认必须逐文件匹配连续期目录。连续期收据本身不替代自动门、演练、Connector 来源 Shadow 或独立发布复核，也不会以原始路径或源内容哈希写入 stable promotion ledger。
