"""End-to-end: files in a folder become alerts, scores and digests."""

from __future__ import annotations

import pandas as pd
import pytest
from sqlalchemy import func, select

from qmis.analytics.engine import evaluate_period
from qmis.analytics.repository import load_alerts, load_scores
from qmis.core.config import load_settings
from qmis.core.models import ORG, OWNER, Alert, Entity, Fact, User
from qmis.core.periods import Period
from qmis.ingest.pipeline import ingest_file, ingest_from_storage
from qmis.ingest.storage import InMemoryStorage
from qmis.ingest.watcher import run_cycle
from qmis.notify import ConsoleNotifier, build_digests, dispatch, select_notifiable
from tests.conftest import BASE_COLUMNS, make_row

WEEKS = [Period.from_key("2026-W33").shift(-(7 - i)).key for i in range(8)]


def steady_rows(week: str, d1: float = 88.0, rjbd1: float = 11.0) -> list[dict]:
    """Two owners, two BAs each, all healthy and all with real volume."""
    rows = []
    for owner, bas in (("Owner A", ["BA 1", "BA 2"]), ("Owner B", ["BA 3", "BA 4"])):
        for ba in bas:
            rows.append(make_row(week, owner, ba, submissions=400, d1=d1, rjbd1=rjbd1))
    return rows


@pytest.fixture()
def loaded(session, tmp_path):
    """Eight steady weeks, then a ninth where one BA collapses."""
    for i, week in enumerate(WEEKS):
        rows = steady_rows(week)
        if i == len(WEEKS) - 1:
            rows[0] = make_row(week, "Owner A", "BA 1", submissions=400, d1=76.0, rjbd1=11.0)
        path = tmp_path / f"week_{i}.xlsx"
        pd.DataFrame(rows, columns=BASE_COLUMNS).to_excel(path, sheet_name="BA", index=False)
        result = ingest_file(session, path)
        assert result.accepted, result.message
    return WEEKS[-1]


def test_the_planted_collapse_is_found_and_explained(session, loaded):
    result = evaluate_period(session, loaded, rolling_window=1)
    alerts = result.alerts
    hit = alerts.loc[(alerts["entity_name"] == "BA 1") & (alerts["metric_key"] == "d1_pct")]
    assert not hit.empty, "the planted Debit 1 collapse was not detected"
    row = hit.iloc[0]
    assert row["severity"] == "RED"
    assert "76.00%" in row["headline"] and "88.00%" in row["headline"]
    assert "critical threshold" in row["explanation"]


def test_untouched_bas_do_not_alert(session, loaded):
    result = evaluate_period(session, loaded, rolling_window=1)
    alerts = result.alerts
    quiet = alerts.loc[alerts["entity_name"].isin(["BA 3", "BA 4"])]
    assert quiet.loc[~quiet["is_improvement"]].empty


def test_owner_rates_are_recomputed_not_averaged(session, loaded):
    """Owner A = (76% on 400) + (88% on 400) = 82%, not the mean of the pair."""
    result = evaluate_period(session, loaded, rolling_window=1)
    row = result.assessments.loc[
        (result.assessments["entity_name"] == "Owner A")
        & (result.assessments["metric_key"] == "d1_pct")
    ].iloc[0]
    assert row["current"] == pytest.approx(82.0, abs=0.3)
    assert row["denominator"] == pytest.approx(800)


def test_org_rollup_covers_every_ba(session, loaded):
    result = evaluate_period(session, loaded, rolling_window=1)
    org = result.assessments.loc[
        (result.assessments["entity_type"] == ORG)
        & (result.assessments["metric_key"] == "submissions")
    ].iloc[0]
    assert org["current"] == pytest.approx(1600)


def test_scores_and_alerts_are_persisted_and_queryable(session, loaded):
    result = evaluate_period(session, loaded, rolling_window=1)
    assert result.run_id is not None
    alerts = load_alerts(session, period_key=loaded)
    scores = load_scores(session, period_keys=[loaded])
    assert not alerts.empty and not scores.empty
    assert set(scores["entity_type"]) >= {"ba", "owner", "org"}


