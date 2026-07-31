# AutoML 阶段 Callback 契约

外部 Agent 平台可以在创建 Run 时通过 `callback_uri` 绑定阶段通知地址。服务在阶段结束后向该
地址发送带 HMAC-SHA256 签名的 Webhook，并在标准事件之外提供紧凑的 `callback` 摘要，明确
当前阶段状态以及是否已经具备进入下一阶段的条件。

`callback_url` 是兼容旧客户端的弃用别名。新接入统一使用 `callback_uri`；如果两个字段同时
出现，值必须完全一致。

## 1. 注册 Callback Endpoint

Callback URI 不能作为未经注册的裸地址直接传入 Run。平台需要先注册 endpoint，并将只返回一次的
`signing_secret` 保存到 secret manager：

```bash
curl -sS -X POST "$AUTOML_API/v1/webhook-endpoints" \
  -H "Authorization: Bearer $AUTOML_TOKEN" \
  -H "Idempotency-Key: register-stage-callback-0001" \
  -H 'Content-Type: application/json' \
  -d '{
    "url": "https://agent.example.com/hooks/automl",
    "event_types": [
      "run.stage_completed.v1",
      "run.failed.v1",
      "run.canceled.v1"
    ],
    "description": "Agent platform AutoML stage receiver"
  }'
```

生产 profile 默认只接受 HTTPS 公网地址。内网 receiver 必须由部署方通过
`AUTOML_WEBHOOK_ALLOWED_CIDRS` 显式允许最小 CIDR。

## 2. 创建带 Callback 的 Run

```json
{
  "dataset_version_id": "dsv_000000000001",
  "objective": {
    "backend_id": "sklearn",
    "target_column": null,
    "task_type": "BINARY_CLASSIFICATION"
  },
  "autonomy": {
    "mode": "GUIDED",
    "production_deploy": "DISABLED"
  },
  "policy": {
    "allow_pii": false,
    "allow_external_llm": true,
    "risk_tier": "STANDARD"
  },
  "budget": {
    "max_trials": 3,
    "max_compute_credits": 1,
    "max_wall_time_seconds": 3600,
    "max_llm_tokens": 0
  },
  "callback_uri": "https://agent.example.com/hooks/automl",
  "webhook_endpoint_ids": ["wh_000000000001"]
}
```

`callback_uri` 必须与当前租户的 ACTIVE endpoint 完全一致。URL 未注册返回
`422 callback_endpoint_not_registered`；URI 与 endpoint ID 不一致返回
`422 callback_endpoint_mismatch`。为了保证阶段成功、失败和取消都能送达，endpoint 的
`event_types` 必须包含 `*`，或同时包含 `run.stage_completed.v1`、`run.failed.v1` 和
`run.canceled.v1`；缺失时创建 Run 返回 `422 callback_event_types_incomplete`。

Python SDK：

```python
run = api.create_run(
    request,
    callback_uri="https://agent.example.com/hooks/automl",
    webhook_endpoint_ids=["wh_000000000001"],
    idempotency_key="create-run-with-callback-0001",
)
```

## 3. 阶段名称

| API phase | Callback `stage` | 含义 |
| --- | --- | --- |
| `INGEST` | `data_read` | 读取并验证上传数据 |
| `PROFILE` | `data_analysis` | 数据结构和质量分析 |
| `PLAN` | `task_recognition` | 识别任务、冻结目标和数据划分 |
| `TRAIN` | `model_training` | baseline 和候选模型训练 |
| `EVALUATE` | `model_evaluation` | sealed holdout 评估和绘图 |
| `PACKAGE` | `result_package` | 打包模型、报告和结果引用 |

## 4. Callback 请求体

Webhook 继续使用统一签名信封。`callback` 是阶段成功、失败或取消事件的紧凑投影，`event` 是可回放
的权威事件：

```json
{
  "delivery_id": "whd_000000000001",
  "webhook_endpoint_id": "wh_000000000001",
  "attempt": 1,
  "callback": {
    "schema_version": "1.0",
    "toolname": "automl",
    "run_id": "run_000000000001",
    "event_id": "evt_000000000004",
    "seq": 4,
    "occurred_at": "2026-08-01T08:30:00Z",
    "stage": "data_analysis",
    "states": "success",
    "next_stage": false,
    "next_stage_name": "task_recognition",
    "reason": "user_input_required"
  },
  "event": {
    "event_id": "evt_000000000004",
    "run_id": "run_000000000001",
    "seq": 4,
    "run_revision": 2,
    "schema_version": "1.0",
    "occurred_at": "2026-08-01T08:30:00Z",
    "type": "run.stage_completed.v1",
    "payload": {
      "phase": "PROFILE",
      "status": "COMPLETED",
      "completed_at": "2026-08-01T08:30:00Z",
      "progress_percent": 30,
      "output_refs": [],
      "next_phase": "PLAN",
      "next_stage_ready": false,
      "reason": "USER_INPUT_REQUIRED"
    },
    "links": {
      "run": "/v1/runs/run_000000000001"
    }
  }
}
```

`next_stage` 是 JSON boolean，不是字符串。其语义是“当前是否已经具备进入
`next_stage_name` 的条件”，并不表示下一阶段是否存在。

## 5. 常见状态

阶段成功且可以继续：

```json
{
  "toolname": "automl",
  "stage": "data_read",
  "states": "success",
  "next_stage": true,
  "next_stage_name": "data_analysis",
  "reason": null
}
```

阶段成功但需要 Agent 或用户补充信息：

```json
{
  "toolname": "automl",
  "stage": "data_analysis",
  "states": "success",
  "next_stage": false,
  "next_stage_name": "task_recognition",
  "reason": "user_input_required"
}
```

数据读取失败：

```json
{
  "toolname": "automl",
  "stage": "data_read",
  "states": "failed",
  "next_stage": false,
  "next_stage_name": null,
  "reason": "data_error"
}
```

最后阶段完成：

```json
{
  "toolname": "automl",
  "stage": "result_package",
  "states": "success",
  "next_stage": false,
  "next_stage_name": null,
  "reason": "workflow_completed"
}
```

`callback` 实际还包含 `schema_version`、`run_id`、`event_id`、`seq` 和 `occurred_at`。上面的短例只
突出业务字段。

## 6. 接收和恢复要求

Callback receiver 必须：

1. 在解析 JSON 前，使用 endpoint 的 signing secret 对
   `ASCII(X-AutoML-Timestamp) + "." + raw_body` 验证 HMAC-SHA256。
2. 拒绝超过 300 秒重放窗口的请求。
3. 使用 `X-AutoML-Delivery-Id` 做持久化唯一约束；服务提供至少一次投递，不承诺只投递一次。
4. 将 delivery ID 和事件原子落库后再返回 2xx。
5. 使用 `event.seq` 检查缺口，并通过 `GET /v1/runs/{run_id}/events` 补拉。
6. 将 RunSnapshot、Output 和 Result 作为权威状态；Callback 用于唤醒，不替代状态查询。

服务对非 2xx、网络错误和超时自动重试。Callback 暂时不可达不会使 AutoML Run 失败。完整接收端
代码见 [`examples/python/webhook_receiver.py`](../examples/python/webhook_receiver.py)。
