"""Trend analysis - any entity, any metric, against its thresholds and history."""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from qmis.app.common import (
    cached_entities,
    cached_history,
    cached_periods,
    empty_state,
    metric_name,
    registry,
    scope_ids,
)
from qmis.auth.rbac import scope_frame
from qmis.core.periods import Period


def render(principal) -> None:
    st.title("Trends")
    if not cached_periods():
        empty_state("No data loaded yet.")
        return

    ids = scope_ids(principal)
    entities = scope_frame(cached_entities(), ids, column="id")
    if entities.empty:
        empty_state("Nothing in scope.")
        return

    reg = registry()
    controls = st.columns([1, 2, 2])
    level = controls[0].selectbox(
        "Level", [t for t in ("org", "owner", "team", "ba") if t in set(entities["entity_type"])]
    )
    pool = entities.loc[entities["entity_type"] == level].sort_values("name")
    chosen = controls[1].multiselect(
        "Compare", pool["name"].tolist(), default=pool["name"].tolist()[:3]
    )
    metric_key = controls[2].selectbox(
        "Metric", [m.key for m in reg], format_func=metric_name
    )
    if not chosen:
        st.info("Pick at least one entity to chart.")
        return

    metric = reg[metric_key]
    selected_ids = tuple(int(i) for i in pool.loc[pool["name"].isin(chosen), "id"])
    history = cached_history((metric_key,), selected_ids)
    if history.empty:
        st.info("No history for this combination.")
        return

    show_average = st.toggle("Show 4-period moving average", value=True)
    figure = go.Figure()
    palette = ["#2c7fb8", "#c0392b", "#2e8b57", "#8e44ad", "#e08c1a", "#16a085"]
    for i, (name, block) in enumerate(history.groupby("entity_name")):
        block = block.sort_values("period_key")
        colour = palette[i % len(palette)]
        figure.add_trace(
            go.Scatter(
                x=block["period_key"], y=block["value"], mode="lines+markers",
                name=str(name), line=dict(color=colour, width=2.5),
            )
        )
        if show_average and len(block) >= 4:
            figure.add_trace(
                go.Scatter(
                    x=block["period_key"],
                    y=block["value"].rolling(4, min_periods=2).mean(),
                    mode="lines", name=f"{name} (4-period avg)",
                    line=dict(color=colour, width=1.5, dash="dot"), opacity=0.6,
                    showlegend=False,
                )
            )

    _bands(figure, metric)
    figure.update_layout(
        height=460, margin=dict(l=10, r=10, t=30, b=10),
        yaxis_title=metric.name, xaxis_title=None,
        legend=dict(orientation="h", y=1.12),
    )
    st.plotly_chart(figure, use_container_width=True)
    st.caption(metric.description or "")
    st.caption(f"Rule: {metric.direction.replace('_', ' ')} - {metric.threshold_reference()}.")

    with st.expander("Values"):
        table = history.pivot_table(
            index="period_key", columns="entity_name", values="value"
        ).sort_index()
        st.dataframe(table.round(metric.decimals), use_container_width=True)


def _bands(figure: go.Figure, metric) -> None:
    """Shade the warning and critical zones so the chart says where trouble starts."""
    warn, crit = metric.warning_threshold, metric.critical_threshold
    if warn is None and crit is None:
        return
    if metric.direction == "higher_is_better":
        if crit is not None:
            figure.add_hrect(y0=crit - 50, y1=crit, fillcolor="#c0392b", opacity=0.07, line_width=0)
        if warn is not None and crit is not None:
            figure.add_hrect(y0=crit, y1=warn, fillcolor="#e08c1a", opacity=0.07, line_width=0)
    elif metric.direction == "lower_is_better":
        if crit is not None:
            figure.add_hrect(y0=crit, y1=crit + 50, fillcolor="#c0392b", opacity=0.07, line_width=0)
        if warn is not None and crit is not None:
            figure.add_hrect(y0=warn, y1=crit, fillcolor="#e08c1a", opacity=0.07, line_width=0)
    for value, colour, label in ((warn, "#e08c1a", "warning"), (crit, "#c0392b", "critical")):
        if value is not None:
            figure.add_hline(y=value, line_dash="dash", line_color=colour, annotation_text=label)
