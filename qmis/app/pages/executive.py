"""Executive dashboard - what changed, who changed, what needs attention now."""

from __future__ import annotations

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from qmis.app.common import (
    BAND_COLOURS,
    SEVERITY_COLOURS,
    alert_table,
    cached_alerts,
    cached_entities,
    cached_latest_period,
    cached_periods,
    cached_scores,
    empty_state,
    format_change,
    format_value,
    metric_name,
    period_selector,
    registry,
    scope_ids,
    severity_chip,
)
from qmis.auth.rbac import scope_frame
from qmis.core.models import GREEN, ORANGE, RED, YELLOW


def render(principal) -> None:
    st.title("Quality intelligence")
    periods = cached_periods()
    if not periods:
        empty_state(
            "No data has been loaded yet.",
            "Go to **Data & uploads** and upload a weekly report, or drop one in the watched folder.",
        )
        return

    period_key = period_selector()
    ids = scope_ids(principal)
    alerts = scope_frame(cached_alerts(period_key), ids)
    scores = scope_frame(cached_scores((period_key,)), ids)
    problems = alerts.loc[~alerts["is_improvement"]] if not alerts.empty else alerts
    wins = alerts.loc[alerts["is_improvement"]] if not alerts.empty else alerts

    _headline(period_key, scores, problems, wins, principal, ids)
    st.divider()

    left, right = st.columns([3, 2], gap="large")
    with left:
        _priority_list(problems)
    with right:
        _score_trend(period_key, ids)
        _improvements(wins)

    st.divider()
    _change_matrix(problems)
    st.divider()
    _heatmap(period_key, alerts, principal)


# --------------------------------------------------------------------------- #
def _headline(period_key, scores, problems, wins, principal, ids) -> None:
    org = scores.loc[scores["entity_type"] == "org"] if not scores.empty else scores
    if org.empty and not scores.empty:
        # An Owner sees their own row where an organisation row is not visible.
        org = scores.loc[scores["entity_type"] == "owner"].head(1)

    counts = problems["severity"].value_counts().to_dict() if not problems.empty else {}
    new_alerts = int(problems["is_new"].sum()) if not problems.empty else 0
    # Count everyone who REPORTED this period, not just those with enough
    # volume to be scored - "4 BAs" would be alarming and wrong.
    entities = scope_frame(cached_entities(), ids, column="id")
    reporting = (
        entities.loc[entities["last_seen_period"] == period_key]
        if not entities.empty
        else entities
    )
    bas = int((reporting["entity_type"] == "ba").sum()) if not reporting.empty else 0
    owners = int((reporting["entity_type"] == "owner").sum()) if not reporting.empty else 0
    scored_bas = int((scores["entity_type"] == "ba").sum()) if not scores.empty else 0

    columns = st.columns(6)
    if not org.empty:
        row = org.iloc[0]
        delta = None if pd.isna(row["delta"]) else f"{row['delta']:+.1f}"
        columns[0].metric("Quality score", f"{row['score']:.1f}", delta, help=str(row["band"]))
    else:
        columns[0].metric("Quality score", "-")
    columns[1].metric("Critical", counts.get(RED, 0), help="Threshold breached or severe deterioration")
    columns[2].metric("High attention", counts.get(ORANGE, 0))
    columns[3].metric("New this week", new_alerts, help="Not alerting in the previous period")
    columns[4].metric("Improving", len(wins))
    columns[5].metric(
        "Reporting",
        f"{bas} BAs" if bas else f"{owners} owners",
        help=(
            f"{owners} owners. {scored_bas} BA(s) had enough submissions this period to carry "
            f"a quality score; the rest are tracked but too small to score weekly."
        ),
    )

    if not problems.empty:
        unconfirmed = int((~problems["statistically_confirmed"].fillna(True)).sum())
        if unconfirmed:
            st.caption(
                f"{unconfirmed} of these findings sit the wrong side of a threshold but are not "
                f"yet statistically distinguishable from normal variation at this sample size. "
                f"They are shown, not escalated."
            )


