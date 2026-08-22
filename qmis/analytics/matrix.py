"""Shared wide-matrix view of history.

Comparison, streak detection and anomaly scoring all need the same thing: for
every (entity, metric), the value at period offset 0 (the period under review),
1 (the one before), 2, and so on.

Building that once as a dense numpy matrix - rather than grouping 27,000 times
in Python - is the difference between a dashboard that answers in seconds and
one nobody opens twice.  650 BAs x 42 metrics x 12 periods is a 27k x 12
float array: about 2.6 MB.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from qmis.core.periods import Period


@dataclass
class OffsetMatrix:
    """Values laid out as (entity, metric) rows x period-offset columns."""

    index: pd.MultiIndex          # (entity_id, metric_key)
    values: np.ndarray            # shape (n_rows, n_offsets); NaN where absent
    denominators: np.ndarray      # same shape
    identity: pd.DataFrame        # entity_id -> entity_type/name/owner
    max_offset: int

    @property
    def entity_ids(self) -> np.ndarray:
        return self.index.get_level_values(0).to_numpy()

    @property
    def metric_keys(self) -> np.ndarray:
        return self.index.get_level_values(1).to_numpy()

    def column(self, offset: int) -> np.ndarray:
        if offset > self.max_offset:
            return np.full(self.values.shape[0], np.nan)
        return self.values[:, offset]

    def window(self, start: int, end: int) -> np.ndarray:
        """Columns ``start..end`` inclusive, clipped to what exists."""
        end = min(end, self.max_offset)
        if start > end:
            return np.empty((self.values.shape[0], 0))
        return self.values[:, start : end + 1]


def build_matrix(history: pd.DataFrame, period: Period, max_offset: int = 60) -> OffsetMatrix | None:
    """Pivot a long history frame into an :class:`OffsetMatrix` for ``period``.

    Rows dated *after* the period under review are dropped: re-evaluating an
    older week must not let a later week leak into its baselines.
    """
    if history.empty:
        return None
    frame = history.copy()

    # Map each distinct period key once, not once per row.
    unique_keys = pd.unique(frame["period_key"].astype(str))
    offsets: dict[str, int] = {}
    for key in unique_keys:
        try:
            other = Period.from_key(key)
        except Exception:
            continue
        if other.grain != period.grain:
            continue
        distance = period.distance(other)
        if 0 <= distance <= max_offset:
            offsets[key] = distance
    if not offsets:
        return None

    frame["_offset"] = frame["period_key"].astype(str).map(offsets)
    frame = frame.loc[frame["_offset"].notna()]
    if frame.empty:
        return None
    frame["_offset"] = frame["_offset"].astype(int)

    frame = frame.drop_duplicates(subset=["entity_id", "metric_key", "_offset"], keep="first")

    values = frame.pivot(index=["entity_id", "metric_key"], columns="_offset", values="value")
    denominators = frame.pivot(
        index=["entity_id", "metric_key"], columns="_offset", values="denominator"
    ).reindex(index=values.index)

    width = int(frame["_offset"].max()) + 1
    full = list(range(width))
    values = values.reindex(columns=full)
    denominators = denominators.reindex(columns=full)

    identity = (
        frame.sort_values("_offset")
        .drop_duplicates(subset=["entity_id"], keep="first")
        .set_index("entity_id")[["entity_type", "entity_name", "owner_id", "owner_name"]]
    )
    return OffsetMatrix(
        index=values.index,
        values=values.to_numpy(dtype=float),
        denominators=denominators.to_numpy(dtype=float),
        identity=identity,
        max_offset=width - 1,
    )


def direction_signs(metric_keys: np.ndarray, registry) -> np.ndarray:
    """+1 where higher is better, -1 where lower is better, 0 otherwise.

    Multiplying a delta by this turns "did it move the wrong way?" into a single
    sign test that works for every metric at once.
    """
    from qmis.core.metric_config import HIGHER_IS_BETTER, LOWER_IS_BETTER

    lookup = {}
    for key in np.unique(metric_keys):
        metric = registry.get(str(key))
        if metric is None:
            lookup[key] = 0.0
        elif metric.direction == HIGHER_IS_BETTER:
            lookup[key] = 1.0
        elif metric.direction == LOWER_IS_BETTER:
            lookup[key] = -1.0
        else:
            lookup[key] = 0.0
    return np.array([lookup[k] for k in metric_keys], dtype=float)


def directional_streaks(
    matrix: OffsetMatrix, signs: np.ndarray, favourable: bool = False
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Consecutive same-direction steps ending at offset 0, the total move, and
    the denominator at the start of the run.

    A missing period ends the streak rather than being bridged: "declining for
    four weeks" has to mean four weeks that were actually observed.  Set
    ``favourable`` to count improvement runs instead - "show me every BA that
    has improved four weeks running" needs the same machinery pointed the other
    way.
    """
    values = matrix.values
    n_rows, n_cols = values.shape
    if n_cols < 2:
        return np.zeros(n_rows, dtype=int), np.full(n_rows, np.nan), np.full(n_rows, np.nan)

    newer, older = values[:, :-1], values[:, 1:]
    steps = (newer - older) * signs[:, None]
    wanted = steps > 0 if favourable else steps < 0
    adverse = np.where(np.isnan(steps), 0, wanted.astype(int))
    # cumprod turns "leading run of 1s" into a run that stops at the first 0.
    streaks = np.cumprod(adverse, axis=1).sum(axis=1).astype(int)

    start = np.clip(streaks, 0, n_cols - 1)
    start_values = np.take_along_axis(values, start[:, None], axis=1).ravel()
    cumulative = np.where(streaks > 0, values[:, 0] - start_values, np.nan)
    if matrix.denominators.shape[1] >= n_cols:
        start_den = np.take_along_axis(matrix.denominators, start[:, None], axis=1).ravel()
    else:
        start_den = np.full(n_rows, np.nan)
    start_den = np.where(streaks > 0, start_den, np.nan)
    return streaks, cumulative, start_den


def adverse_streaks(matrix: OffsetMatrix, signs: np.ndarray):
    """Backwards-compatible alias for the deterioration direction."""
    return directional_streaks(matrix, signs, favourable=False)
