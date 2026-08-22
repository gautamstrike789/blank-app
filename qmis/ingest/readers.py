"""Excel readers.

Two shapes have to be supported and the difference must not leak downstream:

1. **Flat export** - one row per Owner/BA per period, one column per metric.
   This is the shape the weekly quality report should be published in.
2. **Pivot export** - the current ``Master Report`` workbook, whose sheets are
   OLAP PivotTable renderings with a filter block above the header, month
   columns instead of a period column, and ``2024 Total`` subtotal rows.

Both are normalised into the same :class:`SheetLayout` + long-format frame, so
the validation, alerting and dashboard layers never branch on file shape.
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import pandas as pd
import yaml

from qmis.core.metric_config import MetricRegistry, _normalise_alias
from qmis.core.periods import MONTHLY, WEEKLY, Period, PeriodError, parse_month_name

COLUMN_MAP_FILE = Path(__file__).resolve().parent.parent / "config" / "column_map.yaml"

def detect_workbook_format(path: str | Path) -> str:
    """Identify a workbook by its CONTENT rather than its extension.

    .xlsx and .xlsb are both ZIP containers; only the workbook part inside
    differs (XML vs binary). Trusting the extension means a file renamed to get
    past an upload filter, or exported with the wrong suffix, fails with an
    unhelpful parser error instead of simply being read.
    """
    import zipfile

    path = Path(path)
    try:
        with open(path, "rb") as handle:
            magic = handle.read(8)
    except OSError:
        return path.suffix.lower().lstrip(".")
    if magic[:2] != b"PK":
        if magic == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
            return "xls"
        return "text"
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
    except zipfile.BadZipFile:
        return "unknown"
    if any(n.endswith("workbook.bin") for n in names):
        return "xlsb"
    if any(n.endswith("workbook.xml") for n in names):
        return "xlsx"
    return "zip"


def excel_engine(path: str | Path) -> str:
    """The pandas engine that can actually read this file."""
    detected = detect_workbook_format(path)
    if detected == "xlsb":
        try:
            import pyxlsb  # noqa: F401
        except ImportError as exc:
            raise ReaderError(
                f"{Path(path).name} is an Excel binary workbook (.xlsb). "
                "Install the reader with: pip install pyxlsb"
            ) from exc
        return "pyxlsb"
    if detected == "xls":
        return "xlrd"
    return "openpyxl"


_FILENAME_WEEK = re.compile(r"(?:^|[^0-9a-z])(?:wk|week|w)[ _-]?(\d{1,2})(?:[^0-9]|$)", re.I)
_FILENAME_ISO = re.compile(r"(20\d{2})[ _-]?W(\d{1,2})", re.I)
_FILENAME_DMY = re.compile(r"(?:^|[^0-9])(\d{2})(\d{2})(\d{2})(?:[^0-9]|$)")
_FILENAME_YMD = re.compile(r"(20\d{2})[-_.]?(\d{2})[-_.]?(\d{2})")


class ReaderError(ValueError):
    """Raised when a workbook cannot be interpreted at all."""


@dataclass
class ColumnMap:
    """Structural column synonyms, loaded from column_map.yaml."""

    entity_columns: dict[str, list[str]] = field(default_factory=dict)
    period_columns: dict[str, list[str]] = field(default_factory=dict)
    total_row_markers: list[str] = field(default_factory=list)
    null_tokens: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, path: str | Path | None = None) -> "ColumnMap":
        raw = yaml.safe_load(Path(path or COLUMN_MAP_FILE).read_text(encoding="utf-8")) or {}
        return cls(
            entity_columns=raw.get("entity_columns") or {},
            period_columns=raw.get("period_columns") or {},
            total_row_markers=[str(x) for x in (raw.get("total_row_markers") or [])],
            null_tokens=[str(x) for x in (raw.get("null_tokens") or [])],
        )

    def _index(self, block: Mapping[str, Sequence[str]]) -> dict[str, str]:
        out: dict[str, str] = {}
        for role, names in block.items():
            for name in names:
                out[_normalise_alias(name)] = role
        return out

    @property
    def entity_index(self) -> dict[str, str]:
        return self._index(self.entity_columns)

    @property
    def period_index(self) -> dict[str, str]:
        return self._index(self.period_columns)

    @property
    def total_markers(self) -> set[str]:
        return {_normalise_alias(m) for m in self.total_row_markers}

    @property
    def null_set(self) -> set[str]:
        return {str(t).strip().lower() for t in self.null_tokens}


@dataclass
class SheetLayout:
    """What was found on one sheet."""

    sheet_name: str
    header_row: int  # 1-based row index in the original sheet
    entity_columns: dict[str, str] = field(default_factory=dict)  # role -> column label
    period_columns: dict[str, str] = field(default_factory=dict)
    metric_columns: dict[str, str] = field(default_factory=dict)  # metric_key -> column label
    unknown_columns: list[str] = field(default_factory=list)
    grain: str = WEEKLY
    score: int = 0

    @property
    def entity_level(self) -> str:
        """Finest entity grain present on the sheet."""
        if "ba" in self.entity_columns:
            return "ba"
        if "team" in self.entity_columns:
            return "team"
        if "owner" in self.entity_columns:
            return "owner"
        return "org"

    def describe(self) -> str:
        return (
            f"sheet={self.sheet_name!r} header_row={self.header_row} grain={self.grain} "
            f"level={self.entity_level} metrics={len(self.metric_columns)} "
            f"unknown={len(self.unknown_columns)}"
        )


@dataclass
class ParsedSheet:
    """Normalised long-format output of a reader."""

    layout: SheetLayout
    frame: pd.DataFrame  # columns: owner, ba, team, period_key, grain, metric_key, source_value
    source_rows: int
    dropped_total_rows: int
    warnings: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _clean_header(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).replace("\n", " ").strip()
    return re.sub(r"\s+", " ", text)


def _is_blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and pd.isna(value):
        return True
    return str(value).strip() == ""


def to_number(value: Any, null_tokens: set[str]) -> float | None:
    """Parse an Excel cell into a float, or ``None`` when it is not a number.

    Handles the formats real reports arrive in: ``88.6%``, ``"1,234"``,
    ``(9.1)`` for negatives, and the ``#DIV/0!`` family.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return None if pd.isna(value) else float(value)
    text = str(value).strip()
    if text.lower() in null_tokens or text == "":
        return None
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1]
    percent = text.endswith("%")
    if percent:
        text = text[:-1]
    text = text.replace(",", "").replace(" ", "").strip()
    try:
        number = float(text)
    except ValueError:
        return None
    if percent:
        number /= 100.0
    return -number if negative else number