def test_re_evaluating_supersedes_rather_than_duplicating(session, loaded):
    evaluate_period(session, loaded, rolling_window=1)
    first = len(load_alerts(session, period_key=loaded))
    evaluate_period(session, loaded, rolling_window=1)
    second = len(load_alerts(session, period_key=loaded))
    assert first == second
    total = session.execute(
        select(func.count()).select_from(Alert).where(Alert.period_key == loaded)
    ).scalar_one()
    assert total == first * 2  # both runs retained, only one current


def test_a_first_time_alert_is_marked_new(session, loaded):
    """`is_new` is what separates a fresh problem from one already reported."""
    previous = Period.from_key(loaded).previous.key
    evaluate_period(session, previous, rolling_window=1)
    evaluate_period(session, loaded, rolling_window=1)

    # BA 1 was healthy last period, so this week's collapse is new.
    alerts = load_alerts(session, period_key=loaded)
    hit = alerts.loc[(alerts["entity_name"] == "BA 1") & (alerts["metric_key"] == "d1_pct")]
    assert not hit.empty and bool(hit.iloc[0]["is_new"]) is True


def test_notification_routing_scopes_each_owner(session, loaded):
    session.add_all(
        [
            User(email="boss@x.com", name="Boss", role="management", active=True),
            User(email="a@x.com", name="Owner A", role="owner", active=True),
            User(email="b@x.com", name="Owner B", role="owner", active=True),
        ]
    )
    session.flush()
    from qmis.auth.rbac import link_owner_users

    link_owner_users(session)
    evaluate_period(session, loaded, rolling_window=1)

    period = Period.from_key(loaded)
    alerts = load_alerts(session, period_key=loaded, include_improvements=False)
    notifiable = select_notifiable(session, alerts, period, send_severities=("RED", "ORANGE"))
    from qmis.core.metric_config import get_registry

    digests = build_digests(session, notifiable, period, get_registry())
    by_recipient = {d.recipient: d for d in digests}
    assert "boss@x.com" in by_recipient
    if "a@x.com" in by_recipient:
        assert "BA 1" in by_recipient["a@x.com"].body
    # Owner B's team is untouched, so Owner B is not emailed at all.
    assert "b@x.com" not in by_recipient or by_recipient["b@x.com"].alert_count == 0


def test_dispatch_records_what_was_sent(session, loaded):
    session.add(User(email="boss@x.com", name="Boss", role="management", active=True))
    session.flush()
    evaluate_period(session, loaded, rolling_window=1)
    period = Period.from_key(loaded)
    from qmis.core.metric_config import get_registry
    from qmis.core.models import NotificationLog

    alerts = load_alerts(session, period_key=loaded, include_improvements=False)
    notifiable = select_notifiable(session, alerts, period)
    digests = build_digests(session, notifiable, period, get_registry())
    notifier = ConsoleNotifier(stream=type("Sink", (), {"write": lambda self, t: None})())
    dispatch(session, digests, [notifier])
    logged = session.execute(select(func.count()).select_from(NotificationLog)).scalar_one()
    assert logged == len(digests) and notifier.sent


def test_the_watcher_runs_the_whole_pipeline(session, tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    for i, week in enumerate(WEEKS[:5]):
        pd.DataFrame(steady_rows(week), columns=BASE_COLUMNS).to_excel(
            inbox / f"report_{i}.xlsx", sheet_name="BA", index=False
        )
    from qmis.ingest.storage import LocalFolderStorage

    storage = LocalFolderStorage(inbox, tmp_path / "done")
    cycle = run_cycle(session, storage=storage, notify=False)
    assert len(cycle.accepted) == 5 and not cycle.errors
    assert sorted(cycle.evaluated_periods) == sorted(WEEKS[:5])
    assert not list(inbox.glob("*.xlsx"))  # archived after loading
    assert len(list((tmp_path / "done").glob("*.xlsx"))) == 5


def test_derived_owner_facts_are_stored_for_trend_charts(session, loaded):
    evaluate_period(session, loaded, rolling_window=1)
    derived = session.execute(
        select(func.count())
        .select_from(Fact)
        .where(Fact.is_derived.is_(True), Fact.is_current.is_(True))
    ).scalar_one()
    assert derived > 0
