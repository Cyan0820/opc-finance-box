# 连续 Shadow 月度 Runbook

首期 Activation Runbook 覆盖整条激活命令合同；进入第二个月后，每个 `pilot/period-workspaces/YYYY-MM/` 都有一份独立命令合同。月度 Runbook 为这一份合同提供可恢复的操作者进度账本，避免把上月状态复制到新月份，也避免用一张手工清单混淆多个法律主体和期间。

它只回答“操作者最后报告到了哪一步”，不回答“财务事实是否成立”。权威完成仍由对应的 readiness、handoff、Pipeline ledger、Shadow report、portfolio、observation、archive 和 series verifier 决定。

## 记录进度

```bash
opc-finance-box pilot-shadow-period-runbook-record \
  path/to/box.json \
  /absolute/private/activation-root \
  2026-09 \
  shadow-observation-verify \
  --outcome reported-complete \
  --observed-exit-code 0 \
  --actor period-operator \
  --rationale "验证命令本次返回 0；正式状态仍以重新验证为准" \
  --evidence-reference private://period/2026-09/observation-checkpoint
```

允许的 outcome 为：

- `reported-complete`：操作者报告该步骤已完成；命令型步骤若提供退出码，只接受 `0`；
- `reported-failed`：命令型步骤若提供退出码，只接受 `1–255`；
- `blocked`：存在阻塞；
- `deferred`：明确延后。

手工编辑步骤不能伪造命令退出码，未知步骤不能写入。`actor`、`rationale` 和 `evidence-reference` 只进入私有事件行；成功响应不会返回这些值或工作区路径。

## 状态与验证

```bash
opc-finance-box pilot-shadow-period-runbook-status \
  path/to/box.json /absolute/private/activation-root 2026-09

opc-finance-box pilot-shadow-period-runbook-verify \
  path/to/box.json /absolute/private/activation-root 2026-09
```

`status` 按当月 `commands.json` 原顺序投影每一步的最新 reported outcome、事件数、最新 sequence，以及下一条尚未报告完成的步骤。`verify` 重放整个账本并检查：

1. 当前 Box runtime fingerprint；
2. 当月工作区及精确前序归档仍有效；
3. 当月不可变命令合同 SHA-256；
4. 每个 step fingerprint、事件 sequence、previous hash 和 event hash；
5. 私有目录 `0700`、账本和锁文件 `0600`，且不存在符号链接或越界文件。

并发追加由跨进程文件锁串行化。把九月账本复制到十月、把一个 Box 的账本交给另一个 Box、修改命令合同或篡改任一历史事件，都会失败关闭。

## 不可跨越的边界

月度 Runbook 明确返回：

- `authoritative_completion=false`；
- `authoritative_completion_inferred=false`；
- `authoritative_period_completion_inferred=false`；
- `evidence_gates_unlocked=false`；
- `financial_state_changed=false`；
- `external_action_performed=false`。

因此，即使所有步骤都显示 `reported_complete`：

- 当期也不被视为已关账或已归档；
- 不会改变 `activation-workspace-status` 的十一阶段状态；
- 不会授权入账、付款、报税或任何外部动作；
- 不会取代独立复核人、原始证据或相应 verifier；
- 不会把人员、证据引用、路径、金额或 Pipeline attempt 投影到公共状态。

需要判断真实进度时，先运行 `pilot-shadow-period-runbook-status` 找到恢复位置，再运行该步骤自己的权威验证命令；不能把 Runbook 的 reported outcome 当作 verifier 结果。

## Workbench 只读月度视图

生产服务可以把完整 Activation Workspace 以只读方式挂载，并设置：

```bash
OPC_ACTIVATION_WORKSPACE_ROOT=/etc/opc-finance/activation-workspace
```

reader 随后可访问 `GET /api/box/pilot-shadow-periods`。请求不接受 Activation 路径参数；服务端逐月重验当前 Box、工作区、精确前序归档和 Runbook 链，并把不可变步骤合同投影为安全任务队列。每项任务只显示稳定任务类型、阶段、负责角色类型、必须分离的角色类型、所需证据类别、主体范围、完成渠道和 reported outcome；角色类型不是人员分配，证据类别也不表示证据已经存在或通过。

