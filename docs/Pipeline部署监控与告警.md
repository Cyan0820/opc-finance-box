# Pipeline 部署监控与告警

本手册面向把 OPC Finance Box 放进 cron、systemd timer、容器 Job 或云调度器的部署者。Box 只导出只读指标和派生告警状态，**不会**自行安装调度、发送消息、创建工单或确认财务结果。通知路由、值班表和升级策略仍由部署者控制。

## 上线前基线

1. 服务默认绑定 `127.0.0.1`；远程访问必须启用 role policy、TLS、网络访问控制和反向代理。
2. `OPC_FINANCE_PIPELINE_SCHEDULE_FILE` 指向权限受限的 schema v2 计划；每个启用 job 同时锁定 request 内容指纹与人工审批指纹。
3. 调度执行 principal 只有 operator；监控抓取 principal 只有 reader；财务 gate 由独立 reviewer 处理。
4. `.opc-finance-data/pipeline-runs` 位于持久加密卷，进入备份、恢复演练和容量监控。
5. 先运行 `pipeline-schedule-inspect` 和 Shadow，确认没有 blocker，再接入外部调度器。

Box 不替部署者写入 crontab。外部调度器建议每 5 分钟调用一次，实际 cadence 仍由计划文件决定：

```bash
opc-finance-box pipeline-schedule-run BOX.json schedule.json \
  --runs-root /var/lib/opc-finance/pipeline-runs \
  --actor scheduler_operator
```

同一 occurrence 由原子租约去重。调度命令退出成功只表示请求被安全处理；告警系统仍需读取 outcome、复核队列与 ledger 完整性。

Connector access 回执使用独立的、默认关闭的 daily 候选任务 `connector.access_receipt_rotation_alerts`。它调用 `connector-access-alerts BOX ACTIVATION_ROOT --as-of YYYY-MM-DD`，按 Pack + 主体 + 状态输出稳定 alert ID，覆盖未初始化、临期、到期/凭据轮换和请求/回执完整性异常。命令不访问服务方、不发送通知，也不会执行续期；部署者仍须配置时区、接收人、去重/恢复策略和升级责任人。

## 只读导出

CLI JSON：

```bash
opc-finance-box pipeline-observability BOX.json \
  --schedule schedule.json \
  --runs-root /var/lib/opc-finance/pipeline-runs
```

CLI 也可用 `--prometheus` 返回 text 0.0.4 内容的 JSON 包装，方便脚本保存。服务端 reader endpoint 可直接供采集器读取：

- `GET /api/box/pipeline-observability`：JSON 状态、责任人和派生告警；
- `GET /api/box/pipeline-observability?format=prometheus`：`text/plain; version=0.0.4` 指标。

Prometheus 输出故意不带 `job_id`、主体、pipeline、actor、路径或财务标签，避免高基数和业务信息泄漏。当前指标包括：

- `opc_finance_pipeline_ledger_integrity`；
- `opc_finance_pipeline_schedule_jobs{status}`；
- `opc_finance_pipeline_runs_24h{status}`；
- `opc_finance_pipeline_review_gates{state}`；
- `opc_finance_pipeline_alerts{severity}`。

JSON 会返回 job 责任人，但不包含原始 request、Connector 响应、财务明细、secret 值或计划文件路径。导出过程不 dispatch、不领取租约、不发送通知。

24 小时运行数与复核队列各最多投影最近 500 条，JSON 中的 `counts_may_be_truncated` 会明确标记是否触顶。高吞吐部署应同时从集中日志/指标存储计算长期趋势，不能把这个本地有界快照当成完整数据仓库。

## 建议告警规则

| 条件 | 级别 | 首要动作 |
|---|---|---|
| `ledger_integrity != 1` 或 endpoint 返回 409 | critical | 停止调度与复核写入，保全账本副本，执行完整 hash-chain/备份验证 |
| `retry_exhausted`、`blocked_non_retryable` | critical | 由 `alert_owner` 检查 provider、确定性规则与证据，不做盲目重跑 |
| `blocked_configuration` | warning | 修复 Box/主体/request/审批指纹；重新 preflight 和审批 |
| `missed_window` | warning | 检查外部调度器、时区/DST 与运行窗口；不要静默补跑 |
| stale review gate > 24h | warning | 通知独立 reviewer；不得由 operator 代批 |
| `retry_wait` 长期不消失 | warning | 检查调度器是否持续运行及 retry delay，避免人工并发重放 |

告警至少连续两个采集周期成立后再通知，可降低部署重启造成的瞬时噪声；ledger integrity 失败例外，应立即升级。

## 处置 Runbook

### 完整性失败

1. 暂停外部 scheduler，不删除、不截断、不手改 JSONL。
2. 保存当前 ledger 与文件权限元数据，运行 `pipeline-runs-verify`。
3. 验证最近一次独立备份；若需恢复，只能恢复到空目标目录。
4. 对比 chain head、event count 和部署变更，记录事故时间线。
5. 根因确认、账本重新建立信任且 reviewer 批准后再恢复调度。

### 配置或审批指纹失败

1. 运行 `pipeline-request-fingerprint` 取得当前 request 摘要。
2. 对比 job 中批准的 `request_fingerprint`；确认变化是授权变更而非替换。
3. 保持 `enabled=false`，更新内容指纹，执行 inspect/preflight。
4. 由独立复核人生成新的 `approval_fingerprint`、时间与身份，再开启。

### 重试耗尽或非重试失败

1. 查看安全的 attempt 摘要、`failure_code` 与 blocker，不把原始凭证复制进告警系统。
2. 确认问题属于配置、网络/provider、质量门还是代码异常。
3. 只有 Pipeline 明确标记 `retryable=true` 才等待自动重试；其他失败必须人工修复。
4. 需要重放时创建新的受控窗口或批准计划，不绕过 occurrence/claim 记录。

### 复核队列积压

1. 按 gate 和主体分派给有权 reviewer，operator 不得兼任同一 attempt 的审批人。
2. 复核证据只引用受控位置，不把财务明细或 secret 粘贴进 rationale。
3. 即使全部 gate approved，结果仍只是 release candidate，不代表付款、过账或申报授权。

## 日常运营节奏

- 每 5 分钟：采集指标和 JSON 告警状态；
- 每日：检查 missed/retry/blocked、stale review 和外部 scheduler 最近成功时间；
- 每周：验证 ledger hash chain、备份可读性和存储容量；
- 每月：在空目标执行恢复演练，复核 operator/reviewer/reader 权限和通知路由；
- 每次 Box、request、provider 或 schedule 变更：重新生成指纹、preflight、Shadow 与独立审批。

指标只能说明控制面状态，不能证明数据源完整、财务判断正确、税表已提交或资金动作已完成。生产监控应与 Connector provider 指标、外部调度器心跳、证据存储和实际回执监控组合使用。
