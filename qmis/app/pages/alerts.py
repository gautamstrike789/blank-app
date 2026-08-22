"""Alert centre - filter, read the reasoning, acknowledge."""

from __future__ import annotations

import datetime as dt

import pandas as pd
import streamlit as st
from sqlalchemy import select

from qmis.app.common import (
    SEVERITY_COLOURS,
    SEVERITY_ORDER_UI,
    alert_table,
    cached_alerts,
    cached_periods,
    clear_caches,
    db,
    empty_state,
    metric_name,
    period_selector,
    registry,
    scope_ids,
)
from qmis.auth.rbac import ACKNOWLEDGE_ALERTS, scope_frame
from qmis.core.models import Alert


def render(principal) -> None:
    st.title("Alert centre")
    if not cached_periods():
        empty_state("No data loaded yet.")
        return

    period_key = period_selector()
    ids = scope_ids(principal)
    alerts = scope_frame(cached_alerts(period_key), ids)
    if alerts.empty:
        st.success("No alerts in this period.")
        return

    filtered = _filters(alerts)
    st.caption(f"{len(filtered)} of {len(alerts)} findings match.")
    if filtered.empty:
        return

    tab_cards, tab_table = st.tabs(["Reasoned view", "Table"])
    with tab_cards:
        _cards(filtered, principal)
    with tab_table:
        table = alert_table(filtered, limit=1000)
        st.dataframe(table, use_container_width=True, hide_index=True)
        st.download_button(
            "Download as CSV",
            table.to_csv(index=False).encode("utf-8"),
            file_name=f"qmis_alerts_{period_key}.csv",
            mime="text/csv",
        )


def _filters(alerts: pd.DataFrame) -> pd.DataFrame:
    reg = registry()
    with st.container(border=True):
        row1 = st.columns(4)
        severities = row1[0].multiselect(
            "Severity", SEVERITY_ORDER_UI,
            default=[s for s in SEVERITY_ORDER_UI if s in set(alerts["severity"]) and s != "GREEN"],
        )
        levels = row1[1].multiselect(
            "Level", sorted(alerts["entity_type"].unique()),
            default=sorted(alerts["entity_type"].unique()),
        )
        owners = row1[2].multiselect(
            "Owner", sorted(o for o in alerts["owner_name"].unique() if o)
        )
        metrics = row1[3].multiselect(
            "Metric",
            sorted(alerts["metric_key"].unique()),
            format_func=metric_name,
        )
        row2 = st.columns(4)
        kinds = row2[0].multiselect("Trigger", sorted(alerts["kind"].unique()))
        only_new = row2[1].toggle("New this week only")
        hide_improvements = row2[2].toggle("Hide improvements")
        confirmed_only = row2[3].toggle(
            "Statistically confirmed only",
            help="Hide findings the sample size cannot yet distinguish from normal variation.",
        )

    out = alerts
    if severities:
        out = out.loc[out["severity"].isin(severities)]
    if levels:
        out = out.loc[out["entity_type"].isin(levels)]
    if owners:
        out = out.loc[out["owner_name"].isin(owners)]
    if metrics:
        out = out.loc[out["metric_key"].isin(metrics)]
    if kinds:
        out = out.loc[out["kind"].isin(kinds)]
    if only_new:
        out = out.loc[out["is_new"]]
    if hide_improvements:
        out = out.loc[~out["is_improvement"]]
    if confirmed_only:
        out = out.loc[out["statistically_confirmed"].fillna(True)]
    return out.sort_values("priority", ascending=False)


def _cards(alerts: pd.DataFrame, principal) -> None:
    can_ack = principal.can(ACKNOWLEDGE_ALERTS)
    for row in alerts.head(60).itertuples(index=False):
        colour = SEVERITY_COLOURS.get(row.severity, "#888")
        with st.container(border=True):
            acknowledged = row.acknowledged_at is not None
            # Markdown is not processed inside a raw HTML block, so the emphasis
            # has to be HTML too or it renders as literal asterisks.
            title = (
                f"<s>{row.headline}</s>" if acknowledged else f"<strong>{row.headline}</strong>"
            )
            st.markdown(
                f"<div style='border-left:5px solid {colour};padding-left:12px'>{title}</div>",
                unsafe_allow_html=True,
            )
            st.write(row.explanation)
            meta = st.columns(5)
            meta[0].caption(f"**Level**  \n{row.entity_type}")
            meta[1].caption(f"**Owner**  \n{row.owner_name or '-'}")
            meta[2].caption(f"**Trigger**  \n{row.kind}")
            meta[3].caption(f"**Streak**  \n{row.consecutive_declines} period(s)")
            meta[4].caption(f"**New**  \n{'yes' if row.is_new else 'no'}")
            if acknowledged:
                st.caption(f"Acknowledged by {row.acknowledged_by} on {row.acknowledged_at:%d %b %Y}")
            elif can_ack:
                with st.popover("Acknowledge"):
                    note = st.text_input("Note (optional)", key=f"note_{row.id}")
                    if st.button("Confirm", key=f"ack_{row.id}"):
                        _acknowledge(int(row.id), principal.email or principal.name, note)
                        clear_caches()
                        st.rerun()


def _acknowledge(alert_id: int, who: str, note: str) -> None:
    with db() as session:
        alert = session.get(Alert, alert_id)
        if alert is not None:
            alert.acknowledged_at = dt.datetime.now(dt.timezone.utc)
            alert.acknowledged_by = who
            alert.acknowledgement_note = note or None
