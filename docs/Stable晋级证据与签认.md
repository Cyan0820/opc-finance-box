# Stable 晋级证据与签认

OPC Finance Box 将“Pack 的 capability 已有代码 provider”和“Pack 已适合标为 `stable`”分开。`pack-audit` 证明前者；stable promotion 证据链用至少两个连续月份、已独立复核且重新验证全部私有源文件的 Pilot Shadow series，配合代表性脱敏 Shadow Close、网络 Connector 的来源 Shadow、自动门、恢复演练和独立发布复核证明后者。单期报告不能进入 stable candidate。

多主体 Box 的代表性样本还应先通过 `shadow-close-portfolio-assemble`、`shadow-close-portfolio-review` 和 `shadow-close-portfolio-verify`：全部主体同期间覆盖，月结来源运行已由 Pipeline 台账核验，组合结果保持预抵销候选边界，并由不同于逐主体 Shadow 复核人的组合复核人签认。组合验收包不保存财务值，也不替代本文件要求的 evidence preparer、threshold approver 与 release reviewer 职责分离。

证据 JSON 的 `multi_entity_shadow_close_portfolios` 对单主体样本必须为空；多主体样本必须按 `sample.periods` 每个期间提供且只提供一份已复核验收包。`promotion-assess` 会重新验证 manifest/review 指纹、当前 Box 全主体范围、逐主体报告 fingerprint/review ID/计数、组合来源台账链头和复核职责分离。评估与追加式发布账本只保留安全摘要，不持久化组合 manifest 或其中的原始证据。

证据 JSON 的 `pilot_shadow_series` 必须提供已复核连续期收据、严格期间证据根和 Pipeline 运行账本根三个私有路径。`promotion-assess` 不信任先前的“已验证”结果：它会以评估日期重新验证 2–24 个连续月份，并按规范目录逐文件重算内容绑定。`shadow_close_reports` 必须是该连续期收据覆盖的同一批逐主体报告；多主体 `multi_entity_shadow_close_portfolios` 也必须是同一批组合签认文件。换用另一批同主体、同月份且自身有效的报告仍会失败关闭。

若目标 Pack 拥有当前 Box 已选中的网络 Connector，证据 JSON 还必须在 `connector_shadow_artifacts` 中按每个样本主体和期间引用已复核来源 Shadow 文件。评估器要求目标 Pack 出现在工件的 `covered_pack_ids` 中，并重新验证时效、主体/期间、assessment/review 指纹、通过结论与职责分离。未覆盖的网络 Connector Pack 只能得到带 blocker 的评估，不能成为 stable candidate。非网络 Pack 保持该数组为空即可。

该流程只能生成 `stable_candidate` 和 `stable_candidate_approved` 证据。它不修改 Pack manifest，不升级 `tax_readiness`，不授权付款、过账、关账或申报。真正的 Pack 状态变更仍必须作为独立、可审阅的源码变更完成。

## 发布链职责分离

1. `shadow_operator`：运行月结、导入人工基准并准备证据。
2. `shadow_finance_reviewer`：对当前 Shadow Close 指纹签认；如接受差异，必须逐项记录分类、说明和证据引用。
3. `shadow_continuity_reviewer`：复核 2–24 个连续月份及跨期状态，必须不同于所有期间角色。
4. `promotion_evidence_preparer` / `threshold_approver`：组装目标 Pack 的代表性 stable 证据并批准不可降低的阈值。
5. `release_reviewer`：复核完整门禁与演练证据，审批一个精确的 assessment fingerprint。

连续性复核人不能与证据准备人、阈值批准人、样本操作人、逐主体/组合复核人或 Connector 来源复核角色重叠。发布复核人不能是 assessment `separation_principals` 中的任何角色。姓名字符串只是审计标识；生产环境还应通过 API role policy、SSO 或组织身份提供强身份证明。

## 证据输入

`promotion-assess` 接受一个 schema v1 JSON，必须精确锁定：

- Box runtime fingerprint、Pack id 和 Pack version；
- 脱敏且具有代表性的主体、期间、操作人及样本来源；
- 已独立复核的连续期收据、严格期间证据根和 Pipeline 台账根；
- 明确携带当前 Box runtime fingerprint、且该指纹下签认仍有效的 Shadow Close report；
- 网络 Connector Pack 每个样本主体/期间的已复核 Connector Shadow 私有文件路径；
- 全部自动 release gates 的时间、Box fingerprint 和证据引用；
- 备份恢复、升级回滚、权限分离和故障恢复演练；
- 已批准阈值和已知限制。

为避免把财务结果复制进控制 ledger，输入必须声明 `contains_financial_results: true` 和 `storage_boundary: input_only_not_persisted`。ledger 只保留连续期期间/计数/决定安全摘要、report/evidence fingerprint、数量、比例、差异分类摘要、Connector 来源安全摘要和签认状态，不保留连续期私有路径、源内容哈希、人工金额、Agent 金额、完整 Shadow report、Connector 私有文件路径或来源/控制明细。

## 不可降低的阈值底线

