"""Owner view - which Owner needs management attention, and why."""

from __future__ import annotations

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from qmis.app.common import (
    BAND_COLOURS,
    alert_table,
    cached_alerts,
    cached_periods,
    cached_scores,
    empty_state,
    format_change,
    format_value,
    metric_name,
    period_selector,
    registry,
    scope_ids,
)
from qmis.auth.rbac import scope_frame
from qmis.core.models import ORANGE, RED, YELLOW


def render(principal) -> None:
    st.title("Owners")
    if not cached_periods():
        empty_state("No data loaded yet.")
        return

    period_key = period_selector()
    ids = scope_ids(principal)
    scores = scope_frame(cached_scores((period_key,)), ids)
    alerts = scope_frame(cached_alerts(period_key), ids)
    owners = scores.loc[scores["entity_type"] == "owner"] if not scores.empty else scores
    if owners.empty:
        empty_state(
            "No Owner-level data for this period.",
            "Owner analysis needs an export carrying an 'OWNER NAME' column.",
        )
        return

    _ranking(owners, alerts)
    st.divider()
    _profile(owners, alerts, period_key, ids)


def _ranking(owners: pd.DataFrame, alerts: pd.DataFrame) -> None:
    st.subheader("Who needs attention")
    st.caption(
        "Ranked by quality score, then by the weight of what is alerting. "
        "An Owner with one critical metric on a large team outranks one with three on a small team."
    )
    table = owners.copy()
    counts = (
        alerts.loc[~alerts["is_improvement"]].groupby(["owner_name", "severity"]).size().unstack(fill_value=0)
        if not alerts.empty
        else pd.DataFrame()
    )
    table = table.sort_values("score")
    display = pd.DataFrame(
        {
            "Owner": table["entity_name"],
            "Quality score": table["score"].round(1),
            "Band": table["band"],
            "Change": table["delta"].map(lambda v: "-" if pd.isna(v) else f"{v:+.1f}"),
            "Critical": table["red_count"],
            "High": table["orange_count"],
            "Warning": table["yellow_count"],
            "Healthy": table["green_count"],
            "Metrics scored": table["metrics_used"],
        }
    )
    st.dataframe(
        display,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Quality score": st.column_config.ProgressColumn(
                "Quality score", min_value=0, max_value=100, format="%.1f"
            )
        },
    )

    # `table` is already worst-first; plotly draws the first row at the BOTTOM,
    # so sort descending to put the Owner needing most attention at the top.
    chart = table.head(15).sort_values("score", ascending=False)
    figure = px.bar(
        chart, x="score", y="entity_name", orientation="h",
        color="band", color_discrete_map=BAND_COLOURS, text="score",
    )
    figure.update_traces(texttemplate="%{text:.0f}")
    figure.update_layout(
        height=max(320, 26 * len(chart)), margin=dict(l=10, r=10, t=10, b=10),
        xaxis_title="Quality score", yaxis_title=None, legend_title=None,
    )
    st.plotly_chart(figure, use_container_width=True)


def _profile(owners: pd.DataFrame, alerts: pd.DataFrame, period_key: str, ids) -> None:
    st.subheader("Owner profile")
    names = sorted(owners["entity_name"])
    chosen = st.selectbox("Owner", names)
    row = owners.loc[owners["entity_name"] == chosen].iloc[0]

    columns = st.columns(5)
    columns[0].metric(
        "Quality score", f"{row['score']:.1f}",
        None if pd.isna(row["delta"]) else f"{row['delta']:+.1f}",
    )
    columns[1].metric("Band", row["band"])
    columns[2].metric("Critical", int(row["red_count"]))
    columns[3].metric("High attention", int(row["orange_count"]))
    columns[4].metric("Healthy metrics", int(row["green_count"]))

    theirs = alerts.loc[
        (alerts["entity_name"] == chosen) | (alerts["owner_name"] == chosen)
    ] if not alerts.empty else alerts
    if theirs.empty:
        st.success("Nothing alerting for this Owner.")
        return

    own_level = theirs.loc[theirs["entity_type"] == "owner"]
    team_level = theirs.loc[theirs["entity_type"] == "ba"]

    left, right = st.columns(2, gap="large")
    with left:
        st.markdown("**Owner-level findings**")
        if own_level.empty:
            st.caption("None.")
        else:
            for r in own_level.head(8).itertuples(index=False):
                st.markdown(f"- {r.headline}")
    with right:
        st.markdown("**BAs needing attention**")
        critical = team_level.loc[team_level["severity"].isin([RED, ORANGE])]
        if critical.empty:
            st.caption("None.")
        else:
            summary = (
                critical.groupby("entity_name")
                .agg(findings=("id", "count"), worst=("severity", "first"))
                .sort_values("findings", ascending=False)
                .head(10)
                .reset_index()
                .rename(columns={"entity_name": "BA", "findings": "Findings", "worst": "Worst"})
            )
            st.dataframe(summary, use_container_width=True, hide_index=True)

    with st.expander("All findings for this Owner"):
        st.dataframe(alert_table(theirs, limit=500), use_container_width=True, hide_index=True)
