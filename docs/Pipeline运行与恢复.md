# Pipeline 运行与恢复

Pipeline 把多个 Connector、批次质量门和确定性 Service 组合成一个可重跑的财务工作流。它不会扩大任何底层能力的权限，也不会因为被放进 job plan 就自动安装定时任务。

## 当前 Pipeline

| Pipeline | 主体范围 | 阶段 | 外部动作 |
|---|---|---|---|
| `finance.bank_statement_close` | statutory | CSV/XLSX bank statement → quality/entity gate → account/currency reconciliation candidate | mapping、balance reconciliation 两道 gate；无核销/过账 |
| `finance.trial_balance_review` | statutory | CSV/XLSX accounting export → quality/entity-period gate → currency-separated balance and roll-forward validation | mapping、control-total 两道 gate；不改总账/期初、不过账、不关账 |
| `finance.accounting_close_review` | statutory | General Ledger + Trial Balance → dual quality gate → per-journal validation → per-account movement reconciliation → explicit mapping → currency-separated statements | export、control-total、statement mapping、accounting policy 四道 gate；不改账/期初、不过账、不关账、不申报 |
| `finance.first_close_discovery` | statutory | Bank + GL + Trial Balance → three quality gates → exact account inventory → GL/Trial movement check → fail-closed mapping starters | 四道配置 gate；不猜科目/现金映射、不过账、不关账 |
| `finance.month_close_control` | statutory | Bank Statement + General Ledger + Trial Balance → three quality gates → GL/TB reconciliation → explicit bank/GL cash mapping → Founder briefing | 七道 gate；source fingerprint 失效控制；不按金额认领、不过账、不关账、不申报 |
| `finance.multi_entity_month_close_portfolio` | management（至少两个显式主体） | 单主体月结候选 → 逐主体准备度 → 已批准 FX → 抵销前组合候选 → Founder portfolio briefing | `month_close_portfolio_review`；不相加原币、不执行抵销、不生成法定合并报表 |
| `commerce.import_analyze` | management | Connector → quality gate → deterministic analysis | 无 |
| `commerce.channel_close` | management（单一显式主体） | 标准 DTC Commerce Connector → quality/entity gate → order-to-cash → refunds → return/warehouse receipt → import landed-cost candidate → fulfillment → destination evidence | source、cutoff、valuation、return disposition、import landed-cost、sales-tax/nexus 六道 gate |
| `marketplace.channel_close` | management（单一显式主体） | Marketplace Connector → quality/entity gate → fees → receivable → return/warehouse receipt → import landed-cost candidate → platform/ledger inventory | source、contract、inventory、cutoff、valuation、return disposition、import landed-cost 七道 gate |
| `amazon_seller.transaction_close` | statutory | Amazon Seller Finances Connector → quality/entity/seller/Marketplace scope → transaction/component summary → Founder briefing | account、Marketplace、mapping、fee/tax、settlement completeness 五道 gate；不认定收入/税负/结算、不过账 |
| `amazon_seller.marketplace_close` | statutory | Amazon Orders v2026 + FBA 当前库存 + Finances → single quality/entity/seller/Marketplace scope → hashed order/SKU cross-check → Founder briefing | 九道范围、映射、完整性、库存、税务和结算 gate；不证明完整性，不认定收入/税负/银行结算/月末库存/COGS、不过账 |
| `game.channel_settlement_close` | management（单一显式主体） | 游戏渠道 XLSX → quality gate → 显式合同映射 → 结算/应收核对 | `channel_contract_mapping`、`game_principal_agent_assessment` |
| `stripe.daily_close` | statutory | Balance Connector → Payout Connector → quality gate → activity summary → bank candidate reconciliation | `stripe_mapping_approval` |
| `dtc.shopify_stripe_daily_close` | statutory | Shopify Orders → Stripe Balance/Payout → quality gate → order/payment/refund review → processor reconciliation → bank candidate reconciliation | Shopify、processor link、Stripe 三道 gate |
| `dtc.shopify_stripe_month_close` | statutory | Shopify created + updated-since-month-start close capture → refund component/transaction proof → same-window Stripe Balance → DTC metric operands | Shopify、processor link、Stripe、税含政策、实物退货证据五道 gate；不过账、不申报 |

编译后的 `pipeline-catalog.json` 是调用方契约。`implementation_status=executable` 只表示所需 Connector 与 Service 已存在，不表示 live secret、银行资料、review gate 有权人或调度告警已配置。

