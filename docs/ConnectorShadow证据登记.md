# Connector Shadow 证据登记

Connector Shadow Registry 把已经完成真实匿名来源核对和独立复核的 Connector Shadow 工件装入一个私有轮换目录，再以只读方式回答“当前 Box 选中的网络 Connector Pack 是否已有当前证据”。它不发起网络请求，也不把凭证配置解释为 Shadow 已完成。

## 建立轮换目录

先按 [Connector 来源 Shadow 验收](Connector来源Shadow验收.md) 完成 schema v2 baseline、Pipeline observation、assessment 和独立 `passed` 复核。然后把当前轮换窗口内的 reviewed assessment 放入专用目录：

```bash
install -d -m 0700 /absolute/private/connector-shadow-reviews
install -m 0600 private/reviewed-shopify-stripe-wise.json \
  /absolute/private/connector-shadow-reviews/shopify-stripe-wise-2026-08.json

opc-finance-box connector-shadow-status path/to/box.json \
  --review-dir /absolute/private/connector-shadow-reviews \
  --as-of 2026-08-14
```

文件名不参与信任判断，也不会出现在返回结果中。目录必须是绝对真实目录、权限为 `0700`；每个条目必须是非符号链接的 `0600` 常规 JSON 文件，单文件不超过 2 MiB，目录最多 500 个条目。

## 验证规则

状态检查会重新绑定当前 Box fingerprint，并要求：

- schema v2、`real_anonymized`、独立来源声明完整；
- assessment 本身通过，当前 review 的 decision 为 `passed`；
- review 不晚于 `as_of`，且默认不早于 30 天窗口；
- artifact 的主体仍属于当前 Box；
- 同一 `pipeline_id + entity_id + sample_period` 只有一份工件；
- 当前可执行财务任务流实际使用的每个网络 Connector Pack 至少被一份当前工件覆盖；
- 目录没有子目录、符号链接、非 JSON、权限过宽或其他非预期条目。

一个四来源 DTC 工件可以同时覆盖 Shopify、Stripe 和 Wise Pack；Xero、Wise 单来源银行流水、PayPal Transaction Search 及 Airwallex 费用仍使用各自的严格 Shadow profile。旧演示 baseline、`accepted-differences`、过期工件、重复 scope 或只配置凭证都不会放行。

## 服务器挂载

```bash
export OPC_CONNECTOR_SHADOW_REVIEW_DIR=/absolute/private/connector-shadow-reviews
```

Workbench 和生产准备度分别读取：

```text
GET /api/box/connector-shadow?as_of=2026-08-14
GET /api/box/production-readiness?as_of=2026-08-14
```

HTTP 不接受目录路径参数。响应只包含状态、计数、Pack 覆盖、主体数和期间数，不返回目录、文件名、assessment/result hash、人员、理由、证据引用、来源计数、控制明细、凭证或金额。

## 成熟度边界

`ready_for_connector_shadow_evidence=true` 只证明所选网络 Connector Pack 至少有一份当前真实来源证据，并且轮换目录边界有效。稳定版晋级仍会按样本逐主体、逐期间重新验证精确工件集合、连续 Shadow Close、职责分离、发布门和演练；本状态不会自动提升 Pack 成熟度，也不授权调度、过账、付款、关账或外部申报。
