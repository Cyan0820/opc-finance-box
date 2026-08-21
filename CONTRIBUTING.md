# Contributing

感谢你帮助 OPC Finance Box 变得更可靠。这个项目处理高风险财务工作流，因此可复验性、数据边界和明确失败比功能数量更重要。

## 适合提交的内容

- 使用虚构或完全脱敏数据复现的解析、映射和勾稽问题；
- 新增边界条件、失败场景和回归测试；
- 改善五分钟试用、错误提示和操作文档；
- 不扩大权限的数据源兼容性改进；
- 有官方来源、版本和生效日期的纳税地区规则更新；
- 对 Pack、Schema、review gate 和升级兼容性的修正。

## 不要提交

- 真实银行流水、发票、工资、税号、客户或员工信息；
- API token、cookie、OAuth credential、私钥或账户标识；
- 未取得授权的数据抓取方式；
- 绕过人工复核、权限或失败关闭控制的“便利”改动；
- 把候选底稿描述成正式申报、审计意见或专业建议的文案。

即使密钥已经失效，也不要把它加入 Git。若敏感信息曾进入提交历史，请先轮换凭证，再联系维护者处理历史。

## 本地开发

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python -m unittest discover -s tests -q
```

提交前至少运行与你改动直接相关的测试。修改 Pack、Schema、编译或交付逻辑时，还应运行：

```bash
python -m src.cli pack-audit
python -m src.cli eval evals/core_packs.json
python -m src.cli doctor examples/boxes/global_game_studio.json
```

发布负责人会另行运行全矩阵 `release-candidate-audit`。

## 变更原则

1. 保留原始证据引用，不把推断写成事实。
2. 缺主体、币种、期间、来源或授权时失败关闭。
3. 确定性计算与 Agent 判断分离。
4. 任何写入、付款、过账或外部申报能力都需要显式授权、幂等、审计和恢复设计。
5. 新能力必须说明成熟度和仍需人工完成的事项。
6. 使用虚构 fixture；测试值应明显表现为占位符。

## Issue 建议格式

请包含：

- 使用的 Python 和操作系统版本；
- Box profile 与相关 Pack；
- 最小复现步骤；
- 期望结果与实际结果；
- 完全脱敏的输入结构；
- 是否影响金额、主体归属、期间、权限或审计证据。

安全漏洞不要提交公开 Issue，请按 [SECURITY.md](SECURITY.md) 私下报告。
