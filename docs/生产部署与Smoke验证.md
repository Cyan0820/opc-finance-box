# 生产部署与 Smoke 验证

本仓库提供的是**可 fork 的单节点部署起点**，不是托管式财务 SaaS。部署模板把运行用户、配置、持久卷、认证、调度和健康检查边界显式化，但不会替代组织 SSO、TLS/WAF、数据库高可用、集中密钥管理、不可变审计存储或属地财务专业复核。

## 1. 先编译部署契约

```bash
opc-finance-box compile BOX.json --output build/box
```

编译目录中的 `deployment-environment-contract.json` 是当前 Box 的机器可读环境契约，包含：

- 核心路径、监听地址、认证和调度环境变量；
- 当前已选 Connector 实际需要的 secret **名称**，不含值；
- 只读/读写挂载、备份范围和临时存储边界；
- liveness、readiness、Prometheus 路径；
- 单节点部署声明和仍未覆盖的生产能力。

环境契约随 Box runtime fingerprint 编译。更换 Pack、主体或 Connector 后应重新生成并进入版本评审，不能把另一 Box 的环境文件直接复用。

## 2. 验证部署模板

仓库及 wheel 都包含 `deployment/`：

- `Dockerfile`：多阶段构建、非 root UID/GID 10001、healthcheck；配套 `Dockerfile.dockerignore` 明确排除 runtime data、auth/schedule、私有部署目录和构建产物，只放行两个打包所需的演示 JSON；
- `compose.example.yaml`：宿主机仅发布到 `127.0.0.1`、只读根文件系统、移除 capabilities、要求 role policy secret；
- `opc-finance-workbench.service`：systemd 常驻工作台；
- `opc-finance-scheduler.service/.timer`：每五分钟检查到期 Pipeline，实际 cadence 仍由 schema v2 计划控制；
- `box.env.example`：只放路径和非 secret 运行参数。

在部署前运行内置静态控制检查：

```bash
opc-finance-box deployment-assets-verify deployment
```

检查器会阻止常驻 root workbench、缺失认证引用、缺失 systemd hardening、host network、Docker socket 或占位 raw token 等不安全默认值。它不等于真实容器构建；发布环境仍需执行镜像构建、漏洞扫描、SBOM、签名及目标平台启动验证。当前 Dockerfile 构建时仍从配置的 Python index 解析受版本范围约束的依赖；正式发布应使用组织内镜像/包仓、带哈希 lock 和可复现构建，而不是把示例镜像视为已完成供应链加固。

## 3. 生成 role policy

不要把 raw token 写入 Compose、环境样例或 Git。为每个 principal 在安全终端临时生成 SHA-256：

```bash
export OPC_FINANCE_TOKEN_TO_HASH='A_RANDOM_SECRET_WITH_AT_LEAST_32_CHARACTERS'
opc-finance-box auth-token-hash
unset OPC_FINANCE_TOKEN_TO_HASH
```

把摘要写入 `api-auth.json`，分别授予 reader/operator/reviewer/admin。容器示例先由一次性 `auth-init` 以 root 读取 Compose secret，再复制成 UID 10001、权限 `0600` 的独立只读 auth volume；常驻 workbench 始终以 UID 10001 运行。systemd 示例从 `/etc/opc-finance/api-auth.json` 只读加载。远程入口仍必须由反向代理终止 TLS，并配置网络访问控制和速率限制。

## 4. 容器起点

```bash
mkdir -p deployment/private
cp BOX.json deployment/private/box.json
cp /secure/generated/api-auth.json deployment/private/api-auth.json
docker compose -f deployment/compose.example.yaml build
docker compose -f deployment/compose.example.yaml up -d
```

Compose 内部监听 `0.0.0.0`，因此 role policy 缺失或无效时进程会拒绝启动；宿主机端口仍只发布到 `127.0.0.1`。如需远程访问，由受控反向代理连接该回环端口。`opc_finance_data` 是唯一默认读写卷，应纳入加密备份与空目标恢复演练。

如果启用税务适用性运行时门，必须同时只读挂载完整的 `<entity_id>.json` 轮换目录和 `tax-applicability-registry-seal` 生成的 `0600` 收据，并同时设置 `OPC_TAX_APPLICABILITY_REVIEW_DIR` 与 `OPC_TAX_APPLICABILITY_REGISTRY_RECEIPT`。只有目录内容、Box 指纹、职责分离和收据均匹配，日历 release gate 才会打开；只配置目录会保留安全状态展示，但不会释放。收据不是数字签名，也不授予申报或付款权限。

