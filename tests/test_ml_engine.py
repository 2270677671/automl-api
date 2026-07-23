from __future__ import annotations

import hashlib
import io
import json
import math

import joblib
import numpy as np
import pandas as pd
import pytest
from sklearn.datasets import make_classification, make_regression
from sklearn.pipeline import Pipeline

from automl_api.ml_engine import (
    AllNullTargetError,
    ConflictingDuplicateLabelsError,
    ConstantTargetError,
    IIDAssumptionRequiredError,
    MissingTargetError,
    MulticlassTargetError,
    NoUsableFeaturesError,
    PositiveClassRequiredError,
    TargetContainsMissingError,
    TargetLeakageError,
    inspect_tabular_dataset,
    run_tabular_automl,
)
from automl_api.models import (
    BaselineResultPayload,
    DataQualityReportPayload,
    EvaluationReportPayload,
    SplitManifestPayload,
    TaskSpecPayload,
    TrialResultPayload,
)


def _csv_bytes(frame: pd.DataFrame) -> bytes:
    return frame.to_csv(index=False).encode("utf-8")


def _row_id(dataset_sha256: str, position: int) -> str:
    return hashlib.sha256(f"{dataset_sha256}:{position}".encode()).hexdigest()[:24]


def _partition_positions(split: dict[str, object], row_count: int) -> tuple[set[int], set[int]]:
    dataset_sha256 = str(split["dataset_sha256"])
    development_ids = set(split["development_row_ids"])
    test_ids = set(split["test_row_ids"])
    development = {
        position
        for position in range(row_count)
        if _row_id(dataset_sha256, position) in development_ids
    }
    test = {
        position for position in range(row_count) if _row_id(dataset_sha256, position) in test_ids
    }
    return development, test


def _classification_frame(*, rows: int = 150, seed: int = 13) -> pd.DataFrame:
    features, target = make_classification(
        n_samples=rows,
        n_features=6,
        n_informative=5,
        n_redundant=0,
        weights=[0.7, 0.3],
        random_state=seed,
    )
    frame = pd.DataFrame(features, columns=[f"feature_{index}" for index in range(6)])
    frame["segment"] = np.where(np.arange(rows) % 3 == 0, "enterprise", "consumer")
    frame.loc[frame.index % 19 == 0, "segment"] = None
    frame["target"] = np.where(target == 1, "yes", "no")
    return frame


def _assert_protocol_compatible(result: object) -> None:
    DataQualityReportPayload.model_validate(result.profile)
    TaskSpecPayload.model_validate(result.task)
    SplitManifestPayload.model_validate(result.split)
    BaselineResultPayload.model_validate(result.baseline)
    for trial in result.trials:
        TrialResultPayload.model_validate(trial)
    EvaluationReportPayload.model_validate(result.evaluation)


def test_public_inspector_returns_only_target_safe_structural_metadata() -> None:
    frame = pd.DataFrame(
        {
            "record_id": np.arange(105),
            "measurement": np.linspace(0.25, 10.75, 105),
            "enabled": np.arange(105) % 2 == 0,
            "account": [f"secret-account-{index}" for index in range(105)],
        }
    )
    frame.loc[0, "measurement"] = np.nan
    raw = _csv_bytes(frame)

    profile = inspect_tabular_dataset(raw, media_type="text/csv")

    assert profile["dataset_sha256"] == hashlib.sha256(raw).hexdigest()
    assert profile["row_count"] == 105
    assert profile["column_count"] == 4
    assert profile["unique_count_cap"] == 100
    columns = {column["name"]: column for column in profile["columns"]}
    assert columns["record_id"]["semantic_type"] == "INTEGER"
    assert columns["measurement"]["semantic_type"] == "FLOAT"
    assert columns["measurement"]["missing_count"] == 1
    assert columns["enabled"]["semantic_type"] == "BOOLEAN"
    assert columns["account"]["semantic_type"] == "STRING"
    assert columns["account"]["unique_count"] == 100
    assert columns["account"]["unique_count_capped"] is True
    serialized = json.dumps(profile, sort_keys=True)
    assert "secret-account-" not in serialized
    assert not ({"values", "examples", "categories"} & set().union(*map(set, columns.values())))


