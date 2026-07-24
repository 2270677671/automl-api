# Managed AutoML API 路由使用手册

## 1. 文档说明

本文面向外部 Agent 平台、SDK 开发者和 API 接入工程师，逐项说明 Managed AutoML API 0.7.0 的 HTTP 路由用途、状态和调用示例。控制面字段、枚举和响应 Schema 以 [OpenAPI 3.1 契约](../openapi/automl-api.yaml) 为准；上传字节和下载票据 URL 属于 data-plane 路由，由控制面响应提供，不允许客户端自行拼接。

当前服务是独立 AutoML 执行后端，不内置 LLM。外部 Agent 平台负责 LLM、Prompt、凭据保管、预算和人机交互；本 API 负责数据上传、运行编排、结构化中断、训练评估、输出和 artifact 下载。

## 2. 通用约定

示例默认本机部署地址为 `http://127.0.0.1:8000`：

```bash
export AUTOML_API=http://127.0.0.1:8000
export AUTOML_TOKEN=local-exp
```

通用 Header：

```text
Authorization: Bearer <token>
Content-Type: application/json
Idempotency-Key: <16-128 visible ASCII chars>   # 写请求需要
If-Match: "<revision>"                          # 部分并发控制请求需要
Accept: application/json                        # JSON 事件分页
Accept: text/event-stream                       # SSE 事件流
```

重要规则：

- development profile 接受任意非空 Bearer token，并用 token hash 派生本地租户。
- 生产 profile 需要正式 JWT/OIDC 或 workload identity 配置。
- 所有写请求应使用稳定 `Idempotency-Key`；网络重试必须复用同一个 key。
- `DecisionPacket` 回答使用 `wait_set_revision` 的 `If-Match`。
- `pause` 和 `resume` 使用 `run_revision` 的 `If-Match`。
- HTTP ETag 用于缓存读取结果，不能替代 `run_revision` 或 `wait_set_revision`。
- 跨租户资源返回 `404`，不会泄露资源是否存在。

## 3. 当前可用性总览

| 路由类别 | 状态 | 说明 |
| --- | --- | --- |
| 健康检查、OpenAPI、Agent manifest | 可用 | 本机部署和外部 Agent 平台握手使用 |
| 数据集上传 | 可用 | 当前 local durable profile 支持单分片 CSV/Parquet 上传 |
| Run 生命周期 | 可用 | 创建、列表、读取、暂停、恢复、取消 |
| 事件、输出、DecisionPacket、结果 | 可用 | 支持 JSON 分页、SSE、结构化中断和终态结果 |
| artifact 下载 | 可用 | 通过短期 ticket 下载，支持 Range 和 SHA-256 校验 |
| experiments | 兼容占位 | 列表返回空页；按 ID 查询返回 `404` |
| approvals/models/deletions | 可用 | 生产部署审批、候选模型读取和删除任务跟踪 |
| Webhook 管理与 outbox | 可用 | endpoint 管理、delivery 查询和人工重投；HTTP dispatcher 需独立部署 |

## 4. 路由明细

### 4.1 GET `/healthz`

用途：进程存活检查。该路由不需要 Bearer token，不在 OpenAPI schema 中。

响应：`200`，返回运行模式。

```bash
curl -sS "$AUTOML_API/healthz"
```

示例响应：

```json
{"status": "ok", "mode": "milestone-2-local-durable"}
```

### 4.2 GET `/readyz`

用途：就绪检查。SQLite profile 会执行一次轻量 store 查询。该路由不需要 Bearer token，不在 OpenAPI schema 中。

响应：local/partner-preview profile 就绪时返回 `200`。0.7.0 的 formal production profile
包含一个固定失败的 `runtime_adapters` 必选检查，因此无论环境变量是否齐全都返回
`503 production_preflight_failed`；它不能作为当前版本的生产启动探针。

```bash
curl -sS "$AUTOML_API/readyz"
```

示例响应：

```json
{"status": "ready"}
```

### 4.3 GET `/openapi.yaml`

用途：获取完整 canonical OpenAPI 3.1 契约。该路由不需要 Bearer token，不在生成 schema 中。

响应：`200 application/yaml`。

```bash
curl -sS "$AUTOML_API/openapi.yaml" -o automl-api.yaml
```

### 4.4 GET `/v1/agent/tool-openapi.yaml`

用途：获取外部 Agent 平台可作为工具合同使用的精简 OpenAPI。只包含当前可用的 Agent operation，不包含通用自由文本执行入口。

认证：需要 Bearer；生产模式需要 `automl:operation:getAgentInterfaceManifest` scope。

