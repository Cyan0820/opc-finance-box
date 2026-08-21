# OPC Finance Box 架构

## 产品定义

OPC Finance Box 不是按行业复制多套财务软件，而是通过可组合 Pack 生成适合具体 OPC 的财务运营系统：

```text
Finance Core
+ 业务预设
+ 渠道组件
+ 法律主体与纳税地区包
+ 数据连接器
+ 可选能力
```

当前游戏项目是第一个可运行的行业参考实现。平台化不应削弱它的专业深度，而应把已经验证的通用能力提炼为稳定契约，让电商、独立站和后续行业复用。

游戏参考实现已经使用统一 Pipeline 控制面：渠道结算文件先进入 Connector 批次质量门，再与人工提供的合同证据做严格一对一映射，最后调用确定性结算服务。运行只产生候选结果和可审计 attempt；合同映射与总额法/净额法判断是两道独立人工 gate。行业 Pack 因此负责“游戏财务方法”，Pipeline Core 负责幂等、lineage、复核、角色隔离和备份恢复。

## 配置维度

`examples/boxes/` 中的 Box 配置是产品装配入口。业务类型、渠道和税务地区必须保持正交：

- `business_models` 决定行业对象、指标与工作流。
- `channels` 决定交易、结算、费用和应收数据怎样进入系统。
- `entities` 是法律主体；每个主体独立配置本位币、准则、税务登记和地区包。
- `fiscal_year_end` 是主体税务和年结日历锚点，格式为 `MM-DD`。
- `reporting_currency` 只用于管理合并，不能替代主体法定账。
- `connectors` 只负责把外部数据转换为内部标准对象。
- `features` 为多主体合并等可选系统能力。
- 简化规格中的 `integrations` 是可编辑的产品预设；例如 `shopify_stripe` 展开为两个 Connector 和一个显式跨处理器证据链 Feature，不授予额外权限。

独立站属于 Commerce 业务下的 DTC 渠道组件，而不是另一套总账和 Agent 内核。

Commerce 同样分为通用和渠道专用层：`commerce.channel_close` 只由 DTC capability 启用，消费标准订单、结算、退货授权、仓库实收与进口费用对象，负责单主体勾稽、退款、退货处置、landed-cost 候选、履约成本和目的地证据；Shopify + Stripe Pipeline 在其上提供平台原生 transaction/source/payout 证据链；`woocommerce.order_refund_close` 为可 fork 自建独立站提供修改订单快照、状态、目的地税额和退款事件证据，但不推断支付结算、收入、税负、库存或过账；`paypal.transaction_close` 提供主体绑定的余额影响交易、费用、退款/冲正与余额转出证据，但不把处理器事件解释为收入或银行核销；`commerce.shipbob_fulfillment_close` 再提供主体绑定的 3PL 发货、履约账单和退货处置只读证据，明确不自动改变库存或过账；`marketplace.channel_close` 另行处理平台合同、应收、退货实收、进口费用和平台/内部库存。它们共享 Pipeline 运行账本和人工复核模型，但编译器和运行时都会阻止 Marketplace 借用 DTC Pipeline 绕过平台专有 gate。

## Pack 契约

每个 Pack 位于 `packs/<kind>/<name>/manifest.json`，至少声明：

- 稳定 ID、类型、版本和成熟度。
- 提供的 capabilities。
- 依赖与冲突。
- 必须由人完成的 review gates。
- 税务包额外声明地区代码、规则生效日和税务成熟度。

成熟度含义：

| 字段 | 含义 |
|---|---|
| `experimental` | 接口或实现仍可能变化 |
| `preview` | 可用于样板验证，但尚未承诺生产兼容性 |
| `stable` | 契约稳定并具备版本兼容承诺 |

税务成熟度独立管理：

| 字段 | 含义 |
|---|---|
| `design` | 只有数据、证据与人工复核接口 |
| `workpaper` | 可生成候选工作底稿，但不能声称可直接申报 |
| `filing_assist` | 已适配指定申报流程；仍需有权人批准和实际回执 |

任何税务包都必须保留官方来源、规则版本、生效日期、适用条件和人工复核记录。国家代码不足以覆盖州、省、市和销售目的地规则时，应增加更细地区包或登记配置，不能靠 Prompt 猜测。

## 运行边界

Box Resolver 只负责确认组合是否成立并输出启用能力，不直接执行财务动作。运行时仍必须遵循：

1. 金额、汇率、对账、凭证和税额使用确定性代码。
2. LLM 用于资料理解、分类建议、异常解释和计划生成。
3. 每项结论保留来源证据、规则版本、置信度和人工决定。
4. 付款、会计政策、关账和申报不能因模型置信度高而越权。
5. 多主体管理报表可以合并，法定账、银行、纳税和审批必须回到主体。

## 当前样板

- `global_game_studio.json`：Game Studio + 国内/海外游戏渠道 + 中国/新加坡主体 + 多主体管理视图。
- `cn_dtc_store.json`：Commerce + DTC 独立站 + 中国大陆主体。
- `cn_dtc_shopify_stripe_store.json`：Commerce + DTC + Shopify + Stripe + 中国大陆主体的完整订单到银行到账样板。
- `us_dtc_paypal_c_corp.json`：Commerce + DTC + PayPal + US Federal C corporation 的只读资金活动样板。
- `us_dtc_woocommerce_c_corp.json`：Commerce + DTC + WooCommerce + US Federal C corporation 的可 fork 自建独立站样板。

