# 多主体 Tax Pack 工作台

Tax Pack 工作台把一个 Box 中每个法律主体的地区税务包投影到同一只读界面，但不会把不同主体的法定账、登记事实或候选期限混在一起。它适用于游戏、电商、独立站和平台店铺 Box；行业与渠道决定业务数据流程，法律主体选择自己的版本化纳税地区 Pack。

## 工作台回答什么

每个主体单独显示：

- 法定名称、司法辖区、本位币、会计准则和财年结束日；
- Tax Pack ID、版本、状态、`design` / `workpaper` / `filing_assist` 成熟度；
- 规则生效日、最近复核日、authority scope 和 Pack 范围限制；
- 官方规则来源的 `current` / `review_due` / `expired` 生命周期；
- 逐主体适用性签认的事实截止日、提醒日、失效日与安全状态；
- 已配置的登记**类别**以及仍需取得的登记证据；
- 当前年度的候选日历、缺失的属地配置和人工 review gate；
- 从版本化规则自动发现的日期事实输入，以及不落库的候选日期预览；
- Pack 中版本化规则与官方来源；
- 登记画像、证据清单和候选日历服务的无密钥 JSON 请求模板。

全球游戏样板会显示 `cn_studio` 与 `sg_publisher` 两张独立主体卡片；英国独立站样板会显示 CT600 和 Companies House 候选日期，同时把 Corporation Tax 付款日与 VAT 期限保留为人工配置项。

## 明确不做什么

`GET /api/box/tax/workspace` 是 reader 可调用的只读投影。它只会在服务端执行当前 Pack 已注册的确定性日历 read/draft 服务：

- 不接收或返回 UTR、统一社会信用代码、EIN、UEN、BRN、VAT number 等原始标识符；
- 不接收登记回执、申报表、附件或其他证据值；
- 不计算税额，不生成可提交申报文件；
- 不执行申报、付款、登记或任何外部提交；
- 不自动 dispatch 页面上展示的服务请求模板；
- 不把登记类别代码当作登记证据；
- 不把候选日期、`calendar.ready` 或 `filing_assist` 当作 release approval。
- 不保存日期事实预览值，不修改 Box 配置，也不把预览值当作证据确认。
- 不接受浏览器传入的签认文件路径，不返回私有回答、复核理由或证据引用。

工作台的“配置不是登记证据”不是提示语，而是 API 合同：响应中的 `configuration_is_evidence_confirmation` 与 `registration_codes_are_evidence_confirmation` 均固定为 `false`。

## API

```http
GET /api/box/tax/workspace?period_year=2026&as_of=2026-08-13
Authorization: Bearer <reader-token>
```

`period_year` 必须是四位年份，`as_of` 必须为 `YYYY-MM-DD`。省略时使用服务器当日。响应包含顶层控制边界和逐主体记录：

```json
{
  "schema_version": 3,
  "period_year": 2026,
  "as_of": "2026-08-13",
  "summary": {
    "entity_count": 2,
    "jurisdiction_count": 2,
    "calendar_task_count": 5,
    "registration_evidence_required_count": 2,
    "rule_expired_count": 0,
    "applicability_review_attached_count": 0,
    "applicability_review_expired_count": 0,
    "calendar_release_ready_entity_count": 0
  },
  "entities": [],
  "control_boundary": {
    "statutory_books_kept_separate": true,
    "raw_tax_identifiers_requested": false,
    "evidence_values_accepted": false,
    "tax_calculation_performed": false,
    "filing_performed": false,
    "payment_performed": false,
    "external_submission_enabled": false
  }
}
```

### 适用性签认安全投影

工作台把规则来源时效和主体适用性签认显示为两道独立 release gate。页面可复制 `tax-applicability-init`、`review`、`verify` 命令，但真实工作底稿仍在浏览器外完成。`init` 必须声明 `--facts-as-of YYYY-MM-DD`；验证命令使用与页面相同的 `--as-of`，因此回放、CI 和运营观察得到一致生命周期。

服务进程可选配置只读目录：

```bash
OPC_TAX_APPLICABILITY_REVIEW_DIR=/absolute/private/tax-applicability-reviews
OPC_TAX_APPLICABILITY_REGISTRY_RECEIPT=/absolute/private/tax-applicability-registry-receipt.json
```