响应：`200 application/yaml`，带 `ETag`、`Cache-Control`、`Vary: Authorization`。

```bash
curl -sS "$AUTOML_API/v1/agent/tool-openapi.yaml" \
  -H "Authorization: Bearer $AUTOML_TOKEN" \
  -o automl-agent-tools.yaml
```

### 4.5 GET `/v1/agent/manifest`

用途：外部 Agent 平台握手入口。返回服务角色、版本、OpenAPI 链接、安全边界、运行限制、默认后端和后端 readiness。

认证：需要 Bearer；生产模式需要 `automl:operation:getAgentInterfaceManifest` scope。

```bash
curl -sS "$AUTOML_API/v1/agent/manifest" \
  -H "Authorization: Bearer $AUTOML_TOKEN"
```

关键响应字段：

```json
{
  "service_role": "AUTOML_EXECUTION_BACKEND",
  "planner_location": "EXTERNAL_AGENT_PLATFORM",
  "internal_llm_calls": false,
  "default_backend_id": "sklearn",
  "backends": [
    {"backend_id": "sklearn", "installed": true, "available": true},
    {"backend_id": "autogluon", "installed": true, "available": true},
    {
      "backend_id": "tabpfn",
      "installed": true,
      "available": false,
      "unavailable_reason": "MODEL_LICENSE_NOT_ACCEPTED",
      "capabilities": {
        "required_attributions": ["Built with PriorLabs-TabPFN"]
      }
    }
  ]
}
```

外部 Agent 平台对外展示后端能力、选项或结果时，必须原样展示该后端
`capabilities.required_attributions[]` 中的每条文本。

### 4.6 POST `/v1/datasets`

用途：创建数据集上传会话，返回 `dataset_version_id`、`upload_id` 和分片上传 URL。

认证：需要 Bearer；写请求需要 `Idempotency-Key`。

成功响应：`201 DatasetUploadSession`。

```bash
curl -sS -X POST "$AUTOML_API/v1/datasets" \
  -H "Authorization: Bearer $AUTOML_TOKEN" \
  -H "Idempotency-Key: dataset-customer-churn-0001" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "customer-churn",
    "filename": "customer_churn.csv",
    "media_type": "text/csv",
    "size_bytes": 4096
  }'
```

关键响应字段：

```json
{
  "dataset_id": "ds_000000000001",
  "dataset_version_id": "dsv_000000000001",
  "upload_id": "upl_000000000001",
  "parts": [
    {
      "part_number": 1,
      "url": "/v1/dataset-versions/dsv_000000000001/upload-parts/1?upload_id=upl_000000000001",
      "required_headers": {"x-automl-upload-part": "1"}
    }
  ]
}
```

### 4.7 POST `/v1/dataset-versions/{dataset_version_id}/upload-parts:sign`

用途：重新签发上传分片 URL。当前 local durable profile 主要用于补发单分片上传 URL；后续对象存储 profile 可用于短期预签名 URL。

认证：需要 Bearer；写请求需要 `Idempotency-Key`。

成功响应：`200 UploadPartsResponse`。

```bash
curl -sS -X POST "$AUTOML_API/v1/dataset-versions/dsv_000000000001/upload-parts:sign" \
  -H "Authorization: Bearer $AUTOML_TOKEN" \
  -H "Idempotency-Key: sign-upload-parts-0001" \
  -H "Content-Type: application/json" \
  -d '{
    "upload_id": "upl_000000000001",
    "part_numbers": [1]
  }'
```

### 4.8 PUT `/v1/dataset-versions/{dataset_version_id}/upload-parts/{part_number}`

用途：上传数据分片字节。该路由是 data-plane 上传入口，不在 OpenAPI schema 中；应优先使用创建上传会话返回的 `parts[].url` 和 `required_headers`。

认证：local durable profile 与 API 同源，需要 Bearer。后续外部对象存储 profile 可能改为预签名 URL，不携带 API Bearer。

成功响应：`204`，Header 返回 `ETag`、`X-Content-SHA256`、`X-Content-Length`。

```bash
curl -sS -i -X PUT "$AUTOML_API/v1/dataset-versions/dsv_000000000001/upload-parts/1?upload_id=upl_000000000001" \
  -H "Authorization: Bearer $AUTOML_TOKEN" \
  -H "x-automl-upload-part: 1" \
  -H "Content-Type: text/csv" \
  --data-binary @customer_churn.csv
```

### 4.9 POST `/v1/dataset-versions/{dataset_version_id}:finalize`

