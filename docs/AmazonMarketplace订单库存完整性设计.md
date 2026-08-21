# Amazon Marketplace 订单、库存与财务活动完整性设计

本设计把 `connector.amazon_seller` 从单一 Finances 交易证据扩展为三个互不替代的只读来源：Orders、FBA Inventory、Finances。目标是发现跨来源缺口并形成复核候选，不是用某一个 API 推断完整订单、历史库存、已确认收入、税负或银行结算。

## 官方契约与版本选择

- Orders 使用当前 [`Orders API v2026-01-01 searchOrders`](https://developer-docs.amazon.com/sp-api/docs/orders-api)。旧 v0 已弃用，Amazon 要求在 2027-03-27 前迁移；Box 不新增已弃用 v0 依赖。
- Orders 只请求 `includedData=FULFILLMENT`。不请求 `BUYER`、`RECIPIENT`、`PROCEEDS`、`EXPENSE`、`PROMOTION`、`PACKAGES`、`TAX`、`PAYMENT` 或 `FULFILLMENT_ORDERS`，避免取得不需要的 PII、资金、税务和物流明细。订单商品标题、原始 SKU/ASIN 与 order/item ID 即使出现在基础响应中也只做绑定哈希后立即丢弃。
- `searchOrders` 每页最多 100 条；下一页只能把响应 `nextToken` 作为同一固定 endpoint 的 `paginationToken` 重放，并保留原筛选条件。Box 使用 UTC 半开窗口，结束时间至少早于调用两分钟，单次最多 31 天、20 页。
- FBA Inventory 使用 [`getInventorySummaries` v1](https://developer-docs.amazon.com/sp-api/reference/getinventorysummaries)，`details=true`、`granularityType=Marketplace`，`granularityId` 与唯一 `marketplaceIds` 都固定为已绑定 Marketplace。下一页必须在同一请求上下文中立即使用 `nextToken`；响应 URL 永不跟随。
- FBA Inventory 全量读取是“调用时当前 Marketplace 库存观察”，不是月末历史库存。若将来需要法定月末库存，应增加独立的历史报告或仓库账证据，不能回填或伪造调用时点。
- Finances 继续使用 `v2024-06-19 listTransactions`，保留 released/deferred 与层级 component，不改变 v42 的收入、税务、结算和过账边界。

Orders `searchOrders` 可由 Finance and Accounting 或 Inventory and Order Tracking 等角色授权；FBA Inventory `getInventorySummaries` 需要 Amazon Fulfillment 或 Product Listing 角色。真实上线必须验证应用与 Seller 授权确实覆盖所选只读操作。

## 单一 Seller 绑定

三个来源共用同一组环境凭证、区域、Seller ID 与 Marketplace ID 白名单。标准化键都使用同一个 Seller binding：

```text
SHA256("amazon-seller|" + entity_id + "|" + region + "|" + seller_id + "|" + sorted_marketplace_ids)
```

在此绑定下：

- Orders 的 `orderId` 与 Finances `ORDER_ID` 生成相同 `amazon_order_key`；
- Orders 与 Inventory 的 `sellerSku` 生成相同 `amazon_sku_key`；
- ASIN、FNSKU、order item ID 只生成不可逆关联键，不保留原值；
- 一个 credential pack 仍只允许一个 Box 法律主体。

## 最小标准数据集

`commerce.amazon_seller_orders` 只保留：订单关联键、主体、Marketplace、created/updated time、fulfillment status、fulfilled-by、逐 item 的关联键/数量/哈希 SKU/ASIN 和 evidence。

`commerce.amazon_seller_inventory` 只保留：主体、Marketplace、哈希 SKU/ASIN/FNSKU、condition、观察时间、total、fulfillable、inbound、reserved、researching、unfulfillable 数量和 evidence。

FBA API 可省略部分数量分项。Connector 会把缺失的可选分项映射为 0 以保持确定性 schema，同时在 `quantity_fields_present` 中明确披露哪些字段真实出现；Service 单独输出 `inventory_quantity_field_missing_keys`，因此缺失值不会被误说成已知零。

`commerce.amazon_seller_transactions` 沿用 v42。客户、收件人、地址、公司、邮件、电话、购买单号、店名、商品标题、原始订单/SKU/ASIN/FNSKU、价格、税务、支付、包裹、追踪和自由文本均不进入标准数据集。

## 交叉复核结论

确定性 Service 只计算下列差异候选：

- Finances 中带 `ORDER_ID`、但 Orders 创建窗口不存在的交易；
- 已发货/部分发货且非取消订单，在 Finances 窗口中没有订单关联的订单；
- Amazon 履约订单 item 的 SKU 在当前 FBA Inventory 观察中不存在；
- 当前库存中的负数、数量结构异常或重复 SKU；
- Orders、Inventory、Finances 的实体、Seller binding 与 Marketplace 不一致。

时间差、延迟入账、跨窗口退款/替换单、FBM SKU、库存同步延迟都可能产生合理差异，所以“未匹配”默认是复核候选，不自动生成分录或库存调整。结构错误、跨主体/Marketplace、重复键和非法数量会阻塞。

若请求的 UTC 半开窗口精确从月初到下月月初，Connector 会显式输出 `canonical_month_period`。Pipeline 只对已发货/部分发货且由 Amazon 履约的订单形成三源候选人口：订单必须在 Finances 中有同绑定的订单键，且所有哈希 Seller SKU 都出现于本次 FBA 当前库存观察，才计为范围匹配。`marketplace_three_way_scope_match_rate` 只是该候选人口的匹配率，需要 `hashed_cross_source_keys_reviewed` 人工控制，且永不是订单、财务事件或库存完整性声明。

## 明确不证明

即使三源差异为零，系统仍不得声称：

- 订单、退款、费用、settlement 或银行到账完整；
- 当前 FBA Inventory 等于期间末账面库存或可直接计算 COGS；
- Marketplace facilitator、sales tax、VAT/GST、预提税或收入确认政策已经确定；
- 任何分录、库存调整、退款、付款、报税或外部动作已执行。

真实晋级需要连续月份 Connector Shadow、独立 Seller Central/库存/银行或会计来源基线，以及当地税务和收入政策复核。

## 可运行样板

```bash
python -m src.cli pipeline examples/boxes/us_marketplace_amazon_seller_c_corp.json \
  examples/pipelines/amazon_seller_marketplace_close_fixture.json \
  --actor founder
```

编译后的请求模板默认使用 `fetch`，部署环境契约会声明 `OPC_AMAZON_SELLER_ENTITY_BINDINGS_JSON` 的七个逐主体字段和三项动态 secret alias，不会把实际别名值、凭证或 Seller ID 写入 Box JSON。真实 Connector Shadow 以 `amazon_seller.marketplace_close` 为 Amazon Pack 的覆盖 profile；独立基线必须分别确认 Orders、FBA Inventory 与 Finances 记录数，并核对一次 LWA、三源网络读、最小化字段和无写入边界。

## 真实 Shadow 操作链

```bash
opc-finance-box amazon-seller-shadow-request-init BOX.json \
  --entity ENTITY_ID --period YYYY-MM \
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

`--marketplace-id` 是一次运行的显式范围选择，不是凭证；它只进入权限为 `0600` 的私有请求，仍需与当前主体环境切片中的 Marketplace allowlist 一致。init 固定 production、精确已结束自然月、Orders `created` 时间基准以及 Orders / Inventory / Finances 各 20 页上限，拒绝覆盖已有文件。verify 在联网前重新检查文件权限、严格字段、主体 Pack 绑定、月份边界和 Marketplace 格式。observe 先重验当前 Amazon Seller access request/receipt，再在内存执行完整三源 Pipeline 并绑定结果 SHA-256，只把不含 Marketplace、地区、卖家、客户、商品、数量、状态、金额及业务键的控制摘要交给 assessor。

因此首客激活工作区里的 request-init 命令只有 `REPLACE_WITH_MARKETPLACE_ID` 需要操作者替换；替换 CLI 参数后无需编辑 JSON。该显式选择避免 Box 从多 Marketplace allowlist 猜测经营范围，也不把一次范围选择解释为主体或税务归属证明。
