# CFO 指标操作数自动组装

Finance Box 会在受信任的 Pipeline 或 Service 完成后、结果离开运行时边界前，生成 `cfo_metric_operand_assembly`。它把已经标准化的结果映射成主体、自然月、币种和可选业务维度绑定的指标操作数，并立即给出确定性 `evaluation_preview`。用户不需要把结果数字复制到另一份请求中。

自动组装不是自动批准。每份 assembly 都分别列出：

- `confirmed_control_type_ids`：能够由当前确定性结构、范围或权威校验器直接证明的控制；
- `pending_control_type_ids`：仍需会计政策、成本分摊、存货计价、税务口径或人工证据复核的控制；
- `operand_provenance`：标准操作数、稳定来源字段类型、推导类型和来源结果指纹；
- `evaluation_request` 与 `evaluation_preview`：仅在来源币种等于主体本位币时生成；
- `coverage_blocker_type_ids`：来源合同不足以形成指标时的显式原因。

## 当前可执行映射

| 受信任来源 | 自动形成的指标候选 | 保留的人工边界 |
| --- | --- | --- |
| `finance.bank_statement_close` | 未勾稽现金项目数 | 重复项和时间性项目复核 |
| `finance.month_close_control` | 权威月结阻塞数 | 无；数字只表达校验器阻塞，不代表已关账 |
| `commerce.channel_close` | DTC 净销售、退款退货率、贡献率 | 税含口径和 landed cost 政策 |
| `dtc.shopify_stripe_month_close` | 月末捕获的 DTC 净销售、退款率 | 税含口径、实物退货授权/收货证据和显式换汇 |
| `game.project_profitability` | 按游戏标题维度的直接贡献率 | 共享成本分摊证据 |
| `marketplace.channel_close` | 不含税 GMV 口径的平台费率、Marketplace 净收入集中度 | 费用类型/税务处理和完整平台总体复核 |
| `amazon_seller.marketplace_close` | 按单一 Marketplace 自然月绑定的 Orders–Finances–FBA 当前库存三源范围匹配率 | 哈希跨源键人工复核；不代表来源完整 |

游戏渠道结算和 Shopify + Stripe 日结仍会返回明确的来源合同阻塞；只有新的月末 close-capture Pipeline 能形成 Shopify 指标操作数。它分别捕获当月创建订单和月初以来更新订单，以 `Refund.processedAt` 过滤本月退款，并要求退款组件、成功退款交易与 Stripe 同期范围对平。通用 Marketplace 只自动形成已有来源能证明的费率与渠道集中度；结算净额、保留款和历史期末库存覆盖仍显式阻塞。Amazon 三源匹配只比较已发货 FBA 订单的哈希订单/SKU 范围，当前 FBA 库存绝不会被当作历史期末库存。系统不会把结算款偷换成收入，也不会把范围匹配率偷换成完整性结论。

## 币种与维度

操作数先保持来源币种。若来源币种不是主体本位币，assembly 返回 `blocked_source_currency_not_functional_currency`，不生成可执行请求，也不做隐式换汇。完成上游显式换汇和证据复核后，应重新形成主体本位币范围的标准结果。

游戏项目等细分指标通过 `dimension_scope` 绑定。例如：

```json
{
  "dimension_type_id": "game_title",
  "dimension_value_ids": ["G1"]
}
```

维度范围会进入指标输入指纹，避免不同标题的相同金额被误认为同一份指标证据。Marketplace 人口维度按渠道名称稳定排序；Amazon 三源候选绑定唯一 Marketplace ID，不能跨平台合并比率。

## 运行账本

Pipeline 账本继续执行最小留存：保存来源结果指纹、assembly ID、主体/期间/币种、指标和控制状态，但不保存操作数或计算值。完整来源结果仍不持久化。账本是防篡改控制记录，不是财务明细仓库，也不能用于事后重建指标数字。

来源映射定义随 Pack 写入 `cfo-metric-catalog.json`。`coverage_status=executable` 表示运行时具备确定性映射，不表示数据已齐备或人工控制已完成；所有结果仍是经营管理候选，不是权威会计或税务结论。

通用输出形状位于 `box/cfo-metric-operand-assembly.schema.json`；当前 Box 允许的来源映射和指标集合仍以编译后的 `cfo-metric-catalog.json` 为准。
