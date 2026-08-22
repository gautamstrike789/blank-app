"""Weighted quality score.

A single 0-100 number per entity per period, built so that it can be argued
with rather than merely believed:

* Each scored metric is mapped to 0-100 by its own configured anchors
  (critical = 50, warning = 75, target = 100). "62" always means the same
  distance from the thresholds, whatever the metric's natural units.
* Metrics are combined by configured weight, **renormalised over the metrics
  that actually have data**. A missing Debit 3 must not quietly drag a score
  down; it reduces coverage instead, which is reported alongside the score.
* Immature and small-sample values are excluded on the same terms as alerting,
  so the score and the alert list never contradict each other.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from qmis.core.metric_config import MetricRegistry
from qmis.core.periods import Period

DEFAULT_BANDS = [
    (90.0, "Excellent"),
    (75.0, "Healthy"),
    (60.0, "Attention required"),
    (0.0, "Critical"),
]


@dataclass
class ScoreResult:
    entity_id: int
    score: float | None
    band: str | None
    coverage: float
    metrics_used: int
    contributions: dict[str, float] = field(default_factory=dict)
    weights: dict[str, float] = field(default_factory=dict)
    excluded: dict[str, str] = field(default_factory=dict)


def band_for(score: float | None, bands=DEFAULT_BANDS) -> str | None:
    if score is None or (isinstance(score, float) and np.isnan(score)):
        return None
    for floor, label in bands:
        if score >= floor:
            return label
    return bands[-1][1]


def score_entities(
    comparison: pd.DataFrame,
    registry: MetricRegistry,
    period: Period,
    latest_period: Period | None = None,
    bands=DEFAULT_BANDS,
) -> pd.DataFrame:
    """Score every entity present in ``comparison``."""
    columns = [
        "entity_id", "entity_type", "entity_name", "owner_name", "period_key",
        "score", "band", "coverage", "metrics_used",
    ]
    if comparison.empty:
        return pd.DataFrame(columns=columns)

    scored_keys = {m.key: m for m in registry.scored()}
    if not scored_keys:
        return pd.DataFrame(columns=columns)
    total_weight = sum(m.weight for m in scored_keys.values())

    frame = comparison.loc[comparison["metric_key"].isin(scored_keys)].copy()
    if frame.empty:
        return pd.DataFrame(columns=columns)

    latest_period = latest_period or period
    usable: list[bool] = []
    points: list[float] = []
    for row in frame.itertuples(index=False):
        metric = scored_keys[row.metric_key]
        value = row.current
        if value is None or pd.isna(value):
            usable.append(False)
            points.append(np.nan)
            continue
        if not metric.is_mature(period, latest_period):
            usable.append(False)
            points.append(np.nan)
            continue
        denominator = getattr(row, "denominator", None)
        if (
            metric.min_denominator
            and denominator is not None
            and not pd.isna(denominator)
            and denominator < metric.min_denominator
        ):
            usable.append(False)
            points.append(np.nan)
            continue
        normalised = metric.normalise(float(value))
        if normalised is None:
            usable.append(False)
            points.append(np.nan)
            continue
        usable.append(True)
        points.append(normalised)

    frame["_points"] = points
    frame["_usable"] = usable
    frame["_weight"] = frame["metric_key"].map(lambda k: scored_keys[k].weight)
    usable_frame = frame.loc[frame["_usable"]]
    if usable_frame.empty:
        return pd.DataFrame(columns=columns)

    usable_frame = usable_frame.assign(_wp=usable_frame["_points"] * usable_frame["_weight"])
    grouped = usable_frame.groupby(
        ["entity_id", "entity_type", "entity_name", "owner_name"], dropna=False
    ).agg(
        weighted=("_wp", "sum"),
        weight=("_weight", "sum"),
        metrics_used=("_points", "count"),
    ).reset_index()

    grouped["score"] = (grouped["weighted"] / grouped["weight"]).round(2)
    grouped["coverage"] = (grouped["weight"] / total_weight).round(4)
    grouped["band"] = grouped["score"].map(lambda s: band_for(s, bands))
    grouped["period_key"] = period.key
    return grouped[columns]


def explain_score(
    comparison: pd.DataFrame,
    registry: MetricRegistry,
    entity_id: int,
    period: Period,
    latest_period: Period | None = None,
) -> ScoreResult:
    """Per-metric breakdown behind one entity's score, for the drill-down."""
    latest_period = latest_period or period
    scored_keys = {m.key: m for m in registry.scored()}
    rows = comparison.loc[
        (comparison["entity_id"] == entity_id)
        & (comparison["metric_key"].isin(scored_keys))
    ]
    contributions: dict[str, float] = {}
    weights: dict[str, float] = {}
    excluded: dict[str, str] = {}
    for row in rows.itertuples(index=False):
        metric = scored_keys[row.metric_key]
        if row.current is None or pd.isna(row.current):
            excluded[metric.key] = "no value this period"
            continue
        if not metric.is_mature(period, latest_period):
            excluded[metric.key] = "not yet mature for this period"
            continue
        denominator = getattr(row, "denominator", None)
        if (
            metric.min_denominator
            and denominator is not None
            and not pd.isna(denominator)
            and denominator < metric.min_denominator
        ):
            excluded[metric.key] = f"sample below {metric.min_denominator}"
            continue
        normalised = metric.normalise(float(row.current))
        if normalised is None:
            excluded[metric.key] = "no thresholds configured"
            continue
        contributions[metric.key] = round(normalised, 2)
        weights[metric.key] = metric.weight

    if not contributions:
        return ScoreResult(entity_id, None, None, 0.0, 0, {}, {}, excluded)
    weight_sum = sum(weights.values())
    score = round(
        sum(contributions[k] * weights[k] for k in contributions) / weight_sum, 2
    )
    total_weight = sum(m.weight for m in scored_keys.values()) or 1.0
    return ScoreResult(
        entity_id=entity_id,
        score=score,
        band=band_for(score),
        coverage=round(weight_sum / total_weight, 4),
        metrics_used=len(contributions),
        contributions=contributions,
        weights=weights,
        excluded=excluded,
    )
