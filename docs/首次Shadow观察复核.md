# 首次 Shadow 观察复核

这一步回答的不是“首次月结 Pipeline 跑过了吗”，而是“首次真实并行观察的全部证据是否与登记时的主体、期间和 Pipeline attempt 形成了可重复验证的闭环”。输出是私有、不含金额的观察收据；它只决定能否进入下一 Shadow 期间，不能让 Pack 晋级 stable，也不授权过账、付款、关账或申报。

## 前置证据

必须同时具备：

- 当前有效的 Pilot 准入签认、资料交接签认和首次 Shadow Run 登记；
- 每个法律主体一份当前已签认的 Shadow Close 报告，主体和期间与登记完全一致；
- 报告签认决定只能是“验证通过”或“接受差异”；接受差异时必须逐项记录分类、说明和不含敏感值的证据引用；
- 多主体 Box 还要有一份已独立复核的 `multi_entity_shadow_close_acceptance`，其 `source_attempt_ids` 必须与首次登记逐主体 attempt 集合完全一致；
- 实体复核人彼此可不同；组合复核人不得是任何实体复核人或登记人；最终观察复核人必须同时不同于登记人、全部实体复核人和组合复核人。

源 Shadow Close 报告含人工值和 Agent 值，必须保持 `0600`、只读传递。观察收据只保存主体、计数、决定、内容 SHA-256 和必要的角色标识，不复制比较金额、理由或证据内容。

## 1. 组装观察收据

单主体：

```bash
opc-finance-box pilot-shadow-observation-assemble BOX.json \
  private/pilot-shadow-run-registration.json \
  private/pilot-data-handoff-reviewed.json \
  private/pilot-readiness-reviewed.json \
  --runs-root private/pipeline-runs \
  --entity-report private/entity-shadow-reports/cn_dtc_company.json \
  --as-of 2026-08-14 \
  --output private/pilot-shadow-observation.json
```

多主体要对每个主体重复 `--entity-report`，并附上已经独立签认的组合文件：

```bash
  --entity-report private/entity-shadow-reports/cn_studio.json \
  --entity-report private/entity-shadow-reports/sg_publisher.json \
  --portfolio-review private/pilot-shadow-portfolio-reviewed.json
```

单主体附组合文件、多主体缺组合文件、主体或期间不一致、attempt 集合变化、任何源签认无效，都会失败关闭。输出采用独占创建，不覆盖旧收据。

## 2. 第四角色复核

无差异时：

```bash
opc-finance-box pilot-shadow-observation-review BOX.json \
  private/pilot-shadow-observation.json \
  --decision passed \
  --actor observation-independent-reviewer \
  --rationale "已独立检查登记、逐主体报告与组合签认的完整绑定" \
  --evidence-reference audit://pilot/2026-08/observation-review \
  --output private/pilot-shadow-observation-reviewed.json
```

存在已经逐项解释、且没有 `system_defect` 的差异时，只能选 `accepted-differences`。任何差异被分类为 `system_defect` 时，观察候选自动变为 `needs_correction`；最终复核只能选择 `needs-correction`，并关闭下一 Shadow 期间放行门。即使候选干净，复核人仍可因范围、证据或治理问题选择 `needs-correction`。

## 3. 持续验证

```bash
opc-finance-box pilot-shadow-observation-verify BOX.json \
  private/pilot-shadow-observation-reviewed.json \
  private/pilot-shadow-run-registration.json \
  private/pilot-data-handoff-reviewed.json \
  private/pilot-readiness-reviewed.json \
  --runs-root private/pipeline-runs \
  --entity-report private/entity-shadow-reports/cn_dtc_company.json \
  --as-of 2026-08-14
```

验证会重新检查整条准入/交接/登记/Pipeline 台账链，并重新读取每份源报告和组合签认。报告理由被修改、组合内容变化、登记被替换、当前 Pipeline gate 被后续驳回，都会使既有观察复核失效。CLI 结果不会回显 attempt ID、源报告指纹、内容哈希、人员、复核理由、证据引用或金额。

## 4. Workbench 只读挂载

生产 Workbench 使用以下只读环境变量：

- `OPC_PILOT_SHADOW_OBSERVATION_REVIEW`：最终复核文件；
- `OPC_PILOT_SHADOW_ENTITY_REPORT_DIR`：只包含当前 Box 每个主体恰好一个 `<entity_id>.json` 的目录；多余、缺失或符号链接条目都会失败关闭；
- `OPC_PILOT_SHADOW_PORTFOLIO_REVIEW`：仅多主体 Box 配置；
- 前序 `OPC_PILOT_READINESS_REVIEW`、`OPC_PILOT_DATA_HANDOFF_REVIEW` 和 `OPC_PILOT_SHADOW_RUN_REGISTRATION` 仍必须同时挂载。

`GET /api/box/pilot-shadow-observation` 与 Doctor 只展示期间、主体数、比较/差异/系统缺陷计数、组合复核是否配置以及下一 Shadow 期间布尔门禁。浏览器不能提交私有路径，也不能读取原始报告。

## 权限边界

`ready_for_next_shadow_period=true` 仅证明当前首次观察证据闭环仍有效。它明确不表示：

- Pack 达到 stable、filing_assist 或生产质量；
- 会计政策、税务适用性或管理判断已获批准；
- 主体账簿已经过账或期间已经关闭；
- 付款、退款、资金划转、税务申报或任何外部提交可以执行。

这些能力必须继续走各自的确定性规则、独立权限和外部系统控制。
