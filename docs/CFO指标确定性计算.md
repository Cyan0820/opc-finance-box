# CFO 指标确定性计算

`cfo-metric-catalog.json` 是指标定义合同，`core.evaluate_cfo_metrics` 是对应的只读确定性执行器。执行器只处理当前 Box 通过 Pack 选择出的指标和白名单操作符；它不运行表达式、不调用 LLM、不读取任意路径、不自动换汇，也不执行记账、付款或申报。

## 运行边界

每次请求必须同时绑定：

- 当前 Box 的 64 位 `runtime_fingerprint`；Pack、主体或配置变化后旧请求立即失效。
- 一个由 Service 外层 `entity_id` 或 CLI `--entity` 指定的法律主体。
- 一个 `YYYY-MM` 自然月期间。
- 该法律主体的本位币；输入必须已在上游完成来源勾稽和经批准的显式折算。
- 当前指标所需的标准操作数，以及已经完成的控制类型 ID。
- 可选的 `dimension_scope`；标题、产品等细分范围会进入输入指纹，不能在结果生成后替换。

支持的公式只有 `subtract`、`safe_divide`、`safe_divide_scaled`、`count`、`rollforward`、`max_share` 和 `absolute_difference`。金额、比率、月数和天数使用十进制字符串返回，计数返回整数，避免二进制浮点序列化歧义。

缺操作数、缺控制、零或不允许的非正分母、向量总额不一致都会返回显式状态；不会把缺数填成 0。请求包含未知指标、未知操作数、未知控制、过期运行指纹、非本位币或任意额外字段时，整个请求失败关闭。

## CLI

先读取当前运行指纹：

```bash
opc-finance-box context examples/boxes/us_dtc_shopify_stripe_c_corp.json \
  --scope statutory --entity us_store
```

复制 `examples/service_requests/cfo_metric_evaluation_dtc.template.json`，只把其中的运行指纹和经复核操作数替换为当前值，然后执行：

```bash
opc-finance-box cfo-metrics-evaluate \
  examples/boxes/us_dtc_shopify_stripe_c_corp.json \
  /private/path/cfo-metric-request.json \
  --entity us_store
```

也可通过统一服务边界调用 `core.evaluate_cfo_metrics`：

```json
{
  "service_id": "core.evaluate_cfo_metrics",
  "entity_id": "us_store",
  "payload": {
    "runtime_fingerprint": "CURRENT_RUNTIME_FINGERPRINT",
    "period": "2026-07",
    "currency": "USD",
    "metric_type_ids": ["dtc_net_sales"],
    "operand_values": {
      "gross_order_sales_ex_tax_including_shipping": "1250.25",
      "discounts_and_refunds_ex_tax": "125.20"
    },
    "confirmed_control_type_ids": [
      "order_and_refund_period_scope_aligned",
      "tax_inclusive_policy_confirmed"
    ]
  }
}
```

`confirmed_control_type_ids` 是调用方对已完成控制的结构化声明，不是执行器自行验证的证据。真实 Shadow 使用时应把请求、返回的 `input_fingerprint`、操作数来源工作底稿和复核证据一并保留。指标结果是经营管理候选，不替代总账勾稽、会计政策、法定关账或税务判断。

标准 Pipeline 与部分确定性 Service 可在受信任执行边界内直接返回 `cfo_metric_operand_assembly`，省去手工复制操作数，同时把可自动证明与仍需人工确认的控制分开。详见 [CFO 指标操作数自动组装](CFO指标操作数自动组装.md)。调用方提交的任意历史结果不会被重新当作受信任来源。

通用 JSON 形状位于 `box/cfo-metric-evaluation-request.schema.json`；当前 Box 实际允许的指标、操作数与控制集合仍以编译后的 `cfo-metric-catalog.json` 为准，并由运行时再次校验。
