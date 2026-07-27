# 单机生产部署手册

本文档用于把 Managed AutoML API 作为一个独立 HTTPS 服务交付给第三方 Agent 平台。该部署
不包含 LLM：Agent 平台持有 LLM 和编排逻辑，通过 JWT 调用本 API 上传数据、启动 Run、读取
事件和输出、回答 DecisionPacket，并下载结果。

## 1. 生产边界

`single-node-production` 是可运行的小规模生产 profile：单个 API 容器内串行训练，SQLite WAL
保存元数据，本地不可变对象目录保存数据和产物，Caddy 提供 HTTPS，Docker 提供 CPU、内存、
PID 和 GPU 边界。它适合一台受控主机、有限租户和可以接受维护窗口的服务。

它不是集群生产：没有多副本、高可用、PostgreSQL RLS、S3/KMS 或独立训练 worker。需要这些
能力时必须使用 `cluster-production`；该 profile 在相应 adapter 真正接入以前会保持未就绪。

## 2. 前置条件

- Linux 主机、Docker Engine 和 Docker Compose v2；建议至少 8 CPU、32 GB RAM 和足够的数据盘。
- 防火墙只允许 Agent 平台访问配置的 HTTPS 端口；SSH 只允许运维网络。
- GPU 模式需要 NVIDIA 驱动。优先安装 NVIDIA Container Toolkit 并使用 `compose.gpu.yaml`；
  若受控 Linux 主机只有可用的 `/dev/nvidia*` 和驱动库，则先验证路径，再使用
  `compose.gpu-direct.yaml`。两种覆盖文件只能选择一个。
- TabPFN 需要部署方已接受适用许可，并准备批准的 token 或持久化 checkpoint。
- 主机时钟必须通过 NTP 同步；JWT 验证依赖正确时间。
- state、backup 和异地备份必须放在宿主机加密卷上；普通文件权限不等价于静态加密。

## 3. 初始化

从仓库根目录执行。`PUBLIC_HOST` 必须是 Agent 平台实际访问的 DNS 名或 IP；绑定地址建议填
ZeroTier/内网接口地址，不要在没有防火墙的主机上使用 `0.0.0.0`。

```bash
chmod +x scripts/init_single_node_production.sh
./scripts/init_single_node_production.sh \
  192.168.194.67 \
  192.168.194.67

chmod 600 .env.production-single
docker compose \
  --env-file .env.production-single \
  -f compose.production-single.yaml \
  config --quiet
```

初始化器生成三个互不复用的 48-byte 随机密钥，并创建权限为 `0700` 的状态、备份和 Caddy
目录。它不会覆盖已有 `.env.production-single`。正式启动前检查 CPU、内存、数据集、租户和
并发上限，并把环境文件纳入主机秘密备份，禁止提交 Git。

需要同时精确绑定两个内网 IP 时，增加以下配置：

```dotenv
AUTOML_PUBLIC_HOST=192.168.194.67
AUTOML_HTTPS_BIND_ADDRESS=192.168.194.67
AUTOML_SECONDARY_PUBLIC_HOST=192.168.77.32
AUTOML_SECONDARY_HTTPS_BIND_ADDRESS=192.168.77.32
AUTOML_GATEWAY_PIDS_LIMIT=512
AUTOML_GATEWAY_GOMAXPROCS=2
```

后续所有 Compose 命令都附加 `-f compose.dual-ip.yaml`。该覆盖文件会为第二个 IP 启动
独立网关；两个网关共享 API 和 Caddy PKI 存储，但各自维护正确的默认 SNI、IP 证书和精确
端口绑定。API 只会在请求 Origin 准确匹配配置白名单时，返回该 Origin 的
upload/artifact URL，其他 Host 仍使用 canonical `AUTOML_PUBLIC_HOST`。不要用 `0.0.0.0`
代替双精确绑定。

## 4. 启动

CPU 模式：

```bash
docker compose \
  --env-file .env.production-single \
  -f compose.production-single.yaml \
  -f compose.dual-ip.yaml \
  up -d --build --wait --wait-timeout 300
```

单 IP 部署应省略 `-f compose.dual-ip.yaml`。

GPU/TabPFN 模式先在 `.env.production-single` 中设置：

```dotenv
AUTOML_TABPFN_DEVICE=cuda
AUTOML_TABPFN_LICENSE_ACCEPTED=true
AUTOML_TABPFN_MODEL_SOURCE=public-v2
```

然后每条 Compose 命令都同时加载 GPU 覆盖文件：

```bash
docker compose \
  --env-file .env.production-single \
  -f compose.production-single.yaml \
  -f compose.gpu.yaml \
  up -d --build --wait --wait-timeout 900
```

