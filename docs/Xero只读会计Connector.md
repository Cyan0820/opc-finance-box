# Xero 只读会计 Connector

`connector.xero` 是 OPC Finance Box 的第一条真实会计系统 API 边界。当前只实现 Xero Trial Balance 的单主体、单期末只读快照；它不写 Xero，不刷新或保存 OAuth token，不读取 Journals，不执行过账、关账或申报。

## 为什么先做 Trial Balance

Xero Accounting API 提供固定的 [Trial Balance report](https://developer.xero.com/documentation/api/accounting/reports) 和 [Organisation](https://developer.xero.com/documentation/api/accounting/organisation) 端点。Connector 先读取 Organisation，确认返回的 `OrganisationID` 与 Box 主体绑定完全一致，并要求 `BaseCurrency` 等于该主体的 `functional_currency`；随后才读取显式 `as_at` 日期的 Trial Balance。

当前最小权限为：

- `accounting.settings.read`：读取 Organisation 以做主体和本位币核验。
- `accounting.reports.trialbalance.read`：读取 Trial Balance。Xero 当前将它列为细粒度报告 scope；不要为方便而默认申请更宽权限。参见 [OAuth scopes](https://developer.xero.com/documentation/guides/oauth2/scopes)。

Journals 不在当前 Pack 内。Xero 当前对 Journals 访问另有 scope、套餐和应用审批条件，见 [Journals API](https://developer.xero.com/documentation/api/accounting/journals)。在这些条件未被真实租户确认、测试和签认前，完整月结的 General Ledger 仍使用只读文件 Connector。

## 配置

选择示例 Box：

```bash
python -m src.cli connectors examples/boxes/global_game_studio_xero.json
```

真实 fetch 只从进程环境读取 access token 和主体绑定：

```bash
export OPC_XERO_ACCESS_TOKEN='REPLACE_WITH_SHORT_LIVED_TOKEN'
export OPC_XERO_ENTITY_BINDINGS_JSON='{
  "cn_studio": {
    "tenant_id": "11111111-1111-4111-8111-111111111111",
    "organisation_id": "22222222-2222-4222-8222-222222222222"
  },
  "sg_publisher": {
    "tenant_id": "33333333-3333-4333-8333-333333333333",
    "organisation_id": "44444444-4444-4444-8444-444444444444"
  }
}'
```

不要把这些值写进 Box JSON、Pipeline request、fixture、lock、调度文件或 Git。正式部署应由 secret manager 或进程主管注入。OAuth 授权、refresh token 的加密保存与 access token 轮换是外部运维职责；实现授权流程时遵循 Xero 的 [OAuth 2.0 authorization flow](https://developer.xero.com/documentation/guides/oauth2/auth-flow)。

请求只允许业务参数：

```json
{
  "pipeline_id": "finance.trial_balance_review",
  "payload": {
    "entity_id": "cn_studio",
    "period": "2026-07",
    "connector_id": "xero.trial_balance",
    "connector_request": {
      "mode": "fetch",
      "as_at": "2026-07-31",
      "payments_only": false
    }
  }
}
```

`period` 必须与 `as_at` 的月份一致。Connector 只使用以下两个固定 GET 端点，request 不能覆盖 URL、tenant header、token 或 organisation binding：

- `https://api.xero.com/api.xro/2.0/Organisation`
- `https://api.xero.com/api.xro/2.0/Reports/TrialBalance`

## 数据口径

Xero 报表的 `Debit` / `Credit` 映射为期末借贷余额。`YTD Debit` / `YTD Credit` 原样保存在 `xero_ytd_debit` / `xero_ytd_credit`，不会冒充本期发生额。因为这个响应不提供可验证的期初与本期发生链路，标准字段 `opening_*` 和 `period_*` 明确为零，滚动复核标记为未执行。

因此：

- `finance.trial_balance_review` 可以使用 `xero.trial_balance` 检查同主体、同期间、本位币的期末借贷控制总额。
- `finance.accounting_close_review`、`finance.first_close_discovery` 和 `finance.month_close_control` 暂不接受它替代带期初和本期发生额的 Trial Balance 导出。
- 平衡只说明当前快照通过控制总额，不证明账户映射、完整性、已过账或已关账。

输出只保留 tenant / organisation binding 的不可逆短指纹；不返回 access token、Authorization header、tenant ID 或 organisation ID。账户 UUID 也不再原样保留，每行 evidence 只保存完整 SHA-256，用于同一来源对象的确定性追溯而不暴露原始标识符。

## 真实来源 Shadow

`connector.xero` 已提供 schema v2 `real_anonymized` Connector Shadow profile。它只验收一份法律主体、一个月份的月末 Trial Balance 网络快照，并要求 `payments_only=false`。控制包括：来源行数、scope 数、借贷平衡/不平衡数、未执行 roll-forward 的 scope 数、主体与本位币绑定、月末 `as_at`、point-in-time/YTD 口径，以及所有写动作关闭。

先为相同 Box 与主体生成当前权限回执。权限探针读取 Organisation 与一个探测日 Trial Balance，但只保留权限、主体与本位币检查结果，不持久化报表行、账户或金额；回执 schema v2 绑定 token 与 `OPC_XERO_ENTITY_BINDINGS_JSON` 的当前主体切片。随后由独立准备人从 Xero 后台或受控导出填写并封存 baseline，再由 Box 生成主体和月份绑定的完整私有请求。所有私有文件使用 `0600`、拒绝覆盖：

```bash
opc-finance-box connector-access-request-init \
  examples/boxes/global_game_studio_xero.json \
  --pack connector.xero --entity cn_studio \
  --output private-xero-access-request.json

opc-finance-box connector-access-probe \
  examples/boxes/global_game_studio_xero.json \
  private-xero-access-request.json --allow-network \
  --output private-xero-access-receipt.json
```

期间请求不包含 token、tenant / organisation binding、账户标识或金额，也不要求操作者手写字段：

```bash
opc-finance-box xero-shadow-request-init \
  examples/boxes/global_game_studio_xero.json \
  --entity cn_studio --period 2026-07 \
  --output private-xero-live-request.json

opc-finance-box xero-shadow-request-verify \
  examples/boxes/global_game_studio_xero.json \
  private-xero-live-request.json

opc-finance-box xero-shadow-observe \
  examples/boxes/global_game_studio_xero.json \
  private-xero-live-request.json \
  --access-request private-xero-access-request.json \
  --access-receipt private-xero-access-receipt.json \
  --output private-xero-shadow-observation.json
```

init 会按自然月计算精确月末 `as_at` 并固定 `payments_only=false`；verify 在联网前重新检查文件权限、主体 Connector binding、月份、固定字段和无内联凭证。observe 还会先复验同主体、当前凭据组且未过期的 access receipt。observation 不保存账户名称/代码、账户 UUID 或其 hash、tenant / organisation binding、任何期末/YTD 金额，也不保存完整 Pipeline result；只保存 assessor 所需的主体、期间、币种、计数和布尔控制，并绑定完整内存结果的 SHA-256。独立来源导出仍必须在私有证据库中另行留存，不能用 observation 反向制作 baseline。通过 Shadow 只形成 stable 候选证据，不证明账户映射、完整性、过账或关账。

## 离线合同测试

```bash
python packs/connectors/xero/provider_contract_test.py \
  examples/boxes/global_game_studio_xero.json
```

fixture 使用虚构 UUID 和金额，不访问网络。Provider contract test 会验证批次质量、主体范围、原始 UUID 脱敏、证据完整性和重复运行幂等；真实上线仍必须补充租户授权、token 轮换、限流、字段变体、真实 Connector Shadow 和独立复核证据。

Xero 的公开 OpenAPI 规格可用于核对响应字段，但运行时仍以固定、受测的最小映射为准：[xeroapi/xero-openapi](https://github.com/xeroapi/xero-openapi)。