def infer_period_from_filename(name: str, grain: str = WEEKLY) -> Period | None:
    """Last-resort period detection when the sheet carries no period column.

    ``Master_Report__140826.xlsx`` -> 14 Aug 2026 -> the ISO week containing it.
    """
    stem = Path(name).stem
    m = _FILENAME_ISO.search(stem)
    if m:
        try:
            return Period(WEEKLY, int(m.group(1)), int(m.group(2)))
        except PeriodError:
            pass
    m = _FILENAME_YMD.search(stem)
    if m:
        try:
            date = _dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            return Period.from_date(date, grain)
        except ValueError:
            pass
    m = _FILENAME_DMY.search(stem)
    if m:
        day, month, year = (int(g) for g in m.groups())
        try:
            date = _dt.date(2000 + year, month, day)
            return Period.from_date(date, grain)
        except ValueError:
            pass
    m = _FILENAME_WEEK.search(stem)
    if m:
        year_match = re.search(r"20\d{2}", stem)
        if year_match:
            try:
                return Period(WEEKLY, int(year_match.group(0)), int(m.group(1)))
            except PeriodError:
                pass
    return None


# --------------------------------------------------------------------------- #
# layout detection
# --------------------------------------------------------------------------- #
def detect_layouts(
    path: str | Path,
    registry: MetricRegistry,
    column_map: ColumnMap | None = None,
    max_header_scan: int = 25,
) -> list[SheetLayout]:
    """Find every sheet that looks like data, and how to read it.

    A row is treated as the header row when it produces the highest number of
    recognised columns on that sheet.  Scanning instead of assuming row 1 is
    what lets the pivot workbook (header on row 12, filters above it) load
    without a bespoke parser.
    """
    column_map = column_map or ColumnMap.load()
    entity_idx, period_idx = column_map.entity_index, column_map.period_index
    layouts: list[SheetLayout] = []

    book = pd.read_excel(path, sheet_name=None, header=None, engine=excel_engine(path))
    for sheet_name, raw in book.items():
        if raw.empty:
            continue
        best: SheetLayout | None = None
        for row_idx in range(min(max_header_scan, len(raw))):
            headers = [_clean_header(v) for v in raw.iloc[row_idx].tolist()]
            if sum(1 for h in headers if h) < 2:
                continue
            layout = _classify_headers(sheet_name, row_idx + 1, headers, registry, entity_idx, period_idx)
            if best is None or layout.score > best.score:
                best = layout
        if best and best.metric_columns and best.score >= 2:
            layouts.append(best)
    return layouts


