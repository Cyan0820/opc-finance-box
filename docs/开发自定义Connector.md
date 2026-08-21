# 开发自定义 Connector

## 你只需要实现映射

OPC Finance Box 的 Connector 不负责决定会计政策或税务处理。它只负责把外部系统的对象转换为标准数据集，并保留来源证据。

Commerce API 示例：

```bash
python3 -m src.cli connectors examples/boxes/cn_dtc_api_store.json
python3 -m src.cli import \
  examples/boxes/cn_dtc_api_store.json \
  example.commerce_api_payload \
  examples/connectors/commerce_api_payload.json
```

示例不会访问任何网络，也不包含真实 Shopify 或支付宝凭据。`src/default_connectors.py` 中的 `_commerce_api_example_handler` 展示了需要改造的映射位置；实际项目可复制成独立模块。Stripe 已有独立的一方实现，见 [Stripe Connector Pack](Stripe连接器.md)。

现在也可以直接生成一个可发现的本地 Connector Pack：

```bash
opc-finance-box connector-init \
  --output-root packs/connectors \
  --slug my_store \
  --display-name "My Store API" \
  --secret-env OPC_MY_STORE_TOKEN \
  --base-url https://api.example.com/v1/finance
```

生成物包括 manifest、`provider.py`、离线 fixture、provider contract 和独立 `provider_contract_test.py`。把 `connector.my_store` 加入 Box 的 `connectors` 后，CLI、pipeline、compiler 与 Pack audit 会从 manifest 的显式 provider 声明加载它。Provider 文件必须位于 Pack 目录内，且只能注册本 Pack 已声明的 capability。

```bash
python packs/connectors/my_store/provider_contract_test.py examples/boxes/my-store-box.json
```

## ConnectorDefinition

每个 Connector 声明：

- `connector_id`：稳定 ID。
- `pack_id` 与 `capability`：只有 Box 选中对应 Pack 和能力时才可调用。
- `dataset_types`：允许输出的数据集，例如 `commerce.orders`。
- `business_keys`：每类数据的业务唯一键，例如 `order_id`。
- `handler`：接收请求与主体范围，返回标准批次。

批次必须包含稳定 `batch_id`、`datasets` 和每条记录的：

```json
{
  "entity_id": "cn_dtc_company",
  "evidence": {
    "source_file": "api:your-source",
    "source_row": 1,
    "batch_id": "stable-source-cursor-or-hash",
    "source_object_id": "external-object-id"
  }
}
```

SDK 统一拒绝未知主体、缺证据、证据批次不一致、缺业务键和重复业务键。Connector 不应把缺失金额默认为财务结论；只有外部格式明确代表零时才能映射为零。

## 鉴权与网络

示例把“拉取 API”和“标准化”分开。真实 Connector 应：

1. 从 secrets manager 或环境注入获取凭据，不把 token 放进 Box JSON、请求文件或日志。
2. 对 API 版本、分页游标、时区和回溯窗口显式配置。
3. 使用外部对象 ID + 更新时间或内容哈希生成稳定批次 ID。
4. 保存限流、重试和原始响应归档位置，但不在错误中泄露响应中的个人数据。
5. 网络失败时返回失败，不使用旧数据假装本期导入成功。

生成模板拒绝请求体中的 `token`、`api_key`、`secret`、`password` 与 `authorization`；`fetch` 模式在实现分页、超时、重试、限流和脱敏前保持明确失败。Secret 环境变量名可以进入 contract，值不能进入 fixture、lock、异常或审计输出。

## 契约测试

`src/connector_testkit.py` 会把同一虚构 fixture 运行两次，检查：

- 批次质量可用。
- 每类数据达到最低数量。
- 主体范围合法。
- 每条记录证据完整。
- 同一 fixture 的批次 ID、标准数据和质量结果幂等。

新增 Connector 至少要覆盖正常、分页、退款、重复、未知主体、缺字段、币种、时区边界和 API 失败测试。真实凭据和生产响应不能进入测试仓库。

声明式 eval 也支持 `type: connector`，但只允许运行 `mode: fixture` 的离线请求；任何网络 fetch 都会被 eval runner 拒绝。含点号的数据集键在 assertion path 中写作 `~dot~`，例如 `batch.datasets.payments~dot~stripe_payouts.0.status`。