def test_binary_classification_is_deterministic_and_keeps_duplicates_together() -> None:
    original = _classification_frame()
    duplicated_positions = [0, 1, 2, 3]
    frame = pd.concat([original, original.iloc[duplicated_positions]], ignore_index=True)
    raw = _csv_bytes(frame)

    first = run_tabular_automl(
        raw,
        media_type="text/csv",
        target_column="target",
        positive_class="yes",
        iid_confirmed=True,
        seed=101,
    )
    second = run_tabular_automl(
        raw,
        media_type="text/csv",
        target_column="target",
        positive_class="yes",
        iid_confirmed=True,
        seed=101,
    )

    assert first.task["kind"] == "TASK_SPEC"
    assert first.task["task_type"] == "BINARY_CLASSIFICATION"
    assert first.task["primary_metric"] == "average_precision"
    assert first.task["positive_class"] == "yes"
    assert first.task["negative_class"] == "no"
    assert first.task["positive_class_inferred"] is False
    _assert_protocol_compatible(first)
    assert first.profile["exact_duplicate_feature_groups"] == len(duplicated_positions)
    assert first.structured() == second.structured()
    assert first.report_bytes == second.report_bytes
    assert first.model_metadata["sha256"] == second.model_metadata["sha256"]
    assert first.evaluation["holdout_evaluations"] == 1
    assert first.evaluation["production_eligible"] is False
    assert first.model_metadata["disposition"] == "EVALUATED_CANDIDATE"
    assert first.model_metadata["backend_id"] == "sklearn"
    assert first.model_metadata["artifact"] == {
        "kind": "SKLEARN_JOBLIB",
        "media_type": "application/octet-stream",
        "serialization": "joblib",
        "size_bytes": len(first.model_bytes),
        "sha256": first.model_metadata["sha256"],
    }
    assert {trial["family"] for trial in first.trials} == {
        "LOGISTIC_REGRESSION",
        "RANDOM_FOREST_CLASSIFIER",
    }

    development, test = _partition_positions(first.split, len(frame))
    assert development.isdisjoint(test)
    assert development | test == set(range(len(frame)))
    for source_position, duplicate_position in zip(
        duplicated_positions,
        range(len(original), len(frame)),
        strict=True,
    ):
        assert (source_position in development) == (duplicate_position in development)

    model = joblib.load(io.BytesIO(first.model_bytes))
    assert isinstance(model, Pipeline)
    assert set(model.named_steps) == {"preprocessor", "estimator"}
    inference = frame.drop(columns=["target"]).iloc[:3].copy()
    inference.loc[inference.index[0], "segment"] = "previously-unseen-segment"
    assert len(model.predict(inference)) == 3
    assert json.loads(first.report_bytes)["evaluation"]["production_eligible"] is False


def test_regression_parquet_runs_dummy_ridge_and_random_forest() -> None:
    features, target = make_regression(
        n_samples=130,
        n_features=5,
        n_informative=4,
        noise=3.0,
        random_state=29,
    )
    frame = pd.DataFrame(features, columns=[f"x_{index}" for index in range(5)])
    frame["region"] = np.where(np.arange(len(frame)) % 2, "north", "south")
    frame["target"] = target
    buffer = io.BytesIO()
    frame.to_parquet(buffer, index=False)

    result = run_tabular_automl(
        buffer.getvalue(),
        media_type="application/vnd.apache.parquet",
        target_column="target",
        iid_confirmed=True,
        seed=211,
    )

    assert result.task["task_type"] == "REGRESSION"
    assert result.task["primary_metric"] == "rmse"
    assert result.baseline["family"] == "DUMMY_REGRESSOR"
    assert {trial["family"] for trial in result.trials} == {
        "RIDGE",
        "RANDOM_FOREST_REGRESSOR",
    }
    assert all(trial["status"] == "SUCCEEDED" for trial in result.trials)
    assert result.model_metadata["family"] in {"RIDGE", "RANDOM_FOREST_REGRESSOR"}
    assert result.evaluation["holdout_evaluations"] == 1
    candidate_metrics = result.evaluation["all_metrics"]["candidate"]
    baseline_metrics = result.evaluation["all_metrics"]["baseline"]
    assert all(math.isfinite(value) for value in candidate_metrics.values())
    assert candidate_metrics["rmse"] < baseline_metrics["rmse"]
    _assert_protocol_compatible(result)


def test_changing_only_sealed_holdout_does_not_change_cv_or_selected_model() -> None:
    frame = _classification_frame(rows=140, seed=37)
    raw = _csv_bytes(frame)
    first = run_tabular_automl(
        raw,
        media_type="text/csv",
        target_column="target",
        positive_class="yes",
        iid_confirmed=True,
        seed=307,
    )
    development, test = _partition_positions(first.split, len(frame))
    assert development and test

    changed = frame.copy(deep=True)
    changed.loc[sorted(test), "feature_0"] += 10_000
    changed_raw = _csv_bytes(changed)
    second = run_tabular_automl(
        changed_raw,
        media_type="text/csv",
        target_column="target",
        positive_class="yes",
        iid_confirmed=True,
        seed=307,
    )
    second_development, second_test = _partition_positions(second.split, len(changed))

    assert second_development == development
    assert second_test == test
    assert second.baseline["cv_metrics"] == first.baseline["cv_metrics"]
    assert [trial["cv_metrics"] for trial in second.trials] == [
        trial["cv_metrics"] for trial in first.trials
    ]
    assert second.model_metadata["family"] == first.model_metadata["family"]
    assert second.model_metadata["sha256"] == first.model_metadata["sha256"]
    assert (
        second.evaluation["all_metrics"]["candidate"]
        != first.evaluation["all_metrics"]["candidate"]
    )


