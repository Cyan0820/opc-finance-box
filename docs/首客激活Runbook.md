# 首客激活 Runbook

`activation-runbook-*` 为私有首客工作区提供一条可恢复的操作者进度链。它解决“做到哪一步、上次为什么停、下一步是什么”，但不执行 `commands.json`，也不把操作者自报的进度转换成税务、Connector、Shadow Close 或稳定晋级证据。

Runbook 用于带 `runbook/` 的 workspace schema v4 / v5。v5 将同一法律主体、同一 Connector Pack 的私有权限请求与回执作为共享操作范围；旧 v4 工作区继续按原命令合同验证。先创建并验证工作区：

```bash
opc-finance-box activation-init box.json /absolute/new/private-root \
  --period 2026-08 \
  --facts-as-of 2026-08-14 \
  --prepared-by activation-preparer

opc-finance-box activation-workspace-verify \
  box.json /absolute/new/private-root

opc-finance-box activation-runbook-status \
  box.json /absolute/new/private-root
```

## 记录进度

完成手工步骤后可以记录：

```bash
opc-finance-box activation-runbook-record \
  box.json /absolute/new/private-root \
  tax-workpaper-complete:my_entity \
  --outcome reported-complete \
  --actor local-preparer \
  --rationale "已填写私有底稿，仍等待权威校验和独立复核" \
  --evidence-reference private://tax/workpaper/checkpoint
```

执行 CLI 模板的副本后，成功必须附实际退出码 `0`：

```bash
opc-finance-box activation-runbook-record \
  box.json /absolute/new/private-root \
  tax-review:my_entity \
  --outcome reported-complete \
  --observed-exit-code 0 \
  --actor local-review-runner \
  --rationale "命令退出码为零，正式结论仍以 review rotation verifier 为准"
```

可用 outcome：

- `reported-complete`：操作者报告已完成；CLI 步骤必须附退出码 `0`；
- `reported-failed`：操作者报告命令失败；CLI 步骤必须附 `1-255` 的退出码；
- `blocked`：尚未尝试或无法继续，不得附退出码；
- `deferred`：明确延期，不得附退出码。

手工编辑步骤不能声称 CLI 退出码。未知 `step_id`、当前 Box 指纹不符、命令模板改变或 workspace 版本不符都会失败关闭。

## 恢复与校验

```bash
opc-finance-box activation-runbook-status \
  box.json /absolute/new/private-root

opc-finance-box activation-runbook-verify \
  box.json /absolute/new/private-root

opc-finance-box activation-workspace-status \
  box.json /absolute/new/private-root \
  --as-of 2026-08-14
```

`status` 返回逐步骤的最新 reported outcome、事件次数、下一条尚未报告完成的步骤和 hash-chain 头，不返回操作者、证据引用、财务值或私有路径。`verify` 重新检查：

- `0700` Runbook 目录和 `0600` ledger/lock；
- 逐行 sequence、`previous_hash` 与 `event_hash`；
- 当前 runtime fingerprint、完整 `commands.json` SHA-256 和逐步骤 fingerprint；
- outcome 与手工/CLI 动作类型、退出码的一致性；
- 所有事件均声明未改变财务状态、未执行外部动作、未持久化凭证或财务值。

跨进程追加使用文件锁；并发记录仍形成一条唯一连续链。任意历史行被改写后，后续 status/verify 都会拒绝读取。

## 权威边界

Runbook 的 `reported-complete` 永远保持 `authoritative_completion=false`。它不会改变 `activation-workspace-status` 的十一阶段结果，也不会：

- 使未复核税务底稿成为 approved applicability；
- 使 Connector fixture 或未 finalize baseline 成为真实 Shadow；
- 使 Pipeline attempt、Shadow Close 或 portfolio 自动通过；
- 跳过连续月份要求或生成 stable candidate；
- 授权入账、付款、法定关账或外部申报。

恢复工作时先看 Runbook，判断上次报告到哪里；是否真的可以进入下一阶段，仍以 `activation-workspace-status` 和各阶段 verifier 为准。
