# Connector 增量同步控制

网络 Connector 的单次分页和重试不能替代跨运行同步状态。OPC Finance Box 为声明 `sync_window` 的只读 Connector 提供独立控制面，目前内置 Stripe Balance Transactions、Stripe Payouts、Shopify Orders、Wise Balance Statement 和 Airwallex Approved Expenses。

控制面只保存窗口、批次 ID、数据量、重试/限流摘要、质量状态和 hash-chain 审计事件；不保存请求正文、响应正文、access token 或第三方 Authorization header。第三方分页 cursor 只在一次受限调用中使用，不跨运行持久化。跨运行 checkpoint 是明确的时间窗口高水位。

## 1. 生成计划

先准备不含 secret 的 Connector 固定参数，例如：

```json
{
  "shop_domain": "opc-demo.myshopify.com",
  "max_pages": 50
}
```

首次增量同步需要显式起止时间：

```bash
opc-finance-box connector-sync-plan BOX.json shopify.orders \
  --entity cn_dtc_company \
  --stream primary-orders \
  --mode incremental \
  --window-start 2026-08-01T00:00:00Z \
  --window-end 2026-08-02T00:00:00Z \
  --request-base shopify-request-base.json \
  --sync-root /var/lib/opc-finance/connector_sync \
  > plan-envelope.json
```

CLI 的标准输出是 `{ok,result}` envelope。执行前应把其中 `result` 保存为独立 plan JSON。plan 绑定 Box fingerprint、Connector、主体、stream、逻辑窗口、当前 checkpoint event hash 和请求 SHA-256；修改任一字段都会失败。当前生成 schema v2，并增加严格的 `capture_policy`：它分别记录 checkpoint 的逻辑起点、实际来源请求起点、配置/实际 overlap 和 `complete_update_capture_claimed=false`。旧 schema v1 plan 仍可执行，但会被明确记录为 `legacy_contiguous_window`，不会获得完整更新捕获声明。

已有 checkpoint 后，下一份 incremental plan 的逻辑窗口会强制从已提交的 `window_end` 开始。操作者可以省略 `--window-start`；即使提供，也必须与 checkpoint 完全相等。Connector 可以声明有界 `incremental_overlap_seconds`，此时实际来源请求会向前重抓，但 checkpoint 不回退；重复来源对象必须依赖稳定业务键和记录版本控制处理。重叠只降低漏捕概率，不能替代 provider webhook/update-time 能力。单次 incremental 默认最多 31 天，backfill 最多 366 天，具体限制写入 `connector-catalog.json` 和 `connector-sync-policy.json`。

## 2. 执行但不自动推进水位

```bash
opc-finance-box connector-sync-run BOX.json plan.json \
  --actor connector_operator \
  --sync-root /var/lib/opc-finance/connector_sync
```

执行使用 Connector Pack 固定的 HTTPS endpoint、API version、分页和响应大小边界。429 与 5xx 只在上限内重试；数字秒格式的 `Retry-After` 最多接受 30 秒，并记录累计等待、限流次数和是否采用 provider 提示。响应正文不会进入错误消息。

网络窗口执行结束后仍不会推进 checkpoint。零记录窗口可以是完整窗口；是否存在记录和窗口是否完成是两个不同事实。只要发生 rejected row、duplicate business key、网络/协议失败或窗口不完整，该 attempt 就进入 quarantine。

## 3. 人工提交 checkpoint

先查看候选和隔离队列：

```bash
opc-finance-box connector-sync-status BOX.json \
  --sync-root /var/lib/opc-finance/connector_sync
```

对来源计数、归一化计数、异常和 shadow reconciliation 留下证据后，再提交：

```bash
opc-finance-box connector-sync-commit BOX.json ATTEMPT_ID \
  --actor connector_reviewer \
  --rationale "来源计数、拒绝项和财务核对均已复核" \
  --evidence-reference shadow://shopify/2026-08-02 \
  --sync-root /var/lib/opc-finance/connector_sync
```

只有完整 incremental attempt 能提交；backfill、失败或有拒绝/重复的 attempt 永远不能推进生产水位。如果其他操作者先推进了同一 stream，旧 plan 和旧 attempt 都会因 checkpoint event hash 不一致而失败。

## 4. Backfill 和失败隔离

Backfill 必须显式给出窗口：

```bash
opc-finance-box connector-sync-plan BOX.json stripe.balance_transactions \
  --entity cn_dtc_company \
  --stream historical-balance-transactions \
  --mode backfill \
  --window-start 2025-01-01T00:00:00Z \
  --window-end 2025-02-01T00:00:00Z \
  --sync-root /var/lib/opc-finance/connector_sync
```

Backfill 复用相同分页、质量和隔离控制，但无论结果如何都不改变 incremental checkpoint。

隔离 attempt 只能由具名操作者记录为 `dismissed`，或链接到同 stream 的完整 replacement attempt：

```bash
opc-finance-box connector-sync-quarantine-resolve BOX.json FAILED_ATTEMPT_ID \
  --actor connector_reviewer \
  --resolution replaced \
  --replacement-attempt-id COMPLETE_ATTEMPT_ID \
  --rationale "修复映射并完整重跑同一窗口" \
  --sync-root /var/lib/opc-finance/connector_sync
```

隔离 resolution 不删除失败证据，也不会替 replacement 提交 checkpoint。

## 5. 完整性、备份与边界

```bash
opc-finance-box connector-sync-verify \
  --sync-root /var/lib/opc-finance/connector_sync
```

控制 ledger 是追加式 SHA-256 chain，能发现修改和断链，但不是数字签名或 WORM。`connector_sync` 自 runtime data layout v2 起受管，当前 layout v3 仍会将它随整目录离线备份和空目标恢复。旧 layout 目录必须停 workbench 与 scheduler、创建并验证完整备份，再执行显式 `runtime-data-migrate`。

该控制面证明的是“哪段只读来源窗口被提取、复核和推进”，不证明总账已过账、期间已关账、税务已申报，也不授权任何第三方写操作。
