# Managed AutoML API

> [!IMPORTANT]
> `single-node-production` is the supported small-scale production profile: JWT authentication,
> private HTTPS, Host allowlisting, request limits, audit logs, metrics, SQLite WAL, immutable local
> objects, serialized training, automatic verified backups, and Docker resource boundaries are
> wired into the runtime. `cluster-production` remains fail-closed until PostgreSQL/RLS, S3/KMS,
> isolated workers, and high-availability adapters are connected to the actual request path.

This repository provides an API-first, resumable AutoML workflow and a synchronous Python SDK. In
the default local profile it can:

## 中文文档与可运行示例

| 需求 | 入口 |
| --- | --- |
| 从 GitHub 完整复现 | [完整复现指南](docs/reproduction-guide.md) |
| 使用者操作说明 | [使用手册](docs/user-manual.md) |
| 获取带完整案例的单文件手册 | [API 使用手册与示范案例](docs/api-user-guide-with-examples.md) |
| 查询全部 API 路由 | [API 路由使用手册](docs/api-route-reference.md) |
| 运行 SDK/原始 HTTP 案例 | [examples/README.md](examples/README.md) |
| 嵌入 Agent 平台 | [外部 Agent 接入契约](docs/external-agent-integration.md) |
| 接收阶段状态与下一阶段准入结果 | [阶段 Callback 契约](docs/stage-callback-contract.md) |
| 自动获取生产 access token | [OIDC/OAuth2 Client Credentials](docs/oidc-client-credentials.md) |
| 文档总目录 | [docs/README.md](docs/README.md) |

最快本地闭环：

```bash
uv sync --extra dev
uv run automl-api
# 另开终端
AUTOML_API_URL=http://127.0.0.1:8000 \
AUTOML_TOKEN=local-development-token \
PYTHONPATH=packages/python_sdk/src \
uv run python examples/python/sdk_guided_workflow.py
```

**Built with PriorLabs-TabPFN.** TabPFN use is subject to the Prior Labs License and the deployment
operator's model-weight terms; see [framework backend notes](docs/framework-backends.md).

- stream a real CSV or Parquet file into immutable local storage and verify its size, part ETag, and
  SHA-256 digest;
- persist API resources, idempotency results, workflow checkpoints, and execution jobs in SQLite;
- profile a single table without returning raw cell values through the API;
- pause with a structured `DecisionPacket` when the target, i.i.d. assumption, or positive class
  requires a user decision, then continue from its checkpoint;
- select a standard tabular execution backend per Run from scikit-learn, AutoGluon, and TabPFN;
  scikit-learn remains the compatibility default and the Manifest reports each backend's current
  image/runtime availability;
- evaluate bounded candidate pipelines for binary classification or regression while preserving a
  sealed holdout and framework-specific artifact metadata;
- publish immutable outputs, JSON/SSE events, a terminal result, a split manifest, a run report,
  aggregate sealed-holdout evaluation PNGs, and a backend artifact; TabPFN currently returns data-free evaluation metadata rather than a
  loadable model because its native fit state contains development data;
- download artifacts with expiring tickets, byte ranges, resume support, and integrity checks.
- manage production-control resources for Webhook endpoints, signed HTTP delivery with durable
  retry/redelivery, per-stage `callback_uri` status summaries, approval decisions, deletion jobs,
  and approved `ModelCandidate` records.

The service never calls an LLM. A separate Agent platform may discover this API, read a bounded
Run context, and invoke the existing versioned operations. The platform owns model selection,
prompts, credentials, and LLM lifecycle; this service remains a deterministic AutoML execution
backend.

## Run locally

Python 3.11 through 3.13 is required. AutoGluon 1.5 does not support Python 3.14.

```bash
python3 -m pip install -e '.[dev]'
automl-api
```

The base install includes the scikit-learn backend. To install the two heavier optional backends
for local development, use `python3 -m pip install -e '.[dev,all-backends]'`. The Docker image
installs `all-backends` by default.

The API listens on `http://127.0.0.1:8000`. Health and readiness probes are available at
`/healthz` and `/readyz`; the canonical control-plane contract is served at `/openapi.yaml`, and
the active external-Agent tool contract is served at `/v1/agent/tool-openapi.yaml`. Upload and
artifact-download data-plane URLs are issued by control-plane responses and are intentionally not
listed as independently constructible OpenAPI operations.

By default, metadata and jobs are stored in `.automl-data/automl.db`, while dataset and artifact
bytes are stored below `.automl-data/objects`. Set `AUTOML_STATE_DIR` to use another local directory.
Restarting the service restores that state and resumes non-terminal jobs.

Any non-empty Bearer token is accepted in this development profile. A hash of the token determines
the synthetic tenant. This is useful for local isolation tests, but it is not JWT validation or a
production authentication boundary.

