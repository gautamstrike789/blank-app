"""Explainable anomaly detection.

Thresholds catch "this number is bad".  Anomaly detection catches "this number
is not bad *yet*, but it has never behaved like this before" - the RJBD1 that
sits at 2.0-2.5% for two months and prints 4.0% this week without ever touching
its critical line.

Deliberately statistical, not machine-learned.  Two estimators, both of which a
manager can be talked through in one sentence:

``robust``  median and median absolute deviation over the trailing window.  A
            single past spike does not inflate the band and hide the next one,
            which is exactly the failure mode a mean/stdev z-score has on
            weekly operational data.
``zscore``  classical mean and standard deviation, for metrics that really are
            symmetric and well behaved.

Both report the same fields, so the alert text never branches on method.
Scoring is vectorised one metric at a time (42 numpy passes, not 27,000
Python loops).
"""

from __future__ import annotations

import warnings

import numpy as np
import pandas as pd

from qmis.analytics.matrix import OffsetMatrix, build_matrix, direction_signs
from qmis.core.metric_config import MetricRegistry
from qmis.core.periods import Period

# 1/0.6745: scales the median absolute deviation onto the same footing as a
# standard deviation for normally distributed data, so one z_threshold setting
# means the same thing under either method.
MAD_TO_SIGMA = 1.4826

ANOMALY_COLUMNS = [
    "entity_id", "metric_key", "baseline", "dispersion", "z_score",
    "relative_deviation", "is_anomaly", "adverse", "window_points", "method",
]


def detect_anomalies(
    history: pd.DataFrame,
    registry: MetricRegistry,
    period: Period,
    matrix: OffsetMatrix | None = None,
) -> pd.DataFrame:
    """Score every entity/metric in ``period`` against its own recent history."""
    if matrix is None:
        known = set(registry.keys)
        filtered = history.loc[history["metric_key"].isin(known)] if not history.empty else history
        matrix = build_matrix(filtered, period)
    if matrix is None:
        return pd.DataFrame(columns=ANOMALY_COLUMNS)

    metric_keys = matrix.metric_keys
    signs = direction_signs(metric_keys, registry)
    current_all = matrix.column(0)

    blocks: list[pd.DataFrame] = []
    for key in pd.unique(metric_keys):
        metric = registry.get(str(key))
        if metric is None or not metric.anomaly.enabled:
            continue
        rows = np.flatnonzero(metric_keys == key)
        window = matrix.window(1, metric.anomaly.lookback)[rows]
        if window.size == 0:
            continue
        current = current_all[rows]
        counts = (~np.isnan(window)).sum(axis=1)
        valid = (counts >= metric.anomaly.min_history) & (~np.isnan(current))
        if not valid.any():
            continue

        baseline, dispersion = _baseline(window, metric.anomaly.method)
        with np.errstate(divide="ignore", invalid="ignore"):
            z_score = np.where(dispersion > 0, (current - baseline) / dispersion, np.nan)
            relative = np.where(
                baseline != 0, (current - baseline) / np.abs(baseline) * 100.0, np.nan
            )

        triggered = np.zeros(len(rows), dtype=bool)
        if metric.anomaly.z_threshold is not None:
            triggered |= np.nan_to_num(np.abs(z_score), nan=0.0) >= metric.anomaly.z_threshold
        if metric.anomaly.relative_threshold is not None:
            triggered |= (
                np.nan_to_num(np.abs(relative), nan=0.0) >= metric.anomaly.relative_threshold
            )
        deviation = current - baseline
        adverse = np.nan_to_num(deviation * signs[rows], nan=0.0) < 0

        blocks.append(
            pd.DataFrame(
                {
                    "entity_id": matrix.entity_ids[rows][valid],
                    "metric_key": key,
                    "baseline": baseline[valid],
                    "dispersion": dispersion[valid],
                    "z_score": z_score[valid],
                    "relative_deviation": relative[valid],
                    "is_anomaly": (triggered & valid)[valid],
                    "adverse": adverse[valid],
                    "window_points": counts[valid],
                    "method": metric.anomaly.method,
                }
            )
        )
    if not blocks:
        return pd.DataFrame(columns=ANOMALY_COLUMNS)
    return pd.concat(blocks, ignore_index=True)[ANOMALY_COLUMNS]


def _baseline(window: np.ndarray, method: str) -> tuple[np.ndarray, np.ndarray]:
    """Central tendency and dispersion for each row of ``window``."""
    # Rows with an entirely empty window are expected and are filtered out by
    # the caller's `valid` mask; numpy's empty-slice warning is just noise.
    with warnings.catch_warnings(), np.errstate(invalid="ignore"):
        warnings.simplefilter("ignore", RuntimeWarning)
        if method == "zscore":
            baseline = np.nanmean(window, axis=1)
            dispersion = _nanstd(window)
        else:
            baseline = np.nanmedian(window, axis=1)
            mad = np.nanmedian(np.abs(window - baseline[:, None]), axis=1)
            dispersion = mad * MAD_TO_SIGMA
            # A flat window (or one where over half the values are identical)
            # gives a zero MAD.  Falling back to the standard deviation makes
            # the check degrade gracefully instead of dividing by zero and
            # declaring every subsequent value an anomaly.
            fallback = _nanstd(window)
            dispersion = np.where(
                (np.nan_to_num(dispersion, nan=0.0) <= 0), fallback, dispersion
            )
    return baseline, np.nan_to_num(dispersion, nan=0.0)


def _nanstd(window: np.ndarray) -> np.ndarray:
    """Row-wise sample standard deviation, 0 where there is too little data.

    Rows with fewer than two observations are excluded before the call rather
    than after, because numpy warns (loudly, once per slice) on ddof=1 with a
    single point.
    """
    counts = (~np.isnan(window)).sum(axis=1)
    out = np.zeros(window.shape[0], dtype=float)
    usable = counts > 1
    if usable.any():
        with np.errstate(invalid="ignore", divide="ignore"):
            std = np.nanstd(window[usable], axis=1, ddof=1)
        out[usable] = np.nan_to_num(std, nan=0.0)
    return out
