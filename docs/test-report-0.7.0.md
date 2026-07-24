# Managed AutoML API 0.7.0 逐项测试报告

## 1. 文档信息

| 项目 | 内容 |
| --- | --- |
| 报告对象 | Managed AutoML API / Python SDK / Docker 交付包 |
| 版本 | 0.7.0 |
| 报告日期 | 2026-07-24 |
| 目标读者 | 接入方 Agent 平台工程团队、后端交付团队、测试验收人员 |
| 测试目的 | 判断当前 API 是否达到“可由外部 Agent 平台以 HTTP/SDK 方式调用”的交付状态，并明确尚未覆盖或不宜承诺的边界 |
| 测试范围 | API 契约、SDK、上传/运行/中断/恢复/结果/artifact、标准后端、认证、生产预检、Webhook outbox、审批、模型候选、删除任务、限额、Docker、release bundle |
| 不在范围 | 生产级 HA、真实 PostgreSQL/RLS 运行验证、真实 IdP 联调、Webhook HTTP dispatcher、在线推理服务、分布式物理删除、自动生产部署、TabPFN 真实权重商业许可审查 |
| 本轮验证状态 | 最新工作树的 121 项 Python 测试、静态检查、OpenAPI、生产依赖检查、wheel 构建和 release bundle 校验已通过；Docker 全量构建与容器 smoke 未完成，本报告不复用旧镜像作为证据 |

## 2. 总体验收结论

当前 0.7.0 已达到“本地单节点、API-first、外部 Agent 平台可嵌入调用”的可用状态。使用者上传 CSV/Parquet 数据后，API 可以创建可恢复 Run，输出结构化中间结果，在需要确认目标列、i.i.d. 假设或正类时进入 `DecisionPacket` 等待状态，回答后自动继续，并最终返回 `RunResult`、`OutputResource` 和可校验 artifact。

已验证的标准后端包括 scikit-learn、AutoGluon Tabular 和 TabPFN 接口层。scikit-learn 已作为默认后端完成 API 端到端真实运行；AutoGluon 已完成真实 smoke 训练并生成 deployment archive；TabPFN 已验证包/readiness/适配器逻辑/fake runtime 训练路径，但在当前交付环境没有接受模型权重许可，也没有提供真实 checkpoint，因此 manifest 正确报告 `installed=true`、`available=false`、`unavailable_reason=MODEL_LICENSE_NOT_ACCEPTED`。本报告不把 TabPFN 真实权重训练列为已完成。

当前构建仍不是可直接暴露给真实租户的完整生产服务。0.7.0 formal production profile 包含固定
失败的 `runtime_adapters` 必选检查，因此 `/readyz` 无论环境变量是否齐全都返回 `503`。正式上线
必须先在后续版本接入并验收真实 IdP、PostgreSQL/RLS、S3/KMS、DLP、隔离 worker、Webhook
dispatcher、可观测性和备份灾备。

## 3. 测试环境

| 类别 | 记录 |
| --- | --- |
| 测试对象 | 当前发布源工作树；最终 Git 提交标识在推送后记录 |
| API/SDK 版本 | 0.7.0 |
| Python 兼容范围 | `>=3.11,<3.14` |
| 默认 API profile | `local-durable-tabular-v1` |
| 默认执行后端 | `sklearn` |
| 标准后端依赖 | `scikit-learn>=1.5,<2`、`autogluon.tabular>=1.5,<1.6`、`tabpfn>=8.1,<8.2` |
| Docker 全量构建 | 未完成；全量后端镜像下载大型 Torch/CUDA 依赖时主动终止，未形成通过结论 |
| Release bundle | `dist/releases/managed-automl-0.7.0-20260724-verified-r2`；目录内 `SHA256SUMS` 全部通过 |

## 4. 测试矩阵

