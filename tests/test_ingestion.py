"""Ingestion: reading, validating, storing, and refusing."""

from __future__ import annotations

import pandas as pd
import pytest
from sqlalchemy import func, select

from qmis.core.models import BA, ORG, OWNER, UPLOAD_LOADED, UPLOAD_REJECTED, Entity, Fact, Upload
from qmis.ingest.pipeline import ingest_file
from qmis.ingest.readers import infer_period_from_filename, to_number
from tests.conftest import BASE_COLUMNS, make_row


def test_percentages_are_stored_as_0_to_100(session, write_workbook, registry):
    path = write_workbook([make_row("2026-W33", "Owner A", "BA 1", d1=88.0)])
    result = ingest_file(session, path)
    assert result.accepted, result.message
    value = session.execute(
        select(Fact.value).where(Fact.metric_key == "d1_pct", Fact.is_current.is_(True))
    ).scalar_one()
    assert value == pytest.approx(88.0, abs=0.01)


def test_entity_hierarchy_is_built(session, write_workbook):
    path = write_workbook(
        [
            make_row("2026-W33", "Owner A", "BA 1"),
            make_row("2026-W33", "Owner A", "BA 2"),
            make_row("2026-W33", "Owner B", "BA 3"),
        ]
    )
    assert ingest_file(session, path).accepted
    owners = session.execute(select(Entity).where(Entity.entity_type == OWNER)).scalars().all()
    bas = session.execute(select(Entity).where(Entity.entity_type == BA)).scalars().all()
    assert {o.name for o in owners} == {"Owner A", "Owner B"}
    assert len(bas) == 3
    owner_a = next(o for o in owners if o.name == "Owner A")
    assert {b.name for b in bas if b.parent_id == owner_a.id} == {"BA 1", "BA 2"}


def test_denominator_travels_with_each_rate(session, write_workbook):
    path = write_workbook([make_row("2026-W33", "Owner A", "BA 1", submissions=137)])
    assert ingest_file(session, path).accepted
    denominator = session.execute(
        select(Fact.denominator).where(Fact.metric_key == "d1_pct", Fact.is_current.is_(True))
    ).scalar_one()
    assert denominator == 137


def test_identical_file_is_refused(session, write_workbook):
    path = write_workbook([make_row("2026-W33", "Owner A", "BA 1")])
    assert ingest_file(session, path).accepted
    second = ingest_file(session, path)
    assert not second.accepted
    assert any(f.code == "duplicate_file" for f in second.report.errors)


def test_same_period_from_a_different_file_is_refused(session, write_workbook):
    first = write_workbook([make_row("2026-W33", "Owner A", "BA 1", d1=88)], name="a.xlsx")
    second = write_workbook([make_row("2026-W33", "Owner A", "BA 1", d1=77)], name="b.xlsx")
    assert ingest_file(session, first).accepted
    result = ingest_file(session, second)
    assert not result.accepted
    assert any(f.code == "duplicate_period" for f in result.report.errors)


def test_reprocessing_supersedes_without_deleting(session, write_workbook):
    first = write_workbook([make_row("2026-W33", "Owner A", "BA 1", d1=88)], name="a.xlsx")
    second = write_workbook([make_row("2026-W33", "Owner A", "BA 1", d1=77)], name="b.xlsx")
    assert ingest_file(session, first).accepted
    assert ingest_file(session, second, allow_reprocess=True).accepted

    current = session.execute(
        select(Fact.value).where(Fact.metric_key == "d1_pct", Fact.is_current.is_(True))
    ).scalars().all()
    superseded = session.execute(
        select(Fact.value).where(Fact.metric_key == "d1_pct", Fact.is_current.is_(False))
    ).scalars().all()
    assert current == pytest.approx([77.0], abs=0.01)
    assert superseded == pytest.approx([88.0], abs=0.01)  # history is kept


def test_missing_column_blocks_the_load(session, write_workbook, tmp_path):
    first = write_workbook([make_row("2026-W33", "Owner A", "BA 1")], name="a.xlsx")
    assert ingest_file(session, first).accepted

    rows = [make_row("2026-W34", "Owner A", "BA 1")]
    frame = pd.DataFrame(rows, columns=BASE_COLUMNS).drop(columns=["D3%", "Sum of DEBIT3"])
    path = tmp_path / "b.xlsx"
    frame.to_excel(path, sheet_name="BA Quality", index=False)

    result = ingest_file(session, path)
    assert not result.accepted
    error = next(f for f in result.report.errors if f.code == "missing_column")
    assert "Debit 3 %" in error.message
    assert session.execute(
        select(func.count()).select_from(Fact).where(Fact.period_key == "2026-W34")
    ).scalar_one() == 0


def test_unexpected_column_warns_but_loads(session, write_workbook, tmp_path):
    rows = [make_row("2026-W33", "Owner A", "BA 1")]
    frame = pd.DataFrame(rows, columns=BASE_COLUMNS)
    frame["Brand New Metric"] = 1.23
    path = tmp_path / "c.xlsx"
    frame.to_excel(path, sheet_name="BA Quality", index=False)

    result = ingest_file(session, path)
    assert result.accepted
    assert any(f.code == "unexpected_column" for f in result.report.warnings)


