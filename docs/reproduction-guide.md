# Managed AutoML API 完整复现指南

## 1. 目的与复现范围

本文说明如何从公开 GitHub 仓库复现以下交付物：

1. 本地开发 API 和 Python SDK 调用闭环。
2. 国内镜像源构建的 CPU Docker 服务。
3. 带 HTTPS、JWT、备份和资源限制的单机生产服务。
4. 可选的 AutoGluon 与 TabPFN/CUDA 后端。
5. OpenAPI、测试门禁和 release bundle。

本文不把 `cluster-production` 描述为已实现的集群方案；该 profile 在 PostgreSQL/RLS、S3/KMS、
独立 worker 和高可用 adapter 接入前会主动返回未就绪。

## 2. 环境要求

| 组件 | 最低要求 | 说明 |
| --- | --- | --- |
| Git | 2.39+ | 获取源码和核对版本 |
| Python | 3.11-3.13 | AutoGluon 1.5 不支持 Python 3.14 |
| uv | 当前稳定版 | 依赖同步、测试和运行 |
| Docker | Engine 24+、Compose v2 | 容器复现和生产部署 |
| OpenSSL | 3.x 或系统兼容版 | 生成生产随机密钥 |
| GPU 可选 | NVIDIA driver + Container Toolkit | TabPFN CUDA 路径 |

Linux 是生产部署目标。macOS/Windows 可执行本地开发和 CPU 单元测试，但不能证明 Linux GPU
容器可用。主机时钟应通过 NTP 同步，JWT 校验依赖正确时间。

## 3. 获取并核对源码

```bash
git clone https://github.com/2270677671/automl-api.git
cd automl-api
git status --short --branch
git log -1 --oneline
```

稳定交付应固定 commit 或 release tag，不要在生产构建中直接跟随浮动 `main`。仓库不得出现真实
`.env`、JWT、CA 私钥、TabPFN token、云凭据或用户数据。

## 4. 路径 A：本地开发复现

### 4.1 安装基础后端

```bash
uv sync --extra dev
uv run automl-api
```

默认监听 `http://127.0.0.1:8000`，状态持久化到 `.automl-data/`。另开终端检查：

```bash
curl -fsS http://127.0.0.1:8000/healthz
curl -fsS http://127.0.0.1:8000/readyz
curl -fsS http://127.0.0.1:8000/openapi.yaml | head
curl -fsS http://127.0.0.1:8000/v1/agent/manifest \
  -H 'Authorization: Bearer local-development-token'
```

预期 `/healthz` 和 `/readyz` 返回 HTTP 200。本地 profile 只用 token 哈希构造合成租户，不是生产
身份认证。

### 4.2 安装全部后端

```bash
uv sync --extra dev --extra all-backends
```

AutoGluon 安装完成后通常可直接运行。TabPFN package 已安装不代表权重和许可已就绪；未满足条件时
manifest 必须返回 `available=false` 和具体 `unavailable_reason`。

### 4.3 执行 SDK 与原始 HTTP 案例

```bash
AUTOML_API_URL=http://127.0.0.1:8000 \
AUTOML_TOKEN=local-development-token \
PYTHONPATH=packages/python_sdk/src \
uv run python examples/python/sdk_guided_workflow.py

AUTOML_API_URL=http://127.0.0.1:8000 \
AUTOML_TOKEN=local-development-token \
uv run python examples/python/http_guided_workflow.py
```

两套案例都使用 `examples/data/customer_churn.csv`，并覆盖上传、finalize、创建 Run、人工问题、
继续执行、事件、结果和 artifact。生成文件默认写入 `example-output/`，该目录已被 Git 忽略。

## 5. 路径 B：CPU Docker 复现

### 5.1 准备配置

```bash
cp .env.example .env
chmod 600 .env
openssl rand -hex 48
openssl rand -hex 48
openssl rand -hex 48
```

把三个不同的随机值分别填写为 `AUTOML_JWT_SECRET`、`AUTOML_CURSOR_SECRET` 和
`AUTOML_TICKET_SECRET`。示例默认使用 DaoCloud Python 基础镜像和清华 PyPI 镜像；可在 `.env`
中切换到组织批准的镜像源。

### 5.2 构建与启动

```bash
docker compose --env-file .env build automl-api
docker compose --env-file .env up -d --no-build
docker compose --env-file .env ps
curl -fsS http://127.0.0.1:8000/healthz
```

该 Compose 是本机/合作方预览路径，默认只绑定 loopback。不要把明文 `:8000` 直接暴露到公网。

## 6. 路径 C：单机生产复现

### 6.1 初始化

准备一个 Agent 平台可以解析的 DNS 名或私网 IP，并仅在受控网络开放 `8443/tcp`。从仓库根目录执行：

```bash
chmod +x scripts/init_single_node_production.sh
./scripts/init_single_node_production.sh \
  automl.internal.example.com \
  0.0.0.0 \
  .env.production-single \
  .automl-production
chmod 600 .env.production-single
```

脚本生成三个独立随机密钥和权限为 `0700` 的 state、backup、Caddy 目录，并拒绝覆盖已有 env。
启动前检查 CPU、内存、数据集大小、租户配额、备份周期和 HTTPS 绑定地址。

### 6.2 构建、启动和等待健康

```bash
docker compose \
  --env-file .env.production-single \
  -f compose.production-single.yaml \
  build automl-api

docker compose \
  --env-file .env.production-single \
  -f compose.production-single.yaml \
  up -d --no-build --wait --wait-timeout 300
```

