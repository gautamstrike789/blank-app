"""Donation-level aggregation.

The weekly export is one row per submission - 308,922 rows across 96 columns in
the file this was built against - and every metric in the Master Report is a sum
or a mean over those rows. This module folds that into the
(entity x period x metric) facts the rest of the system already understands.

Three things it deliberately does *not* do:

* **It does not average rates.** ``sum(debit1) / sum(SUBMISSION)`` is computed
  fresh at each level of the hierarchy. Averaging a BA-level rate up to an Owner
  gives every BA equal weight regardless of volume, which is how one tiny BA
  with a bad week drags an Owner's number around.
* **It does not trust the case of a value.** ``South`` and ``SOUTH`` are one
  region; ``Triforce`` and ``TRIFORCE`` are one company. The source contains
  both spellings of each, and left alone they split one entity into two, making
  every rate computed over them wrong.
* **It does not invent derivations.** Each derived measure in ``source_map.yaml``
  was solved against the Master Report's own Grand Total and is annotated with
  the number it reproduces.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import numpy as np
import pandas as pd
import yaml

from qmis.core.periods import Period, PeriodError

SOURCE_MAP_FILE = Path(__file__).resolve().parent.parent / "config" / "source_map.yaml"


class AggregationError(ValueError):
    """Raised when a donation-level sheet cannot be folded up."""


@dataclass
class SourceMap:
    """The parsed ``source_map.yaml``."""

    detection: dict = field(default_factory=dict)
    period: dict = field(default_factory=dict)
    hierarchy: list[dict] = field(default_factory=list)
    attributes: dict = field(default_factory=dict)
    normalise_values: dict = field(default_factory=dict)
    measures: dict = field(default_factory=dict)
    rates: dict = field(default_factory=dict)
    expectations: dict = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path | None = None) -> "SourceMap":
        raw = yaml.safe_load(Path(path or SOURCE_MAP_FILE).read_text(encoding="utf-8")) or {}
        return cls(
            detection=raw.get("detection") or {},
            period=raw.get("period") or {},
            hierarchy=raw.get("hierarchy") or [],
            attributes=raw.get("attributes") or {},
            normalise_values=raw.get("normalise_values") or {},
            measures=raw.get("measures") or {},
            rates=raw.get("rates") or {},
            expectations=raw.get("expectations") or {},
        )

    @property
    def levels(self) -> list[str]:
        return [h["level"] for h in self.hierarchy]

    def column_for(self, level: str) -> str | None:
        for h in self.hierarchy:
            if h["level"] == level:
                return h.get("column")
        return None


@dataclass
class AggregationResult:
    facts: pd.DataFrame          # entity_path, level, period_key, metric_key, value, denominator
    entities: pd.DataFrame       # one row per distinct entity, with its parent
    source_rows: int
    periods: list[str]
    warnings: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# value normalisation
# --------------------------------------------------------------------------- #
def normalise_value(value: Any) -> str:
    """Collapse whitespace only. Case is decided per column by frequency."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return " ".join(str(value).strip().split())


def canonical_forms(series: pd.Series) -> dict[str, str]:
    """Pick one surface form per value, by how often each spelling is used.

    Guessing from the shape of a string does not work here. An earlier version
    preserved any short all-capitals token as an acronym, which correctly kept
    ``KOP`` and ``PVR`` but also kept ``ALZA`` apart from ``Alza`` - the very
    split this exists to fix. Letting the data decide handles both: ``Alza``
    appears 14,009 times against 3,576 for ``ALZA`` so it wins, while ``KOP``
    has no rival spelling and is left alone.
    """
    cleaned = series.map(normalise_value)
    counts = cleaned.loc[cleaned != ""].value_counts()
    winners: dict[str, str] = {}
    for surface, count in counts.items():
        key = surface.casefold()
        if key not in winners:
            winners[key] = surface  # value_counts is ordered by frequency
    return {surface: winners[surface.casefold()] for surface in counts.index}


def normalise_columns(frame: pd.DataFrame, columns: Sequence[str]) -> tuple[pd.DataFrame, list[str]]:
    """Normalise the configured columns, reporting what it merged."""
    out = frame.copy()
    notes: list[str] = []
    for column in columns:
        if column not in out.columns:
            continue
        cleaned = out[column].map(normalise_value)
        before = cleaned.loc[cleaned != ""].nunique()
        mapping = canonical_forms(out[column])
        out[column] = cleaned.map(lambda v: mapping.get(v, v))
        after = out[column].loc[out[column] != ""].nunique()
        if after < before:
            merged = sorted(
                {v for v, canon in mapping.items() if v != canon}
            )[:6]
            notes.append(
                f"{column}: {before} spellings collapsed to {after} distinct values "
                f"(merged: {', '.join(merged)}{' ...' if len(merged) == 6 else ''})"
            )
    return out, notes


