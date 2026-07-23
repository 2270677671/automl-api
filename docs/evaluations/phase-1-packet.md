# 阶段一评审包：LLM 托管 AutoML API 概念设计

## 评估对象

- 类型：架构与实施计划（概念阶段）
- 当前状态：空仓库，从零设计，尚无代码和运行指标
- 目标：用户上传 CSV/Parquet 后，由 LLM 托管表格型监督学习流程；遇到目标、业务语义、风险、预算或部署等无法安全自动决定的事项时暂停，获得用户回答后从 checkpoint 继续。

## 成功标准

1. LLM 能规划和解释，但不能直接执行任意代码、改变工作流真相或持有生产凭据。
2. 工作流支持长时间运行、人工等待、worker 故障、幂等重试、取消和审计。
3. ML 流程覆盖数据质量、任务推断、split/leakage、baseline、受限搜索、评估和 candidate 注册。
4. 默认保护隐私、租户隔离、预算上限、提示注入防护和出站网络隔离。
5. MVP 能完成一次“上传 -> 必要时提问 -> 回答后恢复 -> 生成评估报告和 candidate 模型”的纵向闭环。

## 多 Agent 共识

- 架构采用“持久化工作流控制面 + 隔离 worker 数据面”。LLM 是受约束的 Planner/Reporter，不是任意代码执行器。
- LLM 只输出版本化 JSON Schema 计划；Policy Gate 校验后才能调用白名单工具。
- 原始数据和 artifact 不可变并带内容哈希；训练、数据版本、split、代码/容器、随机种子、Prompt/模型版本必须有 lineage。
- 队列采用 at-least-once，依靠 lease、幂等键、CAS 和不可变 artifact 避免重复副作用，不宣称 exactly-once。
- 目标列、预测时点、正类、业务指标、时间/实体切分、敏感数据用途、预算增加和生产部署不能在低置信或高风险时静默推断。
- MVP 仅支持 CSV/Parquet、二分类与回归、固定 pipeline、朴素和简单 baseline、有界搜索、离线评估、candidate 注册；不支持任意用户代码或自动生产发布。

## 待取舍问题与拟定决定

1. 工作流引擎：默认采用 FastAPI 模块化单体 + PostgreSQL 状态机/transactional outbox + Redis/SQS 队列；若团队已有 Temporal 运维经验则用 Temporal。核心状态机和任务协议保持可迁移。
2. 状态模型：使用正交的 `phase`、`status`、`outcome`，避免巨型枚举。`WAITING_USER` 的所有阻塞问题回答后自动恢复；用户主动 `PAUSED` 才需要显式 resume。
3. 自动化阈值：置信度阈值只作为租户策略的初始默认值，不写死为普适真理；高风险门禁不允许通过提升置信度绕过。
4. 部署：MVP 只注册 candidate；生产部署为后续阶段且默认人工批准。
5. Agent 形态：MVP 不做自由多 Agent 对话网络；由一个 durable orchestrator 驱动 Profiler、Planner、Trainer、Evaluator、Reporter 等有限角色和工具。

## 初步核心契约

- `POST /v1/datasets`、`POST /v1/datasets/{id}:finalize`
- `POST /v1/runs`、`GET /v1/runs/{id}`、`GET /v1/runs/{id}/events`
- `POST /v1/runs/{id}/questions/{question_id}:answer`
- `POST /v1/runs/{id}:pause`、`:resume`、`:cancel`
- `GET /v1/runs/{id}/artifacts`

所有变更操作使用 `Idempotency-Key` 与 `expected_run_revision`/`If-Match`。事件至少一次投递、带单调序号，客户端按 `event_id` 去重。问题包含答案 Schema、候选项、推荐及理由、影响、置信度、阻塞性、版本和过期策略。

## 已知风险

- 对“只上传数据”的产品承诺过度，掩盖业务目标无法从表格唯一推断的事实。
- LLM 被文件名、列名或单元格提示注入，越权调用工具或泄露数据。
- split 错误、时间/实体泄漏、测试集反复使用，导致离线指标虚高。
- worker 重试造成重复训练、重复扣费、重复部署；大面积恢复造成级联过载。
- 沙箱、多租户、数据删除、审计只有设计没有实现验证。
- 提问过多导致用户放弃，提问过少又会产生语义错误。

## 请求 Gemini 评审

请按架构/计划混合对象评估：

1. 该概念方案是否真正满足“LLM 托管 + 人工中断后继续”，是否存在关键语义漏洞。
2. 拟定 MVP 技术栈和范围是否过度或不足。
3. 哪些设计决策必须在实现前冻结，哪些可以延后。
4. 给出最小纵向闭环的可验证实施顺序、风险触发条件和验收动作。

输出请控制在 1500 个中文字符以内，但必须完整包含评估总览、核心缺陷、风险预警、行动清单和最终建议；不要在句子中途截断。
