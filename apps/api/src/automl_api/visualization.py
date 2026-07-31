"""Privacy-conscious evaluation visualizations for all tabular backends."""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass
from typing import Any, Callable

import numpy as np
from sklearn.calibration import calibration_curve
from sklearn.metrics import confusion_matrix, precision_recall_curve, roc_curve


_MINIMIZED_METRICS = frozenset({"log_loss", "mae", "rmse"})
_MAXIMIZED_METRICS = frozenset({"accuracy", "average_precision", "r2", "roc_auc"})
_EMPTY_HEXBIN_FAILURE_CODE = "INSUFFICIENT_AGGREGATED_HEXBIN_COUNTS"


class _EmptyAggregatePlotError(Exception):
    """The privacy threshold removed every cell from an aggregate plot."""


@dataclass(frozen=True)
class HoldoutEvaluation:
    """Short-lived sealed-holdout values; never serialize this object."""

    metrics: dict[str, float]
    target: np.ndarray
    predictions: np.ndarray
    positive_scores: np.ndarray | None = None


@dataclass(frozen=True)
class EvaluationVisualization:
    chart_type: str
    status: str
    content: bytes | None
    sample_count: int
    failure_code: str | None = None
    failure_message: str | None = None

    @property
    def artifact_kind(self) -> str:
        return f"EVALUATION_{self.chart_type}_PNG"

    def metadata(self, *, artifact_id: str | None = None) -> dict[str, Any]:
        return {
            "chart_type": self.chart_type,
            "status": self.status,
            "artifact_id": artifact_id,
            "media_type": "image/png" if self.content is not None else None,
            "source_partition": "SEALED_HOLDOUT",
            "sample_count": self.sample_count,
            "aggregate_only": True,
            "contains_raw_rows": False,
            "size_bytes": len(self.content) if self.content is not None else None,
            "sha256": (
                hashlib.sha256(self.content).hexdigest() if self.content is not None else None
            ),
            "failure_code": self.failure_code,
        }


def visualization_status(items: list[dict[str, Any]]) -> str:
    generated = sum(item.get("status") == "GENERATED" for item in items)
    if generated == len(items) and items:
        return "COMPLETE"
    if generated:
        return "PARTIAL"
    return "UNAVAILABLE"


def render_evaluation_visualizations(
    *,
    task_type: str,
    baseline_metrics: dict[str, float],
    candidate: HoldoutEvaluation,
) -> tuple[EvaluationVisualization, ...]:
    """Render deterministic aggregate PNGs without labels or row-level values."""

    sample_count = int(candidate.target.size)
    charts: list[tuple[str, Callable[[Any], None]]] = [
        (
            "METRIC_COMPARISON",
            lambda axis: _draw_metric_comparison(
                axis, baseline_metrics=baseline_metrics, candidate_metrics=candidate.metrics
            ),
        )
    ]
    skipped: list[EvaluationVisualization] = []
    if task_type == "BINARY_CLASSIFICATION":
        scores = candidate.positive_scores
        if scores is None:
            skipped.extend(
                _skipped(name, sample_count, "PROBABILITY_SCORES_UNAVAILABLE")
                for name in ("ROC_CURVE", "PRECISION_RECALL_CURVE", "CALIBRATION_CURVE")
            )
        else:
            charts.extend(
                [
                    ("ROC_CURVE", lambda axis: _draw_roc(axis, candidate.target, scores)),
                    (
                        "PRECISION_RECALL_CURVE",
                        lambda axis: _draw_precision_recall(axis, candidate.target, scores),
                    ),
                ]
            )
            if sample_count >= 20 and np.unique(scores).size >= 3:
                charts.append(
                    (
                        "CALIBRATION_CURVE",
                        lambda axis: _draw_calibration(axis, candidate.target, scores),
                    )
                )
            else:
                skipped.append(
                    _skipped(
                        "CALIBRATION_CURVE",
                        sample_count,
                        (
                            "INSUFFICIENT_HOLDOUT_SAMPLES"
                            if sample_count < 20
                            else "INSUFFICIENT_UNIQUE_PROBABILITIES"
                        ),
                    )
                )
        charts.append(
            (
                "CONFUSION_MATRIX",
                lambda axis: _draw_confusion_matrix(axis, candidate.target, candidate.predictions),
            )
        )
    else:
        residuals = candidate.target.astype("float64") - candidate.predictions.astype("float64")
        charts.extend(
            [
                (
                    "OBSERVED_VS_PREDICTED",
                    lambda axis: _draw_observed_vs_predicted(
                        axis, candidate.target, candidate.predictions
                    ),
                ),
                (
                    "RESIDUALS_VS_PREDICTED",
                    lambda axis: _draw_residuals(axis, candidate.predictions, residuals),
                ),
                (
                    "RESIDUAL_DISTRIBUTION",
                    lambda axis: _draw_residual_distribution(axis, residuals),
                ),
            ]
        )

    rendered = [_render(chart_type, sample_count, draw) for chart_type, draw in charts]
    return tuple(rendered + skipped)


