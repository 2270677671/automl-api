# Managed AutoML Python SDK

This package provides a synchronous Python client for the Managed AutoML HTTP API.
The API service owns deterministic dataset, workflow, event, output, and artifact
operations; an external Agent platform owns any LLM orchestration and credentials.
It also wraps the production control-plane routes for approvals, model candidates,
Webhook endpoints/deliveries, and deletion jobs.

## Compatibility and installation

SDK `0.7.x` is compatible with API `0.7.x`; the service manifest reports the
machine-readable range `>=0.7,<0.8`.

Install the delivered wheel:

```bash
python -m pip install automl_sdk-0.7.0-py3-none-any.whl
```

## Minimal workflow

```python
from automl_sdk import AutoMLClient

with AutoMLClient("http://127.0.0.1:8000", token="platform-service-token") as api:
    manifest = api.get_agent_manifest()
    dataset = api.upload_dataset_file("customer_churn.csv", name="customer-churn")
    run = api.create_run(
        dataset_version_id=dataset["dataset_version_id"],
        objective={
            "backend_id": "sklearn",
            "target_column": "churned",
            "task_type": "BINARY_CLASSIFICATION",
            "positive_class": 1,
            "iid_confirmed": True,
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
    result = api.wait_for_result(run["run_id"])
    print(manifest["service_version"], result["outcome"])
```

For an interrupted Run, use `wait_for_question()` and `answer_and_wait()`.
For process output use `get_run_events()`/`stream_run_events()` and
`iter_outputs()`. Artifact downloads should use `download_artifact_file()` so
ticket refresh, Range resume, ETag, size, and SHA-256 checks remain consistent.

The external Agent platform must keep the Bearer token outside the LLM prompt.
Use `get_agent_context()` and `list_agent_actions()` to obtain the bounded tool
surface, then invoke the canonical SDK method only after validating structured
model output.