用途：完成上传并校验分片 ETag、总大小和 SHA-256。只有 finalize 成功后，数据版本才进入 `READY`。

认证：需要 Bearer；写请求需要 `Idempotency-Key`。

成功响应：`202 DatasetVersion`，通常带 `Retry-After: 1`。

```bash
curl -sS -X POST "$AUTOML_API/v1/dataset-versions/dsv_000000000001:finalize" \
  -H "Authorization: Bearer $AUTOML_TOKEN" \
  -H "Idempotency-Key: finalize-dataset-0001" \
  -H "Content-Type: application/json" \
  -d '{
    "upload_id": "upl_000000000001",
    "sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
    "parts": [{"part_number": 1, "etag": "etag-from-upload-response"}]
  }'
```

### 4.10 GET `/v1/dataset-versions/{dataset_version_id}`

用途：读取数据版本状态和校验信息。

认证：需要 Bearer。

缓存：支持 `If-None-Match`；未变化返回 `304`。

```bash
curl -sS -i "$AUTOML_API/v1/dataset-versions/dsv_000000000001" \
  -H "Authorization: Bearer $AUTOML_TOKEN"
```

### 4.11 POST `/v1/runs`

用途：创建 AutoML Run。`objective.backend_id` 可选择 `sklearn`、`autogluon` 或 `tabpfn`；省略时使用 manifest 的 `default_backend_id`。

认证：需要 Bearer；写请求需要 `Idempotency-Key`。

成功响应：`202 RunSnapshot`，返回 `ETag` 和 `Retry-After: 1`。

```bash
curl -sS -X POST "$AUTOML_API/v1/runs" \
  -H "Authorization: Bearer $AUTOML_TOKEN" \
  -H "Idempotency-Key: create-run-sklearn-0001" \
  -H "Content-Type: application/json" \
  -d '{
    "dataset_version_id": "dsv_000000000001",
    "objective": {
      "backend_id": "sklearn",
      "target_column": "target",
      "task_type": "BINARY_CLASSIFICATION",
      "positive_class": 1,
      "iid_confirmed": true,
      "primary_metric": "roc_auc",
      "business_context": "客户流失二分类离线评估"
    },
    "autonomy": {"mode": "GUIDED", "production_deploy": "DISABLED"},
    "policy": {"allow_pii": false, "allow_external_llm": false, "risk_tier": "STANDARD"},
    "budget": {
      "max_trials": 2,
      "max_compute_credits": 1,
      "max_wall_time_seconds": 600,
      "max_llm_tokens": 0
    }
  }'
```

AutoGluon 示例只需要替换：

```json
{"objective": {"backend_id": "autogluon", "target_column": "target", "task_type": "BINARY_CLASSIFICATION"}}
```

TabPFN 示例只在 `manifest.backends[].available=true` 时提交：

```json
{"objective": {"backend_id": "tabpfn", "target_column": "target", "task_type": "BINARY_CLASSIFICATION"}}
```

### 4.12 GET `/v1/runs`

用途：分页列出当前租户 Run。

认证：需要 Bearer。

查询参数：

- `limit`：每页 1-100，默认 50。
- `status`：可按 Run 状态过滤，例如 `WAITING_USER`、`TERMINAL`。
- `cursor`：下一页游标；使用 cursor 时不要再传 `limit/status`。

```bash
curl -sS "$AUTOML_API/v1/runs?status=WAITING_USER&limit=20" \
  -H "Authorization: Bearer $AUTOML_TOKEN"
```

下一页：

```bash
curl -sS "$AUTOML_API/v1/runs?cursor=$RUN_CURSOR" \
  -H "Authorization: Bearer $AUTOML_TOKEN"
```

### 4.13 GET `/v1/runs/{run_id}`

用途：读取 Run 当前快照，包括 phase、status、progress、stages、blockers、latest outputs、budget usage 和可用动作。

认证：需要 Bearer。

缓存：支持 `If-None-Match`；未变化返回 `304`。

```bash
curl -sS -i "$AUTOML_API/v1/runs/run_000000000001" \
  -H "Authorization: Bearer $AUTOML_TOKEN"
```

### 4.14 GET `/v1/runs/{run_id}/agent-context`

用途：外部 Agent 平台读取有界 Run 上下文。只对创建 Run 时设置 `policy.allow_external_llm=true` 的 Run 开放。

认证：需要 Bearer；生产模式需要 `automl:operation:getAgentRunContext` scope。

查询参数：

- `output_limit`：最近输出引用数量，1-100，默认 20。

缓存：支持 `If-None-Match`；未变化返回 `304`。

