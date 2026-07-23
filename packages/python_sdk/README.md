# Managed AutoML Python SDK

This package provides a synchronous Python client for the Managed AutoML HTTP API.
The API service owns deterministic dataset, workflow, event, output, and artifact
operations; an external Agent platform owns any LLM orchestration and credentials.

Install the wheel and create an `automl_sdk.AutoMLClient` with the service URL and
Bearer token issued by the integrating platform.
