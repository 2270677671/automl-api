# Managed AutoML API 调用流程

完整字段以 [OpenAPI 3.1 契约](../openapi/automl-api.yaml) 为准。以下示例只展示一次典型的“上传、观察过程、回答问题、获取结果”调用。

> 当前默认实现是 Milestone 2 local durable：CSV/Parquet 字节会写入本地对象目录，SQLite 会保存
> 资源、幂等记录和 workflow checkpoint，单个本地 worker 会在重启后继续非终态 Run。它仍不是生产
> 服务：Bearer token 只是开发租户隔离，未实现 JWT/RLS/DLP/HA，模型 artifact 仅供离线评估。
> Webhook、审批决定、删除 saga 和模型注册仍属于后续契约，当前管理路由会返回 `501` 或 `404`。

## 0. 快速开始

启动本地 API：

```bash
python3 -m pip install -e '.[dev]'
automl-api
```

或使用 Docker/Compose：

```bash
docker build -t managed-automl-api:0.7.0 .
docker run --rm -p 127.0.0.1:8000:8000 managed-automl-api:0.7.0
```

客户端统一使用 Bearer 认证。development profile 接受任意非空 token，并用 token hash 派生本地租户；
生产 profile 必须配置 JWT/OIDC 等正式身份参数。

```bash
export AUTOML_API=http://127.0.0.1:8000
export AUTOML_TOKEN=local-development-token

curl -sS "$AUTOML_API/healthz"
curl -sS "$AUTOML_API/readyz"
curl -sS "$AUTOML_API/v1/agent/manifest" \
  -H "Authorization: Bearer $AUTOML_TOKEN"
```

接入方至少应保存这些响应字段：

| 对象 | 关键字段 | 用途 |
| --- | --- | --- |
| `DatasetUploadSession` | `dataset_version_id`、`upload_id`、`parts[]` | 上传和 finalize 数据 |
| `DatasetVersion` | `status`、`revision`、`sha256` | 判断数据是否 READY |
| `RunSnapshot` | `run_id`、`run_revision`、`snapshot_seq`、`status` | 轮询、事件续读、暂停/恢复 |
| `DecisionPacket` | `wait_set_id`、`wait_set_revision`、`questions[]` | 中断后结构化回答 |
| `OutputResource` | `output_id`、`type`、`payload`、`artifact_refs[]` | 获取中间过程和结果证据 |
| `RunResult` | `outcome`、`model_disposition`、`backend_id` | 判断流程终态 |
| `Artifact` / `DownloadTicket` | `artifact_id`、`etag`、`sha256`、`url` | 安全下载产物 |

使用约定：

- 所有写请求带 `Idempotency-Key`，网络重试复用同一个 key。
- `DecisionPacket` 回答使用 `wait_set_revision` 的 `If-Match`。
- `pause` 和 `resume` 使用 `run_revision` 的 `If-Match`。
- 读取 Run 快照返回的 HTTP ETag 只用于缓存验证，不能替代上述 revision。
- API 内部不调用 LLM；外部 Agent 平台通过 manifest/context/actions 调用本 API。

## 1. 创建上传会话

```bash
curl -sS -X POST "$AUTOML_API/v1/datasets" \
  -H "Authorization: Bearer $AUTOML_TOKEN" \
  -H "Idempotency-Key: upload-customer-churn-0001" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "customer-churn",
    "filename": "customers.parquet",
    "media_type": "application/vnd.apache.parquet",
    "size_bytes": 73400320
  }'
```

响应包含 `dataset_id`、`dataset_version_id`、`upload_id` 和分片上传 URL。URL 是当前 data-plane
实现返回的短期地址，不要自行拼接；同时发送响应中的 `required_headers`。local durable profile
的 URL 与 API 同源，因此 SDK 会带上 Bearer；未来外部对象存储 profile 可以改为不带 API 凭证的
预签名地址。

以下是单分片的原始 HTTP 上传方式（推荐使用 SDK 自动处理 ETag、SHA-256 和 finalize；当前上传
不支持断点续传，artifact 下载才支持 `.part` + Range 续传）：

