# Pipeline 调度与可观测性

OPC Finance Box 内置的是一个**受控调度执行器**，不是常驻队列，也不会替部署者安装 cron。部署者决定由 cron、systemd timer、容器 Job 或云调度器何时调用；Box 负责严格校验计划、计算当前 occurrence、原子领取执行租约、运行只读 Pipeline，并把结果写进现有的追加式审计账本。

## 控制边界

- 计划默认关闭；启用的 job 必须同时提供 `approved_by` 和带时区的 `approved_at`。
- `operator` 必须与本次 CLI actor 或经认证 API principal 完全一致。
- `request_file` 必须是调度文件所在目录内的相对 JSON 路径，不能使用绝对路径、`..` 或目录外 symlink。
- 运行前一定执行 Pipeline preflight；未知主体、未启用 Pipeline、占位符或 JSON 中疑似 secret/token 字段都会阻塞。
- 调度只运行当前 Box 已启用且声明 `external_actions=false` 的 Pipeline。成功仍只是待复核候选，不代表入账、付款、报税或外部授权。
- HTTP 不接受客户端提供的“当前时间”；`--now` 只用于 CLI 的确定性测试和有审计责任的运维复核。

编译产物中的 `pipeline-schedule-template.json` 是编辑模板，故意不能直接执行。示例 `examples/pipelines/shopify_stripe_daily_schedule_demo.json` 也是关闭状态；复制后先替换责任人、配置真实 request、完成 Shadow 与勾稽，再由独立负责人审批并开启。

## Schedule schema

根对象固定为：

```json
{
  "schema_version": 2,
  "timezone": "Asia/Shanghai",
  "jobs": []
}
```

每个 job 固定包含：

- `job_id`：计划内唯一、稳定的业务标识；
- `enabled`：默认 `false`；
- `pipeline_id`、`entity_id`、`request_file`：必须与 request 及当前 Box 一致；
- `request_fingerprint`：request 规范化 JSON 的 SHA-256；启用与审批前必填，并由 `approval_fingerprint` 一并锁定；
- `cadence`：支持 `daily`、`weekly`、`monthly`，时间均为 IANA 时区下的 `HH:MM`；月度日期限定 1–28，避免月底隐式顺延；
- `execution_window_minutes`：错过窗口后显示 `missed_window`，不会静默补跑；
- `max_attempts`、`retry_delay_minutes`：只有 Pipeline 明确返回 `retryable=true` 才可重试；
- `lease_seconds`：并发执行租约，范围 60–3600 秒，应长于正常单次运行时间；
- `operator`、`alert_owner`：执行人与异常负责人；
- `approved_by`、`approved_at`、`approval_fingerprint`：调度放行证据。指纹锁定除 `enabled` 与审批字段外的全部运营配置；`request_file` 引用、cadence、主体、重试或责任人变化后旧审批立即失效。

检查结果还包含实际读取到的 `observed_request_fingerprint`。它必须与已批准的 `request_fingerprint` 一致。执行器会在 preflight 后重新读取计划与 request：计划指纹变化会令整次命令失败，request 指纹变化会产生 `request_changed` 且不会领取租约或 dispatch，从而关闭检查到执行之间的替换窗口。

`schema_version: 1` 只保留关闭状态的只读迁移兼容；旧版 job 不具备 request 内容绑定，因此不能审批或启用。迁移时改为版本 2、生成 request 指纹，再重新生成 job 审批指纹。

DST 策略固定为：不存在的本地时间跳过；重复的本地时间使用第一次（`fold=0`）。因此跨 DST 的业务建议选不会落入切换窗口的本地时间，并监控 `missed_window`。

## CLI

只读检查，不访问 Connector：

```bash
opc-finance-box pipeline-schedule-inspect BOX.json schedule.json \
  --runs-root .opc-finance-data/pipeline-runs
```

先在不返回 request 内容的前提下生成其审批指纹：

```bash
opc-finance-box pipeline-request-fingerprint request.json
```

把结果写入 job 的 `request_fingerprint`。首次配置时保持 `enabled=false` 且三个审批字段为 `null`，再执行只读检查；检查结果会返回 `expected_approval_fingerprint`。复核人确认当前配置、request 内容指纹与 preflight 后，把该值写入 `approval_fingerprint`，同时填写 `approved_by`、`approved_at` 并开启 job。不要手工复用另一 job、旧 request 或旧版本计划的指纹。

领取租约并执行当前 due/retry_due job：

```bash
opc-finance-box pipeline-schedule-run BOX.json schedule.json \
  --runs-root .opc-finance-data/pipeline-runs \
  --actor scheduler_operator
```

可用 `--job-id` 限定一个 job。CLI 返回 `selected`、`dispatched`、`ready`、`blocked` 计数；退出成功只表示调度命令被安全处理，不表示每个财务结果都 ready。告警系统必须检查 outcome status 和后续 review queue。

外部调度器可以每 5 分钟调用一次 `pipeline-schedule-run`。同一 occurrence 的并发调用只有一个能取得租约；完成的 occurrence 不会重复 dispatch。进程在领取后崩溃时，租约到期才允许重新领取。

## HTTP 控制面

服务进程通过环境变量引用计划文件：

```bash
export OPC_FINANCE_PIPELINE_SCHEDULE_FILE=/secure/config/pipeline-schedule.json
```

- `GET /api/box/pipeline-schedule`：reader 可查看 due、retry、lease、missed 与 blocker 状态；不返回计划文件路径，不 dispatch。
- `POST /api/box/pipeline-schedule/run`：operator 可提交 `{ "job_id": "可选" }`。认证开启时 actor 一律来自 principal，不能由请求体覆盖。

不要把 HTTP endpoint 直接暴露到公网。远程部署仍需 TLS、网络边界、Secret Manager、进程监督、集中日志与异地备份。

## 审计、重试与告警

租约和结果写入与人工复核相同的 `pipeline_runs.jsonl` SHA-256 hash chain：

1. `PIPELINE_SCHEDULE_CLAIMED` 记录 Box fingerprint、job、occurrence、执行人和租约期限；
2. `PIPELINE_RUN_RECORDED` 消费 claim，记录无 secret 的 Connector/Service 摘要、`schedule_occurrence_id` 与失败边界；
3. `PIPELINE_RUN_REVIEWED` 继续由独立 reviewer 追加复核决定。

同一 occurrence 的重试共享 schedule idempotency key，并通过 `duplicate_of_attempt_id` 和 attempt number 串联。异常抛出会形成 `dispatch_exception` 的非重试结果，避免对未知失败自动循环；修复配置或代码后由操作者明确决定新的运行窗口或受控重放。

至少为以下状态配置告警：

- `blocked_configuration`、`blocked_non_retryable`；
- `retry_wait` 超过预期、`retry_exhausted`；
- `missed_window`；
- lease 长时间未消费；
- ready attempt 的 review gate 长时间未完成；
- hash chain 校验、备份或恢复演练失败。

Pipeline ledger 保存的是控制摘要，不是原始数据仓库，也不是 WORM。生产环境应把账本与业务证据分别加密备份，并定期执行 `pipeline-runs-verify`、备份校验和空目标恢复演练。
