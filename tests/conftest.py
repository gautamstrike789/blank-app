"""Shared fixtures.

Every test runs against a real (in-file, throwaway) database rather than mocks:
the ingestion and superseding logic is largely SQL behaviour, and mocking it
would test the mock.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pandas as pd
import pytest

from qmis.core.db import get_session_factory, init_db, reset_engine
from qmis.core.metric_config import MetricRegistry, get_registry
from qmis.core.periods import Period


@pytest.fixture()
def registry() -> MetricRegistry:
    return get_registry(reload=True)


@pytest.fixture()
def session(tmp_path):
    reset_engine()
    init_db(f"sqlite:///{tmp_path / 'test.db'}")
    factory = get_session_factory()
    db = factory()
    try:
        yield db
        db.commit()
    finally:
        db.close()
        reset_engine()


BASE_COLUMNS = [
    "Week", "OWNER NAME", "BAName",
    "Sum of SUBMISSION", "Rejects Before Debit 1", "Rejects Before Debit 1%",
    "Sum of debit1", "D1%", "Sum of DEBIT3", "D3%", "Net Loss", "Net Loss%",
    "Average of Donamt",
]


def make_row(
    week: str,
    owner: str,
    ba: str,
    submissions: int = 100,
    d1: float = 88.0,
    rjbd1: float = 11.0,
    d3: float = 70.0,
    net_loss: float = 20.0,
    donamt: float = 765.0,
) -> dict:
    """One BA-week, with counts consistent with the rates (as the export is)."""
    debit1 = round(submissions * d1 / 100)
    rejects = round(submissions * rjbd1 / 100)
    debit3 = round(submissions * d3 / 100)
    loss = round(submissions * net_loss / 100)
    return {
        "Week": week,
        "OWNER NAME": owner,
        "BAName": ba,
        "Sum of SUBMISSION": submissions,
        "Rejects Before Debit 1": rejects,
        "Rejects Before Debit 1%": rejects / submissions,
        "Sum of debit1": debit1,
        "D1%": debit1 / submissions,
        "Sum of DEBIT3": debit3,
        "D3%": debit3 / submissions,
        "Net Loss": loss,
        "Net Loss%": loss / submissions,
        "Average of Donamt": donamt,
    }


@pytest.fixture()
def write_workbook(tmp_path):
    """Write rows to an .xlsx and return its path."""

    def _write(rows: list[dict], name: str = "report.xlsx", sheet: str = "BA Quality") -> Path:
        path = tmp_path / name
        pd.DataFrame(rows, columns=BASE_COLUMNS).to_excel(path, sheet_name=sheet, index=False)
        return path

    return _write


@pytest.fixture()
def weeks() -> list[str]:
    end = Period.from_key("2026-W33")
    return [end.shift(-(9 - i)).key for i in range(10)]