没有 NVIDIA Container Toolkit、但已验证直接设备映射的主机，把上例最后一个覆盖文件换为
`compose.gpu-direct.yaml`。构建必须在目标 `linux/amd64` 主机上执行；Apple Silicon 本机导出的
`linux/arm64` 镜像不能交付给 x86_64 服务器。

API 容器没有宿主机端口映射。只有网关发布 HTTPS；API 仅在这个隔离的 Docker 网络内信任
转发头，Caddy 会覆盖外部传入的客户端地址头。`/metrics` 在网关固定返回 `404`，只能由宿主机
通过容器内部读取。不要在 API 端口直出时使用 `AUTOML_FORWARDED_ALLOW_IPS=*`。

## 5. TLS 信任和验收

Caddy 为私有地址签发内部 CA 证书。首次启动后把根证书安全地分发给 Agent 平台，并加入其
HTTP 客户端信任库：

```bash
export AUTOML_CA=.automl-production/caddy-data/caddy/pki/authorities/local/root.crt
export AUTOML_API=https://192.168.194.67:8443

curl --cacert "$AUTOML_CA" -fsS "$AUTOML_API/healthz"

docker compose \
  --env-file .env.production-single \
  -f compose.production-single.yaml \
  exec -T automl-api \
  python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/readyz').read().decode())"

docker compose \
  --env-file .env.production-single \
  -f compose.production-single.yaml \
  exec -T automl-api \
  python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/metrics').read().decode())"
```

交付前必须确认：

- 容器内 `/readyz` 返回 HTTP 200、`status=ready`，且 `production.checks` 没有 `fail`。
- 外部 `/readyz` 和 `/metrics` 都返回 404，避免暴露内部运行信息。
- 无 token 访问受保护路由返回 401；错误 audience/tenant/scope 的 token 被拒绝。
- `curl --cacert ... "$AUTOML_API/metrics"` 返回 404。
- 响应带 HSTS、`nosniff` 等安全 Header；日志不包含 JWT、下载票据、请求体或原始数据。
- GPU 模式下容器内 `torch.cuda.is_available()` 为 true，并完成一条真实 TabPFN 分类和回归 Run。
- 完成一条上传、Run、中断、回答、恢复、结果和 artifact 下载的端到端验收。

不要给生产客户端使用 `curl -k` 或关闭证书校验。若用公网域名，应用组织证书策略或替换为受信
CA 证书，并继续保持 API 容器不发布明文端口。

## 6. 给 Agent 平台提供凭据

### 6.1 推荐：OIDC/OAuth2 自动取 token

正式嵌入 Agent 平台时，使用 `compose.oidc.yaml` 部署独立 Keycloak/PostgreSQL 身份服务。平台通过
OAuth2 `client_credentials` 自动获取五分钟 access token，AutoML API 通过内部 JWKS 地址验证，
不再要求运维人员每小时人工发 JWT。部署、调用、轮换和验收见
[OIDC/OAuth2 Client Credentials 接入手册](oidc-client-credentials.md)。

```bash
docker compose --env-file .env.production-single \
  -f compose.production-single.yaml \
  -f compose.oidc.yaml \
  up -d --wait
```

### 6.2 受控环境兼容：本地 HS256 签发

签发器只输出短期 HS256 token，并拒绝 canonical OpenAPI 之外的 operationId。下面的 Agent
token 可以上传、创建 Run、观察事件和读取结果，但不能回答 `HUMAN_REQUIRED` packet：

```bash
export AUTOML_TOKEN=$(
  docker compose --env-file .env.production-single -f compose.production-single.yaml \
  run --rm --no-deps --entrypoint python automl-api -m automl_api.credentials \
    --tenant partner_a \
    --subject partner-a-agent \
    --actor-type agent \
    --expires-in 3600 \
    --token-only \
    --operation getAgentInterfaceManifest \
    --operation createDatasetUpload \
    --operation signDatasetUploadParts \
    --operation finalizeDatasetUpload \
    --operation getDatasetVersion \
    --operation createRun \
    --operation getRun \
    --operation readRunEvents \
    --operation listRunOutputs \
    --operation getRunOutput \
    --operation listDecisionPackets \
    --operation getRunResult \
    --operation getArtifact \
    --operation createArtifactDownloadTicket
)

curl --cacert "$AUTOML_CA" -fsS \
  -H "Authorization: Bearer $AUTOML_TOKEN" \
  "$AUTOML_API/v1/agent/manifest"
```

目标列、i.i.d. 假设等常见 packet 在生产环境是 `HUMAN_REQUIRED`。由平台的人机确认通道按需签发
独立 human token，不能让 Agent 冒充 human：