## 通用银行对账单请求

所有选择 `connector.file_import` 的 Box 都可运行同一个 Finance Core 路径：

```bash
python -m src.cli pipeline examples/boxes/cn_dtc_store.json \
  examples/pipelines/bank_statement_close_fixture.json
```

payload 必须包含一个法律主体、`YYYY-MM` 期间、`file.bank_statement` 和 CSV/XLSX 路径。Connector 强制稳定银行流水号、明确收支方向、ISO 日期和三位币种；导出文件缺币种或账户列时，调用方必须显式提供 `default_currency` 和已脱敏 `account_reference`。完整本方及对方账号只用于即时解析，标准数据集仅保留掩码。

同一主体内按账户与币种分别汇总收入、支出、期末余额及待认领数。质量通过只表示已形成余额调节候选；`bank_statement_mapping_review` 与 `bank_balance_reconciliation` 未完成前，系统不会声称银行账已调节，也不会自动认领、核销、生成凭证或跨主体/币种抵销。

## 通用试算平衡复核请求

所有选择 `connector.file_import` 的 Box 也会获得同一个 Record-to-Report 入口：

```bash
python -m src.cli pipeline examples/boxes/cn_dtc_store.json \
  examples/pipelines/trial_balance_review_fixture.json
```

payload 必须提供一个法律主体、`YYYY-MM` 期间、`file.trial_balance` 和 CSV/XLSX 路径。标准列包括科目编码/名称、币种、期初借贷、本期借贷发生额与期末借贷；期间和币种列缺失时必须通过请求显式提供默认值。重复的主体—期间—币种—科目行、非法金额、同一行期初或期末同时存在借贷余额，以及跨请求主体/期间的行都会失败关闭。

Service 分币种检查期末借方合计等于期末贷方合计；来源提供期初/发生额时，还检查 `期初净额 + 本期发生净额 = 期末净额`。`accounting_export_mapping_review` 与 `trial_balance_control_total_review` 仍需人工完成。即使全部平衡，也不证明科目映射、会计政策、导出完整性或期间已关账，并且不会修改总账/期初、生成过账或关闭期间。

## General Ledger 到报表候选请求

```bash
python -m src.cli pipeline examples/boxes/cn_dtc_store.json \
  examples/pipelines/accounting_close_review_fixture.json
```

该请求同时绑定 `file.general_ledger` 与 `file.trial_balance`。GL 行必须提供稳定凭证号、分录行号、记账日期、科目、借贷金额和币种；每行只能借或贷一侧为正。Connector 的稳定业务键是 `主体 + 期间 + 币种 + 凭证号 + 行号`，重复行失败关闭。Service 再逐凭证逐币种检查借贷，并逐科目把 GL 借贷发生额与 Trial Balance 本期发生额核对；不能用总额相等掩盖科目差异，也不能跨币种补平。

`account_mappings` 必须逐一声明源科目代码、可选精确名称、`assets/liabilities/equity/revenue/expenses` 报表组及稳定报表行。系统不会从科目名、编码前缀或余额方向推断分类。100% 映射、GL↔Trial 一致和报表方程平衡只形成候选；`accounting_export_mapping_review`、`trial_balance_control_total_review`、`financial_statement_mapping_review` 与 `accounting_policy_decision` 仍须独立完成，且不会触发过账、关账或外部申报。

## 银行、GL 与 Trial Balance 三方月结控制

首月配置先运行：

```bash
python -m src.cli pipeline examples/boxes/cn_dtc_store.json \
  examples/pipelines/first_close_discovery_fixture.json
```

Discovery 输出精确 `bank_account_inventory`、`account_inventory`、GL↔Trial 逐科目发生额核对、同币种全部可选 GL 科目列表，以及带真实 source fingerprint 的下一步请求。列表只是可选范围，不是现金科目建议。报表组、报表行、GL 现金科目、流水复核角色/说明/证据均保留 `REPLACE_WITH_...`；`configuration_starter.runnable_without_review=false`，preflight 会在占位符存在时阻断执行。

```bash
python -m src.cli pipeline examples/boxes/cn_dtc_store.json \
  examples/pipelines/month_close_control_fixture.json
```

