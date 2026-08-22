"""Read-side queries.

One place builds the history frame every analytical component consumes, so the
alert engine, the scoring engine and the dashboard cannot drift apart in what
"current data" means.
"""

from __future__ import annotations

from typing import Iterable, Sequence

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session, aliased

from qmis.core.models import BA, ORG, OWNER, Alert, AlertRun, Entity, Fact, Score

HISTORY_COLUMNS = [
    "entity_id",
    "entity_type",
    "entity_name",
    "owner_id",
    "owner_name",
    "period_key",
    "grain",
    "metric_key",
    "value",
    "denominator",
]


def load_history(
    session: Session,
    period_keys: Sequence[str] | None = None,
    metric_keys: Sequence[str] | None = None,
    entity_types: Sequence[str] | None = None,
    entity_ids: Sequence[int] | None = None,
    include_derived: bool = True,
) -> pd.DataFrame:
    """Current-generation facts, joined to their entity and owning parent."""
    parent = aliased(Entity)
    stmt = (
        select(
            Fact.entity_id,
            Entity.entity_type,
            Entity.name.label("entity_name"),
            Entity.parent_id.label("owner_id"),
            parent.name.label("owner_name"),
            Fact.period_key,
            Fact.grain,
            Fact.metric_key,
            Fact.value,
            Fact.denominator,
        )
        .join(Entity, Entity.id == Fact.entity_id)
        .join(parent, parent.id == Entity.parent_id, isouter=True)
        .where(Fact.is_current.is_(True))
    )
    if not include_derived:
        stmt = stmt.where(Fact.is_derived.is_(False))
    if period_keys:
        stmt = stmt.where(Fact.period_key.in_(list(period_keys)))
    if metric_keys:
        stmt = stmt.where(Fact.metric_key.in_(list(metric_keys)))
    if entity_types:
        stmt = stmt.where(Entity.entity_type.in_(list(entity_types)))
    if entity_ids:
        stmt = stmt.where(Fact.entity_id.in_(list(entity_ids)))

    rows = session.execute(stmt).all()
    frame = pd.DataFrame(rows, columns=HISTORY_COLUMNS)
    if frame.empty:
        return pd.DataFrame(columns=HISTORY_COLUMNS)
    frame["owner_name"] = frame["owner_name"].fillna("")
    return frame


def available_periods(session: Session, grain: str | None = None) -> list[str]:
    stmt = select(Fact.period_key).where(Fact.is_current.is_(True)).distinct()
    if grain:
        stmt = stmt.where(Fact.grain == grain)
    return sorted({key for (key,) in session.execute(stmt)})


def latest_period(session: Session, grain: str | None = None) -> str | None:
    periods = available_periods(session, grain)
    return periods[-1] if periods else None


def dominant_grain(session: Session) -> str | None:
    row = session.execute(
        select(Fact.grain, func.count())
        .where(Fact.is_current.is_(True))
        .group_by(Fact.grain)
        .order_by(func.count().desc())
        .limit(1)
    ).first()
    return row[0] if row else None


def load_entities(session: Session, entity_type: str | None = None) -> pd.DataFrame:
    parent = aliased(Entity)
    stmt = select(
        Entity.id,
        Entity.entity_type,
        Entity.name,
        Entity.parent_id,
        parent.name.label("parent_name"),
        Entity.active,
        Entity.first_seen_period,
        Entity.last_seen_period,
    ).join(parent, parent.id == Entity.parent_id, isouter=True)
    if entity_type:
        stmt = stmt.where(Entity.entity_type == entity_type)
    rows = session.execute(stmt).all()
    return pd.DataFrame(
        rows,
        columns=[
            "id", "entity_type", "name", "parent_id", "parent_name",
            "active", "first_seen_period", "last_seen_period",
        ],
    )


