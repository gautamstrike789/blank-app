"""Collapse correlated alert cascades into one finding.

The debit ladder is not twelve independent metrics.  A cohort that stops giving
at debit 4 is, by construction, also missing from debits 5 through 12, so one
underlying problem prints as nine near-identical alerts and pushes everything
else off the top of the list.  Nobody reads the ninth one.

Metrics that move together are tagged with a ``family`` in ``metrics.yaml``.
When an entity trips three or more members of a family in the same period they
are replaced by a single composite alert naming the span, the worst stage and
the count.  The individual assessments are kept - the drill-down still shows
every stage - they simply stop competing for attention in the alert list.

Headline metrics (anything carrying weight in the quality score) are never
folded away, because those are the numbers management is asking about.
"""

from __future__ import annotations

import pandas as pd

from qmis.core.metric_config import MetricRegistry
from qmis.core.models import GREEN, KIND_COMPOSITE, SEVERITY_ORDER

MIN_FAMILY_SIZE = 3


def group_family_alerts(
    assessments: pd.DataFrame, registry: MetricRegistry, min_size: int = MIN_FAMILY_SIZE
) -> pd.DataFrame:
    """Replace family cascades with composite rows.

    Returns a new frame: constituent rows have ``alertable`` cleared and gain a
    ``rolled_into`` label; one composite row is appended per collapsed group.
    """
    if assessments.empty:
        return assessments

    frame = assessments.copy()
    if "rolled_into" not in frame.columns:
        frame["rolled_into"] = None

    families = {
        m.key: m.family
        for m in registry
        if m.family and not m.include_in_score
    }
    if not families:
        return frame

    frame["_family"] = frame["metric_key"].map(families)
    candidates = frame.loc[
        frame["_family"].notna() & frame["alertable"] & ~frame["is_improvement"].astype(bool)
    ]
    if candidates.empty:
        return frame.drop(columns=["_family"])

    composites: list[dict] = []
    to_clear: list[int] = []
    for (entity_id, family), block in candidates.groupby(["entity_id", "_family"], sort=False):
        if len(block) < min_size:
            continue
        worst = block.sort_values("priority", ascending=False).iloc[0]
        names = [
            registry[k].name if k in registry else str(k)
            for k in block["metric_key"]
        ]
        severity = max(block["severity"], key=lambda s: SEVERITY_ORDER.get(s, 0))
        label = family.replace("_", " ")
        worst_metric = registry[worst["metric_key"]] if worst["metric_key"] in registry else None
        worst_text = (
            f"{worst_metric.name} ({worst_metric.format(worst['previous'])} → "
            f"{worst_metric.format(worst['current'])})"
            if worst_metric is not None
            else str(worst["metric_key"])
        )
        headline = (
            f"{_icon(severity)} {worst['entity_name']}'s {label} deteriorated at "
            f"{len(block)} stages - worst at {worst_text}"
        )
        explanation = (
            f"{len(block)} stages of the {label} moved the wrong way this period: "
            f"{', '.join(sorted(names))}. These stages are not independent - a cohort that "
            f"stops giving at one debit is missing from every later one - so they are "
            f"reported together as a single finding rather than as {len(block)} separate "
            f"alerts. The most serious stage is {worst_text}. "
            f"{worst['explanation']}"
        )
        row = worst.to_dict()
        row.update(
            {
                "metric_key": f"{family}:composite",
                "kind": KIND_COMPOSITE,
                "severity": severity,
                "headline": headline[:500],
                "explanation": explanation,
                "priority": float(worst["priority"]) + 5.0,
                "alertable": True,
                "rolled_into": None,
                # A composite spans several metrics, so it has no single
                # current/previous value. The headline carries the numbers;
                # leaving these blank stops the table implying otherwise.
                "current": None,
                "previous": None,
                "delta": None,
                "pct_change": None,
            }
        )
        composites.append(row)
        to_clear.extend(block.index.tolist())

    if not composites:
        return frame.drop(columns=["_family"])

    frame.loc[to_clear, "alertable"] = False
    frame.loc[to_clear, "rolled_into"] = frame.loc[to_clear, "_family"].map(
        lambda f: f"{f}:composite"
    )
    out = pd.concat([frame, pd.DataFrame(composites)], ignore_index=True)
    return out.drop(columns=["_family"])


def _icon(severity: str) -> str:
    return {"RED": "🔴", "ORANGE": "🟠", "YELLOW": "🟡", GREEN: "🟢"}.get(severity, "")