def test_named_binary_classes_require_an_explicit_positive_class() -> None:
    frame = _classification_frame(rows=60, seed=41)

    with pytest.raises(PositiveClassRequiredError) as error:
        run_tabular_automl(
            _csv_bytes(frame),
            media_type="text/csv",
            target_column="target",
            iid_confirmed=True,
        )

    assert error.value.code == "POSITIVE_CLASS_REQUIRED"


@pytest.mark.parametrize(
    ("labels", "expected_positive"),
    [([False, True], True), ([0, 1], 1)],
)
def test_conventional_binary_classes_infer_positive_class(
    labels: list[object], expected_positive: object
) -> None:
    row_count = 60
    frame = pd.DataFrame(
        {
            "feature": np.linspace(-3, 3, row_count),
            "category": ["left", "right", "center"] * (row_count // 3),
            "target": [labels[index % 2] for index in range(row_count)],
        }
    )

    result = run_tabular_automl(
        _csv_bytes(frame),
        media_type="text/csv",
        target_column="target",
        iid_confirmed=True,
        seed=419,
    )

    assert result.task["positive_class"] == expected_positive
    assert result.task["positive_class_inferred"] is True


def test_task_and_feature_boundaries_raise_typed_errors() -> None:
    base = pd.DataFrame(
        {
            "feature": np.linspace(-2, 2, 30),
            "category": ["a", "b", "c"] * 10,
            "target": [0, 1] * 15,
        }
    )
    raw = _csv_bytes(base)

    with pytest.raises(IIDAssumptionRequiredError):
        run_tabular_automl(raw, media_type="text/csv", target_column="target")
    with pytest.raises(MissingTargetError):
        run_tabular_automl(raw, media_type="text/csv", target_column=None, iid_confirmed=True)
    with pytest.raises(MissingTargetError):
        run_tabular_automl(raw, media_type="text/csv", target_column="missing", iid_confirmed=True)

    all_null = base.copy()
    all_null["target"] = np.nan
    with pytest.raises(AllNullTargetError):
        run_tabular_automl(
            _csv_bytes(all_null),
            media_type="text/csv",
            target_column="target",
            iid_confirmed=True,
        )

    partially_null = base.copy()
    partially_null.loc[0, "target"] = np.nan
    with pytest.raises(TargetContainsMissingError):
        run_tabular_automl(
            _csv_bytes(partially_null),
            media_type="text/csv",
            target_column="target",
            iid_confirmed=True,
        )

    constant = base.copy()
    constant["target"] = 1
    with pytest.raises(ConstantTargetError):
        run_tabular_automl(
            _csv_bytes(constant),
            media_type="text/csv",
            target_column="target",
            iid_confirmed=True,
        )

    multiclass = base.copy()
    multiclass["target"] = ["a", "b", "c"] * 10
    with pytest.raises(MulticlassTargetError):
        run_tabular_automl(
            _csv_bytes(multiclass),
            media_type="text/csv",
            target_column="target",
            iid_confirmed=True,
        )

    with pytest.raises(NoUsableFeaturesError):
        run_tabular_automl(
            _csv_bytes(base[["target"]]),
            media_type="text/csv",
            target_column="target",
            iid_confirmed=True,
        )

    leaked = base.copy()
    leaked["copied_target"] = leaked["target"]
    with pytest.raises(TargetLeakageError):
        run_tabular_automl(
            _csv_bytes(leaked),
            media_type="text/csv",
            target_column="target",
            iid_confirmed=True,
        )


def test_identical_feature_rows_with_conflicting_labels_are_rejected() -> None:
    frame = pd.DataFrame(
        {
            "feature_a": np.arange(30, dtype=float),
            "feature_b": [f"group-{index % 5}" for index in range(30)],
            "target": [0, 1] * 15,
        }
    )
    frame.loc[1, ["feature_a", "feature_b"]] = frame.loc[0, ["feature_a", "feature_b"]].to_numpy()

    with pytest.raises(ConflictingDuplicateLabelsError) as error:
        run_tabular_automl(
            _csv_bytes(frame),
            media_type="text/csv",
            target_column="target",
            iid_confirmed=True,
        )
    assert error.value.code == "CONFLICTING_DUPLICATE_LABELS"
    assert error.value.context["conflicting_group_count"] == 1