```bash
# 将 SESSION.parts[0].url 和 required_headers 替换为上一步响应中的值。
curl -sS -i -X PUT "$UPLOAD_URL" \
  -H "Authorization: Bearer $AUTOML_TOKEN" \
  -H "x-automl-upload-part: 1" \
  -H "Content-Type: application/vnd.apache.parquet" \
  --data-binary @customers.parquet
```

保存响应里的 `ETag`，并用本地文件的真实 SHA-256 完成 finalize。上传接口会在 finalize 时再次
读取分片、核对 ETag、总大小和 SHA-256；只返回 `204` 并不代表数据版本已经 READY。

## 2. 完成上传并创建 Run

```bash
curl -sS -X POST "$AUTOML_API/v1/dataset-versions/dsv_123:finalize" \
  -H "Authorization: Bearer $AUTOML_TOKEN" \
  -H "Idempotency-Key: finalize-dsv-123-0001" \
  -H "Content-Type: application/json" \
  -d '{
    "upload_id": "upl_123",
    "sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
    "parts": [{"part_number": 1, "etag": "etag-1"}]
  }'
```

数据版本进入 `READY` 后创建 Run：

先读取 manifest 中的后端目录。只有 `available=true` 且 capability 与任务匹配的后端才应提交；
`production_eligible` 是独立门禁，不能由 `available` 推导：

```bash
curl -sS "$AUTOML_API/v1/agent/manifest" \
  -H "Authorization: Bearer $AUTOML_TOKEN"
```

```bash
curl -sS -X POST "$AUTOML_API/v1/runs" \
  -H "Authorization: Bearer $AUTOML_TOKEN" \
  -H "Idempotency-Key: run-dsv-123-0001" \
  -H "Content-Type: application/json" \
  -d '{
    "dataset_version_id": "dsv_123",
    "objective": {
      "backend_id": "sklearn",
      "target_column": null,
      "task_type": "BINARY_CLASSIFICATION",
      "positive_class": 1,
      "iid_confirmed": null,
      "primary_metric": "roc_auc",
      "business_context": "预测未来 30 天内会流失的客户"
    },
    "autonomy": {"mode": "GUIDED", "production_deploy": "DISABLED"},
    "policy": {"allow_pii": false, "allow_external_llm": false, "risk_tier": "STANDARD"},
    "budget": {
      "max_trials": 3,
      "max_compute_credits": 1,
      "max_wall_time_seconds": 3600,
      "max_llm_tokens": 0
    }
  }'
```

`objective.backend_id` 可省略；此时使用 manifest 的 `default_backend_id`。AutoGluon、TabPFN 等
标准后端如果列在 `backends[]` 但状态为 `UNAVAILABLE`，调用方应展示 `unavailable_reason` 并改选其他
后端，而不是提交后持续重试。

返回 `202 RunSnapshot`。保存 `run_id`、`run_revision`、ETag 和 `snapshot_seq`。
这里的 HTTP ETag 校验完整快照表示，会随 `snapshot_seq` 等可见字段变化；暂停/恢复的
`If-Match` 则使用响应体 `run_revision` 格式化成带引号整数。两者不能混用。

## 3. 获取中间过程

轮询当前快照：

```bash
curl -sS "$AUTOML_API/v1/runs/run_01" \
  -H "Authorization: Bearer $AUTOML_TOKEN"
```

从快照的 `snapshot_seq` 之后订阅 SSE，避免“先查状态、再建连接”之间漏事件：

```bash
curl -N "$AUTOML_API/v1/runs/run_01/events?after_seq=17" \
  -H "Authorization: Bearer $AUTOML_TOKEN" \
  -H "Accept: text/event-stream"
```

不能保持长连接时，用 JSON 补拉：

```bash
curl -sS "$AUTOML_API/v1/runs/run_01/events?after_seq=17&limit=100" \
  -H "Authorization: Bearer $AUTOML_TOKEN" \
  -H "Accept: application/json"
```

当前 durable workflow 会发出这些事件（实验指标事件仍是后续实现预留类型）：

- `run.phase_changed.v1`
- `output.committed.v1`
- `decision_packet.requested.v1`
- `run.completed.v1` / `run.failed.v1` / `run.canceled.v1` / `run.expired.v1`