def _render(
    chart_type: str,
    sample_count: int,
    draw: Callable[[Any], None],
) -> EvaluationVisualization:
    try:
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from matplotlib.figure import Figure
    except ImportError as error:
        return EvaluationVisualization(
            chart_type=chart_type,
            status="FAILED",
            content=None,
            sample_count=sample_count,
            failure_code="PLOT_RENDERER_UNAVAILABLE",
            failure_message=type(error).__name__,
        )
    try:
        figure = Figure(figsize=(6.4, 4.2), dpi=120, layout="constrained")
        canvas = FigureCanvasAgg(figure)
        axis = figure.subplots()
        draw(axis)
        axis.grid(color="#d9dde3", linewidth=0.6, alpha=0.7)
        buffer = io.BytesIO()
        canvas.print_png(buffer, metadata={"Software": "Managed AutoML API"})
        return EvaluationVisualization(
            chart_type=chart_type,
            status="GENERATED",
            content=buffer.getvalue(),
            sample_count=sample_count,
        )
    except _EmptyAggregatePlotError as error:
        return EvaluationVisualization(
            chart_type=chart_type,
            status="SKIPPED",
            content=None,
            sample_count=sample_count,
            failure_code=_EMPTY_HEXBIN_FAILURE_CODE,
            failure_message=str(error),
        )
    except Exception as error:
        return EvaluationVisualization(
            chart_type=chart_type,
            status="FAILED",
            content=None,
            sample_count=sample_count,
            failure_code="PLOT_RENDER_FAILED",
            failure_message=type(error).__name__,
        )


def _skipped(chart_type: str, sample_count: int, code: str) -> EvaluationVisualization:
    return EvaluationVisualization(
        chart_type=chart_type,
        status="SKIPPED",
        content=None,
        sample_count=sample_count,
        failure_code=code,
    )


def _draw_metric_comparison(
    axis: Any,
    *,
    baseline_metrics: dict[str, float],
    candidate_metrics: dict[str, float],
) -> None:
    names = sorted(set(baseline_metrics) & set(candidate_metrics))
    rows = []
    for name in names:
        direction = _metric_direction(name)
        baseline = float(baseline_metrics[name])
        candidate = float(candidate_metrics[name])
        rows.append(
            [
                name,
                direction,
                _format_metric_value(baseline),
                _format_metric_value(candidate),
                _candidate_outcome(
                    baseline=baseline,
                    candidate=candidate,
                    direction=direction,
                ),
            ]
        )

    axis.set_title("Sealed holdout metric comparison", pad=12)
    axis.set_axis_off()
    if not rows:
        axis.text(
            0.5,
            0.5,
            "No comparable metrics",
            ha="center",
            va="center",
            transform=axis.transAxes,
        )
        return

    table = axis.table(
        cellText=rows,
        colLabels=["Metric", "Direction", "Baseline", "Candidate", "Outcome"],
        cellLoc="center",
        colLoc="center",
        loc="center",
        colWidths=[0.24, 0.18, 0.2, 0.2, 0.18],
    )
    table.auto_set_font_size(False)
    table.set_fontsize(9)
    table.scale(1, 1.45)
    for (row, _column), cell in table.get_celld().items():
        if row == 0:
            cell.set_facecolor("#e9ecef")
            cell.set_text_props(weight="bold", color="#1f2937")
        else:
            cell.set_facecolor("#f8fafc" if row % 2 else "#ffffff")
            cell.set_edgecolor("#d9dde3")


def _metric_direction(name: str) -> str:
    normalized = name.lower().strip()
    if normalized in _MINIMIZED_METRICS:
        return "MINIMIZE"
    if normalized in _MAXIMIZED_METRICS:
        return "MAXIMIZE"
    return "UNSPECIFIED"


def _format_metric_value(value: float) -> str:
    return f"{value:.6g}"


def _candidate_outcome(*, baseline: float, candidate: float, direction: str) -> str:
    if direction == "UNSPECIFIED":
        return "NOT EVALUATED"
    if np.isclose(candidate, baseline, rtol=1e-9, atol=1e-12):
        return "TIED"
    candidate_is_better = candidate > baseline if direction == "MAXIMIZE" else candidate < baseline
    return "BETTER" if candidate_is_better else "WORSE"


