# API 身份与职责分离

内置工作台仍是 local-first：默认只绑定 `127.0.0.1`，没有认证配置时仅适合受信任的本机单用户。需要让 Codex、自动化任务或另一位复核人通过 API 访问时，应启用角色策略。

## 创建 token 指纹

为每个 principal 生成独立、随机、至少 32 个可打印 ASCII 字符的 token。原始 token 只交给该 principal，并放在 secret manager；策略文件只保存 SHA-256 指纹。

```bash
export OPC_FINANCE_TOKEN_TO_HASH='REPLACE_WITH_A_RANDOM_SECRET_OF_AT_LEAST_32_CHARS'
python3 -m src.cli auth-token-hash
unset OPC_FINANCE_TOKEN_TO_HASH
```

创建例如 `/absolute/private/path/api-auth.json`：

```json
{
  "schema_version": 1,
  "principals": [
    {
      "principal_id": "finance_reader",
      "token_sha256": "REPLACE_WITH_64_LOWERCASE_HEX",
      "roles": ["reader"]
    },
    {
      "principal_id": "pipeline_bot",
      "token_sha256": "REPLACE_WITH_ANOTHER_64_LOWERCASE_HEX",
      "roles": ["operator"]
    },
    {
      "principal_id": "independent_reviewer",
      "token_sha256": "REPLACE_WITH_A_THIRD_64_LOWERCASE_HEX",
      "roles": ["reviewer"]
    }
  ]
}
```

```bash
chmod 600 /absolute/private/path/api-auth.json
python3 -m src.cli auth-policy-validate /absolute/private/path/api-auth.json

OPC_FINANCE_API_AUTH_FILE=/absolute/private/path/api-auth.json \
opc-finance-workbench
```

策略拒绝重复 principal、重复 token 指纹、未知角色、宽松文件权限、超过 256 KiB 的文件和明文 token 字段。`box/api-auth.schema.json` 是可 fork 的结构契约。

## 角色边界

| 角色 | 默认权限 |
|---|---|
| `reader` | GET/HEAD 财务 API，包括 Box、运行历史、待复核队列和完整性证明 |
| `operator` | reader + Service/Pipeline dispatch + 执行并记录 Pipeline |
| `reviewer` | reader + 追加 Pipeline review 决定 |
| `admin` | 上述全部 + 工作台其他写操作 |

`operator` 不隐含 `reviewer`，`reviewer` 也不隐含 `operator`。因此可以把执行 token 和复核 token 交给不同人员或 Agent。`admin` 是显式越权角色，不应作为普通自动化 token。

请求使用：

```text
Authorization: Bearer <raw token from secret manager>
```

`GET /api/auth/whoami` 返回当前 principal 和角色，不返回 token 或指纹。开启策略后，JSON 请求体中的 `actor` 不能覆盖认证 principal；Pipeline attempt、schedule claim 和 review 事件都会以 principal ID 留痕。`GET /api/box/pipeline-schedule`、`GET /api/box/pipeline-observability` 与 `GET /api/box/connector-sync` 属于 reader 只读状态，`POST /api/box/pipeline-schedule/run` 需要 operator；调度 reviewer 仍不能借此执行，operator 也不能审批自己的 Pipeline review gate。Connector sync 的 run、commit、backfill 和 quarantine resolution 故意不暴露为 HTTP API，只能在受控主机通过 CLI 执行。

## 兼容与部署边界

`OPC_FINANCE_API_TOKEN` 仍可提供单一 `legacy_api_admin` token，主要用于向后兼容。它和 `OPC_FINANCE_API_AUTH_FILE` 不能同时配置。多角色场景应使用后者。

非回环 `OPC_FINANCE_HOST` 在没有有效 token/策略时拒绝启动。Bearer 认证仍不替代 TLS、反向代理、防火墙、速率限制、SSO、组织级角色治理或外部不可篡改审计；内置服务器不应直接暴露到公网。
