# Managed AutoML API 0.8.0 逐项测试报告

## 1. 结论

0.8.0 已通过 202 项源码回归和发布门禁；本轮服务器更新验收见第 6 节。当前
`single-node-production` 可作为第三方 Agent 平台调用的独立 AutoML API 使用，覆盖 HTTPS/JWT、
数据上传、结构化中断与人工恢复、sklearn、AutoGluon、TabPFN/CUDA、JSON/SSE 事件、结果和
artifact 下载、审计以及在线备份。

该结论限定为单节点生产：`cluster-production` 仍因 PostgreSQL/RLS、S3/KMS、独立 worker 和高可用
adapter 未接线而 fail-closed。API 不内置 LLM，第三方 Agent 平台仍必须负责上传前和 Prompt 出站前的
DLP/脱敏。

## 2. 验证环境

| 项目 | 值 |
| --- | --- |
| 验证日期 | 2026-07-31 |
| API/SDK 版本 | 0.8.0 |
| 本地 Python | 3.12.7 |
| API 主要依赖 | FastAPI 0.140.0、Pydantic 2.13.4、scikit-learn 1.7.2 |
| 可选后端 | AutoGluon Tabular 1.5.0、TabPFN 8.1.0 |
| 身份服务实测 | Keycloak 26.3.3、PostgreSQL 17、Caddy 2.10.2 |
| GPU runtime | Torch 2.13.0+cu130、NVIDIA GeForce RTX 4090 |
| 最终 GPU 镜像 | `sha256:7bdd9d81d9cc7903e6b538bc7296a7eda8ad41c7cb5c5a16dd49b93ec973e7bd`，5,954,648,220 bytes |
| 直接回滚镜像 | `sha256:ee6fd072cc45b4766d48daaa98dda6c6e7a6dd9c21872c4d29c59bd23b95ba8e` |
| 生产锁定 | `uv.lock`、`requirements.production.lock` |
| 生产部署文件 | `compose.production-single.yaml`、`compose.oidc.yaml`、`compose.dual-ip.yaml`、`compose.gpu-direct.yaml`、`deploy/single-node/Caddyfile`、`deploy/identity/*` |

## 3. 自动化门禁

| 检查 | 结果 |
| --- | --- |
| `uv run ruff check .` | 通过 |
| `uv run ruff format --check .` | 通过，75 个 Python 文件无格式偏差 |
| `uv run pytest -q` | 202 个 case：202 通过，0 跳过，0 失败 |
| `uv lock --check` | 通过，103 个 package 解析一致 |
| `scripts/generate_agent_openapi.py --check` | 通过，Agent OpenAPI 无漂移 |
| `python -m automl_api.production` | 通过，5 个必选 Python 生产依赖均为 pass |
| `git diff --check` | 通过 |
| Production Compose/网关约束 | 通过 |

## 4. 逐项验收

