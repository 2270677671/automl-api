# Managed AutoML API 可运行案例

本目录提供不含真实凭据和用户数据的最小可运行案例。默认连接本机开发服务
`http://127.0.0.1:8000`，使用合成样例数据和 sklearn。生产环境通过环境变量传入 HTTPS、CA、
Agent token 和 Human token。

## 1. 文件说明

| 路径 | 用途 |
| --- | --- |
| `python/sdk_guided_workflow.py` | 使用官方 Python SDK 完成上传、中断、回答、结果和 artifact |
| `python/http_guided_workflow.py` | 只使用 HTTP 客户端逐步调用原始 API 路由 |
| `python/webhook_receiver.py` | 验证 raw-body HMAC、300 秒重放窗口和 delivery ID 的 callback receiver |
| `python/sdk_classification_regression.py` | 运行 360 条数据的二分类或回归完整案例 |
| `python/generate_classification_regression_data.py` | 使用固定随机种子重新生成两套 360 条合成数据 |
| `python/record_classification_regression_api_io.py` | 真实调用 API 并重新录制两个案例的逐接口输入输出 |
| `api-io/classification/` | 二分类案例的 18 组请求/响应 JSON 和下载图片 |
| `api-io/regression/` | 回归案例的 18 组请求/响应 JSON 和下载图片 |
| `data/customer_churn.csv` | 合成二分类数据，不含真实个人信息 |
| `data/regression.csv` | 合成回归数据 |
| `data/classification_360.csv` | 360 条客户流失二分类数据，目标列为 `churned` |
| `data/regression_360.csv` | 360 条月租金回归数据，目标列为 `monthly_rent` |
| `requests/sklearn-guided.json` | sklearn 人工确认案例请求体 |
| `requests/sklearn-classification-360.json` | 360 条二分类案例的完整 Run 请求体 |
| `requests/sklearn-regression-360.json` | 360 条回归案例的完整 Run 请求体 |
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

回归示例可直接复用同一脚本，并会下载 RMSE/观测-预测/残差/残差分布图：

```bash
uv run python examples/python/http_guided_workflow.py \
  --backend tabpfn \
  --task-type REGRESSION \
  --primary-metric rmse \
  --target target \
  --data examples/data/regression.csv \
  --preconfirm-objective \
  --output-dir example-output/tabpfn-regression
```

`--preconfirm-objective` 只适用于调用方已经从用户处取得 target 与 i.i.d. 明确确认的情况；省略时，
脚本会按 GUIDED 流程等待 DecisionPacket，并要求通过 `AUTOML_HUMAN_TOKEN` 提供具有人类委托声明的
令牌。Agent 的 `client_credentials` token 不能代替人完成该确认。

本地开发 profile 以 token 哈希作为合成 tenant，因此 Agent 和 Human 应使用同一个 token。输出写入
`example-output/`。

## 3. 分类与回归 360 条数据案例

二分类：

```bash
AUTOML_API_URL=http://127.0.0.1:8000 \
AUTOML_TOKEN=local-development-token \
PYTHONPATH=packages/python_sdk/src \
uv run python examples/python/sdk_classification_regression.py classification
```

回归：

```bash
AUTOML_API_URL=http://127.0.0.1:8000 \
AUTOML_TOKEN=local-development-token \
PYTHONPATH=packages/python_sdk/src \
uv run python examples/python/sdk_classification_regression.py regression
```

两套数据各 360 条，案例会上传数据、创建 Run、等待完成、读取评估指标并下载结果图片。字段说明、
参考指标、Callback 接入和重新生成方式见
[分类与回归完整案例](classification-regression.md)。

每一步实际 HTTP 请求与响应已分别写入 `api-io/classification/` 和
`api-io/regression/`。每个目录的 `index.json` 是调用顺序索引；文件名中的同一序号分别为
`*.request.json` 和 `*.response.json`。

## 4. 生产 HTTPS 案例

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

## 5. 切换后端

先读取 manifest，只有后端 `available=true` 才运行：

```bash
AUTOML_API_URL=http://127.0.0.1:8000 \
AUTOML_TOKEN=local-development-token \
PYTHONPATH=packages/python_sdk/src \
uv run python examples/python/sdk_guided_workflow.py --backend autogluon
```

TabPFN 需要部署方已接受许可并配置批准的权重。脚本不会代替部署方接受许可，也不会自动向 Git 提交
模型 token。

## 6. JSON 请求体

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

## 7. 启动 callback receiver

先创建 Webhook endpoint，再把创建响应中仅返回一次的 `signing_secret` 注入接收端：

```bash
AUTOML_WEBHOOK_SIGNING_SECRET='<one-time-secret>' \
uv run uvicorn webhook_receiver:app \
  --app-dir examples/python --host 0.0.0.0 --port 9000
```

示例用内存 set 去重，只适合单进程联调。生产接收端必须在返回 2xx 前，将 event 和
`delivery_id` 唯一约束原子写入持久存储。