- 每份 report 至少 6 个比较项；
- 样本和已批准阈值都必须覆盖至少 2 个连续自然月，且期间必须与已复核 Pilot Shadow series 完全一致；
- 每行必须携带 Shadow 比较时的明确 `allowed_tolerance`；评估器会重算 `agent - manual`、缺项状态和容差判断，不信任 JSON 里手填的“一致”。
- 每份 report 必须同时覆盖 `trial_balance` 和 `statement`，税务域可按 Pack 范围追加；
- 批准的最低匹配率不得低于 98%；
- Agent 缺项或人工基准缺项始终阻塞；
- 可接受差异数不得高于 10，并且每项都必须有完整 resolution；
- `system_defect` 不得作为可接受差异进入 stable candidate；
- 自动门最大有效期不得高于 90 天，Shadow 和演练证据最大有效期不得高于 365 天。
- `evaluated_at` 进入 assessment ID，不能在记录前换时间；assessment 必须在 7 天内完成记录和独立发布复核，超时后必须用当前证据重新评估。

阈值可以更严，不能绕过上述下限。不同币种的差异金额不做汇总阈值；金额容差仍由每个 Shadow baseline row 的绝对/百分比容差控制。

## CLI 流程

先按照 [连续 Shadow 期间复核](连续Shadow期间复核.md) 完成 2–24 个连续月份的严格证据目录、收据组装和独立连续性复核。把 reviewed receipt、期间证据根和 Pipeline runs 根的绝对路径分别填入模板的 `pilot_shadow_series`。再把同一目录内的逐主体报告对象放入 `shadow_close_reports`；不要另行生成或复制别的报告，因为晋级评估器会校验文件内容的精确绑定。

若目标是网络 Connector Pack，还要先按 [Connector 来源 Shadow 验收](Connector来源Shadow验收.md) 生成 `private-connector-shadow-reviewed.json`，并把其绝对路径放进模板的 `connector_shadow_artifacts` 数组。一个路径只能对应一个样本主体/期间，目标 Pack 必须在该工件的 `covered_pack_ids` 中。

先用当前 Box fingerprint 和目标 Pack 版本生成一份故意未完成的填写起点；输出路径已存在时会拒绝覆盖：

```bash
opc-finance-box promotion-template BOX.json core.finance \
  --output promotion-evidence.json
```

编译 Bundle 中也会同时生成 `stable-promotion-evidence-templates.json` 和 `stable-promotion-evidence.schema.json`。每个未达 stable 的已选 Pack 都有一份绑定模板；`anonymized=false`、`representative=false`、空 Shadow reports、未通过 gate 和 `REQUIRED_*` 占位符会保证它在填完前失败关闭，不是可以直接签认的伪证据。JSON Schema 负责结构约束；指纹、时效、覆盖、勾稽和职责分离仍由 `promotion-assess` 做语义校验。

先做无写入评估：

```bash
opc-finance-box promotion-assess BOX.json promotion-evidence.json
```

评估通过后，用 evidence preparer 的身份记录精确 assessment：

```bash
opc-finance-box promotion-record BOX.json promotion-evidence.json \
  --actor promotion-preparer \
  --promotion-root /var/lib/opc-finance/release_promotion
```

然后由独立发布复核人审批：

```bash
opc-finance-box promotion-review BOX.json ASSESSMENT_ID \
  --decision approved \
  --actor release-reviewer \
  --rationale "已复核全部 Shadow、自动门、职责分离和恢复演练证据" \
  --evidence-reference audit://release-review \
  --promotion-root /var/lib/opc-finance/release_promotion
```

查询与校验：

```bash
opc-finance-box promotion-status BOX.json \
  --promotion-root /var/lib/opc-finance/release_promotion

opc-finance-box promotion-verify \
  --promotion-root /var/lib/opc-finance/release_promotion
```

ledger 使用私有权限的追加式 JSONL SHA-256 chain。它能发现修改和断链，但不是数字签名或 WORM；生产环境应将它纳入加密备份、外部保留和组织签名流程。

需要把批准状态接回十一阶段生产准备总表时，将账本目录以只读方式挂载并设置：

```text
OPC_STABLE_PROMOTION_ROOT=/absolute/private/promotion/ledger
```

总表不会调用写入式 `promotion-status`：它要求既有 `0700` 目录、`0600` ledger/lock，取得共享文件锁后只读验证整条 hash chain，再按当前 Box fingerprint、Pack id 与 version 投影每个 Pack 的最新状态。响应不含路径、assessment id、角色、证据引用、指标明细或金额。部分 Pack 已批准仍保持 Box 级门禁关闭；该投影也不会修改 Pack manifest。

## 从 candidate 到 Pack 源码

`stable_candidate_approved` 只是后续源码变更的必需证据。修改 Pack manifest 前还必须：

1. 在变更评审中引用 assessment id 和 review event hash；
2. 确认 assessment 的 runtime fingerprint、Pack version 与待发布源码一致；
3. 对状态变更后的最终 wheel 重跑 distribution verify 和 deployment smoke；
4. 税务 Pack 另行评估 `design → workpaper → filing_assist`，不因 Pack `stable` 自动升级。
