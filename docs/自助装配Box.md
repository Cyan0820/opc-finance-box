# 自助装配 Box

Box Builder 把“游戏 / 独立站 / 平台电商 + 纳税国家”变成一个可验证的产品入口。它使用仓库里真实安装的 Pack 目录，不维护第二份硬编码能力清单。

当前安装会生成一份 Starter Catalog：3 个产品样板 × 15 个已安装纳税地区，共 45 个契约校验组合。每个条目都带可编辑简化规格、规格 SHA-256、实际解析的 Pack、允许的集成预设和原始税务成熟度。Workbench 选择业务与国家时直接读取该条目的规格；CLI 可以独立查看同一目录：

```bash
python -m src.cli box-starters
```

无需手写规格时，可直接初始化一个标准工作区：

```bash
python -m src.cli starter-init /absolute/new/my-box \
  --profile dtc \
  --country NL \
  --integration shopify_stripe_xero \
  --name "My DTC Box" \
  --entity-id my_dtc_entity \
  --entity-name "My legal entity (confirm)" \
  --actor founder

python -m src.cli handoff-unpack-verify /absolute/new/my-box
python -m src.cli validate /absolute/new/my-box/box.json
python -m src.cli doctor /absolute/new/my-box/box.json
python -m src.cli deployment-smoke /absolute/new/my-box/box.json
```

如果只是先体验产品，可使用更短的本地试用入口。它会把不可变 Box 和可变运行数据放在两个独立子目录，并在启动前联合重验：

```bash
opc-finance-box trial-init /absolute/new/my-opc-trial \
  --profile dtc --country NL --integration shopify_stripe --actor founder
opc-finance-box trial-verify /absolute/new/my-opc-trial
opc-finance-box trial-onboarding /absolute/new/my-opc-trial
opc-finance-box trial-run /absolute/new/my-opc-trial
```

试用始终使用 demo mode、默认只监听回环地址，不读取 Connector 凭证或访问外部网络。`trial-onboarding` 会把已验证的完整 setup checklist 压缩为五段旅程和命令模板，但不记录完成或推断生产就绪。详见 [五分钟本地试用](五分钟本地试用.md)。

`starter-init` 只接受当前安装 Starter Catalog 中唯一存在的 `profile + country`，并把别名归一化到该 Profile 明确允许的 integration preset。目标必须是尚不存在的绝对目录；工具不覆盖或合并已有内容。生成路径与标准配置 Handoff 相同：54 个私有文件（包括最后写入的收据）和 3 个私有目录，其中 `compiled/` 有 40 个工件。内部生成的确定性 Handoff 字节不会另行落成临时 ZIP，工作区仍可使用 `handoff-unpack-verify` 无源归档重验。

需要多个法律主体时，可以直接组合同一 Profile 的多个国家 Starter：

```bash
python -m src.cli starter-compose /absolute/new/global-dtc-box \
  --profile dtc \
  --entity CN=cn_operations \
  --entity NL=nl_sales \
  --entity US=us_marketplace \
  --entity-name "cn_operations=China operations entity (confirm)" \
  --entity-integration cn_operations=xero \
  --entity-integration nl_sales=shopify_stripe \
  --entity-integration us_marketplace=wise \
  --reporting-currency USD \
  --name "Global DTC OPC" \
  --actor founder

python -m src.cli handoff-unpack-verify /absolute/new/global-dtc-box
python -m src.cli validate /absolute/new/global-dtc-box/box.json
python -m src.cli deployment-smoke /absolute/new/global-dtc-box/box.json
```

`--entity` 可重复 2–20 次，格式为 `COUNTRY` 或 `COUNTRY=entity_id`；若同一国家有多个主体，必须分别提供唯一 `entity_id`。`--entity-name entity_id=名称` 只覆盖已选主体。`--entity-integration entity_id=preset` 可重复，把预设 Connector 明确绑定到一个主体；一旦使用，组合器会为全部已选 Connector 生成完整 `connector_bindings`。当前只有 Shopify / Stripe 每个 Pack 使用一组运行时凭证，因此只能绑定一个主体；多主体全局预设会失败关闭。PayPal / WooCommerce / ShipBob / Amazon Seller、Xero、Wise、Airwallex 可以按环境绑定表支持多个主体，其中前四类会逐主体解析动态密钥别名，并在多主体 Box 中拒绝旧根级凭证回退；所有来源仍建议按实际经营主体显式限定。组合器从每个已安装 Starter 复制该国家的 Tax Pack 与起始会计字段，税务登记仍为空；集成仍受所选 Profile 的白名单约束。所有主体使用同一种本位币时，管理报告币种可自动采用该币种；存在多种本位币时，`--reporting-currency` 必填。

