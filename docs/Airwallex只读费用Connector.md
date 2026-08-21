# Airwallex 企业卡已批准费用只读 Connector

`connector.airwallex` 把 Airwallex Spend 的已批准企业卡费用导入 `finance.expense_evidence`，供费用证据复核和后续人工会计映射使用。它不创建报销单、不替代费用审批、不下载附件、不标记同步状态，也不生成或过账凭证。

Airwallex 官方将 Expense API 标为 Beta；因此 Pack 固定为 `experimental`，即使离线契约全部通过也不能表述为 stable。接口固定使用日期版本 `2026-07-17`：认证只用于取得短期 access token，业务数据只调用 `GET /api/v1/spend/expenses` 与 `GET /api/v1/spend/expenses/{id}`；`/api/v1/spend/expenses/{id}/sync` 永不调用。

官方参考：[Expense API](https://www.airwallex.com/docs/api/spend/expenses)、[Spend webhook 事件](https://www.airwallex.com/docs/developer-tools/webhooks/listen-for-webhook-events/spend)、[签名与投递](https://www.airwallex.com/docs/developer-tools/webhooks/listen-for-webhook-events)、[API key 与 scoped key](https://www.airwallex.com/docs/developer-tools/api/manage-api-keys)、[API 日期版本](https://www.airwallex.com/docs/api/versioning)。

## 主体绑定与凭据

```bash
export OPC_AIRWALLEX_CLIENT_ID='...'
export OPC_AIRWALLEX_API_KEY='...'
export OPC_AIRWALLEX_WEBHOOK_SECRET='...'
export OPC_AIRWALLEX_ENTITY_BINDINGS_JSON='{
  "sg_store": {
    "legal_entity_id": "le_...",
    "account_id": "acct_...",
    "environment": "production"
  }
}'
```

每个 Box 法律主体必须绑定精确的 Airwallex `legal_entity_id` 与 `account_id`。认证使用 `x-login-as` 锁定账户；返回的每条 expense 必须同时匹配两个 ID，否则该行进入 rejected rows。绑定原值、Client ID、API key 与 access token都不会出现在 Connector 输出中。生产环境应使用只有 Spend Read 权限的 scoped key，不能为了连通性自动切换成写权限。

## 数据与隐私边界

输出只保留稳定哈希 ID、绑定原始 expense ID 与 `updated_at` 的记录版本 SHA-256、billing/transaction 币种与整数 minor units、来源状态和时间、脱敏商户名、业务用途存在标记、附件/行项目计数、accounting field 类型及主体/账户指纹。稳定业务键用于重抓后替换同一费用证据，版本指纹用于识别同一来源对象是否变化；两者都不暴露原始 ID。

输出拒绝保存原始 expense/card/attachment ID、卡号、人员邮箱、approver、comment、attendee、附件文件名/URL和 accounting field 值。`currency_minor_units` 必须显式覆盖所有币种；无法无损转换成整数 minor units 的金额会失败关闭，不能四舍五入。

## 增量同步

```bash
opc-finance-box connector-sync-plan \
  examples/boxes/sg_dtc_shopify_stripe_wise_airwallex_store.json \
  airwallex.approved_expenses \
  --entity sg_store \
  --stream primary-approved-expenses \
  --mode incremental \
  --window-start 2026-08-01T00:00:00Z \
  --window-end 2026-08-14T00:00:00Z \
  --request-base examples/connectors/airwallex-request-base.json
```

单次 incremental 的逻辑窗口最多 31 天，backfill 最多 366 天；checkpoint 只能在批次证据和 rejected rows 完成复核后提交。首次窗口从显式起点读取；提交首个 checkpoint 后，每个 Airwallex incremental 请求会把 API `from_created_at` 向前重叠 7 天，但逻辑 checkpoint 仍只前进到本次 `window.end`。同步计划 schema v2 的 `capture_policy` 同时记录逻辑起点、实际请求起点、配置/实际 overlap，并固定 `complete_update_capture_claimed=false`。

这层有界重抓能补捕最近 7 天内发生的审批、收据、用途或会计字段变化，但不能保证捕获更老记录的后续更新。签名 webhook inbox 已用于缩小这一缺口，定期重叠轮询仍是恢复网；两者都明确保持 `complete_update_capture=false`。删除事件已有 fail-closed tombstone 候选语义，但外部真实 Connector Shadow 尚未签认，因此 `airwallex_update_capture_review` 与 `airwallex_deleted_expense_tombstone_review` 仍是人工 gate，Pack 继续是 experimental。

## 签名 webhook durable inbox

Workbench 提供 `POST /api/webhooks/airwallex/spend`。该路径不使用 Box Bearer token；它只接受 Airwallex 的 `x-timestamp` 与 `x-signature`，并在解析 JSON 前对“原始时间戳字符串 + 原始请求体字节”执行 HMAC-SHA256 常量时间校验。时间戳容差固定为 300 秒，请求体上限 1 MiB。部署时必须在 TLS 反向代理后公开这一条路径，并按 Airwallex 文档配置生产/演示来源 IP allowlist；其他 API 仍使用原有角色策略。

收到事件后，系统会先把最小事件元数据追加到私有 `0600`、SHA-256 hash-chain 账本，再返回 `200`。相同 event ID 与相同 body 是幂等重复；相同 event ID 配不同 body 会以 `409` 失败关闭。`data` 和 `data.object` 两种官方载荷形态都支持，但 expense 的 `legal_entity_id` 与 `account_id` 必须反向匹配且只匹配一个 Box 主体。账本不保存完整请求体、商户、人员、评论、附件或金额；公开状态不返回原始 event/expense ID。为了异步重抓当前对象，原始 expense ID 只存在于受管私有账本，并随 `connector_sync` runtime store 备份。

HTTP 回执不访问 Expense API。单独的 worker 领取有 5 分钟 lease 的事件，再用主体绑定凭据执行精确的只读 `GET /api/v1/spend/expenses/{id}`：

```bash
opc-finance-box airwallex-webhook-process \
  examples/boxes/sg_dtc_shopify_stripe_wise_airwallex_store.json \
  --request-base examples/connectors/airwallex-request-base.json \
  --actor airwallex_webhook_worker \
  --webhook-root /var/lib/opc-finance/connector_sync/airwallex_webhooks

opc-finance-box airwallex-webhook-status \
  examples/boxes/sg_dtc_shopify_stripe_wise_airwallex_store.json \
  --webhook-root /var/lib/opc-finance/connector_sync/airwallex_webhooks

opc-finance-box airwallex-webhook-verify \
  --webhook-root /var/lib/opc-finance/connector_sync/airwallex_webhooks

opc-finance-box airwallex-webhook-quarantine-resolve \
  examples/boxes/sg_dtc_shopify_stripe_wise_airwallex_store.json RECEIPT_ID \
  --resolution retry \
  --actor airwallex_webhook_reviewer \
  --rationale '绑定和只读权限已修复，批准重新重抓' \
  --evidence-reference review://airwallex/RECEIPT_ID \
  --webhook-root /var/lib/opc-finance/connector_sync/airwallex_webhooks
```

为真实 schema v2 Shadow 留下最小化 assessor 输入时，一次只处理一个事件，并指定私有 observation：

```bash
opc-finance-box airwallex-webhook-process \
  examples/boxes/sg_dtc_shopify_stripe_wise_airwallex_store.json \
  --request-base examples/connectors/airwallex-request-base.json \
  --actor airwallex_webhook_worker \
  --limit 1 \
  --shadow-output private-airwallex-shadow-observation.json \
  --webhook-root /var/lib/opc-finance/connector_sync/airwallex_webhooks
```

observation 使用 `0600`、拒绝覆盖，只保留主体/期间、来源和控制计数、状态候选以及 webhook/refetch 证明；不保存金额、原始 expense ID 或完整来源行。它同时保存完整内存 Pipeline result 的 SHA-256，供 assessment 绑定，但不会代替独立来源 baseline 或私有来源证据。准备人仍必须从 Airwallex 后台/受控导出独立核对范围和计数，并在 schema v2 baseline 中证明该证据另行保留。

worker 不会只凭一个 expense ID 声称“来自 webhook”。它把私有 claim 转换为严格上下文，逐项绑定 receipt ID、事件名、事件时间、body SHA-256、expense ID SHA-256 与当前 Box fingerprint；Connector 在发起 GET 前全部复核，调用方也不能通过 request-base 覆盖这些字段。

重抓对象仍为 `APPROVED` 时进入原费用证据复核；若当前状态已变为 `REJECTED`、`ARCHIVED` 或未来新增的合法非批准状态，则只输出 `finance.expense_evidence_state_changes` 失效候选并强制人工复核，不会把未知状态自动解释成某种会计结论。对 `spend.expense.deleted`，只有上下文通过且精确 GET 返回 `404` 时才形成 `DELETED` tombstone 候选，并记录 `provider_absence_confirmed=true` 与 `signed_webhook_and_get_404`；其他事件的 `404` 不推断删除，而是失败后重试/隔离。乱序事件不会把旧载荷当成当前真相。单个事件连续三次重抓失败后进入 quarantine；操作者必须检查绑定、权限、删除语义和来源证据，再带 rationale 与 evidence reference 选择 `retry` 或 `dismissed`。任何路径都不会创建费用报销、推断会计映射、过账或付款。

离线契约：

```bash
python packs/connectors/airwallex/provider_contract_test.py \
  examples/boxes/sg_dtc_shopify_stripe_wise_airwallex_store.json
```

费用来源 Shadow 的 schema v1 演示 baseline 位于 `examples/shadow/sg_airwallex_expense_connector_baseline.json`。它只演示来源计数、费用缺口控制和职责分离字段，stable promotion 会明确拒绝它。真实验收必须使用 `connector-shadow-baseline-init` 创建 Box/主体/期间绑定工作底稿，独立填写来源计数并经 `connector-shadow-baseline-finalize` 封存为 schema v2 `real_anonymized` baseline。v2 还强制期望并验证网络 refetch、`signed_webhook_then_read_only_refetch` 来源标记和已验证的 webhook context；fixture、普通窗口 fetch 或手工拼出的 refetch 结果不构成 update-capture Shadow。详见 [Connector 来源 Shadow 验收](Connector来源Shadow验收.md)。
