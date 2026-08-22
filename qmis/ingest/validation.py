"""Pre-load validation.

The rule this module exists to enforce: **a file that cannot be trusted is not
loaded**.  A dashboard built on a silently mis-parsed spreadsheet is worse than
no dashboard, because people act on it.

Findings are graded:

``error``    blocks the load.  The upload is recorded and rejected with an
             explanation naming the column, the row and the offending value.
``warning``  loads, but is surfaced on the upload report and in Admin.
``info``     recorded for the audit trail (new BAs, entity churn).

Every rule is pure - it takes a parsed sheet plus a :class:`ValidationContext`
snapshot of what the database already knows, and returns findings.  That makes
each rule directly testable without a database.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence

import pandas as pd

from qmis.core.metric_config import MetricRegistry
from qmis.core.models import ISSUE_ERROR, ISSUE_INFO, ISSUE_WARNING
from qmis.core.periods import Period, PeriodError
from qmis.ingest.readers import ParsedSheet


@dataclass(frozen=True)
class Finding:
    severity: str
    code: str
    message: str
    column: str | None = None
    row_ref: str | None = None
    sample: str | None = None

    def __str__(self) -> str:  # pragma: no cover - display helper
        where = f" [{self.column}]" if self.column else ""
        return f"{self.severity.upper()}{where}: {self.message}"


@dataclass
class ValidationContext:
    """What the database already knows, passed in so rules stay pure."""

    known_columns: set[str] = field(default_factory=set)
    known_owners: set[str] = field(default_factory=set)
    known_bas: set[str] = field(default_factory=set)
    loaded_periods: set[str] = field(default_factory=set)
    known_hashes: dict[str, str] = field(default_factory=dict)  # hash -> filename
    previous_coverage: Mapping[str, int] = field(default_factory=dict)  # metric -> row count
    allow_reprocess: bool = False
    coverage_drop_tolerance: float = 0.30
    ratio_tolerance_pp: float = 1.0


@dataclass
class ValidationReport:
    findings: list[Finding] = field(default_factory=list)

    def add(self, *findings: Finding) -> None:
        self.findings.extend(findings)

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == ISSUE_ERROR]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == ISSUE_WARNING]

    @property
    def infos(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == ISSUE_INFO]

    @property
    def ok(self) -> bool:
        return not self.errors

    def summary(self) -> str:
        return (
            f"{len(self.errors)} error(s), {len(self.warnings)} warning(s), "
            f"{len(self.infos)} note(s)"
        )


def _normalise_name(value: str) -> str:
    return " ".join(str(value).strip().split()).casefold()


# --------------------------------------------------------------------------- #
# individual rules
# --------------------------------------------------------------------------- #
def check_duplicate_upload(
    content_hash: str, filename: str, period_keys: Sequence[str], ctx: ValidationContext
) -> list[Finding]:
    out: list[Finding] = []
    if content_hash in ctx.known_hashes and not ctx.allow_reprocess:
        out.append(
            Finding(
                ISSUE_ERROR,
                "duplicate_file",
                f"This exact file has already been processed (as "
                f"{ctx.known_hashes[content_hash]!r}). Nothing to do. Tick "
                f"'reprocess' if you are deliberately reloading it.",
                sample=content_hash[:12],
            )
        )
    repeats = sorted({p for p in period_keys if p in ctx.loaded_periods})
    if repeats and not ctx.allow_reprocess:
        out.append(
            Finding(
                ISSUE_ERROR,
                "duplicate_period",
                f"Period(s) {', '.join(repeats)} have already been loaded from a different "
                f"file. Loading again would create a second version of the same week. "
                f"Tick 'reprocess' to supersede the existing data (the old version is kept "
                f"for audit).",
                sample=", ".join(repeats[:8]),
            )
        )
    elif repeats and ctx.allow_reprocess:
        out.append(
            Finding(
                ISSUE_INFO,
                "reprocess_period",
                f"Reprocessing {', '.join(repeats)}; the previous version will be superseded "
                f"but retained.",
            )
        )
    return out


def check_structure(
    parsed: ParsedSheet, registry: MetricRegistry, ctx: ValidationContext
) -> list[Finding]:
    """Compare this file's columns against the last accepted file's columns."""
    out: list[Finding] = []
    layout = parsed.layout
    present = set(layout.metric_columns.keys())

    unknown = [c for c in layout.unknown_columns if c and c.lower() not in ("nan", "none", "unnamed")]
    unknown = [c for c in unknown if not c.lower().startswith("unnamed:")]
    if unknown:
        out.append(
            Finding(
                ISSUE_WARNING,
                "unexpected_column",
                f"{len(unknown)} column(s) on sheet {layout.sheet_name!r} were not recognised "
                f"and have been ignored: {', '.join(unknown[:12])}"
                + (" ..." if len(unknown) > 12 else "")
                + ". If any of these is a metric, add it to config/metrics.yaml.",
                column=unknown[0],
                sample=", ".join(unknown[:12]),
            )
        )

    if ctx.known_columns:
        missing = sorted(ctx.known_columns - present)
        if missing:
            names = [registry[k].name if k in registry else k for k in missing]
            out.append(
                Finding(
                    ISSUE_ERROR,
                    "missing_column",
                    f"{len(missing)} metric column(s) present in the previous report are "
                    f"missing from this one: {', '.join(names)}. The export has changed shape - "
                    f"loading it would silently blank these metrics.",
                    column=names[0],
                    sample=", ".join(names[:12]),
                )
            )
        added = sorted(present - ctx.known_columns)
        if added:
            names = [registry[k].name if k in registry else k for k in added]
            out.append(
                Finding(
                    ISSUE_INFO,
                    "new_column",
                    f"New metric column(s) in this report: {', '.join(names)}.",
                    sample=", ".join(names[:12]),
                )
            )
    return out


def check_entities(parsed: ParsedSheet, ctx: ValidationContext) -> list[Finding]:
    out: list[Finding] = []
    frame = parsed.frame
    level = parsed.layout.entity_level
    if frame.empty:
        return [Finding(ISSUE_ERROR, "no_rows", "The sheet produced no usable data rows.")]

    if level == "org":
        out.append(
            Finding(
                ISSUE_WARNING,
                "org_level_only",
                f"Sheet {parsed.layout.sheet_name!r} carries no Owner or BA column, so only "
                f"organisation-level totals can be loaded. Owner-level and BA-level alerting "
                f"needs an export with 'OWNER NAME' and 'BAName' columns.",
            )
        )
        return out

    if "ba" in parsed.layout.entity_columns:
        blank_ba = frame.loc[frame["ba"].astype(str).str.strip() == ""]
        if not blank_ba.empty:
            periods = sorted(blank_ba["period_key"].dropna().unique())[:5]
            out.append(
                Finding(
                    ISSUE_ERROR,
                    "missing_ba",
                    f"{blank_ba['period_key'].count()} value(s) have no BA name and cannot be "
                    f"attributed. Affected period(s): {', '.join(map(str, periods))}.",
                    column=parsed.layout.entity_columns["ba"],
                    sample=", ".join(map(str, periods)),
                )
            )
        if "owner" in parsed.layout.entity_columns:
            named = frame.loc[frame["ba"].astype(str).str.strip() != ""]
            blank_owner = named.loc[named["owner"].astype(str).str.strip() == ""]
            if not blank_owner.empty:
                bas = sorted(blank_owner["ba"].unique())[:8]
                out.append(
                    Finding(
                        ISSUE_ERROR,
                        "missing_owner",
                        f"{len(set(blank_owner['ba']))} BA(s) have no Owner and cannot be rolled "
                        f"up: {', '.join(map(str, bas))}.",
                        column=parsed.layout.entity_columns["owner"],
                        sample=", ".join(map(str, bas)),
                    )
                )
            # One BA reporting to two Owners in the same period breaks the rollup.
            pairs = named.loc[named["owner"].astype(str).str.strip() != ""]
            pairs = pairs[["ba", "owner", "period_key"]].drop_duplicates()
            counts = pairs.groupby(["ba", "period_key"])["owner"].nunique()
            clashes = counts[counts > 1]
            if not clashes.empty:
                sample = [f"{ba} ({period})" for ba, period in list(clashes.index)[:8]]
                out.append(
                    Finding(
                        ISSUE_ERROR,
                        "ba_multiple_owners",
                        f"{len(clashes)} BA/period combination(s) map to more than one Owner, "
                        f"so Owner totals would double-count them: {', '.join(sample)}.",
                        sample=", ".join(sample),
                    )
                )

        if ctx.known_bas:
            seen = {_normalise_name(b) for b in frame["ba"].unique() if str(b).strip()}
            new = sorted(seen - ctx.known_bas)
            gone = sorted(ctx.known_bas - seen)
            if new:
                out.append(
                    Finding(
                        ISSUE_INFO,
                        "new_ba",
                        f"{len(new)} BA(s) appear for the first time in this report.",
                        sample=", ".join(new[:15]),
                    )
                )
            if gone:
                severity = ISSUE_WARNING if len(gone) > max(5, 0.2 * len(ctx.known_bas)) else ISSUE_INFO
                out.append(
                    Finding(
                        severity,
                        "missing_ba_vs_history",
                        f"{len(gone)} previously-reporting BA(s) are absent from this report. "
                        f"They will be treated as inactive for this period, not deleted.",
                        sample=", ".join(gone[:15]),
                    )
                )
    return out


def check_duplicate_rows(parsed: ParsedSheet) -> list[Finding]:
    frame = parsed.frame
    if frame.empty:
        return []
    keys = ["owner", "ba", "team", "period_key", "metric_key"]
    dupes = frame.duplicated(subset=keys, keep=False)
    if not dupes.any():
        return []
    sample_frame = frame.loc[dupes, keys].drop_duplicates().head(8)
    labels = [
        f"{r.ba or r.owner or 'ORG'}/{r.metric_key}/{r.period_key}"
        for r in sample_frame.itertuples()
    ]
    return [
        Finding(
            ISSUE_ERROR,
            "duplicate_rows",
            f"{int(dupes.sum())} rows duplicate an existing entity/period/metric combination. "
            f"The same BA appears twice for the same week, which would double-count. "
            f"Examples: {', '.join(labels)}.",
            sample=", ".join(labels),
        )
    ]


def check_values(parsed: ParsedSheet, registry: MetricRegistry) -> list[Finding]:
    """Range, sign and percentage-format checks, per metric."""
    out: list[Finding] = []
    frame = parsed.frame
    if frame.empty:
        return out
    for metric_key, block in frame.groupby("metric_key"):
        metric = registry.get(str(metric_key))
        if metric is None:
            continue
        values = block["source_value"].dropna()
        if values.empty:
            continue
        column = parsed.layout.metric_columns.get(str(metric_key), str(metric_key))

        negatives = values[values < 0]
        if metric.unit in ("count", "currency") and not negatives.empty:
            out.append(
                Finding(
                    ISSUE_ERROR,
                    "negative_value",
                    f"{metric.name} has {len(negatives)} negative value(s); counts and amounts "
                    f"cannot be negative. Smallest: {negatives.min():,.2f}.",
                    column=column,
                    sample=f"{negatives.min():,.2f}",
                )
            )
        elif metric.unit == "percent" and not negatives.empty:
            out.append(
                Finding(
                    ISSUE_WARNING,
                    "negative_percent",
                    f"{metric.name} has {len(negatives)} negative value(s). Verify this is a "
                    f"genuine negative rate and not a formatting artefact.",
                    column=column,
                    sample=f"{negatives.min():,.4f}",
                )
            )

        if metric.unit == "percent":
            out.extend(_check_percent_scale(metric, values, column))
    return out


def _check_percent_scale(metric, values: pd.Series, column: str) -> list[Finding]:
    """Detect fraction-vs-percent confusion, the classic weekly-report bug."""
    out: list[Finding] = []
    finite = values[values.notna()]
    if finite.empty:
        return out
    high = float(finite.max())
    declared_fraction = metric.stored_as == "fraction"
    if declared_fraction and high > 1.5:
        out.append(
            Finding(
                ISSUE_ERROR,
                "percent_scale",
                f"{metric.name} is configured as a fraction (0.88 = 88%) but this file "
                f"contains values up to {high:,.2f}. The export has switched to 0-100 "
                f"percentages. Fix the export, or change stored_as to 'percent' for this "
                f"metric in config/metrics.yaml.",
                column=column,
                sample=f"max={high:,.4f}",
            )
        )
    elif not declared_fraction and high <= 1.5 and len(finite) >= 5:
        out.append(
            Finding(
                ISSUE_WARNING,
                "percent_scale",
                f"{metric.name} is configured as 0-100 percentages but no value exceeds "
                f"{high:,.2f}. If the export now uses fractions, set stored_as: fraction.",
                column=column,
                sample=f"max={high:,.4f}",
            )
        )
    scaled = finite * (100.0 if declared_fraction else 1.0)
    absurd = scaled[(scaled > 100.5)]
    if not absurd.empty and not (declared_fraction and high > 1.5):
        out.append(
            Finding(
                ISSUE_WARNING,
                "percent_out_of_range",
                f"{metric.name} has {len(absurd)} value(s) above 100%. Highest: "
                f"{absurd.max():,.2f}%.",
                column=column,
                sample=f"{absurd.max():,.2f}",
            )
        )
    return out


def check_ratio_consistency(
    parsed: ParsedSheet, registry: MetricRegistry, tolerance_pp: float = 1.0
) -> list[Finding]:
    """Recompute every ``numerator / denominator`` rate and compare.

    Every percentage in the Master Report is ``something / SUBMISSION``.  If the
    stated rate and the recomputed rate disagree, either the export is broken or
    the columns have been re-pointed - both of which must surface before the
    numbers reach a dashboard.
    """
    out: list[Finding] = []
    frame = parsed.frame
    if frame.empty:
        return out
    wide = frame.pivot_table(
        index=["owner", "ba", "team", "period_key"],
        columns="metric_key",
        values="source_value",
        aggfunc="first",
    )
    for metric in registry:
        if not (metric.numerator and metric.denominator and metric.unit == "percent"):
            continue
        if metric.key not in wide.columns or metric.numerator not in wide.columns:
            continue
        if metric.denominator not in wide.columns:
            continue
        num = wide[metric.numerator]
        den = wide[metric.denominator]
        stated = wide[metric.key] * (100.0 if metric.stored_as == "fraction" else 1.0)
        mask = den.notna() & (den > 0) & num.notna() & stated.notna()
        if not mask.any():
            continue
        recomputed = (num[mask] / den[mask]) * 100.0
        diff = (recomputed - stated[mask]).abs()
        bad = diff[diff > tolerance_pp]
        if bad.empty:
            continue
        worst_idx = diff.idxmax()
        out.append(
            Finding(
                ISSUE_WARNING,
                "ratio_mismatch",
                f"{metric.name} disagrees with {metric.numerator}/{metric.denominator} on "
                f"{len(bad)} row(s) by more than {tolerance_pp}pp. Worst row {worst_idx}: "
                f"stated {stated[mask].loc[worst_idx]:.2f}%, recomputed "
                f"{recomputed.loc[worst_idx]:.2f}%.",
                column=parsed.layout.metric_columns.get(metric.key),
                sample=f"{diff.max():.2f}pp",
            )
        )
    return out


def check_ladder_monotonicity(parsed: ParsedSheet, registry: MetricRegistry) -> list[Finding]:
    """A donor cannot reach debit 5 without reaching debit 4."""
    out: list[Finding] = []
    frame = parsed.frame
    if frame.empty:
        return out
    ladder = [f"d{n}_pct" for n in range(1, 13)]
    present = [k for k in ladder if k in set(frame["metric_key"])]
    if len(present) < 2:
        return out
    wide = frame.pivot_table(
        index=["owner", "ba", "team", "period_key"],
        columns="metric_key",
        values="source_value",
        aggfunc="first",
    )
    breaches = 0
    example = ""
    for earlier, later in zip(present, present[1:]):
        if earlier not in wide.columns or later not in wide.columns:
            continue
        mask = wide[earlier].notna() & wide[later].notna() & (wide[later] > wide[earlier] * 1.001)
        # A zero later-stage value is immaturity, not a breach.
        mask &= wide[later] > 0
        count = int(mask.sum())
        if count and not example:
            row = wide.loc[mask].index[0]
            example = (
                f"{later} ({wide.loc[mask, later].iloc[0]:.4f}) exceeds {earlier} "
                f"({wide.loc[mask, earlier].iloc[0]:.4f}) at {row}"
            )
        breaches += count
    if breaches:
        out.append(
            Finding(
                ISSUE_WARNING,
                "ladder_not_monotonic",
                f"The debit ladder is not monotonic on {breaches} row(s): a later debit stage "
                f"reports a higher retention than an earlier one, which is not possible. "
                f"Example: {example}.",
                sample=example,
            )
        )
    return out


def check_coverage(parsed: ParsedSheet, registry: MetricRegistry, ctx: ValidationContext) -> list[Finding]:
    """Catch large blank areas - a half-exported file looks fine until charted."""
    out: list[Finding] = []
    frame = parsed.frame
    if frame.empty or not ctx.previous_coverage:
        return out
    counts = frame.groupby("metric_key").size().to_dict()
    dropped: list[str] = []
    for metric_key, previous in ctx.previous_coverage.items():
        if previous <= 0:
            continue
        now = counts.get(metric_key, 0)
        if now < previous * (1 - ctx.coverage_drop_tolerance):
            name = registry[metric_key].name if metric_key in registry else metric_key
            dropped.append(f"{name} ({now} vs {previous})")
    if dropped:
        out.append(
            Finding(
                ISSUE_WARNING,
                "coverage_drop",
                f"{len(dropped)} metric(s) have far fewer values than the previous report, "
                f"which usually means a partial export: {', '.join(dropped[:10])}.",
                sample=", ".join(dropped[:10]),
            )
        )
    return out


def check_periods(parsed: ParsedSheet) -> list[Finding]:
    out: list[Finding] = []
    frame = parsed.frame
    if frame.empty:
        return out
    keys = sorted({k for k in frame["period_key"].dropna().unique()})
    bad = []
    for key in keys:
        try:
            Period.from_key(key)
        except PeriodError:
            bad.append(key)
    if bad:
        out.append(
            Finding(
                ISSUE_ERROR,
                "bad_period",
                f"Unparseable reporting period(s): {', '.join(map(str, bad[:10]))}.",
                sample=", ".join(map(str, bad[:10])),
            )
        )
    if len(keys) > 1:
        out.append(
            Finding(
                ISSUE_INFO,
                "multi_period_file",
                f"This file covers {len(keys)} periods ({keys[0]} to {keys[-1]}). All of them "
                f"will be loaded.",
            )
        )
    return out


# --------------------------------------------------------------------------- #
# orchestration
# --------------------------------------------------------------------------- #
def validate(
    parsed: ParsedSheet,
    registry: MetricRegistry,
    ctx: ValidationContext,
    content_hash: str = "",
    filename: str = "",
) -> ValidationReport:
    """Run every rule and return the combined report."""
    report = ValidationReport()
    period_keys = (
        sorted({k for k in parsed.frame["period_key"].dropna().unique()})
        if not parsed.frame.empty
        else []
    )
    report.add(*check_duplicate_upload(content_hash, filename, period_keys, ctx))
    report.add(*check_structure(parsed, registry, ctx))
    report.add(*check_periods(parsed))
    report.add(*check_entities(parsed, ctx))
    report.add(*check_duplicate_rows(parsed))
    report.add(*check_values(parsed, registry))
    report.add(*check_ratio_consistency(parsed, registry, ctx.ratio_tolerance_pp))
    report.add(*check_ladder_monotonicity(parsed, registry))
    report.add(*check_coverage(parsed, registry, ctx))
    if parsed.warnings:
        report.add(
            *[Finding(ISSUE_WARNING, "reader_warning", w) for w in parsed.warnings]
        )
    return report