def _classify_headers(
    sheet_name: str,
    header_row: int,
    headers: Sequence[str],
    registry: MetricRegistry,
    entity_idx: Mapping[str, str],
    period_idx: Mapping[str, str],
) -> SheetLayout:
    layout = SheetLayout(sheet_name=sheet_name, header_row=header_row)
    seen: set[str] = set()
    for header in headers:
        if not header or header in seen:
            continue
        seen.add(header)
        token = _normalise_alias(header)
        if token in entity_idx:
            layout.entity_columns.setdefault(entity_idx[token], header)
            continue
        if token in period_idx:
            layout.period_columns.setdefault(period_idx[token], header)
            continue
        metric = registry.resolve(header)
        if metric is not None:
            layout.metric_columns.setdefault(metric.key, header)
            continue
        layout.unknown_columns.append(header)
    # A column labelled MONTH may be a period column *or* a pivot filter; the
    # period index already claimed it, which is the correct reading.
    if "week" in layout.period_columns or "date" in layout.period_columns:
        layout.grain = WEEKLY
    elif "month" in layout.period_columns or "year" in layout.period_columns:
        layout.grain = MONTHLY
    layout.score = (
        len(layout.metric_columns)
        + 3 * len(layout.entity_columns)
        + 2 * len(layout.period_columns)
    )
    return layout


