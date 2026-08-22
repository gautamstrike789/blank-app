"""QMIS - Quality Metrics Intelligence & Alert System.

Entry point.  Run with:

    uv run streamlit run streamlit_app.py

Navigation is built from the signed-in user's permissions, so an Owner never
sees an admin page.  That is a convenience, not the security boundary - the
boundary is in ``qmis.auth.rbac.visible_entity_ids``, which scopes every query.
"""

from __future__ import annotations

import streamlit as st

st.set_page_config(
    page_title="Quality Intelligence",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

from qmis.app.common import bootstrap, current_principal, sidebar_identity  # noqa: E402
from qmis.app.pages import admin, alerts, associates, data, executive, owners, trends  # noqa: E402
from qmis.auth.rbac import (  # noqa: E402
    EDIT_METRICS,
    MANAGE_USERS,
    UPLOAD_FILES,
    VIEW_ALL_OWNERS,
)

bootstrap()
principal = current_principal()

if not principal.role:
    st.title("Quality Intelligence")
    st.error(
        "You are not signed in, or your account has not been given a role yet.\n\n"
        "This app expects to sit behind your organisation's identity provider, which passes "
        "the verified email address in a request header. For a local trial, set "
        "`auth.mode: demo` in `qmis/config/settings.yaml`."
    )
    st.stop()

sidebar_identity(principal)

def _page(module, title: str, icon: str, path: str, default: bool = False) -> st.Page:
    """Bind a page module to the signed-in principal.

    ``url_path`` has to be given explicitly: every page here is the same
    closure, so Streamlit would otherwise derive the pathname from the
    callable's name and give all of them "<lambda>".
    """
    return st.Page(
        lambda: module.render(principal),
        title=title,
        icon=icon,
        url_path=path,
        default=default,
    )


pages = [
    _page(executive, "Executive", "📊", "executive", default=True),
    _page(alerts, "Alert centre", "🚨", "alerts"),
]
if principal.can(VIEW_ALL_OWNERS) or principal.owner_entity_id:
    pages.append(_page(owners, "Owners", "👥", "owners"))
pages.append(_page(associates, "Business associates", "🧑‍💼", "associates"))
pages.append(_page(trends, "Trends", "📈", "trends"))
if principal.can(UPLOAD_FILES):
    pages.append(_page(data, "Data & uploads", "📂", "data"))
if principal.can(EDIT_METRICS) or principal.can(MANAGE_USERS):
    pages.append(_page(admin, "Administration", "⚙️", "admin"))

st.navigation(pages).run()