收到 `output.committed.v1` 后，按 ID 获取结构化输出：

```bash
curl -sS "$AUTOML_API/v1/runs/run_01/outputs/out_42" \
  -H "Authorization: Bearer $AUTOML_TOKEN"
```

也可以分页列出阶段结果：

```bash
curl -sS "$AUTOML_API/v1/runs/run_01/outputs?type=DATA_QUALITY_REPORT,TRIAL_RESULT,EVALUATION_REPORT&limit=50" \
  -H "Authorization: Bearer $AUTOML_TOKEN"
```

## 4. 回答阻塞问题

当 `RunSnapshot.status=WAITING_USER` 时，读取当前开放 packet：

```bash
curl -sS "$AUTOML_API/v1/runs/run_01/decision-packets?status=OPEN" \
  -H "Authorization: Bearer $AUTOML_TOKEN"
```

原子提交整个 wait-set：

```bash
curl -sS -X POST "$AUTOML_API/v1/runs/run_01/decision-packets/ws_01:answer" \
  -H "Authorization: Bearer $AUTOML_TOKEN" \
  -H "Idempotency-Key: answer-ws-01-0001" \
  -H 'If-Match: "3"' \
  -H "Content-Type: application/json" \
  -d '{
    "answers": [
      {"question_id": "q_target", "value": "churned"},
      {"question_id": "q_iid", "value": true}
    ]
  }'
```

API 返回 `202 CommandReceipt`。通过 `/v1/commands/{command_id}` 查询命令；成功后 workflow 自动从
checkpoint 继续，不需要再调用通用 resume。如果目标是二分类且没有在 objective 中给出
`positive_class`，训练前还会产生一个新的 `q_positive_class` wait-set，按同样方式回答即可。
这里的 `"3"` 来自 `DecisionPacket.wait_set_revision`，不是频繁变化的 Run ETag；不相关的进度、trial 或阶段更新不会让当前回答得到 `412`。

## 5. 获取终态结果

收到终态事件后：

```bash
curl -sS "$AUTOML_API/v1/runs/run_01/result" \
  -H "Authorization: Bearer $AUTOML_TOKEN"
```

公开契约保留三种 `model_disposition` 语义：

- `ELIGIBLE_MODEL_AVAILABLE`：质量门禁通过，返回 `model_id`；
- `NO_ELIGIBLE_MODEL`：流程成功，但模型未达到业务/统计门槛；
- `INCOMPLETE`：失败、取消或过期，仍返回已经提交的部分输出。

当前 local durable profile 的成功训练固定返回 `NO_ELIGIBLE_MODEL`；
`ELIGIBLE_MODEL_AVAILABLE` 是后续生产门禁与模型注册实现预留的契约分支。

## 6. 下载大型产物

先通过 API 获取稳定 artifact 元数据，再签发短期下载票据：

```bash
curl -sS -X POST "$AUTOML_API/v1/artifacts/art_99:download" \
  -H "Authorization: Bearer $AUTOML_TOKEN" \
  -H "Idempotency-Key: download-art-99-0001"
```

票据固定有效 900 秒，并要求下载请求带上票据返回的 `If-Match: <etag>`。每个下载/Range 请求
开始时校验票据；已建立的响应可以完成。票据过期后使用新的 `Idempotency-Key` 重新调用该 API，
并用返回的相同 `etag` 和 `Range` 从已完成字节继续；复用旧幂等键只会重放旧票据。下载完成后
校验响应中的 `X-Content-SHA256` 和 `Content-Length`；不要长期保存短期 URL。Python SDK 的
`download_artifact_file()` 会自动完成 `.part` 续传、Range、ETag、大小和 SHA-256 校验。

M2 的真实模型与报告产物可以这样下载：先从 `MODEL_CARD` 或 `RUN_REPORT` 的 `artifact_refs`
取 `artifact_id`，再调用上面的 ticket API。即使 Run 成功，`model_disposition` 仍会是
`NO_ELIGIBLE_MODEL`，因为本地 slice 不执行生产质量门禁。

## 7. 外部 Agent 平台接入

本服务不调用 LLM。Agent 平台先读取机器可解析的能力清单：

