"""Period-over-period comparison.

Turns the long history frame into one row per entity/metric for the period
under review, carrying every baseline the alert engine and the dashboard need:

    current, previous, absolute change, % change,
    trailing average (default 4 periods), historical best / worst,
    consecutive adverse periods and the cumulative move across that run.

Computed once, vectorised, for every entity at once via
:mod:`qmis.analytics.matrix`.
"""

from __future__ import annotations

import warnings

import numpy as np
import pandas as pd

from qmis.analytics.matrix import OffsetMatrix, build_matrix, direction_signs, directional_streaks
from qmis.core.metric_config import LOWER_IS_BETTER, MetricRegistry
from qmis.core.periods import Period

COMPARISON_COLUMNS = [
    "entity_id", "entity_type", "entity_name", "owner_id", "owner_name",
    "metric_key", "period_key", "current", "previous", "delta", "pct_change",
    "trailing_avg", "delta_vs_trailing", "best", "worst", "history_points",
    "consecutive_adverse", "streak_delta", "consecutive_favourable",
    "favourable_streak_delta", "trend", "denominator", "previous_denominator", "streak_start_denominator",
]

TREND_IMPROVING = "improving"
TREND_DETERIORATING = "deteriorating"
TREND_STABLE = "stable"
TREND_UNKNOWN = "unknown"


def build_comparison(
    history: pd.DataFrame,
    registry: MetricRegistry,
    period: Period,
    trailing_window: int = 4,
    stable_band_pct: float = 1.0,
    matrix: OffsetMatrix | None = None,
) -> pd.DataFrame:
    """One row per entity/metric for ``period``, with all baselines attached."""
    if matrix is None:
        known = set(registry.keys)
        filtered = history.loc[history["metric_key"].isin(known)] if not history.empty else history
        matrix = build_matrix(filtered, period)
    if matrix is None:
        return pd.DataFrame(columns=COMPARISON_COLUMNS)

    values = matrix.values
    current = matrix.column(0)
    keep = ~np.isnan(current)
    if not keep.any():
        return pd.DataFrame(columns=COMPARISON_COLUMNS)

    previous = matrix.column(1)
    trailing_block = matrix.window(1, trailing_window)
    # A row with no observations in the window is normal (a BA who reported
    # nothing for a month); numpy warns about the empty slice, which is noise.
    with warnings.catch_warnings(), np.errstate(invalid="ignore"):
        warnings.simplefilter("ignore", RuntimeWarning)
        trailing = (
            np.nanmean(np.where(np.isnan(trailing_block), np.nan, trailing_block), axis=1)
            if trailing_block.size
            else np.full(values.shape[0], np.nan)
        )
        maxima = np.nanmax(values, axis=1)
        minima = np.nanmin(values, axis=1)
    history_points = (~np.isnan(values)).sum(axis=1)

    metric_keys = matrix.metric_keys
    signs = direction_signs(metric_keys, registry)
    streaks, streak_delta, streak_start_den = directional_streaks(matrix, signs, favourable=False)
    good_streaks, good_delta, _ = directional_streaks(matrix, signs, favourable=True)

    delta = current - previous
    with np.errstate(divide="ignore", invalid="ignore"):
        pct_change = np.where(
            (~np.isnan(previous)) & (previous != 0),
            (current - previous) / np.abs(previous) * 100.0,
            np.nan,
        )

    lower_better = np.array(
        [registry[k].direction == LOWER_IS_BETTER if k in registry else False for k in metric_keys]
    )
    best = np.where(lower_better, minima, maxima)
    worst = np.where(lower_better, maxima, minima)

    denominators = matrix.denominators
    denominator = denominators[:, 0] if denominators.shape[1] else np.full(len(current), np.nan)
    previous_denominator = (
        denominators[:, 1] if denominators.shape[1] > 1 else np.full(len(current), np.nan)
    )

    out = pd.DataFrame(
        {
            "entity_id": matrix.entity_ids,
            "metric_key": metric_keys,
            "period_key": period.key,
            "current": current,
            "previous": previous,
            "delta": delta,
            "pct_change": pct_change,
            "trailing_avg": trailing,
            "delta_vs_trailing": current - trailing,
            "best": best,
            "worst": worst,
            "history_points": history_points,
            "consecutive_adverse": streaks,
            "streak_delta": streak_delta,
            "consecutive_favourable": good_streaks,
            "favourable_streak_delta": good_delta,
            "denominator": denominator,
            "previous_denominator": previous_denominator,
            "streak_start_denominator": streak_start_den,
        }
    ).loc[keep]

    identity = matrix.identity
    out = out.join(identity, on="entity_id")
    out["owner_name"] = out["owner_name"].fillna("")
    out["trend"] = _classify_trend(out, signs[keep], stable_band_pct)
    return out[COMPARISON_COLUMNS].reset_index(drop=True)


def _classify_trend(out: pd.DataFrame, signs: np.ndarray, stable_band_pct: float) -> pd.Series:
    signed = out["delta"].to_numpy(dtype=float) * signs
    magnitude = np.abs(out["pct_change"].to_numpy(dtype=float))
    known = ~np.isnan(out["delta"].to_numpy(dtype=float))

    trend = np.full(len(out), TREND_UNKNOWN, dtype=object)
    trend[known] = np.where(signed[known] > 0, TREND_IMPROVING, TREND_DETERIORATING)
    stable = known & ((np.nan_to_num(magnitude, nan=0.0) < stable_band_pct) | (signed == 0))
    trend[stable] = TREND_STABLE
    trend[known & (signs == 0)] = TREND_STABLE
    return pd.Series(trend, index=out.index)