| 编号 | 对象 | 测试动作 | 预期 | 实际 | 结论 | 证据 |
| --- | --- | --- | --- | --- | --- | --- |
| T-001 | 全量 Python 测试 | 执行 `pytest --override-ini addopts= -q` | 所有单元、契约、SDK、持久化、后端和端到端测试通过 | `121 passed in 8.45s` | 通过 | 最新工作树 pytest 输出 |
| T-002 | 代码静态检查 | 执行 `ruff check .` | 无 lint 问题 | 通过 | 通过 | ruff 输出 |
| T-003 | 代码格式检查 | 执行 `ruff format --check .` | 所有文件格式符合配置 | 通过 | 通过 | ruff 输出 |
| T-004 | Agent OpenAPI 生成一致性 | 执行 `python scripts/generate_agent_openapi.py --check` | 生成合同与 `openapi/automl-agent-tools.yaml` 一致 | 通过 | 通过 | 脚本输出 |
| T-005 | 版本同步 | 检查 API pyproject、SDK pyproject、OpenAPI、Compose、代码版本 | 版本均为 0.7.0 | 通过 | 通过 | `scripts/package_release.py` 内置校验 |
| T-006 | 数据集创建 | `POST /v1/datasets` | 返回 `dataset_id`、`dataset_version_id`、`upload_id` 和上传 part | 通过 | 通过 | `tests/test_api_end_to_end.py`、`tests/test_storage_end_to_end.py` |
| T-007 | 上传完整性 | PUT part 后 `:finalize` 校验 ETag、大小、SHA-256 | 合法上传 READY，错误 hash 被拒绝 | 通过 | 通过 | `tests/test_storage_end_to_end.py`、`tests/test_sdk_transfers.py` |
| T-008 | Run 创建和幂等 | `POST /v1/runs` 带 `Idempotency-Key` 重放 | 相同请求返回同一快照，不同请求复用 key 返回 409 | 通过 | 通过 | `tests/test_api_end_to_end.py` |
| T-009 | JSON/SSE 事件 | `GET /v1/runs/{run_id}/events` JSON 与 SSE | 事件按 `seq` 有序，可从 `after_seq`/cursor/`Last-Event-ID` 继续 | 通过 | 通过 | `tests/test_api_end_to_end.py`、`tests/test_sdk.py` |
| T-010 | DecisionPacket 中断 | 缺少目标列或 i.i.d. 确认时进入 `WAITING_USER` | 返回开放 `DecisionPacket`，问题结构化且带 revision | 通过 | 通过 | `tests/test_durable_api_end_to_end.py`、`tests/test_agent_contract.py` |
| T-011 | DecisionPacket 回答后继续 | `POST /decision-packets/{wait_set_id}:answer` | `If-Match` 校验 wait-set revision，回答后 workflow 自动继续 | 通过 | 通过 | `tests/test_api_end_to_end.py`、`tests/test_sdk.py` |
| T-012 | 暂停/恢复/取消 | 调用 `:pause`、`:resume`、`:cancel` | 暂停/恢复校验 run revision，取消幂等且终态清空 blocker | 通过 | 通过 | `tests/test_api_end_to_end.py`、`tests/test_sdk.py` |
| T-013 | 输出资源 | `GET /outputs`、`GET /outputs/{output_id}` | 返回 `DATA_QUALITY_REPORT`、`TASK_SPEC`、`TRIAL_RESULT`、`EVALUATION_REPORT`、`MODEL_CARD`、`RUN_REPORT` 等结构化输出 | 通过 | 通过 | `tests/test_durable_api_end_to_end.py` |
| T-014 | 终态结果 | `GET /v1/runs/{run_id}/result` | 默认成功训练返回 `NO_ELIGIBLE_MODEL`，审批通过返回 `ELIGIBLE_MODEL_AVAILABLE`，失败/取消返回 `INCOMPLETE` | 通过 | 通过 | `tests/test_api_end_to_end.py`、`tests/test_durable_api_end_to_end.py`、`tests/test_production_controls.py` |
| T-015 | artifact 元数据与下载 | `GET /v1/artifacts/{artifact_id}`、`POST /v1/artifacts/{artifact_id}:download` | 短期票据 900 秒有效，下载校验 ETag、Range、SHA-256、大小 | 通过 | 通过 | `tests/test_storage_end_to_end.py`、`tests/test_sdk_transfers.py` |
| T-016 | Python SDK 主流程 | SDK 执行创建、finalize、Run、等待问题、回答、等待结果、下载 artifact | 高层 SDK 能完成小调用面端到端流程 | 通过 | 通过 | `tests/test_sdk.py`、`tests/test_sdk_durable_end_to_end.py` |
| T-017 | 外部 Agent manifest | `GET /v1/agent/manifest` | 声明 API 独立执行后端、无内部 LLM、LLM budget 属于外部平台、列出后端 readiness | 通过 | 通过 | `tests/test_agent_interface.py`、`tests/test_agent_contract.py` |
| T-018 | Agent context/actions | `GET /agent-context`、`GET /agent-actions` | 只读上下文 gated by `allow_external_llm=true`，动作引用 canonical API，不提供通用 tool executor | 通过 | 通过 | `tests/test_agent_interface.py` |
| T-019 | scikit-learn 后端 | 默认 `backend_id=sklearn` 训练二分类/回归 | CPU 单线程 bounded baseline/CV/holdout，返回 joblib artifact | 通过 | 通过 | `tests/test_ml_engine.py`、`tests/test_durable_api_end_to_end.py` |
| T-020 | AutoGluon 后端 descriptor | 读取 AutoGluon backend descriptor | `available`、版本、artifact、CPU 限制可机器读取 | 通过 | 通过 | `tests/test_optional_backends.py` |
| T-021 | AutoGluon 真实训练 smoke | 在可用环境运行 `AutoGluonBackend().run(...)` | 生成 deployment predictor archive，包内有 `predictor/learner.pkl`，不包含训练数据目录 | 通过 | 通过 | `test_real_autogluon_smoke_produces_deployment_archive` |
| T-022 | TabPFN readiness | 在安装但未接受许可/无权重环境读取 descriptor/manifest | `installed=true`、`available=false`、`unavailable_reason=MODEL_LICENSE_NOT_ACCEPTED` | 通过 | 通过 | `tests/test_optional_backends.py` |
| T-023 | TabPFN fake runtime 路径 | 使用 fake `TabPFNClassifier/Regressor` 运行适配器 | CV、holdout、metadata artifact 路径可执行，artifact 不含训练数据、类别值或 fit-state | 通过 | 通过 | `tests/test_optional_backends.py` |
| T-024 | 认证与 scope | development Bearer、production 配置 fail-closed、JWT scope 精确校验 | 未授权返回 401，缺 scope 返回 403，生产弱配置拒绝启动 | 通过 | 通过 | `tests/test_auth_security.py` |
| T-025 | 租户隔离 | 使用不同 Bearer 读取资源 | 跨租户资源返回 404，不泄露存在性 | 通过 | 通过 | `tests/test_api_end_to_end.py` |
| T-026 | 运行限额 | 数据大小、租户存储、活跃 Run、预算限制 | 超限返回稳定 problem code | 通过 | 通过 | `tests/test_runtime_limits.py` |
| T-027 | 持久化恢复 | 重启 app 后读取已完成 Run 和 result | SQLite/local object 状态保留，终态可恢复 | 通过 | 通过 | `tests/test_durable_api_end_to_end.py` |
| T-028 | Docker 国内源全量构建 | 使用默认 Dockerfile build args 构建最新工作树镜像 | 使用 DaoCloud Python base 和清华 PyPI 源，完整依赖安装和镜像构建成功 | 构建确认使用国内源，但下载大型 Torch/CUDA 依赖时主动终止，未生成最终镜像 | 未完成 | 本轮 Docker 构建记录 |
| T-029 | Docker health/readiness | 容器启动后访问 `/healthz`、`/readyz`、manifest | partner-preview 探针正常；formal `/readyz` 固定返回 `503 production_preflight_failed` | 因 T-028 未生成最新镜像，未执行本轮容器 smoke；formal 行为已有 HTTP 自动化覆盖 | 未完成 | `tests/test_production_controls.py` |
| T-030 | Wheel 构建 | 构建 API wheel 与 SDK wheel | 两个 wheel 成功生成，版本为 0.7.0 | API 与 SDK wheel 均成功构建 | 通过 | `python scripts/package_release.py --no-archive` 输出 |
| T-031 | Release bundle | 执行 `python scripts/package_release.py --no-archive` | bundle 含 wheels、OpenAPI、Compose、Dockerfile、文档和 SHA256SUMS | `dist/releases/managed-automl-0.7.0-20260724-verified-r2` 已生成 | 通过 | 本轮 release bundle |
| T-032 | Release bundle 校验 | 对本轮 release 目录执行 `shasum -a 256 -c SHA256SUMS` | 所有条目校验通过 | 所有条目 `OK` | 通过 | 本轮 SHA256SUMS 校验输出 |
| T-033 | 生产依赖与 fail-closed | 执行镜像依赖检查并验证 formal `/readyz` | 依赖检查只反映客户端包存在；0.7.0 formal profile 因 `runtime_adapters=false` 返回 `503` | 客户端依赖检查通过；formal HTTP 测试返回 `503 production_preflight_failed` | 通过 | `python -m automl_api.production`、`tests/test_production_controls.py` |
| T-034 | Webhook outbox 与删除任务 | 创建 endpoint、生成 outbox、请求重投、删除数据集并查询 deletion job | API 返回可跟踪资源；local durable 同步撤销访问并物理删除 dataset/upload 和派生 artifact 字节 | 自动化覆盖通过，删除后旧 artifact ticket 返回 `404` | 通过 | `tests/test_production_controls.py` |
| T-035 | 审批与模型候选 | `REQUIRE_APPROVAL` Run 训练后审批 | Run 进入 `WAITING_APPROVAL`；批准后注册 `ModelCandidate` 并返回合格模型结果 | 审批、终态事件、模型候选和过期审批 `409` 自动化覆盖通过 | 通过 | `tests/test_production_controls.py` |