`examples/box_specs/shopify_stripe_cn.json`、`shopify_stripe_sg.json`、`shopify_stripe_us_c_corp.json`、`shopify_stripe_hk.json` 与 `shopify_stripe_uk_ltd.json` 展示同一业务/渠道/集成预设如何分别选择 CN、SG、US Federal、HK 与 UK Ltd 纳税地区 Pack。`opc-finance-box create` 根据已安装 Pack 解析国家代码；存在同国多个 Pack 时要求显式选择 `tax_pack`，不按国家代码猜地区制度。

新加坡、美国联邦 C corporation、香港法团与英国 Private Limited Company 税务包当前是 design 级，Resolver 会明确输出非申报级警告；中国大陆包当前是 workpaper 级，与现有代码的真实边界一致。US Federal Pack 不覆盖州/地方或 sales tax；香港 Pack 不判断来源地、离岸申索或两级税率资格；UK Pack 不计算 Corporation Tax/VAT，也不覆盖 PAYE、海关和个人税。国家代码不能替代这些登记与业务事实。

## 运行时接口

`src/box_runtime.py` 将通过校验的 Box 解析为线程安全、只读、可热加载的运行时快照；`src/box_api.py` 输出前端和 Agent 可以共同消费的上下文：

- 当前主体或管理合并范围。
- 已启用 Pack 与 capabilities。
- 税务规则核验日期和成熟度。
- 必须人工完成的 review gates。
- 非稳定 Pack 和非申报级税务能力的警告。

法定口径必须指定一个 `entity_id`。管理口径可以选择多个主体，但多币种时必须配置报告币种，并明确内部往来抵销要求。

## Commerce / DTC 确定性引擎

`src/commerce.py` 已实现首个 Commerce Pack 计算闭环：

```text
订单含税收款
→ 折扣与退款
→ 不含税净收入
→ 支付/平台结算
→ 税款代扣或代缴证据
→ 实际打款
→ 商品、履约、物流和渠道费用
→ 贡献利润
```

订单、结算和贡献利润按法律主体、期间、渠道和币种分别计算。目的地国家和已收税额只形成间接税判断证据，不自动推导登记义务、税率或应纳税额。可运行虚构数据位于 `data/commerce_demo.json`。

### Commerce 导入与运行

标准连接器位于 `src/commerce_import.py`，支持：

- CSV：UTF-8、GB18030、Big5。
- XLSX：自动寻找“订单明细”“渠道结算”“退货授权与退款”“退货入库”和“进口成本与关税”表头。
- 中文、英文与标准字段 ID 别名。
- 连接器层显式补充默认法律主体和渠道。
- 基于文件内容的稳定批次 ID、来源工作表和行号证据。
- 单行拒绝、重复业务键和批次就绪状态。

生成空白导入模板：

```bash
node scripts/build_commerce_template.mjs outputs/commerce-template.xlsx
```

模板包含使用说明、订单明细、渠道结算、退货授权与退款、退货入库、进口成本与关税、字段说明和公式驱动的检查页。空模板保持 `FAIL`，不会被误认为已准备好；可选页一旦填写就接受主体、唯一键、数量、费用和证据契约校验。

端到端运行虚构独立站样板：

```bash
python3 scripts/run_commerce_box.py \
  examples/commerce/dtc_orders.csv \
  examples/commerce/dtc_settlements.csv
```

命令会依次校验 Box capabilities、法律主体、输入批次、订单与结算方程和贡献利润；存在缺资料、未知主体或金额差异时返回非零状态。

### 通用银行流水导入

`file.bank_statement` 把 CSV/XLSX 银行导出转换为 `finance.bank_transactions`。导入契约强制法律主体、稳定流水号、ISO 日期、明确方向、币种、金额、账户掩码和文件批次证据；同一主体内的重复流水号失败关闭。完整账号不会进入标准数据集。`finance.bank_statement_close` 只生成按账户与币种分开的余额调节候选，必须经过来源映射和余额调节复核，不能自动认领、核销或过账。

### 通用会计试算平衡导入

`file.trial_balance` 把 CSV/XLSX 会计系统导出转换为 `finance.trial_balance_lines`。稳定业务键绑定法律主体、期间、币种和科目；每行保留文件、工作表、行号和批次 evidence。`finance.trial_balance_review` 只在同主体、同期间、同币种内做借贷控制总额与可选滚动一致性检查。平衡结果是月结复核证据，不会修改总账或期初余额，也不会自动过账、关账或推断科目映射完整。

`xero.trial_balance` 是同一 Pipeline 的真实网络只读替代来源。它先用固定 Organisation 端点核对 Box 主体绑定和功能本位币，再读取显式期末日期的 Trial Balance。tenant、organisation 与 access token 只能由环境配置，结果只保留绑定指纹。Xero 的 YTD 列不会被解释为本期发生额，因此这一版 Connector 只能做期末控制总额复核，不能替代 `finance.accounting_close_review`、First Close 或三方月结所需的期初/本期发生数据。详见 [Xero 只读会计 Connector](Xero只读会计Connector.md)。

`wise.balance_statement` 是标准银行文件的真实网络只读替代来源。它把 Box 法律主体绑定到一个精确的 Wise BUSINESS profile，再按本位币绑定一个 Balance；profile、balance 和 statement query echo 全部核对后才映射流水。Global API 版本、COMPACT/English statement、31 天增量与 366 天回溯窗口固定，personal token 国家资格和 403/SCA 均 fail closed。输出只保留绑定与 reference 指纹、脱敏账户引用和 allowlist 交易摘要，可进入银行对账、First Close 与三方月结，但不会替代 GL/TB 或执行支付。详见 [Wise Business 只读银行 Connector](Wise只读银行Connector.md)。

