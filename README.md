# Managed AutoML API

> [!WARNING]
> The Dockerfile defines a single-node partner-preview target plus a fail-closed formal production
> target. The formal target bundles OIDC/JWKS, PostgreSQL, S3/KMS, and Webhook client dependencies,
> but it does not wire those external runtime adapters. Consequently, in version 0.7.0,
> `AUTOML_DEPLOYMENT_PROFILE=production` deliberately keeps `/readyz` at `503`
> regardless of environment configuration. A later code release must wire and validate the external
> runtime adapters before that hard fail-closed check can be removed.

This repository provides an API-first, resumable AutoML workflow and a synchronous Python SDK. In
the default local profile it can:

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
  and a backend artifact; TabPFN currently returns data-free evaluation metadata rather than a
  loadable model because its native fit state contains development data;
- download artifacts with expiring tickets, byte ranges, resume support, and integrity checks.
- manage production-control resources for Webhook endpoints, delivery outbox/redelivery, approval
  decisions, deletion jobs, and approved `ModelCandidate` records.

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

Docker builds default to domestic sources for the Python base image and pip downloads:
`docker.m.daocloud.io/library/python:3.12-slim` and
`https://pypi.tuna.tsinghua.edu.cn/simple`. Override them when needed:

```bash
docker build \
  --build-arg PYTHON_BASE_IMAGE=python:3.12-slim \
  --build-arg PIP_INDEX_URL=https://pypi.org/simple \
  -t managed-automl-api:0.7.0 .
```

For Compose, set `AUTOML_PYTHON_BASE_IMAGE`, `AUTOML_PIP_INDEX_URL`, and (when loading or
publishing a differently named image) `AUTOML_IMAGE` in `.env`. `AUTOML_IMAGE` also accepts a
registry reference pinned by digest, for example `registry.example.com/automl/api@sha256:...`.
`AUTOML_BIND_ADDRESS` defaults to `127.0.0.1`; set it to a specific private interface address only
when callers on that trusted network must reach the API.

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
  --docker-image managed-automl-api:0.7.0
```

An exported Docker image matches the build host's CPU architecture. For a different target
architecture, let the receiver build from the included Dockerfile or publish a multi-architecture
image through the target registry.

The receiver should verify the bundle before installation:

```bash
cd managed-automl-0.7.0-20260724T120000Z
sha256sum -c SHA256SUMS  # macOS: shasum -a 256 -c SHA256SUMS
python -m pip install wheels/automl_sdk-0.7.0-py3-none-any.whl
# Only when the bundle includes images/*.tar:
docker load --input images/managed-automl-api_0.7.0.tar
```

For a controlled partner preview, use the default `AUTOML_DEPLOYMENT_PROFILE=partner-preview` and
configure the authentication mode appropriate to that environment. The formal production target
sets `AUTOML_DEPLOYMENT_PROFILE=production`; provide OIDC/JWKS (`AUTOML_JWKS_URL` or
`AUTOML_JWKS_JSON`), `AUTOML_JWT_ISSUER`, `AUTOML_JWT_AUDIENCE`, PostgreSQL/RLS, S3/KMS, DLP,
Webhook, deletion, model-registry, worker-isolation, `AUTOML_CURSOR_SECRET`, and
`AUTOML_TICKET_SECRET` configuration. These values are inputs for integration testing; in 0.7.0 they
cannot make the formal profile ready. `/readyz` always returns `503 production_preflight_failed`
because the required external runtime adapters are not wired into request execution.
Every public operation requires its exact scope:
`automl:operation:<operationId>`.

Production delivery is intentionally gate-based. The formal image installs client dependencies and
exposes the control-plane APIs, but dependency presence or environment strings never count as
runtime readiness. Final exposure requires implementing and validating external identity, DLP,
RLS, object storage, worker isolation, Webhook dispatcher, observability, and backup gates.

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

The local vertical slice deliberately does not provide:

- a complete external-infrastructure implementation for PostgreSQL/RLS, S3/KMS, production DLP,
  audit controls, or high-availability identity federation; the image provides fail-closed
  configuration and dependency gates, while those systems must be validated and operated by the
  deployer;
- high availability, multi-process workers, distributed leases, lease heartbeats, or PostgreSQL
  transactional projections;
- an outbound Webhook HTTP dispatcher, distributed deletion worker, model-serving endpoint, or
  automated production-quality gate; the API does provide the corresponding Webhook outbox,
  approval, model-candidate, and deletion control-plane resources;
- group/time-series/multiclass tasks, relational datasets, arbitrary model search, inference serving,
  or automatic production eligibility;
- an internal LLM planner (by design), production-safe external LLM data transfer, or an endpoint
  that executes arbitrary Agent-generated tool calls.

The experiment routes remain compatibility placeholders: listing returns an empty page and a
specific experiment returns `404`. In contrast, approval decisions, model-candidate lookup,
dataset deletion jobs, and Webhook endpoint/outbox management are implemented. The local durable
deletion path revokes access and physically removes local dataset/upload and derived artifact bytes;
production storage still needs a separate deletion worker. A deployment also needs a dispatcher to
perform actual outbound Webhook HTTP delivery.

See [docs/api-usage.md](docs/api-usage.md) for the API workflow and examples,
[docs/api-route-reference.md](docs/api-route-reference.md) for per-route usage,
[docs/complete-api-design.md](docs/complete-api-design.md) for the full v1 API design,
[docs/external-agent-integration.md](docs/external-agent-integration.md) for the platform boundary,
[docs/framework-backends.md](docs/framework-backends.md) for the scikit-learn/AutoGluon/TabPFN
backend contracts, [docs/production-delivery.md](docs/production-delivery.md) for the production
handoff gates, [docs/test-report-0.7.0.md](docs/test-report-0.7.0.md) for the itemized verification
report, and [openapi/automl-api.yaml](openapi/automl-api.yaml) for the canonical schema.

## Verify

```bash
pytest
ruff check .
ruff format --check .
```