Docker builds default to domestic sources for the digest-pinned Python base image and pip downloads:
`docker.m.daocloud.io/library/python:3.12-slim@sha256:...` and
`https://pypi.tuna.tsinghua.edu.cn/simple`. The full three-backend image pins the CPU-only PyTorch
wheel from `https://download.pytorch.org/whl/cpu`, avoiding unused CUDA layers; a GPU image requires
a separately reviewed Torch index/version and NVIDIA Container Toolkit configuration. Override the
build sources when needed:

```bash
docker build \
  --build-arg PYTHON_BASE_IMAGE=python:3.12-slim \
  --build-arg PIP_INDEX_URL=https://pypi.org/simple \
  --build-arg TORCH_FIND_LINKS=https://download.pytorch.org/whl/cpu/torch/ \
  --build-arg TORCH_VERSION=2.13.0+cpu \
  -t managed-automl-api:0.8.0 .
```

For Compose, set `AUTOML_PYTHON_BASE_IMAGE`, `AUTOML_PIP_INDEX_URL`, and (when loading or
publishing a differently named image) `AUTOML_IMAGE` in `.env`. `AUTOML_IMAGE` also accepts a
registry reference pinned by digest, for example `registry.example.com/automl/api@sha256:...`.
`AUTOML_BIND_ADDRESS` defaults to `127.0.0.1`; set it to a specific private interface address only
when callers on that trusted network must reach the API.

For an NVIDIA deployment, install NVIDIA Container Toolkit on the Docker host, then apply the GPU
Compose override. The GPU image pins the CUDA 13.0 Torch wheel separately from the portable CPU
image:

```bash
cp .env.gpu.example .env.gpu
docker compose --env-file .env --env-file .env.gpu \
  -f compose.yaml -f compose.gpu.yaml build automl-api
docker compose --env-file .env --env-file .env.gpu \
  -f compose.yaml -f compose.gpu.yaml up -d --no-build
docker compose --env-file .env --env-file .env.gpu \
  -f compose.yaml -f compose.gpu.yaml exec automl-api \
  python -c 'import torch; print(torch.__version__, torch.cuda.is_available())'
```

Do not set `AUTOML_TABPFN_LICENSE_ACCEPTED=true` until the organization responsible for the
deployment has accepted the applicable model-weight license. TabPFN also requires either a
process-local `TABPFN_TOKEN` for approved first-use download or an approved checkpoint mounted in
the persistent state volume and selected with `AUTOML_TABPFN_MODEL_PATH`.

After license acceptance, `AUTOML_TABPFN_MODEL_SOURCE=public-v2` selects the official public v2
classification and regression checkpoints from `TABPFN_MODEL_CACHE_DIR`. Both files must be
prefetched before the manifest reports the backend as available. This avoids retaining a v3 API
key while keeping the credentialed `auto` and custom checkpoint modes available.

On a Linux host where the NVIDIA driver works but Container Toolkit cannot yet be installed,
`compose.gpu-direct.yaml` provides an explicit single-GPU compatibility path. It maps the NVIDIA
device nodes and read-only driver libraries without changing the host. Confirm the driver library
paths in `.env.gpu` first, then use this override instead of `compose.gpu.yaml`:

```bash
readlink -f /usr/lib64/libcuda.so.1
readlink -f /usr/lib64/libnvidia-ml.so.1
docker compose --env-file .env --env-file .env.gpu \
  -f compose.yaml -f compose.gpu-direct.yaml up -d --no-build
```

This compatibility profile is Linux- and host-driver-specific, exposes one GPU, and does not
replace Container Toolkit for portable partner deployments.

## Build a partner delivery bundle

Generate a version-checked bundle containing both wheels, the canonical and active Agent OpenAPI
contracts, Compose deployment files, integration documentation, and SHA-256 metadata:

```bash
python scripts/package_release.py
```

The command writes a new directory and `.tar.gz` below `dist/releases/`. It refuses to overwrite an
existing bundle, verifies that API/SDK/Compose/OpenAPI versions agree, and fails if the generated
Agent contract is stale. To reuse already-built wheels and include the domestic-source Docker image
as an offline-loadable tar file:

```bash
python scripts/package_release.py \
  --skip-build \
  --target-platform linux/amd64 \
  --docker-image managed-automl-api:0.8.0-production \
  --docker-image docker.m.daocloud.io/library/caddy:2.10.2-alpine@sha256:4c6e91c6ed0e2fa03efd5b44747b625fec79bc9cd06ac5235a779726618e530d
```

Repeat `--docker-image` for every offline image. Shared layers are stored once in
`images/docker-images.tar`. Bundle manifest schema 2 records the requested and loadable references,
image ID, repo digest, image/archive byte sizes, archive SHA-256 and `os/architecture`. The command
fails when images target mixed platforms or differ from `--target-platform`. Build or pull images
for the receiver's platform before packaging; a local ARM image cannot be delivered to an amd64 host.