```bash
export AUTOML_HUMAN_TOKEN=$(
  docker compose --env-file .env.production-single -f compose.production-single.yaml \
  run --rm --no-deps --entrypoint python automl-api -m automl_api.credentials \
    --tenant partner_a \
    --subject partner-a-reviewer \
    --actor-type human \
    --expires-in 900 \
    --token-only \
    --operation listDecisionPackets \
    --operation answerDecisionPacket
)
```

不同合作方必须使用不同 `tenant_id` 和 subject。只签发其实际调用的 operation scopes；生产 token
有效期不得超过业务所需时间。HS256 密钥轮换需要协调所有签发方；可接入组织 OIDC/JWKS 后由
身份平台负责签发和轮换，不再使用本地签发脚本。

## 7. 备份、校验与恢复

每天至少执行一次备份，并把生成目录复制到另一故障域。备份工具使用 SQLite online backup、
对象快照和 SHA-256 manifest：

```bash
COMPOSE="docker compose --env-file .env.production-single -f compose.production-single.yaml"

$COMPOSE exec -T automl-api python -m automl_api.backup create
$COMPOSE exec -T automl-api python -m automl_api.backup verify /var/backups/automl/BACKUP_ID
```

`automl-backup` 侧车默认每 24 小时创建一次备份，失败后 5 分钟重试，并按保留数量清理旧备份。
工具在对象复制前后比较两份 SQLite 逻辑快照；若状态持续变化，会放弃本次备份并重试，
避免把不一致的元数据和对象集合标记为成功。仍须把备份复制到另一故障域，并监控最近成功时间。

每次升级前手工创建并验证备份。恢复是维护操作，必须同时停止入口、API 和自动备份侧车，
明确指定已验证的备份目录，并传入 `--force`：

```bash
$COMPOSE stop gateway automl-backup automl-api
$COMPOSE run --rm --no-deps --entrypoint python automl-api \
  -m automl_api.backup restore /var/backups/automl/BACKUP_ID --force
$COMPOSE up -d --wait automl-api gateway automl-backup
```

恢复会保留 `tabpfn-cache` 等不属于备份的运行缓存，并把旧的受管状态移动到 state 目录下的
`.pre-restore-*` 回滚目录。恢复后重新执行第 5 节全部验收；确认无误并已有异地备份后，再按审批
流程清理回滚目录。仅把备份留在同一块数据盘不构成灾备。

### 7.1 从旧 named volume 迁移

旧 Compose 使用 Docker named volume，而本部署使用显式 bind mount。不能直接启动后把空目录当成
升级成功。先用新镜像的 backup 工具只读挂载旧 volume，创建并验证一致性备份，再恢复到新 state；
迁移期间保留旧 volume 和旧容器用于回滚。恢复后至少执行 SQLite `quick_check`、对象 SHA-256、
TabPFN checkpoint、分类/回归 Run 和完整 API 闭环验收。两个 API 不得同时写同一状态目录。

## 8. 日常运维

```bash
# 服务、健康和资源
$COMPOSE ps
docker stats --no-stream

# JSON 应用审计日志和网关访问日志
$COMPOSE logs --since 30m automl-api
$COMPOSE logs --since 30m gateway

# CUDA 验证（GPU 模式）
$COMPOSE exec -T automl-api python -c \
  "import torch; print(torch.cuda.is_available(), torch.cuda.get_device_name(0))"
```

监控至少覆盖 5xx、429、请求延迟、活跃请求、SSE 连接/拒绝、磁盘空间、备份年龄、容器重启、Run 失败率、GPU
显存和温度。日志目录由 Docker `json-file` 轮转控制；集中采集时继续按 `tenant_id` 和关联 ID
定位，禁止采集 Bearer token 和数据内容。Uvicorn 原始 access log 已关闭，访问审计由应用输出；
下载票据路径同时从 Caddy access log 排除。

## 9. 升级与回滚

1. 固定待发布 Git commit、API 镜像 tag/digest、`os/arch` 和 Caddy digest；先在同配置的预发布主机验收。
   源构建使用 `uv.lock` 与 `requirements.production.lock` 固定 Python 依赖。
2. 创建并校验备份，保存旧环境文件和旧镜像 digest。
3. 构建新镜像，执行 `config --quiet`，再用 `up -d --wait` 替换。
4. 执行第 5 节验收；任何检查失败立即停止流量。
5. 回滚到旧镜像。仅在状态 schema/数据已经不兼容时，停止服务并从升级前备份恢复。

单节点升级会有短暂中断；不要并行启动两个 API 实例访问同一 SQLite 和对象目录。

## 10. 停止与卸载

```bash
$COMPOSE down
```

`down` 不删除 bind-mounted 状态、备份、密钥或 Caddy CA。数据销毁必须另行审批、确认精确目录并
执行可审计的删除流程，不要使用 `down -v` 代替数据生命周期管理。
