# ShipBob 只读仓配 Connector

`connector.shipbob` 是 Commerce / 独立站 Box 的可编辑 3PL 证据 Pack。它把 ShipBob 订单内嵌发货、履约账单和退货处置标准化为一个法律主体内的确定性复核候选，不确认收入、不改变库存、不生成凭证，也不调用任何写接口。

当前 Pack 版本为 `0.1.0`，成熟度为 `experimental`。API 契约按 2026-08-16 核验并固定为 ShipBob `2026-07`：权限探测使用 `GET /2026-07/channel`，订单读取使用 `GET /2026-07/order`，退货读取使用 `GET /2026-07/return`。订单 `Page` 与退货 `Cursor` 都按本地构造的有界整数页码递增；响应 `next` URL 只作为“还有下一页”的信号，其主机、路径和查询值从不被跟随或复用。生产和 Sandbox 只允许 `api.shipbob.com` 与 `sandbox-api.shipbob.com` 两个固定主机。

## 官方契约依据

- [认证、PAT、OAuth 与应用权限](https://developer.shipbob.com/auth)
- [Get Channels](https://developer.shipbob.com/api/channels/get-channels)
- [Get Orders](https://developer.shipbob.com/api/orders/get-orders)
- [Get Return Orders](https://developer.shipbob.com/api/returns/get-return-orders)
- [Orders、Shipments、Returns 与 Inventory 概念](https://developer.shipbob.com/concepts)

单一 ShipBob 用户自己使用时可在 ShipBob 后台创建 Personal Access Token；面向多个 ShipBob 用户交付时应采用官方 OAuth 流程。新部署使用 `OPC_SHIPBOB_ENTITY_BINDINGS_JSON`：每个法律主体必须提供 `environment`、正整数 `channel_id` 和 `token_env`，token 则通过该动态环境别名单独注入。绑定 JSON、请求 JSON、Box 配置、编译产物、日志和 Pipeline 结果都不得包含 token。access gate 要求服务方返回的 scope 集合**精确**为 `channels_read`、`orders_read`、`fulfillments_read`、`returns_read`；多余读取或任何写 scope 都失败关闭。旧 `OPC_SHIPBOB_ACCESS_TOKEN` 只保留单主体 Provider fetch 迁移兼容，不能生成当前 access receipt。

示例绑定只应存在于受保护环境或 Secret Manager，不进入仓库：

```json
{
  "us_dtc_company": {
    "environment": "production",
    "channel_id": 123456,
    "token_env": "OPC_SHIPBOB_US_DTC_TOKEN"
  }
}
```

## 数据最小化与会计边界

标准数据集只有：

- `commerce.shipbob_orders`
- `commerce.shipbob_shipments`
- `commerce.shipbob_returns`
- `commerce.shipbob_return_items`

源订单、发货、退货、库存项、渠道、仓库和运单标识全部哈希；客户姓名、邮箱、电话、完整地址、原始运单号、条码、图片和处置说明不会进入标准数据集。目的地只保留国家代码，SKU 和有限状态文本用于确定性业务核对。

ShipBob fulfillment invoice 只是履约成本来源证据，不是应付账款、费用或已批准凭证。`Restock`、`Quarantine`、`Dispose` 等动作只是退货处置候选；没有独立库存与会计复核时不得自动增加库存、确认损失或过账。当本月退货引用上月发货时，Pipeline 会保留跨窗口引用异常但不会错误阻断整批；同一窗口内发货找不到订单、退货明细找不到退货主记录仍会失败关闭。

## 装配与离线验证

创建美国 Shopify + Stripe + ShipBob 独立站样板：

```bash
python -m src.cli create \
  examples/box_specs/shopify_stripe_shipbob_us_c_corp.json \
  --output outputs/us-dtc-shipbob.json
python -m src.cli validate outputs/us-dtc-shipbob.json
python packs/connectors/shipbob/provider_contract_test.py outputs/us-dtc-shipbob.json
```

运行完整离线 Pipeline：

```bash
python -m src.cli pipeline \
  examples/boxes/us_dtc_shopify_stripe_shipbob_c_corp.json \
  examples/pipelines/shipbob_fulfillment_close_fixture.json
```

fixture 中故意包含客户邮箱、地址和原始运单号，以回归验证这些字段不会出现在 Connector 批次和 Pipeline 结果中。它是虚构演示，不是 Shadow、财务证据或 stable 晋级依据。

## 真实读取

先为当前主体生成、探测并验证一份 `0600` access receipt；请求只声明 `entity_environment_binding`，不复制 channel 或 token：

```bash
opc-finance-box connector-access-request-init \
  examples/boxes/us_dtc_shopify_stripe_shipbob_c_corp.json \
  --pack connector.shipbob --entity us_dtc_company \
  --output private-shipbob-access-request.json

opc-finance-box connector-access-probe \
  examples/boxes/us_dtc_shopify_stripe_shipbob_c_corp.json \
  private-shipbob-access-request.json --allow-network \
  --output private-shipbob-access-receipt.json

opc-finance-box connector-access-receipt-verify \
  examples/boxes/us_dtc_shopify_stripe_shipbob_c_corp.json \
  private-shipbob-access-request.json private-shipbob-access-receipt.json
```

探测只读取 Channels，精确匹配绑定 channel 与四项 read scope；回执只保存当前主体绑定切片和实际 token 别名值的组合指纹，不保存 channel、scope 列表或响应。真实 Shadow request 再由 Box 完整生成，无需从编译模板复制或手写窗口：

```bash
opc-finance-box shipbob-shadow-request-init \
  examples/boxes/us_dtc_shopify_stripe_shipbob_c_corp.json \
  --entity us_dtc_company --period 2026-08 \
  --output private-shipbob-live-request.json

opc-finance-box shipbob-shadow-request-verify \
  examples/boxes/us_dtc_shopify_stripe_shipbob_c_corp.json \
  private-shipbob-live-request.json

opc-finance-box shipbob-shadow-observe \
  examples/boxes/us_dtc_shopify_stripe_shipbob_c_corp.json \
  private-shipbob-live-request.json \
  --access-request private-shipbob-access-request.json \
  --access-receipt private-shipbob-access-receipt.json \
  --output private-shipbob-shadow-observation.json
```

init 自动写出主体绑定、production、精确 UTC 自然月、每页 100 条且最多 50 页的 `0600` 请求，拒绝覆盖且不包含 token 或商户账户绑定。verify 不读取环境凭证或网络；observe 先重验 access request/receipt 与当前 Box、主体、绑定切片及 token 别名值，再在内存运行真实只读。Orders 与 Returns 都发送当前绑定的 `shipbob_channel_id` header；结果只保存订单/发货/退货/退货项计数、分页/重试、channel-header 使用布尔值、结构候选和隐私/禁写控制。完整 Pipeline result 只以 SHA-256 绑定；履约/退货金额、channel、商户账户、仓库、SKU、状态、处置候选、客户、运单和原始/哈希来源键都不进入 observation。

生成后的请求结构为：

```json
{
  "pipeline_id": "commerce.shipbob_fulfillment_close",
  "payload": {
    "entity_id": "us_dtc_company",
    "period": "2026-08",
    "shipbob_request": {
      "mode": "fetch",
      "default_entity_id": "us_dtc_company",
      "environment": "production",
      "interval_start": "2026-08-01T00:00:00Z",
      "interval_end": "2026-09-01T00:00:00Z",
      "page_size": 100,
      "max_pages": 50
    }
  }
}
```

一个增量窗口最长 31 天。HTTP 429 和 5xx 采用有界重试并记录安全摘要；响应 body、Authorization 和 token 不进入错误或结果。成功同步也不会自动推进 checkpoint，仍须使用 Connector 增量同步控制完成显式 commit。

一个根级 ShipBob 凭证不得静默服务多个法律主体。多主体 Box 可以把 `connector.shipbob` 显式绑定到多个真实经营主体，但每个主体都必须在 `OPC_SHIPBOB_ENTITY_BINDINGS_JSON` 中拥有自己的 channel 与 token 别名；缺少、重复或格式错误的主体切片均失败关闭，且绝不回退到旧根级 token。同一 JSON 中无关主体的变化不会使当前主体回执失效。

## 上线前人工门

- `shipbob_entity_binding_review`：确认 Box 主体与 ShipBob merchant account 一致。
- `shipbob_order_mapping_review`：确认 Shopify、Marketplace 或其他上游订单引用映射。
- `shipbob_fulfillment_cost_review`：确认 fulfillment invoice 的币种、期间和会计处理。
- `return_disposition_review`：确认补库存、隔离和报废候选。

只有真实只读样本、独立来源基线、主体/期间范围、失败恢复和独立复核都完成后，才可进入 Connector Shadow 与 stable promotion 流程。脱敏 observation 不能反填独立 baseline，也不授权修改 ShipBob、库存或账簿。