```bash
curl -sS "$AUTOML_API/v1/agent/manifest" \
  -H "Authorization: Bearer $AUTOML_TOKEN"
```

只有使用 `policy.allow_external_llm=true` 创建的 Run 才能读取 Agent 上下文：

```bash
curl -sS "$AUTOML_API/v1/runs/run_01/agent-context?output_limit=20" \
  -H "Authorization: Bearer $AUTOML_TOKEN"

curl -sS "$AUTOML_API/v1/runs/run_01/agent-actions" \
  -H "Authorization: Bearer $AUTOML_TOKEN"
```

`agent-context` 返回 Run 快照、开放 DecisionPacket、最近输出引用和事件水位；
`agent-actions` 只返回对 canonical `answer/pause/resume/cancel` 端点的引用。平台必须按 descriptor
中的 `operation_id`、`href`、`If-Match` 和请求 Schema 调用原端点，不存在通用
`execute_agent_action` 写入接口。

Bearer 只能保存在平台 tool executor，不能传入 LLM。上下文不包含原始数据行，但正类值、
DecisionPacket 选项、列名和问题文本仍可能含数据派生内容；响应以
`contains_raw_dataset_rows=false`、`may_include_dataset_derived_values=true` 和
`dataset_derived_text_trust=UNTRUSTED` 明示这一边界。当前本地 profile 没有生产 DLP。

`budget.max_llm_tokens` 仅为 v1 兼容保留字段，本后端不消费它并始终报告
`llm_tokens.used=0`。Agent 平台必须自行限制 LLM token、费用和调用次数。
完整平台边界见 [外部 Agent 平台接入契约](external-agent-integration.md)。

## 8. Python SDK 完整端到端案例

以下案例假设数据为单表 CSV，字段包含：

- `customer_id`：客户标识。当前示例不把它作为特征解释，真实生产前应评估是否需要删除或哈希化。
- `tenure_months`、`monthly_fee`、`support_tickets`：普通特征。
- `plan_type`：类别特征。
- `churned`：二分类目标列，正类为 `1`。

任务设置：

- 任务类型：二分类。
- split：API 内部使用 sealed holdout，开发集内做 bounded CV/验证。
- 主要指标：`roc_auc`。
- seed：后端内部固定/请求传入的 seed 用于可复现评估；公开 API 当前不暴露自定义 seed 字段。
- 泄漏风险：上传前应移除发生在预测时点之后才知道的字段，例如 `cancel_date`、`refund_after_churn`。
- 版本：API/SDK 0.7.0，标准后端为 scikit-learn、AutoGluon Tabular、TabPFN。
- 限制：当前 artifact 仅为离线评估产物；成功 Run 不代表生产可部署模型。

```python
from pathlib import Path

from automl_sdk import AutoMLClient

api_url = "http://127.0.0.1:8000"
token = "local-development-token"
dataset_path = Path("customer_churn.csv")

with AutoMLClient(api_url, token=token) as api:
    manifest = api.get_agent_manifest()
    available_backend_ids = {
        backend["backend_id"]
        for backend in manifest["backends"]
        if backend["available"]
    }
    backend_id = "sklearn"
    if backend_id not in available_backend_ids:
        raise RuntimeError(f"{backend_id} is not available in this runtime")

    dataset = api.upload_dataset_file(dataset_path, name="customer-churn")

    run = api.create_run(
        dataset_version_id=dataset["dataset_version_id"],
        objective={
            "backend_id": backend_id,
            "target_column": "churned",
            "task_type": "BINARY_CLASSIFICATION",
            "positive_class": 1,
            "iid_confirmed": True,
            "primary_metric": "roc_auc",
            "business_context": "预测未来 30 天内可能流失的客户，用于人工运营优先级排序。",
        },
        autonomy={"mode": "GUIDED", "production_deploy": "DISABLED"},
        policy={"allow_pii": False, "allow_external_llm": False, "risk_tier": "STANDARD"},
        budget={
            "max_trials": 3,
            "max_compute_credits": 1,
            "max_wall_time_seconds": 3600,
            "max_llm_tokens": 0,
        },
        idempotency_key="run-customer-churn-sklearn-0001",
    )

    for event in api.stream_run_events(run["run_id"], after_seq=run["snapshot_seq"]):
        print(event["seq"], event["type"])

    result = api.wait_for_result(run["run_id"])
    print(result["outcome"], result["model_disposition"], result["backend_id"])

    outputs = list(api.iter_outputs(run["run_id"], types=["RUN_REPORT", "MODEL_CARD"]))
    for output in outputs:
        for artifact in output["artifact_refs"]:
            filename = f"{output['type'].lower()}-{artifact['artifact_id']}.artifact"
            api.download_artifact_file(artifact["artifact_id"], filename)
```

