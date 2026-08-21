# Connector 上线准备

Connector 准备台把“Pack 已安装”与“可以安全进入生产调度”分开。它只展示当前 Box 中**可执行 Pipeline 实际引用**的 Connector；随 Pack 一同安装但当前 Pipeline 未引用的 Connector 收在附录，不混入主要上线流程。

## 安全边界

`GET /api/box/connectors/readiness` 是只读控制面接口：

- 只返回环境变量名称和 `configured` 布尔值，永不返回凭证值；
- 不 dispatch Connector，不访问外部网络或本地源文件；
- 不因凭证存在而声称 Shadow 已执行；
- 不安装 cron、队列或其他调度；
- 不把已安装但未被可执行 Pipeline 引用的 Connector 当成上线要求。

命令行使用相同投影：

```bash
opc-finance-box connector-preflight /absolute/BOX_CONFIG
```

该命令同时提供两个层级：底层仍逐一列出每个 Pipeline 实际引用的数据集适配器；创始人视图则按 `connector.*` Pack 归组。同一 Shopify Pack 的 Orders 与月度证据、同一 Stripe Pack 的 Balance Transactions 与 Payouts 不再被误解为四个独立服务方。每个能力包只形成一个当前状态、一个下一动作和一条可选命令模板。

服务方级诊断依次显示能力包合同、凭证引用、主体绑定、服务方只读权限探测、私有并行请求、财务勾稽和调度放行。Shopify / Stripe / Wise / Xero / PayPal / WooCommerce / ShipBob / Amazon Seller 在凭证引用就绪后，先返回 `connector-access-request-init`，不会直接跳到 Shadow；Wise / Xero 的 token 与主体绑定 JSON 必须整组就绪，PayPal / WooCommerce / ShipBob / Amazon Seller 的主体绑定表与当前主体引用的动态密钥别名也必须整组就绪。其他已有专用链路的 Pack 仍返回相应的 `*-shadow-request-init` 模板。所有模板都不会自动执行。凭证未配置时只列环境变量名和应完成的证据，不生成包含占位密钥的 shell 命令。文件导入则要求先准备版本固定的脱敏样例与主体/期间/业务键映射。

页面显示“凭证引用就绪”时，只代表服务进程能看到所需环境变量。它不代表权限范围正确、数据完整、财务勾稽通过或已经获得调度批准。

## 七步证据链

每个 Connector 独立完成以下控制门：

1. **Provider 合同测试**：保留测试结果及 fixture 版本，确认分页、重试、金额单位、业务键和错误行为。
2. **凭证引用**：网络 Connector 只配置 Secret Manager 或环境变量引用，不把密钥复制进 Box、请求、日志或截图。
3. **主体与来源映射**：明确法律主体、店铺/账户、数据集和业务键，由负责人批准；不能按币种或渠道名称猜主体。
4. **服务方只读权限探测**：由有权人在 CLI 显式授权一次最小请求，证明凭证类型、必需读取权限和私有账户/店铺绑定；凭证非空不算通过。
5. **有界只读 Shadow**：限定起止时间和最大页数，保留 run ID、源记录数、分页/重试摘要和拒绝记录；首次运行不得自动写账。
6. **财务勾稽**：比较源数据合计、标准化合计、拒绝行和未解释差异。差异未清零或有明确解释前保持阻塞。
7. **调度放行**：明确 operator、独立 reviewer、告警负责人和回退方式后单独审批。Pipeline 的 release candidate 仍不等于付款、过账或报税授权。

## 八类 Connector 显式只读权限探测

先生成绑定当前 Box、法律主体和服务方账户的 `0600` 私有请求。Shopify / Stripe 模板故意保留待替换的私有账户值；Wise / Xero / PayPal / WooCommerce / ShipBob / Amazon Seller 只写 `entity_environment_binding`，无需也不允许把 provider ID、站点 origin、channel、seller 或 Marketplace allowlist 抄进请求。PayPal 的 `OPC_PAYPAL_ENTITY_BINDINGS_JSON` 每个主体固定 environment、REST App ID、merchant account ID 和 Client ID/Secret 环境别名；WooCommerce 的 `OPC_WOOCOMMERCE_ENTITY_BINDINGS_JSON` 每个主体固定 HTTPS site origin、声明为 `read` 的 key permission 和 Consumer Key/Secret 环境别名；ShipBob 的 `OPC_SHIPBOB_ENTITY_BINDINGS_JSON` 固定 environment、channel ID 和 token 环境别名；Amazon Seller 的 `OPC_AMAZON_SELLER_ENTITY_BINDINGS_JSON` 固定 environment、区域、seller ID、Marketplace allowlist 和三项 LWA 环境别名。生成后先离线验证：