`finance.month_close_control` 同时导入一个主体/期间的银行对账单、GL 和 Trial Balance。`bank_gl_mappings` 必须逐账户和币种声明掩码账户、GL 现金科目、当前银行来源 fingerprint、流水复核状态/角色/说明/证据以及逐项调节项目。GL 期末余额只能从 Trial Balance 的 `closing_debit - closing_credit` 派生，请求不能手填覆盖。

只有当前来源 fingerprint 一致、流水复核完成、所有调节项有证据且已批准、GL↔Trial 与报表候选通过、调整后银行余额等于调整后账面余额时，控制才进入 `ready_for_month_close_review`。该状态仍只是 `month_close_control_review` 的输入；金额相等不会触发流水认领、现金分配、凭证、关账或申报。Founder 简报按币种分别展示现金控制、资产、负债和税前利润候选，禁止跨主体或跨币种轧差。

## 多主体月结组合视图

```bash
python -m src.cli pipeline examples/boxes/global_game_studio.json \
  examples/pipelines/multi_entity_month_close_portfolio_fixture.json \
  --verify-source-runs --runs-root .opc-finance-data/pipeline-runs
```

payload 必须显式列出至少两个已配置主体，并为每个主体提供同一期间的 `finance.month_close_control` 运行 ID、证据引用、就绪状态和逐币种摘要。不能用一个主体的就绪状态代替另一个，也不能省略有异常的主体。

报告币种使用确定性 1:1 身份汇率。每个非报告币种必须提供与月份相同的已批准期间平均汇率与期末汇率，并绑定 `source_reference`、`reviewed_by` 和 evidence。收入/费用按期间平均汇率，现金/资产/负债按期末汇率。任一主体或汇率阻塞时，`management_portfolio_totals=null`，不显示部分总额。

这条 Pipeline 没有 Connector，不访问原始文件或网络；它只组合已存在的候选摘要。输出保留每个主体的原币视图，报告币种总额明确标注为 `pre_elimination_view`；它不执行内部抵销、CTA/权益处理、过账、关账或法定合并。

正式组合路径必须开启 `--verify-source-runs`，或通过工作台的记录运行 API 执行。每个 source 需要台账 `source_attempt_id` 与 `pipeline-ledger://attempts/<id>` 证据引用。台账不保存原始请求、完整结果或财务摘要，只保存从可组合字段规范化计算的 SHA-256 指纹。只有运行就绪、所有必需 gate 已批准、主体/运行 ID/摘要指纹全部一致时，`source_run_ledger_verified` 才为 true。

## 工作台启动与无副作用预检

工作台的 **Pipeline → 启动与预检** 读取当前运行时目录，而不是展示硬编码行业菜单。游戏、独立站和平台电商 Box 因此只看到自身 Pack 与 provider 共同启用的 Pipeline；每个 statutory/单主体入口会按已配置法律主体生成请求起点。

```text
GET  /api/box/pipelines
POST /api/box/pipelines/preflight
```

预检 POST 的 body 就是待运行的原始 `{pipeline_id, payload}`，不是额外 wrapper。它只校验当前 Box 是否启用该 Pipeline、主体和 Connector 是否在范围内、是否残留 fail-closed 占位符，以及 JSON key 是否疑似携带 token、password、secret、credential 或 authorization。凭证只允许经服务端环境配置，不能粘贴进编辑器。

预检不会连接 Shopify、Stripe、文件或银行来源，不会执行 Pipeline、写运行台账或改变业务状态，因此 reader 角色也可调用。只有预检通过且当前 principal 具有 operator/admin 权限时，工作台才解锁“运行并记录候选”；运行结果仍然只是 candidate，并须进入独立人工 gate。请求一经修改，已有预检立即失效。

## 游戏渠道结算请求

复制安全模板后替换路径和所有业务字段：

```bash
cp examples/pipelines/game_channel_settlement_close_template.json \
  outputs/game-channel-close.json
python -m src.cli pipeline examples/boxes/global_game_studio.json \
  outputs/game-channel-close.json
```

payload 必须包含单一 `entity_id`、一个已启用的游戏结算 `connector_id`、XLSX `connector_request` 和非空 `contract_mappings`。Pipeline 会把主体强制注入 Connector，不能用请求内的 `default_entity_id` 覆盖。

每个合同映射必须包含主体、账期、游戏、渠道、币种、合同基数、0–1 费率，以及带 `source_reference` 和 `captured_at` 的证据。映射按 `(entity_id, period, game, normalized channel, currency)` 精确连接；如果这个业务键在导入批次内不唯一，必须补充 Connector 生成的 `settlement_id`。每个导入行必须恰好被映射一次，未知映射、遗漏、复用、跨主体、无证据或模糊匹配都会在财务 Service 前失败关闭。

