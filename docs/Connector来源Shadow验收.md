# Connector 来源 Shadow 验收

财务报表 Shadow Close 回答“系统关账结果是否与人工关账一致”；Connector 来源 Shadow 回答更前置的问题：“网络来源是否覆盖了同一个法律主体、期间和来源范围，以及该 Pipeline 的关键确定性控制是否完整运行”。两者不能互相替代。

当前合同按 Pipeline 提供严格 profile，覆盖 Stripe 日结、Shopify + Stripe 日常四来源链、Shopify + Stripe 自然月链、Airwallex 已批准费用、Xero Trial Balance、Wise 银行流水，以及 ShipBob、PayPal、WooCommerce、Amazon Seller 专用只读链路。独立准备人先从来源后台或受控导出核对记录数，填写私有 baseline；系统不会用一个 profile 的证据替另一个 Connector 宣称完整覆盖。

四来源 profile 覆盖：

- Shopify order/transaction/refund 记录总数；
- Stripe Balance Transaction 和 Payout 数；
- Wise Balance Statement 流水数；
- Processor link 匹配/异常数；
- Payout→银行候选/异常数；
- Pipeline 是否达到候选就绪状态。

baseline 的 `covered_pack_ids` 必须明确列出这份样本实际覆盖的 Pack。当前四来源示例覆盖 `connector.shopify`、`connector.stripe`、`connector.wise` 和 `feature.shopify_stripe_order_to_cash`；不能把未被该 Pipeline 运行和核对的 Connector 填入覆盖范围。

Airwallex schema v1 演示 profile 覆盖批准费用记录数、收据/用途/清算/会计映射缺口，以及没有创建报销、过账、付款或其他外部动作。schema v2 真实 profile 额外要求：状态变更候选数、确实发生网络只读 refetch、batch 的 update-capture basis 明确为 `signed_webhook_then_read_only_refetch`，且 webhook context 已绑定 receipt/event/body/expense/Box 指纹并通过验证。因此 fixture、普通窗口 fetch 或缺少 claim 上下文的手工 refetch 即使费用计数相同也不能冒充 webhook 更新捕获证据。评估器还会逐行确认费用 `entity_id` 与 baseline 主体一致，`created_at` 属于 baseline 月份；金额和原始费用 ID不会进入 assessment。

Xero 真实 profile 覆盖 Trial Balance 行数与 scope 数、借贷平衡/不平衡数、roll-forward 是否被错误声称为已执行、真实网络快照、月末 `as_at`、`payments_only=false`、主体/本位币绑定、point-in-time/YTD 口径和写动作关闭。baseline 必须期望所有 scope 平衡、零个 roll-forward checked scope；这不会把平衡误称为完整性、账户映射、已过账或已关账。fixture、非月末、cash-only 或跨主体/跨币种结果不能通过真实 profile。

Shopify + Stripe 月结 profile 覆盖当月创建订单、月初以来更新订单、去重订单、当月退款事件、Shopify 订单/交易/退款与同窗 Stripe Balance Transaction 数量，以及双人口、退款组件/成功退款交易闭合、主体、自然月半开区间、72 小时 close capture 和只读候选边界。它只覆盖 `connector.shopify` 与 `feature.shopify_stripe_order_to_cash`；完整 `connector.stripe` 覆盖仍由独立 `stripe.daily_close` 的 Balance/Payout→银行控制提供，Wise 仍由 `finance.bank_statement_close` 提供。

示例只用于演示字段结构：[四来源基线](../examples/shadow/sg_shopify_stripe_wise_connector_baseline.json)、[Airwallex 费用基线](../examples/shadow/sg_airwallex_expense_connector_baseline.json)。两者都是 schema v1 demonstration，能用于本地评估，但会被 stable promotion 明确拒绝。真实 baseline 必须由下面的 schema v2 工作底稿流程生成；evidence reference 必须指向独立取得的来源导出、范围确认和工作底稿，不能把 Pipeline 自己的输出当作独立基线，也不能使用 `demo`、`fixture`、`example` 或 `synthetic` reference。

