"""Role-based access control.

Three roles, and one rule that matters: an Owner sees their own BAs and nothing
else.  That is enforced by scoping the *query*, not by hiding widgets - a view
filter that a URL parameter can defeat is not access control.

Authentication itself is deliberately delegated.  The app expects to sit behind
the organisation's existing identity provider (Azure AD, Google Workspace,
Cloudflare Access, an nginx auth proxy) which passes the verified email in a
header.  Building a password store here would add a credential-handling
liability to a reporting tool for no gain.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from qmis.core.models import BA, ORG, OWNER, TEAM, Entity, User

# Permissions ---------------------------------------------------------------
VIEW_ORG = "view_org"
VIEW_ALL_OWNERS = "view_all_owners"
VIEW_OWN_TEAM = "view_own_team"
UPLOAD_FILES = "upload_files"
EDIT_METRICS = "edit_metrics"
MANAGE_USERS = "manage_users"
ACKNOWLEDGE_ALERTS = "acknowledge_alerts"
SEND_NOTIFICATIONS = "send_notifications"

ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "admin": frozenset(
        {
            VIEW_ORG, VIEW_ALL_OWNERS, VIEW_OWN_TEAM, UPLOAD_FILES, EDIT_METRICS,
            MANAGE_USERS, ACKNOWLEDGE_ALERTS, SEND_NOTIFICATIONS,
        }
    ),
    "management": frozenset({VIEW_ORG, VIEW_ALL_OWNERS, VIEW_OWN_TEAM, ACKNOWLEDGE_ALERTS}),
    "owner": frozenset({VIEW_OWN_TEAM, ACKNOWLEDGE_ALERTS}),
}


@dataclass(frozen=True)
class Principal:
    """The authenticated user and the scope they may see."""

    email: str
    name: str
    role: str
    owner_entity_id: int | None = None

    @property
    def permissions(self) -> frozenset[str]:
        return ROLE_PERMISSIONS.get(self.role, frozenset())

    def can(self, permission: str) -> bool:
        return permission in self.permissions

    def require(self, permission: str) -> None:
        if not self.can(permission):
            raise PermissionError(
                f"{self.email} ({self.role}) is not allowed to {permission.replace('_', ' ')}"
            )

    @property
    def is_scoped(self) -> bool:
        """True when this principal only sees part of the organisation."""
        return not self.can(VIEW_ALL_OWNERS)


ANONYMOUS = Principal(email="", name="Not signed in", role="", owner_entity_id=None)


def resolve_principal(
    session: Session, email: str | None, fallback_role: str | None = None
) -> Principal:
    """Look up a user, optionally falling back to a demo role."""
    if email:
        user = session.execute(
            select(User).where(User.email == email.strip().lower(), User.active.is_(True))
        ).scalar_one_or_none()
        if user is not None:
            return Principal(user.email, user.name, user.role, user.owner_entity_id)
    if fallback_role:
        return Principal(
            email=email or "demo@local",
            name="Demo user",
            role=fallback_role,
            owner_entity_id=None,
        )
    return ANONYMOUS


def visible_entity_ids(session: Session, principal: Principal) -> list[int] | None:
    """Entity ids this principal may see. ``None`` means "no restriction".

    Every read path passes its result through this, so an Owner cannot reach
    another Owner's BAs by changing a filter, a query string or an export.
    """
    if principal.can(VIEW_ALL_OWNERS):
        return None
    if principal.owner_entity_id is None:
        return []
    ids = {principal.owner_entity_id}
    frontier = [principal.owner_entity_id]
    while frontier:
        rows = session.execute(
            select(Entity.id).where(Entity.parent_id.in_(frontier))
        ).scalars().all()
        new = [i for i in rows if i not in ids]
        ids.update(new)
        frontier = new
    return sorted(ids)


def scope_frame(frame, entity_ids: Sequence[int] | None, column: str = "entity_id"):
    """Apply an entity-id scope to a dataframe, if one applies."""
    if entity_ids is None or frame is None or getattr(frame, "empty", True):
        return frame
    if column not in frame.columns:
        return frame
    return frame.loc[frame[column].isin(list(entity_ids))]


def ensure_seed_admin(session: Session, email: str, name: str = "Administrator") -> User:
    """Create the first admin so a fresh install is usable."""
    existing = session.execute(select(User).where(User.email == email)).scalar_one_or_none()
    if existing:
        return existing
    user = User(email=email.strip().lower(), name=name, role="admin", active=True)
    session.add(user)
    session.flush()
    return user


def link_owner_users(session: Session) -> int:
    """Attach each ``owner`` user to the Owner entity that matches their name.

    Owner accounts are usually created before the first file arrives, so their
    entity does not exist yet; this is run after each load to close the gap.
    """
    linked = 0
    owners = {
        " ".join(e.name.split()).casefold(): e.id
        for e in session.execute(select(Entity).where(Entity.entity_type == OWNER)).scalars()
    }
    for user in session.execute(
        select(User).where(User.role == "owner", User.owner_entity_id.is_(None))
    ).scalars():
        key = " ".join(user.name.split()).casefold()
        if key in owners:
            user.owner_entity_id = owners[key]
            linked += 1
    return linked
