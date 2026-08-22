"""Admin - metric rules, users and system state."""

from __future__ import annotations

import pandas as pd
import streamlit as st
from sqlalchemy import select

from qmis.app.common import clear_caches, db, registry, require
from qmis.auth.rbac import EDIT_METRICS, MANAGE_USERS, link_owner_users
from qmis.core.config import load_settings
from qmis.core.metric_config import METRICS_FILE, MetricRegistry, get_registry
from qmis.core.models import ROLES, Entity, NotificationLog, User


def render(principal) -> None:
    st.title("Administration")
    tab_metrics, tab_users, tab_notify, tab_system = st.tabs(
        ["Metric rules", "Users & access", "Notifications", "System"]
    )
    with tab_metrics:
        _metrics(principal)
    with tab_users:
        _users(principal)
    with tab_notify:
        _notifications()
    with tab_system:
        _system()


def _metrics(principal) -> None:
    reg = registry()
    st.caption(
        "Every alerting decision in the system comes from this table. Editing a threshold here "
        "changes behaviour everywhere without a code change."
    )
    review = reg.needing_review()
    if review:
        st.warning(
            f"**{len(review)} metric(s) are waiting on a business decision.** They are tracked and "
            f"charted, but raise no alerts and carry no weight in the quality score until someone "
            f"confirms which direction is good: "
            + ", ".join(m.name for m in review),
            icon="❓",
        )

    frame = pd.DataFrame(
        [
            {
                "Metric": m.name,
                "key": m.key,
                "Group": m.group,
                "Direction": m.direction,
                "Warning": m.warning_threshold,
                "Critical": m.critical_threshold,
                "Target": m.target,
                "Weight": m.weight,
                "In score": m.include_in_score,
                "Change %": m.percentage_change_threshold,
                "Change abs": m.absolute_change_threshold,
                "Min sample": m.min_denominator,
                "Anomaly": m.anomaly.enabled,
                "Needs review": m.needs_review,
            }
            for m in reg
        ]
    )
    if not require(principal, EDIT_METRICS):
        st.dataframe(frame.drop(columns=["key"]), use_container_width=True, hide_index=True)
        return

    edited = st.data_editor(
        frame,
        use_container_width=True,
        hide_index=True,
        disabled=["Metric", "key", "Group", "Direction"],
        column_config={
            "key": None,
            "Weight": st.column_config.NumberColumn(min_value=0, max_value=100),
        },
        key="metric_editor",
    )
    total = float(edited.loc[edited["In score"], "Weight"].sum())
    st.caption(
        f"Score weights currently total {total:g}. They are renormalised at evaluation time, "
        f"so they need not add to 100 - but keeping them there makes the numbers easier to read."
    )
    if st.button("Save metric rules", type="primary"):
        _save_metrics(edited)