def test_percent_scale_switch_is_caught(session, write_workbook, tmp_path):
    rows = [make_row("2026-W33", "Owner A", "BA 1")]
    frame = pd.DataFrame(rows, columns=BASE_COLUMNS)
    for column in ("D1%", "D3%", "Rejects Before Debit 1%", "Net Loss%"):
        frame[column] = frame[column] * 100  # the export switched to 0-100
    path = tmp_path / "d.xlsx"
    frame.to_excel(path, sheet_name="BA Quality", index=False)

    result = ingest_file(session, path)
    assert not result.accepted
    assert any(f.code == "percent_scale" for f in result.report.errors)


def test_duplicate_ba_rows_are_refused(session, write_workbook):
    path = write_workbook(
        [
            make_row("2026-W33", "Owner A", "BA 1", submissions=100),
            make_row("2026-W33", "Owner A", "BA 1", submissions=120),
        ]
    )
    result = ingest_file(session, path)
    assert not result.accepted
    assert any(f.code == "duplicate_rows" for f in result.report.errors)


def test_one_ba_under_two_owners_is_refused(session, write_workbook):
    path = write_workbook(
        [
            make_row("2026-W33", "Owner A", "BA 1"),
            make_row("2026-W33", "Owner B", "BA 1"),
        ]
    )
    result = ingest_file(session, path)
    assert not result.accepted
    assert any(f.code == "ba_multiple_owners" for f in result.report.errors)


def test_negative_count_is_refused(session, write_workbook, tmp_path):
    rows = [make_row("2026-W33", "Owner A", "BA 1")]
    frame = pd.DataFrame(rows, columns=BASE_COLUMNS)
    frame["Sum of debit1"] = -5
    path = tmp_path / "e.xlsx"
    frame.to_excel(path, sheet_name="BA Quality", index=False)
    result = ingest_file(session, path)
    assert not result.accepted
    assert any(f.code == "negative_value" for f in result.report.errors)


def test_ratio_mismatch_warns(session, write_workbook, tmp_path):
    rows = [make_row("2026-W33", "Owner A", "BA 1", submissions=200, d1=88)]
    frame = pd.DataFrame(rows, columns=BASE_COLUMNS)
    frame["D1%"] = 0.55  # stated rate contradicts debit1/submissions
    path = tmp_path / "f.xlsx"
    frame.to_excel(path, sheet_name="BA Quality", index=False)
    result = ingest_file(session, path)
    assert result.accepted
    assert any(f.code == "ratio_mismatch" for f in result.report.warnings)


def test_new_and_departed_bas_are_reported(session, write_workbook):
    first = write_workbook(
        [make_row("2026-W32", "Owner A", "BA 1"), make_row("2026-W32", "Owner A", "BA 2")],
        name="a.xlsx",
    )
    assert ingest_file(session, first).accepted
    second = write_workbook(
        [make_row("2026-W33", "Owner A", "BA 1"), make_row("2026-W33", "Owner A", "BA 3")],
        name="b.xlsx",
    )
    result = ingest_file(session, second)
    assert result.accepted
    codes = {f.code for f in result.report.findings}
    assert "new_ba" in codes and "missing_ba_vs_history" in codes
    # A BA who stops reporting is not deleted.
    assert session.execute(
        select(func.count()).select_from(Entity).where(Entity.name == "BA 2")
    ).scalar_one() == 1


def test_rejected_upload_is_recorded_for_audit(session, write_workbook):
    path = write_workbook(
        [make_row("2026-W33", "Owner A", "BA 1"), make_row("2026-W33", "Owner A", "BA 1")]
    )
    result = ingest_file(session, path)
    assert not result.accepted
    upload = session.get(Upload, result.upload_id)
    assert upload.status == UPLOAD_REJECTED
    assert upload.issues  # the reasons are queryable, not just printed


def test_filename_date_is_used_when_the_sheet_has_no_period(tmp_path):
    assert infer_period_from_filename("Master_Report__140826.xlsx").key == "2026-W33"
    assert infer_period_from_filename("weekly-2026-08-14.xlsx").key == "2026-W33"
    assert infer_period_from_filename("no-date-here.xlsx") is None


def test_a_workbook_is_identified_by_content_not_extension(write_workbook, tmp_path):
    """A file renamed to get past an upload filter must still be readable."""
    import shutil

    from qmis.ingest.readers import detect_workbook_format, excel_engine

    real = write_workbook([make_row("2026-W33", "Owner A", "BA 1")], name="real.xlsx")
    renamed = tmp_path / "mislabelled.xlsb"
    shutil.copy(real, renamed)

    assert detect_workbook_format(renamed) == "xlsx"
    assert excel_engine(renamed) == "openpyxl"


def test_a_mislabelled_workbook_still_ingests(session, write_workbook, tmp_path):
    import shutil

    real = write_workbook([make_row("2026-W33", "Owner A", "BA 1")], name="real.xlsx")
    renamed = tmp_path / "actually_xlsx.xlsb"
    shutil.copy(real, renamed)
    result = ingest_file(session, renamed)
    assert result.accepted, result.message


@pytest.mark.parametrize(
    "raw,expected",
    [("88.6%", 0.886), ("1,234", 1234.0), ("(9.1)", -9.1), ("#DIV/0!", None), ("", None), (None, None)],
)
def test_number_parsing_handles_real_spreadsheet_values(raw, expected):
    null_tokens = {"", "#div/0!", "n/a"}
    result = to_number(raw, null_tokens)
    if expected is None:
        assert result is None
    else:
        assert result == pytest.approx(expected)