## 5. 机器学习验证说明

### 5.1 已验证的任务类型

| 任务 | 数据假设 | split | 指标 | 覆盖后端 |
| --- | --- | --- | --- | --- |
| 二分类 | 单表 CSV/Parquet，目标列可确认，类别数为 2 | sealed holdout，开发集内 CV/验证 | `roc_auc`、`average_precision`、`log_loss`、`accuracy` 等 | scikit-learn、AutoGluon、TabPFN fake runtime |
| 回归 | 单表 CSV/Parquet，目标列为数值 | sealed holdout，开发集内 CV/验证 | 回归误差/得分指标 | scikit-learn、TabPFN fake runtime |

### 5.2 泄漏和复现控制

- 数据解析、目标列验证、重复样本分组、泄漏检查和 sealed holdout 由公共执行层统一处理。
- 训练选择只使用开发集；最终评估使用一次 sealed holdout。
- TabPFN categorical preprocessing 在每个 fold 内拟合，验证集不影响缺失值填补和类别映射。
- scikit-learn 和 AutoGluon 的 artifact 属于受信 artifact store 场景；不能从不可信来源反序列化。
- 测试使用固定 seed 覆盖确定性路径，例如 AutoGluon smoke 使用 `seed=19`，TabPFN fake runtime 使用 `seed=23`。