| ID | 范围 | 预期 | 结果 | 证据 |
| --- | --- | --- | --- | --- |
| P-001 | 单机生产预检 | 所有必选配置真实接线 | 通过 | `tests/test_single_node_production.py` |
| P-002 | SQLite 完整性 | `/readyz` 执行 `PRAGMA quick_check` | 通过 | `sqlite_quick_check=pass` |
| P-003 | 对象存储 | 受保护目录完成写入/fsync 探针 | 通过 | `local_blob_store=pass` |
| P-004 | Worker 存活 | 持久化串行 worker 实际运行 | 通过 | `worker_running=pass` |
| P-005 | 备份目录 | 与 state 分离且可写 | 通过 | `backup_directory=pass` |
| P-006 | 集群门禁 | adapter 未接线时 `/readyz=503` | 通过 | `runtime_adapters=fail` |
| P-007 | Host 白名单 | 非法外部 Host 返回 421 | 通过 | Caddy/TrustedHost 专项 case |
| P-008 | HTTPS 外部 URL | 上传/下载 URL 使用 public HTTPS origin | 通过 | public base URL case |
| P-009 | 安全 Header | HSTS、nosniff、frame deny、CSP 等存在 | 通过 | hardening case 与外部请求 |
| P-010 | 限流 | client/token/租户并发超限稳定拒绝 | 通过 | hardening、429 与 Retry-After case |
| P-011 | 请求并发 | 请求、SSE 总量和每租户 SSE 分别有上限 | 通过 | runtime limits case |
| P-012 | 指标边界 | 内部 `/metrics` 可用，公网返回 404 | 通过 | metrics case 与外部请求 |
| P-013 | 审计 | 记录 operation/tenant/subject/resource，不记录 token/请求体 | 通过 | caplog 断言 |
| P-014 | JWT 签发 | 短期 token 包含 tenant/subject/actor/scope | 通过 | credentials case |
| P-015 | JWT 校验 | issuer/audience/kid/expiry/scope fail-closed | 通过 | `tests/test_auth_security.py` |
| P-016 | 租户隔离 | 跨租户资源统一隐藏 | 通过 | API/auth 回归 |
| P-017 | 备份一致性 | SQLite 前后逻辑快照相同才接受 | 通过 | backup case |
| P-018 | 备份防篡改 | manifest 文件集、大小、SHA-256 全部校验 | 通过 | tamper case |
| P-019 | 恢复与回滚 | 恢复 DB/对象，保留 TabPFN cache 和旧状态 | 通过 | restore case |
| P-020 | 文件权限 | state `0700`，DB/对象/密钥 `0600` | 通过 | permissions case |
| P-021 | 数据上传 | CSV/Parquet、ETag、SHA-256、大小门禁 | 通过 | storage/end-to-end 回归 |
| P-022 | 中断恢复 | DecisionPacket 等待、human 回答后续跑 | 通过 | durable/API/SDK 与真实服务器 |
| P-023 | 幂等与修订 | Idempotency-Key、If-Match、cursor 签名 | 通过 | API/persistence 回归 |
| P-024 | Artifact 下载 | 短期 ticket、ETag、Range、续传与撤销 | 通过 | transfer/production 回归 |
| P-025 | Agent 契约 | manifest、tool OpenAPI、context、actions 一致 | 通过 | contract/interface 回归 |
| P-026 | 三后端注册 | sklearn/AutoGluon/TabPFN readiness 如实报告 | 通过 | backend registry 回归 |
| P-027 | TabPFN 归属 | 返回 `Built with PriorLabs-TabPFN` | 通过 | manifest 与真实服务器 |
| P-028 | 生产 Compose | 只有 Caddy 发布 HTTPS，API 不发布明文端口 | 通过 | Compose config |
| P-029 | 依赖可重现 | uv/production constraints 保持锁定 | 通过 | lock 与构建门禁 |
| P-030 | 下载票据日志 | 网关不记录票据 URI，artifact ETag 不被压缩改写 | 通过 | Caddy config 与真实下载 |
| P-031 | Webhook 投递 | run 级 endpoint 绑定、HMAC、durable retry、固定已校验 IP 和 SSRF 默认拒绝 | 通过 | `tests/test_webhook_delivery.py` 与真实 TLS/SNI case |
| P-032 | 历史失败事件 | 旧 `run.failed.v1` 缺字段仍可读 | 通过 | `run_000000000006` 返回 `retriable=false` |
| P-033 | 网关启动 | Caddy capability、default SNI 和健康检查正确 | 通过 | 三容器 healthy |
| P-034 | 备份临时文件 | WAL 源库归档不残留 `-shm/-wal/.automl-before` | 通过 | 单元测试与真实新备份 |
| P-035 | GPU 运行 | 容器能识别 RTX 4090 并执行 TabPFN 真实任务 | 通过 | Torch 2.13.0+cu130 探针与 TabPFN 回归 `run_000000000020` |
| P-036 | 外部 LLM 边界 | manifest 不虚报生产 DLP | 通过 | `production_external_llm_safe=false` |
| P-037 | 文档与案例 | 链接、JSON、CSV、Python 和发布包收录持续可验证 | 通过 | `tests/test_documentation_examples.py` 与 release packaging case |
| P-038 | 双 IP 生产入口 | 两个精确绑定各自通过 TLS，数据面 URL 保持请求 Origin | 通过 | multi-origin 回归与真实服务器验收 |
| P-039 | OAuth2 自动取 token | Keycloak `client_credentials` 签发五分钟 RS256 JWT，API 通过 JWKS 验签 | 通过 | 真实容器栈与 `scripts/verify_oidc_deployment.py` |
| P-040 | 身份隔离 | 错误 secret 返回 401，admin/DB/management 不对外发布 | 通过 | Keycloak 26 返回 `401 unauthorized_client`，外部 `/admin/=404` |
| P-041 | Agent 权限边界 | JWT 携带 tenant/agent/operation scopes，不授予 `decideApproval` | 通过 | 34 个精确 operation scopes，含 9 个 Webhook 与 2 个数据生命周期 scope，JWKS 实际验签通过 |
| P-042 | 评估图 | 分类/回归统一生成聚合 PNG，进入 EvaluationReport artifact | 通过 | 分类 `run_000000000018/19` 各 4 张；回归 `run_000000000020` 生成 3 张并按聚合阈值跳过 1 张；全部下载校验 magic/size/SHA-256 |
| P-043 | 六阶段 callback | 六个 `run.stage_completed.v1` 在真实计算边界顺序发布、去重并可恢复 | 通过 | `run_000000000018` 产生 `INGEST -> PROFILE -> PLAN -> TRAIN -> EVALUATE -> PACKAGE` 和 6 个 durable delivery |
| P-044 | Callback 恢复与重投 | EXHAUSTED 不阻塞后续事件，人工重投独立保留 30 天 | 通过 | 队首保序、熔断恢复、过期 410 回归 |
| P-045 | Worker 租约 | 长训练自动续租，lease generation/control epoch 丢失后陈旧 worker 不再写阶段 | 通过 | 长任务跨越 2 倍原租期、失去 fence 和心跳清理专项测试 |
| P-046 | 终态原子性 | success/failed/canceled 的 result、Run 终态、终态事件和 outbox 同时可见 | 通过 | SQLite checkpoint 故障注入、回滚、重开和重试测试 |
| P-047 | 阶段重试幂等 | EVALUATE/PACKAGE 中途重试不重复 Output、trial 或 artifact | 通过 | 首次 terminal checkpoint 失败后自动重试，单例 Output、trial number、`kind+sha256` 均唯一 |
| P-048 | 无正文响应协议 | 204/205/304 不发送 JSON 正文，幂等首次请求和回放保持 header 一致 | 通过 | Webhook 删除首次/回放均为 bodyless 204；历史 304 cache 的 ETag 保留，无 h11 Content-Length 冲突 |

