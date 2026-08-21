# Shopify Connector Pack

`connector.shopify` 是独立站 OPC 的只读订单财务证据连接器。provider 同时提供日常事实入口 `shopify.orders` 和月末证据入口 `shopify.monthly_order_evidence`，并原子标准化三组数据：

- `commerce.shopify_orders`：订单状态、履约状态、目的地国家代码及订单 MoneyBag。
- `commerce.shopify_transactions`：SALE、CAPTURE、REFUND 等支付交易的 kind、status 与 MoneyBag。
- `commerce.shopify_refunds`：Refund、退款商品、退款运费、Order Adjustment、关联退款交易及其 MoneyBag。

订单、交易和退款属于来源事实，不自动等同于收入、税额、COGS、贡献利润或会计凭证。字段边界依据 Shopify 官方的 [Order](https://shopify.dev/docs/api/admin-graphql/latest/objects/Order)、[Refund](https://shopify.dev/docs/api/admin-graphql/latest/objects/Refund) 和 [OrderTransaction](https://shopify.dev/docs/api/admin-graphql/latest/objects/OrderTransaction) 对象。

## 权限与版本

为单一店铺创建只读 Admin API access token，并通过服务端 secret manager 或环境变量提供：

```bash
export OPC_SHOPIFY_ADMIN_TOKEN='shpat_...'
```

请求中不能携带 token、Authorization、API key 或其他内联 secret。Pack 只接受一个严格的 `*.myshopify.com` 店铺域名，固定访问：

```text
https://{store}.myshopify.com/admin/api/2026-07/graphql.json
```

当前至少需要 `read_orders` scope。Shopify 默认只允许访问最近 60 天订单；需要更早历史时，应用还要取得 `read_all_orders`。权限含义见官方 [Access scopes](https://shopify.dev/docs/api/usage/access-scopes)。Pack 固定在稳定版本 `2026-07`，响应如果发生 API 版本 fall-forward 会失败关闭；版本支持周期与升级节奏见 [API versioning](https://shopify.dev/docs/api/usage/versioning)。

## 数据最小化与金额边界

GraphQL query 不读取客户姓名、邮箱、电话或完整地址，只保留配送国家代码作为目的地证据。Provider 不读取 `receiptJson`，因为该字段由支付网关决定且结构不稳定。

每个金额同时保留 `shop_money` 和 `presentment_money` 的十进制字符串及币种，不转成浮点数，也不跨币种汇总。订单至少分别保留原始总额、当前总额、实收总额和退款总额。缺失字段保持缺失；它们不会被补成零。

`shopify.summarize_order_activity` 只做以下确定性检查：

- 成功 SALE/CAPTURE 与 `totalReceivedSet` 是否一致。
- 成功 REFUND transaction、Refund 对象与 `totalRefundedSet` 是否一致。
- 订单、交易和退款业务键是否重复，是否存在孤儿记录。
- 目的地、取消状态、pending/failed transaction 等风险是否需要复核。

服务始终返回 `ready_for_commerce_margin=false`，直到另外提供批准的收入政策、退款拆分、库存成本、履约/运费、处理商结算和银行证据。

## 月末双窗口证据

历史月不能用“该月创建订单的当前值”直接求和。`shopify.monthly_order_evidence` 因此在月末执行两次有界查询：

- `created_at` 位于目标自然月的订单，用非 `current*` 原始金额形成当月新订单总体；
- `updated_at` 从月初到本次 `source_observed_at` 的订单，用于发现旧订单在目标月发生的退款；退款归属只看不可变的 `Refund.processedAt`。

两个总体按 order ID 去重；同一订单在两次读取之间发生变化会要求重跑。月末快照必须在下月开始后的 72 小时内捕获，因此它是运营中的 close-capture 合同，不是任意历史月份回填器。需要更早订单时仍必须取得 `read_all_orders`。

每个目标月退款必须完整返回 `refundLineItems`、`refundShippingLines`、`orderAdjustments` 和 `transactions` 四个连接；任一 `hasNextPage=true` 都会失败关闭，不能把前 100 项误当完整总体。税外退款组件必须与 `totalRefundedSet` 对平，且成功 `REFUND` transaction 必须与退款总额对平。Shopify 官方明确说明 Refund 对象存在并不证明款项已经退回，因此失败或待处理交易不会进入指标操作数。

只有 `taxesIncluded=false` 的订单可自动形成税外金额。含税或未知税制订单保持阻塞，等待 `tax_inclusive_policy_confirmed`；实物退货的授权和收货也不会从退款付款反推，继续等待 `return_authorization_and_receipt_scope_aligned`。

## 离线验收与 Fetch

不需要 Shopify 凭据即可运行 Pack 合同：

```bash
python packs/connectors/shopify/provider_contract_test.py \
  examples/boxes/cn_dtc_shopify_stripe_store.json
```

真实拉取请求示例：

```json
{
  "mode": "fetch",
  "default_entity_id": "cn_dtc_company",
  "shop_domain": "your-store.myshopify.com",
  "created_at_gte": "2026-08-01T00:00:00Z",
  "created_at_lt": "2026-09-01T00:00:00Z",
  "max_pages": 50
}
```

月末请求使用自然月 UTC 半开区间，并应在月末 72 小时内运行：

```json
{
  "mode": "fetch",
  "default_entity_id": "cn_dtc_company",
  "shop_domain": "your-store.myshopify.com",
  "interval_start": "2026-07-01T00:00:00Z",
  "interval_end": "2026-08-01T00:00:00Z",
  "max_pages": 50
}
```

Connector 使用 GraphQL `pageInfo.hasNextPage/endCursor` 游标分页，每页最多 250 条，并设置页数、重试、超时和响应大小上限。分页规则见 Shopify 官方 [GraphQL pagination](https://shopify.dev/docs/api/usage/pagination-graphql)。429/5xx 可重试；GraphQL errors、游标异常、版本 fall-forward 或任一订单的嵌套金额错误都会失败关闭。一个订单映射失败时，它的 transaction/refund 不会作为孤儿数据漏入批次。

## Shopify + Stripe 证据链

选择 `feature.shopify_stripe_order_to_cash` 后，可运行完整离线样例：

```bash
python -m src.cli pipeline examples/boxes/cn_dtc_shopify_stripe_store.json \
  examples/pipelines/shopify_stripe_daily_close_fixture.json
```

`dtc.shopify_stripe_daily_close` 依次运行 Shopify Orders、Stripe Balance Transactions、Stripe Payouts、批次质量门和四个确定性服务。跨平台核对必须显式提供：

- `shopify_transaction_id → stripe_source_object_id` 一对一证据链接；
- 每个币种的 `currency_minor_units`；
- Stripe Payout 对应的银行到账证据。

系统不会用金额或日期猜 Shopify 与 Stripe 的关联。输出即使全部匹配，也保持 `candidate_only=true`、`revenue_claim_prohibited=true`、`margin_claim_prohibited=true`，不自动核销、过账或执行外部动作。失败阶段、幂等键和恢复规则见 [Pipeline 运行与恢复](Pipeline运行与恢复.md)。

`dtc.shopify_stripe_month_close` 使用月末双窗口 Shopify 批次和严格相同 Unix 秒边界的 Stripe Balance Transactions。两个来源的主体或半开月份不一致时，在任何指标组装前停止。通过后只自动组装 `dtc_net_sales` 与 `dtc_refund_return_rate` 的标准操作数；税含政策、实物退货证据和显式换汇仍保持人工控制。

## 真实月结 Shadow 证据

生产接入不需要把含金额、店铺域名或原始 ID 的完整 Pipeline result 写到磁盘。先由独立准备人生成并填写来源计数与控制期望工作底稿：

```bash
opc-finance-box connector-shadow-baseline-init BOX.json \
  --pipeline dtc.shopify_stripe_month_close \
  --entity ENTITY_ID \
  --period 2026-07 \
  --prepared-by independent-source-preparer \
  --output private-shopify-monthly-workpaper.json

opc-finance-box connector-shadow-baseline-finalize BOX.json \
  private-shopify-monthly-workpaper.json \
  --output private-shopify-monthly-baseline.json
```

baseline 必须来自 Shopify 后台/受控导出和 Stripe 同窗导出，不能从待评估 Pipeline 输出反填。真实请求也不需要从空白 JSON 开始编写；生成器会绑定 Box、法律主体和目标月份，并自动计算 Shopify UTC 与 Stripe Unix 的同一半开自然月边界：

```bash
opc-finance-box shopify-monthly-shadow-request-init BOX.json \
  --entity ENTITY_ID \
  --period 2026-07 \
  --output private-shopify-monthly-live-request.json

# 在 0600 私有文件中填写店铺域名、实际币种精度和逐笔处理器链接后：
opc-finance-box shopify-monthly-shadow-request-verify BOX.json \
  private-shopify-monthly-live-request.json
```

模板初始 `template_only=true`，不含凭证、金额或伪造的处理器链接，不能直接联网。验证器要求 Shopify/Stripe 都绑定同一法律主体，拒绝测试订单、占位符、错月边界、重复链接、内联 secret、额外字段、权限不是 `0600` 的文件。验证摘要不回显店铺域名或 Shopify/Stripe 原始 ID。验证通过后再生成最小化 observation：

```bash
opc-finance-box shopify-monthly-shadow-observe BOX.json \
  private-shopify-monthly-live-request.json \
  --shopify-access-request private-shopify-access-request.json \
  --shopify-access-receipt private-shopify-access-receipt.json \
  --stripe-access-request private-stripe-access-request.json \
  --stripe-access-receipt private-stripe-access-receipt.json \
  --output private-shopify-monthly-observation.json

opc-finance-box connector-shadow-assess BOX.json \
  private-shopify-monthly-baseline.json \
  private-shopify-monthly-observation.json \
  --output private-shopify-monthly-assessment.json
```

observation 只保存主体、期间、两个来源的记录数、双人口/退款闭合/同窗/72 小时捕获等布尔控制，以及完整内存结果的 SHA-256；不保存订单金额、店铺域名、原始订单/交易/退款 ID，也不执行外部动作。它仍须由不同人员运行 `connector-shadow-review` 独立复核。

Shopify access receipt 默认 30 天有效；Workbench 会在到期前 7 天显示 `renewal_due` 和剩余天数，此时原有有界读取仍可运行。Admin token 轮换后会立即进入 `renewal_required`，并必须重新证明当前 app 安装、`read_orders` 和最小只读 scope。配置新 token 后运行 `connector-access-receipt-renew BOX.json private-shopify-access-request.json private-shopify-access-receipt.json --allow-network`。续期保留旧回执原字节归档，原子安装新回执，且不输出 token、店铺域名或归档路径。历史回执被篡改时会失败关闭，不允许通过“续期”掩盖。详见 [Connector 上线准备](Connector上线准备.md#回执续期与密钥轮换)。

该月结 profile 覆盖 `connector.shopify` 与跨处理器 Feature，但不会借用同窗 Stripe Balance Transactions 宣称整个 `connector.stripe` Pack 已覆盖，因为它没有验证 Payout→银行链路。完整 Shopify + Stripe Box 的 baseline plan 因此还会生成独立的 `stripe.daily_close` 工作底稿；配置 Wise 时再生成 `finance.bank_statement_close` 工作底稿。三份证据按职责组合，不能互相替代。

## 当前边界

- 当前按批次轮询，不使用 webhook。
- 当前只读取退款 line items，不读取完整销售 line items、inventory、fulfillment costs、Shopify Payments payouts 或税务申报数据。
- 当前只保留配送国家代码，它是目的地证据，不足以自动判定纳税义务。
- `include_test_orders=true` 只允许 demo Box；生产数据模式默认排除测试订单。
- live token、店铺与法律主体必须一对一配置并经过 `shopify_mapping_approval`。