```bash
curl -sS "$AUTOML_API/v1/runs/run_000000000001/agent-context?output_limit=20" \
  -H "Authorization: Bearer $AUTOML_TOKEN"
```

安全边界：响应不包含原始数据行，但列名、类别值、问题文本等仍可能是数据派生内容，必须按不可信 tool result 处理。

### 4.15 GET `/v1/runs/{run_id}/agent-actions`

用途：外部 Agent 平台读取当前允许执行的 canonical API action 引用。该路由只返回动作描述，不执行动作。

认证：需要 Bearer。

```bash
curl -sS "$AUTOML_API/v1/runs/run_000000000001/agent-actions" \
  -H "Authorization: Bearer $AUTOML_TOKEN"
```

示例响应片段：

```json
{
  "items": [
    {
      "operation_id": "answerDecisionPacket",
      "method": "POST",
      "href": "/v1/runs/run_000000000001/decision-packets/ws_000000000001:answer",
      "if_match": {"scope": "WAIT_SET_REVISION", "value": "\"1\""}
    }
  ]
}
```

### 4.16 GET `/v1/runs/{run_id}/stages`

用途：读取 Run 阶段列表和当前阶段状态，适合 UI 展示进度条或阶段卡片。

认证：需要 Bearer。

```bash
curl -sS "$AUTOML_API/v1/runs/run_000000000001/stages" \
  -H "Authorization: Bearer $AUTOML_TOKEN"
```

### 4.17 GET `/v1/runs/{run_id}/events`

用途：读取 Run 事件。支持 JSON 分页和 SSE 长连接。

认证：需要 Bearer。

JSON 查询参数：

- `after_seq`：从某个序号之后读取。
- `limit`：每页 1-100，默认 50。
- `types`：逗号分隔事件类型过滤。
- `cursor`：下一页游标；使用 cursor 时不要再传其他过滤参数。

JSON 示例：

```bash
curl -sS "$AUTOML_API/v1/runs/run_000000000001/events?after_seq=0&limit=100" \
  -H "Authorization: Bearer $AUTOML_TOKEN" \
  -H "Accept: application/json"
```

SSE 示例：

```bash
curl -N "$AUTOML_API/v1/runs/run_000000000001/events?after_seq=17" \
  -H "Authorization: Bearer $AUTOML_TOKEN" \
  -H "Accept: text/event-stream"
```

SSE 断线续读：

```bash
curl -N "$AUTOML_API/v1/runs/run_000000000001/events" \
  -H "Authorization: Bearer $AUTOML_TOKEN" \
  -H "Accept: text/event-stream" \
  -H "Last-Event-ID: 17"
```

### 4.18 GET `/v1/runs/{run_id}/outputs`

用途：分页列出 Run 已提交的结构化输出。

认证：需要 Bearer。

查询参数：

- `type`：逗号分隔输出类型，例如 `DATA_QUALITY_REPORT,MODEL_CARD,RUN_REPORT`。
- `phase`：逗号分隔阶段过滤。
- `state`：输出状态过滤。
- `limit`：每页 1-100，默认 50。
- `cursor`：下一页游标。

```bash
curl -sS "$AUTOML_API/v1/runs/run_000000000001/outputs?type=MODEL_CARD,RUN_REPORT&limit=20" \
  -H "Authorization: Bearer $AUTOML_TOKEN"
```

常见输出类型：

```text
DATA_QUALITY_REPORT
TASK_SPEC
SPLIT_MANIFEST
BASELINE_RESULT
COST_ESTIMATE
TRIAL_RESULT
EVALUATION_REPORT
MODEL_CARD
RUN_REPORT
FAILURE_REPORT
```

### 4.19 GET `/v1/runs/{run_id}/outputs/{output_id}`

用途：读取单个输出资源完整内容。

认证：需要 Bearer。

```bash
curl -sS "$AUTOML_API/v1/runs/run_000000000001/outputs/out_000000000001" \
  -H "Authorization: Bearer $AUTOML_TOKEN"
```

### 4.20 GET `/v1/runs/{run_id}/decision-packets`

用途：分页列出 Run 的结构化中断问题。Run 进入 `WAITING_USER` 时，通常读取 `status=OPEN` 的 packet。

认证：需要 Bearer。

查询参数：

- `status`：例如 `OPEN`、`ANSWERED`、`SUPERSEDED`、`EXPIRED`。
- `limit`：每页 1-100，默认 50。
- `cursor`：下一页游标。

```bash
curl -sS "$AUTOML_API/v1/runs/run_000000000001/decision-packets?status=OPEN" \
  -H "Authorization: Bearer $AUTOML_TOKEN"
```