`airwallex.approved_expenses` 是企业卡费用证据源。它把 Box 法律主体绑定到精确 Airwallex legal entity/account，只读取 `APPROVED` Expense，并把 billing/transaction 金额无损转换为整数 minor units。原始 expense/card/attachment ID、人员邮箱、评论和附件 URL 不落输出；source sync status 只是来源证据，Connector 不调用写接口。checkpoint 后的轮询会有界回看 7 天；签名 webhook durable inbox 则完成 raw-body HMAC、防重放、event ID 幂等、主体绑定与异步当前对象重抓。worker 的 refetch context 绑定 receipt/event/body/expense/Box 指纹；乱序载荷不会成为账务真相，未知非批准状态只形成失效候选，只有 deleted+精确 GET 404 形成删除 tombstone 候选，其他 404 失败关闭，三次失败进入隔离。worker 还能选择性写出 `0600`、无金额/原始 ID 的 Shadow observation，并绑定完整内存 Pipeline result SHA-256；独立来源证据仍须另行保留。Connector Shadow 已能生成和封存 schema v2 `real_anonymized` 基线，并核对来源独立性、匿名化、状态变化及 webhook 触发的网络只读重抓；schema v1/demo 基线不能用于稳定晋级。官方 Expense API 仍为 Beta，外部真实样本也尚未由独立复核人签认，因此 Pack 保持 experimental。详见 [Airwallex 只读费用 Connector](Airwallex只读费用Connector.md)。

`paypal.transaction_activity` 是支付处理商资金活动证据源。它以环境中的 Client ID/Secret 进行内存 OAuth 换证，只调用固定生产或 Sandbox Transaction Search 端点，并把单次查询限制为最多 31 天、500 条/页和 10,000 条。标准数据集只保留哈希交易引用、T-code、金额、费用与候选分类；客户、地址、商品、invoice、自由文本和原始 ID 均被丢弃。`paypal.transaction_close` 按币种和 T-code 确定性汇总退款、冲正、费用与转出候选，但不会确认收入、核销银行到账、生成分录或触发外部动作。多主体使用必须逐商户绑定凭证或具备合规 Partner 授权。详见 [PayPal 只读交易 Connector](PayPal只读交易Connector.md)。

`woocommerce.order_refund_activity` 是自建独立站经营证据源。它把环境中的站点 origin 和只读 Consumer Key/Secret 绑定到一个 Box 法律主体，只访问固定 WooCommerce REST API v3 Orders / Refunds collection，本地控制分页并拒绝响应链接。标准数据集保留哈希订单/退款引用、状态、币种、聚合金额、目的地国家、商品行/数量合计和 evidence；客户、地址、网络身份、商品名/SKU/meta、自由文本和原始 ID 均被丢弃。`woocommerce.order_refund_close` 按币种确定性比较窗口退款事件与订单 lifetime refund，暴露 orphan、算术和状态复核候选，但不确认处理器/银行结算、收入、税负、库存或过账。详见 [WooCommerce 只读订单与退款 Connector](WooCommerce只读订单退款Connector.md)。

`amazon_seller.transaction_activity` 是 Marketplace 原生财务证据源。它以环境中的 LWA app/refresh credential 把一个 Box 主体绑定到精确 Seller、NA/EU/FE 区域和 Marketplace ID 白名单，只访问固定 SP-API Finances `v2024-06-19 listTransactions`，同参数本地续传 opaque `nextToken`，拒绝响应 URL。标准数据集只保留哈希 transaction/related identifier、状态、逐币种 total、item 数量摘要和 transaction/item 分层 component；客户、地址、店名、商品描述、SKU/ASIN/context、自由文本和原始 ID 均被丢弃。`amazon_seller.transaction_close` 分开 released/deferred 与层级 component，暴露退款、费用和 settlement completeness 候选，但不认定收入、Marketplace tax、银行结算、库存或过账。详见 [Amazon Seller 只读财务 Connector](AmazonSeller只读财务Connector.md)。

`amazon_seller.marketplace_evidence` 在同一主体/Seller/区域/Marketplace 绑定和一次内存 LWA 换证下，组合 Orders `v2026-01-01`、FBA Inventory v1 与 Finances v2024。Orders 只请求 FULFILLMENT；订单、item、SKU/ASIN/FNSKU 与 Finances `ORDER_ID` 全部以同一 binding 哈希后关联。一个质量门同时覆盖三个标准数据集，之后的确定性 Service 只暴露跨源差异候选。FBA 数量是当前抓取观察，缺失的可选分项显式以 0 承载并另列字段缺失候选，不能伪装成历史期末或已知零。详见 [Amazon Marketplace 订单库存完整性设计](AmazonMarketplace订单库存完整性设计.md)。

### General Ledger、Trial Balance 与报表候选

`file.general_ledger` 将外部会计系统的 CSV/XLSX 明细转换为 `finance.general_ledger_lines`，保留稳定凭证行身份、主体/期间/币种、科目、借贷与文件证据。`finance.accounting_close_review` 同时消费 GL 和 Trial Balance 两个独立批次，先逐凭证验证，再逐科目核对本期发生额，最后只使用显式 `financial_statement_account_mapping` 生成逐币种报表候选。内部 `ledger_store`、凭证过账与期初结转保持独立；这条 Pipeline 只读，不会把外部导出重新写回内部账本。

### 三方月结控制与 Founder 简报

