"""The real Master Report workbook.

Skipped unless ``QMIS_MASTER_REPORT`` points at the file, because the workbook
is 28 MB of Power Pivot cache and does not belong in the repository.  It is the
only test that proves the reader copes with an OLAP PivotTable export - filter
block above the header, month columns instead of a period column, and
``2024 Total`` subtotal rows that would double-count every metric.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from qmis.analytics.engine import evaluate_period
from qmis.core.metric_config import get_registry
from qmis.core.periods import MONTHLY
from qmis.ingest.pipeline import ingest_file
from qmis.ingest.readers import detect_layouts, pick_best_sheet, read_workbook

MASTER = os.environ.get("QMIS_MASTER_REPORT", "")
pytestmark = pytest.mark.skipif(
    not MASTER or not Path(MASTER).exists(),
    reason="set QMIS_MASTER_REPORT to the Master Report workbook to run these",
)


@pytest.fixture(scope="module")
def parsed():
    registry = get_registry()
    return pick_best_sheet(read_workbook(MASTER, registry))


def test_the_header_row_is_found_below_the_pivot_filter_block(parsed):
    # The header is on row 12; assuming row 1 would read the filter list.
    assert parsed.layout.header_row > 1
    assert len(parsed.layout.metric_columns) >= 15


def test_pivot_subtotal_rows_are_dropped(parsed):
    assert parsed.dropped_total_rows >= 3
    assert "Total" not in " ".join(str(k) for k in parsed.frame["period_key"].unique())


def test_the_monthly_grain_is_detected(parsed):
    assert parsed.layout.grain == MONTHLY
    keys = sorted(parsed.frame["period_key"].unique())
    assert keys[0].startswith("202") and "-" in keys[0]
    assert len(keys) > 24  # more than two years of history


def test_the_headline_metrics_are_recognised(parsed):
    found = set(parsed.frame["metric_key"])
    assert {"d1_pct", "d3_pct", "rjbd1_pct", "net_loss_pct", "submissions"} <= found


def test_it_loads_and_is_flagged_as_organisation_level_only(session):
    """The export carries no Owner/BA columns - that has to be said, not hidden."""
    result = ingest_file(session, MASTER, uploaded_by="test")
    assert result.accepted, result.message
    assert result.entity_level == "org"
    assert any(f.code == "org_level_only" for f in result.report.warnings)
    assert result.facts_loaded > 400


def test_percentages_land_on_the_0_to_100_scale(session):
    from sqlalchemy import select

    from qmis.core.models import Fact

    assert ingest_file(session, MASTER, uploaded_by="test").accepted
    values = session.execute(
        select(Fact.value).where(Fact.metric_key == "d1_pct", Fact.is_current.is_(True))
    ).scalars().all()
    assert values and max(values) <= 100.0
    # The organisation runs at roughly 88% Debit 1.
    assert 60.0 <= max(values) <= 95.0


def test_immature_debit_3_does_not_produce_a_false_critical(session):
    """The two most recent months read 0% Debit 3 because those donors have
    not reached a third debit yet. Alerting on that would be a guaranteed
    weekly false alarm."""
    assert ingest_file(session, MASTER, uploaded_by="test").accepted
    result = evaluate_period(session, rolling_window=1)
    d3 = result.assessments.loc[result.assessments["metric_key"] == "d3_pct"]
    assert not d3.empty
    assert d3.iloc[0]["suppressed"] == "immature"
    assert not bool(d3.iloc[0]["alertable"])