### 4.21 POST `/v1/runs/{run_id}/decision-packets/{wait_set_id}:answer`

用途：原子回答一个 wait-set 中的所有问题。成功后 workflow 自动从 checkpoint 继续。

认证：需要 Bearer；写请求需要 `Idempotency-Key` 和 `If-Match: "<wait_set_revision>"`。

成功响应：`202 CommandReceipt`。

```bash
curl -sS -X POST "$AUTOML_API/v1/runs/run_000000000001/decision-packets/ws_000000000001:answer" \
  -H "Authorization: Bearer $AUTOML_TOKEN" \
  -H "Idempotency-Key: answer-wait-set-0001" \
  -H 'If-Match: "1"' \
  -H "Content-Type: application/json" \
  -d '{
    "answers": [
      {"question_id": "q_target", "value": "target"},
      {"question_id": "q_iid", "value": true}
    ]
  }'
```

常见错误：

- `412 stale_revision`：`If-Match` 与当前 wait-set revision 不一致。
- `409 invalid_run_state`：Run 状态已经不允许回答。
- `403`：生产环境下 actor/scope 不允许回答该 packet。

### 4.22 POST `/v1/runs/{run_id}:pause`

用途：暂停非终态 Run。暂停后不会继续调度 workflow，直到恢复。

认证：需要 Bearer；写请求需要 `Idempotency-Key` 和 `If-Match: "<run_revision>"`。

```bash
curl -sS -X POST "$AUTOML_API/v1/runs/run_000000000001:pause" \
  -H "Authorization: Bearer $AUTOML_TOKEN" \
  -H "Idempotency-Key: pause-run-command-0001" \
  -H 'If-Match: "3"'
```

### 4.23 POST `/v1/runs/{run_id}:resume`

用途：恢复已暂停 Run。

认证：需要 Bearer；写请求需要 `Idempotency-Key` 和 `If-Match: "<run_revision>"`。

```bash
curl -sS -X POST "$AUTOML_API/v1/runs/run_000000000001:resume" \
  -H "Authorization: Bearer $AUTOML_TOKEN" \
  -H "Idempotency-Key: resume-run-command-0001" \
  -H 'If-Match: "4"'
```

### 4.24 POST `/v1/runs/{run_id}:cancel`

用途：取消非终态 Run。取消是单调幂等紧急命令，不需要 `If-Match`。

认证：需要 Bearer；写请求需要 `Idempotency-Key`。

```bash
curl -sS -X POST "$AUTOML_API/v1/runs/run_000000000001:cancel" \
  -H "Authorization: Bearer $AUTOML_TOKEN" \
  -H "Idempotency-Key: cancel-run-command-0001"
```

### 4.25 GET `/v1/commands/{command_id}`

用途：查询异步命令状态，例如 answer、pause、resume、cancel 的执行结果。

认证：需要 Bearer。

```bash
curl -sS "$AUTOML_API/v1/commands/cmd_000000000001" \
  -H "Authorization: Bearer $AUTOML_TOKEN"
```

响应状态：

```text
ACCEPTED
RUNNING
SUCCEEDED
FAILED
```

### 4.26 GET `/v1/runs/{run_id}/result`

用途：读取 Run 终态结果。只有 Run 进入终态后才有结果。

认证：需要 Bearer。

```bash
curl -sS "$AUTOML_API/v1/runs/run_000000000001/result" \
  -H "Authorization: Bearer $AUTOML_TOKEN"
```

当前 local durable profile 中，成功训练通常返回：

```json
{
  "outcome": "SUCCEEDED",
  "model_disposition": "NO_ELIGIBLE_MODEL",
  "backend_id": "sklearn"
}
```

说明：`NO_ELIGIBLE_MODEL` 表示流程成功但未执行生产质量门禁/模型注册，不代表训练失败。

### 4.27 GET `/v1/artifacts/{artifact_id}`

用途：读取 artifact 元数据，例如大小、SHA-256、media type、ETag、所属 Run 和 lineage。

认证：需要 Bearer。

```bash
curl -sS "$AUTOML_API/v1/artifacts/art_000000000001" \
  -H "Authorization: Bearer $AUTOML_TOKEN"
```

### 4.28 POST `/v1/artifacts/{artifact_id}:download`

用途：签发短期 artifact 下载票据。票据默认 900 秒有效。

认证：需要 Bearer；写请求需要 `Idempotency-Key`。

成功响应：`201 DownloadTicket`。