每一种任务还绑定版本化的安全方法 Playbook：预期工作产出类型、操作者检查清单类型和必须暂停的条件类型。它把“一个 CFO 会检查什么”做成可翻译、可测试的稳定知识合同，例如主体/期间范围、来源独立性、职责分离、逐项差异处置、台账完整性和自然月连续性。API 只返回这些类型 ID，Workbench 再映射为中英文说明；不返回私有文件内容，也不会把检查清单变成浏览器执行入口。任何月度步骤缺少 Playbook 时，整个任务索引失败关闭。

同一索引还返回 Pack 驱动的业务模型 CFO 控制覆盖层。游戏工作室会突出平台结算、退款/拒付/递延、项目贡献、授权/云资源预付释放与集中度；DTC 独立站会突出订单—支付—退款、履约/退货/库存截止、落地成本、处理商结算和商品利润；Marketplace 会突出订单—平台财务—库存三方范围、费用/保留款/结算、多仓截止与库存计价。每个已选 Connector 另有“能证明什么、不能证明什么”的来源边界；未知 fork Connector 只返回需补方法扩展的安全类型，不回显其 ID，也不能被误当成已有完整方法覆盖。

编译后的 `cfo-control-overlay.json` 把这套组合知识与当前 Box fingerprint 绑定，用户可以在自己的 fork 中继续修改；Workbench 只投影稳定类型 ID 的中英文说明。该覆盖层即使尚未挂载首客私有工作区也可读取，因为它只来自已验证 Pack 契约，不读取财务值、源记录、凭证或私有路径。

同一次组合还会生成 `cfo-metric-catalog.json`。Finance Core 固定提供现金生存周期、逾期应收、未勾稽现金项目和权威月结阻塞定义；游戏补充平台净收入、退款拒付率、递延收入、产品贡献、平台集中度与预付释放证据覆盖；DTC 补充净销售、退款退货率、订单到结算覆盖、商品贡献、库存周转天数与处理商结算差额；Marketplace 补充净结算、费率、保留款、三方匹配、库存范围覆盖和平台集中度。每项定义明确操作数、必需数据域、控制条件与聚合规则。缺输入、分母为零、历史期末证据不足或币种未显式折算时只能返回不可用，不能填零或推断；Workbench 只展示这些定义，不计算实际值。

需要形成实际经营指标候选时，优先使用标准 Pipeline/Service 返回的 `cfo_metric_operand_assembly`：它已经绑定来源结果指纹、主体、期间、币种和可选业务维度，并区分自动证明与待人工复核的控制。来源合同尚不足或币种不是本位币时会明确阻塞，不做补数或换汇。没有受支持来源时，操作者仍可把已标准化操作数提交给 `core.evaluate_cfo_metrics`；返回值与 `input_fingerprint` 应和操作数来源底稿、控制证据一起保存。浏览器只读指标字典不会自动调用执行器，控制 ID 的确认也不能代替证据复核。详见 [CFO 指标确定性计算](CFO指标确定性计算.md) 与 [CFO 指标操作数自动组装](CFO指标操作数自动组装.md)。

任务投影严格覆盖当前月度合同的每一种步骤与动作类型。出现未知步骤、动作类型漂移或主体后缀不属于当前 Box 时，整个索引失败关闭。API 不返回 argv、shell preview、runtime/command/chain hash、人员、理由、实际证据引用、路径、金额或 attempt，Workbench 也不提供执行和签认按钮。

空 Runbook 使用无副作用快照，不创建账本或锁；已有 Runbook 用共享只读文件锁重放，不以追加模式打开文件，因此兼容只读 bind mount。Workbench 的“当前安全任务”说明下一步应由哪类角色形成什么工作产出、检查哪些控制并在什么情况下暂停；显示“已报告完成”仍明确保持 `authoritative_period_completion=false` 和 `authoritative_verifier_required=true`，不能代替归档或任何原 verifier。