# --------------------------------------------------------------------------- #
# detection
# --------------------------------------------------------------------------- #
def looks_donation_level(frame: pd.DataFrame, source: SourceMap) -> bool:
    """Is this one row per submission rather than a pre-aggregated summary?"""
    columns = set(frame.columns)
    required = source.detection.get("required_any") or []
    if required and not any(c in columns for c in required):
        return False
    period_column = source.period.get("column")
    fallbacks = source.period.get("fallback_columns") or []
    if period_column not in columns and not any(c in columns for c in fallbacks):
        return False
    if not any(source.column_for(level) in columns for level in source.levels):
        return False
    flags = 0
    for column in columns:
        series = frame[column].dropna()
        if series.empty or not pd.api.types.is_numeric_dtype(series):
            continue
        if set(pd.unique(series)[:5]) <= {0, 1, 0.0, 1.0, True, False}:
            flags += 1
    return flags >= int(source.detection.get("min_flag_columns", 8))


# --------------------------------------------------------------------------- #
# period resolution
# --------------------------------------------------------------------------- #
def resolve_period_column(frame: pd.DataFrame, source: SourceMap) -> tuple[pd.Series, str]:
    """Map every row onto a period key using the configured week column."""
    candidates = [source.period.get("column"), *(source.period.get("fallback_columns") or [])]
    grain = source.period.get("grain", "weekly")
    for column in candidates:
        if not column or column not in frame.columns:
            continue
        dates = pd.to_datetime(frame[column], errors="coerce")
        if dates.notna().mean() < 0.5:
            continue
        keys = dates.map(
            lambda d: None if pd.isna(d) else Period.from_date(d.date(), grain).key
        )
        return keys, column
    raise AggregationError(
        "no usable period column: expected one of "
        f"{', '.join(str(c) for c in candidates if c)}"
    )


# --------------------------------------------------------------------------- #
# measures
# --------------------------------------------------------------------------- #
def _mask_for(frame: pd.DataFrame, spec: Mapping[str, Any] | None) -> pd.Series | None:
    if not spec:
        return None
    column = spec.get("column")
    if column not in frame.columns:
        return None
    series = frame[column]
    if "in" in spec:
        wanted = {normalise_value(v) for v in spec["in"]}
        return series.map(normalise_value).isin(wanted)
    if "equals" in spec:
        return series == spec["equals"]
    return None


def compute_measures(
    grouped: pd.core.groupby.DataFrameGroupBy,
    frame: pd.DataFrame,
    keys: list[str],
    source: SourceMap,
) -> pd.DataFrame:
    """Every configured measure, as one wide frame indexed by the group keys."""
    out: pd.DataFrame | None = None

    def merge(block: pd.DataFrame) -> None:
        nonlocal out
        out = block if out is None else out.merge(block, on=keys, how="outer")

    deferred: list[tuple[str, Mapping[str, Any]]] = []
    for name, spec in source.measures.items():
        if not isinstance(spec, Mapping):
            continue
        if "sum" in spec:
            column = spec["sum"]
            if column not in frame.columns:
                continue
            merge(grouped[column].sum().rename(name).reset_index())
        elif "sum_of" in spec:
            columns = [c for c in spec["sum_of"] if c in frame.columns]
            if not columns:
                continue
            total = frame[columns].fillna(0).sum(axis=1)
            merge(
                frame.assign(_v=total).groupby(keys, dropna=False)["_v"].sum()
                .rename(name).reset_index()
            )
        elif "count_where" in spec:
            mask = _mask_for(frame, spec["count_where"])
            if mask is None:
                continue
            merge(
                frame.assign(_v=mask.astype(int)).groupby(keys, dropna=False)["_v"].sum()
                .rename(name).reset_index()
            )
        elif "mean" in spec:
            column = spec["mean"]
            if column not in frame.columns:
                continue
            mask = _mask_for(frame, spec.get("where"))
            block = frame if mask is None else frame.loc[mask]
            if block.empty:
                continue
            merge(block.groupby(keys, dropna=False)[column].mean().rename(name).reset_index())
        elif "sum_of_measures" in spec:
            deferred.append((name, spec))

    if out is None:
        raise AggregationError("no configured measure matched any column in the sheet")

    # Measures built from other measures, once those exist.
    for name, spec in deferred:
        parts = [m for m in spec["sum_of_measures"] if m in out.columns]
        if not parts:
            continue
        out[name] = out[parts].fillna(0).sum(axis=1)
    return out


def compute_rates(wide: pd.DataFrame, source: SourceMap) -> pd.DataFrame:
    """Recompute every rate from its components, as a 0-100 percentage."""
    out = wide.copy()
    for name, spec in source.rates.items():
        numerator, denominator = spec.get("numerator"), spec.get("denominator")
        if numerator not in out.columns or denominator not in out.columns:
            continue
        num = out[numerator].astype(float)
        den = out[denominator].astype(float)
        with np.errstate(divide="ignore", invalid="ignore"):
            out[name] = np.where(den > 0, num / den * 100.0, np.nan)
    return out


