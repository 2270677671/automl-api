# API-first 阶段评审包：LLM 托管 AutoML API v0.4

## 评估目标

评估一个面向外部使用者的托管 AutoML API 契约。用户上传表格数据后，LLM 负责规划、解释和在受控策略内选择机器学习步骤；确定性 workflow 和白名单 worker 执行。用户不得直接访问 Temporal、MLflow、worker、数据库或对象存储。

本阶段重点不是评价模型算法，而是确认：中间过程、阶段结果、人工问题、失败与最终结果能否全部通过稳定 API 消费；当系统需要用户信息时能否暂停，并在回答后从 checkpoint 可靠继续。

权威文件：

- `docs/automl-api-design.md`：完整架构设计 v0.4。
- `openapi/automl-api.yaml`：OpenAPI 3.1 公共契约，33 paths、36 operations、113 schemas。
- `docs/api-usage.md`：端到端 curl 调用流程。

## 外部读取模型

1. `RunSnapshot`：可变快照，返回 phase/status/progress、预算、阻塞项、可用动作、最新输出引用、`run_revision`、`snapshot_seq` 和 `retained_from_seq`。
2. `RunEvent`：追加事件流，Run 内 `seq` 单调，事件采用 `oneOf + discriminator`。覆盖阶段、进度、输出提交、用户问题、审批、实验指标和所有终态。
3. `OutputResource`：已经提交的不可变结构化输出。顶层以 `type` 判别为 11 个 schema 分支，每个分支同时固定 `payload.kind`。
4. `RunResult`：终态清单，以 `model_disposition` 判别并强制三种合法组合。

原始 worker 日志不公开。用户只能获得脱敏、结构化的 `LOG_SUMMARY`，不含原始样本、列值、Prompt、栈或凭据。大型内容用稳定 `artifact_id` 引用，客户端下载前通过 API 获取固定 900 秒的 opaque `DownloadTicket`；过期后用新幂等键换票，并按稳定 ETag 做 Range 续传。

## 用户调用闭环

```text
POST dataset -> multipart upload -> finalize
POST run -> 202 RunSnapshot(snapshot_seq, ETag)
GET run / stages / outputs / experiments
GET events?after_seq=snapshot_seq as JSON replay or SSE
  output.committed.v1 -> GET /outputs/{output_id}
  decision_packet.requested.v1 -> POST .../{wait_set_id}:answer
  approval.requested.v1 -> POST .../{approval_id}:decide
answer/approval/pause/resume/cancel -> 202 CommandReceipt -> GET /commands/{id}
terminal event -> GET /result
GET artifact metadata -> POST artifact:download -> DownloadTicket
```

回答 API 使用 `Idempotency-Key` 和 `If-Match`，后者只匹配 `DecisionPacket.wait_set_revision`；审批匹配 `Approval.evidence_version`，暂停/恢复才匹配 Run 控制 revision。不相关更新不会使开放问题的回答冲突。接受整个 wait-set 后返回异步 `CommandReceipt`，命令成功时 workflow 自动从 checkpoint 恢复。过期目标版本返回 `412`，非法状态返回 `409`。取消是无需 `If-Match` 的单调幂等紧急命令。

## 中间输出契约

`OutputResource` 支持：

- `DATA_QUALITY_REPORT`
- `TASK_SPEC`
- `SPLIT_MANIFEST`
- `BASELINE_RESULT`
- `COST_ESTIMATE`
- `TRIAL_RESULT`
- `EVALUATION_REPORT`
- `LOG_SUMMARY`
- `MODEL_CARD`
- `RUN_REPORT`
- `FAILURE_REPORT`

每个输出包含 `output_id`、`schema_version`、`run_id`、`run_revision`、`created_seq`、phase/state、摘要、专用 payload、lineage、artifact 引用、`supersedes` 和创建时间。`PARTIAL` 是已提交且不可变的阶段快照，不是半写对象；更新产生新 output ID，并由新资源的 `supersedes` 指向旧资源。

OpenAPI 结构等价于：

```yaml
OutputResource:
  oneOf: [DataQualityReportOutput, TaskSpecOutput, ..., FailureReportOutput]
  discriminator: {propertyName: type}
TrialResultOutput:
  allOf:
    - BaseOutputResource
    - required: [type, payload]
      properties:
        type: {const: TRIAL_RESULT}
        payload: {ref: TrialResultPayload} # payload.kind const TRIAL_RESULT
```

## 终态结果契约

```text
ELIGIBLE_MODEL_AVAILABLE:
  outcome=SUCCEEDED, partial=false, eligible_model=ModelRef, reason=null

NO_ELIGIBLE_MODEL:
  outcome=SUCCEEDED, partial=false, eligible_model=null, reason=ResultReason

INCOMPLETE:
  outcome=FAILED|CANCELED|EXPIRED, partial=true,
  eligible_model=null, reason=ResultReason
```

`ResultReason` 必须包含稳定 code、用户安全 message、`retriable`、failed gates、evidence refs 和 remediation。终态转换、`RunResult`、ResultManifest、输出引用和 artifact 元数据在同一逻辑提交边界原子可见；禁止先看到终态再得到空结果。失败或取消不会丢弃已经提交的中间输出，也绝不产生 eligible candidate。

## 重连、一致性与演进