组合成功只表明配置与 Pack 契约可编译、主体边界已生成且工作区完整。它不会生成汇率、抵销分录、集团会计政策或法定合并，也不授权跨币种加总。编译后的多主体月结组合保持 pre-elimination；每个主体仍需独立来源证据、Pipeline attempt、Shadow Close 和复核人。

可选的 `--data-mode live` 只改变 Box 数据模式，不配置任何 Connector 凭证或宣称真实接入完成。Starter 的主体名称、币种、会计准则、财年结束日和空税务登记仍是待确认起点；当地专业复核、Connector Shadow、职责分离和稳定版门禁保持不变。

目录中的 `contract_checked` 只证明该规格能由当前 Pack 契约解析，完整矩阵测试还会逐一执行 preview/compile。它不证明真实主体适用、不表示税务登记完成，也不会把任何条目标记为可直接申报。若以后安装了缺少 Starter 默认字段或依赖不完整的 Pack，对应组合会进入 `unavailable_combinations`，不会伪装成可用样板。

## 使用流程

1. 在工作台左侧进入 **装配 Box**。
2. 选择游戏、独立站或平台电商样板。
3. 为每个法律主体显式选择一个已安装 Tax Pack，并检查本位币、会计准则、财年结束日和主体信息。
4. 分别选择主主体和其他主体的数据源预设；文件导入始终显式绑定全部主体，外部 Connector 只绑定实际服务的主体。
5. 独立站可选择 Shopify、Stripe、PayPal、WooCommerce、Wise、Xero、Airwallex、ShipBob，或已安装的 Shopify + Stripe + 银行 / 会计 / 企业卡组合预设；游戏与平台电商只展示各自 Profile 白名单内的预设。
6. 实时检查“数据源主体范围”。若 Shopify / Stripe 被分配到多个主体，浏览器会在发出 API 请求前失败关闭，因为当前 provider 每个 Pack 只使用一组运行凭证。
7. 点击“生成并校验候选”，检查 Pack、逐主体 Tax 成熟度、精确 Connector 绑定、Pipeline、上线任务与警告。
8. 复制可复现 `starter-init` / `starter-compose` CLI、简化规格或严格 `box.json`。也可点击“下载并校验配置包”：浏览器先核对响应长度，再对实际 ZIP 字节重新计算 SHA-256；只有与服务端固定完整性元数据一致才触发保存。
9. 保存成功后下载 `.browser-receipt.json`，或复制页面生成的校验收据。页面同时给出 `chmod 600`、`handoff-verify`、`handoff-receipt-verify`、`handoff-unpack` 与 `handoff-unpack-verify` 命令。命令里的下载路径、目标绝对路径和 `RECIPIENT` 必须替换；浏览器收据不是数字签名，接收方仍必须运行正式 verifier。
10. 在新私有工作区通过 Handoff 重验后再运行 `validate`、`compile` 和 `doctor`。界面不会执行命令、展开归档或切换当前运行时。

也可以完全绕过浏览器，直接把简化规格导出为确定性 Handoff ZIP：

```bash
opc-finance-box handoff-bundle examples/box_specs/dtc_cn.json \
  --output /absolute/delivery/customer-handoff.zip
opc-finance-box handoff-verify /absolute/delivery/customer-handoff.zip
opc-finance-box handoff-receipt-verify /absolute/delivery/customer-handoff.zip \
  /absolute/delivery/customer-handoff.browser-receipt.json
opc-finance-box handoff-unpack /absolute/delivery/customer-handoff.zip \
  /absolute/new/handoff-workspace --actor HANDOFF_RECIPIENT
opc-finance-box handoff-unpack-verify /absolute/new/handoff-workspace
```

