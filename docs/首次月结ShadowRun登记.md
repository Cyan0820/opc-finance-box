# 首次月结 Shadow Run 登记

这一步把“真实资料已经受控交接”与“每个法律主体已经完成首个月结试跑并通过全部人工复核”连接起来。登记只组装已有控制证据，不新增审批权，也不执行 Pipeline。

适用前提：

- 当前 Box 的 Pilot 准入签认仍有效；
- 同一期间的资料交接签认已经通过独立访问复核；
- 每个法律主体都有一个 `finance.month_close_control` 台账 attempt；
- 每个 attempt 都为 `ready`，且全部 required review gate 的当前决定均为 `approved`；
- Pipeline 台账 SHA-256 链完整，运行没有外部动作、过账或完整请求/结果持久化；
- 登记人不同于运行操作人和全部当前复核人。

## 1. 找到逐主体月结 attempt

先按主体运行并记录月结 Pipeline，再完成所有 gate 的独立复核：

```bash
opc-finance-box pipeline BOX.json month-close-cn.json \
  --record --runs-root private/pipeline-runs --actor shadow-operator

opc-finance-box pipeline-run-review BOX.json ATTEMPT_ID \
  --runs-root private/pipeline-runs \
  --gate bank_statement_mapping_review \
  --decision approved \
  --actor independent-close-reviewer \
  --rationale "已核对银行来源映射与期间证据" \
  --evidence-reference evidence://shadow/2026-08/bank-mapping
```

对该 attempt 的每个 required gate 重复复核命令。多主体 Box 必须分别运行主体法定范围内的月结；不能用管理合并结果替代任何主体记录。

## 2. 独立登记

单主体：

```bash
opc-finance-box pilot-shadow-run-register BOX.json \
  private/pilot-data-handoff-reviewed.json \
  private/pilot-readiness-reviewed.json \
  --runs-root private/pipeline-runs \
  --entity-attempt cn_dtc_company=0123456789abcdef01234567 \
  --actor shadow-run-registrar \
  --rationale "已确认首期逐主体月结试跑及全部当前复核决定" \
  --evidence-reference workpaper://shadow/2026-08/registration \
  --as-of 2026-08-14 \
  --output private/pilot-shadow-run-registration.json
```

多主体时对每个主体重复 `--entity-attempt`：

```bash
--entity-attempt cn_studio=0123456789abcdef01234567 \
--entity-attempt sg_publisher=89abcdef0123456701234567
```

主体集合必须与当前 Box 完全一致，attempt 不能重复。登记文件以 `0600` 独占创建且不会覆盖旧文件。

## 3. 持续验证

```bash
opc-finance-box pilot-shadow-run-verify BOX.json \
  private/pilot-shadow-run-registration.json \
  private/pilot-data-handoff-reviewed.json \
  private/pilot-readiness-reviewed.json \
  --runs-root private/pipeline-runs \
  --as-of 2026-08-14
```

验证会重新读取当前 Pipeline 台账，而不是只相信登记时的快照。以下任一情况都会失败关闭：Box/主体/期间变化、Pilot 或交接签认变化或过期、台账链被篡改、attempt 不存在、结果指纹变化、任一当前 gate 被驳回、主体覆盖不完整，或职责分离失效。登记之后正常追加其他台账事件不会让历史链头失效。

生产服务可用 `OPC_PILOT_SHADOW_RUN_REGISTRATION` 以只读方式挂载登记文件。Doctor 与 `GET /api/box/pilot-shadow-run` 只投影状态、期间、主体覆盖数和布尔门禁，不返回 attempt ID、结果指纹、执行人、复核理由、证据引用或金额。

## 权限边界

`ready_for_first_shadow_observation=true` 仅表示可以开始观察和比较首期受控月结结果。它不证明会计政策正确、税务适用性成立或数据不存在遗漏，也不授权：

- 会计过账或修改主体账簿；
- 付款、退款或资金划转；
- 法定关账或锁定期间；
- 税务申报、外部提交或 stable 晋级。

这些动作仍必须经过各自的确定性控制、独立签认和外部系统权限。
