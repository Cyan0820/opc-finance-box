# 开发自定义 Pack Service

Service 是 Pack capability 的可执行边界。它不是随意暴露一个 Python 函数，而是声明“哪个 Pack 提供什么能力、能否确定性复算、作用于哪些法律主体、会不会改变状态、需要哪一道人工控制”。

## 1. 先定义能力归属

在目标 Pack 的 `manifest.json` 中声明 capability。不要把行业规则放入 `core.finance`，也不要把某国税务规则放入行业 Pack。

典型归属：

- 通用银行、采购、月结：`core.finance`
- 游戏项目利润：`industry.game_studio`
- DTC 支付退款：`channel.dtc_storefront`
- 中国税务工作底稿：`jurisdiction.cn_mainland`

## 2. 编写纯 handler

handler 接收 `payload` 与 `ServiceContext`，返回字典：

```python
def summarize(payload, context):
    allowed = set(context.entity_ids)
    rows = payload.get("rows") or []
    if any(row.get("entity_id") not in allowed for row in rows):
        raise ValueError("record is outside the selected entity scope")
    return {"entity_ids": list(context.entity_ids), "rows": rows}
```

确定性服务不调用 LLM，不读取隐式汇率，不把缺失金额当零。输出应保留主体、币种、期间、来源证据、规则版本和候选/定稿状态。

## 3. 注册契约

在 `src/default_services.py` 注册 `ServiceDefinition`：

```python
registry.register(ServiceDefinition(
    service_id="my_pack.summarize",
    pack_id="industry.my_pack",
    capability="my_pack.summary",
    display_name="生成业务摘要",
    handler=summarize,
    deterministic=True,
    action_class="read",
    entity_scope="management",
))
```

`action_class`：

| 类型 | 约束 |
|---|---|
| `read` | 只读取或计算，不改变正式状态 |
| `draft` | 生成候选政策、底稿、目标或审批事件，不声称已生效 |
| `mutating` | 修改正式状态，必须声明 review gate 且传入批准记录 |
| `external` | 银行、申报等外部动作，必须声明 review gate 且传入批准记录 |

`entity_scope`：

- `statutory`：调用时必须提供一个 `entity_id`。
- `management`：调用时可提供 `entity_ids`，默认使用 Box 全部主体。
- `none`：只用于真正与主体无关的能力。

地区 Pack 服务还会校验该主体实际选择了同一个 `tax_pack`，不能用 SG 服务处理 CN 主体。

## 4. 测试控制边界

至少覆盖：

1. 只有选择该 Pack 的 Box 能看到并调用服务。
2. 未知主体、跨主体数据与错误地区包被拒绝。
3. 金额按币种分组，不隐式折算。
4. 缺失证据或负库存等硬错误进入 blocker，不变成建议。
5. `mutating` / `external` 没有满足 review gate 时不能执行。
6. 草稿输出明确 `state_changed=false` 或相应候选状态。

完成后运行：

```bash
python -m unittest discover -s tests -q
python -m src.cli pack-audit
```

只有 capability 出现在 `executable` 覆盖中，才说明它绑定了代码 provider；Pack 的 `preview/experimental/stable` 与税务 `design/workpaper/filing_assist` 仍需独立评估。
