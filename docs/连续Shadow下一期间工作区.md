# 连续 Shadow 下一期间工作区

首期 Activation Workspace 同时包含税务适用性、Connector 来源 Shadow、Pilot 准入和首月运行。第二个月开始不应复制整个首期目录，也不能把上月 readiness 或 handoff 改名后复用：原始验证器要求资料覆盖和运行登记精确绑定当月。

`pilot-shadow-next-period-init` 会在上月已通过完整观察复核并事务性归档后，生成一个只包含新期间工作的私有增量工作区。

## 生成下一期间

```bash
opc-finance-box pilot-shadow-next-period-init \
  path/to/box.json \
  /absolute/private/activation-root \
  --prepared-by period-preparer \
  --facts-as-of 2026-09-15
```

命令不接受用户指定的新月份，而是从 `pilot/series-periods/` 最新已验证归档推导下一个自然月。生成前必须同时满足：

- Activation Workspace 本身仍通过 Box 指纹、主体、权限、不可变文件和命令合同验证；
- 归档根只有 1–24 个权限为 `0700` 的连续 `YYYY-MM` 目录，且第一期与 Activation 首期一致；
- 每期文件为 `0600`，逐主体报告精确覆盖当前 Box，多主体还有当期 portfolio 复核；
- 所有归档均能对当前 Pipeline hash-chain 及当期原始工件重新验证；
- 最新观察的 `ready_for_next_shadow_period=true`，且未达 24 期 Pilot 上限。

最新期为 `2026-08` 时，目标位置固定为：

```text
private/activation-root/pilot/period-workspaces/2026-09/
├── readiness/
│   └── workpaper.json
├── handoff/
├── registrations/
├── shadow-baselines/
├── shadow-reports/
├── entity-reports/
├── portfolio/
├── observations/
├── runbook/
├── artifact-paths.env
├── commands.json
└── next-period-workspace-manifest.json
```

目录和文件全部分别使用 `0700` 和 `0600`。目标月已存在时拒绝覆盖；初始化任何一步失败会回滚本次新月份，不删除既有 Activation、归档或其他月份。

## 月度命令合同

`commands.json` 是当前 Box、主体、上期归档和新期间绑定的不可变命令合同，顺序包含：

```text
填写当期 readiness → 独立复核与税务轮换重验
→ 创建、填写并独立复核当期 handoff
→ 每主体完成当期 month-close Pipeline attempt 及复核
→ 当期 Shadow Run 登记与台账重验
→ 每主体人工 baseline、比对、复核与验证
→ 多主体 portfolio 组装与独立复核（仅多主体）
→ observation 组装、独立复核与全源重验
→ 事务性归档当期
→ 重验月度工作区、月度进度链与 Activation 状态
```

单主体不生成 portfolio 命令；多主体会在登记、观察和归档命令中自动枚举每个法律主体，不依赖操作者手工计数。所有决定仍使用失败关闭默认值，生成器不执行命令或创建复核。

## 重新验证

```bash
opc-finance-box pilot-shadow-next-period-verify \
  path/to/box.json \
  /absolute/private/activation-root \
  2026-09 \
  --as-of 2026-09-15
```

验证器重新检查 Activation Workspace、月度目录权限和符号链接、readiness 私有边界、不可变路径/命令文件，以及创建当时的精确前序归档指纹。当期后续成功归档、甚至继续新增更多月份，不会使历史月度工作区自动失效；但创建时的前序归档被替换、篡改或当前 Pipeline 台账无法复验时必须失败关闭。

CLI 输出不返回私有根、归档 hash、人员、证据引用、Pipeline attempt 或财务值。它不授权过账、付款、法定关账或申报。

## 恢复当月操作进度

每个月度工作区都有独立的 `runbook/`。操作者可以在不修改不可变 `commands.json` 的前提下，追加 `reported_complete`、`reported_failed`、`blocked` 或 `deferred` 进度事件；状态投影会给出下一条尚未报告完成的步骤：

```bash
opc-finance-box pilot-shadow-period-runbook-record \
  path/to/box.json \
  /absolute/private/activation-root \
  2026-09 \
  pilot-readiness-complete \
  --outcome reported-complete \
  --actor period-preparer \
  --rationale "当期底稿已填写，仍等待原验证器和独立复核" \
  --evidence-reference private://period/2026-09/readiness-checkpoint

opc-finance-box pilot-shadow-period-runbook-status \
  path/to/box.json /absolute/private/activation-root 2026-09

opc-finance-box pilot-shadow-period-runbook-verify \
  path/to/box.json /absolute/private/activation-root 2026-09
```

事件链绑定当前 Box fingerprint、当月不可变命令合同 SHA-256 和具体步骤 fingerprint。并发写入通过私有跨进程锁形成唯一 sequence 与 hash chain；复制到另一个月份、换 Box、改命令或篡改历史都会失败关闭。人员、理由和证据引用保留在 `0600` 私有账本内，CLI 只返回计数、步骤报告状态与链头。

Runbook 是恢复线索，不是事实来源。即使所有步骤都报告 `reported_complete`，它也不会创建复核工件、执行命令、改变 `activation-workspace-status`、解锁 evidence gate，或推断当期已经归档。完整边界见 [连续 Shadow 月度 Runbook](连续Shadow月度Runbook.md)。