## 真实匿名 baseline 工作底稿

先生成与精确 Box fingerprint、Pipeline、法律主体、期间和 Pack 覆盖绑定的私有工作底稿：

```bash
opc-finance-box connector-shadow-baseline-init \
  examples/boxes/sg_dtc_shopify_stripe_wise_airwallex_store.json \
  --pipeline finance.expense_evidence_review \
  --entity sg_store \
  --period 2026-08 \
  --prepared-by independent-source-preparer \
  --output private-airwallex-shadow-workpaper.json
```

生成文件权限为 `0600`，所有来源计数、控制期望和 evidence reference 初始为空，且 `finalization_ready=false`。独立准备人必须从 Airwallex 后台或受控来源导出核对，而不是读取待评估 Pipeline 的输出。填完后需要显式确认：

- `prepared_from_independent_source=true`；
- `pipeline_output_used_as_baseline=false`；
- `source_scope_confirmed=true`；
- baseline 已移除原始标识符和金额；
- 原始来源证据仍在私有证据库中留存；
- `finalization_ready=true`。

然后封存严格 schema v2 baseline：

```bash
opc-finance-box connector-shadow-baseline-finalize \
  examples/boxes/sg_dtc_shopify_stripe_wise_airwallex_store.json \
  private-airwallex-shadow-workpaper.json \
  --output private-airwallex-shadow-baseline.json
```

finalizer 会拒绝缺失计数/布尔值、被改动的来源或控制范围、错误主体/Box fingerprint、demo/fixture/Pipeline-output reference、credential-like reference，以及未完成的独立性或匿名化签认。封存 baseline 只包含计数、布尔值和证据引用，不复制真实 expense ID、客户、账户或金额。

Airwallex 真实 webhook refetch 可直接生成最小化 Pipeline observation，避免把金额或原始 expense ID 写到终端/工作文件：

```bash
opc-finance-box airwallex-webhook-process BOX.json \
  --request-base airwallex-request-base.json \
  --actor airwallex_webhook_worker \
  --limit 1 \
  --shadow-output private-airwallex-shadow-observation.json
```

该 observation 绑定完整内存 Pipeline result 的 SHA-256，但内容只保留 assessor 所需的主体、期间、计数、状态候选和 webhook/refetch 证明。它不是独立 baseline，也不证明来源后台导出已经保存；后者仍必须由不同准备人另行保留并在 baseline 中签认。

Xero 真实快照同样不需要先把含余额的完整 Pipeline result 落盘，也不需要手写 live request：

```bash
opc-finance-box xero-shadow-request-init BOX.json \
  --entity ENTITY_ID --period 2026-07 \
  --output private-xero-live-request.json

opc-finance-box xero-shadow-request-verify BOX.json \
  private-xero-live-request.json

opc-finance-box xero-shadow-observe BOX.json \
  private-xero-live-request.json \
  --output private-xero-shadow-observation.json
```

init 从已验证 Box 取得主体绑定，按自然月生成精确月末 `as_at`，固定使用 accrual-basis 的 `payments_only=false`，并排他写入 `0600` 文件。请求不允许 token、tenant / organisation binding、账户标识或金额；verify 不联网且不回显这些值。observe 只接受该完整 `finance.trial_balance_review` 请求，在内存中执行 `xero.trial_balance` fetch。输出不含账户名称、代码、原始 UUID、UUID hash、tenant / organisation binding 或任何余额，只保留主体/期间/币种、行数、平衡控制和来源口径布尔值，并绑定完整内存结果 SHA-256。

Stripe 的完整 Pack 覆盖使用独立 `stripe.daily_close`，先生成精确自然月、主体绑定的私有请求，再填入银行到账证据：