- 客户端先读 `RunSnapshot.snapshot_seq`，再读取 seq 大于该值的 events，消除查询与订阅之间的空窗。
- JSON 首次事件读取冻结 `high_watermark`，后续只使用包含该水位和过滤条件的 opaque cursor。`(run_id, seq)` 和 `event_id` 唯一，水位只能推进到最大连续已提交 seq，不能跨越投影缺口。
- SSE 先回放 backlog，再原子切换 live；用 seq 作为 SSE id，支持 `Last-Event-ID`，最长 15 秒心跳，最长 30 秒权限重验。
- SSE 的 `Last-Event-ID` 无条件覆盖 `after_seq`，以兼容 EventSource 自动重连时保留初始 query。两者缺省时 JSON 从保留边界读取，SSE 只读建连后的 live。
- continuation 的下一 seq 小于保留边界时返回强类型 `410 EventCursorExpiredProblem`，强制包含 `retained_from_seq`、不可回放的 `lost_event_range` 和 `recovery={action: GET_RUN_SNAPSHOT, href}`。集合分页游标使用独立 problem，要求从第一页恢复并按资源 ID 去重。
- 写请求的幂等指纹由 method、规范化 path/query 和规范化 JSON body 的 SHA-256 构成。同 key/同指纹回放首次响应，同 key/不同指纹返回 `409 idempotency_key_reused`。
- `/v1` 只做兼容扩展；事件 type 带 `.v1`，输出带 `schema_version`；长 Run 在创建时固定契约和策略版本。

## Webhook API

- 创建时签名密钥只返回一次；它是 32 个随机字节的无填充 base64url，接收方解码后作为 HMAC key。轮换 API 返回一次新密钥，旧密钥保留固定 300 秒宽限期。
- `X-AutoML-Timestamp` 为 Unix 秒。
- `X-AutoML-Signature` 为 `v1=<64 lowercase hex>`。
- 签名为 `HMAC-SHA256(base64url_decode(secret), ASCII(timestamp) || 0x2e || raw_body_octets)`；Webhook operation 显式 `security: []`，不继承 API Bearer。接收方常量时间比较并拒绝时间偏差超过 300 秒的请求。
- 至少一次投递，delivery ID 在自动重试和人工重投中稳定，接收方按其去重。
- 非 2xx/10 秒超时使用 full-jitter 指数退避，20 次或 72 小时后 `EXHAUSTED`；它就是租户可查询的死信状态，保留并允许重投 30 天。endpoint 连续 20 次 attempt 失败后熔断为 `PAUSED_DELIVERY_FAILURES`，`:enable` 恢复 PENDING。
- 管理 API 可列出 delivery 状态，并对指定 delivery 发起新 attempt。
- SSE/Webhook 都不是事实源，掉线后用 JSON events 补齐并回查 snapshot。

## 验证结果

- Redocly recommended lint：0 error、0 warning。
- `openapi-typescript 7.13.0`：成功生成 3,107 行类型。
- Webhook 固定测试向量已分别用 Python 和 Node.js 计算，得到相同 HMAC。
- 生成结果中 `OutputResource` 是 11 分支联合，`RunResult` 是 3 分支联合，两类 cursor-expired problem 的 `recovery` 都为必填。
- `git diff --check`：通过。
- 三个独立 Agent 已分别从控制面可靠性、ML 输出治理、公共契约/Webhook 角度完成两轮审查；所有报告的 P1 均已修复。

Gemini 首轮独立评估给出 92/100 和“补证后推进”：要求冻结 Dataset 删除生命周期并精确说明 Webhook raw-body 字节。最终版本已选择并写入级联取消删除策略，同时加入 byte-level 签名定义、含中文固定向量及 Python/Node 验证代码。本次请求是对修复后版本的最终复核。

Gemini 第二轮提高到 96/100，确认契约已达到实现成熟度，同时建议进一步缩小 `If-Match` 争用、冻结 Webhook 死信策略并显式标记事件截断缺口。最终版本已分别加入 wait-set/evidence 作用域 ETag、72 小时/20 次重试与熔断恢复 API，以及 `lost_event_range.historical_events_recoverable=false`。

Gemini 最终复核给出 97/100，结论为“立即推进 API 骨架与 SDK 同步实现”。其非阻断建议中的下载票据 TTL 和死信可见性也已冻结为 900 秒及租户可查询/可重投 30 天；内部 Planner 诊断只记录结构化证据，明确禁止暴露思维链。

## 已知范围与尚未确定的业务输入

- MVP 限 CSV/Parquet、二分类/回归、离线评估和 candidate 注册，不自动生产部署。
- Dataset 删除固定采用级联取消：先撤销读取并对关联非终态 Run 发出幂等 cancel，再执行删除 saga；`202 DeletionJob.affected_run_ids` 暴露影响范围。活动 Run 不导致 409，legal hold、策略禁止或冲突删除任务才返回 409。
- 目标行业和监管等级、单文件/总数据规模、部署环境、是否允许外部 LLM、租户 SLO 与保留期尚待业务方确认。
- 这些输入会改变配额、策略和基础设施配置，但不应改变本阶段公共资源模型和恢复语义。

## 请求独立评审

请重点判断以下问题，并优先报告可执行的 P0/P1，而不是泛化表扬：

1. API 是否真正覆盖了使用者需要观察的中间过程、阶段结果、用户介入和终态结果。
2. 是否仍存在 schema 允许的矛盾状态、断线漏事件、重复副作用或无法恢复的路径。
3. Webhook 签名、轮换、去重和重投是否足够明确且可跨语言实现。
4. 哪些缺口必须在开始实现前冻结，哪些可以留到部署配置阶段。
5. 给出是否可以进入 API skeleton 实现的结论，以及最多 5 个最重要的下一步验收动作。