# --------------------------------------------------------------------------- #
# the aggregator
# --------------------------------------------------------------------------- #
def aggregate_donations(
    frame: pd.DataFrame,
    source: SourceMap | None = None,
    levels: Sequence[str] | None = None,
) -> AggregationResult:
    """Fold donation rows into (entity x period x metric) facts.

    Facts are produced at every configured level. Each level is aggregated from
    the donation rows directly rather than from the level below it, so a rate is
    always ``sum(numerator) / sum(denominator)`` over the rows that actually
    belong to that entity.
    """
    source = source or SourceMap.load()
    if frame.empty:
        raise AggregationError("the sheet contains no rows")

    warnings: list[str] = []
    columns = list(source.normalise_values.get("columns") or [])
    frame, notes = normalise_columns(frame, columns)
    warnings.extend(notes)

    period_keys, period_column = resolve_period_column(frame, source)
    frame = frame.assign(_period=period_keys)
    dropped = int(frame["_period"].isna().sum())
    if dropped:
        warnings.append(f"{dropped:,} row(s) had no usable {period_column} and were skipped")
    frame = frame.loc[frame["_period"].notna()]
    if frame.empty:
        raise AggregationError(f"no row had a usable date in {period_column!r}")

    wanted_levels = list(levels or source.levels)
    fact_blocks: list[pd.DataFrame] = []
    entity_rows: list[dict] = []

    for depth, level in enumerate(wanted_levels):
        chain = [source.column_for(l) for l in wanted_levels[: depth + 1]]
        chain = [c for c in chain if c and c in frame.columns]
        if not chain:
            continue
        block = frame.loc[frame[chain[-1]].astype(str).str.len() > 0]
        if block.empty:
            continue

        keys = ["_period", *chain]
        grouped = block.groupby(keys, dropna=False)
        wide = compute_measures(grouped, block, keys, source)
        wide = compute_rates(wide, source)

        # A stable identity for the entity: its full path down the hierarchy.
        path = wide[chain].astype(str).agg(" | ".join, axis=1)
        wide = wide.assign(
            entity_path=path,
            entity_name=wide[chain[-1]].astype(str),
            parent_path=(
                wide[chain[:-1]].astype(str).agg(" | ".join, axis=1) if len(chain) > 1 else ""
            ),
            level=level,
        )
        for row in wide[["entity_path", "entity_name", "parent_path", "level"]].drop_duplicates().to_dict("records"):
            entity_rows.append(row)

        measure_columns = [
            c for c in wide.columns
            if c not in {*keys, "entity_path", "entity_name", "parent_path", "level"}
        ]
        long = wide.melt(
            id_vars=["entity_path", "entity_name", "parent_path", "level", "_period"],
            value_vars=measure_columns,
            var_name="metric_key",
            value_name="value",
        )
        long = long.loc[long["value"].notna()]
        denominators = wide[["entity_path", "_period", "submissions"]] if "submissions" in wide.columns else None
        if denominators is not None:
            long = long.merge(denominators, on=["entity_path", "_period"], how="left")
            long = long.rename(columns={"submissions": "denominator"})
        else:
            long["denominator"] = np.nan
        fact_blocks.append(long.rename(columns={"_period": "period_key"}))

    if not fact_blocks:
        raise AggregationError("no hierarchy column produced any entity")

    facts = pd.concat(fact_blocks, ignore_index=True)
    entities = pd.DataFrame(entity_rows).drop_duplicates(subset=["entity_path"])
    periods = sorted(facts["period_key"].unique())
    warnings.extend(_check_expectations(facts, source))
    return AggregationResult(
        facts=facts,
        entities=entities,
        source_rows=len(frame),
        periods=[str(p) for p in periods],
        warnings=warnings,
    )


def _check_expectations(facts: pd.DataFrame, source: SourceMap) -> list[str]:
    """Cheap post-aggregation sanity checks, reported rather than raised."""
    notes: list[str] = []
    ladder = source.expectations.get("ladder_monotonic") or []
    max_rate = float(source.expectations.get("max_rate_pct", 100.5))

    rates = facts.loc[facts["metric_key"].str.endswith("_pct")]
    if not rates.empty:
        over = rates.loc[rates["value"] > max_rate]
        if not over.empty:
            notes.append(
                f"{len(over):,} rate value(s) exceed {max_rate}% - check the source flags"
            )

    present = [m for m in ladder if m in set(facts["metric_key"])]
    tolerance = float(source.expectations.get("ladder_monotonic_tolerance_pct", 0.0))
    if len(present) >= 2:
        wide = facts.loc[facts["metric_key"].isin(present)].pivot_table(
            index=["entity_path", "period_key"], columns="metric_key", values="value"
        )
        breaches = comparisons = 0
        for earlier, later in zip(present, present[1:]):
            if earlier in wide.columns and later in wide.columns:
                mask = wide[earlier].notna() & wide[later].notna()
                comparisons += int(mask.sum())
                breaches += int((wide.loc[mask, later] > wide.loc[mask, earlier] + 0.001).sum())
        share = (breaches / comparisons * 100.0) if comparisons else 0.0
        if comparisons and share > tolerance:
            notes.append(
                f"the debit ladder runs backwards on {breaches:,} of {comparisons:,} "
                f"stage comparisons ({share:.1f}%), above the {tolerance:.0f}% expected from "
                f"donors resuming after a missed debit - worth checking the source flags"
            )
    return notes