```bash
opc-finance-box stripe-shadow-request-init BOX.json \
  --entity ENTITY_ID --period 2026-08 \
  --output private-stripe-live-request.json

opc-finance-box stripe-shadow-request-verify BOX.json \
  private-stripe-live-request.json

opc-finance-box stripe-shadow-observe BOX.json \
  private-stripe-live-request.json \
  --access-request private-stripe-access-request.json \
  --access-receipt private-stripe-access-receipt.json \
  --output private-stripe-shadow-observation.json
```

请求使用 `0600` 且拒绝覆盖，Balance Transaction 与 Payout 必须共享同一自然月 Unix 半开区间。银行流水 ID、金额、reference 和来源证据只留在私有请求中；verify 不联网且不回显这些值。observe 要求真实 API、同窗、两批次零拒绝、每笔 Payout 都形成候选且零异常，输出只保留计数、来源窗口、分页/重试控制、候选状态和完整结果 SHA-256，不含处理器/银行原始 ID、银行 reference 或金额。assessment 还会再次将两份来源窗口绑定到 baseline 月份，因此修改 observation 指纹也不能把错月结果伪装成通过。

Wise 独立银行流水验收不再依赖 Shopify/Stripe 四源组合 Pipeline，也不需要用户手写 live request：

```bash
opc-finance-box wise-shadow-request-init BOX.json \
  --entity ENTITY_ID --period 2026-07 \
  --output private-wise-live-monthly-request.json

opc-finance-box wise-shadow-request-verify BOX.json \
  private-wise-live-monthly-request.json

opc-finance-box wise-shadow-observe BOX.json \
  private-wise-live-monthly-request.json \
  --output private-wise-shadow-observation.json
```

init 直接从已验证 Box 取得主体本位币并生成精确 UTC 自然月半开区间，使用 `0600`、拒绝覆盖、无需人工编辑。请求不允许 token、profile/balance ID、business name、access contract、账户 reference 或金额；verify 在联网前重新检查主体 Connector binding、本位币、月份、严格字段和文件权限。observe 只接受 `finance.bank_statement_close` 的真实 `wise.balance_statement` 请求。Provider 必须先证明期初余额、逐笔 CREDIT/DEBIT running balance 与期末余额连续；observation 只保留主体/期间/币种、交易日期、计数和控制布尔值，不保存金额、账户尾号、交易对手、reference 或原始 ID。

PayPal 余额影响交易同样使用完整生成的 production 请求与安全 observation：

```bash
opc-finance-box paypal-shadow-request-init BOX.json \
  --entity ENTITY_ID --period 2026-08 \
  --output private-paypal-live-request.json

opc-finance-box paypal-shadow-request-verify BOX.json \
  private-paypal-live-request.json

opc-finance-box paypal-shadow-observe BOX.json \
  private-paypal-live-request.json \
  --output private-paypal-shadow-observation.json
```

init 固定 production、每页 500 条、最多 20 页和精确 UTC 自然月，无需人工编辑且不包含 OAuth、商户账户、客户、交易 ID 或金额。observe 强制真实 Transaction Search、内存 OAuth、只查询 `transaction_info` 与 balance-affecting records、零拒绝/重复和全部写动作关闭；输出只保留月份、分页/重试、交易/退款/冲正/引用复核计数和隐私控制，不含金额、客户、自由文本或原始/哈希交易标识。

ShipBob 订单、发货与退货证据也使用完整生成的 production 请求与安全 observation：

```bash
opc-finance-box shipbob-shadow-request-init BOX.json \
  --entity ENTITY_ID --period 2026-08 \
  --output private-shipbob-live-request.json

opc-finance-box shipbob-shadow-request-verify BOX.json \
  private-shipbob-live-request.json

opc-finance-box shipbob-shadow-observe BOX.json \
  private-shipbob-live-request.json \
  --access-request private-shipbob-access-request.json \
  --access-receipt private-shipbob-access-receipt.json \
  --output private-shipbob-shadow-observation.json
```