如果 `target_column`、`iid_confirmed` 或 `positive_class` 不确定，可把对应字段设为 `null` 或省略。
API 会在需要时返回 `WAITING_USER`，SDK 可这样处理中断：

```python
packet = api.wait_for_question(run["run_id"])
answers = {}
for question in packet["questions"]:
    if question["question_id"] == "q_target":
        answers["q_target"] = "churned"
    elif question["question_id"] == "q_iid":
        answers["q_iid"] = True
    elif question["question_id"] == "q_positive_class":
        answers["q_positive_class"] = 1

api.answer_and_wait(run["run_id"], packet, answers)
result = api.wait_for_result(run["run_id"])
```

## 9. 三个标准后端案例

### 9.1 scikit-learn 二分类

适用场景：通用 CPU 环境、轻量依赖、需要可重复的 baseline。artifact 为受信 store 内的 `joblib`
pipeline。

```json
{
  "dataset_version_id": "dsv_123",
  "objective": {
    "backend_id": "sklearn",
    "target_column": "churned",
    "task_type": "BINARY_CLASSIFICATION",
    "positive_class": 1,
    "iid_confirmed": true,
    "primary_metric": "roc_auc",
    "business_context": "客户流失预测"
  },
  "autonomy": {"mode": "GUIDED", "production_deploy": "DISABLED"},
  "policy": {"allow_pii": false, "allow_external_llm": false, "risk_tier": "STANDARD"},
  "budget": {
    "max_trials": 3,
    "max_compute_credits": 1,
    "max_wall_time_seconds": 3600,
    "max_llm_tokens": 0
  }
}
```

### 9.2 AutoGluon 二分类或回归

适用场景：仍是单表 tabular，但希望让 AutoGluon 在受控时间和 CPU 预算内做 bounded model
selection。artifact 为 deployment-only predictor 目录的 `tar.gz`，只能在可信环境用兼容 AutoGluon
runtime 加载。

创建 Run 前先检查 manifest：

```bash
curl -sS "$AUTOML_API/v1/agent/manifest" \
  -H "Authorization: Bearer $AUTOML_TOKEN" |
  python -m json.tool
```

若 `autogluon.available=true`，提交：

```json
{
  "dataset_version_id": "dsv_456",
  "objective": {
    "backend_id": "autogluon",
    "target_column": "is_fraud",
    "task_type": "BINARY_CLASSIFICATION",
    "positive_class": 1,
    "iid_confirmed": true,
    "primary_metric": "roc_auc",
    "business_context": "交易欺诈离线评估"
  },
  "autonomy": {"mode": "GUIDED", "production_deploy": "DISABLED"},
  "policy": {"allow_pii": false, "allow_external_llm": false, "risk_tier": "STANDARD"},
  "budget": {
    "max_trials": 1,
    "max_compute_credits": 1,
    "max_wall_time_seconds": 600,
    "max_llm_tokens": 0
  }
}
```

回归任务只需调整：

```json
{
  "objective": {
    "backend_id": "autogluon",
    "target_column": "monthly_revenue",
    "task_type": "REGRESSION",
    "iid_confirmed": true,
    "primary_metric": "root_mean_squared_error"
  }
}
```

### 9.3 TabPFN readiness 与运行

适用场景：小数据 tabular 评估。TabPFN 当前需要 operator 明确接受模型权重许可，并提供
`TABPFN_TOKEN` 或 `AUTOML_TABPFN_MODEL_PATH`。即使训练运行成功，当前 API 也只返回 data-free
evaluation metadata，不导出可加载 fit-state。

Docker/Compose 环境变量示例：