`finance.first_close_discovery` 是配置阶段：它读取三个批次，验证 GL/Trial 逐科目发生额，并输出真实账户身份、来源 fingerprint 与默认阻塞的映射 starter。它只展示同币种候选科目全集，不从名称、编码、余额方向或金额猜测报表分类和银行→GL 关系。

`finance.month_close_control` 是复核阶段：在上述会计勾稽之外增加独立银行批次。银行账户只能通过 `bank_gl_mapping` 显式绑定到同币种 GL 现金科目；流水复核绑定银行来源 fingerprint，任何源数据变化都会使旧复核失效。账面余额由 Trial Balance 决定，证据化调节项按 bank/ledger 与 increase/decrease 分侧计算。最终输出是只读的逐账户控制与逐币种 Founder 简报，不执行交易匹配、现金分配、账簿修改、过账、关账或申报。

### 多主体月结组合与 Founder Portfolio

`finance.multi_entity_month_close_portfolio` 是一条 management-scope、无 Connector 的二段 Pipeline。它只消费已生成的单主体 `finance.month_close_control` 摘要、运行 ID 和证据，并对每个选定主体单独判定准备度。任一主体缺失、来源并非候选、月结未就绪或报告异常时，整体管理总额为空。

原币结果按主体和币种原样保留。跨币种组合只使用显式、同期间、已批准且有来源/复核人/证据的 P&L 和期末汇率；报告币种本身使用确定性 1:1。产物为抵销前创始人组合候选，不是法定合并报表，不会执行抵销、CTA/权益处理、修改主体账簿、过账、关账或申报。

正式运行时，每个摘要还必须绑定追加式 Pipeline 台账的 `source_attempt_id`。单主体月结记录只保存从可组合字段规范化得到的 SHA-256 指纹，不保存原始请求、完整结果或财务摘要。组合前验证整条 hash chain、主体、运行 ID、摘要指纹和所有必需 review gate；任一不符即拒绝生成可记录的组合运行。

## Pack Service Registry

`src/pack_services.py` 将 Pack capability 绑定到真实代码服务。Agent 只会看到当前 Box 同时满足“已选择 Pack + 已启用 capability”的工具：

- `read`：确定性读取或分析。
- `draft`：生成待确认政策、工作底稿或候选产物。
- `mutating`：修改正式状态，必须声明并取得 review gate。
- `external`：付款、申报等外部动作，必须声明并取得 review gate。

每个服务还必须声明法定单主体、管理多主体或无主体范围。服务调用结果带 Pack、capability、确定性标记、动作类型、主体范围、执行时间和审批记录，供审计日志继续保存。

当前已注册：

- Finance Core：标准银行 CSV/XLSX Connector、主体绑定 Wise Business 只读余额账户流水、PayPal 余额影响交易/费用/退款证据、主体/站点绑定 WooCommerce 修改订单与退款证据、主体绑定 Airwallex 已批准企业卡费用证据、文件 Trial Balance / General Ledger Connector、主体绑定 Xero Trial Balance 只读快照、首月结来源发现/fail-closed 配置起点、银行候选对账、双来源会计勾稽、显式银行账户→GL 现金科目三方月结控制与 Founder 简报 Pipeline、现金预测、采购到付款、月结准备度、证据链、Pack 驱动的白名单 CFO 指标计算，以及不持久化的 goal/plan/approval 草稿服务。
- 文件 Connector：受限内存 PDF 文本提取和可选本地 OCR，不接受任意服务器路径；OCR 与低置信度结果必须人工复核。
- Commerce / DTC / Marketplace：五表导入、订单—结算、退款、退货授权—多仓实收—处置候选、进口费用/关税 landed-cost 候选、履约成本、目的地证据、库存成本和平台库存核对。
- Game Studio：渠道合同公式结算、采购承诺/验收成本桥、显式项目人力成本与工时证据分摊、授权/IP license/云资源预付与逐期间证据释放候选、项目利润、LTV/ROI 预算门控、经营/KPI 分析、收入政策草稿和按已批准政策计算收入确认；不因付款自动资本化或推断税务结论。
- 多主体：逐主体月结准备度、原币保留、已批准汇率组合简报、管理合并和基于批准证据的内部往来调整；不修改主体法定账。
- 中国大陆税务：属地配置任务，以及 VAT、企业所得税预缴、印花税和个税扣缴候选工作底稿。
- 新加坡税务：登记事实、证据清单和 ECI/CIT/GST 候选日历；保持 design 成熟度，不计算税额、不生成可提交表单。
- 香港法团税务：BRN/UBI、BIR51、两级税率资格与预缴税证据，以及人工配置日历；保持 design 成熟度，不判断来源地或计算/申报。
- 英国 Private Limited Company 税务：主体与 Corporation Tax/VAT 登记证据、CT600 和 Companies House 候选日历，以及人工配置的付款/VAT 日历；保持 design 成熟度，不收集 UTR/公司号/VAT 号原值，不计算或提交。

游戏服务已经强制要求 `entity_id`。旧游戏演示数据未迁移主体前会返回阻塞，不会在全球管理视图中把来源主体抹掉。

## 税务规则来源

每个 jurisdiction Pack 必须提供经过结构化校验的 `rules.json`：

- 官方来源、主管机关和 HTTPS 地址。
- 规则核验日期和生效日期。
- 每条规则引用的来源 ID。
- 自动化级别：日历、证据或工作底稿。
- 强制人工复核标记。

规则文件也进入运行时指纹；来源或规则内容发生变化后，Box Runtime 会重新加载。地区包缺少来源、引用不存在或尝试取消人工复核时，整个 Pack 无法加载。

### 税务日历

