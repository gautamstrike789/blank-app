"""Reporting period handling.

The system stores every fact against a *period* rather than a raw date so that
weekly files, monthly extracts and ad-hoc corrections all live on one timeline.

A period is identified by a ``period_key``:

    weekly   -> ``2026-W33``   (ISO year + ISO week)
    monthly  -> ``2026-08``

Weekly is the primary grain.  Monthly exists because the current Master Report
is published as a month-level pivot; the comparison engine treats both grains
identically (previous period, trailing average, best/worst) so nothing else in
the system needs to know which grain it is looking at.
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass
from typing import Iterable

WEEKLY = "weekly"
MONTHLY = "monthly"
GRAINS = (WEEKLY, MONTHLY)

_WEEK_RE = re.compile(r"^(?P<year>\d{4})-?W(?P<week>\d{1,2})$", re.IGNORECASE)
_MONTH_RE = re.compile(r"^(?P<year>\d{4})-(?P<month>\d{1,2})$")

_MONTH_NAMES = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}


class PeriodError(ValueError):
    """Raised when a period cannot be parsed or built."""


@dataclass(frozen=True, order=True)
class Period:
    """An immutable, orderable reporting period."""

    grain: str
    year: int
    index: int  # ISO week number, or month number

    def __post_init__(self) -> None:
        if self.grain not in GRAINS:
            raise PeriodError(f"unknown grain {self.grain!r}")
        limit = 53 if self.grain == WEEKLY else 12
        if not 1 <= self.index <= limit:
            raise PeriodError(f"{self.grain} index {self.index} out of range 1..{limit}")

    # -- construction -----------------------------------------------------
    @classmethod
    def from_key(cls, key: str) -> "Period":
        key = str(key).strip()
        m = _WEEK_RE.match(key)
        if m:
            return cls(WEEKLY, int(m["year"]), int(m["week"]))
        m = _MONTH_RE.match(key)
        if m:
            return cls(MONTHLY, int(m["year"]), int(m["month"]))
        raise PeriodError(f"cannot parse period key {key!r} (expected 2026-W33 or 2026-08)")

    @classmethod
    def from_date(cls, value: _dt.date | _dt.datetime, grain: str = WEEKLY) -> "Period":
        if isinstance(value, _dt.datetime):
            value = value.date()
        if grain == WEEKLY:
            iso = value.isocalendar()
            return cls(WEEKLY, iso[0], iso[1])
        if grain == MONTHLY:
            return cls(MONTHLY, value.year, value.month)
        raise PeriodError(f"unknown grain {grain!r}")

    @classmethod
    def from_year_month(cls, year: int, month: int | str) -> "Period":
        """Build a monthly period from ``2026`` + ``8`` or ``2026`` + ``"Aug"``."""
        if isinstance(month, str):
            token = month.strip().lower()
            if token.isdigit():
                month_no = int(token)
            else:
                month_no = _MONTH_NAMES.get(token[:9]) or _MONTH_NAMES.get(token[:3])
                if month_no is None:
                    raise PeriodError(f"cannot parse month {month!r}")
        else:
            month_no = int(month)
        return cls(MONTHLY, int(year), month_no)

    # -- representation ---------------------------------------------------
    @property
    def key(self) -> str:
        if self.grain == WEEKLY:
            return f"{self.year}-W{self.index:02d}"
        return f"{self.year}-{self.index:02d}"

    @property
    def label(self) -> str:
        if self.grain == WEEKLY:
            start, end = self.date_range()
            return f"W{self.index:02d} {self.year} ({start:%d %b} - {end:%d %b})"
        return f"{_dt.date(self.year, self.index, 1):%b %Y}"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.key

    # -- arithmetic -------------------------------------------------------
    def date_range(self) -> tuple[_dt.date, _dt.date]:
        """Inclusive first/last calendar date covered by the period."""
        if self.grain == WEEKLY:
            start = _dt.date.fromisocalendar(self.year, self.index, 1)
            return start, start + _dt.timedelta(days=6)
        start = _dt.date(self.year, self.index, 1)
        if self.index == 12:
            end = _dt.date(self.year, 12, 31)
        else:
            end = _dt.date(self.year, self.index + 1, 1) - _dt.timedelta(days=1)
        return start, end

    def shift(self, n: int) -> "Period":
        """Return the period ``n`` steps away (negative = earlier)."""
        if self.grain == MONTHLY:
            total = (self.year * 12 + (self.index - 1)) + n
            return Period(MONTHLY, total // 12, total % 12 + 1)
        anchor = _dt.date.fromisocalendar(self.year, self.index, 1)
        return Period.from_date(anchor + _dt.timedelta(weeks=n), WEEKLY)

    @property
    def previous(self) -> "Period":
        return self.shift(-1)

    @property
    def next(self) -> "Period":
        return self.shift(1)

    def trailing(self, n: int, include_self: bool = False) -> list["Period"]:
        """The ``n`` periods immediately before this one (oldest first)."""
        if n < 0:
            raise PeriodError("trailing window must be >= 0")
        offset = 0 if include_self else 1
        return [self.shift(-(i + offset)) for i in range(n)][::-1]

    def distance(self, other: "Period") -> int:
        """Signed number of steps from ``other`` to ``self``."""
        if self.grain != other.grain:
            raise PeriodError("cannot measure distance across grains")
        if self.grain == MONTHLY:
            return (self.year * 12 + self.index) - (other.year * 12 + other.index)
        a = _dt.date.fromisocalendar(self.year, self.index, 1)
        b = _dt.date.fromisocalendar(other.year, other.index, 1)
        return (a - b).days // 7


def sort_keys(keys: Iterable[str]) -> list[str]:
    """Chronologically sort period keys (works within a single grain)."""
    return [p.key for p in sorted(Period.from_key(k) for k in keys)]


def parse_month_name(token: str) -> int | None:
    """Best-effort month-name lookup used by the pivot reader."""
    if token is None:
        return None
    key = str(token).strip().lower()
    return _MONTH_NAMES.get(key) or _MONTH_NAMES.get(key[:3])