```bash
export AUTOML_TABPFN_LICENSE_ACCEPTED=true
export TABPFN_TOKEN=prior-labs-token
# 或离线 checkpoint：
export AUTOML_TABPFN_MODEL_PATH=/var/lib/automl/tabpfn-cache/checkpoint.ckpt
```

先看 readiness：

```python
from automl_sdk import AutoMLClient

with AutoMLClient("http://127.0.0.1:8000", token="local-development-token") as api:
    tabpfn = next(
        backend for backend in api.list_backends()
        if backend["backend_id"] == "tabpfn"
    )
    print(tabpfn["installed"], tabpfn["available"], tabpfn["unavailable_reason"])
```

当 `available=true` 时提交小数据运行：

```json
{
  "dataset_version_id": "dsv_789",
  "objective": {
    "backend_id": "tabpfn",
    "target_column": "label",
    "task_type": "BINARY_CLASSIFICATION",
    "positive_class": 1,
    "iid_confirmed": true,
    "primary_metric": "roc_auc",
    "business_context": "小样本二分类评估"
  },
  "autonomy": {"mode": "GUIDED", "production_deploy": "DISABLED"},
  "policy": {"allow_pii": false, "allow_external_llm": false, "risk_tier": "STANDARD"},
  "budget": {
    "max_trials": 1,
    "max_compute_credits": 1,
    "max_wall_time_seconds": 600,
    "max_llm_tokens": 0
  }
}
```

若返回 `available=false` 且 `unavailable_reason=MODEL_LICENSE_NOT_ACCEPTED`，这是预期保护行为：
接入方应提示运维完成许可和权重配置，或改选 `sklearn`/`autogluon`。

## 10. 外部 Agent 平台调用案例

外部 Agent 平台的职责是：持有凭据、调用 API、把结构化上下文交给 LLM、校验 LLM 结构化输出、
再调用 canonical API。AutoML API 不接收自由文本工具指令，也不保存 LLM prompt 或推理过程。

```python
from automl_sdk import AutoMLClient

with AutoMLClient("http://127.0.0.1:8000", token="platform-service-token") as backend:
    manifest = backend.get_agent_manifest()
    assert manifest["service_role"] == "AUTOML_EXECUTION_BACKEND"
    assert manifest["internal_llm_calls"] is False

    dataset = backend.upload_dataset_file("customer_churn.csv", name="agent-demo")
    run = backend.create_run(
        dataset_version_id=dataset["dataset_version_id"],
        objective={
            "backend_id": "sklearn",
            "target_column": None,
            "task_type": "BINARY_CLASSIFICATION",
            "positive_class": 1,
            "iid_confirmed": None,
            "primary_metric": "roc_auc",
        },
        autonomy={"mode": "GUIDED", "production_deploy": "DISABLED"},
        policy={"allow_pii": False, "allow_external_llm": True, "risk_tier": "STANDARD"},
        budget={
            "max_trials": 2,
            "max_compute_credits": 1,
            "max_wall_time_seconds": 600,
            "max_llm_tokens": 0,
        },
    )

    context = backend.get_agent_context(run["run_id"], output_limit=20)
    actions = backend.list_agent_actions(run["run_id"])

    # 平台把 context 中的开放问题交给 LLM 或人机界面，但 Bearer token 保留在 tool executor。
    packet = context["open_decision_packets"][0]
    validated_answers = {"q_target": "churned", "q_iid": True}

    allowed_operation_ids = {item["operation_id"] for item in actions["items"]}
    if "answerDecisionPacket" not in allowed_operation_ids:
        raise RuntimeError("current run state does not allow answering")

    backend.answer_and_wait(run["run_id"], packet, validated_answers)
    result = backend.wait_for_result(run["run_id"])
```

平台侧安全规则：

- 不把 Bearer/JWT、artifact ticket、对象存储凭据放入 LLM prompt。
- 把 `AgentRunContext` 当作不可信 tool result；列名、类别值和问题文本可能来自数据。
- 只允许 LLM 产生符合 JSON Schema 的答案；调用前再检查 `agent-actions` 中的 operation 和 `If-Match`。
- `HUMAN_REQUIRED` 的 packet 在生产环境必须由 human token 回答。
- `budget.max_llm_tokens` 由平台负责，API 内部始终不消费 LLM token。