def _save_metrics(edited: pd.DataFrame) -> None:
    reg = get_registry()
    payload = {}
    for row in edited.itertuples(index=False):
        payload[row.key] = {
            "warning_threshold": None if pd.isna(row.Warning) else float(row.Warning),
            "critical_threshold": None if pd.isna(row.Critical) else float(row.Critical),
            "target": None if pd.isna(row.Target) else float(row.Target),
            "weight": float(row.Weight or 0),
            "include_in_score": bool(getattr(row, "_8")),
        }
    import yaml

    raw = yaml.safe_load(METRICS_FILE.read_text(encoding="utf-8"))
    for entry in raw.get("metrics", []):
        update = payload.get(entry.get("key"))
        if update:
            entry.update(update)
    try:
        MetricRegistry.from_dict(raw)  # refuse to persist a registry that will not load
    except Exception as exc:
        st.error(f"Not saved - the edited rules are invalid: {exc}")
        return
    header = METRICS_FILE.read_text(encoding="utf-8").split("version:")[0]
    METRICS_FILE.write_text(
        header + yaml.safe_dump(raw, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    get_registry(reload=True)
    clear_caches()
    st.success("Metric rules saved. Re-run the alert engine to apply them to existing periods.")


def _users(principal) -> None:
    if not require(principal, MANAGE_USERS):
        return
    st.caption(
        "Authentication itself is delegated to your identity provider; this table controls "
        "what each verified user may see. An Owner's scope is enforced in the query, not "
        "by hiding controls."
    )
    with db() as session:
        users = session.execute(select(User).order_by(User.role, User.email)).scalars().all()
        owners = session.execute(
            select(Entity).where(Entity.entity_type == "owner").order_by(Entity.name)
        ).scalars().all()
        owner_names = {o.id: o.name for o in owners}
        rows = [
            {
                "Email": u.email,
                "Name": u.name,
                "Role": u.role,
                "Owner entity": owner_names.get(u.owner_entity_id, "-"),
                "Notify": u.notify_email,
                "Active": u.active,
            }
            for u in users
        ]
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True) if rows else st.info(
        "No users configured yet."
    )

    with st.form("add_user"):
        st.markdown("**Add a user**")
        columns = st.columns(4)
        email = columns[0].text_input("Email")
        name = columns[1].text_input("Name")
        role = columns[2].selectbox("Role", list(ROLES))
        owner_id = columns[3].selectbox(
            "Owner entity (owner role only)",
            [None] + [o.id for o in owners],
            format_func=lambda i: "-" if i is None else owner_names.get(i, str(i)),
        )
        if st.form_submit_button("Add") and email:
            with db() as session:
                session.add(
                    User(
                        email=email.strip().lower(),
                        name=name or email,
                        role=role,
                        owner_entity_id=owner_id,
                        active=True,
                    )
                )
            clear_caches()
            st.success(f"Added {email}")
            st.rerun()

    if st.button("Link owner accounts to their entity by name"):
        with db() as session:
            linked = link_owner_users(session)
        st.success(f"Linked {linked} account(s).")
        st.rerun()


def _notifications() -> None:
    settings = load_settings()
    config = settings.section("notifications")
    st.caption(
        "Notifications are deliberately restrained. Only the severities below are ever sent, "
        "and a recipient is not told about the same finding again until it gets worse or the "
        "repeat window passes. A system that emails everyone about every 0.1pp move gets "
        "filtered to junk, after which none of the analysis behind it matters."
    )
    st.code(
        f"enabled          : {config.get('enabled')}\n"
        f"severities sent  : {', '.join(config.get('send_severities', []))}\n"
        f"repeat after     : {config.get('repeat_after_periods')} periods\n"
        f"channels         : {len(config.get('channels') or [])} configured",
        language="text",
    )
    st.caption("Edit `qmis/config/settings.yaml` to add an email, Slack or Teams channel.")

    with db() as session:
        logs = session.execute(
            select(NotificationLog).order_by(NotificationLog.created_at.desc()).limit(50)
        ).scalars().all()
    if not logs:
        st.info("Nothing has been sent yet.")
        return
    st.markdown("**Recent notifications**")
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "When": log.created_at,
                    "Channel": log.channel,
                    "To": log.recipient,
                    "Subject": log.subject,
                    "Alerts": log.alert_count,
                    "Status": log.status,
                }
                for log in logs
            ]
        ),
        use_container_width=True,
        hide_index=True,
    )
    chosen = st.selectbox("Preview", range(len(logs)), format_func=lambda i: logs[i].subject)
    st.code(logs[chosen].body, language="text")


def _system() -> None:
    from qmis.analytics.repository import available_periods
    from qmis.core.db import database_url
    from qmis.core.models import Alert, Entity, Fact

    from sqlalchemy import func

    settings = load_settings()
    with db() as session:
        facts = session.execute(select(func.count()).select_from(Fact)).scalar_one()
        current = session.execute(
            select(func.count()).select_from(Fact).where(Fact.is_current.is_(True))
        ).scalar_one()
        entities = session.execute(
            select(Entity.entity_type, func.count()).group_by(Entity.entity_type)
        ).all()
        alerts = session.execute(select(func.count()).select_from(Alert)).scalar_one()
        periods = available_periods(session)

    columns = st.columns(4)
    columns[0].metric("Values stored", f"{facts:,}", help="Including superseded generations")
    columns[1].metric("Current values", f"{current:,}")
    columns[2].metric("Alerts recorded", f"{alerts:,}")
    columns[3].metric("Periods", len(periods))
    st.write({t: n for t, n in entities})
    st.code(
        f"database   : {database_url()}\n"
        f"grain      : {settings.get('evaluation.grain')}\n"
        f"rolling    : {settings.get('evaluation.rolling_window')} period(s) "
        f"for {', '.join(settings.get('evaluation.rolling_levels', []))}\n"
        f"confidence : {settings.get('evaluation.confidence')}\n"
        f"periods    : {periods[0] if periods else '-'} → {periods[-1] if periods else '-'}",
        language="text",
    )
