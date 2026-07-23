# Changelog

All notable changes to this project are documented here. Versions follow
Semantic Versioning for the public HTTP and Python SDK contracts.

## Unreleased

- Added production delivery guidance, a complete v1 API design document, a production environment
  template, and GitHub Actions CI coverage for linting, OpenAPI generation, tests, and release
  input verification.

## 0.7.0 - 2026-07-24

- Added built-in scikit-learn, AutoGluon Tabular, and TabPFN execution backends behind one
  resumable Run, output, artifact, and result protocol.
- Added backend discovery, readiness, limits, runtime requirements, artifact metadata, and
  `objective.backend_id` selection to the Agent-facing API and Python SDK.
- Preserved the group-aware development boundary and sealed holdout across frameworks; AutoGluon
  exports a deployment clone, while TabPFN exports data-free evaluation metadata only.
- Added framework smoke tests, standard framework dependencies, and domestic-source Docker
  packaging for the complete three-backend service.

## 0.6.0 - 2026-07-23

- Added the external Agent manifest, bounded run context, and state-scoped
  action discovery endpoints while keeping all LLM execution outside the API.
- Added wheel-packaged OpenAPI resources and an environment-configurable server
  entry point.
- Added a non-root OCI image, a hardened single-node Compose example, and
  distribution verification scripts.
- Added the standalone `automl-sdk` wheel as the supported Python client
  distribution alongside the API wheel.

## 0.5.0 - 2026-07-22

- Added durable local state, byte upload/download, background execution, and
  resumable SDK workflows.
