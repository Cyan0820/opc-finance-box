# Amazon Seller 只读财务 Connector

本文说明 `connector.amazon_seller` 的 Finances 单源兼容路径。Pack 还提供 Orders v2026、FBA 当前库存和 Finances 的三源 `amazon_seller.marketplace_evidence`；新部署与真实 Shadow 应优先使用 [Amazon Marketplace 订单库存完整性设计](AmazonMarketplace订单库存完整性设计.md)。两条路径都把一个 Box 法律主体绑定到明确的 Amazon Seller、销售区域和 Marketplace ID 白名单，且都不是税表、银行或总账的替代品。

## 官方契约

- 接口：[`GET /finances/2024-06-19/transactions`](https://developer-docs.amazon.com/sp-api/reference/listtransactions)。Amazon 说明单页最多 500 条，返回 `nextToken` 时需使用相同参数续页，允许出现空页；`postedBefore` 是开区间端点，时间窗超过 180 天会返回空结果，结束时间必须早于请求至少两分钟。
- 权限：[`listTransactions` 需要 Finance and Accounting role](https://developer-docs.amazon.com/sp-api/docs/finances-api-v0-use-case-guide)。应用还需取得卖家授权。
- 鉴权：先向固定 LWA token 端点换取短期 access token，再用 `x-amz-access-token` 调用 SP-API。Amazon 自 2023-10-02 起[不再要求 AWS IAM 或 SigV4](https://developer-docs.amazon.com/sp-api/changelog/sp-api-will-no-longer-require-aws-iam-or-aws-signature-version-4)。
- 区域：端点只从 Amazon 的[官方区域表](https://developer-docs.amazon.com/sp-api/docs/sp-api-endpoints)选择：NA、EU、FE。请求和响应中的 URL 都不能改变该端点。

Box 进一步收紧为单次最多 31 天、最多 20 页（10,000 条），并拒绝重复/非法游标、超过限制、响应结构变化、429/5xx 重试耗尽、主体/卖家/Marketplace 不一致和非固定端点。

## 环境绑定

新部署必须通过 `OPC_AMAZON_SELLER_ENTITY_BINDINGS_JSON` 把每个 Box 法律主体绑定到 `environment`、`region`、`seller_id`、`marketplace_ids`，以及三项不同的 LWA 环境别名；Client ID、Client Secret 和 Refresh Token 的实际值再由这些别名单独从 Secret Manager 注入。绑定、凭证和 seller/Marketplace 标识都不能写入 Box、Pipeline request、日志或 Shadow artifact：

```json
{
  "us_amazon_marketplace_company": {
    "environment": "production",
    "region": "NA",
    "seller_id": "A1EXAMPLESELLER",
    "marketplace_ids": ["ATVPDKIKX0DER"],
    "client_id_env": "OPC_AMAZON_US_CLIENT_ID",
    "client_secret_env": "OPC_AMAZON_US_CLIENT_SECRET",
    "refresh_token_env": "OPC_AMAZON_US_REFRESH_TOKEN"
  }
}
```

一个主体可包含多个 Marketplace ID，但每次 Pipeline request 只选一个 ID，结果也必须只属于该 ID。该 Connector Pack 可以显式绑定多个法律主体；每个主体必须拥有自己的完整切片与三项别名，多主体运行遇到缺失绑定时失败关闭。旧根级六项 `OPC_AMAZON_SELLER_*` 环境变量只保留单主体 Provider fetch 迁移兼容，不能生成当前 access receipt，也不能在多主体 Box 中回退。

## 最小数据集

标准数据集为 `commerce.amazon_seller_transactions`，只保留：

- 绑定后哈希的 transaction 与 related business identifier；
- 法律主体、Marketplace ID、account type、transaction type/status、posted timestamp；
- 逐币种 total amount；
- transaction/item 两个 scope 的层级 financial component path 和金额；
- item 数量与逐币种 item total 摘要；
- source/page/row/batch/API contract evidence。

以下字段主动丢弃：seller 原始 ID、订单/退款/结算等原始 ID、客户与地址、店名、商品描述、SKU、ASIN、Product/Amazon Pay context、自由文本以及响应链接。哈希只用于稳定关联，不能替代 Seller Central 或独立导出的原始证据。

层级 component 可能在 transaction 和 item 上同时出现，也可能父子层都带金额。系统按 `scope + path + currency` 分开呈现，并明确禁止跨层相加，避免把父级与子级金额重复计算。

## Pipeline

离线样板：

```bash
python -m src.cli pipeline \
  examples/boxes/us_marketplace_amazon_seller_c_corp.json \
  examples/pipelines/amazon_seller_transaction_close_fixture.json \
  --actor founder
```

真实请求使用 `amazon_seller.transaction_close`，把 `amazon_seller_request.mode` 改为 `fetch`，提供白名单内的 `marketplace_id` 和不超过 31 天的 UTC 半开窗口。可选 `transaction_status` 为 `RELEASED`、`DEFERRED` 或 `DEFERRED_RELEASED`。

确定性 Service 按币种、状态、交易类型、Marketplace、related identifier 和 financial component 汇总，单独展示 deferred、退款、费用与缺少 settlement reference 的候选。它不会：

- 把交易解释为已确认收入或完整订单；
- 确定 Marketplace facilitator、VAT/GST/sales tax 或预提税处理；
- 证明 settlement 已完整或已到银行；
- 修改库存/COGS、生成分录、过账、退款、付款或调用任何写接口。

上线前必须完成五道复核：主体—卖家账户绑定、Marketplace scope、transaction/component 映射、费用与税务政策、settlement 完整性。Finances API 可能不包含最近 48 小时的订单事件，因此日结应保留延迟窗口并用连续期 Shadow 验证，而不是把一次拉取解释为完整性证明。

## Shadow 与发布

`amazon_seller.transaction_close` 仍保留为可执行的 Finances 单源兼容 Pipeline；Amazon Pack 的 schema-v2 Connector Shadow profile 已升级为 `amazon_seller.marketplace_close`。真实匿名 baseline 必须由独立来源工作底稿提供 Orders、FBA Inventory 和 Finances 三源记录数及控制结论，并确认：

- 网络只读、LWA 内存换证和固定区域端点；
- 精确主体/卖家/Marketplace scope；
- 客户、商品、店名、自由文本和原始 ID 未进入标准数据集；
- 三源共用一次 LWA 内存换证，Orders 只请求 `FULFILLMENT`，FBA 库存明确为当前观察；
- 哈希订单/SKU 关联只形成差异候选，未执行写入、收入、税务、结算、库存估值/调整或过账动作。

真实三源 Shadow 不手写 Pipeline JSON。先生成当前法律主体的私有 access request，显式授权一次 LWA 换证和四个固定最小 GET，再验证回执：

```bash
opc-finance-box connector-access-request-init BOX.json \
  --pack connector.amazon_seller --entity ENTITY_ID \
  --output private-amazon-seller-access-request.json

opc-finance-box connector-access-probe BOX.json \
  private-amazon-seller-access-request.json --allow-network \
  --output private-amazon-seller-access-receipt.json

opc-finance-box connector-access-receipt-verify BOX.json \
  private-amazon-seller-access-request.json \
  private-amazon-seller-access-receipt.json
```

探测依次读取 Sellers marketplace participations、单条 FULFILLMENT Orders、无 details 的当前 FBA Inventory 和有界 Finances。所有绑定 Marketplace 必须 active，四类 read 都必须成功。Sellers 响应不能安全反查 seller ID，因此该 ID 仍是操作者绑定，回执固定保留 `seller_id_provider_verified=false`；Finances 响应中的财务值和全部来源行只在内存检查后丢弃。随后显式选择本次唯一 Marketplace；该值只写入 `0600` 私有请求，不出现在 stdout、Box 配置或安全 observation：

```bash
opc-finance-box amazon-seller-shadow-request-init BOX.json \
  --entity ENTITY_ID --period 2026-07 \
  --marketplace-id MARKETPLACE_ID \
  --output private-amazon-seller-live-request.json

opc-finance-box amazon-seller-shadow-request-verify BOX.json \
  private-amazon-seller-live-request.json

opc-finance-box amazon-seller-shadow-observe BOX.json \
  private-amazon-seller-live-request.json \
  --access-request private-amazon-seller-access-request.json \
  --access-receipt private-amazon-seller-access-receipt.json \
  --output private-amazon-seller-shadow-observation.json
```

init 只接受至少已结束两分钟的完整 UTC 自然月，固定 production、Orders created-time 窗口及三源各 20 页上限，并重新验证该主体确实绑定 Amazon Seller Pack。Seller ID、区域、LWA 凭证和 Marketplace allowlist 继续只从当前主体环境切片注入；命令行所选 Marketplace 还必须落在该 allowlist 内。observe 先验证 access request/receipt 仍与当前 Box、主体、绑定切片和三项别名值一致，再要求三源均为非空真实网络读取、一次内存 LWA、固定区域端点、零拒绝/重复和全部外部动作关闭。落盘 observation 只保留三源计数、差异候选计数、分页/重试和控制布尔值，不保存 Marketplace ID、区域、卖家、客户、商品、库存数量、状态、金额或原始/哈希业务键。

离线 fixture 和“把 fixture 标成 api”的单测只能验证契约，不能作为真实 Shadow 或 stable 晋级证据。