```bash
curl -sS -X POST "$AUTOML_API/v1/artifacts/art_000000000001:download" \
  -H "Authorization: Bearer $AUTOML_TOKEN" \
  -H "Idempotency-Key: artifact-ticket-0001"
```

关键响应字段：

```json
{
  "ticket_id": "dlt_000000000001",
  "artifact_id": "art_000000000001",
  "url": "http://127.0.0.1:8000/v1/artifact-downloads/<token>",
  "expires_in_seconds": 900,
  "etag": "\"artifact-etag\"",
  "sha256": "0123456789abcdef...",
  "size_bytes": 1024,
  "required_headers": {"If-Match": "\"artifact-etag\""},
  "supports_range": true
}
```

### 4.29 GET `/v1/artifact-downloads/{token}`

用途：按 download ticket 下载 artifact bytes。该路由是 data-plane 下载入口，不在 OpenAPI schema 中；客户端应使用 ticket 返回的 `url` 和 `required_headers`。

认证：不使用 Bearer；使用短期 token 和 `If-Match`。

成功响应：`200` 或 Range 下载的 `206`，Header 包含 `ETag`、`X-Content-SHA256`、`Content-Length`。

```bash
curl -sS -L "$DOWNLOAD_URL" \
  -H 'If-Match: "artifact-etag"' \
  -o model-or-report.artifact
```

断点续传示例：

```bash
curl -sS -L "$DOWNLOAD_URL" \
  -H 'If-Match: "artifact-etag"' \
  -H "Range: bytes=1048576-" \
  -o model-or-report.artifact.part
```

### 4.30 GET `/v1/runs/{run_id}/experiments`

用途：列出 Run 实验。当前 0.7.0 是兼容占位路由，返回空页。

认证：需要 Bearer。

```bash
curl -sS "$AUTOML_API/v1/runs/run_000000000001/experiments" \
  -H "Authorization: Bearer $AUTOML_TOKEN"
```

示例响应：

```json
{"items": [], "page": {"next_cursor": null, "has_more": false, "high_watermark": 0}}
```

### 4.31 GET `/v1/runs/{run_id}/experiments/{experiment_id}`

用途：读取单个实验详情。当前 0.7.0 尚未注册实验资源，返回 `404`。

认证：需要 Bearer。

```bash
curl -sS -i "$AUTOML_API/v1/runs/run_000000000001/experiments/exp_000000000001" \
  -H "Authorization: Bearer $AUTOML_TOKEN"
```

### 4.32 GET `/v1/runs/{run_id}/approvals`

用途：列出审批对象。`production_deploy=REQUIRE_APPROVAL` 的 Run 在模型包装后会进入
`WAITING_APPROVAL`，此路由返回当前审批对象。

认证：需要 Bearer。

```bash
curl -sS "$AUTOML_API/v1/runs/run_000000000001/approvals" \
  -H "Authorization: Bearer $AUTOML_TOKEN"
```

### 4.33 POST `/v1/runs/{run_id}/approvals/{approval_id}:decide`

用途：提交审批决定。`APPROVE` 会注册 `ModelCandidate` 并使 Run 进入终态；
`REQUEST_CHANGES` 或 `REJECT` 会终止候选注册并返回无合格模型结果。

认证：需要 Bearer。

```bash
curl -sS -i -X POST "$AUTOML_API/v1/runs/run_000000000001/approvals/apr_000000000001:decide" \
  -H "Authorization: Bearer $AUTOML_TOKEN" \
  -H "Idempotency-Key: decide-approval-0001" \
  -H 'If-Match: "1"' \
  -H "Content-Type: application/json" \
  -d '{"decision": "APPROVE", "reason": "指标和限制已审核", "evidence_version": 1}'
```

### 4.34 GET `/v1/models/{model_id}`

用途：读取已注册模型候选。只有生产部署审批通过后才会出现 `ELIGIBLE_CANDIDATE`。

认证：需要 Bearer。

```bash
curl -sS -i "$AUTOML_API/v1/models/model_000000000001" \
  -H "Authorization: Bearer $AUTOML_TOKEN"
```

### 4.35 Webhook 管理路由组 `/v1/webhook-endpoints...`

用途：Webhook 管理和投递查询。当前 API 已支持 endpoint 创建、列表、读取、删除、
密钥轮换、启用、delivery outbox 查询和人工重投；真实 HTTP 投递 dispatcher 可作为独立
worker 消费 outbox。

认证：需要 Bearer；生产模式下仍会按具体 operation 做 scope 检查。

路由清单：

