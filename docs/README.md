# Managed AutoML API 文档中心

本文档中心面向部署方、第三方 Agent 平台工程师、API/SDK 使用者和验收人员。当前稳定版本为
`0.8.0`。服务本身不调用 LLM，而是提供可恢复的 AutoML HTTP API；外部 Agent 平台负责 LLM、
Prompt、DLP、凭据、人机交互和工具调用策略。

## 1. 按目标选择文档

| 目标 | 首先阅读 | 后续资料 |
| --- | --- | --- |
| 从 GitHub 完整复现服务 | [完整复现指南](reproduction-guide.md) | [单机生产部署手册](single-node-production.md) |
| 了解使用者完整操作流程 | [使用手册](user-manual.md) | [API 调用流程](api-usage.md) |
| 获取一份带完整案例的独立手册 | [API 使用手册与示范案例](api-user-guide-with-examples.md) | [可运行案例](../examples/README.md) |
| 查询每个 HTTP 路由 | [API 路由使用手册](api-route-reference.md) | [OpenAPI 3.1](../openapi/automl-api.yaml) |
| 嵌入第三方 Agent 平台 | [外部 Agent 接入契约](external-agent-integration.md) | [Agent 工具 OpenAPI](../openapi/automl-agent-tools.yaml) |
| 接收每个阶段的状态和准入结果 | [阶段 Callback 契约](stage-callback-contract.md) | [Webhook 接收端案例](../examples/python/webhook_receiver.py) |
| 自动获取和刷新生产 token | [OIDC/OAuth2 Client Credentials](oidc-client-credentials.md) | [Python SDK](../packages/python_sdk/README.md) |
| 运行代码案例 | [示例目录](../examples/README.md) | [Python SDK README](../packages/python_sdk/README.md) |
| 选择 sklearn/AutoGluon/TabPFN | [后端说明](framework-backends.md) | [API 三后端案例](api-usage.md#9-三个标准后端案例) |
| 生产交付和安全评审 | [生产交付方案](production-delivery.md) | [0.8.0 测试报告](test-report-0.8.0.md) |

## 2. 五分钟本地体验

要求 Python 3.11-3.13 和 `uv`。本地开发模式接受任意非空 Bearer，仅用于功能复现。

```bash
git clone https://github.com/2270677671/automl-api.git
cd automl-api
uv sync --extra dev
uv run automl-api
```

另开终端：

```bash
curl -fsS http://127.0.0.1:8000/healthz
curl -fsS http://127.0.0.1:8000/readyz
AUTOML_API_URL=http://127.0.0.1:8000 \
AUTOML_TOKEN=local-development-token \
PYTHONPATH=packages/python_sdk/src \
uv run python examples/python/sdk_guided_workflow.py
```

案例会上传仓库内样例数据，创建 sklearn Run，处理结构化中断，等待终态，并通过短期票据下载和
校验 artifact。生产环境不能使用本地开发 token，完整步骤见[完整复现指南](reproduction-guide.md)。

## 3. 契约文件

| 文件 | 用途 |
| --- | --- |
| [`openapi/automl-api.yaml`](../openapi/automl-api.yaml) | 完整控制面 API 契约 |
| [`openapi/automl-agent-tools.yaml`](../openapi/automl-agent-tools.yaml) | 外部 Agent 可注册的受限工具契约 |
| [`packages/python_sdk`](../packages/python_sdk) | 同步 Python SDK 及类型化异常 |
| [`examples/requests`](../examples/requests) | sklearn、AutoGluon、TabPFN Run 请求体 |

上传 URL 和 artifact 下载 URL 是服务动态签发的数据面地址，不应由客户端自行拼接，因此不会作为
独立控制面 operation 出现在 Agent 工具 OpenAPI 中。

## 4. 生产边界

- `single-node-production` 已接入 JWT、私有 HTTPS、Host 白名单、限流、审计、SQLite、对象目录、
  串行训练和校验备份，适合小规模单节点生产。
- `cluster-production` 在 PostgreSQL/RLS、S3/KMS、独立 worker 和高可用 adapter 未接入前保持
  fail-closed，不能把单机部署描述为集群高可用。
- `production_external_llm_safe=false` 是硬边界。Agent 平台必须在上传前和 API 结果进入 LLM 前
  完成 DLP/脱敏，并校验 LLM 生成的所有工具参数。
- TabPFN 只有在部署方接受适用许可并提供批准的模型权重后才能启用；对外展示时必须保留
  `Built with PriorLabs-TabPFN`。

## 5. 验证基线

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
uv lock --check
uv run python scripts/generate_agent_openapi.py --check
PYTHONPATH=apps/api/src uv run python -m automl_api.production
git diff --check
```

逐项证据、真实 GPU/三后端运行结果和已知限制见
[Managed AutoML API 0.8.0 逐项测试报告](test-report-0.8.0.md)。
