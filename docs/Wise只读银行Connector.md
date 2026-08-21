# Wise Business 只读银行 Connector

`connector.wise` 是 OPC Finance Box 的首条真实银行 API 边界。首版只读取一个法律主体绑定的 Wise Business profile、一个本位币 Balance 和显式 UTC 区间的余额对账单；它不发起转账、不创建收款人、不改账户、不分配现金、不过账，也不替代人工银行余额复核。

## 固定 API 合同

Pack 固定使用 Wise Global API `2026Q3` 的三个 GET 端点：

- `GET /2026Q3/profiles/{profileId}`：确认是绑定的 `BUSINESS` profile 和精确 business name。
- `GET /2026Q3/profiles/{profileId}/balances/{balanceId}`：确认余额账户 ID、币种和 `STANDARD` / `SAVINGS` 类型。
- `GET /2026Q3/profiles/{profileId}/balance-statements/{balanceId}/statement.json`：读取 `COMPACT`、英文 JSON 对账单。

Wise 使用季度 CalVer 维护 Global API；Box 将版本钉死，升级时必须重新跑 Provider contract、Shadow Close 和字段审计，不能静默追随最新版本。参见 [Wise Global API versioning](https://docs.wise.com/guides/developer/global-versioning) 与 [Balance statement API](https://docs.wise.com/api-reference/balance-statement/balancestatementget)。

官方接口允许最长 469 天的 statement 区间；Box 进一步收紧为：增量最多 31 天、回溯最多 366 天，并使用 `[interval_start, interval_end)` 的 UTC 半开区间。checkpoint 只有在完整批次通过质量门并由复核人提交后才推进。

## 认证与国家边界

认证只允许 `Authorization: Bearer ...`，token 从 `OPC_WISE_ACCESS_TOKEN` 读取，不允许出现在 Box、请求、fixture、调度或同步 ledger 中。

Wise 的 [personal API token 指南](https://docs.wise.com/guides/developer/auth-and-security/personal-api-token) 明确限制：使用 personal token 读取 balance statement 的账户 profile 目前只支持美国、加拿大、澳大利亚、新西兰、新加坡和马来西亚。Pack 因此要求每个主体显式选择：

- `personal_token_eligible`：Box 主体 jurisdiction 必须在上述集合中；或
- `wise_partner_approved`：运营方已经具备 Wise 合作方/用户 token 合同，并保存外部批准证据。

这只是访问资格控制，不代表某个国家的税务、外汇或银行合规结论。

Wise 可能对 statement 返回 403 并要求强客户认证。首版不模拟交互式 OTT/SCA，也不保存挑战内容；它会脱敏失败，由操作人在 Wise 网站或 App 完成要求后重跑。参见 [Wise SCA over API](https://docs.wise.com/guides/developer/auth-and-security/sca-over-api)。429 和 5xx 使用有界重试，并尊重最长 30 秒的 `Retry-After`。

## 主体与账户绑定

真实 fetch 只从环境读取严格绑定：

```bash
export OPC_WISE_ACCESS_TOKEN='INJECT_WITH_SECRET_MANAGER'
export OPC_WISE_ENTITY_BINDINGS_JSON='{
  "sg_store": {
    "profile_id": 123456,
    "business_name": "OPC Wise Demo Pte Ltd",
    "access_contract": "personal_token_eligible",
    "balances": {
      "SGD": {
        "balance_id": 987654,
        "account_reference_masked": "Wise SGD ••7654"
      }
    }
  }
}'
```

请求不能覆盖 profile、balance、business name、access contract、token 或 URL：

```json
{
  "pipeline_id": "finance.bank_statement_close",
  "payload": {
    "entity_id": "sg_store",
    "period": "2026-07",
    "connector_id": "wise.balance_statement",
    "connector_request": {
      "mode": "fetch",
      "currency": "SGD",
      "interval_start": "2026-07-01T00:00:00Z",
      "interval_end": "2026-08-01T00:00:00Z"
    }
  }
}
```

`currency` 必须等于 Box 主体 `functional_currency`。返回 profile、balance、statement query echo 和 `accountHolder.type` 都要与绑定一致，否则 fail closed。运营方只提供脱敏账户引用；连续九位以上数字会被拒绝。

## 数据与隐私口径

每条 Wise `CREDIT` / `DEBIT` 映射为标准 `finance.bank_transactions` 的 inflow / outflow，保留金额、币种、running balance、费用金额和原始发生日期。`referenceNumber` 只参与不可逆业务键和指纹，不进入输出；profile ID、balance ID、完整账户信息、地址、bank details 和整个 `details` 对象也不会输出。Provider 还会按对账单顺序重算“期初 ± 每笔金额”，逐笔勾稽 running balance 并最终勾稽期末；任一差异都整批 fail closed。

交易摘要和对手方只从小型 allowlist 中提取，并遮蔽连续九位以上的嵌入式账号。由此产生的记录仍是“待人工确认”的对账候选，不是收入确认、费用分类、现金分配或会计凭证。

`wise.balance_statement` 可替代以下流程的银行文件来源：

- `finance.bank_statement_close`
- `finance.first_close_discovery`
- `finance.month_close_control`
- `dtc.shopify_stripe_daily_close`（把 Wise 主单位金额严格转换为已配置的整数最小货币单位，再参与 Stripe Payout 候选核对）

后两者的 General Ledger 和带期初/本期发生额的 Trial Balance 仍必须来自受控文件 Connector。

组合样板 `sg_dtc_shopify_stripe_wise_store.json` 串联四个只读来源：Shopify 交易 → 显式 processor link → Stripe Balance Transaction/Payout → Wise 银行入账。金额/币种/日期或 Payout 引用不一致时只产生异常；即使形成 `high_confidence_candidate`，仍需六道 Shopify、processor、Stripe、Wise 人工 gate，不会自动核销或过账。

## 验证与上线

```bash
python packs/connectors/wise/provider_contract_test.py \
  examples/boxes/sg_dtc_wise_store.json

# wise-request-base.json: {"currency":"SGD"}
opc-finance-box connector-sync-plan examples/boxes/sg_dtc_wise_store.json \
  wise.balance_statement \
  --entity sg_store \
  --stream sgd-operating-balance \
  --mode incremental \
  --window-start 2026-07-01T00:00:00Z \
  --window-end 2026-08-01T00:00:00Z \
  --request-base wise-request-base.json
```

上线前必须完成 `wise_entity_profile_binding_review`、`wise_balance_account_mapping_review` 和 `wise_statement_access_review`，并用脱敏真实数据做 Shadow Close。fixture 只证明离线合同、幂等、主体范围和隐私输出，不证明真实 token 权限、SCA 状态、profile 国家资格或生产字段完整性。

在生成月度 Shadow 请求前，必须先为相同 Box 与主体生成并通过当前权限回执。访问探针只读取 Business Profile 与本位币 Balance 元数据，不读取 balance statement、交易或金额；回执 schema v2 绑定 token 与 `OPC_WISE_ENTITY_BINDINGS_JSON` 的当前主体切片，二者任一变化都要求重新探测：

```bash
opc-finance-box connector-access-request-init BOX.json \
  --pack connector.wise --entity ENTITY_ID \
  --output private-wise-access-request.json

opc-finance-box connector-access-probe BOX.json \
  private-wise-access-request.json --allow-network \
  --output private-wise-access-receipt.json
```

真实月度 statement 不需要手写 Pipeline JSON。先由 Box 根据主体本位币和自然月生成一个完整、无需人工编辑的私有请求：

```bash
opc-finance-box wise-shadow-request-init BOX.json \
  --entity ENTITY_ID --period 2026-07 \
  --output private-wise-live-request.json

opc-finance-box wise-shadow-request-verify BOX.json \
  private-wise-live-request.json

opc-finance-box wise-shadow-observe BOX.json \
  private-wise-live-request.json \
  --access-request private-wise-access-request.json \
  --access-receipt private-wise-access-receipt.json \
  --output private-wise-shadow-observation.json
```

init 自动绑定 `connector.wise` 允许主体、主体 `functional_currency` 和精确 UTC 自然月半开区间，文件使用 `0600` 且拒绝覆盖。请求不包含 token、profile ID、balance ID、business name、access contract、账户 reference 或金额；这些绑定只能从独立的环境/secret manager 注入。verify 不读取凭证、不访问网络，并拒绝 fixture、错主体、错币种、错月份、额外字段、内联账户绑定和权限过宽文件。

observe 会先复验同一主体、当前凭据组且未过期的 access receipt，再次执行同一期间请求合同，然后产生 `0600` 的 assessor observation：它绑定完整内存 Pipeline result SHA-256，但不落盘金额、账户引用、对手或原始 ID。独立来源导出与范围证据仍须由 baseline 准备人私下保留；observation 不自动把 Pack 提升为 stable。