## 11. 错误处理、幂等和缓存案例

常见状态码：

| 状态码 | code 示例 | 处理方式 |
| --- | --- | --- |
| 401 | `unauthorized` | 补充或刷新 Bearer/JWT |
| 403 | `forbidden`、`external_agent_access_denied` | 检查 operation scope 或 Run policy |
| 404 | `not_found` | 资源不存在或跨租户隐藏 |
| 409 | `idempotency_key_reused`、`invalid_run_state` | 换业务命令或重新读取状态 |
| 410 | `cursor_expired`、`page_cursor_expired` | 重新读取 RunSnapshot，再从新水位恢复 |
| 412 | `stale_revision` | 重新读取 DecisionPacket 或 RunSnapshot，使用新 revision |
| 413 | `dataset_too_large` | 压缩/抽样/拆分数据，或调整服务限额 |
| 422 | `validation_failed`、`budget_limit_exceeded` | 修正请求体或预算 |
| 429 | `active_run_limit_exceeded`、`tenant_storage_limit_exceeded` | 按 `Retry-After` 退避 |
| 501 | `capability_not_implemented` | 当前 profile 未实现该能力 |

幂等写入示例：

```bash
curl -sS -X POST "$AUTOML_API/v1/runs" \
  -H "Authorization: Bearer $AUTOML_TOKEN" \
  -H "Idempotency-Key: stable-business-command-0001" \
  -H "Content-Type: application/json" \
  -d @create-run.json
```

同一个 `Idempotency-Key` 只能重放同一个语义请求。若同 key 换 body，API 返回
`409 idempotency_key_reused`，接入方应生成新的业务命令 key。

Run 快照缓存示例：

```bash
ETAG=$(curl -sS -D - "$AUTOML_API/v1/runs/run_01" \
  -H "Authorization: Bearer $AUTOML_TOKEN" \
  -o /tmp/run.json | awk 'tolower($1)=="etag:" {print $2}' | tr -d '\r')

curl -sS -i "$AUTOML_API/v1/runs/run_01" \
  -H "Authorization: Bearer $AUTOML_TOKEN" \
  -H "If-None-Match: $ETAG"
```

注意：上面的 HTTP ETag 只用于缓存。回答 DecisionPacket 时必须使用 `wait_set_revision`：

```bash
curl -sS -X POST "$AUTOML_API/v1/runs/run_01/decision-packets/ws_01:answer" \
  -H "Authorization: Bearer $AUTOML_TOKEN" \
  -H "Idempotency-Key: answer-ws-01-0001" \
  -H 'If-Match: "3"' \
  -H "Content-Type: application/json" \
  -d '{"answers":[{"question_id":"q_target","value":"churned"}]}'
```

## 12. Artifact 格式和下载建议

| 后端 | artifact 语义 | 格式 | 使用限制 |
| --- | --- | --- | --- |
| `sklearn` | 离线评估 pipeline | `joblib` | 只从可信 artifact store 加载 |
| `autogluon` | deployment-only predictor archive | `tar.gz` | 需要兼容 AutoGluon runtime，只在可信环境加载 |
| `tabpfn` | data-free evaluation metadata | JSON | `exportable=false`，不含 fit-state 或训练数据 |

下载建议：

- 总是先创建 download ticket，再使用 ticket 中的 `required_headers`。
- 校验 `ETag`、`Content-Length` 和 `X-Content-SHA256`。
- 大文件使用 SDK `download_artifact_file()`，它会自动处理 `.part`、Range 和 SHA-256。
- ticket 过期后重新申请，不要长期保存短期 URL。

## 13. Webhook（后续契约，当前不可用）

下面的签名和投递规则是冻结的后续公开契约，便于客户端提前实现互操作；Milestone 2 当前所有
Webhook 管理路由返回 `501`，不会创建 endpoint，也不会投递事件。
不要把本节示例当成当前可调用的能力。

注册 Webhook 时，签名密钥只在创建响应中返回一次：

