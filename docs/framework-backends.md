# Tabular backend integration

The service ships three in-process tabular backends. They share dataset parsing, target validation,
duplicate grouping, leakage checks, and a one-shot sealed holdout. Each backend owns only its
development-set model selection, native preprocessing, prediction adapter, and artifact format.

| Backend | Pinned range | Selection boundary | Runtime | Artifact |
| --- | --- | --- | --- | --- |
| scikit-learn | `>=1.5,<2` | fixed 3-fold CV over LR/Ridge and random forest | CPU, one thread | trusted `joblib` pipeline |
| AutoGluon Tabular | `>=1.5,<1.6` | bounded AutoGluon validation on development data; RF/XT/LR portfolio | CPU=1, GPU=0 by default | deployment-only predictor directory in `tar.gz` |
| TabPFN | `>=8.1,<8.2` | fold-fitted small-data evaluation, then one development fit | GPU recommended; guarded CPU mode | data-free evaluation metadata JSON; no fitted model export |

The service wheel always ships all three adapters, while framework packages are installed by
profile. The base install includes scikit-learn; `.[autogluon]`, `.[tabpfn]`, or
`.[all-backends]` add the heavier runtimes. The normal Docker build installs `all-backends`, so its
manifest can expose all three when their separate runtime gates pass. The default image uses a
CPU-only PyTorch wheel; GPU execution requires a separately configured image and NVIDIA runtime.
Installation alone does not make TabPFN ready: model-weight access and license gates are checked
separately at runtime.

## AutoGluon

The adapter uses `TabularPredictor` with a deliberately bounded `RF`/`XT`/`LR` portfolio. It keeps
AutoGluon's own native booster add-ons disabled and uses this service's direct TabPFN 8.1 adapter
instead of AutoGluon 1.5's separate TabPFN integration.

`AUTOML_AUTOGLUON_TIME_LIMIT_SECONDS` is an operator ceiling, not a replacement for the Run budget.
The actual `fit(time_limit=...)` is the smaller of that ceiling and
`RunBudget.max_wall_time_seconds` after reserving time for evaluation and packaging. The default is
20 seconds. AutoGluon has no hard process-kill guarantee, so an individual model fit may finish
slightly after its cooperative time limit.

Predictor directories are created below `AUTOML_BACKEND_WORK_DIR` when configured and are removed
in a `finally`-equivalent `TemporaryDirectory` scope. Before packaging, the adapter calls
`clone_for_deployment(return_clone=True)`, which keeps the selected inference model and removes
training-only data and unused models. The archive still contains Python pickle state and must only
be loaded from this trusted artifact store with a compatible AutoGluon runtime.

AutoGluon 1.5 is Apache-2.0 and supports Python `>=3.10,<3.14`. See the
[AutoGluon installation guide](https://auto.gluon.ai/stable/install.html) and
[`TabularPredictor`](https://auto.gluon.ai/stable/api/autogluon.tabular.TabularPredictor.html).

## TabPFN

TabPFN is not credential-free. The backend reports `available=true` only when all of these are true:

1. the `tabpfn` package is importable;
2. `AUTOML_TABPFN_LICENSE_ACCEPTED=true` is set by the operator;
3. either `TABPFN_TOKEN` is present for headless first-use download or
   `AUTOML_TABPFN_MODEL_PATH` points to an existing checkpoint.

The token remains process-local and is never copied into Run records, output payloads, logs, or
artifacts. For an offline deployment, prefetch an approved checkpoint into a protected volume and
set `AUTOML_TABPFN_MODEL_PATH`.
The Compose profile sets `TABPFN_MODEL_CACHE_DIR=/var/lib/automl/tabpfn-cache`, which is on the
persistent state volume instead of the container's read-only filesystem.

TabPFN's native `save_fit_state` contains the development feature matrix, labels, preprocessing
state, and categorical vocabulary. That conflicts with this API's artifact data boundary. The
backend therefore performs real training and evaluation but returns only a small metadata JSON with
`exportable=false`, `contains_model_state=false`, and `contains_training_data=false`. It does not
return a loadable TabPFN model until a separate encrypted, policy-approved export design exists.

The direct adapter uses median imputation for numeric values and fold-fitted ordinal encoding for
categorical values. It does not scale features or one-hot expand them. The categorical column
indices are passed to the native `TabPFNClassifier` or `TabPFNRegressor`. Each fold fits its own
preprocessor, so validation values cannot influence imputation or category mappings.

The profile enforces conservative operational limits:

- CPU: at most 1,000 rows;
- configured non-CPU device: at most 100,000 rows;
- all devices: at most 2,000 usable features;
- four TabPFN estimators, `low_memory` fit mode, one preprocessing worker.

TabPFN 8.1 does not expose a cooperative wall-time cancellation hook for a running forward pass.
The service checks pause/cancel at workflow checkpoints, but cannot interrupt one in-process TabPFN
prediction safely. Production isolation therefore still needs a supervised worker process with a
hard resource deadline.

TabPFN's package code uses the Prior Labs License (Apache 2.0 with an additional attribution
provision). Current default TabPFN-3 model weights require separate access and are documented as
non-commercial. Shipping the Python package does **not** grant permission to redistribute or use
those weights commercially. The operator must review and accept the exact checkpoint license before
enabling this backend. Refer to the [TabPFN repository](https://github.com/PriorLabs/TabPFN) and
[model documentation](https://priorlabs.ai/docs/models) for current terms and hardware limits.

## API behavior

The Agent platform should discover backend readiness from `GET /v1/agent/manifest` or the Python
SDK's `list_backends()` helper, then select `objective.backend_id` as `sklearn`, `autogluon`,
or `tabpfn`. Do not infer readiness from package installation alone. A backend descriptor
distinguishes `installed` from `available`, returns stable `unavailable_reason` codes, and
publishes limits, runtime requirements, and the artifact contract.

All three backends produce evaluation artifacts and do not host an inference endpoint. The default
Run therefore reports `model_disposition=NO_ELIGIBLE_MODEL`. With
`production_deploy=REQUIRE_APPROVAL`, scikit-learn or AutoGluon results can become an approved
control-plane `ModelCandidate`; this still does not deploy serving infrastructure. Scikit-learn and
AutoGluon return trusted-store model artifacts, while TabPFN returns only non-exportable evaluation
metadata and cannot become an exportable serving artifact.