确定性服务只计算 `contract_basis × contract_rate + contract_adjustments`，并与渠道报告结算及扣缴后应收比较。它不推断合同条款、不决定总额法/净额法、不确认收入，也不执行收款、核销或过账。`ready=true` 仍只是等待两道独立人工 gate 的候选。

## 通用电商 / 独立站渠道关账

仓库内的完整离线 API Payload 样板可以直接运行：

```bash
python -m src.cli pipeline examples/boxes/cn_dtc_api_store.json \
  examples/pipelines/commerce_channel_close_fixture.json
```

真实使用时可将 `connector_id` 替换为当前 DTC Box 已启用的 `file.commerce`、`file.csv_commerce`、`file.xlsx_commerce` 或自定义 API Payload Connector。payload 强制包含单一 `entity_id`；Pipeline 会注入 Connector 默认主体，并在 Service 前再次确认整个批次只包含该主体。rejected row、重复业务键、空批次或跨主体数据都会失败关闭。运行时还会强制要求 DTC capability，所以 Marketplace Box 不能调用这个入口绕过平台专有控制。

通过质量门后依次执行六类确定性输出：订单—渠道结算与 payout 方程、退款摘要、退货授权—仓库实收—处置核对、进口批次 landed-cost 候选、COGS/履约/物流贡献摘要，以及目的地和已收税额证据摘要。退货数据分为 `commerce.returns` 与 `commerce.return_receipts`：一条授权按主体、退货单和 SKU 唯一，一条入库按主体和收货事件唯一；退款多于实收、超收、孤立入库和跨主体均失败关闭。待退回授权可以保留为 warning；`restockable` 只生成补库存候选，不自动改变数量、价值或总账。进口费用按主体、期间、币种、SKU、仓库分组；进口税默认排除在库存成本候选之外，分类、税率、可抵扣与入账均不由系统决定。目的地事实不推导 nexus、登记、税率或应纳税额；COGS 不替代已批准库存计价政策；订单和 payout 勾稽不决定收入截止。运行始终需要 `commerce_source_mapping`、`revenue_cutoff`、`inventory_valuation_policy`、`return_disposition_review`、`import_landed_cost_policy` 和 `sales_tax_nexus_review` 六道 gate。

早期的 `commerce.import_analyze` 保留为多主体数据探索兼容入口；新的 `commerce.channel_close` 是需要台账、职责分离和关账复核时的产品化路径。

## Marketplace 专用关账

```bash
python -m src.cli pipeline examples/boxes/cn_marketplace_store.json \
  examples/pipelines/marketplace_channel_close_fixture.json
```

请求包含一个 Marketplace 文件或可编辑 API Payload Connector，以及同一主体的 `platform_inventory` 与 `ledger_inventory`。Connector 可同时输出订单、结算、退货授权、退货入库和进口费用五类标准数据集。每条库存记录必须有 SKU、仓库、非负数量和证据；订单、结算、退货、入库、进口费用和两组库存事实在 Service 前共同接受单主体检查。

费用服务只核对平台报告费用、代扣税和 payout 方程，不解释合同；应收服务只核对订单净流入、平台报告流入和 payout 候选，不收款、不核销；退货服务按授权、实收、仓库与处置状态生成异常和补库存候选；进口成本服务只形成排除进口税的 landed-cost 候选；库存服务按主体、SKU、仓库比较期末数量，不生成调整。七道 gate 分别保护来源映射、合同解释、平台库存映射、收入截止、库存计价、退货处置和进口 landed-cost 政策。任何一项差异都保留在 Founder briefing 和运行账本的候选状态。

## Stripe 日结请求

```bash
python -m src.cli pipeline examples/boxes/cn_dtc_stripe_store.json \
  examples/pipelines/stripe_daily_close_fixture.json
```

HTTP 调用使用同一 JSON：

```text
POST /api/box/pipelines/dispatch
Content-Type: application/json
```

请求顶层为 `pipeline_id` 与 `payload`。Stripe payload 必须包含一个 `entity_id`、`balance_request`、`payout_request` 和 `bank_transactions`。Pipeline 会覆盖两个 Connector 的 `default_entity_id`；调用者如果传入不同主体会被拒绝。