`src/tax_calendar.py` 只处理 `automation_level=calendar` 的结构化规则：

- 新加坡 ECI 根据财年结束日加三个月生成候选日期。
- 新加坡年度申报根据财年和规则中的固定日期生成候选日期。
- GST 只有在主体明确登记后才根据 GST 期间结束日生成候选日期；`gst_review_required` 会阻断并要求确认。
- 中国大陆国家包不推断属地统一截止日，而是输出申报周期、属地来源和期间结束日的配置任务。

每个任务保留主体、规则 ID、Pack 版本、规则核验日、官方来源、review gate 和 `candidate_only` 标记。生成任务不代表申报、付款或外部提交已经完成。

## Box 创建与编译

面向用户的简化规格位于 `examples/box_specs/`。创建器会把行业、渠道和纳税国家别名转换为严格配置，并立即执行 Pack 依赖和主体校验：

```bash
python3 -m src.cli options
python3 -m src.cli create examples/box_specs/global_game.json --output outputs/my-box.json
python3 -m src.cli validate outputs/my-box.json
python3 -m src.cli compile outputs/my-box.json --output outputs/my-box-build
```

国家选项来自已安装 jurisdiction Pack，不写死在创建器。目前内置 AE、AU、CA、CN、DE、FR、GB/UK、HK、IE、JP、KR、NL、NZ、SG 与 US；不支持的国家会明确拒绝。新增地区按 [添加纳税地区 Pack](添加纳税地区包.md) 执行。

编译产物包括：

- `box.lock.json`：Pack 版本、主体、capabilities、服务、工作流和运行时指纹。
- `setup-checklist.json`：非稳定 Pack、税务成熟度、待确认登记和 review gate 有权人任务。
- `data-model.json`：当前行业/渠道启用的标准对象、必填字段和跨对象控制。
- `agent-contracts.json`：可编辑 Agent 输入、允许输出和禁止声明；它本身不是运行时授权。
- `agent-prompts.md`：按所选行业能力生成的角色、输入、输出和禁止动作模板，可直接 fork；review gate 仍由代码强制执行。
- `service-catalog.json` / `connector-catalog.json`：当前 Box 真正可调用的 provider。
- `pipeline-catalog.json`：Connector、质量门和确定性 Service 的端到端编排及主体/外部动作契约。
- `workflow-plan.json` / `job-plan.json`：工作流与默认禁用的调度建议；时区、主体、幂等和告警必须由部署者配置。
- `pipeline-schedule-template.json`：逐 Pipeline/主体生成的严格调度模板；必须补齐本地 request、IANA 时区、operator、告警责任人和独立审批后才能使用。
- `dashboard-layout.json`：按 capability 生成的面板与主体切换约束。
- `cfo-control-overlay.json`：由当前行业、渠道和 Connector Pack 组合出的业务模型 CFO 月度控制重点、数据源边界与创始人复盘问题；只含稳定类型 ID 和 Box fingerprint，可 fork 修改，不含财务值或完成结论。
- `cfo-metric-catalog.json`：由当前业务模型组合出的月度指标定义，逐项声明公式操作数、缺数策略、必需数据域、控制条件、决策用途、聚合边界、可信来源映射与确定性执行合同；目录本身不包含实际指标值。`core.evaluate_cfo_metrics` 另行按当前 runtime fingerprint、单一法律主体、自然月、本位币和可选业务维度执行白名单公式，缺输入或控制时失败关闭，不执行隐式换汇或任意表达式。受支持的 Pipeline/Service 在可信执行边界内附带最小化 `cfo_metric_operand_assembly`；人工提交的历史结果不会被重新标记为可信来源。
- `cfo-metric-evaluation-request.schema.json` / `cfo-metric-operand-assembly.schema.json`：分别约束显式指标计算请求和可信来源自动组装结果；运行账本只留来源指纹与控制状态，不留操作数或指标值。
- `jurisdiction-rules.json`：逐主体锁定的地区包版本、规则核验日、官方来源和结构化规则。
- `tax-applicability-questionnaire.json`：逐主体、按所选地区 Pack 生成的未回答适用性问卷；不采集原始税号，不自动形成税务结论。
- `tax-applicability-artifact.schema.json` / `tax-applicability-artifact-security-policy.json` / `tax-applicability-registry-receipt.schema.json`：锁定工作底稿、独立签认与轮换收据的数据契约，以及私有文件权限、符号链接、目录内容绑定和安全摘要边界。
- `tax-applicability-init/review/verify`：在编译目录外生成带 `facts_as_of` 的逐主体私有工作底稿和 fingerprint-bound 独立签认；签认验证只返回安全生命周期摘要，Pack/Box 变化或 Pack 策略到期后旧签认自动失效。
- `pilot-readiness-plan.json` / `pilot-readiness-artifact.schema.json`：按当前 Box 精确列出首家真实 OPC 的逐主体、行业资料域、网络 Connector、职责分离与受限 Shadow Close 准入契约。私有签认第 60 天提示复核、超过第 90 天关闭新的受限 Shadow Run；Doctor 与 Workbench 只消费安全投影。
- `pilot-data-handoff-plan.json` / `pilot-data-handoff-artifact.schema.json`：把真实资料交接绑定到仍有效的 Pilot 签认、当前 Box、主体、期间和行业资料域；只保存传输/隐私控制、计数、私有清单指纹和不透明引用，不复制或回显原始资料。
- `pilot-shadow-run-registration.schema.json`：把每个法律主体一个、与资料交接同期且全部当前 review gate 已批准的 `finance.month_close_control` 台账 attempt 绑定成首期登记；验证重新读取当前 hash chain 和复核状态，后续驳回或篡改即失效，但正常追加事件允许保留历史链头。
- `upgrade-policy.json`：定义主体、Pack、服务、Connector 和 review gate 的破坏性变化；与上一份 `box.lock.json` 比较后再接受升级。
- `release-gates.json`：单测、Pack audit、声明式 finance eval、doctor、upgrade check 与人工 shadow close/恢复演练；编译时永远不自动批准发布。
- `stable-promotion-policy.json`：绑定 Box fingerprint 的 Shadow 覆盖阈值、自动门、恢复演练和独立签认契约；只允许形成候选证据，不修改 Pack manifest。
- `pilot-shadow-period-archive` / `pilot-shadow-next-period-init/verify` / `pilot-shadow-period-runbook-*` / `pilot-shadow-series-artifact.schema.json`：归档命令先验证一个期间的准入、交接、登记、逐主体报告、可选 portfolio、观察和 Pipeline 台账，再事务性创建精确私有期间目录；下一期生成器重验归档前缀并生成当月增量工作区，不复用上月的 period-bound readiness/handoff；每月 Runbook 把操作者报告的进度绑定到当月命令合同与 hash chain，但不进入权威证据图。只读任务投影为每个稳定步骤绑定版本化工作产出、CFO 式操作检查和暂停条件类型，缺少方法合同时失败关闭；连续期 schema 把 2–24 个连续月份的完整源证据重新验证后绑定成无金额跨期收据。独立连续性复核只打开 stable 晋级证据准备门，不直接形成 candidate。
- `shadow-close-portfolio-*`：把全部逐主体当前签认与台账核验的多主体月结组合按 SHA-256 指纹绑定；输出只含计数、证据身份和指纹，不复制财务值，且强制组合复核人与逐主体复核人分离。
- `stable-promotion-evidence-templates.json` / `stable-promotion-evidence.schema.json`：逐个未达 stable 的已选 Pack 生成 fingerprint-bound 填写起点和机器结构契约；模板在替换占位符、补齐 Shadow 和真实门禁证据前故意不可签认。
- `OPC_STABLE_PROMOTION_ROOT`：将已由独立流程写入的追加式 promotion ledger 作为只读私有目录接回生产准备总表；运行时取得共享锁、验证目录/文件权限与完整 hash chain，只投影当前 Box 下各 Pack 当前版本的最新批准计数，不回显 assessment id、角色、证据或金额，也不修改 Pack manifest。
- `runtime-data-contract.json`：当前 v3 运行时数据布局，包含总账、Agent、资料箱、Pipeline、Connector sync 和 Release promotion 受管 store 及离线升级/恢复边界。
- `skill-catalog.json`：随产品交付的 Box 装配、月结审查和地区包扩展 Skills；不会自动写入个人全局 Skill 目录。
- `README.md`：当前 Box 的装配范围与上线边界。

