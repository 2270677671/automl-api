# OIDC/OAuth2 Client Credentials 接入手册

## 1. 交付结果

`compose.oidc.yaml` 为单节点生产部署增加独立 Keycloak 身份服务、PostgreSQL 身份数据库和
HTTPS 身份网关。Agent 平台不再接收人工签发的一小时 JWT，而是持有独立的
`client_id/client_secret`，通过标准 OAuth2 `client_credentials` grant 自动取得五分钟 access
token。AutoML API 只从 Keycloak JWKS 验证签名、issuer、audience、tenant、actor type 和 operation
scopes，不保存第三方密码。

当前生产入口约定：

| 用途 | 地址 |
| --- | --- |
| AutoML API | `https://192.168.194.67:8443` |
| OIDC discovery | `https://192.168.194.67:9443/realms/automl/.well-known/openid-configuration` |
| OAuth2 token | `https://192.168.194.67:9443/realms/automl/protocol/openid-connect/token` |
| JWKS | `https://192.168.194.67:9443/realms/automl/protocol/openid-connect/certs` |

身份网关只发布 discovery、token 和 JWKS 三个端点。Keycloak 管理端、管理 realm、数据库端口和
健康端点不对外发布。

## 2. 部署初始化

首次部署时生成独立的 API、OIDC、数据库和管理凭据：

```bash
./scripts/init_single_node_production.sh \
  192.168.194.67 \
  192.168.194.67
```

在 `.env.production-single` 中确认：

```dotenv
AUTOML_OIDC_PUBLIC_HOST=192.168.194.67
AUTOML_OIDC_HTTPS_BIND_ADDRESS=192.168.194.67
AUTOML_OIDC_HTTPS_PORT=9443
AUTOML_OIDC_REALM=automl
AUTOML_OIDC_AGENT_CLIENT_ID=automl-agent-platform
AUTOML_OIDC_AGENT_TENANT_ID=partner_a
AUTOML_GATEWAY_GOMAXPROCS=2
```

以下值必须由初始化脚本生成并保持非空，不能提交到 Git：

```text
AUTOML_OIDC_AGENT_CLIENT_SECRET
AUTOML_OIDC_ADMIN_PASSWORD
AUTOML_OIDC_DB_PASSWORD
```

已经在运行、环境文件中尚无 `AUTOML_OIDC_*` 的单机部署，使用原地迁移脚本；它会
原子更新环境文件、保持 `0600`、生成三个独立凭据，且不输出凭据值：

```bash
./scripts/enable_oidc_single_node.sh \
  .env.production-single \
  .automl-production \
  192.168.194.67 \
  192.168.194.67 \
  partner_a
```

如果环境文件已包含任何 `AUTOML_OIDC_*` 配置，脚本会拒绝修改，避免误轮换正在使用的
client secret 或身份数据库凭据。

`AUTOML_GATEWAY_GOMAXPROCS` 应与 Caddy 的 CPU 限额匹配。默认值 `2` 会限制长期运行时的
Go worker 线程数量，避免网关耗尽 `AUTOML_GATEWAY_PIDS_LIMIT` 后无法执行健康检查。

启动 CPU 单节点：

```bash
docker compose --env-file .env.production-single \
  -f compose.production-single.yaml \
  -f compose.oidc.yaml \
  up -d --wait
```

Keycloak 和 PostgreSQL 均通过预构建的本地标签运行；对应 Dockerfile 的 `FROM` 使用国内代理并
固定 digest。如果 Docker daemon 的直接 pull 不稳定，可先显式通过 BuildKit 构建两个本地镜像：

```bash
docker compose --env-file .env.production-single \
  -f compose.production-single.yaml \
  -f compose.oidc.yaml \
  build automl-identity-db automl-identity
```

GPU、双 IP 部署追加现有 overlay：

```bash
docker compose --env-file .env.production-single \
  -f compose.production-single.yaml \
  -f compose.oidc.yaml \
  -f compose.gpu.yaml \
  -f compose.dual-ip.yaml \
  up -d --wait
```

OIDC issuer 固定使用主入口 `192.168.194.67:9443`。同一 access token 可以调用两个 AutoML API
入口，但 Agent 平台始终从主身份入口取 token，避免一个部署出现多个 issuer。

## 3. 给 Agent 平台交付什么

每个合作方应获得独立 tenant 和 OAuth client。交付包只包含：

```dotenv
AUTOML_API_URL=https://192.168.194.67:8443
AUTOML_OIDC_TOKEN_URL=https://192.168.194.67:9443/realms/automl/protocol/openid-connect/token
AUTOML_OIDC_CLIENT_ID=automl-agent-platform
AUTOML_OIDC_CLIENT_SECRET=<从密钥管理系统安全交付>
AUTOML_CA_FILE=/secure/path/automl-root.crt
```

不得向合作方交付 Keycloak admin 密码、数据库密码、JWT 私钥、Caddy 私钥或其他 tenant 的
client secret。client secret 只能进入 Agent 平台的 secret manager 和 tool executor，不能进入
LLM Prompt、记忆、trace、日志或前端。

## 4. 原始 HTTP 获取 token

```bash
TOKEN_RESPONSE=$(
  {
    printf 'user = "%s:%s"\n' \
      "$AUTOML_OIDC_CLIENT_ID" "$AUTOML_OIDC_CLIENT_SECRET"
    printf 'url = "%s"\n' "$AUTOML_OIDC_TOKEN_URL"
  } | curl --cacert "$AUTOML_CA_FILE" -fsS \
    --config - \
    -H 'Content-Type: application/x-www-form-urlencoded' \
    --data 'grant_type=client_credentials'
)

export AUTOML_TOKEN=$(printf '%s' "$TOKEN_RESPONSE" | jq -er '.access_token')
printf '%s' "$TOKEN_RESPONSE" | jq '{token_type, expires_in, scope}'
unset TOKEN_RESPONSE
```