## 5. OIDC 真实容器验收

| 验收项 | 实测结果 |
| --- | --- |
| 身份栈健康 | PostgreSQL、Keycloak、HTTPS identity gateway 全部 healthy |
| Discovery | issuer/token endpoint/JWKS 均为配置的 HTTPS 主入口 |
| 错误凭据 | HTTP 401，OAuth error=`unauthorized_client`，不泄露 secret |
| 正确凭据 | Bearer JWT，`exp-iat=300` 秒，RS256 `kid` 存在 |
| Claims | `aud=managed-automl-api`、`tenant_id=partner_a`、`actor_type=agent` |
| 权限 | 34 个 Agent operation scopes，含 9 个 Webhook 管理和 2 个数据生命周期 scope，不包含 `decideApproval` |
| API 联调 | 无 token 访问 manifest=401，SDK 自动取 token 后 manifest=200 |
| 双入口联调 | 同一个 access token 调用两个 AutoML API IP，manifest 均为 200 |
| 验签配置 | API 仅配置内网 JWKS URL，`AUTOML_JWT_SECRET` 和 `AUTOML_JWKS_JSON` 均为空 |
| 对外边界 | identity gateway 只发布 discovery/token/JWKS，`/admin/` 返回 404 |
| 身份备份 | 首份 PostgreSQL `pg_dump` gzip 归档非空且通过 `gzip -t` |
| 网关资源 | `GOMAXPROCS=2` + 回环 HTTP 探针；75 秒 6 次采样保持 7–8 PID，cgroup 与真实线程一致 |

## 6. 最终服务器验收

