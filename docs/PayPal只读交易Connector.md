# PayPal 只读交易 Connector

`connector.paypal` 是独立站、平台电商和其他收取 PayPal 款项的 OPC 可编辑证据 Pack。它使用 PayPal Transaction Search v1，只读取余额影响交易的 `transaction_info`，按法律主体形成交易事件、费用、退款、冲正和余额转出候选；不读取客户、收货或商品明细，不确认收入，不核销银行到账，不生成凭证，也不调用任何业务写接口。

当前 Pack 版本为 `0.1.0`，成熟度为 `experimental`。官方合同按 2026-08-15 核验：OAuth token 使用固定的 `/v1/oauth2/token`；交易查询使用 `GET /v1/reporting/transactions`。生产与 Sandbox 只允许 `api-m.paypal.com` 和 `api-m.sandbox.paypal.com`。Transaction Search 最大日期范围为 31 天、单页最多 500 条、一个查询最多 10,000 条；超出时 Connector 失败关闭，要求缩短区间。分页只在本地递增 `page`，响应 HATEOAS URL 永不跟随。

## 官方契约依据

- [REST API OAuth 认证](https://developer.paypal.com/api/rest/authentication/)
- [Transaction Search v1 API](https://developer.paypal.com/docs/api/transaction-search/v1/)
- [Transaction Search 接入与权限](https://developer.paypal.com/docs/transaction-search/)
- [Transaction Event Codes](https://developer.paypal.com/docs/transaction-search/transaction-event-codes/)
- [Rate Limiting Guidelines](https://developer.paypal.com/api/rest/reference/rate-limiting/)

操作者必须在 PayPal REST App 中启用 Transaction Search，并确认 access token 包含 reporting search read 权限。PayPal 文档提示：为既有 App 新增该权限后，生效可能最多需要约 9 小时。OPC 自己的单一商户账户可使用自己的 App 凭证；若产品方代表多个第三方商户集中调用 Transaction Search，必须先满足 PayPal Partner Network 要求，不能把单商户 Client ID/Secret 当作多租户授权。

新接入应使用 `OPC_PAYPAL_ENTITY_BINDINGS_JSON`：每个 Box 法律主体分别声明 `environment`、REST `app_id`、13 位 merchant `account_id`，以及 `client_id_env` / `client_secret_env` 两个动态环境别名。Provider fetch 暂时仍兼容根级 `OPC_PAYPAL_CLIENT_ID` 与 `OPC_PAYPAL_CLIENT_SECRET`，但旧形式没有逐主体 provider-account 证明，不能生成当前 access receipt，也不能解锁 live Shadow。Client Secret、Basic Authorization、短期 access token、API 原始 body、Box 配置、编译产物和 Pipeline 结果都不会保存凭证。每次有界读取只在内存中换取一个 token；当前 Pack 不代管 refresh token 或集中商户授权。

## 数据最小化与财务边界

标准数据集只有 `payments.paypal_balance_activity`。每条记录保留：

- SHA-256 化的 PayPal transaction 与 reference transaction key；
- T-code、T-code group 和有限的处理器活动分类；
- 发起/更新时间、处理状态；
- 交易金额、费用及各自币种；
- 同币种时的 `amount + fee` 确定性净额；
- 退款、冲正和费用退回候选布尔值；
- 批次、页码与标准化证据引用。

查询固定 `fields=transaction_info` 与 `balance_affecting_records_only=Y`。payer name/email/account、shipping address、cart/items、invoice ID、交易备注、subject、原始 transaction ID、merchant account number 和响应 links 都不会进入标准数据集。Fixture 故意包含这些私有字段和恶意 `next` URL，用来证明它们被忽略。

官方 T-code 只描述 PayPal 资金活动类型。`T1107` 可标记 merchant refund 候选，`T1106` 可标记 payment reversal 候选，`T00` / `T01` / `T04` / `T11` group 可用于处理器核对；但这些分类不能单独决定收入、税务、会计科目或银行核销。不同币种永不加总；费用币种与交易币种不同时分别汇总，不制造净额。

## 装配与离线验证

```bash
python -m src.cli create \
  examples/box_specs/paypal_us_c_corp.json \
  --output outputs/us-dtc-paypal.json
python -m src.cli validate outputs/us-dtc-paypal.json
python packs/connectors/paypal/provider_contract_test.py outputs/us-dtc-paypal.json
python -m src.cli pipeline \
  examples/boxes/us_dtc_paypal_c_corp.json \
  examples/pipelines/paypal_transaction_close_fixture.json
```

离线 Pipeline 为 `paypal.transaction_close`。它执行 Connector、质量门、主体范围、确定性事件/金额汇总和 Founder briefing，并可进入通用 Pipeline 运行台账与独立复核队列。Fixture 不访问网络，也不能作为真实 Shadow 或 Stable 晋级证据。

## 真实读取与安全 Shadow

先为当前主体配置别名绑定，并显式生成/执行最小权限探测。以下值仅为格式示例，真实 provider ID 与密钥必须留在私有 Secret Manager 或进程环境：

```bash
export OPC_PAYPAL_ENTITY_BINDINGS_JSON='{"us_dtc_company":{"environment":"production","app_id":"APPID_1234","account_id":"2ABCD3EFGH4JK","client_id_env":"OPC_PAYPAL_US_CLIENT_ID","client_secret_env":"OPC_PAYPAL_US_CLIENT_SECRET"}}'
export OPC_PAYPAL_US_CLIENT_ID='...'
export OPC_PAYPAL_US_CLIENT_SECRET='...'

opc-finance-box connector-access-request-init \
  examples/boxes/us_dtc_paypal_c_corp.json \
  --pack connector.paypal --entity us_dtc_company \
  --output private-paypal-access-request.json
opc-finance-box connector-access-probe \
  examples/boxes/us_dtc_paypal_c_corp.json \
  private-paypal-access-request.json --allow-network \
  --output private-paypal-access-receipt.json
```

探测只执行 OAuth token exchange 和一次按主体本位币过滤的 Reporting Balance GET；它核验 reporting/search scope、REST App 和 merchant account。Balance 响应包含的财务值会被请求，但不会进入回执、日志或标准数据集。通过仍不证明 Transaction Search 数据完整、财务勾稽或调度批准。

随后由 Box 直接生成主体和月份绑定的完整私有请求，不再复制模板或手写时间窗：

```bash
opc-finance-box paypal-shadow-request-init \
  examples/boxes/us_dtc_paypal_c_corp.json \
  --entity us_dtc_company --period 2026-08 \
  --output private-paypal-live-request.json

opc-finance-box paypal-shadow-request-verify \
  examples/boxes/us_dtc_paypal_c_corp.json \
  private-paypal-live-request.json

opc-finance-box paypal-shadow-observe \
  examples/boxes/us_dtc_paypal_c_corp.json \
  private-paypal-live-request.json \
  --access-request private-paypal-access-request.json \
  --access-receipt private-paypal-access-receipt.json \
  --output private-paypal-shadow-observation.json
```

init 排他写入 `0600` 文件，固定 production、`page_size=500`、`max_pages=20` 和精确 UTC 自然月半开区间。请求不包含 Client ID/Secret、access token、merchant account binding、客户值、交易 ID 或金额，也无需人工编辑。verify 在联网前重新检查 Box 能力、主体 Connector binding、严格字段、文件权限与无内联凭证，安全摘要不回显私有值。

Box 使用半开窗口 `[interval_start, interval_end)`；Connector 将结束点减一微秒后作为 API `end_date`，并再次按原半开窗口检查每条发起时间。这个减法是 Box 自身的防重叠控制，不代表替 PayPal 文档扩展时间语义。HTTP 429 与 5xx 采用有界重试；错误只返回状态和操作阶段，不返回 PayPal response body、Authorization 或凭证。成功读取也不自动推进 checkpoint。observe 只接受真实 production Transaction Search、零拒绝/重复、主体与整月范围一致且全部写动作关闭的结果；输出仅保留来源窗口、分页/重试、计数和候选控制，排除金额、客户、自由文本及原始/哈希交易 ID，并绑定完整内存 Pipeline result SHA-256。独立来源 baseline 仍须由另一准备人保存，不能从 observation 反填。

一个根级 PayPal Client ID/Secret 不得静默服务多个法律主体。多主体 Box 必须通过 `connector_bindings` 限定 Pack 范围，并在 `OPC_PAYPAL_ENTITY_BINDINGS_JSON` 中为每个主体选择自己的 App、merchant account 和密钥别名；当前主体切片或任一别名值变化会立即使旧回执失效，同表无关主体变化不会误伤。若产品方代表第三方商户集中访问，逐主体别名不能替代 PayPal Partner Network 或其他适用授权。

## 上线前人工门

- `paypal_entity_account_binding_review`：确认 Box 法律主体、PayPal merchant account 和 REST App 一致。
- `paypal_transaction_event_mapping_review`：确认实际 T-code 及业务含义，保留未知码复核。
- `paypal_fee_treatment_review`：确认费用币种、符号、期间和会计处理。
- `paypal_refund_reversal_review`：逐笔核对退款/冲正与原交易、客服和业务证据。

只有真实只读样本、独立来源基线、主体/期间范围、限流与失败恢复、字段变体和独立复核都完成后，才可进入 Connector Shadow 与 Stable promotion。