编译只生成部署契约，不安装定时任务，也不启用付款或申报。受控执行器、租约、重试和审计边界见 [Pipeline 调度与可观测性](Pipeline调度与可观测性.md)；生产采集、告警矩阵和事故处置见 [Pipeline 部署监控与告警](Pipeline部署监控与告警.md)。

每次编译还生成 `deployment-environment-contract.json`，把当前 Connector secret 名称、逐主体 credential-binding JSON、动态密钥别名字段、旧根级变量迁移边界、只读配置、私有税务与 Pilot 签认、持久数据、认证、调度、网络与健康检查绑定到 Box fingerprint。动态别名本身只有部署者知道，产物只声明所选 Pack 的绑定字段、别名数量、主体范围与失败关闭规则，不臆造或泄露实际变量名。可 fork 的容器/systemd 起点及真实进程隔离验证见 [生产部署与 Smoke 验证](生产部署与Smoke验证.md)。这些构件证明单节点部署边界，不把产品宣称为已具备多租户、高可用或托管申报能力。

`handoff-bundle` 将简化规格、严格配置、编译目录、部署模板和首客 `ACTIVATION.md` 组装为确定性 ZIP；manifest 为每个成员记录字节数和 SHA-256。接收端的 `handoff-verify` 不解压文件，限制归档/成员规模并拒绝不安全路径、重复或非确定性成员；它先独立重算 manifest 和编译锁绑定，再用当前安装 Pack 重建全部成员逐字节比对，因此 manifest 自述或自洽篡改都不能通过。`handoff-unpack` 使用同一份已验证内存字节排他写入全新 `0700` 工作区，成员为 `0600`，完成收据最后生成；`handoff-unpack-verify` 可在无源 ZIP 时重验整个树，但收据明确不是数字签名或财务证据。真实激活另在 `0700` workspace schema v4 中运行。其 `commands.json` 是 Box/期间绑定的不可变操作合同，`runbook/activation-runbook.jsonl` 是 `0600`、跨进程锁保护的 append-only 操作者进度链；后续 `period-workspaces/YYYY-MM/runbook/period-runbook.jsonl` 以相同不变量将事件隔离到单月命令合同。两类 Runbook 事件都绑定 runtime fingerprint、完整命令合同 SHA-256 和单步骤 fingerprint，只投影 reported outcome，不进入生产准备度证据图，也不替代任何阶段 verifier。`OPC_ACTIVATION_WORKSPACE_ROOT` 只读挂载允许 Workbench 用共享读锁投影逐月恢复位置；请求不接收路径，空账本不创建文件，公共响应再移除全部 runtime/command/chain hash 和私有值。`activation-workspace-status` 从已验证工作区向十一阶段总表传递单文件工件和白名单目录工件；逐主体报告、连续期源证据与 promotion ledger 因此保留目录完整性语义，未列入白名单的 Runbook 或其他私有目录不会进入 Readiness 验证器。

