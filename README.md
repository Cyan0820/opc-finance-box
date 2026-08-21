# OPC Finance Box

面向游戏与跨境 OPC 的开源、本地优先财务 Agent 工作台。

导入渠道结算、银行流水和业务资料，生成可由会计复核的月结差异、凭证候选、经营分析和证据包。系统默认不自动过账、不付款、不申报。

> Open-source, local-first finance agents for founder-led game and commerce companies. Human review remains required for accounting, tax and payment decisions.

## 它先解决什么

第一个公开场景是**游戏公司月结**：

```text
渠道结算 / 订单与退款
        + 银行流水
        + 采购、人员与项目成本
        ↓
按主体、期间和币种整理证据
        ↓
收入与到账勾稽、差异解释、凭证候选
        ↓
会计复核后形成 Shadow Close 证据包
```

它适合希望自己掌握数据和源码、但尚未配置完整财务团队的创始人型公司。当前游戏公司工作台是完整参考实现；同一套 Finance Core 也能装配 DTC 独立站和 Marketplace 电商 Box。

## 五分钟本地体验

需要 Python 3.10 或更高版本。

```bash
git clone https://github.com/Cyan0820/opc-finance-box.git
cd opc-finance-box
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python run.py
```

打开 `http://127.0.0.1:8765`。默认加载虚构的全球游戏公司演示数据，不需要真实凭证，也不会访问外部账户。

Windows PowerShell 激活虚拟环境时使用：

```powershell
.venv\Scripts\Activate.ps1
```

想从一个干净、可验证的本地试用目录开始：

```bash
python -m src.cli trial-init /absolute/path/my-trial \
  --profile game --country CN --actor local-user
python -m src.cli trial-onboarding /absolute/path/my-trial
python -m src.cli trial-run /absolute/path/my-trial
```

完整说明见[五分钟本地试用](docs/五分钟本地试用.md)。

## 你会看到什么

- 游戏渠道收入、结算与到账差异；
- 项目人力、采购、授权和云资源成本候选；
- 月结任务、缺失资料和待复核队列；
- 凭证、税务底稿和管理分析草稿；
- 每项结论使用的数据来源、控制条件和人工确认边界；
- 单主体与多主体的独立账务进度，不进行隐式换汇或法定合并。

## 为什么是 Finance Box

项目交付的不是黑盒 SaaS，而是一套可 fork、可替换和可组合的财务底座：

```text
Finance Core
+ 行业 Pack（Game / Commerce）
+ 渠道 Pack（App Store / Google Play / 国内游戏渠道 / DTC / Marketplace）
+ Connector Pack（文件 / Shopify / Stripe / Wise / Xero / PayPal 等）
+ 法律主体与多主体控制
+ 纳税地区 Pack
= 一个可运行的 OPC Finance Box
```

业务类型、销售渠道和纳税地区彼此独立。管理视图可以汇总进度，但法定账、银行、税务和审批始终保持主体隔离。

## 当前能力

| 层 | 当前范围 |
|---|---|
| Finance Core | 银行与余额调节、总账/试算平衡控制、月结、凭证候选、证据链、经营分析 |
| Game Studio | 渠道结算、项目成本、预付成本、项目利润、LTV/ROI 与游戏工作台 |
| Commerce | 订单、退款、履约、库存、平台结算和 Shopify/Stripe 订单到款证据链 |
| Connector | CSV、XLSX、受限 PDF/OCR，以及多个只读 API Connector |
| Multi-entity | 主体隔离、原币保留、显式汇率和逐主体 Shadow Close |
| Tax Pack | 中国大陆工作底稿，以及多个地区的版本化设计级 Pack |
| Delivery | CLI、Workbench、wheel、确定性 Handoff、可 fork Source Kit 和部署模板 |

完整清单、成熟度和仍缺少的真实验证见[产品成熟度与路线图](docs/产品成熟度与路线图.md)。

## 当前状态：技术预览

这是一个 `0.1.0`、本地单用户、Alpha/Preview 项目。

- 当前没有 Pack 被标记为 `stable`；
- 尚未完成代表性真实公司、连续两个月、外部会计独立签认的 Shadow Close；
- 真实 Connector 需要使用者自行取得合法授权并显式启用只读网络访问；
- OCR、凭证、税务和 Agent 输出都是候选，不替代会计师、税务师或有权人的复核；
- 付款、正式过账和外部申报默认禁用。

请不要把演示、测试通过或能力覆盖解释为生产成熟度。首次真实使用建议从一个主体、一个月、一个只读流程开始，并与现有人工结果并行比较。

## 使用自己的公司做 Shadow Close

建议按以下顺序开始，而不是一次接入全部能力：

1. 选择一个法律主体和一个已完成人工月结的期间；
2. 只导入渠道结算、银行流水及必要成本资料；
3. 保留原会计结果作为 baseline；
4. 运行只读 Shadow Close，分类处理差异；
5. 由独立会计复核，连续验证至少两个月；
6. 验证稳定后，再考虑更多 Connector 或自动化动作。

操作说明见[Shadow Close 试运行](docs/Shadow%20Close试运行.md)和[逐主体月结与外部会计复核包](docs/逐主体月结与外部会计复核包.md)。

## 常用命令

查看已安装的行业、渠道、集成和纳税地区：

```bash
python -m src.cli options
python -m src.cli box-starters
```

检查默认游戏 Box：

```bash
python -m src.cli validate examples/boxes/global_game_studio.json
python -m src.cli doctor examples/boxes/global_game_studio.json
```

运行声明式财务边界评测：

```bash
python -m src.cli eval evals/core_packs.json
```

创建自己的单主体 Starter：

```bash
python -m src.cli starter-init /absolute/path/my-box \
  --profile game --country CN --actor founder
```

更多操作请从[文档索引](docs/文档索引.md)进入，不建议从完整 CLI 帮助逐项探索。

## 项目结构

```text
src/          Finance Core、Agent runtime、服务与控制
packs/        行业、渠道、Connector 与纳税地区 Pack
examples/     虚构 Box、请求和演示数据
public/       本地 Workbench 前端
box/          配置与证据 Schema
evals/        声明式评测
tests/        单元与集成测试
docs/         架构、运行、Connector、税务与 Shadow Close 文档
deployment/   容器和 systemd 部署起点
skills/       可选的 Codex/Agent 操作技能
```

## 验证

快速验证：

```bash
python -m unittest discover -s tests -q
python -m src.cli pack-audit
python -m src.cli eval evals/core_packs.json
python -m src.cli doctor examples/boxes/global_game_studio.json
```

全矩阵 RC 审计会生成和复验大量交付物，适合发布前运行：

```bash
python -m src.cli release-candidate-audit --project-root .
```

CI 在 Python 3.10 和 3.12 上运行完整单元测试。

## 参与项目

欢迎提交真实但完全脱敏的版式兼容问题、失败用例、测试、文档和 Connector 改进。请先阅读[贡献指南](CONTRIBUTING.md)。不要在 Issue、测试 fixture 或提交记录中包含真实财务资料、客户信息或凭证。

## 安全与责任边界

- 服务默认只监听 `127.0.0.1`；不要把开发服务器直接暴露到公网；
- 原始上传、运行数据和生成物默认不进入 Git；
- 网络 Connector 的密钥只能通过运行环境注入；
- 正式多用户部署仍需 TLS、SSO/组织角色、数据库事务、备份恢复和不可篡改审计；
- 本项目不提供会计、税务、审计、法律或投资意见。

安全问题请按 [SECURITY.md](SECURITY.md) 私下报告。

## License

[MIT](LICENSE)
