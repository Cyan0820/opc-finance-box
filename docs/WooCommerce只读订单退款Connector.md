# WooCommerce 只读订单与退款 Connector

## 定位

`connector.woocommerce` 让自建 WooCommerce 独立站进入 OPC Finance Box，而不要求切换到 Shopify。它使用 WooCommerce REST API v3 读取一个法律主体绑定站点在指定期间内发生修改的订单快照，以及该期间新建的退款事件，再交给确定性服务和 `woocommerce.order_refund_close` Pipeline 形成经营与关账复核候选。

它不是订单系统、支付处理器、总账或税务引擎。订单状态、`date_paid_gmt`、目的地国家、商店税额和退款记录都只是来源证据，不能单独证明处理器结算、银行到账、收入确认、销售税登记/税负、库存变动或会计过账。

## 官方契约与固定端点

截至 2026-08-15，本 Pack 依据 WooCommerce 官方 REST API v3 和 WordPress REST API 分页契约实现：

- API 版本与根路径：[WooCommerce REST API v3](https://developer.woocommerce.com/docs/apis/rest-api/v3/)，固定使用站点下的 `/wp-json/wc/v3`。
- 认证：[WooCommerce REST API Authentication](https://developer.woocommerce.com/docs/apis/rest-api/authentication)，要求管理员签发只读 Consumer Key/Secret，并仅在 HTTPS `Authorization: Basic ...` 头中使用。
- 修改订单：[Orders API](https://developer.woocommerce.com/docs/apis/rest-api/v3/orders)，固定读取 `GET /orders`，按 `modified_after` / `modified_before`、GMT、升序修改时间查询。
- 退款事件：[Refunds API](https://developer.woocommerce.com/docs/apis/rest-api/v3/refunds)，固定读取顶层 `GET /refunds`，按 `after` / `before`、升序创建时间查询。
- 分页：[WordPress REST API Pagination](https://developer.wordpress.org/rest-api/using-the-rest-api/pagination/)，本地生成 `page` / `per_page`，用 `X-WP-Total` 和 `X-WP-TotalPages` 校验总量。

响应中的 `Link`、对象 `_links` 或其他 URL 从不被跟随。站点 origin 只能来自运营环境配置，必须是无凭证、无端口、无 query/fragment 的 HTTPS 域名；请求只能落到上述两个固定 collection path。

## 凭证与主体绑定

新接入使用逐主体别名绑定，配置只允许进入环境或 secret manager：

```bash
export OPC_WOOCOMMERCE_ENTITY_BINDINGS_JSON='{"us_dtc_company":{"site_origin":"https://shop.example.com","key_permission":"read","consumer_key_env":"OPC_WC_US_KEY","consumer_secret_env":"OPC_WC_US_SECRET"}}'
export OPC_WC_US_KEY='ck_...'
export OPC_WC_US_SECRET='cs_...'
```

绑定表的每个主体独立选择 HTTPS site origin、声明为 `read` 的 key permission 和两个动态密钥别名。Provider fetch 暂时仍兼容根级 `OPC_WOOCOMMERCE_SITE_ORIGIN` / `CONSUMER_KEY` / `CONSUMER_SECRET`，但旧形式不能生成当前 access receipt 或解锁 live Shadow。Key 必须在 WooCommerce 管理端签发为 `read`；Pack 不调用任何 POST、PUT、PATCH 或 DELETE 业务端点。WooCommerce 没有供这个安全探测反查“该 key 绝无写权限”的固定端点，因此成功回执只证明两个 GET 可读和站点绑定，明确保留 `write_permission_provider_verified=false`，仍须不同人员在后台复核 key 权限。

Consumer Key、Consumer Secret 和站点 origin 都不能出现在 Box JSON、Pipeline request、fixture、编译 lock、日志或错误消息中。Basic credential 只为单次 HTTPS 请求构造，不写入 Connector 输出。

## 增量、分页与失败边界

- Pipeline 采用 UTC 半开窗口 `[interval_start, interval_end)`；单次最长 31 天。
- 为适配 WooCommerce inclusive date filter，HTTP 查询起点使用 `interval_start - 1 microsecond`，标准化阶段再次严格检查记录必须落入半开窗口。
- 每个 collection 最多 100 页、每页 100 条，即最多 10,000 条；超过时要求缩短期间，不能静默截断。
- 只有 429 和 5xx 以及有限 transport failure 会重试；重试次数、总等待和是否采用 `Retry-After` 进入安全来源摘要。
- `X-WP-Total` / `X-WP-TotalPages` 缺失、非法或分页中变化时失败关闭，避免把变化中的大范围读取误称完整。
- 单行时间、金额、状态、币种、来源 ID 或列表结构异常会进入 `rejected_rows`，整批 `quality.ready=false`，后续服务不执行。

订单是“窗口内修改过的当前状态快照”，退款是“窗口内创建的事件”。修改窗口不是订单创建 cohort；嵌套 lifetime refund 与当前窗口退款事件不一致时必须复核完整性。

## 最小化数据模型

标准数据集只有：

- `commerce.woocommerce_orders`：哈希订单键、主体、创建/修改/支付/完成时间、状态、币种、折扣/配送/税/订单总额、支付方式代码、目的地国家、商品行数/数量合计、lifetime 退款计数/金额和证据定位。
- `commerce.woocommerce_refunds`：哈希退款键、哈希父订单键、主体、创建时间、退款金额、是否退回支付方式、退款行数和证据定位。

明确丢弃：客户姓名、邮箱、电话、完整地址、客户 ID、IP、User-Agent、客户备注、商品名、SKU、product/variation ID、自由文本、meta、原始订单/退款/transaction ID 和响应链接。国家代码是目的地税务证据；它不等同于客户身份，也不能自动生成 nexus、登记或税负结论。

## 可运行样板

离线运行不会访问网络：

```bash
python -m src.cli pipeline \
  examples/boxes/us_dtc_woocommerce_c_corp.json \
  examples/pipelines/woocommerce_order_refund_close_fixture.json
```

先初始化并由有权人显式执行只读权限探测：

```bash
opc-finance-box connector-access-request-init \
  examples/boxes/us_dtc_woocommerce_c_corp.json \
  --pack connector.woocommerce --entity us_dtc_company \
  --output private-woocommerce-access-request.json
opc-finance-box connector-access-probe \
  examples/boxes/us_dtc_woocommerce_c_corp.json \
  private-woocommerce-access-request.json --allow-network \
  --output private-woocommerce-access-receipt.json
```

探测只对绑定站点执行 Orders 与 Refunds 两个 `context=view&per_page=1&_fields=id` GET；源 ID 只在内存校验响应形状后丢弃，不请求金额。真实 Shadow request 再由 Box 完整生成，只携带非密钥控制参数：

```bash
opc-finance-box woocommerce-shadow-request-init \
  examples/boxes/us_dtc_woocommerce_c_corp.json \
  --entity us_dtc_company --period 2026-08 \
  --output private-woocommerce-live-request.json

opc-finance-box woocommerce-shadow-request-verify \
  examples/boxes/us_dtc_woocommerce_c_corp.json \
  private-woocommerce-live-request.json

opc-finance-box woocommerce-shadow-observe \
  examples/boxes/us_dtc_woocommerce_c_corp.json \
  private-woocommerce-live-request.json \
  --access-request private-woocommerce-access-request.json \
  --access-receipt private-woocommerce-access-receipt.json \
  --output private-woocommerce-shadow-observation.json
```

init 自动写出主体绑定、精确 UTC 自然月、每页 100 条且最多 100 页的 `0600` 请求，无需人工编辑并拒绝覆盖。verify 不读取环境凭证或网络；observe 才在内存运行真实 REST API v3 只读，并只保存订单/退款计数、分页/重试、异常候选和隐私/禁写控制。完整 Pipeline result 只以 SHA-256 绑定；金额、站点 origin、客户、商品、支付方式、原始或哈希订单/退款键都不进入 observation。

生成后的请求结构为：

```json
{
  "pipeline_id": "woocommerce.order_refund_close",
  "payload": {
    "entity_id": "us_dtc_company",
    "period": "2026-08",
    "woocommerce_request": {
      "mode": "fetch",
      "default_entity_id": "us_dtc_company",
      "interval_start": "2026-08-01T00:00:00Z",
      "interval_end": "2026-09-01T00:00:00Z",
      "page_size": 100,
      "max_pages": 100
    }
  }
}
```

输出按币种分别汇总订单、税额、配送、折扣、lifetime refund 和窗口退款事件；不生成跨币种总数值。重复业务键、找不到窗口内父订单的退款或退款/税额算术越界会阻塞。父订单未在修改窗口内出现时，应缩小/调整读取策略或提供受控的订单证据补充，而不是凭退款事件猜测订单金额。

## 四道人工作业门

1. `woocommerce_site_entity_binding_review`：确认站点经营者、密钥和 Box 法律主体完全一致。
2. `woocommerce_order_status_mapping_review`：确认商户插件、自定义状态与财务含义；状态不证明到账或收入。
3. `woocommerce_refund_completeness_review`：确认窗口退款事件、订单 lifetime refund、处理器退款和跨窗口订单引用完整。
4. `woocommerce_tax_and_revenue_policy_review`：由适用地区专业人士确认收入截止、总额/净额、目的地证据、间接税登记和税额处理。

## Shadow 与成熟度

Pack 当前为 `experimental`。进入真实 Shadow 前，先生成 `woocommerce.order_refund_close` Connector baseline workpaper，以独立来源记录订单/退款总数和控制布尔值；再运行 `woocommerce-shadow-request-init/verify/observe` 并由不同人员复核。Fixture、Pipeline 自身输出、脱敏 observation 或 demo 引用都不能反填为独立 baseline，也不能把 Pack 晋级为 stable。

真实运行还需验证具体商店的插件、自定义状态、API 字段变体、限流、退款跨窗口和大数据量行为。任何 Shadow 通过都不授权修改 WooCommerce、过账、库存动作、退款、付款或报税。