| 验收项 | 实测结果 |
| --- | --- |
| 最终镜像切换 | API 运行 `7bdd9d81…`（`managed-automl-api:0.8.0-cuda-callback-plots-r5-20260731`），API、双 IP gateway 与 identity gateway healthy |
| HTTPS | `/healthz=200`，安全响应头存在 |
| 双 IP HTTPS | `192.168.194.67:8443` 和 `192.168.77.32:8443` 分别由独立网关精确绑定，CA/IP SAN 校验通过 |
| 双 IP 数据面 | 两个入口创建 upload session 均返回同 Origin URL，测试数据已删除 |
| 内部端点隔离 | 外部 `/readyz=404`、`/metrics=404` |
| 旧服务兼容 | 旧 `:8000/healthz=200`，未被本次发布替换 |
| sklearn callback+绘图 | `run_000000000018` 成功，真实六阶段事件、6 个 delivery、4 张 PNG 校验通过；callback 不可达不阻塞 Run |
| AutoGluon 分类 | `run_000000000019` 成功，AutoGluon 1.5.0，1 trial，4 张 PNG |
| TabPFN CUDA 回归 | `run_000000000020` 成功，TabPFN 8.1.0、RTX 4090；3 张 PNG，稀疏 residual hexbin 按隐私阈值明确跳过 |
| Artifact | 三个 Run 的 PNG 均经 ticket 下载，magic/大小/SHA-256 全通过 |
| 204 协议回归 | Webhook endpoint 首次删除和同 key 回放均返回 0 字节 204，无 Content-Type/Content-Length；新容器 h11/ERROR 日志为 0 |
| 测试清理 | 最新 3 个 Run 的 dataset 删除均返回 `COMPLETED`，测试 Webhook endpoint 已删除 |
| 在线备份 | r5 切换前 `automl-backup-20260731T105425Z-6bcdf93f` 与切换后 `automl-backup-20260731T105536Z-b22642cf` 均校验通过；最终 manifest 含 63 个数据文件（加 manifest 共 64 个物理文件）、0 个 SQLite sidecar |
| 历史兼容 | `run_000000000006` 事件 API 从 500 恢复为 200 |

## 7. 公开文档与案例验收

| 验收项 | 实测结果 |
| --- | --- |
| Python SDK 案例 | sklearn Run 成功，经历 `WAITING_USER`，14 个事件，3 个 artifact 校验通过 |
| 原始 HTTP 案例 | upload/finalize/answer/result/download 闭环成功，14 个事件，3 个 artifact 校验通过 |
| 请求体 | sklearn、AutoGluon、TabPFN 三份 JSON 均可解析且通过基本契约断言 |
| 样例数据 | 两份合成 CSV 列结构正确，每份 64 行数据，不含真实用户信息 |
| 文档链接 | README、文档中心、复现指南、使用手册、案例和 SDK README 的本地链接全部可解析 |
| 发布包 | 新文档、案例、API/SDK wheel 和 OpenAPI 均收录，`SHA256SUMS` 全部通过 |

## 8. DLP 与责任边界

- API 不把原始数据行嵌入 Agent context，但列名、文件名、类别值、问题和摘要仍是不可信数据派生内容。
- 第三方 Agent 平台必须在上传前完成数据分级、PII/密钥检测、租户授权和删除/哈希化等处理。
- API tool result 进入 LLM 前必须再次执行字段 allowlist、opaque ID、PII 检测和长度限制。
- LLM 生成的工具参数必须重新校验 Schema、tenant、scope、资源 ID、策略和 `If-Match`。
- Bearer、下载票据、artifact、原始数据行和模型访问 token 不得进入 Prompt、记忆或 trace。
- `allow_pii=false`、`allow_external_llm=true` 都不是 DLP 已完成的证明。

## 9. 已知边界

- 单机 profile 不是高可用；不得同时启动两个 API 实例读写同一 SQLite 和对象目录。
- 单节点 Webhook dispatcher 与 API 同进程，不支持多个 API 实例并发写同一 SQLite；事件轮询/SSE 仍是恢复源。
- API 不提供在线推理服务；训练成功不等于自动生产合格。
- `production_external_llm_safe=false` 保持不变，DLP 由第三方 Agent 平台实施并共同验收。
- 集群生产仍需 PostgreSQL/RLS、S3/KMS、独立 worker、分布式 dispatcher 和高可用运维。

## 10. 交付判定

源码、SDK、OpenAPI、生产部署文件、公开文档/案例和单机服务器均通过本轮验收。
`scripts/package_release.py` 生成的发布包包含双 wheel、源码、OpenAPI、单/双 IP Compose、文档、可运行案例、
manifest 和 `SHA256SUMS`；GitHub 交付不包含 14.7 GB GPU 镜像。需要离线自托管时，应从受控
镜像仓库按 digest 分发，或单独用 `scripts/package_release.py --docker-image ...` 生成平台匹配的镜像包。

最终源码交付包为 `dist/releases/managed-automl-0.8.0-20260731T110000Z.tar.gz`；其同名目录保留可直接
浏览的发布内容，二者均不包含 Docker 镜像 tar。