The receiver should verify the bundle before installation:

```bash
cd managed-automl-0.8.0-20260726T120000Z
sha256sum -c SHA256SUMS  # macOS: shasum -a 256 -c SHA256SUMS
python -m pip install wheels/automl_sdk-0.8.0-py3-none-any.whl
# Only when the bundle includes images/*.tar:
docker load --input images/docker-images.tar
```

For an offline deployment, set `AUTOML_IMAGE` and `AUTOML_CADDY_IMAGE` to the manifest's
`load_reference` values after `docker load`, then use Compose with `--no-build`. A registry digest
may resolve to an untagged platform image when exported, so the original digest-qualified reference
is evidence, while `load_reference` is the local name restored by Docker.

For a third-party Agent platform, use
[the single-node production runbook](docs/single-node-production.md) and
`compose.production-single.yaml`. The API container has no host port; Caddy is the only published
entry point and issues private-CA HTTPS by default. The profile uses short-lived JWTs with exact
`automl:operation:<operationId>` scopes, and `/readyz` verifies live SQLite, object-store, worker,
and backup-directory health in addition to static configuration.

Use `cluster-production` only as an integration gate. It intentionally returns
`503 production_preflight_failed` until the external PostgreSQL/RLS, S3/KMS, dispatcher, and
isolated-worker adapters are implemented and validated.

## Python SDK quick path

The high-level SDK owns single-part streaming upload, local hashing, upload finalization, idempotency
keys, event replay, wait-set revisions, and resumable verified artifact download.

```python
from automl_sdk import AutoMLClient

with AutoMLClient("http://127.0.0.1:8000", token="local-development") as api:
    available_backends = api.list_backends(available_only=True)
    dataset = api.upload_dataset_file("customer_churn.csv", name="customer-churn")
    run = api.create_run(
        dataset_version_id=dataset["dataset_version_id"],
        objective={
            "backend_id": "sklearn",
            "target_column": None,
            "task_type": "BINARY_CLASSIFICATION",
            "positive_class": 1,
            "iid_confirmed": None,
            "primary_metric": "roc_auc",
        },
        autonomy={"mode": "GUIDED", "production_deploy": "DISABLED"},
        policy={"allow_pii": False, "allow_external_llm": False},
        budget={
            "max_trials": 3,
            "max_compute_credits": 1,
            "max_wall_time_seconds": 3600,
            "max_llm_tokens": 0,
        },
    )

    packet = api.wait_for_question(run["run_id"])
    api.answer_and_wait(
        run["run_id"],
        packet,
        {"q_target": "churned", "q_iid": True},
    )
    result = api.wait_for_result(run["run_id"])

    model_card = next(api.iter_outputs(run["run_id"], types=["MODEL_CARD"]))
    artifact = model_card["artifact_refs"][0]
    api.download_artifact_file(artifact["artifact_id"], "evaluated-model.artifact")
```

The example assumes `customer_churn.csv` has a `churned` target whose positive value is `1`. Omit
`positive_class` when it is unknown; a second `DecisionPacket` will request it if the target is
binary. Answering a packet automatically resumes the workflow, so a separate `:resume` call is not
needed. Omitting `objective.backend_id` selects the Manifest's `default_backend_id` (`sklearn` in
this profile). Do not infer backend availability from its name: inspect `backends[].available`,
`capabilities`, and `artifact` before creating a Run.

The default `production_deploy=DISABLED` flow returns
`model_disposition=NO_ELIGIBLE_MODEL`, so its artifact remains evaluation-only. When a caller sets
`production_deploy=REQUIRE_APPROVAL`, a successful training Run enters `WAITING_APPROVAL`; an
explicit approval registers a `ModelCandidate` and returns `ELIGIBLE_MODEL_AVAILABLE`. This is a
control-plane candidate record, not an inference deployment or a substitute for external quality
gates. Artifact format depends on the backend: scikit-learn returns a trusted-store `joblib`
pipeline, AutoGluon returns a trusted-store `tar.gz` predictor archive, and TabPFN returns data-free
JSON evaluation metadata with `exportable=false`.

## External Agent platform

The integration surface is read-only and does not introduce a generic action executor:

- `GET /v1/agent/manifest` describes the backend boundary and canonical OpenAPI operations;
- `GET /v1/agent/tool-openapi.yaml` returns the active OpenAPI contract containing only currently
  implemented Agent operations;
- `GET /v1/runs/{run_id}/agent-context` returns a bounded snapshot, open `DecisionPacket` objects,
  recent output references, and an event checkpoint;
- `GET /v1/runs/{run_id}/agent-actions` returns state-scoped references to the existing
  `answer/pause/resume/cancel` endpoints and their `If-Match` requirements.