### 5.3 未验证或不承诺的 ML 能力

- 未验证 TabPFN 真实 checkpoint/权重训练；当前受模型权重许可、token 或本地 checkpoint 条件限制。
- 未覆盖多分类、时间序列、关系型多表、图像/文本/音频、在线推理服务。
- 未提供模型托管、在线推理、A/B 发布或自动生产部署。
- 默认 Run 不声明模型达到业务门槛；`REQUIRE_APPROVAL` 路径只注册经人工批准的控制面候选，仍需
  接入方完成真实质量、风险和部署门禁。

## 6. API 可用范围

当前可由第三方 Agent 平台稳定调用的能力包括：

- 创建数据集上传会话、上传分片、finalize 数据版本；
- 创建 Run，并通过 `objective.backend_id` 选择 `sklearn`、`autogluon` 或 `tabpfn`；
- 查询 Run 快照、阶段、事件、输出、终态结果；
- 处理 `DecisionPacket`，提交结构化回答后自动恢复；
- 暂停、恢复、取消 Run；
- 下载 artifact，支持 ticket、Range 续传和完整性校验；
- 读取 Agent manifest、Agent tool OpenAPI、Agent context 和 Agent action refs；
- 管理 Webhook endpoint/outbox、审批生产候选、查询 `ModelCandidate`、创建和查询删除任务；
- 使用 Python SDK 执行同等流程。

当前不可用或仅为后续契约的能力包括：

- Webhook 的真实 HTTP dispatcher、自动退避与外部接收端联调；
- PostgreSQL/RLS 和对象存储上的分布式物理删除；
- 在线推理和自动生产部署；
- 生产级外部 LLM 数据安全边界；
- 高可用和分布式 worker。

## 7. 生产前门禁

1. 认证从 development Bearer/preview HS256 升级为正式 OIDC/JWKS 或 workload identity。
2. 建立 PostgreSQL/RLS、对象存储隔离、加密/KMS、备份和灾备。
3. 增加 Agent context 出站 DLP、字段 allowlist、opaque column ID 和租户同意审计。
4. 建立 prompt-injection 回归集，覆盖文件名、列名、类别值、问题文本和 artifact 摘要。
5. 将 TabPFN 真实权重许可、checkpoint 来源、商业使用范围和运行资源纳入部署审批。
6. 为 AutoGluon 和 TabPFN 增加进程级资源隔离和硬超时控制。
7. 部署 Webhook dispatcher、异步删除 worker、真实质量门禁和在线部署系统。
8. 增加可观测性、审计日志、长期事件保留和运维 runbook。

## 8. 最终结论

除 T-028/T-029 的最新全量 Docker 镜像构建与容器 smoke 尚未完成外，0.7.0 的源码、wheel、
OpenAPI、SDK 和 release bundle 已通过本轮验证，可以作为“外部 Agent 平台调用的独立 AutoML
执行 API”交付给合作方做嵌入式集成、功能联调和非生产试运行。交付时应同时提供 OpenAPI、
Python SDK、Docker/Compose、后端说明、API 使用文档和本测试报告；接收方如使用 Docker，应在
目标构建环境完成全量镜像构建与 smoke 后再验收镜像。

若合作方计划上线生产，应把第 7 节列为正式上线门禁；0.7.0 formal profile 固定返回 `503`，不能
通过补齐环境变量改成生产就绪。尤其不能把当前 development 认证、local durable 状态、未启用许可
的 TabPFN 权重或 `NO_ELIGIBLE_MODEL` 评估产物解释为生产可托管模型。