```bash
curl -sS -X POST "$AUTOML_API/v1/webhook-endpoints" \
  -H "Authorization: Bearer $AUTOML_TOKEN" \
  -H "Idempotency-Key: webhook-ci-0001" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://example.internal/automl-events",
    "event_types": ["output.committed.v1", "decision_packet.requested.v1", "run.completed.v1", "run.failed.v1", "run.canceled.v1", "run.expired.v1"]
  }'
```

接收方先将 43 字符、无填充 base64url 的 `signing_secret` 解码为 32 个 key 字节，再校验 `X-AutoML-Signature: v1=<hex>`。签名值是对 `X-AutoML-Timestamp + "." + raw HTTP body` 计算的 HMAC-SHA256；Webhook 回调不携带 API Bearer token。时间戳与本地时间相差超过 300 秒时拒绝请求，并按 `X-AutoML-Delivery-Id` 去重。

这里的 raw body 是移除 HTTP transfer framing 后、任何字符解码或 JSON 解析之前的精确字节；请求不使用 `Content-Encoding`。不得解析后重新序列化再验签。以下固定向量用于跨语言互操作测试（只测试签名，不测试 300 秒时间窗）：

```text
signing_secret = AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8
timestamp      = 1784700000
body UTF-8     = {"event":"测试","ok":true}
body hex       = 7b226576656e74223a22e6b58be8af95222c226f6b223a747275657d
signature      = v1=821c89a03f489a5e6e0ea22735d9a0f7f9a1a3dce1bb4144c19be73850fa6229
```

Python 验证核心：

```python
import base64
import hashlib
import hmac

secret = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"
timestamp = b"1784700000"
raw_body = '{"event":"测试","ok":true}'.encode("utf-8")
key = base64.urlsafe_b64decode(secret + "=")
actual = "v1=" + hmac.new(key, timestamp + b"." + raw_body, hashlib.sha256).hexdigest()
assert hmac.compare_digest(actual, "v1=821c89a03f489a5e6e0ea22735d9a0f7f9a1a3dce1bb4144c19be73850fa6229")
```

Node.js 验证核心：

```javascript
import { createHmac, timingSafeEqual } from "node:crypto";

const secret = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8";
const timestamp = Buffer.from("1784700000", "ascii");
const rawBody = Buffer.from('{"event":"测试","ok":true}', "utf8");
const message = Buffer.concat([timestamp, Buffer.from("."), rawBody]);
const actual = `v1=${createHmac("sha256", Buffer.from(secret, "base64url")).update(message).digest("hex")}`;
const expected = "v1=821c89a03f489a5e6e0ea22735d9a0f7f9a1a3dce1bb4144c19be73850fa6229";
if (!timingSafeEqual(Buffer.from(actual), Buffer.from(expected))) throw new Error("invalid signature");
```

查询失败或重试中的投递：

```bash
curl -sS "$AUTOML_API/v1/webhook-endpoints/wh_01/deliveries?status=RETRYING,EXHAUSTED" \
  -H "Authorization: Bearer $AUTOML_TOKEN"
```

非 2xx 或 10 秒超时会按 full-jitter 指数退避自动重试，最多 20 次或 72 小时。耗尽后 `EXHAUSTED` 就是租户可查询的死信记录，不另设隐藏 DLQ，并在 `exhausted_at` 后保留、允许人工重投 30 天。连续 20 次 attempt 失败后 endpoint 进入 `PAUSED_DELIVERY_FAILURES`，新事件保留为 `PENDING`。修复接收端后恢复投递：

```bash
curl -sS -X POST "$AUTOML_API/v1/webhook-endpoints/wh_01:enable" \
  -H "Authorization: Bearer $AUTOML_TOKEN" \
  -H "Idempotency-Key: enable-wh-01-0001"
```

恢复会继续 `PENDING`，但不会自动重放 `EXHAUSTED`。

修复接收端后，可请求重投。重投增加 attempt，但保持原 delivery ID：

```bash
curl -sS -X POST "$AUTOML_API/v1/webhook-endpoints/wh_01/deliveries/whd_01:redeliver" \
  -H "Authorization: Bearer $AUTOML_TOKEN" \
  -H "Idempotency-Key: redeliver-whd-01-0001"
```