生产 profile 只有 Caddy 发布 `8443`，API 容器不发布明文端口。提取私有 CA 并分发给受信客户端：

```bash
export AUTOML_CA=.automl-production/caddy-data/caddy/pki/authorities/local/root.crt
export AUTOML_API=https://automl.internal.example.com:8443
curl --cacert "$AUTOML_CA" -fsS "$AUTOML_API/healthz"
```

生产客户端不得使用 `curl -k` 或关闭 TLS 校验。公网部署应改用组织认可的证书与域名策略。

### 6.3 签发最小权限凭据

Agent token 至少需要本案例所使用的 operation：

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
    --operation finalizeDatasetUpload \
    --operation createRun \
    --operation getRun \
    --operation readRunEvents \
    --operation listRunOutputs \
    --operation listDecisionPackets \
    --operation getRunResult \
    --operation getArtifact \
    --operation createArtifactDownloadTicket
)
```

`HUMAN_REQUIRED` packet 必须由同一 tenant 的 human token 回答：

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
    --operation answerDecisionPacket \
    --operation getCommand
)
```

真实接入优先使用组织 OIDC/JWKS 或 workload identity；内置 HS256 签发器适用于受控单机部署。

### 6.4 从外部执行验收案例

```bash
AUTOML_API_URL="$AUTOML_API" \
AUTOML_CA_FILE="$AUTOML_CA" \
AUTOML_TOKEN="$AUTOML_TOKEN" \
AUTOML_HUMAN_TOKEN="$AUTOML_HUMAN_TOKEN" \
PYTHONPATH=packages/python_sdk/src \
uv run python examples/python/sdk_guided_workflow.py
```

预期结果：Run 先进入 `WAITING_USER`，human token 回答后进入 `TERMINAL/SUCCEEDED`，事件同时可通过
JSON 和 SSE 读取，下载文件的大小与 SHA-256 和票据一致。

## 7. GPU 与 TabPFN 复现

部署方必须先接受适用的 Prior Labs 模型许可，并准备批准的 checkpoint 或下载凭据。只有完成该
组织行为后才能在 `.env.production-single` 中设置：

```dotenv
AUTOML_TABPFN_LICENSE_ACCEPTED=true
AUTOML_TABPFN_DEVICE=cuda
AUTOML_TABPFN_MODEL_SOURCE=public-v2
```

使用 NVIDIA Container Toolkit：

```bash
docker compose \
  --env-file .env.production-single \
  -f compose.production-single.yaml \
  -f compose.gpu.yaml \
  build automl-api

docker compose \
  --env-file .env.production-single \
  -f compose.production-single.yaml \
  -f compose.gpu.yaml \
  up -d --no-build --wait --wait-timeout 300
```

若主机暂时无法安装 Container Toolkit，可按[单机生产部署手册](single-node-production.md)审查并使用
`compose.gpu-direct.yaml`。该方式依赖 Linux NVIDIA 驱动路径，不是可移植默认方案。

容器内验证：

```bash
docker compose \
  --env-file .env.production-single \
  -f compose.production-single.yaml \
  -f compose.gpu.yaml \
  exec automl-api python -c \
  'import torch; print(torch.__version__, torch.cuda.is_available(), torch.cuda.get_device_name(0))'
```

随后读取 `/v1/agent/manifest`，只有 `tabpfn.available=true` 才提交
`examples/requests/tabpfn-regression.json`。对外展示结果时保留 `Built with PriorLabs-TabPFN`。

## 8. 备份和恢复验证

创建并校验在线备份：

```bash
docker compose --env-file .env.production-single -f compose.production-single.yaml \
  exec automl-api python -m automl_api.backup create

docker compose --env-file .env.production-single -f compose.production-single.yaml \
  exec automl-api python -m automl_api.backup verify \
  /var/backups/automl/<backup-directory>
```

恢复必须在停止 API、确认目标目录和保留异地副本后执行。完整恢复、回滚和保留策略见
[单机生产部署手册](single-node-production.md#7-备份校验与恢复)。

## 9. 源码与契约门禁

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
uv lock --check
uv run python scripts/generate_agent_openapi.py --check
PYTHONPATH=apps/api/src uv run python -m automl_api.production
git diff --check
```

生成可验证交付包：

```bash
uv run python scripts/package_release.py
cd dist/releases/managed-automl-0.8.0-<timestamp>
sha256sum -c SHA256SUMS
```

macOS 使用 `shasum -a 256 -c SHA256SUMS`。默认包包含 API/SDK wheel、源码、OpenAPI、部署文件、
本文档和 `examples/`，不包含大型 Docker 镜像；离线镜像通过 `--docker-image` 单独加入。

## 10. 复现完成判定

- `/healthz=200`，生产容器内 `/readyz=200`。
- 外部生产入口不能访问 `/readyz` 和 `/metrics`。
- 无 token 返回 401，错误 scope 返回 403，错误 audience 返回 401，错误 Host 返回 421。
- sklearn 上传、中断、human 回答、恢复、结果、事件和 artifact 完成闭环。
- 已启用的 AutoGluon/TabPFN 后端各完成至少一个真实 Run。
- 新备份通过 manifest、文件大小、SHA-256 和 SQLite `quick_check`。
- 仓库与 release bundle 中不存在真实凭据、生产 env、私钥或用户数据。

遇到问题时先读取容器状态、应用审计日志和结构化 problem response，不要把 Bearer、请求体或数据内容
粘贴到公开 issue。
