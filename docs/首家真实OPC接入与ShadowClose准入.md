# 首家真实 OPC 接入与 Shadow Close 准入

这套流程把“样板能跑”与“真实公司可以开始受限影子运行”分开。它适用于游戏、电商、独立站及多主体 Box，会从当前 Pack 组合生成精确的主体、行业资料域和网络 Connector 范围。

## 1. 生成私有工作底稿

```bash
opc-finance-box pilot-readiness-init examples/boxes/global_game_studio.json \
  --period 2026-07 \
  --prepared-by finance-preparer \
  --output private/pilot-readiness-workpaper.json
```

输出文件以 `0600` 独占创建，不覆盖旧文件。它只接受不透明的 `evidence://`、`workpaper://` 等证据引用，不收凭证值、银行/店铺原始账号、税号或财务金额。

逐主体完成所有资料域：基础域包含主体档案、期初 Trial Balance、General Ledger、银行、收入和费用；游戏 Box 增加渠道结算与经营 KPI；Commerce Box 增加订单、支付结算、退款退货、履约、库存及适用时的进口到岸成本；多主体 Box 增加内部交易范围。

已选网络 Connector 必须逐项标记为：

- `ready`：凭证引用已配置、provider contract 已通过、只读窗口已限定；
- `approved_file_fallback`：暂不使用网络凭证，已由独立复核人接受文件导出替代。

工作底稿中的数据操作人、映射复核人、checkpoint owner 和人工基准负责人按职责分离配置。候选资料、凭证存在或 Connector 凭证已配置，都不会自动视为准入完成。

## 2. 独立复核

```bash
opc-finance-box pilot-readiness-review examples/boxes/global_game_studio.json \
  private/pilot-readiness-workpaper.json \
  --actor finance-control-reviewer \
  --rationale "已复核逐主体只读映射、证据覆盖和首期影子关账计划" \
  --evidence-reference advisor://pilot/readiness-review \
  --output private/pilot-readiness-reviewed.json
```

复核人必须与准备人和数据操作人分离。系统重新计算当前 Box 的主体、资料域、网络 Connector 与 plan fingerprint；主体错配、范围缺失、可疑占位符、职责重叠或输出文件已存在都会 fail closed。

签认从 UTC 复核日开始计算生命周期：第 60 天进入 `review_due`，应安排重新核对；超过第 90 天进入 `expired`，新的一期受限 Shadow Run 保持关闭。Box 指纹、主体范围、资料域要求、网络 Connector 选择或来源映射/控制变化时，不等待期限到达，旧签认会立即因绑定不匹配而失效。

## 3. 安全验证

```bash
opc-finance-box pilot-readiness-verify examples/boxes/global_game_studio.json \
  private/pilot-readiness-reviewed.json
```

如需同时验证 Tax Pack 适用性目录激活状态，两个参数必须成对提供：

```bash
opc-finance-box pilot-readiness-verify examples/boxes/global_game_studio.json \
  private/pilot-readiness-reviewed.json \
  --tax-review-dir /absolute/private/tax-reviews \
  --tax-registry-receipt /absolute/private/tax-registry-receipt.json \
  --as-of 2026-08-14
```

标准输出只包含指纹、计数、布尔门禁和安全错误摘要，不返回 actor、证据引用、路径、原始账号、税号、凭证或财务数值。

可用同一观察日生成安全告警候选：

```bash
opc-finance-box pilot-readiness-alerts examples/boxes/global_game_studio.json \
  --review private/pilot-readiness-reviewed.json \
  --as-of 2026-10-13
```

命令只返回稳定告警 ID、严重级别、复核/失效日期和布尔门禁；默认不安装计划任务，也不发送通知。编译 `job-plan.json` 中的每日轮换建议同样保持 `enabled=false`，部署者必须先配置 IANA 时区、观察日、接收人、去重和升级责任人。

## 4. 激活 Doctor 与工作台只读投影

生产运行时通过服务器私有环境变量只读挂载签认：

```bash
export OPC_PILOT_READINESS_REVIEW=/absolute/private/pilot-readiness-reviewed.json
opc-finance-box doctor examples/boxes/global_game_studio.json \
  --pilot-readiness-review "$OPC_PILOT_READINESS_REVIEW"
```

该文件必须是绝对路径、普通文件、POSIX `0600`，容器或 systemd 中只读挂载。Doctor 的 `pilot.readiness_activation` 在 `current` 时通过，在 `missing` / `review_due` 时告警，在 `invalid` / `expired` 时阻断新的受限 Shadow Run。reader 可通过 `GET /api/box/pilot-readiness` 查看逐主体资料域计划、网络 Connector 范围、安全生命周期摘要和告警候选；API 不接收客户端文件路径，也不返回私有文件内容、执行人、证据引用、账号、税号或金额。

## 边界

`ready_for_bounded_shadow=true` 仅表示可以开始一个主体隔离、只读、限定期间的 Shadow Close。它不表示 Shadow Close 已完成，也不表示法定关账、过账、付款、税务日历释放或外部申报已经获批。后续仍需运行 Shadow Close 比较与独立签认，并按适用地区完成真实税务登记与专业复核。
