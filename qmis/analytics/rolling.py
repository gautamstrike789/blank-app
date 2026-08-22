"""Rolling-window aggregation.

The organisation signs roughly 2,300 submissions a week across ~650 BAs -
about **three or four submissions per BA per week**.  A Debit 1 rate computed
on three submissions can only be 0%, 33%, 67% or 100%; week-on-week it will
swing by 33 points for reasons that have nothing to do with quality.  Alerting
on that would bury every real signal under arithmetic noise.

So BA-level evaluation runs on a trailing window instead: the last N weeks of
*counts* are summed, and the rates are recomputed from those sums.  Four weeks
of a typical BA is ~14 submissions - still small, but no longer nonsense - and
the window slides every week, so the report stays weekly.

This is not smoothing.  Nothing is averaged: ``sum(debit1) / sum(submissions)``
over four weeks is the BA's actual Debit 1 rate over those four weeks.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from qmis.core.metric_config import MetricRegistry
from qmis.core.periods import Period


def apply_rolling(
    history: pd.DataFrame,
    registry: MetricRegistry,
    window: int,
    entity_types: tuple[str, ...] | None = None,
) -> pd.DataFrame:
    """Return history with rates recomputed over a trailing ``window``.

    Counts become window sums; rates with a configured numerator/denominator
    are recomputed from those sums; everything else (currency, unclassified
    numbers) becomes a denominator-weighted mean where a denominator exists and
    a plain mean where one does not.
    """
    if history.empty or window <= 1:
        return history

    target = history
    passthrough = history.iloc[0:0]
    if entity_types is not None:
        mask = history["entity_type"].isin(entity_types)
        target, passthrough = history.loc[mask], history.loc[~mask]
    if target.empty:
        return history

    frame = target.copy()
    frame["_ordinal"] = _ordinals(frame["period_key"])
    frame = frame.loc[frame["_ordinal"].notna()]
    if frame.empty:
        return history
    frame["_ordinal"] = frame["_ordinal"].astype(int)

    counts_keys = {m.key for m in registry if m.unit == "count"}
    count_rows = frame.loc[frame["metric_key"].isin(counts_keys)]
    other_rows = frame.loc[~frame["metric_key"].isin(counts_keys)]

    rolled_counts = _roll(count_rows, window, how="sum")
    lookup = _lookup(rolled_counts)

    rolled_rates = _recompute_rates(
        other_rows, registry, lookup, window, counts_frame=rolled_counts
    )
    combined = pd.concat([rolled_counts, rolled_rates], ignore_index=True)
    if combined.empty:
        return history
    return pd.concat([passthrough, combined], ignore_index=True)


def _ordinals(period_keys: pd.Series) -> pd.Series:
    """Map period keys onto a dense integer timeline for window arithmetic."""
    unique = pd.unique(period_keys.astype(str))
    parsed: dict[str, Period] = {}
    for key in unique:
        try:
            parsed[key] = Period.from_key(key)
        except Exception:
            continue
    if not parsed:
        return pd.Series([None] * len(period_keys), index=period_keys.index)
    anchor = min(parsed.values())
    # period.distance(anchor) counts steps forward from the earliest period, so
    # the timeline is dense and increasing regardless of year boundaries.
    mapping = {key: period.distance(anchor) for key, period in parsed.items()}
    return period_keys.astype(str).map(mapping)


def _roll(frame: pd.DataFrame, window: int, how: str) -> pd.DataFrame:
    """Trailing-window aggregate per (entity, metric), on a dense timeline."""
    if frame.empty:
        return frame.drop(columns=["_ordinal"], errors="ignore")

    identity = (
        frame.drop_duplicates(subset=["entity_id"], keep="last")
        .set_index("entity_id")[["entity_type", "entity_name", "owner_id", "owner_name", "grain"]]
    )
    key_by_ordinal = (
        frame.drop_duplicates(subset=["_ordinal"])[["_ordinal", "period_key"]]
        .set_index("_ordinal")["period_key"]
        .to_dict()
    )

    wide = frame.pivot_table(
        index=["entity_id", "metric_key"], columns="_ordinal", values="value", aggfunc="sum"
    )
    full = list(range(int(frame["_ordinal"].min()), int(frame["_ordinal"].max()) + 1))
    wide = wide.reindex(columns=full)
    # A period a BA did not report is a genuine zero for a count sum, but the
    # window must still only span periods that exist in the data at all.
    rolled = wide.T.rolling(window=window, min_periods=1).sum().T
    present = (wide.notna().T.rolling(window=window, min_periods=1).sum().T) > 0
    rolled = rolled.where(present)

    long = rolled.stack().rename("value").reset_index()
    long["period_key"] = long["_ordinal"].map(key_by_ordinal)
    long = long.loc[long["period_key"].notna()]
    long = long.join(identity, on="entity_id")
    long["denominator"] = np.nan
    return long[
        ["entity_id", "entity_type", "entity_name", "owner_id", "owner_name",
         "period_key", "grain", "metric_key", "value", "denominator"]
    ]


def _lookup(counts: pd.DataFrame) -> dict:
    if counts.empty:
        return {}
    return {
        (row.entity_id, row.period_key, row.metric_key): row.value
        for row in counts.itertuples(index=False)
    }


def _recompute_rates(
    frame: pd.DataFrame,
    registry: MetricRegistry,
    lookup: dict,
    window: int,
    counts_frame: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Rebuild each rate from its rolled-up components."""
    if frame.empty and (counts_frame is None or counts_frame.empty):
        return frame.drop(columns=["_ordinal"], errors="ignore")
    if counts_frame is None:
        counts_frame = frame.iloc[0:0]

    rebuilt: list[pd.DataFrame] = []
    fallback_keys: list[str] = []
    for key in pd.unique(frame["metric_key"]):
        metric = registry.get(str(key))
        if metric is None:
            continue
        if not (metric.unit == "percent" and metric.numerator and metric.denominator):
            fallback_keys.append(str(key))
            continue
        # Build the rate from the ROLLED COUNT coordinates, not from the periods
        # where a rate happened to be reported.  A BA who missed one week still
        # has four weeks of counts in the window, so the window rate exists for
        # that week even though no rate row does; deriving from the rate rows
        # punched holes in exactly the series the trend rules read.
        block = counts_frame.loc[counts_frame["metric_key"] == metric.denominator].copy()
        if block.empty:
            continue
        num = np.array(
            [
                lookup.get((row.entity_id, row.period_key, metric.numerator), np.nan)
                for row in block.itertuples(index=False)
            ],
            dtype=float,
        )
        den = block["value"].to_numpy(dtype=float)
        with np.errstate(divide="ignore", invalid="ignore"):
            block["value"] = np.where(den > 0, num / den * 100.0, np.nan)
        block["denominator"] = den
        block["metric_key"] = metric.key
        rebuilt.append(block.loc[block["value"].notna()])

    if fallback_keys:
        # Currency and unclassified numeric metrics have no components to
        # rebuild from, so they take a submission-weighted window mean.
        others = frame.loc[frame["metric_key"].isin(fallback_keys)]
        rebuilt.append(_weighted_window_mean(others, window, lookup))

    if not rebuilt:
        return frame.iloc[0:0].drop(columns=["_ordinal"], errors="ignore")
    out = pd.concat(rebuilt, ignore_index=True)
    return out[
        ["entity_id", "entity_type", "entity_name", "owner_id", "owner_name",
         "period_key", "grain", "metric_key", "value", "denominator"]
    ]