def _draw_confusion_matrix(axis: Any, target: np.ndarray, predictions: np.ndarray) -> None:
    matrix = confusion_matrix(target, predictions, labels=[0, 1])
    image = axis.imshow(matrix, cmap="Blues")
    for row in range(2):
        for column in range(2):
            axis.text(column, row, str(int(matrix[row, column])), ha="center", va="center")
    axis.set_title("Confusion matrix")
    axis.set_xlabel("Predicted class")
    axis.set_ylabel("Observed class")
    axis.set_xticks([0, 1], ["Negative", "Positive"])
    axis.set_yticks([0, 1], ["Negative", "Positive"])
    axis.figure.colorbar(image, ax=axis, label="Count")


def _draw_roc(axis: Any, target: np.ndarray, scores: np.ndarray) -> None:
    false_positive, true_positive, _ = roc_curve(target, scores)
    axis.plot(false_positive, true_positive, color="#087f5b", label="Candidate")
    axis.plot([0, 1], [0, 1], color="#6b7280", linestyle="--", label="Random")
    axis.set_title("ROC curve")
    axis.set_xlabel("False positive rate")
    axis.set_ylabel("True positive rate")
    axis.set_xlim(0, 1)
    axis.set_ylim(0, 1)
    axis.legend()


def _draw_precision_recall(axis: Any, target: np.ndarray, scores: np.ndarray) -> None:
    precision, recall, _ = precision_recall_curve(target, scores)
    prevalence = float(np.mean(target.astype("float64")))
    axis.plot(recall, precision, color="#087f5b", label="Candidate")
    axis.axhline(prevalence, color="#6b7280", linestyle="--", label="Prevalence")
    axis.set_title("Precision-recall curve")
    axis.set_xlabel("Recall")
    axis.set_ylabel("Precision")
    axis.set_xlim(0, 1)
    axis.set_ylim(0, 1)
    axis.legend()


def _draw_calibration(axis: Any, target: np.ndarray, scores: np.ndarray) -> None:
    observed, predicted = calibration_curve(target, scores, n_bins=10, strategy="quantile")
    axis.plot(predicted, observed, marker="o", color="#087f5b", label="Candidate")
    axis.plot([0, 1], [0, 1], color="#6b7280", linestyle="--", label="Ideal")
    axis.set_title("Calibration curve")
    axis.set_xlabel("Mean predicted probability")
    axis.set_ylabel("Observed positive rate")
    axis.set_xlim(0, 1)
    axis.set_ylim(0, 1)
    axis.legend()


def _draw_observed_vs_predicted(axis: Any, target: np.ndarray, predictions: np.ndarray) -> None:
    low = float(min(np.min(target), np.min(predictions)))
    high = float(max(np.max(target), np.max(predictions)))
    image = axis.hexbin(predictions, target, gridsize=28, mincnt=2, cmap="viridis")
    _require_aggregated_hexbin_cell(image)
    axis.plot([low, high], [low, high], color="#b42318", linestyle="--", label="Ideal")
    axis.set_title("Observed vs predicted")
    axis.set_xlabel("Predicted value")
    axis.set_ylabel("Observed value")
    axis.figure.colorbar(image, ax=axis, label="Aggregated count")
    axis.legend()


def _draw_residuals(axis: Any, predictions: np.ndarray, residuals: np.ndarray) -> None:
    image = axis.hexbin(predictions, residuals, gridsize=28, mincnt=2, cmap="viridis")
    _require_aggregated_hexbin_cell(image)
    axis.axhline(0, color="#b42318", linestyle="--")
    axis.set_title("Residuals vs predicted")
    axis.set_xlabel("Predicted value")
    axis.set_ylabel("Residual")
    axis.figure.colorbar(image, ax=axis, label="Aggregated count")


def _require_aggregated_hexbin_cell(image: Any) -> None:
    counts = np.asarray(image.get_array())
    if counts.size == 0:
        raise _EmptyAggregatePlotError(
            "No hexbin cell met the minimum aggregate count of two samples."
        )


def _draw_residual_distribution(axis: Any, residuals: np.ndarray) -> None:
    axis.hist(residuals, bins="auto", color="#087f5b", edgecolor="white")
    axis.axvline(0, color="#b42318", linestyle="--", label="Zero")
    axis.axvline(float(np.median(residuals)), color="#6b7280", linestyle=":", label="Median")
    axis.set_title("Residual distribution")
    axis.set_xlabel("Residual")
    axis.set_ylabel("Aggregated count")
    axis.legend()


__all__ = [
    "EvaluationVisualization",
    "HoldoutEvaluation",
    "render_evaluation_visualizations",
    "visualization_status",
]