生成器产生的 client ID/secret 只包含安全的字母数字字符；这里通过 curl 标准输入传递 Basic
凭据，避免 secret 出现在进程参数列表。不要输出 `.access_token`。使用 token 调用 AutoML API：

```bash
curl --cacert "$AUTOML_CA_FILE" -fsS \
  -H "Authorization: Bearer $AUTOML_TOKEN" \
  "$AUTOML_API_URL/v1/agent/manifest" | jq .service_version
```

Keycloak `client_credentials` 默认不返回 refresh token。Agent 平台应缓存 access token，并在到期前
重新调用 token endpoint；不能等待业务请求收到 `401` 后才刷新。

## 5. Python SDK 自动获取和续期

```python
import os
import ssl

import httpx

from automl_sdk import AutoMLClient, OAuth2ClientCredentialsTokenProvider

tls = ssl.create_default_context(cafile=os.environ["AUTOML_CA_FILE"])

with (
    OAuth2ClientCredentialsTokenProvider(
        os.environ["AUTOML_OIDC_TOKEN_URL"],
        client_id=os.environ["AUTOML_OIDC_CLIENT_ID"],
        client_secret=lambda: os.environ["AUTOML_OIDC_CLIENT_SECRET"],
        verify=tls,
    ) as tokens,
    httpx.Client(verify=tls, timeout=30) as http,
    AutoMLClient(
        os.environ["AUTOML_API_URL"],
        token=tokens,
        http_client=http,
    ) as api,
):
    manifest = api.get_agent_manifest()
    print(manifest["service_version"])
```

Provider 会并发串行化 token 获取、在过期前 30 秒刷新，并允许 secret manager 通过 callable 提供
轮换后的 client secret。可直接运行：

```bash
PYTHONPATH=packages/python_sdk/src \
python examples/python/oauth_client_credentials.py
```

## 6. JWT 中的授权边界

默认 Agent client 的 access token 包含：

- `aud=managed-automl-api`；
- `tenant_id=partner_a`，实际部署应改成合作方唯一 tenant；
- `actor_type=agent`；
- 上传、Run、事件、Output、DecisionPacket 读取和 artifact 下载所需的精确 operation scopes；
- 不包含 `decideApproval`，也不能绕过 `HUMAN_REQUIRED`。

`client_credentials` 代表机器主体，不代表人。Human 决策必须由组织身份平台的交互式登录、授权码
加 PKCE 或受审计的 token exchange 产生 `actor_type=human` token。不得创建一个共享 human
client secret 让 Agent 自动冒充人工。

## 7. 密钥轮换

轮换前先让 Agent 平台 secret manager 支持新旧 secret 的有界切换窗口。Keycloak 管理端不对外
发布，部署管理员从容器内部执行管理命令。重新生成 client secret 的命令必须在受控服务器终端运行，
输出直接写入密钥管理系统，不得发到聊天、issue 或普通日志。

轮换后：

1. 更新 Agent 平台 secret manager。
2. 调用 token endpoint 验证新 secret。
3. 等待旧 access token 最长五分钟自然过期。
4. 更新受保护的灾难恢复配置，确保重建身份数据库时不会恢复旧 secret。

## 8. 备份和恢复

`automl-identity-backup` 每天使用 `pg_dump` 生成：

```text
<AUTOML_BACKUP_HOST_DIR>/identity/keycloak-YYYYMMDDTHHMMSSZ.sql.gz
```

备份包含 client、claim mapper 和 Keycloak 签名密钥，应按凭据材料加密、异地保存并限制访问。
恢复演练必须验证：discovery、token、JWKS、JWT `kid` 和 AutoML manifest 调用完整通过。只备份
AutoML SQLite 而不备份身份数据库，不构成完整灾备。

## 9. 验收

```bash
curl --cacert "$AUTOML_CA_FILE" -fsS \
  "https://192.168.194.67:9443/realms/automl/.well-known/openid-configuration" | jq .issuer

curl --cacert "$AUTOML_CA_FILE" -fsS \
  "https://192.168.194.67:9443/realms/automl/protocol/openid-connect/certs" | jq '.keys | length'
```

最终验收必须证明：错误 secret 返回 HTTP 401 OAuth client error；正确 secret 返回五分钟 Bearer token；
token 的 issuer/audience/tenant/actor/scopes 正确；过期 token 返回 `401`；缺少 operation scope 返回
`403`；数据库、Keycloak admin 和 management 端点无法从外部网络访问。

Keycloak 26 对错误 secret 可能返回 `invalid_client` 或 `unauthorized_client`，两者都必须是 HTTP
401，且错误描述不能泄露凭据。可使用仓库内的安全验收脚本一次完成 discovery、错误 secret、
RS256/JWKS、claims、管理路由隔离和 AutoML manifest 检查；脚本不会输出 access token 或 client
secret：

```bash
export AUTOML_API_URL=https://192.168.194.67:8443
export AUTOML_OIDC_ISSUER=https://192.168.194.67:9443/realms/automl
export AUTOML_OIDC_CLIENT_ID=automl-agent-platform
export AUTOML_OIDC_CLIENT_SECRET='<从 secret manager 注入>'
export AUTOML_OIDC_EXPECTED_TENANT_ID=partner_a
export AUTOML_CA_FILE=.automl-production/caddy-data/caddy/pki/authorities/local/root.crt

uv run python scripts/verify_oidc_deployment.py
```