`trial-init` 在标准单主体 Starter/Handoff 外再包一层本地试用合同，将不可变 `box/` 和版本化 `runtime-data/` 分离。`trial-onboarding` 必须先重验两层及 Handoff 根级正式 `setup-checklist.json`，再生成五段 Founder journey；它只压缩控制，不存储完成状态。`trial-run` 才把试用根通过服务端 `OPC_FINANCE_TRIAL_ROOT` 传入子进程，`GET /api/box/trial-onboarding` 不接收客户端路径，普通部署返回不可用。因此浏览器投影能证明“此响应来自当前已验证试用包装”，但仍不能证明 setup、税务适用性、Connector Shadow 或生产准备度已经完成。

Connector 上线预检在数据集级 Registry 之上增加 Pack 级压缩层。`connector-preflight` 与 `/api/box/connectors/readiness` 使用相同纯投影：可执行 Pipeline 先决定真实引用的数据集 Connector，再按 `pack_id` 聚合凭证引用、主体、数据集和财务任务流，并生成唯一的当前动作。聚合不访问外部账号，不读取凭证值，不读写私有请求；Shopify / Stripe / Wise / Xero / PayPal / WooCommerce / ShipBob / Amazon Seller 凭证引用就绪后只开放私有 `connector-access-request-init`，不会直接开放 Shadow。Wise / Xero 的 token 与主体绑定表必须同时就绪；PayPal / WooCommerce / ShipBob / Amazon Seller 的主体切片及其动态密钥别名必须逐主体完整。未运行的权限探测、Shadow、勾稽和调度始终保持未完成或锁定，不能由环境变量存在反推。

`connector_access_probe` 是与浏览器控制面隔离的 CLI-only 外部读取边界。账户/店铺只进入 `0600` 私有请求或服务端环境绑定；Wise / Xero / PayPal / WooCommerce / ShipBob / Amazon Seller 的 provider ID/site/channel/seller/Marketplace allowlist 只存在于环境绑定表，request 只声明 `entity_environment_binding`。凭证必须由操作者逐次追加 `--allow-network`并指定新的私有回执路径。Shopify 使用固定版本和 GraphQL 权限查询；Stripe 拒绝 `sk_` 并只接受 `rk_`，使用固定 Account、Balance Transactions 与 Payouts；Wise 只验证 Business Profile 与本位币 Balance 元数据；Xero 验证 Organisation 和 Trial Balance 读取范围但不保留报表值；PayPal 验证 OAuth scope、App、merchant account 与 Balance 读取，余额值只在内存出现；WooCommerce 对绑定站点执行两个 id-only GET，并明确不宣称 provider 已证明 key 无写权限；ShipBob 验证绑定 channel 和精确四项 read scope；Amazon Seller 用 Sellers、Orders、FBA Inventory、Finances 四个有界 GET 验证真实读取，但明确不宣称 provider 已反查 seller ID，Finances 财务值也不保留。schema v2 回执把绑定表中的当前法律主体切片与其动态密钥别名值作为有序凭据组统一指纹，避免无关主体配置变化导致误失效。回执只包含运行时/请求/绑定指纹、布尔控制、API 版本和凭证模式，不包含服务方标识、scope 列表、响应体、来源记录或财务值。旧 schema v1 单凭据回执保持可读。回执为自校验 SHA-256 证据而非数字签名，默认 30 天有效；八类 live Shadow 都必须重新验证它与当前 Box、私有请求、Pack、主体及当前凭据组的绑定。Stripe Connect 账户标识只从已验证的私有 access request 注入内存请求头，不进入 Shadow 回执。探测通过仍不是勾稽或调度批准。

`starter-init` 位于 Starter Catalog 与标准 Handoff 之间：它按安装 Pack 选择唯一的行业/国家起点，验证集成预设属于该 Profile 白名单，允许有限的 Box/主体命名覆盖，然后在内存中生成同一份确定性 Handoff 并调用相同的验证与私有落地逻辑。因此一键初始化没有绕过 `create/resolve/compile`、Tax Pack 成熟度或 Handoff 完整性门禁，也不会产生另一种较弱的工作区格式。

`starter-compose` 复用同一条路径，但先把 2–20 个同 Profile Starter 的单一主体投影组合成一个规格。每个选择必须解析到唯一的已安装国家 Starter，主体 ID 必须唯一；组合器保留逐主体 Tax Pack、本位币、会计准则、财年与空登记，集成只取该 Profile 共同允许的预设。`--entity-integration entity_id=preset` 将预设并入产品能力，同时生成完整、规范排序的 `connector_bindings`。配置验证要求每个已选 Connector Pack 恰好出现一次、绑定非空且仅引用当前主体；只有单凭证 Shopify / Stripe 必须各自恰好绑定一个主体。PayPal / WooCommerce / ShipBob / Amazon Seller 可绑定多个主体，但 Provider 在多主体 Box 中强制使用逐主体环境绑定和动态密钥别名，拒绝旧根级凭证回退。旧配置没有该字段时按全主体规范化，但单凭证 Connector 在多主体 Box 中必须显式迁移，避免一套凭证被误当成多主体隔离。多币种时报告币种必须由操作者显式提供，随后由 Scaffold 自动加入 `feature.multi_entity`。报告币种只是管理展示合同，不携带汇率或跨币种加总授权；编译后的主体账簿、证据、Pipeline 和税务门禁继续按 `entity_id` 分离。