def current_run(session: Session, period_key: str) -> AlertRun | None:
    return session.execute(
        select(AlertRun)
        .where(AlertRun.period_key == period_key, AlertRun.is_current.is_(True))
        .order_by(AlertRun.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def load_alerts(
    session: Session,
    period_key: str | None = None,
    severities: Sequence[str] | None = None,
    entity_types: Sequence[str] | None = None,
    entity_ids: Sequence[int] | None = None,
    include_improvements: bool = True,
) -> pd.DataFrame:
    parent = aliased(Entity)
    stmt = (
        select(
            Alert.id,
            Alert.period_key,
            Alert.entity_id,
            Entity.name.label("entity_name"),
            Alert.entity_type,
            Entity.parent_id.label("owner_id"),
            parent.name.label("owner_name"),
            Alert.metric_key,
            Alert.severity,
            Alert.kind,
            Alert.current_value,
            Alert.previous_value,
            Alert.delta,
            Alert.pct_change,
            Alert.baseline_value,
            Alert.z_score,
            Alert.threshold_status,
            Alert.trend,
            Alert.basis,
            Alert.statistically_confirmed,
            Alert.consecutive_declines,
            Alert.is_improvement,
            Alert.is_new,
            Alert.priority,
            Alert.headline,
            Alert.explanation,
            Alert.acknowledged_at,
            Alert.acknowledged_by,
        )
        .join(AlertRun, AlertRun.id == Alert.run_id)
        .join(Entity, Entity.id == Alert.entity_id)
        .join(parent, parent.id == Entity.parent_id, isouter=True)
        .where(AlertRun.is_current.is_(True))
    )
    if period_key:
        stmt = stmt.where(Alert.period_key == period_key)
    if severities:
        stmt = stmt.where(Alert.severity.in_(list(severities)))
    if entity_types:
        stmt = stmt.where(Alert.entity_type.in_(list(entity_types)))
    if entity_ids:
        stmt = stmt.where(Alert.entity_id.in_(list(entity_ids)))
    if not include_improvements:
        stmt = stmt.where(Alert.is_improvement.is_(False))
    stmt = stmt.order_by(Alert.priority.desc())
    rows = session.execute(stmt).all()
    columns = [
        "id", "period_key", "entity_id", "entity_name", "entity_type", "owner_id",
        "owner_name", "metric_key", "severity", "kind", "current_value", "previous_value",
        "delta", "pct_change", "baseline_value", "z_score", "threshold_status", "trend",
        "basis", "statistically_confirmed", "consecutive_declines", "is_improvement", "is_new", "priority", "headline",
        "explanation", "acknowledged_at", "acknowledged_by",
    ]
    frame = pd.DataFrame(rows, columns=columns)
    if not frame.empty:
        frame["owner_name"] = frame["owner_name"].fillna("")
    return frame


def load_scores(
    session: Session,
    period_keys: Sequence[str] | None = None,
    entity_types: Sequence[str] | None = None,
) -> pd.DataFrame:
    parent = aliased(Entity)
    stmt = (
        select(
            Score.entity_id,
            Entity.name.label("entity_name"),
            Score.entity_type,
            parent.name.label("owner_name"),
            Score.period_key,
            Score.score,
            Score.band,
            Score.previous_score,
            Score.delta,
            Score.coverage,
            Score.metrics_used,
            Score.red_count,
            Score.orange_count,
            Score.yellow_count,
            Score.green_count,
        )
        .join(Entity, Entity.id == Score.entity_id)
        .join(parent, parent.id == Entity.parent_id, isouter=True)
        .where(Score.is_current.is_(True))
    )
    if period_keys:
        stmt = stmt.where(Score.period_key.in_(list(period_keys)))
    if entity_types:
        stmt = stmt.where(Score.entity_type.in_(list(entity_types)))
    rows = session.execute(stmt).all()
    frame = pd.DataFrame(
        rows,
        columns=[
            "entity_id", "entity_name", "entity_type", "owner_name", "period_key", "score",
            "band", "previous_score", "delta", "coverage", "metrics_used", "red_count",
            "orange_count", "yellow_count", "green_count",
        ],
    )
    if not frame.empty:
        frame["owner_name"] = frame["owner_name"].fillna("")
    return frame