The Agent platform must retain the Bearer token itself and expose only structured tool results to
the model. It must never place API credentials in an LLM prompt. Create the Run with
`policy.allow_external_llm=true` before reading its Agent context:

```python
from automl_sdk import AutoMLClient

with AutoMLClient("http://127.0.0.1:8000", token="platform-service-token") as backend:
    manifest = backend.get_agent_manifest()
    available_backends = backend.list_backends(available_only=True)
    tool_openapi = backend.get_agent_tool_openapi()
    context = backend.get_agent_context("run_123", output_limit=20)
    actions = backend.list_agent_actions("run_123")

    # After the external platform has produced and validated structured answers,
    # it calls the same canonical method used by any other API client.
    packet = context["open_decision_packets"][0]
    receipt = backend.answer_decision_packet(
        "run_123",
        packet["wait_set_id"],
        {"q_target": "churned", "q_iid": True},
        wait_set_revision=packet["wait_set_revision"],
    )
```

`agent-context` contains no raw dataset rows, but an objective or `DecisionPacket` may include class
values, column names, filenames, question text, and other data-derived content. The response marks
this boundary with `contains_raw_dataset_rows=false`, `may_include_dataset_derived_values=true`, and
`dataset_derived_text_trust=UNTRUSTED`. The current local profile has no production DLP or real
service-identity authorization, so its manifest reports
`production_external_llm_safe=false`.

`budget.max_llm_tokens` remains required only for v1 request compatibility. This backend never
consumes it and always reports zero LLM-token usage; the Agent platform must enforce its own model
budget.

DecisionPackets declare `resolution_policy`. `HUMAN_REQUIRED` packets can only be answered with a
delegated human token in production. `AGENT_ALLOWED` packets are exposed in `agent-actions`, and
agent/service tokens may submit only the packet's recommendation; otherwise the platform must pause
and request human input.

The manifest also declares runtime limits such as dataset size, upload part size, active runs per
tenant, tenant storage bytes, trials, wall time, and compute credits. Limit violations return stable
problem codes such as `dataset_too_large`, `tenant_storage_limit_exceeded`,
`active_run_limit_exceeded`, and `budget_limit_exceeded`.

Backend discovery is part of the same Manifest handshake. `default_backend_id` preserves clients
that omit a backend, while each `backends[]` descriptor reports package/runtime readiness,
supported task/media types, CPU/GPU traits, deterministic behavior, and artifact serialization.
`available=true` means the adapter can run in this service instance; it does not mean
`production_eligible=true`.

## Current boundary

The single-node production profile deliberately does not provide:

- PostgreSQL/RLS, S3/KMS, high availability, or an external identity provider; those belong to the
  fail-closed `cluster-production` target. Single-node production instead uses protected local
  state, JWT/JWKS or an independent HS256 key, private HTTPS, audit logs, and verified backups;
- high availability, multi-process workers, distributed leases, lease heartbeats, or PostgreSQL
  transactional projections;
- a multi-replica Webhook dispatcher, distributed deletion worker, model-serving endpoint, or
  automated production-quality gate; the single-node profile does include an in-process durable
  Webhook dispatcher plus approval, model-candidate, and deletion control-plane resources;
- group/time-series/multiclass tasks, relational datasets, arbitrary model search, inference serving,
  or automatic production eligibility;
- an internal LLM planner (by design), production-safe external LLM data transfer, or an endpoint
  that executes arbitrary Agent-generated tool calls.

The experiment routes remain compatibility placeholders: listing returns an empty page and a
specific experiment returns `404`. In contrast, approval decisions, model-candidate lookup,
dataset deletion jobs, and Webhook endpoint/outbox management are implemented. The local durable
deletion path revokes access and physically removes local dataset/upload and derived artifact bytes;
production storage still needs a separate deletion worker.

See [docs/api-usage.md](docs/api-usage.md) for the API workflow and examples,
[docs/api-route-reference.md](docs/api-route-reference.md) for per-route usage,
[docs/complete-api-design.md](docs/complete-api-design.md) for the full v1 API design,
[docs/external-agent-integration.md](docs/external-agent-integration.md) for the platform boundary,
[docs/framework-backends.md](docs/framework-backends.md) for the scikit-learn/AutoGluon/TabPFN
backend contracts, [docs/production-delivery.md](docs/production-delivery.md) for the production
handoff gates, [docs/single-node-production.md](docs/single-node-production.md) for deployable
single-node production, [docs/test-report-0.8.0.md](docs/test-report-0.8.0.md) for the itemized verification
report, and [openapi/automl-api.yaml](openapi/automl-api.yaml) for the canonical schema.

## Verify

```bash
pytest
ruff check .
ruff format --check .
```