init 固定 production、每页 100 条、最多 50 页和精确 UTC 自然月，无需人工编辑且不包含 token、channel、来源 ID 或金额。observe 先验证当前主体 access request/receipt，再强制真实 `2026-07` Orders/Returns 只读、绑定 channel header、零拒绝/重复、结构完整和全部写/过账/库存动作关闭；输出只保留订单/发货/退货/退货项及复核候选计数、分页/重试与隐私控制，不含履约金额、channel、仓库、SKU、状态、处置候选、客户、运单或来源键。ShipBob access probe 另行读取 Channels，要求绑定 channel 与精确四项 read scope。

Amazon Seller 三源 Marketplace 证据使用显式范围选择和无需手改 JSON 的私有链：

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

init 只接受已结束完整自然月，固定 production、Orders created-time 范围和 Orders / FBA Inventory / Finances 各 20 页上限。Marketplace ID 只写入 `0600` 私有请求并必须匹配当前主体环境 allowlist；Seller、区域与三项 LWA 别名只走环境绑定。observe 先验证当前主体 access request/receipt，再强制三源真实网络读取、一次内存 LWA、同一 Seller binding、固定区域端点、零拒绝/重复和全部外部动作关闭；安全 observation 不含 Marketplace、区域、卖家、客户、商品、库存数量、状态、金额或任何业务键。Amazon access probe 用 Sellers、Orders、FBA Inventory、Finances 四个最小 GET 验证读取边界，但不冒充 provider 已反查 Seller ID，Finances 值也不持久化。

WooCommerce 修改订单与退款事件使用同样完整生成、无需人工编辑的私有链：

```bash
opc-finance-box woocommerce-shadow-request-init BOX.json \
  --entity ENTITY_ID --period 2026-08 \
  --output private-woocommerce-live-request.json

opc-finance-box woocommerce-shadow-request-verify BOX.json \
  private-woocommerce-live-request.json

opc-finance-box woocommerce-shadow-observe BOX.json \
  private-woocommerce-live-request.json \
  --output private-woocommerce-shadow-observation.json
```

站点 origin 与 Consumer Key/Secret 只从运行环境读取，绝不进入请求或 stdout。init 固定精确 UTC 自然月、每页 100 条和最多 100 页；observe 要求真实 REST API v3 Orders/Refunds 只读、主体/站点哈希绑定、零拒绝/重复/孤儿退款/算术阻塞并关闭全部业务写入、过账、收入、税负与库存动作。observation 只保留订单/退款计数、分页/重试及控制布尔值，不含金额、站点域名、客户、商品、支付方式或订单/退款标识。

Shopify + Stripe 自然月链先生成一个主体/月份绑定的私有请求模板；系统自动填入 Shopify UTC 与 Stripe Unix 的精确同月边界，操作者只填写店铺域名、实际币种精度和显式处理器链接：

```bash
opc-finance-box shopify-monthly-shadow-request-init BOX.json \
  --entity ENTITY_ID --period 2026-07 \
  --output private-shopify-monthly-live-request.json

opc-finance-box shopify-monthly-shadow-request-verify BOX.json \
  private-shopify-monthly-live-request.json
```

模板使用 `0600` 且拒绝覆盖，初始为空链接和占位币种，因此不会被误当作可运行证据。verify 不访问网络，也不返回店铺或原始 ID；它会失败关闭地检查主体 Connector binding、自然月同窗、测试订单关闭、币种精度、逐笔一对一链接、证据引用、重复 ID、内联凭证和严格字段范围。验证通过后才直接生成最小化 observation，而不落盘完整订单与处理器金额：

```bash
opc-finance-box shopify-monthly-shadow-observe BOX.json \
  private-shopify-monthly-live-request.json \
  --shopify-access-request private-shopify-access-request.json \
  --shopify-access-receipt private-shopify-access-receipt.json \
  --stripe-access-request private-stripe-access-request.json \
  --stripe-access-receipt private-stripe-access-receipt.json \
  --output private-shopify-monthly-shadow-observation.json
```