# --------------------------------------------------------------------------- #
# reading
# --------------------------------------------------------------------------- #
def read_sheet(
    path: str | Path,
    layout: SheetLayout,
    registry: MetricRegistry,
    column_map: ColumnMap | None = None,
    fallback_period: Period | None = None,
) -> ParsedSheet:
    """Read one sheet into long format using a detected layout."""
    column_map = column_map or ColumnMap.load()
    null_tokens = column_map.null_set
    markers = column_map.total_markers

    raw = pd.read_excel(
        path,
        sheet_name=layout.sheet_name,
        header=layout.header_row - 1,
        engine=excel_engine(path),
    )
    raw.columns = [_clean_header(c) for c in raw.columns]
    # Duplicate labels (Excel pivots repeat "SigninDT (Year)" side by side) are
    # de-duplicated by pandas as ".1" suffixes; the layout only names the first.
    source_rows = len(raw)
    warnings: list[str] = []

    entity_cols = {role: col for role, col in layout.entity_columns.items() if col in raw.columns}
    period_cols = {role: col for role, col in layout.period_columns.items() if col in raw.columns}
    metric_cols = {k: c for k, c in layout.metric_columns.items() if c in raw.columns}
    if not metric_cols:
        raise ReaderError(f"sheet {layout.sheet_name!r}: no recognised metric columns remain")

    # -- drop pivot subtotal / filler rows --------------------------------
    key_cols = [*entity_cols.values(), *period_cols.values()]
    drop_mask = pd.Series(False, index=raw.index)
    for col in key_cols:
        text = raw[col].astype(str).str.strip()
        token = text.map(_normalise_alias)
        contains_total = token.str.contains(r"\btotal\b|\bsubtotal\b", regex=True, na=False)
        drop_mask |= contains_total | token.isin(markers)
    dropped_totals = int(drop_mask.sum())
    body = raw.loc[~drop_mask].copy()

    # Rows with no metric value at all carry no information.
    numeric_any = pd.Series(False, index=body.index)
    parsed_metrics: dict[str, pd.Series] = {}
    for metric_key, col in metric_cols.items():
        parsed = body[col].map(lambda v: to_number(v, null_tokens))
        parsed_metrics[metric_key] = parsed
        numeric_any |= parsed.notna()
    body = body.loc[numeric_any]
    for key in list(parsed_metrics):
        parsed_metrics[key] = parsed_metrics[key].loc[body.index]

    # -- resolve the period for every row ---------------------------------
    period_keys, grain = _resolve_periods(body, period_cols, layout.grain, fallback_period, warnings)
    layout.grain = grain

    # -- assemble long format ---------------------------------------------
    frame = pd.DataFrame(index=body.index)
    for role in ("owner", "ba", "team", "city"):
        col = entity_cols.get(role)
        frame[role] = (
            body[col].astype(str).str.strip().replace({"nan": "", "None": ""}) if col else ""
        )
    frame["period_key"] = period_keys
    frame["grain"] = grain
    frame = frame.loc[frame["period_key"].notna()]

    records: list[pd.DataFrame] = []
    for metric_key, series in parsed_metrics.items():
        series = series.loc[frame.index]
        block = frame.copy()
        block["metric_key"] = metric_key
        block["source_value"] = series.values
        records.append(block.loc[series.notna()])

    long = (
        pd.concat(records, ignore_index=True)
        if records
        else pd.DataFrame(
            columns=["owner", "ba", "team", "city", "period_key", "grain", "metric_key", "source_value"]
        )
    )
    return ParsedSheet(
        layout=layout,
        frame=long,
        source_rows=source_rows,
        dropped_total_rows=dropped_totals,
        warnings=warnings,
    )


