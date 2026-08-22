"""The evaluation orchestrator.

    load history -> roll up -> compare -> detect anomalies -> judge -> persist

One entry point, :func:`evaluate_period`, produces the alert run, the alerts and
the scores for a period, and is safe to re-run: a fresh run supersedes the
previous one rather than duplicating it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

import numpy as np
import pandas as pd
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from qmis.core.metric_config import MetricRegistry, get_registry
from qmis.core.models import (
    BA,
    Fact,
    GREEN,
    KIND_IMPROVEMENT,
    ORANGE,
    ORG,
    OWNER,
    RED,
    SEVERITY_ORDER,
    TEAM,
    YELLOW,
    Alert,
    AlertRun,
    Score,
)
from qmis.core.periods import Period
from qmis.analytics.anomaly import detect_anomalies
from qmis.analytics.comparison import build_comparison
from qmis.analytics.grouping import group_family_alerts
from qmis.analytics.matrix import build_matrix
from qmis.analytics.repository import (
    available_periods,
    dominant_grain,
    load_entities,
    load_history,
)
from qmis.analytics.rolling import apply_rolling
from qmis.analytics.rollup import rollup_history
from qmis.analytics.scoring import band_for, score_entities
from qmis.analytics.severity import judge

ASSESSMENT_COLUMNS = [
    "entity_id", "entity_type", "entity_name", "owner_id", "owner_name",
    "period_key", "metric_key", "current", "previous", "delta", "pct_change",
    "trailing_avg", "delta_vs_trailing", "best", "worst", "denominator",
    "consecutive_adverse", "streak_delta", "consecutive_favourable",
    "favourable_streak_delta", "trend", "z_score", "baseline", "is_anomaly",
    "severity", "kind", "threshold_status", "is_improvement", "alertable",
    "suppressed", "priority", "basis", "statistically_confirmed", "headline", "explanation",
]

# Added by the family grouper; not produced by assess() itself.
OPTIONAL_COLUMNS = ["rolled_into"]


@dataclass
class EvaluationResult:
    period_key: str
    grain: str
    run_id: int | None
    assessments: pd.DataFrame = field(default_factory=pd.DataFrame)
    scores: pd.DataFrame = field(default_factory=pd.DataFrame)
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def alerts(self) -> pd.DataFrame:
        if self.assessments.empty:
            return self.assessments
        return self.assessments.loc[self.assessments["alertable"]].sort_values(
            "priority", ascending=False
        )

    def summary(self) -> str:
        c = self.counts
        return (
            f"{self.period_key}: {c.get('RED', 0)} red, {c.get('ORANGE', 0)} orange, "
            f"{c.get('YELLOW', 0)} yellow, {c.get('improvements', 0)} improvements "
            f"across {c.get('entities', 0)} entities"
        )


def assess(
    history: pd.DataFrame,
    registry: MetricRegistry,
    period: Period,
    latest_period: Period | None = None,
    trailing_window: int = 4,
    confidence: float = 0.90,
    basis: str = "period",
) -> pd.DataFrame:
    """Pure function: history in, fully judged assessment frame out."""
    # Build the wide matrix once and share it: comparison and anomaly scoring
    # both need exactly this layout, and pivoting 200k rows twice is pure waste.
    known = set(registry.keys)
    filtered = history.loc[history["metric_key"].isin(known)] if not history.empty else history
    matrix = build_matrix(filtered, period)
    if matrix is None:
        return pd.DataFrame(columns=ASSESSMENT_COLUMNS)

    comparison = build_comparison(
        history, registry, period, trailing_window=trailing_window, matrix=matrix
    )
    if comparison.empty:
        return pd.DataFrame(columns=ASSESSMENT_COLUMNS)

    anomalies = detect_anomalies(history, registry, period, matrix=matrix)
    if not anomalies.empty:
        comparison = comparison.merge(
            anomalies[
                ["entity_id", "metric_key", "baseline", "z_score", "relative_deviation",
                 "is_anomaly", "adverse"]
            ],
            on=["entity_id", "metric_key"],
            how="left",
        )
    else:
        for column in ("baseline", "z_score", "relative_deviation"):
            comparison[column] = np.nan
        comparison["is_anomaly"] = False
        comparison["adverse"] = False
    comparison["is_anomaly"] = comparison["is_anomaly"].fillna(False).astype(bool)
    comparison["adverse"] = comparison["adverse"].fillna(False).astype(bool)

    latest_period = latest_period or period
    records: list[dict] = []
    level_cache: dict[tuple[str, str], object] = {}
    for row in comparison.itertuples(index=False):
        base = registry.get(str(row.metric_key))
        if base is None:
            continue
        # Thresholds may be tuned per entity level (see MetricDefinition.for_level).
        cache_key = (str(row.metric_key), str(row.entity_type))
        metric = level_cache.get(cache_key)
        if metric is None:
            metric = base.for_level(str(row.entity_type))
            level_cache[cache_key] = metric
        verdict = judge(
            metric,
            current=_f(row.current),
            previous=_f(row.previous),
            delta=_f(row.delta),
            pct_change=_f(row.pct_change),
            trailing_avg=_f(row.trailing_avg),
            consecutive_adverse=int(row.consecutive_adverse or 0),
            streak_delta=_f(row.streak_delta),
            consecutive_favourable=int(row.consecutive_favourable or 0),
            favourable_streak_delta=_f(row.favourable_streak_delta),
            denominator=_f(row.denominator),
            previous_denominator=_f(row.previous_denominator),
            streak_start_denominator=_f(row.streak_start_denominator),
            confidence=confidence,
            anomaly_z=_f(row.z_score),
            anomaly_baseline=_f(row.baseline),
            anomaly_relative=_f(row.relative_deviation),
            is_anomaly=bool(row.is_anomaly),
            anomaly_adverse=bool(row.adverse),
            entity_label=str(row.entity_name),
            period=period,
            latest_period=latest_period,
        )
        records.append(
            {
                "entity_id": row.entity_id,
                "entity_type": row.entity_type,
                "entity_name": row.entity_name,
                "owner_id": row.owner_id,
                "owner_name": row.owner_name,
                "period_key": period.key,
                "metric_key": metric.key,
                "current": _f(row.current),
                "previous": _f(row.previous),
                "delta": _f(row.delta),
                "pct_change": _f(row.pct_change),
                "trailing_avg": _f(row.trailing_avg),
                "delta_vs_trailing": _f(row.delta_vs_trailing),
                "best": _f(row.best),
                "worst": _f(row.worst),
                "denominator": _f(row.denominator),
                "consecutive_adverse": int(row.consecutive_adverse or 0),
                "streak_delta": _f(row.streak_delta),
                "consecutive_favourable": int(row.consecutive_favourable or 0),
                "favourable_streak_delta": _f(row.favourable_streak_delta),
                "trend": row.trend,
                "z_score": _f(row.z_score),
                "baseline": _f(row.baseline),
                "is_anomaly": bool(row.is_anomaly),
                "severity": verdict.severity,
                "kind": verdict.kind,
                "threshold_status": verdict.threshold_status,
                "is_improvement": verdict.kind == KIND_IMPROVEMENT,
                "alertable": verdict.alertable,
                "suppressed": verdict.suppressed,
                "priority": verdict.priority,
                "basis": basis,
                "statistically_confirmed": verdict.statistically_confirmed,
                "headline": verdict.headline,
                "explanation": verdict.explanation,
            }
        )
    return pd.DataFrame(records, columns=ASSESSMENT_COLUMNS)


def evaluate_period(
    session: Session,
    period_key: str | None = None,
    *,
    registry: MetricRegistry | None = None,
    trailing_window: int = 4,
    rolling_window: int = 4,
    rolling_levels: tuple[str, ...] = (BA, TEAM, OWNER),
    confidence: float = 0.90,
    persist: bool = True,
    upload_id: int | None = None,
) -> EvaluationResult:
    """Evaluate one period end to end and (by default) persist the results.

    ``rolling_window`` > 1 evaluates the named levels over a trailing window of
    that many periods instead of a single one.  At this organisation's volumes
    a single week gives a BA three or four submissions, so BA-level weekly
    rates are arithmetic noise; four weeks is the smallest window that carries
    a usable signal.  Owner and organisation levels have the volume to be read
    weekly and are left alone by default.
    """
    registry = registry or get_registry()
    grain = dominant_grain(session) or "weekly"
    periods = available_periods(session, grain)
    if not periods:
        return EvaluationResult(period_key or "", grain, None, counts={})
    target_key = period_key or periods[-1]
    period = Period.from_key(target_key)
    latest_period = Period.from_key(periods[-1])

    # Source facts only: rolling up previously-derived Owner rows would
    # aggregate the aggregate.
    history = load_history(session, metric_keys=registry.keys, include_derived=False)
    history = history.loc[history["grain"] == grain]
    entities = load_entities(session)
    # Roll up BA facts to Owner/Org BEFORE any windowing, so the aggregate is
    # built from raw counts rather than from already-windowed rates.
    history = rollup_history(history, registry, entities, levels=(TEAM, OWNER, ORG))
    if persist:
        _persist_derived(session, history, period.key, grain)


    assessments = assess(
        history,
        registry,
        period,
        latest_period=latest_period,
        trailing_window=trailing_window,
        confidence=confidence,
        basis="period",
    )
    if rolling_window > 1:
        # Two bases, because they fail in opposite directions.  A single week
        # catches a sharp collapse but drowns in small-sample noise; a rolling
        # window is statistically sound but dilutes a one-week collapse to a
        # quarter of its size.  Each is judged on its own merits and the more
        # serious verdict wins, with the alert stating which view produced it.
        rolled = apply_rolling(history, registry, rolling_window, entity_types=rolling_levels)
        rolled_assessments = assess(
            rolled,
            registry,
            period,
            latest_period=latest_period,
            trailing_window=trailing_window,
            confidence=confidence,
            basis=f"rolling_{rolling_window}",
        )
        assessments = _merge_bases(assessments, rolled_assessments)
    assessments = group_family_alerts(assessments, registry)
    comparison_for_scores = assessments.rename(columns={"current": "current"})
    scores = score_entities(comparison_for_scores, registry, period, latest_period=latest_period)
    scores = _attach_alert_counts(scores, assessments)
    scores = _attach_previous_scores(session, scores, period)

    counts = _count(assessments)
    counts["entities"] = int(assessments["entity_id"].nunique()) if not assessments.empty else 0

    run_id = None
    if persist:
        run_id = _persist(session, period, grain, assessments, scores, counts, upload_id)
    return EvaluationResult(period.key, grain, run_id, assessments, scores, counts)


def _persist_derived(session: Session, history: pd.DataFrame, period_key: str, grain: str) -> int:
    """Store the rolled-up Owner/Org facts for the period under review.

    The rollup happens in memory for the alert engine anyway; writing it back
    means the trend charts, exports and any future consumer read the same
    aggregate the alerts were computed from, instead of each re-deriving it and
    quietly disagreeing.
    """
    if history.empty:
        return 0
    derived = history.loc[history["entity_type"].isin([TEAM, OWNER, ORG])]
    derived = derived.loc[derived["period_key"] == period_key]
    if derived.empty:
        return 0

    session.execute(
        update(Fact)
        .where(
            Fact.period_key == period_key,
            Fact.is_derived.is_(True),
            Fact.is_current.is_(True),
        )
        .values(is_current=False)
    )
    payload = [
        {
            "entity_id": int(row.entity_id),
            "period_key": period_key,
            "grain": grain,
            "metric_key": str(row.metric_key),
            "value": None if pd.isna(row.value) else float(row.value),
            "source_value": None,
            "denominator": None if pd.isna(row.denominator) else float(row.denominator),
            "upload_id": None,
            "is_derived": True,
            "is_current": True,
        }
        for row in derived.itertuples(index=False)
        if not pd.isna(row.value)
    ]
    if payload:
        session.bulk_insert_mappings(Fact, payload)
    return len(payload)


def _merge_bases(period_view: pd.DataFrame, rolling_view: pd.DataFrame) -> pd.DataFrame:
    """Keep the more serious verdict for each entity/metric across both bases.

    Ties break towards the rolling view, which rests on more data.  Rows that
    exist in only one basis are carried through unchanged.
    """
    if period_view.empty:
        return rolling_view
    if rolling_view.empty:
        return period_view
    combined = pd.concat([period_view, rolling_view], ignore_index=True)
    combined["_rank"] = combined["severity"].map(SEVERITY_ORDER).fillna(0)
    # An improvement is not "less severe than GREEN"; it is news in its own
    # right, so it outranks a plain GREEN when nothing is wrong.
    combined["_rank"] += combined["is_improvement"].astype(int) * 0.5
    combined["_basis_rank"] = (combined["basis"] != "period").astype(int)
    combined = combined.sort_values(
        ["_rank", "_basis_rank", "priority"], ascending=[False, False, False]
    )
    combined = combined.drop_duplicates(subset=["entity_id", "metric_key"], keep="first")
    return combined.drop(columns=["_rank", "_basis_rank"]).reset_index(drop=True)


def _f(value) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if np.isnan(number) else number


def _count(assessments: pd.DataFrame) -> dict[str, int]:
    counts = {RED: 0, ORANGE: 0, YELLOW: 0, GREEN: 0, "improvements": 0, "alerts": 0}
    if assessments.empty:
        return counts
    alertable = assessments.loc[assessments["alertable"]]
    for severity, n in alertable["severity"].value_counts().items():
        counts[str(severity)] = int(n)
    counts["improvements"] = int(alertable["is_improvement"].sum())
    counts["alerts"] = int(len(alertable))
    counts[GREEN] = int((assessments["severity"] == GREEN).sum())
    return counts


def _attach_alert_counts(scores: pd.DataFrame, assessments: pd.DataFrame) -> pd.DataFrame:
    if scores.empty:
        return scores
    for column in ("red_count", "orange_count", "yellow_count", "green_count"):
        scores[column] = 0
    if assessments.empty:
        return scores
    judged = assessments.loc[assessments["suppressed"].isna()]
    pivot = (
        judged.pivot_table(
            index="entity_id", columns="severity", values="metric_key", aggfunc="count"
        )
        .fillna(0)
        .astype(int)
    )
    mapping = {RED: "red_count", ORANGE: "orange_count", YELLOW: "yellow_count", GREEN: "green_count"}
    for severity, column in mapping.items():
        if severity in pivot.columns:
            scores[column] = scores["entity_id"].map(pivot[severity]).fillna(0).astype(int)
    return scores


def _attach_previous_scores(session: Session, scores: pd.DataFrame, period: Period) -> pd.DataFrame:
    if scores.empty:
        return scores
    previous_key = period.previous.key
    rows = session.execute(
        select(Score.entity_id, Score.score).where(
            Score.period_key == previous_key, Score.is_current.is_(True)
        )
    ).all()
    lookup = {entity_id: value for entity_id, value in rows}
    scores["previous_score"] = scores["entity_id"].map(lookup)
    scores["delta"] = (scores["score"] - scores["previous_score"]).round(2)
    return scores


def _persist(
    session: Session,
    period: Period,
    grain: str,
    assessments: pd.DataFrame,
    scores: pd.DataFrame,
    counts: dict[str, int],
    upload_id: int | None,
) -> int:
    """Write a new run, superseding any previous run for the same period."""
    session.execute(
        update(AlertRun)
        .where(AlertRun.period_key == period.key, AlertRun.is_current.is_(True))
        .values(is_current=False)
    )
    session.execute(
        update(Score)
        .where(Score.period_key == period.key, Score.is_current.is_(True))
        .values(is_current=False)
    )
    run = AlertRun(
        period_key=period.key,
        grain=grain,
        upload_id=upload_id,
        alert_count=counts.get("alerts", 0),
        red_count=counts.get(RED, 0),
        orange_count=counts.get(ORANGE, 0),
        yellow_count=counts.get(YELLOW, 0),
        improvement_count=counts.get("improvements", 0),
        is_current=True,
    )
    session.add(run)
    session.flush()

    previous_keys = _previous_alert_keys(session, period)
    alertable = assessments.loc[assessments["alertable"]] if not assessments.empty else assessments
    payload = []
    for row in alertable.itertuples(index=False):
        payload.append(
            {
                "run_id": run.id,
                "entity_id": int(row.entity_id),
                "entity_type": str(row.entity_type),
                "period_key": period.key,
                "metric_key": str(row.metric_key),
                "severity": str(row.severity),
                "kind": str(row.kind),
                "current_value": row.current,
                "previous_value": row.previous,
                "delta": row.delta,
                "pct_change": row.pct_change,
                "baseline_value": row.baseline,
                "z_score": row.z_score,
                "threshold_status": str(row.threshold_status),
                "trend": str(row.trend),
                "basis": str(getattr(row, "basis", "period")),
                "statistically_confirmed": bool(getattr(row, "statistically_confirmed", True)),
                "consecutive_declines": int(row.consecutive_adverse or 0),
                "is_improvement": bool(row.is_improvement),
                "is_new": (int(row.entity_id), str(row.metric_key)) not in previous_keys,
                "priority": float(row.priority or 0.0),
                "headline": str(row.headline),
                "explanation": str(row.explanation),
            }
        )
    if payload:
        session.bulk_insert_mappings(Alert, payload)

    score_payload = []
    for row in scores.itertuples(index=False):
        score_payload.append(
            {
                "entity_id": int(row.entity_id),
                "entity_type": str(row.entity_type),
                "period_key": period.key,
                "score": _f(row.score),
                "band": row.band,
                "previous_score": _f(getattr(row, "previous_score", None)),
                "delta": _f(getattr(row, "delta", None)),
                "coverage": _f(row.coverage),
                "metrics_used": int(row.metrics_used or 0),
                "red_count": int(getattr(row, "red_count", 0) or 0),
                "orange_count": int(getattr(row, "orange_count", 0) or 0),
                "yellow_count": int(getattr(row, "yellow_count", 0) or 0),
                "green_count": int(getattr(row, "green_count", 0) or 0),
                "run_id": run.id,
                "is_current": True,
            }
        )
    if score_payload:
        session.bulk_insert_mappings(Score, score_payload)
    session.flush()
    return run.id


def _previous_alert_keys(session: Session, period: Period) -> set[tuple[int, str]]:
    """Which (entity, metric) pairs already alerted last period.

    Used to mark an alert "new this week" - a manager needs to tell a fresh
    problem apart from one they were already told about.
    """
    rows = session.execute(
        select(Alert.entity_id, Alert.metric_key)
        .join(AlertRun, AlertRun.id == Alert.run_id)
        .where(
            Alert.period_key == period.previous.key,
            AlertRun.is_current.is_(True),
            Alert.is_improvement.is_(False),
        )
    ).all()
    return {(int(a), str(b)) for a, b in rows}
