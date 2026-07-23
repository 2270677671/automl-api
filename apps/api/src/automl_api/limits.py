from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from .errors import APIProblem


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as error:
        raise ValueError(f"{name} must be an integer.") from error
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum}.")
    return value


def _env_float(name: str, default: float, *, minimum: float = 0.0) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError as error:
        raise ValueError(f"{name} must be a number.") from error
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum}.")
    return value


@dataclass(frozen=True, slots=True)
class RuntimeLimits:
    max_dataset_bytes: int = 100 * 1024 * 1024
    max_upload_part_bytes: int = 100 * 1024 * 1024
    max_active_runs_per_tenant: int = 4
    max_storage_bytes_per_tenant: int = 1024 * 1024 * 1024
    max_trials_per_run: int = 20
    max_wall_time_seconds: int = 3600
    max_compute_credits: float = 20.0

    @classmethod
    def from_env(cls) -> RuntimeLimits:
        defaults = cls()
        return cls(
            max_dataset_bytes=_env_int(
                "AUTOML_MAX_DATASET_BYTES",
                defaults.max_dataset_bytes,
            ),
            max_upload_part_bytes=_env_int(
                "AUTOML_MAX_UPLOAD_PART_BYTES",
                defaults.max_upload_part_bytes,
            ),
            max_active_runs_per_tenant=_env_int(
                "AUTOML_MAX_ACTIVE_RUNS_PER_TENANT",
                defaults.max_active_runs_per_tenant,
            ),
            max_storage_bytes_per_tenant=_env_int(
                "AUTOML_MAX_STORAGE_BYTES_PER_TENANT",
                defaults.max_storage_bytes_per_tenant,
            ),
            max_trials_per_run=_env_int(
                "AUTOML_MAX_TRIALS_PER_RUN",
                defaults.max_trials_per_run,
            ),
            max_wall_time_seconds=_env_int(
                "AUTOML_MAX_WALL_TIME_SECONDS",
                defaults.max_wall_time_seconds,
                minimum=60,
            ),
            max_compute_credits=_env_float(
                "AUTOML_MAX_COMPUTE_CREDITS",
                defaults.max_compute_credits,
            ),
        )

    def manifest(self) -> dict[str, int | float]:
        return {
            "max_dataset_bytes": self.max_dataset_bytes,
            "max_upload_part_bytes": self.max_upload_part_bytes,
            "max_active_runs_per_tenant": self.max_active_runs_per_tenant,
            "max_storage_bytes_per_tenant": self.max_storage_bytes_per_tenant,
            "max_trials_per_run": self.max_trials_per_run,
            "max_wall_time_seconds": self.max_wall_time_seconds,
            "max_compute_credits": self.max_compute_credits,
        }

    def validate_budget(self, budget: dict[str, Any]) -> None:
        violations: dict[str, dict[str, int | float]] = {}
        checks = {
            "max_trials": (int(budget["max_trials"]), self.max_trials_per_run),
            "max_wall_time_seconds": (
                int(budget["max_wall_time_seconds"]),
                self.max_wall_time_seconds,
            ),
            "max_compute_credits": (
                float(budget["max_compute_credits"]),
                self.max_compute_credits,
            ),
        }
        for field, (requested, limit) in checks.items():
            if requested > limit:
                violations[field] = {"requested": requested, "limit": limit}
        if violations:
            raise APIProblem(
                422,
                "budget_limit_exceeded",
                "Run budget exceeds this service profile",
                "Reduce the requested bounded compute budget and create the Run again.",
                extras={"violations": violations, "runtime_limits": self.manifest()},
            )


__all__ = ["RuntimeLimits"]