```bash
opc-finance-box connector-access-request-init /absolute/BOX_CONFIG \
  --pack connector.shopify --entity ENTITY_ID \
  --output /absolute/private/shopify-access-request.json

opc-finance-box connector-access-request-verify \
  /absolute/BOX_CONFIG /absolute/private/shopify-access-request.json
```

验证命令不访问网络，也不返回店铺域名、Stripe account ID 或凭证。只有有权操作者明确接受这一次有界外部读取时，才追加 `--allow-network`：

```bash
opc-finance-box connector-access-probe \
  /absolute/BOX_CONFIG /absolute/private/shopify-access-request.json \
  --allow-network \
  --output /absolute/private/shopify-access-receipt.json

opc-finance-box connector-access-receipt-verify \
  /absolute/BOX_CONFIG \
  /absolute/private/shopify-access-request.json \
  /absolute/private/shopify-access-receipt.json
```

Shopify 探测只向私有绑定的、固定 `2026-07` Admin GraphQL 端点发送 `currentAppInstallation { accessScopes { handle } }`，要求 `read_orders`，允许可选历史订单读取，并在出现任何 `write_*` 或无关读取范围时保持阻塞。Stripe 探测只接受 `rk_test_` / `rk_live_` restricted key，固定 `2026-06-24.dahlia`：standalone 模式通过 `/v1/account` 核验当前账户，Connect 模式通过私有 expected account 核验连接账户；随后对 Balance Transactions 与 Payouts 各执行 `limit=1` 的只读列表请求。connected-account 模式会把私有 account ID 仅放入 `Stripe-Account` 请求头。

Wise 与 Xero 的 request 不复制 provider ID，只写 `entity_environment_binding`。Wise 把 `OPC_WISE_ACCESS_TOKEN` 与 `OPC_WISE_ENTITY_BINDINGS_JSON` 中当前主体的切片作为一个凭据组，按 Box 主体核验 Business Profile、本位币 Balance、访问合同与只读元数据端点；不会请求 balance statement 或任何交易。Xero 把 `OPC_XERO_ACCESS_TOKEN` 与 `OPC_XERO_ENTITY_BINDINGS_JSON` 中当前主体的切片作为一个凭据组，固定读取 Organisation 与一个探测日 Trial Balance，验证 tenant / organisation、`accounting.settings.read`、`accounting.reports.trialbalance.read` 和本位币；响应中的报表行、账户和金额均不写入回执。token 或当前主体绑定变化会使旧回执进入 `renewal_required`；同一 JSON 中新增、删除或修改无关主体不会误伤当前主体回执。

PayPal 与 WooCommerce 同样只在环境中解析当前主体切片和动态密钥别名。PayPal 固定执行 OAuth client-credentials exchange 与一次 `/v1/reporting/balances` GET，验证 reporting/search scope、REST App、merchant account 和本位币读取；Balance 数值会被服务方返回，但仅在内存用于结构/绑定检查并立即丢弃。WooCommerce 只对绑定站点执行 Orders 与 Refunds 两个 `context=view&per_page=1&_fields=id` GET；这能证明读取成功和站点绑定，却不能通过安全 GET 证明 key 没有写权限，因此回执固定声明 `write_permission_provider_verified=false`，操作者仍须在后台独立核验 key permission 为 `read`。