银行金额必须是整数最小货币单位，且每条银行记录带 `entity_id`、`bank_transaction_id` 和 evidence。Pipeline 不根据货币代码猜小数位。

## Shopify + Stripe 日结请求

```bash
python -m src.cli pipeline examples/boxes/cn_dtc_shopify_stripe_store.json \
  examples/pipelines/shopify_stripe_daily_close_fixture.json
```

payload 必须包含 `entity_id`、`shopify_request`、`stripe_balance_request`、`stripe_payout_request`、`processor_links`、`currency_minor_units` 和 `bank_transactions`。Pipeline 将同一个主体强制注入三个 Connector；任何请求内的主体覆盖都会被拒绝。

`processor_links` 是人工或受控集成产生的一对一证据映射，每条必须带 `entity_id`、`shopify_transaction_id`、`stripe_source_object_id` 和 evidence。`currency_minor_units` 显式定义金额从 Shopify 十进制字符串转换为 Stripe 整数最小单位的指数。系统不会按币种名称猜指数，也不会按金额、日期或顺序猜跨处理器链接。

该 Pipeline 的“ready”只表示订单/收退款来源事实内部一致、显式跨处理器链接一致、Payout 与银行到账形成候选。它仍不执行收入确认、税额判断、COGS/利润计算、银行核销或总账过账。

## 幂等与 lineage

成功通过 Connector 后，`run_id` 由以下内容的规范化哈希生成：

- Box runtime fingerprint
- Balance Transaction batch ID
- Payout batch ID
- 银行证据内容
- 到达日容差

Shopify + Stripe Pipeline 还纳入 Shopify batch ID、显式 processor links、币种小数位配置和是否包含测试订单。processor links 与银行证据先按稳定 JSON 排序，因此相同证据不因这些集合的排列顺序产生新 `run_id`；Connector 原始对象顺序仍属于各自 batch contract。

游戏渠道 Pipeline 的 `run_id` 纳入 Box fingerprint、单一主体、Connector ID、结算 batch ID、规范化合同映射证据和金额容差。映射数组顺序不影响 `run_id`，但合同证据、条款或源文件变化会产生新的运行身份。

通用 Commerce Channel Close 的 `run_id` 纳入 Box fingerprint、单一主体、Connector ID、规范化 Connector request、batch ID 和金额容差。API Payload 内容或文件路径/映射变化会形成新的运行身份；完整 request 仍不会写入运行台账。

Marketplace Channel Close 还把规范化的平台库存与账面库存证据纳入 `run_id`；库存行排列顺序不改变运行身份，但数量、SKU、仓库或证据引用变化会产生新的身份。

相同输入重跑会得到相同 `run_id`、Connector batch ID、Founder briefing 和 lineage；`executed_at` 仅表示本次执行时间，不参与财务幂等键。纯计算入口默认不持久化，也不把结果写入总账；操作者可显式启用下述运行台账。

## 运行台账与人工复核

CLI 加 `--record` 会在计算后追加一条秘密值安全的控制记录：

```bash
python -m src.cli pipeline examples/boxes/cn_dtc_shopify_stripe_store.json \
  examples/pipelines/shopify_stripe_daily_close_fixture.json \
  --record --actor '月结执行人' \
  --runs-root .opc-finance-data/pipeline-runs

python -m src.cli pipeline-runs \
  examples/boxes/cn_dtc_shopify_stripe_store.json \
  --runs-root .opc-finance-data/pipeline-runs

python -m src.cli pipeline-run-show \
  examples/boxes/cn_dtc_shopify_stripe_store.json ATTEMPT_ID \
  --runs-root .opc-finance-data/pipeline-runs

python -m src.cli pipeline-review-queue \
  examples/boxes/cn_dtc_shopify_stripe_store.json \
  --runs-root .opc-finance-data/pipeline-runs

python -m src.cli pipeline-runs-verify \
  examples/boxes/cn_dtc_shopify_stripe_store.json \
  --runs-root .opc-finance-data/pipeline-runs
```

记录只保存 Box、请求和结果 fingerprint，`run_id`、attempt/idempotency 历史、Connector/Service 控制摘要、有限 lineage 字段以及复核事件。它不会自动复制原始请求、Connector 响应、完整结果、凭据或环境变量秘密值。人工填写的 rationale 和 evidence reference 仍属于持久化控制元数据，不要在其中粘贴 secret、原始财务明细或个人信息。

