"""Shared plumbing for the Streamlit pages.

Keeps three things in one place: how a page gets a database session, how it
learns who is looking at it, and how numbers are rendered.  Pages that each
invent their own answer to those questions drift apart within a month.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterable, Sequence

import pandas as pd
import streamlit as st

from qmis.analytics.repository import (
    available_periods,
    latest_period,
    load_alerts,
    load_entities,
    load_history,
    load_scores,
)
from qmis.auth.rbac import (
    ANONYMOUS,
    Principal,
    resolve_principal,
    scope_frame,
    visible_entity_ids,
)
from qmis.core.config import load_settings
from qmis.core.db import init_db, session_scope
from qmis.core.metric_config import MetricRegistry, get_registry
from qmis.core.models import GREEN, ORANGE, RED, YELLOW

SEVERITY_COLOURS = {
    RED: "#c0392b",
    ORANGE: "#e08c1a",
    YELLOW: "#d4b106",
    GREEN: "#2e8b57",
}
SEVERITY_BADGE = {RED: "🔴 Critical", ORANGE: "🟠 High", YELLOW: "🟡 Warning", GREEN: "🟢 Healthy"}
SEVERITY_ORDER_UI = [RED, ORANGE, YELLOW, GREEN]

BAND_COLOURS = {
    "Excellent": "#2e8b57",
    "Healthy": "#5aa469",
    "Attention required": "#e08c1a",
    "Critical": "#c0392b",
}


# --------------------------------------------------------------------------- #
# bootstrap
# --------------------------------------------------------------------------- #
@st.cache_resource(show_spinner=False)
def bootstrap():
    """Create the schema once per server process."""
    settings = load_settings()
    init_db(settings.get("database.url"))
    return settings


def registry() -> MetricRegistry:
    return get_registry()


@contextmanager
def db():
    with session_scope() as session:
        yield session


def current_principal() -> Principal:
    """Who is looking at this page.

    Identity comes from the reverse proxy / identity provider in front of the
    app.  ``demo`` mode exists so the dashboard can be evaluated before that is
    wired up, and says so loudly in the sidebar.
    """
    settings = load_settings()
    mode = str(settings.get("auth.mode", "header"))
    email = None
    try:
        header_name = str(settings.get("auth.header_name", "X-Forwarded-Email"))
        headers = st.context.headers or {}
        email = headers.get(header_name) or headers.get(header_name.lower())
    except Exception:  # pragma: no cover - older Streamlit without st.context
        email = None
    if "impersonate_email" in st.session_state and st.session_state["impersonate_email"]:
        email = st.session_state["impersonate_email"]
    fallback = str(settings.get("auth.demo_role", "admin")) if mode == "demo" else None
    with db() as session:
        return resolve_principal(session, email, fallback_role=fallback)


def scope_ids(principal: Principal) -> list[int] | None:
    with db() as session:
        return visible_entity_ids(session, principal)


# --------------------------------------------------------------------------- #
# cached reads
# --------------------------------------------------------------------------- #
@st.cache_data(ttl=120, show_spinner=False)
def cached_periods() -> list[str]:
    with db() as session:
        return available_periods(session)


@st.cache_data(ttl=120, show_spinner=False)
def cached_latest_period() -> str | None:
    with db() as session:
        return latest_period(session)


@st.cache_data(ttl=120, show_spinner=False)
def cached_alerts(period_key: str, include_improvements: bool = True) -> pd.DataFrame:
    with db() as session:
        return load_alerts(session, period_key=period_key, include_improvements=include_improvements)


@st.cache_data(ttl=120, show_spinner=False)
def cached_scores(period_keys: tuple[str, ...] | None = None) -> pd.DataFrame:
    with db() as session:
        return load_scores(session, period_keys=list(period_keys) if period_keys else None)


@st.cache_data(ttl=120, show_spinner=False)
def cached_entities(entity_type: str | None = None) -> pd.DataFrame:
    with db() as session:
        return load_entities(session, entity_type)


@st.cache_data(ttl=120, show_spinner=False)
def cached_history(
    metric_keys: tuple[str, ...] | None = None,
    entity_ids: tuple[int, ...] | None = None,
) -> pd.DataFrame:
    with db() as session:
        return load_history(
            session,
            metric_keys=list(metric_keys) if metric_keys else None,
            entity_ids=list(entity_ids) if entity_ids else None,
        )


def clear_caches() -> None:
    st.cache_data.clear()


# --------------------------------------------------------------------------- #
# presentation
# --------------------------------------------------------------------------- #
def metric_name(key: str) -> str:
    reg = registry()
    if key in reg:
        return reg[key].name
    if key.endswith(":composite"):
        return key.split(":")[0].replace("_", " ").title() + " (grouped)"
    return key


def format_value(metric_key: str, value) -> str:
    reg = registry()
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "-"
    if metric_key in reg:
        return reg[metric_key].format(float(value))
    return f"{value:,.2f}"


def format_change(metric_key: str, value) -> str:
    reg = registry()
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "-"
    if metric_key in reg:
        return reg[metric_key].format_change(float(value))
    return f"{value:+,.2f}"


def severity_chip(severity: str) -> str:
    return SEVERITY_BADGE.get(severity, severity)


def period_selector(label: str = "Reporting period", key: str = "period") -> str | None:
    periods = cached_periods()
    if not periods:
        return None
    default = len(periods) - 1
    chosen = st.selectbox(label, periods[::-1], index=0, key=key)
    return chosen


def require(principal: Principal, permission: str) -> bool:
    """Render a clear refusal instead of a stack trace."""
    if principal.can(permission):
        return True
    st.warning(
        f"Your role ({principal.role or 'unauthenticated'}) does not include "
        f"'{permission.replace('_', ' ')}'. Ask an administrator if you need it."
    )
    return False


def empty_state(message: str, hint: str = "") -> None:
    st.info(message + (f"\n\n{hint}" if hint else ""))


def sidebar_identity(principal: Principal) -> None:
    settings = load_settings()
    with st.sidebar:
        st.caption("Signed in as")
        st.markdown(f"**{principal.name or 'Unknown'}**  \n`{principal.role or 'no role'}`")
        if principal.is_scoped:
            st.caption("You see your own team only.")
        if str(settings.get("auth.mode", "header")) == "demo":
            st.warning("Demo authentication is on. Put the app behind your identity provider before real use.", icon="⚠️")
        st.divider()


def alert_table(frame: pd.DataFrame, limit: int = 50) -> pd.DataFrame:
    """Shape an alert frame for display."""
    if frame.empty:
        return frame
    view = frame.head(limit).copy()
    view["Metric"] = view["metric_key"].map(metric_name)
    view["Previous"] = [format_value(k, v) for k, v in zip(view["metric_key"], view["previous_value"])]
    view["Current"] = [format_value(k, v) for k, v in zip(view["metric_key"], view["current_value"])]
    view["Change"] = [format_change(k, v) for k, v in zip(view["metric_key"], view["delta"])]
    view["Severity"] = view["severity"].map(severity_chip)
    view["Basis"] = view["basis"].fillna("period").replace({"period": "single period"})
    columns = {
        "Severity": "Severity",
        "entity_name": "Who",
        "entity_type": "Level",
        "owner_name": "Owner",
        "Metric": "Metric",
        "Previous": "Previous",
        "Current": "Current",
        "Change": "Change",
        "kind": "Trigger",
        "Basis": "Basis",
    }
    return view[list(columns)].rename(columns=columns)