ShipBob 先对固定 `2026-07` 端点读取 Channels，要求精确命中当前主体绑定的 channel，并要求服务方返回的 scope 集合恰好为 `channels_read`、`orders_read`、`fulfillments_read`、`returns_read`；任何额外或写 scope 都阻塞。后续 Orders/Returns Provider 请求把同一个 channel 作为 `shipbob_channel_id` header 发送。Amazon Seller 先进行一次 LWA 内存换证，再对绑定区域固定主机执行 Sellers marketplace participations、单条 FULFILLMENT Orders、无 details 的当前 FBA Inventory 和有界 Finances 四个 GET；所有绑定 Marketplace 必须 active，四类读取都必须成功。Sellers 接口不直接反查 seller ID，因此回执固定声明 `seller_id_provider_verified=false`，该绑定仍需操作者独立复核；Finances 可能返回财务值，但金额和来源行立即丢弃，不进入回执。

回执只保留 Box / 请求 / 账户绑定指纹、检查布尔值、API 版本和凭证模式，以 `0600` 新文件落盘且不覆盖。密钥、绑定 JSON、店铺/账户/tenant/organisation/profile/balance/app/site/channel/seller/Marketplace 标识、scope 列表、原始响应、来源记录和财务值均不会返回。schema v2 使用有序 `env_names` 与整组指纹支持多环境变量凭据；v76 生成的 Shopify/Stripe schema v1 单凭据回执仍可复验，新探测统一生成 v2。回执是 SHA-256 自校验的私有运行证据，明确不是数字签名；默认超过 30 天、Box/请求/主体/账户或凭据组变化、任一必需检查失败均不能解锁 Shadow。workspace schema v5 按 `Connector Pack + 法律主体` 只生成一组 access request / receipt；所有 Pipeline 仍分别验证自己的期间请求和财务来源控制。`activation-workspace-status` 与服务端挂载后的 `/api/box/activation` 只输出安全状态和计数。八类 live observe 命令必须显式提交当前 access request 与 receipt；探测通过仍不完成财务勾稽、不释放调度，也不执行付款、退款、过账或报税。

### 回执续期与密钥轮换

`connector-access-receipt-verify` 同时核验两层条件：历史回执本身的完整性与 Box/请求绑定，以及它是否仍匹配当前凭据并在 30 天时效内。Connector access registry schema v3 默认在到期前 7 天把范围标记为 `renewal_due`，同时给出向上取整、不会暴露账户信息的 `days_until_expiry`；这段预警窗口内仍可进入原有的有界 Shadow dispatch，但应在到期前主动续期。历史合同完整但凭据已轮换或回执已经过期时改为 `renewal_required` 并关闭 dispatch；指纹、字段或绑定被破坏时显示 `blocked_invalid_receipt`，不会把篡改误当成普通续期。

完成密钥轮换后，或 Workbench 首次显示 `renewal_due` 时，由有权操作者显式授权一次新的最小只读探测：

```bash
opc-finance-box connector-access-receipt-renew \
  /absolute/BOX_CONFIG \
  /absolute/private/shopify-access-request.json \
  /absolute/private/shopify-access-receipt.json \
  --allow-network
```

续期不允许符号链接、并发改写、已篡改的旧回执或失败的新探测。它先完整验证新回执，再把旧文件原字节保留为同目录的 `<name>--superseded-<fingerprint>.json`，最后原子安装新的当前回执。成功摘要不返回归档路径、账户、密钥或凭据指纹；旧回执仅作运行证据，不再解锁 Shadow。不带 `--allow-network` 不会发送任何请求。

提前 7 天是产品默认值，不是服务方凭据的法定有效期，也不会自动联网、发送通知或执行续期。调用 registry API 的宿主可以在 `0..maximum_age_days` 内调整提醒窗口；首客工作区状态与 Workbench 使用默认值。只有操作者执行上面的命令并通过新探测，当前回执才会原子更新。

### 安全告警候选

需要让外部 cron、systemd timer 或云调度器每日检查时，读取同一个私有 Activation workspace：

```bash
opc-finance-box connector-access-alerts \
  /absolute/BOX_CONFIG \
  /absolute/private/ACTIVATION_ROOT \
  --as-of YYYY-MM-DD
```

命令按 `Connector Pack + 法律主体 + 状态` 生成稳定 `alert_id`。`renewal_due`、尚未初始化、等待凭据和等待授权探测是 warning；已到期/凭据轮换、凭据缺失、孤立回执、无效请求或无效回执是 critical。每条候选只含 Pack、主体、状态、剩余天数和安全下一动作，不含私有路径、账户、凭据/指纹、原始响应或财务值。schema v4 旧工作区只生成迁移提醒，不会冒充已经具备共享权限登记。

