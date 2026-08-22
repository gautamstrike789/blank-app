"""Aggregation from BA level up to Owner and Organisation.

A rate cannot be averaged.  ``mean(D1% per BA)`` gives every BA equal say
regardless of whether they submitted four donors or four hundred, which is how
an Owner with one terrible tiny BA ends up looking worse than an Owner with
systematic problems across a large team.

So rates are always **recomputed from their components** where the components
exist (``sum(debit1) / sum(submissions)``), and only fall back to a
submission-weighted mean when they do not.  Counts sum.  The fallback is
recorded so the dashboard can say which of the two it used.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from qmis.core.metric_config import MetricRegistry
from qmis.core.models import BA, ORG, OWNER, TEAM

ROLLUP_COLUMNS = ["entity_id", "period_key", "grain", "metric_key", "value", "denominator", "method"]


def rollup_history(
    history: pd.DataFrame,
    registry: MetricRegistry,
    entities: pd.DataFrame,
    levels: tuple[str, ...] = (OWNER, ORG),
) -> pd.DataFrame:
    """Derive Owner/Org facts from BA facts.

    Returns rows in the same shape as ``load_history`` so the result can simply
    be concatenated onto it.  Existing facts at a level are *not* overwritten:
    if the source file already reports Owner totals, those are authoritative
    and this function only fills genuine gaps.
    """
    if history.empty or entities.empty:
        return history

    base = history.loc[history["entity_type"] == BA]
    if base.empty:
        return history

    parents = entities.set_index("id")[["parent_id", "entity_type", "name"]].to_dict("index")
    out_frames: list[pd.DataFrame] = [history]

    child_level = BA
    for level in levels:
        mapped = _map_to_parent(base if child_level == BA else out_frames[-1], parents, level, entities)
        if mapped.empty:
            continue
        aggregated = _aggregate(mapped, registry)
        if aggregated.empty:
            continue
        existing = set(
            zip(
                history.loc[history["entity_type"] == level, "entity_id"],
                history.loc[history["entity_type"] == level, "period_key"],
                history.loc[history["entity_type"] == level, "metric_key"],
            )
        )
        keep = [
            not ((row.entity_id, row.period_key, row.metric_key) in existing)
            for row in aggregated.itertuples(index=False)
        ]
        aggregated = aggregated.loc[keep]
        if aggregated.empty:
            continue
        out_frames.append(_decorate(aggregated, entities, level))
    return pd.concat(out_frames, ignore_index=True)


def _map_to_parent(
    frame: pd.DataFrame, parents: dict, level: str, entities: pd.DataFrame
) -> pd.DataFrame:
    """Attach the target-level ancestor id to every BA-level fact."""
    if frame.empty:
        return frame
    lookup = entities.set_index("id")[["parent_id", "entity_type"]].to_dict("index")

    def ancestor(entity_id: int) -> int | None:
        seen: set[int] = set()
        current = entity_id
        while current is not None and current not in seen:
            seen.add(current)
            info = lookup.get(current)
            if info is None:
                return None
            if info["entity_type"] == level:
                return current
            current = info["parent_id"]
        return None

    base = frame.loc[frame["entity_type"] == BA].copy()
    if base.empty:
        return base
    base["target_id"] = base["entity_id"].map(ancestor)
    return base.loc[base["target_id"].notna()]


def _aggregate(frame: pd.DataFrame, registry: MetricRegistry) -> pd.DataFrame:
    """Sum counts; recompute rates from their components."""
    counts = frame.loc[
        frame["metric_key"].map(lambda k: registry[k].unit in ("count",) if k in registry else False)
    ]
    summed = (
        counts.groupby(["target_id", "period_key", "grain", "metric_key"], as_index=False)["value"]
        .sum()
        .assign(denominator=np.nan, method="sum")
    )

    component_index = {}
    if not summed.empty:
        for row in summed.itertuples(index=False):
            component_index[(row.target_id, row.period_key, row.metric_key)] = row.value

    rate_rows: list[dict] = []
    rates = frame.loc[
        frame["metric_key"].map(
            lambda k: k in registry and registry[k].unit in ("percent", "currency", "number")
        )
    ]
    for (target_id, period_key, grain, metric_key), block in rates.groupby(
        ["target_id", "period_key", "grain", "metric_key"], sort=False
    ):
        metric = registry[str(metric_key)]
        numerator = component_index.get((target_id, period_key, metric.numerator or ""))
        denominator = component_index.get((target_id, period_key, metric.denominator or ""))
        if metric.unit == "percent" and numerator is not None and denominator:
            rate_rows.append(
                {
                    "target_id": target_id,
                    "period_key": period_key,
                    "grain": grain,
                    "metric_key": metric_key,
                    "value": numerator / denominator * 100.0,
                    "denominator": denominator,
                    "method": "recomputed",
                }
            )
            continue
        weights = block["denominator"]
        if weights.notna().any() and weights.fillna(0).sum() > 0:
            value = float(
                np.average(
                    block["value"].to_numpy(dtype=float),
                    weights=weights.fillna(0).to_numpy(dtype=float),
                )
            )
            method = "weighted_mean"
            total = float(weights.fillna(0).sum())
        else:
            value = float(block["value"].mean())
            method = "simple_mean"
            total = np.nan
        rate_rows.append(
            {
                "target_id": target_id,
                "period_key": period_key,
                "grain": grain,
                "metric_key": metric_key,
                "value": value,
                "denominator": total,
                "method": method,
            }
        )

    rate_frame = pd.DataFrame(rate_rows)
    combined = pd.concat([summed, rate_frame], ignore_index=True) if not rate_frame.empty else summed
    if combined.empty:
        return combined
    # Attach the denominator to summed count rows too, for consistency.
    return combined.rename(columns={"target_id": "entity_id"})


def _decorate(frame: pd.DataFrame, entities: pd.DataFrame, level: str) -> pd.DataFrame:
    info = entities.set_index("id")[["name", "parent_id", "parent_name"]].to_dict("index")
    frame = frame.copy()
    frame["entity_type"] = level
    frame["entity_name"] = frame["entity_id"].map(lambda i: info.get(i, {}).get("name", ""))
    frame["owner_id"] = frame["entity_id"].map(lambda i: info.get(i, {}).get("parent_id"))
    frame["owner_name"] = frame["entity_id"].map(lambda i: info.get(i, {}).get("parent_name") or "")
    return frame[
        [
            "entity_id", "entity_type", "entity_name", "owner_id", "owner_name",
            "period_key", "grain", "metric_key", "value", "denominator",
        ]
    ]
