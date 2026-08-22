import datetime as dt

import pytest

from qmis.core.periods import MONTHLY, WEEKLY, Period, PeriodError, sort_keys


def test_week_key_round_trip():
    period = Period.from_key("2026-W33")
    assert (period.year, period.index, period.grain) == (2026, 33, WEEKLY)
    assert period.key == "2026-W33"


def test_week_boundaries_are_iso():
    start, end = Period.from_key("2026-W33").date_range()
    assert start == dt.date(2026, 8, 10) and end == dt.date(2026, 8, 16)
    assert start.isoweekday() == 1


def test_previous_crosses_the_year_correctly():
    # 2020 had 53 ISO weeks; naive year-1 arithmetic lands on a week that
    # does not exist and silently loses a period of history.
    assert Period.from_key("2021-W01").previous.key == "2020-W53"


def test_month_shift_crosses_the_year():
    assert Period.from_year_month(2026, "Jan").shift(-1).key == "2025-12"
    assert Period.from_key("2025-12").shift(2).key == "2026-02"


def test_trailing_window_is_ordered_and_excludes_self():
    trailing = Period.from_key("2026-W33").trailing(4)
    assert [p.key for p in trailing] == ["2026-W29", "2026-W30", "2026-W31", "2026-W32"]


def test_distance_is_signed():
    assert Period.from_key("2026-W33").distance(Period.from_key("2026-W30")) == 3
    assert Period.from_key("2026-W30").distance(Period.from_key("2026-W33")) == -3


def test_sorting_is_chronological_not_lexical():
    assert sort_keys(["2026-W10", "2026-W09", "2026-W2"]) == ["2026-W02", "2026-W09", "2026-W10"]


def test_bad_keys_are_rejected():
    for bad in ("2026-W54", "not-a-period", "2026-13"):
        with pytest.raises(PeriodError):
            Period.from_key(bad)


def test_grains_cannot_be_compared():
    with pytest.raises(PeriodError):
        Period.from_key("2026-W33").distance(Period.from_key("2026-08"))
