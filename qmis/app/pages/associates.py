"""BA view - individual profiles and the saved questions managers actually ask."""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from qmis.app.common import (
    alert_table,
    cached_alerts,
    cached_entities,
    cached_history,
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
from qmis.core.models import ORANGE, RED
from qmis.core.periods import Period

SAVED_QUESTIONS = {
    "Show everything": None,
    "Critical Debit 1 decline": ("d1_pct", "decline"),
    "RJBD1 increased significantly": ("rjbd1_pct", "decline"),
    "More than 3 critical findings": ("*", "many_critical"),
    "Improving consistently for 4+ periods": ("*", "improving_streak"),
}


def render(principal) -> None:
    st.title("Business associates")
    if not cached_periods():
        empty_state("No data loaded yet.")
        return

    period_key = period_selector()
    ids = scope_ids(principal)
    alerts = scope_frame(cached_alerts(period_key), ids)
    scores = scope_frame(cached_scores((period_key,)), ids)
    bas = scores.loc[scores["entity_type"] == "ba"] if not scores.empty else scores
    if bas.empty and (alerts.empty or "ba" not in set(alerts["entity_type"])):
        empty_state(
            "No BA-level data for this period.",
            "BA analysis needs an export carrying a 'BAName' column.",
        )
        return

    tab_find, tab_profile = st.tabs(["Find BAs", "Individual profile"])
    with tab_find:
        _finder(alerts, bas)
    with tab_profile:
        _profile(alerts, bas, period_key, ids)


def _finder(alerts: pd.DataFrame, bas: pd.DataFrame) -> None:
    st.caption("Answers to the questions that would otherwise mean comparing two spreadsheets.")
    question = st.selectbox("Question", list(SAVED_QUESTIONS))
    ba_alerts = alerts.loc[alerts["entity_type"] == "ba"] if not alerts.empty else alerts

    selection = SAVED_QUESTIONS[question]
    if selection is None:
        result = ba_alerts
    else:
        metric_key, mode = selection
        if mode == "decline":
            result = ba_alerts.loc[
                (ba_alerts["metric_key"] == metric_key)
                & (~ba_alerts["is_improvement"])
                & (ba_alerts["severity"].isin([RED, ORANGE]))
            ]
        elif mode == "many_critical":
            counts = (
                ba_alerts.loc[ba_alerts["severity"] == RED].groupby("entity_id").size()
            )
            keep = counts[counts > 3].index
            result = ba_alerts.loc[ba_alerts["entity_id"].isin(keep)]
        else:  # improving_streak
            result = ba_alerts.loc[
                ba_alerts["is_improvement"] & (ba_alerts["consecutive_declines"] == 0)
            ]
            history_streak = result.loc[result["headline"].str.contains("SUSTAINED", na=False)]
            result = history_streak if not history_streak.empty else result

    if result.empty:
        st.info("No BAs match this question in the selected period.")
        return
    st.caption(f"{result['entity_id'].nunique()} BA(s), {len(result)} finding(s).")
    st.dataframe(alert_table(result, limit=400), use_container_width=True, hide_index=True)


def _profile(alerts: pd.DataFrame, bas: pd.DataFrame, period_key: str, ids) -> None:
    entities = scope_frame(cached_entities("ba"), ids, column="id")
    if entities.empty:
        empty_state("No BAs on record.")
        return
    names = sorted(entities["name"])
    chosen = st.selectbox("Business associate", names, key="ba_profile_pick")
    row = entities.loc[entities["name"] == chosen].iloc[0]
    entity_id = int(row["id"])

    score_row = bas.loc[bas["entity_id"] == entity_id]
    columns = st.columns(5)
    columns[0].metric("Owner", row["parent_name"] or "-")
    if not score_row.empty:
        s = score_row.iloc[0]
        columns[1].metric(
            "Quality score", f"{s['score']:.1f}",
            None if pd.isna(s["delta"]) else f"{s['delta']:+.1f}",
        )
        columns[2].metric("Band", s["band"])
        columns[3].metric("Critical", int(s["red_count"]))
        columns[4].metric("High attention", int(s["orange_count"]))
    else:
        columns[1].metric("Quality score", "-", help="Not enough data to score this BA")

    st.caption(
        f"First seen {row['first_seen_period'] or '-'}, last reported "
        f"{row['last_seen_period'] or '-'}."
    )

    theirs = alerts.loc[alerts["entity_id"] == entity_id] if not alerts.empty else alerts
    if not theirs.empty:
        st.markdown("**Findings this period**")
        for r in theirs.head(10).itertuples(index=False):
            with st.container(border=True):
                st.markdown(f"**{r.headline}**")
                st.caption(r.explanation)
    else:
        st.success("Nothing alerting for this BA in this period.")

    _metric_history(entity_id, period_key)


def _metric_history(entity_id: int, period_key: str) -> None:
    reg = registry()
    st.markdown("**History**")
    choices = [m.key for m in reg if m.direction != "neutral"]
    metric_key = st.selectbox(
        "Metric", choices, format_func=metric_name, key="ba_metric_pick"
    )
    history = cached_history((metric_key, "submissions"), (entity_id,))
    if history.empty:
        st.caption("No history recorded.")
        return
    series = history.loc[history["metric_key"] == metric_key].sort_values("period_key")
    volume = history.loc[history["metric_key"] == "submissions"].sort_values("period_key")
    metric = reg[metric_key]

    figure = go.Figure()
    figure.add_trace(
        go.Bar(
            x=volume["period_key"], y=volume["value"], name="Submissions",
            marker_color="#dfe6ec", yaxis="y2", hovertemplate="%{y:.0f} submissions<extra></extra>",
        )
    )
    figure.add_trace(
        go.Scatter(
            x=series["period_key"], y=series["value"], mode="lines+markers",
            name=metric.name, line=dict(color="#2c7fb8", width=3),
        )
    )
    if metric.warning_threshold is not None:
        figure.add_hline(y=metric.warning_threshold, line_dash="dash", line_color="#e08c1a",
                         annotation_text="warning")
    if metric.critical_threshold is not None:
        figure.add_hline(y=metric.critical_threshold, line_dash="dash", line_color="#c0392b",
                         annotation_text="critical")
    figure.update_layout(
        height=380, margin=dict(l=10, r=10, t=30, b=10),
        yaxis=dict(title=metric.name),
        yaxis2=dict(title="Submissions", overlaying="y", side="right", showgrid=False),
        legend=dict(orientation="h", y=1.12), xaxis_title=None,
    )
    st.plotly_chart(figure, use_container_width=True)
    st.caption(
        "Submission volume is shown behind the rate deliberately: at this organisation's "
        "volumes a weekly BA rate on a handful of submissions moves for arithmetic reasons, "
        "not quality reasons."
    )
