# Managed AutoML API 0.8.0 逐项测试报告

## 1. 结论

0.8.0 已通过源码层的完整回归和单机生产专项测试。`single-node-production`
已接线 JWT、HTTPS 网关、Host 白名单、限流、并发上限、审计、指标、SQLite WAL、
本地不可变对象存储、串行 worker、备份校验与可回滚恢复。`cluster-production`
仍因外部 runtime adapter 未接线而 fail-closed，不得报告为集群生产就绪。

## 2. 验证环境

| 项目 | 值 |
| --- | --- |
| 验证日期 | 2026-07-26 |
| 源码版本 | 0.8.0 |
| 本地 Python | 3.12.7 |
| 主要依赖 | FastAPI 0.140.0、Pydantic 2.13.4、scikit-learn 1.7.2 |
| 生产锁定 | `uv.lock`、`requirements.production.lock` |
| 生产部署文件 | `compose.production-single.yaml`、`deploy/single-node/Caddyfile` |

## 3. 自动化结果

| 检查 | 结果 |
| --- | --- |
| `ruff check .` | 通过 |
| `ruff format --check .` | 通过，61 个文件无格式偏差 |
| Agent OpenAPI 生成一致性 | 通过 |
| 生产 Python 依赖预检 | 通过 |
| `uv lock --check` 及 production export diff | 通过 |
| Pytest | 144 个 case：144 通过，0 跳过，0 失败 |
| Production Compose 解析 | 通过 |
| 初始化权限 | env `0600`，state/backup 目录 `0700`，通过 |

本地环境已安装 AutoGluon，真实 AutoGluon smoke 与所有 optional-backend 契约用例均执行。

## 4. 逐项验收

| ID | 范围 | 预期 | 结果 | 证据 |
| --- | --- | --- | --- | --- |
| P-001 | 单机生产预检 | 所有必选配置真实接线 | 通过 | `tests/test_single_node_production.py` |
| P-002 | SQLite 完整性 | `/readyz` 执行 `PRAGMA quick_check` | 通过 | `sqlite_quick_check=pass` |
| P-003 | 对象存储 | 受保护目录完成写入/fsync 探针 | 通过 | `local_blob_store=pass` |
| P-004 | Worker 存活 | 持久化串行 worker 实际运行 | 通过 | `worker_running=pass` |
| P-005 | 备份目录 | 与 state 分离且可写 | 通过 | `backup_directory=pass` |
| P-006 | 集群门禁 | adapter 未接线时 `/readyz=503` | 通过 | `runtime_adapters=fail` |
| P-007 | Host 白名单 | 恶意 Host 被 400 拒绝 | 通过 | TrustedHost 专项 case |
| P-008 | HTTPS 外部 URL | 上传/下载 URL 使用 public HTTPS origin | 通过 | public base URL case |
| P-009 | 安全 Header | HSTS、nosniff、frame deny、CSP 等存在 | 通过 | hardening case |
| P-010 | 限流 | 受信网关还原 client IP；client 与 token 双层限流，轮换无效 Bearer 仍返回 429 | 通过 | hardening/proxy case |
| P-011 | 请求并发 | 请求、SSE 总量和每租户 SSE 分别有上限，超限返回 503/Retry-After | 通过 | hardening 单元逻辑与指标 |
| P-012 | 指标 | `/metrics` 返回 Prometheus 计数和耗时 | 通过 | metrics case |
| P-013 | 审计 | 记录 canonical operation、tenant/subject/resource，且不记录 token/请求体 | 通过 | caplog 断言 |
| P-014 | JWT 签发 | 短期 token 包含 tenant/subject/actor/scope | 通过 | credentials case |
| P-015 | JWT 校验 | issuer/audience/kid/expiry/scope fail-closed | 通过 | `tests/test_auth_security.py` |
| P-016 | 租户隔离 | 跨租户资源统一隐藏 | 通过 | API/auth 回归 |
| P-017 | 备份一致性 | SQLite 前后逻辑快照相同才接受 | 通过 | backup case |
| P-018 | 备份防篡改 | manifest 文件集、大小、SHA-256 全部校验 | 通过 | tamper case |
| P-019 | 恢复与回滚 | 恢复 DB/对象，保留 TabPFN cache 和旧状态 | 通过 | restore case |
| P-020 | 文件权限 | state `0700`，DB/对象/密钥 `0600` | 通过 | permissions case |
| P-021 | 数据上传 | CSV/Parquet、ETag、SHA-256、大小门禁 | 通过 | storage/end-to-end 回归 |
| P-022 | 中断恢复 | DecisionPacket 等待、回答后续跑 | 通过 | durable/API/SDK 回归 |
| P-023 | 幂等与修订 | Idempotency-Key、If-Match、cursor 签名 | 通过 | API/persistence 回归 |
| P-024 | Artifact 下载 | 短期 ticket、ETag、Range、续传与撤销 | 通过 | transfer/production 回归 |
| P-025 | Agent 契约 | manifest、tool OpenAPI、context、actions 一致 | 通过 | contract/interface 回归 |
| P-026 | 三后端注册 | sklearn/AutoGluon/TabPFN 可用性如实报告 | 通过 | backend registry 回归 |
| P-027 | TabPFN 归属 | manifest 返回 `Built with PriorLabs-TabPFN` | 通过 | attribution 契约 |
| P-028 | 生产 Compose | 只有 Caddy 发布 HTTPS，API/metrics/readyz 不直出 | 通过 | Compose config |
| P-029 | 依赖可重现 | lock 与 production constraints 一致 | 通过 | uv export diff |
| P-030 | 下载票据日志 | Uvicorn 关闭原始 access log；Caddy access log 跳过票据且 error log 删除 URI | 通过 | Caddy validate/config case |
| P-031 | Webhook 边界 | manifest/OpenAPI 明示仅 outbox，无内置 HTTP dispatcher | 通过 | contract/interface case |

## 5. 仍然存在的边界

- 单机 profile 不是高可用；不能同时启动两个 API 实例读写同一 SQLite 和对象目录。
- Webhook 为持久化 outbox，尚无内置 HTTP dispatcher；Agent 平台应使用事件轮询/SSE 或外部消费器。
- API 不提供在线推理服务；训练成功不等于自动生产合格。
- `production_external_llm_safe=false` 仍保留；Agent 平台必须把数据派生字段当作不可信数据，
  并在 tool executor 内保管凭据。
- 集群生产需继续实现 PostgreSQL/RLS、S3/KMS、独立 worker、dispatcher 和高可用运维。

## 6. 交付判定

源码与单机生产部署契约的自动化门禁已通过。真实服务器交付还必须对本次构建的镜像完成
HTTPS/JWT 端到端、备份恢复演练、CUDA 和 TabPFN 分类/回归验收，并记录镜像 digest。
