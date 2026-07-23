# Managed AutoML Milestone 2a 最终评估包

## 评估对象

- 类型：API / SDK / durable workflow / tabular ML 引擎的阶段实现结果
- 日期：2026-07-23
- 阶段目标：达到“可从零安装、可在单机本地通过 API/SDK 完成真实小数据 AutoML
  闭环，并在需要用户信息时中断、回答后继续”。
- 非阶段目标：多租户生产托管、横向扩展、外部 LLM Planner、生产模型注册或部署。

## 可复验事实

1. 默认 `create_app()` 使用 SQLite WAL/FULL、`LocalBlobStore`、
   `DurableWorkflowService` 和单活 `LocalExecutionWorker`。
2. 真实 CSV/Parquet 字节上传会校验分片 ETag、总大小和 SHA-256；artifact 支持签名票据、
   Range 续传、ETag 与 SHA-256 校验。
3. workflow 支持 `QUEUED -> PROFILE -> WAITING_USER -> TRAIN -> EVALUATE -> PACKAGE -> TERMINAL`；
   `DecisionPacket` 会请求 target、i.i.d. 假设和必要时的 positive class，Answer command 异步接受后
   从 durable checkpoint 继续。
4. 固定 sklearn 白名单支持单表 i.i.d. 二分类/回归，包含 fold 内预处理、精确重复样本分组、
   sealed holdout、固定 seed、Dummy baseline、线性模型和随机森林。
5. `RunBudget.max_trials` 会限制实际候选数；结果明确为评估用，
   `model_disposition=NO_ELIGIBLE_MODEL` 且 `production_eligible=false`。
6. Worker shutdown 使用 `lease_generation + control_epoch` fencing 将租约立即放回 `RETRY`；
   SQLite 重开后 Run/Result/Artifact 可恢复。
7. 真实 API E2E 覆盖上传、DecisionPacket、异步 Answer、训练、结果、artifact 下载和重开；
   真实 SDK E2E 覆盖高层上传/等待/回答/结果/下载链路。
8. 隔离 venv 从零 `pip install -e '.[dev]'` 成功，`pip check` 无 broken requirements；
   全量 `pytest -q` 为 34 passed，`ruff check .`、`ruff format --check .`、`py_compile` 和
   Redocly OpenAPI lint 全部通过。

## 已知边界和风险

1. Bearer token 只是本地合成身份，没有 OIDC/JWT/API-key 验证、RBAC/RLS、审计、DLP 或 KMS。
2. SQLite 全状态 checkpoint、本地对象目录和单活 worker 只适合单进程本地 profile，不支持 HA、
   多 worker 或水平扩展。
3. 租约暂无 heartbeat；`max_wall_time_seconds` 未在可强制终止的进程边界执行。
4. CSV/Parquet 在训练前全量读入内存，尚无上传大小/内存硬上限。
5. SDK 上传目前是单分片；`joblib` 产物只允许从受信本地 artifact store 加载。
6. 无 Webhook 投递、生产观测性/告警/备份/灾备/chaos，也无生产模型门禁、注册和部署。
7. 尚未调用外部 LLM；当前 DecisionPacket 由受限确定性规则生成。

## 请评估

1. 是否足以称为“本地单机开发/演示可用”？
2. 是否可以称为“生产可用”？
3. 将该系统推进到原始目标“LLM 托管 AutoML 且可对外提供 API”前，优先级最高的验收门禁是什么？
4. 请分别给出当前本地可用性和生产成熟度的 0-100 分。