目录中只识别严格的 `<entity_id>.json` 文件；HTTP query 不能覆盖目录、收据或文件名。服务端拒绝符号链接，以及在 POSIX 上可被 group/other 访问的私有签认文件；目录内出现非预期条目也会关闭 registry release gate。运维人员应使用 `tax-applicability-import` 受控导入，完成全部主体后由独立 controller 使用 `tax-applicability-registry-seal` 生成 `0600` 内容指纹收据，并把目录和收据同时只读挂载。服务端每次用当前 Box、Pack 指纹和观察日验证目录与收据，只返回决定、签认 ID、事实截止日、提醒日、失效日、生命周期及 release gate 布尔值。没有匹配收据时即使目录内容完整也不会释放。缺失显示 `not_attached`，结构、指纹或权限无效显示 `invalid`，但错误只以 SHA-256 摘要返回。原始回答、理由、证据引用和文件路径均不进入响应；收据不是数字签名，也不是申报授权。

一份 `approved-in-scope` 历史决定在 `expired` 后仍保留审计意义，但 `applicability_gate_passed` 会变为 `false`。只有规则来源未过期、适用性签认已批准且未过期时，逐主体 `calendar_release_ready` 才为 `true`；这仍不是申报或付款授权。

### 日期事实安全预览

工作台会从当前主体所选 Tax Pack 的版本化 `rules.json` 派生 `anchor_contracts`。只有规则明确声明、且不是主体配置隐式提供的日期锚点，才会成为可编辑的 `type=date` 输入。例如：

- 爱尔兰 LTD 的 `cro_annual_return_date`；
- 加拿大联邦公司的 `federal_corporation_anniversary_date`；
- 澳大利亚 Proprietary Company 的 `asic_annual_review_date`。

`financial_year_end` 来自法律主体配置，工作台会显示其作用但拒绝通过预览参数覆盖。页面把非空输入按主体放入一次 GET 请求：

```http
GET /api/box/tax/workspace?period_year=2026&as_of=2026-08-13&anchors=<URL-encoded JSON>
```

解码后的 `anchors` 形状如下：

```json
{
  "ie_store": {
    "cro_annual_return_date": "2026-09-30"
  }
}
```

服务端只接受当前主体合同中的已知可编辑字段，每个值只能是一个 `YYYY-MM-DD` 日期；未知主体、未知字段、列表、原始公司/税务标识符和无效日期都会 fail closed。以上示例只会生成 `2026-11-25` 的 CRO 年报候选日，仍需核对官方事实、首份年报例外、ARD 变更、节假日以及人工复核 gate。

输入只存在于页面内存和当前 GET URL，不写入磁盘或数据库。再次不带 `anchors` 请求时，候选日会恢复为缺少配置。响应中的 `anchor_preview.persistent_write_performed`、`anchor_preview.box_configuration_changed`、`anchor_values_persisted` 和 `anchor_values_are_evidence_confirmation` 均为 `false`。

页面始终通过 Pack 顶层 `review_policy` 评估观察日、规则复核日、提醒日和失效日。若具体日历服务还返回自己的兼容性来源检查，界面会作为次级状态展示，但不能覆盖顶层 Pack 生命周期。

## 可编辑服务模板

页面为 Pack 已注册的 `registration_profile`、`evidence_checklist` 与 calendar 服务提供“复制请求模板”。复制动作只写入本地剪贴板。OPC 负责人应先确认主体、Pack 版本、证据范围和有权复核人，再决定是否显式调用：

```http
POST /api/box/services/dispatch
Content-Type: application/json

{
  "service_id": "tax.sg.evidence_checklist",
  "entity_id": "sg_publisher",
  "payload": {
    "as_of": "2026-08-13",
    "provided_evidence": []
  }
}
```

模板故意不包含凭证、税号或证据内容。需要提交证据时，应由后续受控资料工作流保存文件引用、访问权限、复核决定和回执，而不是把敏感原值嵌入 Box 配置或请求模板。

## 扩展新的纳税地区

工作台不硬编码国家。新 Pack 只要提供合法 manifest、版本化规则和可选的 `registration_profile`、`evidence_checklist`、`filing_calendar` / `review_calendar_skeleton` capability，就会被当前运行时按主体发现。创建流程见 [添加纳税地区 Pack](添加纳税地区包.md)。没有对应服务时，页面显示“待补能力”，而不是模拟成功结果。