生成命令使用排他创建，不覆盖现有文件；返回 ZIP 的 SHA-256、字节数、runtime fingerprint 和成员计数。交付前及接收后分别运行 `handoff-verify`：它不解压文件，会检查归档大小、私有权限、成员路径与确定性元数据、manifest 完整覆盖、逐成员大小/SHA-256、编译锁 fingerprint，并用当前安装的 Pack 重新生成后逐成员做字节比对。任何额外、缺失、重复、改写或版本不匹配都会失败关闭；同时单独报告压缩归档本身是否与当前 builder 逐字节一致，避免把压缩库差异误当成内容可信。

需要多主体时，可以使用 `starter-compose` 一键生成，也可以在工作台点击“添加主体”，为每个主体分别设置 Tax Pack、数据源、本位币、会计准则和财年结束日。浏览器与 CLI 使用同一合同：Builder 始终为候选生成完整、规范排序的 `connector_bindings`，并自动加入 `feature.multi_entity`；多币种 CLI 组合必须显式选择管理报告币种，界面草稿则必须在保存前确认。每个绑定只能引用当前 Box 主体；运行时会在 handler 或网络调用前拒绝错绑的 `default_entity_id`。Pipeline 模板、Pilot/Production Readiness、Connector Shadow、Activation 和 stable promotion 都消费同一绑定范围。各主体的法定账、税务成熟度、登记证据和 Pipeline 请求起点仍然独立。界面会分别汇总例如 `CN/CNY · SG/SGD` 与 `workpaper / design`，不会用第一个主体的成熟度代表整个 Box。

Builder 不把国家代码当作税务事实。CN 当前是 `workpaper`；AE / AU / CA / DE / FR / GB / HK / IE / JP / KR / NL / NZ / SG / US 当前都是 `design`。界面和输出会保留每个已安装 Pack 的实际成熟度、规则核验日和复核策略，不从国家代码推断申报能力。默认本位币和会计准则只是起点，税务登记默认空数组，必须依据真实法律主体、登记凭证和当地专业复核补齐。

## API

```text
GET  /api/box-builder/options
POST /api/box-builder/preview
POST /api/box-builder/bundle
```

`options` 只列出本次安装中依赖完整的产品样板和 Tax Pack，并返回相同的 `starter_catalog`。它还返回 `connector_binding_policy`，包含默认文件 Connector、必须完整覆盖的显式绑定规则，以及当前单凭证 Connector Pack 列表；前端不硬编码 Shopify / Stripe 的限制。`handoff_download_policy` schema v2 另行声明 SHA-256 算法、固定响应头、收据 schema/文件名后缀、正式 verifier 和 `0600` 私有权限要求；任何缺失或不一致都会阻止下载，它不把响应头或收据当成数字签名。`preview` body 使用与 `box/box-spec.schema.json` 相同的简化规格：

```json
{
  "name": "我的美国独立站 OPC",
  "business_type": "commerce",
  "channels": ["dtc"],
  "integrations": ["shopify_stripe"],
  "data_mode": "demo",
  "reporting_currency": "USD",
  "entities": [
    {
      "id": "us_store",
      "name": "美国经营主体候选",
      "tax_country": "US",
      "tax_pack": "jurisdiction.us_federal",
      "functional_currency": "USD",
      "accounting_basis": "US_GAAP",
      "fiscal_year_end": "12-31",
      "tax_registrations": []
    }
  ],
  "connector_bindings": [
    {"connector_pack": "connector.file_import", "entity_ids": ["us_store"]},
    {"connector_pack": "connector.shopify", "entity_ids": ["us_store"]},
    {"connector_pack": "connector.stripe", "entity_ids": ["us_store"]}
  ]
}
```

响应包含简化规格、严格配置、主体与 Pack 摘要、规则核验生命周期、真实可执行 Pipeline、请求起点数量、上线任务、release gates、stable promotion policy 和稳定 runtime fingerprint。相同 Pack 内容与相同规格会产生相同 fingerprint。

