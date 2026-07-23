# Managed AutoML Milestone 1 实现评审包

## 评估对象

- 类型：项目实现结果 / API 架构 / SDK / 可靠性测试（混合）
- 当前阶段：Milestone 1 空壳暂停恢复
- 仓库：`/Users/wangxitao/Documents/机器学习api`
- 评估日期：2026-07-22

## 目标与成功标准

本阶段只实现单进程、内存态、合成元数据闭环，不接收真实文件字节、不调用 LLM、不训练模型。
目标是让使用者仅通过公共 API/同步 Python SDK 完成：

1. Dataset 上传会话与 finalize；
2. 创建 Run，读取 RunSnapshot 与连续事件；
3. 获取结构化中间 Output；
4. 在缺少目标列时收到 DecisionPacket，提交完整 wait-set 后自动继续；
5. 获取 CommandReceipt、终态 RunResult、Artifact 与 900 秒下载票据；
6. 对 mutating API 提供租户+operation 作用域幂等；
7. 对 Run/DecisionPacket 提供作用域正确的并发控制；
8. SDK 用不超过 20 行的核心调用完成闭环。

## 已提交实现

- FastAPI 入口：`apps/api/src/automl_api/app.py`
- 公开 Pydantic 模型：`apps/api/src/automl_api/models.py`
- 合成工作流：`apps/api/src/automl_api/workflow.py`
- 线程安全内存 Store：`apps/api/src/automl_api/store.py`
- 协议工具：`apps/api/src/automl_api/protocol.py`
- 同步 SDK：`packages/python_sdk/src/automl_sdk/client.py`
- RFC 9457 错误分类：`apps/api/src/automl_api/errors.py`、`packages/python_sdk/src/automl_sdk/exceptions.py`
- canonical 契约：`openapi/automl-api.yaml`
- 集成测试：`tests/test_api_end_to_end.py`、`tests/test_sdk.py`

## 已实现的公共行为

- 20 个 Milestone 1 核心 operation：dataset create/sign/finalize/get；Run create/list/get/stages；
  events JSON/SSE；outputs list/get；DecisionPacket list/answer；pause/resume/cancel；command/result；
  artifact/get-download-ticket。
- 未来契约边界：experiment/approval 读取为空页；model 读取 404；Webhook/删除管理显式返回
  `501 not_implemented_in_milestone_1`，README 已声明能力边界。
- 同键同指纹回放第一次成功或业务 Problem 的原 status/body/关键 headers；同键不同指纹为 409。
- Run 条件读取 ETag 是完整 RunSnapshot 内容哈希；pause/resume 的 If-Match 独立使用响应体
  `run_revision`；answer 使用 `wait_set_revision`。
- JSON event continuation 只发送 opaque cursor，固定首次 high-watermark；SSE 中 Last-Event-ID
  覆盖 after_seq，15 秒心跳，按 seq/event_id 去重，并在活跃/空闲路径每 30 秒复查资源归属。
- 410 event cursor body 包含 `lost_event_range` 和 `GET_RUN_SNAPSHOT` 恢复动作；SDK 默认显式抛出，
  只有调用者选择 `recover_expired=True` 时才跳到快照边界。
- Run 与 DecisionPacket 状态过滤分页使用不可变资源 ID keyset，避免状态变化导致 offset 漏项。
- SDK 同时提供有限 JSON replay 和 `stream_run_events()` SSE 断线续读；过滤掉终态事件时通过
  EOF 后的最新 RunSnapshot 正常结束，不会无限重连。
- cancel 不需要 If-Match；取消后 OPEN DecisionPacket 被 SUPERSEDED，Run blockers 清空，未完成
  stages 标 CANCELED；相同幂等请求回放，新的终态 cancel 请求返回 409。

## 验证事实

- `pytest -q`：8 passed。
- 覆盖：完整 API/SDK 闭环、幂等成功/错误原样回放、同 key 异体冲突、跨租户 404、
  Dataset/Run 304、wait-set/run stale revision、JSON cursor-only 多页、连续 event seq、SSE 终态回放、
  SSE 过滤终态、410 丢失区间、Output/DecisionPacket 契约 nullability、pause/resume/cancel、
  cancel blocker 清理、动态状态过滤 keyset、snapshot_seq 独立变化时 ETag 失效。
- `ruff check .`：通过。
- `ruff format --check .`：17 files already formatted。
- `python3 -m py_compile ...`：通过。
- Redocly：`openapi/automl-api.yaml` valid。
- `openapi-typescript`：成功生成 3,107 行 TypeScript declaration（临时验证产物）。
- `python3 -m pip install -e '.[dev]'`：成功。
- Uvicorn 已在 `127.0.0.1:8000` 启动；真实网络 smoke：health 200、OpenAPI 200、未认证 401
  `application/problem+json`。

## 已知限制与不得掩盖的风险

1. Store 仅为单进程内存状态；进程重启会丢数据，不具备真实 Temporal/PostgreSQL/object storage
   恢复能力。
2. answer、output/event/latest refs、terminal result/Run/event 仍是服务锁内的多步 Store 写入；对当前
   单 app 实例不可并发观察，但无法抵御跨进程并发或任意写点进程崩溃。生产化前必须改为持久事务、
   outbox/projection 或 durable workflow update。
3. Bearer token 只哈希成 synthetic tenant，不验证 JWT、过期、撤权或角色；30 秒复查只能确认当前
   内存资源归属，不能替代 M2 的身份重验。
4. 本阶段硬编码提出目标列问题，即使 objective 已给 target 也仍用于演示暂停恢复；真实“仅必要时
   中断”要在 TaskSpec 推断策略落地后实现。
5. collection cursor 尚无 TTL/410，未实现 429、真实慢消费者治理、Webhook、删除 saga、模型注册、
   真实 artifact 字节下载。
6. SDK 公共数据仍以 `dict[str, Any]` 为主，虽然包发布 `py.typed`，尚未交付由 OpenAPI 生成的强类型
   请求/响应模型；高层等待 timeout 也不能缩短正在执行的单次 HTTP 请求超时。
7. SDK 的真实断网重连、命令异步轮询和进程崩溃注入尚未通过网络故障代理测试；当前内存 workflow
   在单次回答请求内同步完成，测试验证的是协议与状态结果，不是 durable worker 恢复。
8. canonical OpenAPI 描述完整 36 operation，M1 之外的 operation 尚未在契约中统一标注 maturity/501；
   对外发布前需要 capability/profile 表达，避免生成客户端误认为全部可用。

## 请 Gemini 重点判断

1. 以上证据是否足以将“API 骨架与 SDK 同步实现”判定为 Milestone 1 阶段完成；
2. 是否存在会阻止本地/演示接入的遗漏；
3. 已知限制中哪些必须在接入真实数据或 LLM 之前升级为阻断门禁；
4. 给出 0-100 健康度评分，以及提高到下一档所需的最小、可验证行动。

请严格按“合成数据、单进程内存 Milestone 1 骨架”评估，不要把未实现生产持久化本身当成目标偏离；
但必须评价其是否被准确披露，以及是否有任何代码行为超出了这一边界。
