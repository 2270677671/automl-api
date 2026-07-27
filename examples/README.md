# Managed AutoML API 可运行案例

本目录提供不含真实凭据和用户数据的最小可运行案例。默认连接本机开发服务
`http://127.0.0.1:8000`，使用合成样例数据和 sklearn。生产环境通过环境变量传入 HTTPS、CA、
Agent token 和 Human token。

## 1. 文件说明

| 路径 | 用途 |
| --- | --- |
| `python/sdk_guided_workflow.py` | 使用官方 Python SDK 完成上传、中断、回答、结果和 artifact |
| `python/http_guided_workflow.py` | 只使用 HTTP 客户端逐步调用原始 API 路由 |
| `data/customer_churn.csv` | 合成二分类数据，不含真实个人信息 |
| `data/regression.csv` | 合成回归数据 |
| `requests/sklearn-guided.json` | sklearn 人工确认案例请求体 |
| `requests/autogluon-binary.json` | AutoGluon 二分类请求体 |
| `requests/tabpfn-regression.json` | TabPFN 回归请求体 |

## 2. 启动本地 API

从仓库根目录执行：

```bash
uv sync --extra dev
uv run automl-api
```

另开终端运行 SDK 案例：

```bash
AUTOML_API_URL=http://127.0.0.1:8000 \
AUTOML_TOKEN=local-development-token \
PYTHONPATH=packages/python_sdk/src \
uv run python examples/python/sdk_guided_workflow.py
```

运行原始 HTTP 案例：

```bash
AUTOML_API_URL=http://127.0.0.1:8000 \
AUTOML_TOKEN=local-development-token \
uv run python examples/python/http_guided_workflow.py
```

本地开发 profile 以 token 哈希作为合成 tenant，因此 Agent 和 Human 应使用同一个 token。输出写入
`example-output/`。

## 3. 生产 HTTPS 案例

生产 Agent token 应通过 OAuth2 `client_credentials` 自动获取，而不是等待人工发放。验证身份链路：

```bash
export AUTOML_API_URL=https://automl.internal.example.com:8443
export AUTOML_OIDC_TOKEN_URL=https://identity.internal.example.com/realms/automl/protocol/openid-connect/token
export AUTOML_OIDC_CLIENT_ID=automl-agent-platform
export AUTOML_OIDC_CLIENT_SECRET='<secret-manager-value>'
export AUTOML_CA_FILE=/secure/path/automl-root.crt

PYTHONPATH=packages/python_sdk/src \
uv run python examples/python/oauth_client_credentials.py
```

完整 workflow 还需要同 tenant 的 Human token。Agent token 负责上传、创建和读取；Human token 只
负责回答 `HUMAN_REQUIRED` packet 和查询 command。

```bash
export AUTOML_API_URL=https://automl.internal.example.com:8443
export AUTOML_CA_FILE=/secure/path/automl-root.crt
export AUTOML_TOKEN='<short-lived-agent-jwt>'
export AUTOML_HUMAN_TOKEN='<short-lived-human-jwt>'

PYTHONPATH=packages/python_sdk/src \
uv run python examples/python/sdk_guided_workflow.py
```

不要把 token 或 client secret 写入命令参数、脚本、`.env.example`、Git、Prompt、trace 或 issue。
自动取 token 见 [OIDC 接入手册](../docs/oidc-client-credentials.md)，Human token 规则见
[完整复现指南](../docs/reproduction-guide.md#63-获取最小权限凭据)。

## 4. 切换后端

先读取 manifest，只有后端 `available=true` 才运行：

```bash
AUTOML_API_URL=http://127.0.0.1:8000 \
AUTOML_TOKEN=local-development-token \
PYTHONPATH=packages/python_sdk/src \
uv run python examples/python/sdk_guided_workflow.py --backend autogluon
```

TabPFN 需要部署方已接受许可并配置批准的权重。脚本不会代替部署方接受许可，也不会自动向 Git 提交
模型 token。

## 5. JSON 请求体

`requests/*.json` 中的 `dsv_REPLACE_ME` 必须替换成实际 `DatasetVersion` ID。例如：

```bash
jq --arg dsv 'dsv_000000000001' \
  '.dataset_version_id = $dsv' \
  examples/requests/autogluon-binary.json |
curl -sS -X POST "$AUTOML_API_URL/v1/runs" \
  -H "Authorization: Bearer $AUTOML_TOKEN" \
  -H "Idempotency-Key: example-autogluon-0001" \
  -H 'Content-Type: application/json' \
  --data-binary @-
```

逐路由 curl 示例见[API 路由使用手册](../docs/api-route-reference.md)。
