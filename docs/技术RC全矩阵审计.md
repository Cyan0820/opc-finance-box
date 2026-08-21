# 技术 RC 全矩阵审计

`release-candidate-audit` 把 OPC Finance Box 的“可 fork 技术发行候选”变成一个可重复执行的产品契约。它不是简单列出 Pack，而是从当前安装目录实际生成、编译并重验游戏、DTC 独立站和 Marketplace 三类产品的完整 Starter / Tax Pack 矩阵。

## 运行

源码工作区：

```bash
python -m src.cli release-candidate-audit --project-root .
```

正式发布时同时绑定 wheel 与 Source Kit：

```bash
opc-finance-box release-candidate-audit \
  --wheel /absolute/release/opc_finance_box-0.1.0-py3-none-any.whl \
  --source-kit /absolute/release/opc-finance-box-source-kit.zip
```

自定义 Pack 根目录必须使用全局参数，放在子命令之前：

```bash
opc-finance-box --packs /absolute/packs release-candidate-audit \
  --project-root /absolute/product-root
```

完整审计会重复生成并逐成员复现全部 Handoff，当前通常需要 2–3 分钟。它适合发布门和定期主干检查，不适合作为每个浏览器请求的即时健康检查。

## 自动证明范围

当前安装矩阵会验证：

- 36 个 Pack 的契约与 114 项 capability 全部有 executable provider，0 项 declared-only；
- 3 个产品 profile × 15 个已安装纳税地区 = 45 个 Starter Handoff；
- 每个 Handoff 都绑定当前 Pack、Box runtime fingerprint、40 个编译工件、部署模板和逐成员 manifest，并能由当前安装内容逐字节复现；
- 每个 profile 暴露的全部集成预设都至少启用一个真实 Connector 或 Feature；当前共 19 个 profile-scoped 集成变体；
- 游戏、DTC、Marketplace 各有一个两国、两法律主体的可编译变体，法定主体仍分离；
- Finance boundary Eval 全部通过，部署资产继续满足非 root、loopback、只读文件系统、职责分离与 Secret 引用控制；
- 提供 wheel 时验证元数据、console entry points 和强制产品成员；提供 Source Kit 时验证 allowlist、manifest、可复现性和无私有/运行数据边界。

结果的 `matrix_fingerprint` 绑定 Starter Handoff、集成、多主体、Eval 与 Pack 数量。新增国家、Connector、Feature、Pipeline 或编译内容都会改变 fingerprint，发布人不能沿用旧的 RC 结果。

## 结果语义

- `source_tree_release_candidate=true`：当前安装源可以完整生成三类产品矩阵。
- `release_artifacts_provided=true`：wheel 与 Source Kit 两者都已传入。
- `release_artifacts_verified=true`：两份发行工件都与当前安装产品匹配并通过各自 verifier。
- `passed=true`：源矩阵通过，且如果两份发行工件都提供，它们也必须通过。
- CLI 在审计结果不通过时返回退出码 6；结构、文件或 Pack 错误返回退出码 2。

只提供其中一份工件时，该工件仍会被验证，但 `release_artifacts_provided` 和 `release_artifacts_verified` 保持 false，避免把半套交付误说成完整发行包。

## 明确不证明

技术 RC 不等于 `stable`，也不等于真实客户可以无人值守上线。审计不读取凭证或私有财务数据，不发网络 Connector 请求，不启动服务器，不验证真实商户字段，也不制造税务适用性、真实 Shadow Close、连续月份、发布签认或申报回执。

因此结果固定保留：

- `stable_release_ready=false`
- `real_customer_shadow_evidence_verified=false`
- `tax_filing_ready=false`
- `posting_payment_or_filing_authorized=false`

真实投产仍必须进入十一阶段 Production Readiness Matrix，逐主体完成税务适用性、Connector Shadow、首客准入、资料交接、首次月结、连续期间和独立 Stable 晋级签认。