浏览器 Builder 是上述组合合同的无凭证投影，而不是另一套较弱的配置逻辑。`GET /api/box-builder/options` 从当前安装返回 `connector_binding_policy`；前端据此为主主体和每个附加主体提供 Profile 白名单内的 integration preset，展开 preset 的 Connector Pack，并把文件导入绑定到全部主体、外部来源绑定到实际选择主体。草稿出现单凭证 Connector 多主体范围时，前端不发送 preview；服务端仍独立执行完整覆盖、主体存在性与单凭证范围校验。校验后的界面同时展示严格绑定和等价的单主体 `starter-init` 或多主体 `starter-compose --entity-integration` 文本，但永不执行命令。浏览器、CLI、Scaffold、Runtime 和后续证据门因此共享一个 `connector_bindings` 权威字段。

同一 options 响应的 `handoff_download_policy` 定义浏览器 Handoff 的传输完整性合同。Bundle 响应把整包 SHA-256、runtime fingerprint、manifest schema 和 manifest 文件数放入固定响应头；前端在创建 Blob URL 前验证固定格式与响应长度，并用 WebCrypto 对实际 Blob 重新计算 SHA-256。任何缺失或不一致都会销毁本次候选收据并阻止保存；通过后生成可另存的无密钥、无路径 `.browser-receipt.json`，以及带占位绝对路径的私有权限、`handoff-verify`、`handoff-receipt-verify`、`handoff-unpack` 与 `handoff-unpack-verify` 命令。联合 verifier 不信任收据自述：它先复核 ZIP manifest 与当前安装 Pack 的逐字节可复现性，再绑定收据中的文件名、长度、SHA、运行指纹和清单元数据。该收据不执行归档成员、不改变活动运行时、不能证明浏览器执行或发行方身份，也不是数字签名；安全展开收据仍是独立门禁。

运行时把绑定投影到 Connector catalog 的 `entity_ids`，并在调用 handler 前校验 `default_entity_id`；Channel Pack 自带、非顶层 Connector Pack 的 Connector 仍按 Channel 的全部主体范围工作。Pipeline catalog 进一步给出 `available_connectors_by_entity` 与 `eligible_entity_ids`，只为满足来源绑定的主体生成法定模板。Pilot、Production Readiness、Connector Shadow baseline、首客 Activation 与网络 Connector stable promotion 复用同一主体范围；改变或缩小已锁定 Connector 的主体绑定属于 blocking upgrade，旧 lock 中缺少 `entity_ids` 则按旧 Box 全部主体归一化。

源码分发不复用配置 Handoff。`source-kit-bundle` 以固定白名单组装顶层项目文件、完整 `src/tests`、Pack、三类样板、文档、前端、部署、脚本、Skills 与 CI；只含两个明确 demo data 文件，排除 Git 历史、运行状态、私有证据、依赖和构建输出。`source-kit-manifest.json` 逐文件绑定内容 fingerprint，`source-kit-verify` 使用当前安装源码/资源重建全部逻辑成员。`source-kit-unpack` 对同一次验证的内存字节进行排他写入，生成权限受限且带非签名收据的全新 Fork 工作区；`source-kit-unpack-verify` 在源 ZIP 不存在时仍可复核原始树。因此用户拿到的是可审计、可运行、可建立自己 Git 历史的源码树，而不是把 wheel 或单个 Box Bundle 伪装成 repo。

## 统一客户端接口

`src/box_service_api.py` 提供两个与具体 HTTP 框架无关的入口：

- `build_box_bootstrap`：Box Context + 当前配置真正可调用的服务目录。
- `dispatch_box_service_request`：校验不可信 JSON 后交给 Pack Service Registry。
- `dispatch_box_pipeline_request`：按已注册 pipeline 串联 Connector 与 Service，并在质量失败时停止后续财务计算。

服务请求的编辑器/网关契约位于 `box/box-service-request.schema.json`。`entity_id` 与 `entity_ids` 互斥；schema 只负责 JSON 形状，服务是否可用、主体是否属于当前 Box、地区包是否匹配以及审批是否满足 review gate，仍由运行时强制校验。

统一命令行 `opc-finance-box`（或 `python3 -m src.cli`）支持 `options`、`box-starters`、`starter-init`、`starter-compose`、`create`、`validate`、`compile`、`context`、`services`、`dispatch`、`cfo-metrics-evaluate` 和 `tax-calendar`。服务调用仍经过 Pack、capability、法律主体和 review gate 四层控制。

游戏工作台服务器已挂载：

- `GET /api/box`：管理范围 bootstrap。
- `GET /api/box?scope=statutory&entity_id=<id>`：单主体 bootstrap。
- `POST /api/box/services/dispatch`：不可信 JSON 服务请求边界。

Box Agent 的 goal/plan/approval Pack 服务输出的是可复算草稿，明确 `state_changed=false`；游戏工作台持久化 Agent 状态仍通过带事件日志的 Agent Runtime API 写入。两者不能通过相似命名混淆状态。

## 验证

```bash
python3 scripts/validate_box_config.py examples/boxes/global_game_studio.json
python3 scripts/validate_box_config.py examples/boxes/cn_dtc_store.json
python3 -m src.cli services examples/boxes/global_game_studio.json
python3 -m unittest discover -s tests -q
```

Box 配置同时提供 `box/box-config.schema.json`，便于编辑器提示；程序运行时以 `src/box_config.py` 的跨 Pack 和跨主体校验为准。