网络 Connector 形成真实并行证据后，把当前 schema v2 reviewed assessment 放入一个 `0700` 私有轮换目录，文件保持 `0600`，并设置 `OPC_CONNECTOR_SHADOW_REVIEW_DIR`。`GET /api/box/connector-shadow` 会重新验证 Box 指纹、真实匿名分类、独立 `passed` 复核、30 天时效、重复 scope 和所选网络 Connector Pack 覆盖；目录、文件名、人员、证据引用、源控制和金额都不会返回。该门只证明 Pack 级当前证据存在，不替代稳定版逐主体/逐期间晋级评估。

首家 OPC 运行时依次只读挂载 `OPC_PILOT_READINESS_REVIEW`、`OPC_PILOT_DATA_HANDOFF_REVIEW` 和 `OPC_PILOT_SHADOW_RUN_REGISTRATION`。最后一份登记还必须与 `/var/lib/opc-finance/pipeline_runs` 的当前防篡改台账一致；容器示例把三份 `0600` 工件挂为 `:ro`，而台账仍位于持久卷。少任一配对工件、Box/主体/期间不符、Pilot 过期、当前 gate 被驳回或台账链损坏都会关闭首次受控观察门。该门不授权过账、付款、法定关账或申报。

首次观察完成后，再同时只读挂载 `OPC_PILOT_SHADOW_OBSERVATION_REVIEW` 与 `OPC_PILOT_SHADOW_ENTITY_REPORT_DIR`；多主体 Box 还必须挂载 `OPC_PILOT_SHADOW_PORTFOLIO_REVIEW`。报告目录只能包含当前 Box 每个主体恰好一个 `<entity_id>.json`，内容权限为 `0600`，不得含多余文件或符号链接。`GET /api/box/pilot-shadow-observation` 会重新核验整条前序链和源内容，但只返回安全计数与布尔门禁；放行仅适用于下一 Shadow 期间，不表示 stable 晋级、过账、付款、关账或申报获批。

积累至少两个连续月份后，可再只读挂载 `OPC_PILOT_SHADOW_SERIES_REVIEW` 与 `OPC_PILOT_SHADOW_SERIES_EVIDENCE_ROOT`。证据根目录只能包含 2–24 个连续 `YYYY-MM` 目录，每期必须使用文档规定的精确私有布局。`GET /api/box/pilot-shadow-series` 会重新验证每期源文件和当前 Pipeline 台账，只返回期间、计数和晋级证据准备门；它不直接运行 stable 晋级、不返回路径、角色、哈希或金额。

需要在 Workbench 恢复正在处理的后续月份时，可额外把完整 Activation Workspace 以只读方式挂载为 `OPC_ACTIVATION_WORKSPACE_ROOT`。`GET /api/box/pilot-shadow-periods` 只接受服务端挂载，逐月验证月度工作区与 Runbook，并返回无命令、无路径、无哈希的安全任务队列：当前任务、负责/独立复核角色类型、证据类别、预期工作产出、操作检查清单、暂停条件和报告状态。方法指导使用稳定类型 ID，不包含命令或私有值；空账本不会因读取而创建锁或文件，已有账本使用共享读锁。角色类型不是具名授权，检查清单也不是权威完成状态；该接口不提供执行或签认入口。

完成 `promotion-record` 与独立 `promotion-review` 后，可把 promotion ledger 目录另行只读挂载为 `OPC_STABLE_PROMOTION_ROOT`。`GET /api/box/production-readiness` 只读验证目录权限、ledger/lock、hash chain、当前 Box fingerprint 以及每个已选 Pack 的当前版本和最新复核状态；不会创建 lock、写事件或修改 Pack manifest。生产写入晋级账本与 Workbench 只读投影应使用分离的运行权限。