| 方法 | 路由 | 预期用途 | 当前状态 |
| --- | --- | --- | --- |
| `POST` | `/v1/webhook-endpoints` | 创建 Webhook endpoint | 可用 |
| `GET` | `/v1/webhook-endpoints` | 列出 Webhook endpoint | 可用 |
| `GET` | `/v1/webhook-endpoints/{id}` | 读取 Webhook endpoint | 可用 |
| `DELETE` | `/v1/webhook-endpoints/{id}` | 删除 Webhook endpoint | 可用 |
| `POST` | `/v1/webhook-endpoints/{id}:rotate-secret` | 轮换签名密钥 | 可用 |
| `POST` | `/v1/webhook-endpoints/{id}:enable` | 恢复投递 | 可用 |
| `GET` | `/v1/webhook-endpoints/{id}/deliveries` | 列出投递记录 | 可用 |
| `GET` | `/v1/webhook-endpoints/{id}/deliveries/{delivery_id}` | 读取投递记录 | 可用 |
| `POST` | `/v1/webhook-endpoints/{id}/deliveries/{delivery_id}:redeliver` | 请求重投 | 可用 |

示例：

```bash
curl -sS -i -X POST "$AUTOML_API/v1/webhook-endpoints" \
  -H "Authorization: Bearer $AUTOML_TOKEN" \
  -H "Idempotency-Key: webhook-create-0001" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://example.internal/automl-events",
    "event_types": ["run.completed.v1", "run.failed.v1"]
  }'
```

### 4.36 DELETE `/v1/datasets/{dataset_id}`

用途：请求删除数据集并创建 deletion job。当前 local durable profile 会先撤销控制面访问、取消
关联的非终态 Run，再同步物理删除本地 upload/source 和派生 artifact 字节，并把 artifact 标为
`DELETED`；local model-registry 状态记为 `INACCESSIBLE`。这不等价于生产级分布式删除 saga：
PostgreSQL、外部对象存储和外部模型注册表仍需独立 worker 执行并回写逐存储状态。

认证：需要 Bearer。

```bash
curl -sS -i -X DELETE "$AUTOML_API/v1/datasets/ds_000000000001" \
  -H "Authorization: Bearer $AUTOML_TOKEN" \
  -H "Idempotency-Key: delete-dataset-0001"
```

### 4.37 GET `/v1/deletions/{deletion_id}`

用途：查询删除任务状态、受影响 Run 和逐存储删除状态。

认证：需要 Bearer。

```bash
curl -sS -i "$AUTOML_API/v1/deletions/del_000000000001" \
  -H "Authorization: Bearer $AUTOML_TOKEN"
```

## 5. 一次完整调用顺序

推荐接入顺序：

1. `GET /v1/agent/manifest`：确认服务版本、后端 readiness 和限制。
2. `POST /v1/datasets`：创建上传会话。
3. `PUT /v1/dataset-versions/{dataset_version_id}/upload-parts/{part_number}`：上传数据字节。
4. `POST /v1/dataset-versions/{dataset_version_id}:finalize`：完成数据版本。
5. `POST /v1/runs`：创建 Run。
6. `GET /v1/runs/{run_id}` 或 `GET /v1/runs/{run_id}/events`：观察进度。
7. `GET /v1/runs/{run_id}/decision-packets?status=OPEN`：如进入 `WAITING_USER`，读取问题。
8. `POST /v1/runs/{run_id}/decision-packets/{wait_set_id}:answer`：提交结构化答案。
9. `GET /v1/runs/{run_id}/outputs`：读取中间和终态输出。
10. `GET /v1/runs/{run_id}/result`：读取终态结果。
11. `GET /v1/artifacts/{artifact_id}`、`POST /v1/artifacts/{artifact_id}:download`、`GET /v1/artifact-downloads/{token}`：下载产物。

## 6. 常见错误

| HTTP 状态码 | code 示例 | 含义 | 建议处理 |
| --- | --- | --- | --- |
| `400` | `invalid_cursor`、`invalid_upload_header` | 参数组合或 Header 不合法 | 按错误 detail 修正请求 |
| `401` | `unauthorized` | 缺少或无效 Bearer | 刷新/补充 token |
| `403` | `forbidden`、`external_agent_access_denied` | scope、actor 或 policy 不允许 | 检查生产 scope 和 Run policy |
| `404` | `not_found` | 资源不存在或跨租户隐藏 | 确认租户/token/ID |
| `409` | `idempotency_key_reused`、`invalid_run_state` | 幂等键复用冲突或状态不允许 | 换业务命令或重新读取状态 |
| `410` | `cursor_expired`、`download_ticket_expired` | 事件/分页游标或下载票据过期 | 重新读快照或重新签发 ticket |
| `412` | `stale_revision`、`artifact_etag_mismatch` | revision/ETag 不匹配 | 重新读取资源后使用最新值 |
| `413` | `dataset_too_large` | 数据超过限制 | 缩小数据或调整部署限额 |
| `422` | `validation_failed`、`budget_limit_exceeded` | 请求体或预算不合法 | 按 schema 和限制修正 |
| `429` | `active_run_limit_exceeded`、`tenant_storage_limit_exceeded` | 并发或存储超限 | 按 `Retry-After` 退避 |

