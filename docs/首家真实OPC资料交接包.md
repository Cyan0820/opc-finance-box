# 首家真实 OPC 资料交接包

这套流程把“已批准开始受限 Shadow Close”与“真实资料可以受控交接”分开。它适用于游戏、电商、独立站及多主体 Box，必须绑定一份仍有效的 `pilot-readiness-review`。

资料交接工件不是数据仓库。它只记录逐主体资料域的传输方式、文件数量、私有来源清单 SHA-256、期间覆盖、个人数据分类、隐私控制和不透明证据引用；不复制源文件、文件名、路径、账号、税号、凭证、密钥或财务金额。

## 1. 生成私有交接底稿

```bash
opc-finance-box pilot-data-handoff-init BOX.json \
  private/pilot-readiness-reviewed.json \
  --prepared-by handoff-preparer \
  --custodian-principal controlled-data-custodian \
  --as-of 2026-08-14 \
  --output private/pilot-data-handoff-workpaper.json
```

工件以 `0600` 独占创建，不覆盖旧文件。它从当前 Box 与 Pilot 准入计划精确复制主体和资料域：游戏会包含渠道结算，多主体会包含内部交易，电商/独立站会包含订单、支付结算、退款退货、履约及适用的库存与进口到岸成本。

逐项填写：

- `status`：`delivered` 或在计划允许时填写 `not_applicable`；
- `transfer_mode`：`local_only`、`encrypted_archive` 或 `controlled_drive`；
- `source_file_count`：私有来源清单中的文件数量，不填文件名；
- `source_manifest_sha256`：由受控环境中的私有清单计算，不是单个财务文件的公开内容摘要；
- `period_coverage`：必须精确等于首期 Shadow Close 期间；
- `contains_personal_data` 与 `privacy_control`：个人数据为 `yes` 时必须选择 `anonymized` 或 `access_restricted`；
- `source_owner`、`access_approved_by` 与 `evidence_references`：只使用稳定角色标识和不含敏感值的不透明引用。

来源负责人、访问批准人和数据保管人必须分离。清单 SHA-256 只证明某份私有清单未变化，不证明其中的文件安全、完整、合法取得或财务口径正确；这些仍需人工复核。

## 2. 独立访问复核

```bash
opc-finance-box pilot-data-handoff-review BOX.json \
  private/pilot-data-handoff-workpaper.json \
  private/pilot-readiness-reviewed.json \
  --actor independent-access-reviewer \
  --rationale "已核对逐主体资料范围、传输控制、隐私分类与访问授权" \
  --evidence-reference advisor://pilot/data-access-review \
  --as-of 2026-08-14 \
  --output private/pilot-data-handoff-reviewed.json
```

独立复核人不能是交接准备人或数据保管人。主体/资料域错配、跨主体映射、必需资料缺失、无效 SHA-256、个人数据无保护、职责重叠、Pilot 签认变更或过期都会失败关闭。

## 3. 安全验证

```bash
opc-finance-box pilot-data-handoff-verify BOX.json \
  private/pilot-data-handoff-reviewed.json \
  private/pilot-readiness-reviewed.json \
  --as-of 2026-08-14
```

安全输出只包含 Box/Pilot/交接复核标识、主体数、资料域计数、交付/不适用计数和布尔门禁。它不返回文件名、路径、来源清单哈希值、角色、证据引用、账号、税号或金额，也不导入任何数据。

`ready_for_controlled_data_intake=true` 只表示交接控制清单可以支持下一步受限导入。它不证明数据质量、对账完成、会计政策正确、税务适用性已确认，也不授权过账、付款、关账或申报。后续仍要执行数据质量门、来源映射复核、首次 Shadow Close 对比和独立签认。

下一步使用 `pilot-shadow-run-register/verify` 将每个法律主体一个、与交接期一致且全部当前 review gate 已批准的月结 Pipeline attempt 绑定为首期受控观察登记。详见 [首次月结 Shadow Run 登记](首次月结ShadowRun登记.md)。

## 职责与存储建议

建议把角色至少拆成：Pilot 准入准备人、数据操作人、交接准备人、数据保管人、来源负责人、访问批准人、交接独立复核人、人工关账基准负责人。小团队可以由外部会计或税务顾问承担独立复核，但同一具体控制不能自批。

原始资料应保留在客户控制的加密位置或本地受控目录，使用最小权限、到期回收和独立备份。不要把 raw token、完整银行账号、税号、客户个人信息或财务明细写入交接 JSON、Git、聊天记录或公开工单。