def _resolve_periods(
    body: pd.DataFrame,
    period_cols: Mapping[str, str],
    declared_grain: str,
    fallback: Period | None,
    warnings: list[str],
) -> tuple[pd.Series, str]:
    """Turn whatever period columns exist into a single period_key series."""
    n = len(body)
    if "period_key" in period_cols:
        keys = body[period_cols["period_key"]].astype(str).str.strip()
        parsed = keys.map(lambda k: _safe_period_key(k))
        grain = _grain_of_first(parsed, declared_grain)
        return parsed, grain

    if "week" in period_cols:
        year_col = period_cols.get("year")
        years = body[year_col] if year_col else None
        weeks = body[period_cols["week"]]
        out = []
        for i in range(n):
            cell = weeks.iloc[i]
            # A "Week" column may hold a bare week number (33), a full period
            # key ("2026-W33"), or a date. Try each rather than assuming.
            direct = _safe_period_key(cell) if not _is_blank(cell) else None
            if direct:
                out.append(direct)
                continue
            week = to_number(cell, set())
            year = to_number(years.iloc[i], set()) if years is not None else None
            if week is None:
                parsed_date = pd.to_datetime(cell, errors="coerce")
                out.append(
                    None if pd.isna(parsed_date) else Period.from_date(parsed_date.date(), WEEKLY).key
                )
                continue
            if year is None and fallback is not None:
                year = fallback.year
            if year is None:
                out.append(None)
                continue
            try:
                out.append(Period(WEEKLY, int(year), int(week)).key)
            except PeriodError:
                out.append(None)
        return pd.Series(out, index=body.index), WEEKLY

    if "date" in period_cols:
        dates = pd.to_datetime(body[period_cols["date"]], errors="coerce")
        if dates.notna().any():
            grain = declared_grain if declared_grain in (WEEKLY, MONTHLY) else WEEKLY
            return (
                dates.map(lambda d: None if pd.isna(d) else Period.from_date(d.date(), grain).key),
                grain,
            )
        warnings.append(
            f"date column {period_cols['date']!r} held no parseable dates; falling back"
        )

    if "year" in period_cols and "month" in period_cols:
        years, months = body[period_cols["year"]], body[period_cols["month"]]
        out = []
        for i in range(n):
            year, month = years.iloc[i], months.iloc[i]
            if _is_blank(year) or _is_blank(month):
                out.append(None)
                continue
            try:
                year_no = int(float(str(year).strip()))
            except (TypeError, ValueError):
                out.append(None)
                continue
            month_no = parse_month_name(month)
            if month_no is None:
                num = to_number(month, set())
                month_no = int(num) if num and 1 <= num <= 12 else None
            if month_no is None:
                out.append(None)
                continue
            out.append(Period(MONTHLY, year_no, month_no).key)
        return pd.Series(out, index=body.index), MONTHLY

    if fallback is not None:
        warnings.append(
            f"no period column found; every row assigned to {fallback.key} "
            "inferred from the file name"
        )
        return pd.Series([fallback.key] * n, index=body.index), fallback.grain

    raise ReaderError(
        "no reporting period could be determined: the sheet has no week/month/date "
        "column and the file name carries no date"
    )


def _safe_period_key(value: str) -> str | None:
    try:
        return Period.from_key(value).key
    except PeriodError:
        return None


def _grain_of_first(series: pd.Series, default: str) -> str:
    for value in series:
        if value:
            try:
                return Period.from_key(value).grain
            except PeriodError:
                continue
    return default


def read_workbook(
    path: str | Path,
    registry: MetricRegistry,
    column_map: ColumnMap | None = None,
    sheet_name: str | None = None,
    fallback_period: Period | None = None,
) -> list[ParsedSheet]:
    """Detect and read every data sheet in a workbook."""
    column_map = column_map or ColumnMap.load()
    layouts = detect_layouts(path, registry, column_map)
    if sheet_name:
        layouts = [l for l in layouts if l.sheet_name == sheet_name]
    if not layouts:
        raise ReaderError(
            f"no sheet in {Path(path).name} contains recognisable metric columns. "
            "Check the export, or add the new column names to config/column_map.yaml "
            "and config/metrics.yaml."
        )
    if fallback_period is None:
        fallback_period = infer_period_from_filename(Path(path).name)
    out: list[ParsedSheet] = []
    for layout in layouts:
        try:
            out.append(read_sheet(path, layout, registry, column_map, fallback_period))
        except ReaderError:
            continue
    if not out:
        raise ReaderError(f"no sheet in {Path(path).name} could be read")
    return out


def pick_best_sheet(sheets: Sequence[ParsedSheet]) -> ParsedSheet:
    """Choose the sheet that carries the most granular, richest data.

    Finer entity level wins first (BA beats Owner beats org-only), then metric
    breadth, then row count.  Preferring granularity matters: an org-level
    summary and a BA-level detail sheet in the same workbook describe the same
    numbers, and only the detail sheet can drive BA alerts.
    """
    rank = {"ba": 3, "team": 2, "owner": 1, "org": 0}
    return max(
        sheets,
        key=lambda s: (
            rank.get(s.layout.entity_level, 0),
            len(s.layout.metric_columns),
            len(s.frame),
        ),
    )