预检通过后可通过 HTTP 或 `handoff-bundle` CLI 下载完整配置 Handoff Bundle。HTTP 响应同时返回 `X-OPC-Handoff-SHA256`、`X-OPC-Runtime-Fingerprint`、`X-OPC-Manifest-Schema` 和 `X-OPC-Manifest-File-Count`；浏览器必须先验证这些字段格式、`Content-Length` 与实际 Blob 大小，再用 WebCrypto 对 Blob 重新计算 SHA-256。任何头缺失、格式错误、长度不符、浏览器缺少 WebCrypto 或摘要不符都会失败关闭且不创建下载链接。通过后可把同一组安全字段保存为 `.browser-receipt.json`。`handoff-receipt-verify` 会先执行正式 Handoff 可复现验证，再逐项绑定文件名、长度、SHA、运行指纹和 manifest；它只证明收据描述了该 ZIP，不能证明某个浏览器确实执行过 WebCrypto、不能防御恶意服务端，也不是代码签名。

ZIP 包含 `box-spec.json`、严格 `box.json`、`setup-checklist.json`、`HANDOFF.md`、首客私有接入用的 `ACTIVATION.md`、`bundle-manifest.json`，`compiled/` 下的数据模型、Agent prompts、服务/Connector/Pipeline 目录、请求模板、运行策略、部署环境契约、版本化运行数据与恢复契约、地区规则、release gates 以及 stable 晋级证据模板/schema，还包含经过安全控制检查的 `deployment/` Docker/systemd 模板。manifest 记录除自身外每个成员的路径、字节数和 SHA-256；相同规格与 Pack 生成的 ZIP 字节完全一致，便于进入版本控制和供应链校验。

Handoff 默认以 `0600` 写入；验证器在 POSIX 上拒绝 group/other 可访问文件，并拒绝符号链接、绝对路径、`..`、反斜杠、重复成员、加密成员、非预期压缩方式和 zip bomb 式体积。验证结果只返回摘要、整包 SHA-256 和 runtime fingerprint，不返回规格内容或内部路径，也不会写出或执行归档成员。

需要落地时再运行 `handoff-unpack`。它只写入一个全新的 `0700` 工作区，以 `0600` 保存成员并最后生成非签名收据；中断目录没有收据且不能复用。`handoff-unpack-verify` 不依赖源 ZIP，会重新扫描权限、文件集合、哈希、编译锁并按当前 Pack 重建全部成员。详见 [Handoff 接收与安全展开](Handoff接收与安全展开.md)。

该 ZIP 是 Box 配置与部署 handoff，不重复打包整套产品源码。Dockerfile 需要完整 starter repo 作为 build context：将 Bundle 解压到已 clone 的 OPC Finance Box repo，或安装通过 `distribution-verify` 的 wheel 后使用 systemd 模板。不要在只有 `compiled/` 的目录中把 Dockerfile 当成独立镜像源码。

上线任务会进一步投影为五阶段诊断清单：保护运行入口、确认主体与税务边界、连接并影子核对数据源、配置职责分离与有权人、接受预览边界与升级计划。每项任务都包含 blocking/required/advisory 级别、负责人角色和完成证据要求。Connector 任务只展示需要配置的环境变量名称，不读取或返回变量值。

## 控制边界

- preview 不替换服务器当前 `BOX_RUNTIME`。
- preview 不持久化候选文件，不安装定时任务，也不访问 Connector。
- preview 不包含凭证或 secret；请求上限为 256 KiB、单个候选最多 20 个法律主体。
- preview 与 bundle 都拒绝规格契约外字段，以及 key 名中含 secret、token、password、api key、credential 或 authorization 的字段。
- 浏览器下载必须先通过实际响应字节 SHA-256、长度、runtime fingerprint 与 manifest 元数据校验；失败时不保存文件、不保留旧收据，也不生成接收命令。
- 浏览器校验收据不是服务端签名或安装收据；下载文件通常不是 owner-private，接收方先按页面命令对 ZIP 和 JSON 运行 `chmod 600`，再运行 `handoff-verify` 与 `handoff-receipt-verify`，安全展开后运行 `handoff-unpack-verify`。
- `handoff-verify` 只读取并验证归档；不解压、不更改 active runtime、不运行 Connector，也不把 manifest 自述当成充分证据。
- `handoff-unpack` 不覆盖现有目录、不删除源包、不执行成员；本地收据不是数字签名或财务证据。
- 复制配置不是上线。保存后仍需显式运行校验、编译、`tax-rule-status`、doctor、fixture/shadow reconciliation 和当地税务复核。
- 多主体 Box 会自动启用主体隔离 Feature，但管理合并视图仍不能混用法定账、税务登记或银行证据。
