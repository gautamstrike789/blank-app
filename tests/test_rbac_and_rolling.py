"""Access scoping and the rolling-window machinery."""

from __future__ import annotations

import pandas as pd
import pytest

from qmis.analytics.repository import load_history
from qmis.analytics.rolling import apply_rolling
from qmis.auth.rbac import (
    ANONYMOUS,
    EDIT_METRICS,
    MANAGE_USERS,
    UPLOAD_FILES,
    VIEW_ALL_OWNERS,
    Principal,
    ensure_seed_admin,
    link_owner_users,
    resolve_principal,
    scope_frame,
    visible_entity_ids,
)
from qmis.core.models import User
from qmis.core.periods import Period
from qmis.ingest.pipeline import ingest_file
from tests.conftest import BASE_COLUMNS, make_row

PERIOD = Period.from_key("2026-W33")


# --------------------------------------------------------------------------- #
# RBAC
# --------------------------------------------------------------------------- #
def test_role_permissions_are_distinct():
    admin = Principal("a@x.com", "A", "admin")
    management = Principal("m@x.com", "M", "management")
    owner = Principal("o@x.com", "O", "owner", owner_entity_id=1)

    assert admin.can(EDIT_METRICS) and admin.can(MANAGE_USERS) and admin.can(UPLOAD_FILES)
    assert management.can(VIEW_ALL_OWNERS) and not management.can(EDIT_METRICS)
    assert not owner.can(VIEW_ALL_OWNERS) and not owner.can(UPLOAD_FILES)
    assert owner.is_scoped and not management.is_scoped


def test_unknown_user_gets_nothing(session):
    principal = resolve_principal(session, "stranger@x.com")
    assert principal is ANONYMOUS
    assert not principal.can(VIEW_ALL_OWNERS)


def test_require_raises_rather_than_silently_allowing():
    with pytest.raises(PermissionError):
        Principal("o@x.com", "O", "owner").require(EDIT_METRICS)


@pytest.fixture()
def two_owners(session, tmp_path):
    rows = [
        make_row("2026-W33", "Owner A", "BA 1", submissions=300),
        make_row("2026-W33", "Owner A", "BA 2", submissions=300),
        make_row("2026-W33", "Owner B", "BA 3", submissions=300),
    ]
    path = tmp_path / "r.xlsx"
    pd.DataFrame(rows, columns=BASE_COLUMNS).to_excel(path, sheet_name="BA", index=False)
    assert ingest_file(session, path).accepted
    session.add(User(email="a@x.com", name="Owner A", role="owner", active=True))
    session.add(User(email="b@x.com", name="Owner B", role="owner", active=True))
    session.flush()
    assert link_owner_users(session) == 2
    return session


def test_owner_scope_covers_their_own_team_only(two_owners):
    session = two_owners
    from sqlalchemy import select

    from qmis.core.models import Entity

    principal = resolve_principal(session, "a@x.com")
    ids = visible_entity_ids(session, principal)
    names = {
        name
        for (name,) in session.execute(select(Entity.name).where(Entity.id.in_(ids)))
    }
    # The Owner entity itself plus its BAs - and nothing above or beside it.
    assert names == {"Owner A", "BA 1", "BA 2"}
    assert "Organisation" not in names and "Owner B" not in names

    # And the scope actually filters the data, not just the entity list.
    visible = scope_frame(load_history(session), ids)
    assert set(visible["entity_name"]) == {"BA 1", "BA 2"}


def test_management_scope_is_unrestricted(two_owners):
    session = two_owners
    session.add(User(email="m@x.com", name="M", role="management", active=True))
    session.flush()
    principal = resolve_principal(session, "m@x.com")
    assert visible_entity_ids(session, principal) is None
    history = load_history(session)
    assert len(scope_frame(history, None)) == len(history)


def test_an_owner_without_a_linked_entity_sees_nothing(session):
    session.add(User(email="c@x.com", name="Unlinked", role="owner", active=True))
    session.flush()
    principal = resolve_principal(session, "c@x.com")
    assert visible_entity_ids(session, principal) == []


def test_seed_admin_is_idempotent(session):
    first = ensure_seed_admin(session, "boss@x.com")
    second = ensure_seed_admin(session, "boss@x.com")
    assert first.id == second.id and first.role == "admin"


# --------------------------------------------------------------------------- #
# rolling window
# --------------------------------------------------------------------------- #
def rolling_history(submissions, debit1):
    rows = []
    for offset in range(len(submissions)):
        period = PERIOD.shift(-offset).key
        rows.append(dict(
            entity_id=1, entity_type="ba", entity_name="BA 1", owner_id=9, owner_name="Owner A",
            period_key=period, grain="weekly", metric_key="submissions",
            value=float(submissions[offset]), denominator=None))
        rows.append(dict(
            entity_id=1, entity_type="ba", entity_name="BA 1", owner_id=9, owner_name="Owner A",
            period_key=period, grain="weekly", metric_key="debit1_count",
            value=float(debit1[offset]), denominator=None))
        rows.append(dict(
            entity_id=1, entity_type="ba", entity_name="BA 1", owner_id=9, owner_name="Owner A",
            period_key=period, grain="weekly", metric_key="d1_pct",
            value=debit1[offset] / submissions[offset] * 100, denominator=float(submissions[offset])))
    return pd.DataFrame(rows)


def test_rolling_sums_counts_and_recomputes_the_rate(registry):
    # offset 0 is the newest, so the trailing window at 2026-W33 is [4,4,4,4].
    frame = rolling_history([4, 4, 4, 4], [4, 3, 4, 3])
    rolled = apply_rolling(frame, registry, 4, entity_types=("ba",))
    current = rolled.loc[
        (rolled["period_key"] == PERIOD.key) & (rolled["metric_key"] == "d1_pct")
    ].iloc[0]
    assert current["denominator"] == 16
    assert current["value"] == pytest.approx(14 / 16 * 100)


def test_rolling_never_averages_rates(registry):
    """100% on 1 submission and 50% on 100 must not average to 75%."""
    frame = rolling_history([1, 100], [1, 50])
    rolled = apply_rolling(frame, registry, 2, entity_types=("ba",))
    current = rolled.loc[
        (rolled["period_key"] == PERIOD.key) & (rolled["metric_key"] == "d1_pct")
    ].iloc[0]
    assert current["value"] == pytest.approx(51 / 101 * 100)


def test_rolling_fills_a_period_the_ba_did_not_report(registry):
    frame = rolling_history([4, 4, 4, 4], [4, 3, 4, 3])
    gap = PERIOD.shift(-1).key
    frame = frame.loc[~((frame["period_key"] == gap) & (frame["metric_key"] == "d1_pct"))]
    rolled = apply_rolling(frame, registry, 4, entity_types=("ba",))
    keys = set(rolled.loc[rolled["metric_key"] == "d1_pct", "period_key"])
    # The window still spans four weeks of counts, so the rate exists for the
    # week whose own rate row was missing.
    assert gap in keys


def test_rolling_leaves_other_levels_untouched(registry):
    frame = rolling_history([4, 4], [4, 3])
    frame.loc[frame.index, "entity_type"] = "owner"
    rolled = apply_rolling(frame, registry, 2, entity_types=("ba",))
    assert len(rolled) == len(frame)


def test_a_window_of_one_is_a_no_op(registry):
    frame = rolling_history([4, 4], [4, 3])
    assert apply_rolling(frame, registry, 1, entity_types=("ba",)) is frame