需要人工 gate 的 Pipeline 初始为 `pending_review`。复核只能追加事件，不能改写过去的决定：

```bash
python -m src.cli pipeline-run-review \
  examples/boxes/cn_dtc_shopify_stripe_store.json ATTEMPT_ID \
  --gate shopify_mapping_approval \
  --decision approved \
  --actor '独立复核人' \
  --rationale '抽样和映射证据完整' \
  --evidence-reference 'evidence://close/2026-07/shopify-v2' \
  --runs-root .opc-finance-data/pipeline-runs
```

允许的决定是 `approved`、`rejected` 和 `needs_more_evidence`。同一 gate 可以再次复核，`current_reviews` 采用最新决定，`review_history` 保留全部历史。只有 Pipeline 本身 `ready=true` 且所有必需 gate 的最新决定均为 `approved`，`release_candidate` 才为 true。它只是“复核控制已满足的候选”标识，`release_candidate_is_external_authorization` 永远为 false，不会触发核销、过账、付款、关账或申报。

HTTP 提供同一边界：

- `POST /api/box/pipeline-runs`：请求体为 `{ "actor": "...", "request": { ...Pipeline request... } }`，执行并记录。
- `GET /api/box/pipeline-runs`：按当前 Box runtime fingerprint 列出记录，可按 `pipeline_id`、`entity_id`、`limit` 过滤。
- `GET /api/box/pipeline-runs/{attempt_id}`：读取一个 attempt 的当前投影视图和完整复核历史。
- `POST /api/box/pipeline-run-reviews`：追加 `{attempt_id, gate, decision, actor, rationale, evidence_references}`。
- `GET /api/box/pipeline-review-queue`：列出尚未批准或被退回补证的 gate，可按 Pipeline、主体和数量过滤。
- `GET /api/box/pipeline-run-integrity`：重新验证完整 hash chain，并返回当前 Box 的 attempt/review 计数与 chain head。

调度执行复用同一账本，并新增 `PIPELINE_SCHEDULE_CLAIMED` 租约事件。它不会绕开人工 review gate，也不会把 ready attempt 解释为外部授权。完整 schema、CLI/HTTP 调用、重试与告警状态见 [Pipeline 调度与可观测性](Pipeline调度与可观测性.md)。

台账是逐行 JSONL，每个事件包含 sequence、previous hash 与 SHA-256 event hash；写入采用跨进程锁和逐事件 `fsync`，目录权限为 `0700`、文件权限为 `0600`。读取前会从 Genesis 验证整条链，损坏、删除中间行或事后改写都会失败关闭。这是 tamper-evident，不是不可篡改存储；正式运行仍应配置加密备份、保留策略、恢复演练和受控访问。编译产物 `pipeline-run-policy.json` 固化了当前 Box 的这些规则。

## 备份与安全恢复

备份复制的是一条物理 ledger 的完整 hash chain，可能包含同一存储目录中多个 Box runtime fingerprint 的控制元数据。因此备份目录应按财务审计资料保护，不能当成单一 Box 的公开导出。

```bash
python3 -m src.cli pipeline-runs-backup \
  /absolute/private/backups/pipeline-runs-2026-08-13 \
  --runs-root .opc-finance-data/pipeline-runs \
  --actor '备份操作人'

python3 -m src.cli pipeline-backup-verify \
  /absolute/private/backups/pipeline-runs-2026-08-13
```

备份命令只允许创建一个不存在的新目录，永不覆盖旧备份。目录内包含私有权限的 `pipeline_runs.jsonl` 和 `pipeline_runs_backup.json`；manifest 锁定文件 SHA-256、事件数与 chain head。校验会同时重算文件 fingerprint 并重放完整事件链。

恢复只允许写入从未存在 ledger 的空目标，不支持 merge 或 overwrite：

```bash
python3 -m src.cli pipeline-runs-restore \
  /absolute/private/backups/pipeline-runs-2026-08-13 \
  --runs-root /absolute/new-runtime/pipeline-runs \
  --actor '恢复操作人'
```

成功后会生成 `pipeline_runs_restore_receipt.json`，并重新验证恢复后的 chain。恢复功能故意不暴露为 HTTP API。若当前目标已有 ledger，应停止服务、保留现状并由有权人决定独立目录切换方案；工具不会替操作者覆盖或拼接审计证据。manifest 与 hash chain 仍不是数字签名或 WORM 存储，生产备份应另行加密、异地复制、限制访问并定期演练。

