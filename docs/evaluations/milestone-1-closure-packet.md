# Managed AutoML Milestone 1 收口复评包

## 评估对象

- 类型：项目实现结果 / API 契约 / Python SDK / 测试（混合）
- 阶段：Milestone 1 最终收口
- 仓库：`/Users/wangxitao/Documents/机器学习api`
- 日期：2026-07-22
- 前次独立评审：`milestone-1-implementation-gemini.md`，92/100，结论为继续推进

## 本阶段目标与边界

本阶段目标是交付可运行的 FastAPI 骨架和同步 Python SDK，让调用者仅通过 API 完成：

`Dataset -> RunSnapshot -> Events/SSE -> DecisionPacket -> Answer -> Outputs -> RunResult -> ArtifactTicket`

当缺少任务信息时 Run 进入 `WAITING_USER`，使用者回答后继续到终态；同时支持 pause、resume、
cancel。验收范围明确限定为单进程、内存态、合成元数据。它不接收真实文件字节、不调用 LLM、
不训练模型，也不具备生产恢复或正式身份认证能力。

## 已实现结果

- FastAPI 服务、Pydantic 公共模型、合成状态机和进程内 Store。
- canonical OpenAPI 3.1 契约：`openapi/automl-api.yaml`。
- 同步 Python SDK：JSON opaque cursor 回放、SSE `Last-Event-ID` 续读、事件去重、显式 410、
  高层 wait/answer/result 辅助方法、run controls、outputs、artifact ticket。
- RFC 9457 Problem、租户+operation 幂等、请求指纹、完整 RunSnapshot 表示 ETag、
  `run_revision` / `wait_set_revision` 并发控制。
- Dataset、Run、DecisionPacket 等父资源和 synthetic tenant 隔离；跨租户资源按 404 隐藏。
- Run/DecisionPacket 状态过滤使用 ID keyset cursor，JSON event continuation 只携带 opaque cursor。
- cancel 清空 blockers、关闭 OPEN DecisionPacket、取消未完成 stage，并生成终态结果。
- README 首屏和启动日志明确披露内存易失、synthetic-only、禁止生产或真实数据使用。
- 16 个 M1 外 operation 均带 `x-maturity`；其中实际返回 501 的 11 个 operation 已精确声明公共
  `NotImplemented` Problem response。空页 placeholder 与实际 404 查询未误标。

## 本轮定向收口

1. SDK 终态过滤 SSE 使用默认重连配置并注入失败型 sleep，证明终态快照会结束流而不会重连。
2. 新增 SSE 410 回归，默认明确抛出 `EventCursorExpiredError` 及 `lost_event_range`。
3. 新增 DecisionPacket 两页回归，阻塞 packet 位于第二页，续页只发送 opaque cursor。
4. WAITING_USER 初始和 resume 后均断言 `available_actions = {ANSWER, PAUSE, CANCEL}`。
5. OpenAPI 为 11 个实际 501 operation 补齐机器可读响应声明。

## 可复验证据

- `pytest -q`：9 passed。
- `ruff check .`：All checks passed。
- `ruff format --check .`：17 files already formatted。
- `python3 -m py_compile ...`：通过。
- `git diff --check`：通过。
- Redocly recommended lint：OpenAPI valid，0 error。
- `openapi-typescript`：由 canonical OpenAPI 成功生成 3,149 行临时 TypeScript declaration。
- 运行中 `GET /healthz`：`{"status":"ok","mode":"milestone-1-synthetic"}`。
- 真实网络同步 SDK smoke：`SUCCEEDED NO_ELIGIBLE_MODEL 3 8 run.completed.v1 SUCCEEDED`，分别表示
  Run outcome、model disposition、output 数量、event 数量、末事件类型和 answer command 状态。

## 已知限制与生产化门禁

1. 所有状态仅存在单进程内存中，重启全丢；多资源状态变更不是跨进程、抗崩溃事务。
2. Bearer token 仅哈希为 synthetic tenant，不校验 JWT、过期、撤权、成员或角色。SSE 每 30 秒
   重新检查资源归属，但无法完成正式身份重新验证；接触真实数据前必须实现 OIDC/JWT、授权和撤权。
3. 上传、报告和下载 URL 均为 synthetic，不处理真实文件或 artifact 字节。
4. 工作流固定提出目标列问题以演示暂停恢复；尚无真实 TaskSpec 推断、LLM、训练、评估或模型注册。
5. SDK 公共模型主要仍是 `dict[str, Any]`；强类型模型和 `mypy --strict` 留到下一阶段。
6. 尚无 PostgreSQL、object storage、Temporal/durable worker、transactional outbox、RLS、DLP、
   deletion saga、结构化 Metrics/Tracing、故障代理测试或进程崩溃注入测试。

这些限制已在 README、启动日志和实现评审处置中明确披露，不能将本实现描述为生产 AutoML 或
可恢复托管系统。

## 请 Gemini 判断

1. 在以上明确边界内，是否可以判定“API 骨架与同步 SDK”Milestone 1 已完成；
2. 是否仍有阻断本地演示或下一阶段启动的 P0/P1 缺陷；
3. 哪些已知限制必须作为接触真实数据、外部 LLM 或生产部署前的硬门禁；
4. 给出 0-100 分、风险触发条件和最小可验证的下一阶段行动。

请严格按 M1 synthetic skeleton 目标评估，不因未实现生产持久化本身扣成目标失败；同时不要把
协议设计、测试或警告等同于真实生产可靠性。