## 7. Python SDK 对应关系

| HTTP 路由 | SDK 方法 |
| --- | --- |
| `GET /v1/agent/manifest` | `get_agent_manifest()`、`list_backends()` |
| `GET /v1/agent/tool-openapi.yaml` | `get_agent_tool_openapi()` |
| `GET /v1/runs/{run_id}/agent-context` | `get_agent_context()` |
| `GET /v1/runs/{run_id}/agent-actions` | `list_agent_actions()` |
| `POST /v1/datasets` | `create_dataset()`、`upload_dataset_file()` |
| `POST /v1/dataset-versions/{id}/upload-parts:sign` | `sign_upload_parts()` |
| `POST /v1/dataset-versions/{id}:finalize` | `finalize_dataset()` |
| `GET /v1/dataset-versions/{id}` | `get_dataset_version()` |
| `POST /v1/runs` | `create_run()` |
| `GET /v1/runs` | `list_runs()` |
| `GET /v1/runs/{run_id}` | `get_run()` |
| `GET /v1/runs/{run_id}/stages` | `get_run_stages()` |
| `GET /v1/runs/{run_id}/experiments...` | `list_run_experiments()`、`get_run_experiment()` |
| `GET /v1/runs/{run_id}/events` | `get_run_events()`、`iter_run_events()`、`stream_run_events()` |
| `GET /v1/runs/{run_id}/outputs` | `list_outputs()`、`iter_outputs()` |
| `GET /v1/runs/{run_id}/outputs/{output_id}` | `get_output()` |
| `GET /v1/runs/{run_id}/decision-packets` | `list_decision_packets()`、`wait_for_question()` |
| `POST /v1/runs/{run_id}/decision-packets/{wait_set_id}:answer` | `answer_decision_packet()`、`answer_and_wait()` |
| `POST /v1/runs/{run_id}:pause` | `pause_run()` |
| `POST /v1/runs/{run_id}:resume` | `resume_run()` |
| `POST /v1/runs/{run_id}:cancel` | `cancel_run()` |
| `GET /v1/commands/{command_id}` | `get_command()`、`wait_for_command()` |
| `GET /v1/runs/{run_id}/result` | `get_run_result()`、`wait_for_result()` |
| `GET /v1/artifacts/{artifact_id}` | `get_artifact()` |
| `POST /v1/artifacts/{artifact_id}:download` | `create_artifact_download_ticket()` |
| `GET /v1/artifact-downloads/{token}` | `download_artifact_file()` |
| `GET/POST /v1/runs/{run_id}/approvals...` | `list_approvals()`、`decide_approval()` |
| `GET /v1/models/{model_id}` | `get_model_candidate()` |
| `/v1/webhook-endpoints...` | `create/list/get/delete/rotate/enable_webhook_*()`、delivery helpers |
| `DELETE /v1/datasets/{dataset_id}` | `delete_dataset()` |
| `GET /v1/deletions/{deletion_id}` | `get_deletion_job()` |

SDK 端到端示例：

```python
from automl_sdk import AutoMLClient

with AutoMLClient("http://127.0.0.1:8000", token="local-exp") as api:
    dataset = api.upload_dataset_file("customer_churn.csv", name="customer-churn")
    run = api.create_run(
        dataset_version_id=dataset["dataset_version_id"],
        objective={
            "backend_id": "sklearn",
            "target_column": "target",
            "task_type": "BINARY_CLASSIFICATION",
            "positive_class": 1,
            "iid_confirmed": True,
            "primary_metric": "roc_auc",
        },
        autonomy={"mode": "GUIDED", "production_deploy": "DISABLED"},
        policy={"allow_pii": False, "allow_external_llm": False, "risk_tier": "STANDARD"},
        budget={
            "max_trials": 2,
            "max_compute_credits": 1,
            "max_wall_time_seconds": 600,
            "max_llm_tokens": 0,
        },
    )
    result = api.wait_for_result(run["run_id"])
    print(result["outcome"], result["model_disposition"])
```
