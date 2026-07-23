# Managed AutoML Milestone 2a 本地 Durable 评估包

## 评估对象与目标

- 类型：项目实现结果 / API / Worker / ML 引擎 / 测试（混合）
- 阶段：Milestone 2a 本地 durable 纵向切片
- 日期：2026-07-23
- 目标：让默认 `create_app()` 在单机完成真实数据上传、持久化 Run、人工中断与回答、异步训练、
  结构化输出、artifact 下载和进程重启恢复。

本阶段不是生产就绪目标。

## 已验证事实

- 默认应用使用 SQLite WAL/FULL、`LocalBlobStore`、`DurableWorkflowService` 和
  `LocalExecutionWorker`。
- lifespan 启动时修复缺失 execution job 并启动 worker；shutdown 时停止 worker、关闭 SQLite。
- worker 被取消时用 `lease_generation + control_epoch` fencing 立即将 job 置回 `RETRY`。
- 流程为 `PROFILE -> DecisionPacket -> RESOLVE_TASK -> TRAIN -> EVALUATE -> PACKAGE`。
- 固定 sklearn allowlist 支持单表 i.i.d. 二分类/回归，包含 fold 内预处理、exact duplicate grouping、
  sealed holdout、固定 seed、dummy baseline、线性模型与随机森林。
- `max_trials` 会限制实际候选数，端到端测试验证 `used <= limit`。
- 真实 E2E 覆盖 CSV 上传、target/i.i.d. DecisionPacket、异步 Answer、真实训练、结果和 artifact、
  SHA-256 下载校验，以及关闭并重开应用后的 Run/Result 恢复。
- 可复验证据：`pytest -q` 为 34 passed；`ruff check .` 通过；Python `py_compile` 通过。

## 已知限制

1. synthetic bearer，无正式身份、授权/RLS、审计或 DLP。
2. SQLite 全状态 checkpoint + 单活 worker，不是 PostgreSQL 事务投影，不可水平扩容。
3. 无 lease heartbeat；训练超过 30 秒可能触发 fencing。`max_wall_time_seconds` 尚无硬中断。
4. CSV/Parquet 全量读入内存；尚无上传大小或运行内存硬上限。
5. joblib 仅可作为可信本地 artifact，不能加载不可信模型包。
6. 无 webhook、生产部署、LLM planner、生产可观测性、灾备或 chaos 验证。
7. group/time/multiclass 尚不支持。

## 请独立评估

判断该阶段能否称为本地开发/演示可用、能否称为生产可用，并列出进入 M2b/M3 前优先级最高的
可靠性和契约缺口及其验收标准。