命令只接受 `dtc.shopify_stripe_month_close` 请求，并要求 Shopify 与 Stripe 都确实执行网络只读、主体与自然月窗口一致、Shopify 在月末后 72 小时内捕获、双人口及退款闭合通过且没有外部动作。observation 不含金额、店铺域名或原始订单/交易/退款 ID，只保留 assessor 所需计数/布尔控制和完整内存结果 SHA-256。

## 评估、复核与验证

```bash
opc-finance-box pipeline examples/boxes/sg_dtc_shopify_stripe_wise_store.json \
  live-four-source-request.json > private-pipeline-result.json

opc-finance-box connector-shadow-assess \
  examples/boxes/sg_dtc_shopify_stripe_wise_store.json \
  private-source-baseline.json private-pipeline-result.json \
  --output private-connector-shadow-assessment.json

opc-finance-box connector-shadow-review \
  examples/boxes/sg_dtc_shopify_stripe_wise_store.json \
  private-connector-shadow-assessment.json \
  --decision passed \
  --actor independent-shadow-reviewer \
  --rationale "四个来源范围与跨来源控制已复核" \
  --evidence-reference review://sg-store/2026-08/connector-shadow \
  --output private-connector-shadow-reviewed.json

opc-finance-box connector-shadow-verify \
  examples/boxes/sg_dtc_shopify_stripe_wise_store.json \
  private-connector-shadow-reviewed.json
```

`assess` 接受 CLI `{ok,result}` envelope、原始 Pipeline result，或上述受控 Airwallex/Xero/Wise/Shopify 月结 observation。输出使用 `0600`、拒绝覆盖，并绑定 Box fingerprint、完整 baseline SHA-256 与完整 Pipeline result SHA-256；observation 模式使用 worker 在内存中对完整 result 计算的 SHA-256，而不是把敏感 result 落盘。schema v2 assessment 还保存独立性/匿名化 attestation 的 SHA-256 和 `real_sample_evidence=true`，不保存 attestation 原文或私有路径。验收文件只保留来源计数、控制计数和匹配布尔值，不复制订单、客户、店铺域名、账户、银行流水或金额。

复核人必须与 baseline 准备人不同；review 绑定精确 assessment fingerprint 和带时区的 `reviewed_at`，任何计数、结论、时间或指纹改动都会使验证失败。`passed` 仍只表示来源 Shadow 样本通过，不会自动批准 Connector 为 stable，也不会授权核销、过账、支付或申报。

## Stable 晋级衔接

当 `promotion-assess` 的目标 Pack 拥有已选中的网络 Connector 时，`connector_shadow_artifacts` 必须按 stable 样本中的每个“法律主体 × 月份”引用一份当前、已独立复核的 Connector Shadow 文件。评估器会重新验证：

- Box fingerprint、目标 Pack 覆盖、主体和期间，以及 schema v2 `real_anonymized` 分类；
- 完整 baseline 与 Pipeline result SHA-256；
- review 是否当前、是否在 `maximum_shadow_age_days` 内；
- baseline 准备人和复核人与 Pipeline 操作人的职责分离；
- assessment 与 review 是否都为 `passed`。

schema v1/demo artifact 会在进入 promotion assessment 前直接失败；缺少完整样本覆盖会产生 `connector_shadow_coverage` blocker；接受差异或控制未通过会产生 `connector_shadow_not_passed` blocker。私有文件路径、来源/控制明细和原始数据不会进入发布账本，账本只保存覆盖 Pack、主体、期间、计数、复核身份、真实样本分类和各层指纹的安全摘要。Xero 现在已具备形成 stable 候选所需的机器验收链，但在真实租户样本、OAuth 运维和独立复核尚未完成前仍保持 experimental。
