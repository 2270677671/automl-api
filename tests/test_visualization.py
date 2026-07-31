from __future__ import annotations

import numpy as np
from matplotlib.figure import Figure

from automl_api.visualization import (
    HoldoutEvaluation,
    _draw_metric_comparison,
    render_evaluation_visualizations,
)


def test_metric_comparison_uses_direction_aware_table_instead_of_shared_value_axis() -> None:
    figure = Figure()
    axis = figure.subplots()

    _draw_metric_comparison(
        axis,
        baseline_metrics={"r2": 0.8, "rmse": 1_250.0},
        candidate_metrics={"r2": 0.7, "rmse": 900.0},
    )

    assert axis.axison is False
    assert not axis.containers
    table = list(axis.tables)[0]
    cell_text = {
        (row, column): cell.get_text().get_text()
        for (row, column), cell in table.get_celld().items()
    }
    assert [cell_text[(0, column)] for column in range(5)] == [
        "Metric",
        "Direction",
        "Baseline",
        "Candidate",
        "Outcome",
    ]
    assert [cell_text[(1, column)] for column in range(5)] == [
        "r2",
        "MAXIMIZE",
        "0.8",
        "0.7",
        "WORSE",
    ]
    assert [cell_text[(2, column)] for column in range(5)] == [
        "rmse",
        "MINIMIZE",
        "1250",
        "900",
        "BETTER",
    ]


def test_sparse_regression_hexbins_are_skipped_when_no_cell_has_two_samples() -> None:
    visualizations = render_evaluation_visualizations(
        task_type="REGRESSION",
        baseline_metrics={"rmse": 2.0, "mae": 1.5, "r2": 0.1},
        candidate=HoldoutEvaluation(
            metrics={"rmse": 1.0, "mae": 0.8, "r2": 0.5},
            target=np.asarray([0.0, 100.0, 250.0]),
            predictions=np.asarray([10.0, 90.0, 200.0]),
        ),
    )

    by_type = {item.chart_type: item for item in visualizations}
    for chart_type in ("OBSERVED_VS_PREDICTED", "RESIDUALS_VS_PREDICTED"):
        item = by_type[chart_type]
        assert item.status == "SKIPPED"
        assert item.content is None
        assert item.failure_code == "INSUFFICIENT_AGGREGATED_HEXBIN_COUNTS"
        assert item.metadata()["aggregate_only"] is True
        assert item.metadata()["contains_raw_rows"] is False

    assert by_type["METRIC_COMPARISON"].status == "GENERATED"
    assert by_type["RESIDUAL_DISTRIBUTION"].status == "GENERATED"


def test_regression_hexbins_are_generated_when_aggregate_threshold_is_met() -> None:
    visualizations = render_evaluation_visualizations(
        task_type="REGRESSION",
        baseline_metrics={"rmse": 2.0, "mae": 1.5, "r2": 0.1},
        candidate=HoldoutEvaluation(
            metrics={"rmse": 1.0, "mae": 0.8, "r2": 0.5},
            target=np.asarray([1.0, 1.0, 3.0, 3.0]),
            predictions=np.asarray([1.5, 1.5, 2.5, 2.5]),
        ),
    )

    by_type = {item.chart_type: item for item in visualizations}
    for chart_type in ("OBSERVED_VS_PREDICTED", "RESIDUALS_VS_PREDICTED"):
        item = by_type[chart_type]
        assert item.status == "GENERATED"
        assert item.content is not None
        assert item.failure_code is None
