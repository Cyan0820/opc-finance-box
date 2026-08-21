# Stripe Connector Pack

`connector.stripe` 是面向游戏、订阅、电商和独立站 OPC 的只读支付证据连接器。它提供两个独立 provider：

- `stripe.balance_transactions` → `payments.stripe_balance_transactions`
- `stripe.payouts` → `payments.stripe_payouts`

Balance Transactions 表示影响 Stripe 账户余额的交易；Stripe 也建议在会计分类时优先考虑 `reporting_category`，而不是把 `type` 当作会计科目。Payout 则表示发往银行账户或借记卡的打款。两者因此都属于对账证据，不自动等同于订单、履约收入、税务收入或总账凭证。字段与对象边界依据 Stripe 的 [Balance Transactions API](https://docs.stripe.com/api/balance_transactions)、[Payouts API](https://docs.stripe.com/api/payouts) 和 [分页契约](https://docs.stripe.com/api/pagination)。

## 安全设置

在 Stripe Dashboard 创建 restricted key，只给 Balance Transactions 与 Payouts 所需的读取权限，然后通过服务端 secret manager 或环境变量提供：

```bash
export OPC_STRIPE_RESTRICTED_KEY='rk_test_...'
```

不要把值写入 Box JSON、`.env` 示例、fixture、命令参数或聊天消息。Stripe 官方也建议优先使用最小权限 restricted key、避免把密钥放入源码，并在有固定出口 IP 时配置 IP 限制，详见 [Stripe API key best practices](https://docs.stripe.com/keys-best-practices)。

Pack 固定访问 `https://api.stripe.com/v1/balance_transactions` 和 `https://api.stripe.com/v1/payouts`，不接受调用者覆盖 URL、Authorization 或账户头；请求固定携带 `Stripe-Version: 2026-06-24.dahlia`。版本升级前应按 Stripe 的 [API versioning](https://docs.stripe.com/api/versioning) 说明运行 fixture、sandbox 与 shadow reconciliation，再修改 Pack 版本和 provider contract。

## 离线验收

```bash
python packs/connectors/stripe/provider_contract_test.py \
  examples/boxes/cn_dtc_stripe_store.json

python -m src.cli eval evals/core_packs.json
```

fixture 覆盖一笔 charge、一笔负数 refund 和一笔 payout。自动测试另外覆盖 429 重试、`starting_after` 分页、失败 payout、重复业务键、未知主体、缺密钥、响应契约错误和密钥/响应体脱敏。

## 确定性核对服务

选中 Stripe Pack 后，Box 同时开放两个只读财务服务：

- `stripe.summarize_balance_activity`：按主体、币种与 `reporting_category` 汇总 amount、fee、net、退款流出与 pending 风险，并检查 `amount_minor - fee_minor = net_minor`。
- `stripe.reconcile_payouts`：逐笔连接 Payout、对应 Balance Transaction 与银行到账证据。

可直接运行虚构示例：

```bash
python -m src.cli dispatch examples/boxes/cn_dtc_stripe_store.json \
  examples/service_requests/stripe_payout_reconciliation.json
```

产品化调用优先运行完整 pipeline，避免手工复制两个 Connector 的输出：

```bash
python -m src.cli pipeline examples/boxes/cn_dtc_stripe_store.json \
  examples/pipelines/stripe_daily_close_fixture.json
```

同一请求也可 POST 到 `/api/box/pipelines/dispatch`。编译后的 `pipeline-catalog.json` 会声明所需 Connector、Service、主体范围、阶段和外部动作边界；`job-plan.json` 只给出默认关闭的调度候选。

银行输入必须显式提供整数 `amount_minor`。候选匹配至少满足同一法律主体、同币种、同金额，并且银行参考包含 payout ID，或银行交易日在 `arrival_date` 配置窗口内且候选唯一。重复 ID、余额交易缺失、金额符号不匹配、多笔银行候选、银行候选被复用以及失败 payout 都会形成异常。

即使 payout ID 精确命中，输出仍为 `high_confidence_candidate`，并保持 `candidate_only=true`、`bank_reconciliation_completed=false`、`posting_performed=false`。Founder briefing 只给出分币种事实与风险信号，不生成跨币种总额。

## Fetch 请求

Connector 请求始终指定目标法律主体；可选时间窗口使用 Unix timestamp：

```json
{
  "mode": "fetch",
  "default_entity_id": "cn_dtc_company",
  "created_gte": 1785542400,
  "created_lt": 1788220800,
  "max_pages": 50
}
```

Payout provider 还允许 `status` 为 `pending`、`paid`、`failed` 或 `canceled`。网络访问具有页数、重试、超时和单响应大小上限；分页游标重复、响应不是 Stripe list、`has_more` 无法取得下一对象 ID 或达到页数上限时会失败关闭。

## 真实 Connector Shadow

真实首次读取不要手写一个长期保存的完整 Pipeline result。先生成主体与自然月绑定的私密请求：

```bash
opc-finance-box stripe-shadow-request-init BOX.json \
  --entity ENTITY_ID --period 2026-08 \
  --output private-stripe-live-request.json
```

模板自动给 Balance Transactions 与 Payouts 写入完全一致的 UTC 自然月 Unix 边界，并使用 `0600`、拒绝覆盖。操作者只在 `bank_transactions` 中填写该主体的银行到账证据；其中包含金额、银行流水 ID、reference 和私有来源引用，所以不能上传到 repo、assessment 或聊天。请求不允许内联密钥，银行金额必须是正整数最小货币单位，日期必须位于目标月份或最多七天的显式到账容差内。

联网前先做失败关闭的本地预检，再在内存执行真实读取：

```bash
opc-finance-box stripe-shadow-request-verify BOX.json \
  private-stripe-live-request.json

opc-finance-box stripe-shadow-observe BOX.json \
  private-stripe-live-request.json \
  --access-request private-stripe-access-request.json \
  --access-receipt private-stripe-access-receipt.json \
  --output private-stripe-shadow-observation.json
```

verify 不访问网络，也不返回银行 ID、reference 或金额。observe 只接受同窗、真实 API、无拒绝记录、每笔 Payout 都形成复核候选且零异常的结果；持久化 observation 只含记录数、分页/重试摘要、候选状态和完整内存结果 SHA-256，不含 Balance/Payout/银行原始 ID、银行 reference 或任何财务金额。它仍不是独立来源 baseline，也不会自动完成核销、过账或 stable 晋级。

access receipt 默认 30 天有效；Workbench 会在到期前 7 天显示 `renewal_due` 和剩余天数，此时原有有界读取仍可运行。restricted key 轮换后，即使旧回执未过期也会立即进入 `renewal_required`。配置新的 `rk_test_` / `rk_live_` 最小权限密钥后，运行 `connector-access-receipt-renew BOX.json private-stripe-access-request.json private-stripe-access-receipt.json --allow-network`。命令必须重新执行账户绑定、Balance Transactions 和 Payouts 三项有界只读检查；成功后保留旧回执原字节归档并原子安装新回执。密钥、account ID、归档路径和原始响应不会进入输出。完整操作边界见 [Connector 上线准备](Connector上线准备.md#回执续期与密钥轮换)。

所有金额保存为 `amount_minor`、`fee_minor`、`net_minor` 整数，不在 Connector 层猜测货币小数位。这避免把 JPY 等零小数货币错误除以 100。币种转为大写三字母代码；换算、收入确认、退款政策、手续费归类以及银行候选的最终确认留给确定性 Finance 服务与 `stripe_mapping_approval` 人工 review gate。

## 当前边界

- 当前不使用 webhook，因此没有 webhook signature 验证路径。
- 当前不自动读取 Charges、PaymentIntents、Invoices、Subscriptions 或 Checkout Sessions。
- Stripe Connect 模式只接受当前、已通过的私有 access request/receipt；account ID 只在 dispatch 内存中进入 `Stripe-Account` 头，不进入 Connector 或 Shadow 输出。
- 当前不保存 payout destination，避免把银行目标标识扩散到标准数据集。
- `livemode` 会保留在 Payout 记录中；从 sandbox 切到 live 前必须完成一次受控 shadow reconciliation。

Pipeline 的阶段失败、幂等键和恢复语义见 [Pipeline 运行与恢复](Pipeline运行与恢复.md)。