def _priority_list(problems: pd.DataFrame) -> None:
    st.subheader("Needs attention now")
    if problems.empty:
        st.success("Nothing is alerting in this period.")
        return
    for row in problems.head(8).itertuples(index=False):
        colour = SEVERITY_COLOURS.get(row.severity, "#888")
        with st.container(border=True):
            st.markdown(
                f"<div style='border-left:5px solid {colour};padding-left:12px'>"
                f"<strong>{row.headline}</strong></div>",
                unsafe_allow_html=True,
            )
            with st.expander("Why this is flagged"):
                st.write(row.explanation)
                detail = st.columns(4)
                detail[0].caption(f"**Level**  \n{row.entity_type}")
                detail[1].caption(f"**Owner**  \n{row.owner_name or '-'}")
                detail[2].caption(f"**Trigger**  \n{row.kind}")
                detail[3].caption(
                    f"**Basis**  \n{'single period' if row.basis in (None, 'period') else row.basis}"
                )


def _score_trend(period_key: str, ids) -> None:
    st.subheader("Quality score")
    scores = scope_frame(cached_scores(), ids)
    if scores.empty:
        st.caption("No scores yet.")
        return
    org = scores.loc[scores["entity_type"] == "org"]
    if org.empty:
        org = scores.loc[scores["entity_type"] == "owner"]
        if org.empty:
            st.caption("No scores yet.")
            return
        org = org.loc[org["entity_id"] == org["entity_id"].iloc[0]]
    org = org.sort_values("period_key")
    figure = go.Figure()
    figure.add_trace(
        go.Scatter(
            x=org["period_key"], y=org["score"], mode="lines+markers",
            line=dict(color="#2c7fb8", width=3), name="Quality score",
        )
    )
    for floor, label in [(90, "Excellent"), (75, "Healthy"), (60, "Attention")]:
        figure.add_hline(
            y=floor, line_dash="dot", line_color=BAND_COLOURS.get(label, "#999"),
            annotation_text=label, annotation_position="right",
        )
    figure.update_layout(
        height=260, margin=dict(l=10, r=10, t=10, b=10), yaxis_range=[40, 102],
        showlegend=False, xaxis_title=None, yaxis_title=None,
    )
    st.plotly_chart(figure, use_container_width=True)


def _improvements(wins: pd.DataFrame) -> None:
    st.subheader("Going the right way")
    if wins.empty:
        st.caption("No significant improvements this period.")
        return
    for row in wins.head(5).itertuples(index=False):
        st.markdown(f"- {row.headline}")


def _change_matrix(problems: pd.DataFrame) -> None:
    st.subheader("Weekly change matrix")
    st.caption("Every alerting metric, previous value against current, ranked by priority.")
    if problems.empty:
        st.caption("Nothing to show.")
        return
    st.dataframe(alert_table(problems, limit=200), use_container_width=True, hide_index=True)


def _heatmap(period_key: str, alerts: pd.DataFrame, principal) -> None:
    st.subheader("Where the problems are")
    if alerts.empty:
        st.caption("Nothing to show.")
        return
    reg = registry()
    level = "ba" if principal.is_scoped else "owner"
    block = alerts.loc[alerts["entity_type"] == level]
    if block.empty:
        block = alerts
        level = str(block["entity_type"].iloc[0])
    severity_rank = {RED: 3, ORANGE: 2, YELLOW: 1, GREEN: 0}
    block = block.assign(rank=block["severity"].map(severity_rank).fillna(0))
    block = block.loc[block["metric_key"].isin(set(reg.keys))]
    if block.empty:
        st.caption("Only grouped findings this period; open the Alert centre for the detail.")
        return
    pivot = block.pivot_table(
        index="entity_name", columns="metric_key", values="rank", aggfunc="max"
    ).fillna(0)
    pivot = pivot.loc[pivot.max(axis=1).sort_values(ascending=False).index].head(30)
    pivot.columns = [metric_name(c) for c in pivot.columns]
    figure = px.imshow(
        pivot,
        color_continuous_scale=[(0, "#e8f0e8"), (0.34, "#d4b106"), (0.67, "#e08c1a"), (1, "#c0392b")],
        aspect="auto",
        labels=dict(color="Severity"),
    )
    figure.update_layout(
        height=max(320, 22 * len(pivot)), margin=dict(l=10, r=10, t=10, b=10),
        coloraxis_showscale=False, xaxis_title=None, yaxis_title=None,
    )
    st.plotly_chart(figure, use_container_width=True)
    st.caption(f"{level.upper()}s with the most severe findings. Green = healthy, red = critical.")