## 失败与恢复

| `blocked_at` | 含义 | 是否可直接重试 |
|---|---|---|
| `stripe_balance_connector` | 第一个 API/fixture Connector 失败 | 是，修复凭据、网络或响应后整单重跑 |
| `stripe_payout_connector` | 第二个 Connector 失败；第一个批次 lineage 会保留 | 是，修复后整单重跑 |
| `shopify_orders_connector` | Shopify API/fixture Connector 失败 | 是，修复凭据、版本、网络或响应后整单重跑 |
| `game_settlement_connector` | 游戏渠道文件缺失、不可解析或 Connector 不可用 | 是，修复文件或 Connector 后整单重跑 |
| `commerce_connector` | Commerce 文件/API Payload 缺失、不可解析或 Connector 不可用 | 是，修复来源后整单重跑 |
| `marketplace_connector` | Marketplace 文件/API Payload 缺失、不可解析或 Connector 不可用 | 是，修复来源后整单重跑 |
| `quality_gate` | rejected row 或重复业务键 | 否，先修复映射或源数据 |
| `entity_scope` | Commerce 批次不是且仅是请求指定的法律主体 | 否，拆分或修复主体映射后重跑 |
| `contract_mapping` | 游戏结算合同映射遗漏、复用、跨主体或存在歧义 | 否，补齐显式合同证据后整单重跑 |
| `settlement_reconciliation` | 合同公式、渠道报告结算或扣缴后应收存在差异 | 否，调查合同和来源证据后重跑 |
| `order_settlement_reconciliation` | 订单净流入、渠道报告流入或 payout 方程存在差异 | 否，调查来源、cutoff、费用和调整项后重跑 |
| `refund_summary` / `fulfillment_cost_summary` / `destination_evidence` | 下游确定性事实无法解析或不完整 | 否，修复对应证据后整单重跑 |
| `marketplace_fee_reconciliation` | 平台费用、代扣或 payout 方程有差异 | 否，复核平台合同和结算证据后重跑 |
| `marketplace_receivable_reconciliation` | 订单净流入与平台报告应收有差异 | 否，调查订单、退款、cutoff 和平台结算后重跑 |
| `marketplace_inventory_reconciliation` | 平台与内部台账库存不一致或证据不完整 | 否，调查后重跑；系统不会自动调库存 |
| `shopify_order_activity` | Shopify 订单、成功交易与退款事实不一致 | 否，调查源事实或映射后重跑 |
| `shopify_stripe_activity_reconciliation` | 显式链接缺失、重复，或金额/币种/类别不一致 | 否，补充证据或人工调查后重跑 |
| `payout_bank_reconciliation` | 银行缺失、候选歧义、余额关系或 payout 状态异常 | 否，补证据或人工调查后重跑 |

部分失败不会继续调用财务 Service。失败响应和日志只保留安全错误与已完成批次的 ID/质量，不包含 Authorization、环境变量值或 Stripe 响应体。

失败 attempt 也可以写入台账，并保留 `blocked_at` 与 `retryable`。修复后应重跑完整 Pipeline；相同 `idempotency_key` 的每次尝试都会生成独立 `attempt_id`，后一次通过 `duplicate_of_attempt_id` 链接此前尝试。台账不恢复半个执行栈，也不会从中间阶段继续财务计算，因此不会把部分成功误当成完整日结。

## 上线检查

1. 在 Stripe sandbox 和脱敏银行数据上完成 fixture 与 pipeline 回归。
2. 配置只读 restricted key、固定出口 IP（如适用）、密钥轮换和 API 失败告警。
3. 明确法律主体与 Stripe account 的一对一映射；不要用请求头临时切换 connected account。
4. 运行至少一个完整期间的 shadow reconciliation，调查所有 fee、refund、failed payout 和 timing difference。
5. 为 `stripe_mapping_approval` 配置主审和替补。
6. 只有在上述步骤完成后才单独配置调度；编译器永远保持 `enabled=false`。

Shopify + Stripe Box 还应先确认 token 的 `read_orders` 权限、历史订单访问范围、店铺与法律主体映射、API 版本升级负责人、processor link 的产生机制，并为 `shopify_mapping_approval` 与 `processor_link_mapping_approval` 配置有权人。

Pipeline 输出中的 `high_confidence_candidate` 仍需人工确认。它不等于银行已核销、会计凭证已过账、期间已关账或税务处理已批准。