def _weighted_window_mean(frame: pd.DataFrame, window: int, lookup: dict) -> pd.DataFrame:
    if frame.empty:
        return frame.drop(columns=["_ordinal"], errors="ignore")
    weights = np.array(
        [
            lookup.get((row.entity_id, row.period_key, "submissions")) or np.nan
            for row in frame.itertuples(index=False)
        ],
        dtype=float,
    )
    block = frame.copy()
    identity = ["entity_id", "metric_key"]
    wide = block.pivot_table(index=identity, columns="_ordinal", values="value", aggfunc="mean")
    full = list(range(int(block["_ordinal"].min()), int(block["_ordinal"].max()) + 1))
    wide = wide.reindex(columns=full)
    rolled = wide.T.rolling(window=window, min_periods=1).mean().T
    key_by_ordinal = (
        block.drop_duplicates(subset=["_ordinal"])[["_ordinal", "period_key"]]
        .set_index("_ordinal")["period_key"]
        .to_dict()
    )
    ident = (
        block.drop_duplicates(subset=["entity_id"], keep="last")
        .set_index("entity_id")[["entity_type", "entity_name", "owner_id", "owner_name", "grain"]]
    )
    long = rolled.stack().rename("value").reset_index()
    long["period_key"] = long["_ordinal"].map(key_by_ordinal)
    long = long.loc[long["period_key"].notna()].join(ident, on="entity_id")
    long["denominator"] = [
        lookup.get((row.entity_id, row.period_key, "submissions")) for row in long.itertuples(index=False)
    ]
    return long