该命令只读、不访问服务方、不安装计划任务、不发送通知。编译出的 `connector.access_receipt_rotation_alerts` daily job 也默认关闭；部署者必须自行配置时区、接收人、稳定 alert ID 去重、恢复通知和升级责任人。成功退出只表示候选生成完成，不表示告警已送达或续期已执行。

服务方合同依据仍以各 Pack 的 `provider-contract.json`、固定端点和 Provider contract test 为准。外部文档链接只用于人工核对访问合同，不替代当前 Pack 固定版本、测试和真实账户 Shadow。

## 建议上线顺序

先用版本固定的 fixture 跑 Provider 合同测试，再配置只读、最小权限凭证。第一次 Shadow 使用最小日期窗口和显式主体，核对源数量与金额后逐步扩大窗口。只有连续运行稳定、异常有负责人、财务勾稽已复核时，才进入调度设计。

Shopify 和 Stripe 使用各自的单凭据引用；Wise 和 Xero 使用 token + 主体绑定表的完整凭据组；PayPal、WooCommerce、ShipBob 与 Amazon Seller 使用主体绑定表 + 当前主体动态密钥别名值的完整凭据组。后四类旧根级环境变量只为单主体既有 Provider fetch 保持迁移兼容，不能通过新 access gate，也不能在多主体 Box 中回退。同一个服务方凭据可以服务多个只读数据集，但每个主体、数据集仍应单独检查字段、分页和财务用途。Connector 输出只是证据，不会被直接认定为收入、税额或已批准凭证。

Shopify + Stripe 月结上线时，`activation-init` 会按职责生成三类独立工作底稿，而不是用一个“大而全”的结果替代所有来源控制：`dtc.shopify_stripe_month_close` 核验 Shopify 当月创建/更新双人口、退款 `processedAt` 归属、同窗 Stripe Balance Transactions 和月末 72 小时捕获；`stripe.daily_close` 另行核验 Stripe Balance/Payout→银行候选；绑定 Wise 时，`finance.bank_statement_close` 再核验真实银行月度流水。

Shopify 命令链使用 `shopify-monthly-shadow-request-init/verify/observe`，Stripe 使用 `stripe-shadow-request-init/verify/observe`，Wise 使用 `wise-shadow-request-init/verify/observe`，Xero Trial Balance 使用 `xero-shadow-request-init/verify/observe`，PayPal 使用 `paypal-shadow-request-init/verify/observe`，WooCommerce 使用 `woocommerce-shadow-request-init/verify/observe`，ShipBob 使用 `shipbob-shadow-request-init/verify/observe`，Amazon Seller 三源链使用 `amazon-seller-shadow-request-init/verify/observe`。这些链路都先生成主体/月份绑定的 `0600` 私有请求并在联网前检查权限、窗口或月末、主体绑定和无内联凭证。

Shopify 请求由操作者填写店铺/币种/逐笔处理器链接；Stripe 请求只在私有文件填写含金额、银行 ID 和 reference 的到账证据；Wise、Xero、PayPal、WooCommerce 与 ShipBob 请求由 Box 分别按本位币自然月、accrual-basis 月末、production Transaction Search 自然月、REST API v3 自然月和 `2026-07` Orders/Returns 自然月完整生成，无需手写或放入账户/租户/站点/channel 绑定。Amazon Seller 只要求操作者在 CLI 显式选择一个 Marketplace，随后自动生成完整三源请求，不从 allowlist 猜测范围。ShipBob 与 Amazon Seller observe 也必须提交各自的当前 access request/receipt。通过后 observe 才在内存运行并只持久化无金额、无客户/店铺/仓库/SKU/银行 reference、无 Marketplace/地区/卖家、无库存数量且无原始 ID 的控制摘要。每份 baseline 都必须由独立来源准备，不能从 observation 或 Pipeline result 反填。

完成七步证据链后，可由外部 cron、systemd timer、容器 Job 或云调度器调用 Box 的受控执行器。Box 自身仍不安装调度基础设施；它只负责显式审批、due-window 计算、原子租约、受限重试和审计记录。配置格式与告警状态见 [Pipeline 调度与可观测性](Pipeline调度与可观测性.md)。