首家真实 OPC 开始受限 Shadow Close 前，还要把 `pilot-readiness-review` 生成的 `0600` 签认文件只读挂载，并设置 `OPC_PILOT_READINESS_REVIEW=/absolute/private/pilot-readiness-reviewed.json`。文件必须匹配当前 Box 指纹、全部主体、行业资料域和网络 Connector 范围；第 60 天开始提示复核，超过第 90 天后关闭新的受限 Shadow Run。`GET /api/box/pilot-readiness` 只投影安全摘要，签认本身不授权过账、付款、法定关账或申报。

启用调度时，另行挂载 schema v2 `schedule.json` 及同目录 request 文件，并解除 Compose 中对应注释。容器示例只运行工作台；周期任务应由宿主/编排平台以独立 operator principal 调用，避免把常驻 Web 进程兼作 scheduler。

## 5. systemd 起点

1. 建立固定低权限用户 `opc-finance`，创建 `/var/lib/opc-finance`，权限 `0700`。
2. 在 `/opt/opc-finance/venv` 安装经 `distribution-verify` 通过的 wheel。
3. 对 `/var/lib/opc-finance` 执行 `runtime-data-upgrade-preflight`；新目录由 systemd 的 `ExecStartPre` 初始化，遗留目录必须先离线备份再显式接管。
   若报告 `offline_migration_required`，必须保持 workbench 与 scheduler 停止，创建并验证完整备份，再执行 `runtime-data-migrate`；不要直接修改 manifest。
4. 将 Box、role policy、可选 Pilot 签认和 schedule/request 放入 `/etc/opc-finance`。`api-auth.json` 与 Pilot 签认必须由受控用户持有且权限 `0600`；运行时只读加载。systemd 的 `ProtectSystem=strict` 再把 `/etc` 对服务进程设为只读。其余配置可由 root 管理并只读授权服务用户。
5. 复制 `box.env.example` 为 `/etc/opc-finance/box.env`，权限建议 `0640 root:opc-finance`；Connector secret 由 systemd credentials 或 Secret Manager 注入，不写入该文件。
6. 安装 service/timer 后执行 daemon-reload、启动 workbench，再按需启用 scheduler timer。

模板使用 `ProtectSystem=strict`、`ProtectHome=yes`、`NoNewPrivileges=yes`、`UMask=0077`，只允许写 `/var/lib/opc-finance`。目标发行版的 systemd 版本和 hardening 支持仍需在部署主机验证。

## 6. 启动真实进程的隔离 Smoke

```bash
opc-finance-box --packs PACKS_ROOT deployment-smoke BOX.json
```

验证器不是 mock：它会用当前 Python 环境启动真正的 `src.server`/workbench 子进程，但强制满足：

- 只绑定临时 loopback 端口；
- 使用全新的临时 runtime data，结束后删除；
- 不继承 Connector secret、代理或父进程认证变量；临时生成的 role-policy token 只存在于验证器内存，不返回；
- 不加载 schedule、不 dispatch Connector、不执行外部动作；
- 验证 `/api/health`、无 token 为 401、reader 可读、reader 调 operator 路由为 403、operator 能进入受控路由但因未配置 schedule 得到 409、预期 Box fingerprint、只读 observability 及安全响应头；
- 成功或失败都终止子进程，超时后强制清理。

Smoke 的 `temporary_role_policy_on_loopback` 会证明内置认证和 reader/operator 分权，但不会读取或证明生产 role policy，也不验证 TLS 或反向代理。目标环境仍应使用真实 reader token 验证 `/api/box` 和 metrics，并分别确认无 token 为 401、错误角色为 403。

## 7. 发布与回滚门

建议发布流水线依次执行：

1. `pack-audit`、finance boundary eval、全量测试；
2. 编译 Box 并复核 runtime fingerprint、环境契约、runtime data contract 与 Connector sync policy；
3. `deployment-assets-verify`；
4. 构建 wheel 并执行 `distribution-verify`；
5. 对 wheel 安装环境执行 `deployment-smoke`；
6. 构建/扫描/签名容器，在目标主机通过 runtime data preflight，并验证 role policy、卷权限、离线备份和空目标恢复；
7. Shadow Close 与人工复核通过后，才批准新的 schedule 或 Connector live scope。

回滚时使用上一份已签名 wheel、Box 配置和 Pack lock。Pipeline ledger 不能覆盖或向旧版本强行合并；升级前先备份并验证，恢复只进入空目标。任何回滚都不能撤销已经发生的银行、税务或外部系统动作，这些系统必须单独对账。
