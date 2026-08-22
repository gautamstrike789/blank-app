"""Database schema.

Design rules that the rest of the system depends on:

* **Nothing is ever overwritten.**  A re-uploaded week does not update rows; it
  inserts a new generation of facts and flips ``is_current`` on the old ones, so
  a correction is auditable and reversible.
* **One fact table, long format** (entity x period x metric x value).  Adding a
  metric never means adding a column, which is what makes a 42-metric registry
  configurable rather than a migration.
* **Entities are self-referencing.**  Org -> Owner -> BA is a parent chain, so
  a team level can be inserted later without a schema change.
* Types stay portable: no JSON columns, no arrays, no dialect-specific
  defaults.  The same DDL runs on SQLite today and PostgreSQL when the volume
  or the concurrency justifies it.
"""

from __future__ import annotations

import datetime as _dt
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# Entity levels ---------------------------------------------------------------
ORG = "org"
OWNER = "owner"
BA = "ba"
TEAM = "team"
ENTITY_TYPES = (ORG, OWNER, TEAM, BA)

# Upload lifecycle ------------------------------------------------------------
UPLOAD_PENDING = "pending"
UPLOAD_VALIDATED = "validated"
UPLOAD_REJECTED = "rejected"
UPLOAD_LOADED = "loaded"
UPLOAD_SUPERSEDED = "superseded"

# Issue severities ------------------------------------------------------------
ISSUE_ERROR = "error"
ISSUE_WARNING = "warning"
ISSUE_INFO = "info"

# Alert severities ------------------------------------------------------------
GREEN = "GREEN"
YELLOW = "YELLOW"
ORANGE = "ORANGE"
RED = "RED"
SEVERITY_ORDER = {GREEN: 0, YELLOW: 1, ORANGE: 2, RED: 3}

# Alert kinds -----------------------------------------------------------------
KIND_THRESHOLD = "threshold"
KIND_CHANGE = "change"
KIND_TREND = "trend"
KIND_ANOMALY = "anomaly"
KIND_IMPROVEMENT = "improvement"
KIND_COMPOSITE = "composite"

# Roles -----------------------------------------------------------------------
ROLE_ADMIN = "admin"
ROLE_MANAGEMENT = "management"
ROLE_OWNER = "owner"
ROLES = (ROLE_ADMIN, ROLE_MANAGEMENT, ROLE_OWNER)


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


class Base(DeclarativeBase):
    pass


class Entity(Base):
    """An organisation, owner, team or business associate."""

    __tablename__ = "entities"
    __table_args__ = (
        UniqueConstraint("entity_type", "normalised_name", name="uq_entity_type_name"),
        Index("ix_entity_parent", "parent_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entity_type: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    normalised_name: Mapped[str] = mapped_column(String(255), nullable=False)
    external_ref: Mapped[Optional[str]] = mapped_column(String(128))
    parent_id: Mapped[Optional[int]] = mapped_column(ForeignKey("entities.id"))
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    first_seen_period: Mapped[Optional[str]] = mapped_column(String(16))
    last_seen_period: Mapped[Optional[str]] = mapped_column(String(16))
    created_at: Mapped[_dt.datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)

    parent: Mapped[Optional["Entity"]] = relationship(remote_side=[id], back_populates="children")
    children: Mapped[list["Entity"]] = relationship(back_populates="parent")

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<Entity {self.entity_type}:{self.name}>"


class Upload(Base):
    """One ingested source file."""

    __tablename__ = "uploads"
    __table_args__ = (Index("ix_upload_period", "period_key", "status"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    source_uri: Mapped[Optional[str]] = mapped_column(String(1024))
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    grain: Mapped[str] = mapped_column(String(16), nullable=False)
    period_key: Mapped[Optional[str]] = mapped_column(String(16))
    period_min: Mapped[Optional[str]] = mapped_column(String(16))
    period_max: Mapped[Optional[str]] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default=UPLOAD_PENDING, nullable=False)
    profile: Mapped[Optional[str]] = mapped_column(String(64))
    sheet_name: Mapped[Optional[str]] = mapped_column(String(255))
    row_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    fact_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    entity_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    uploaded_by: Mapped[Optional[str]] = mapped_column(String(255))
    uploaded_at: Mapped[_dt.datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    notes: Mapped[Optional[str]] = mapped_column(Text)
    superseded_by_id: Mapped[Optional[int]] = mapped_column(ForeignKey("uploads.id"))

    issues: Mapped[list["ValidationIssue"]] = relationship(
        back_populates="upload", cascade="all, delete-orphan"
    )


class ValidationIssue(Base):
    """A single finding from the pre-load validation pass."""

    __tablename__ = "validation_issues"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    upload_id: Mapped[int] = mapped_column(ForeignKey("uploads.id"), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    column_name: Mapped[Optional[str]] = mapped_column(String(255))
    row_ref: Mapped[Optional[str]] = mapped_column(String(128))
    sample: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[_dt.datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)

    upload: Mapped[Upload] = relationship(back_populates="issues")


class Fact(Base):
    """One metric value for one entity in one period.

    ``is_current`` implements non-destructive corrections: reprocessing a week
    inserts a fresh generation and demotes the previous one rather than
    deleting it.
    """

    __tablename__ = "facts"
    __table_args__ = (
        Index("ix_fact_lookup", "entity_id", "metric_key", "period_key", "is_current"),
        Index("ix_fact_period", "period_key", "is_current"),
        Index("ix_fact_metric", "metric_key", "period_key", "is_current"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entity_id: Mapped[int] = mapped_column(ForeignKey("entities.id"), nullable=False)
    period_key: Mapped[str] = mapped_column(String(16), nullable=False)
    grain: Mapped[str] = mapped_column(String(16), nullable=False)
    metric_key: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[Optional[float]] = mapped_column(Float)
    source_value: Mapped[Optional[float]] = mapped_column(Float)
    denominator: Mapped[Optional[float]] = mapped_column(Float)
    upload_id: Mapped[Optional[int]] = mapped_column(ForeignKey("uploads.id"), index=True)
    # True for Owner/Org rows the rollup computed from BA facts rather than
    # read from a file. Stored rather than recomputed on every read so that
    # trends, exports and alerts all quote the same numbers.
    is_derived: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[_dt.datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)


class AlertRun(Base):
    """One execution of the alert engine over one period."""

    __tablename__ = "alert_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    period_key: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    grain: Mapped[str] = mapped_column(String(16), nullable=False)
    upload_id: Mapped[Optional[int]] = mapped_column(ForeignKey("uploads.id"))
    config_fingerprint: Mapped[Optional[str]] = mapped_column(String(64))
    alert_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    red_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    orange_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    yellow_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    improvement_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[_dt.datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)


class Alert(Base):
    """A generated, explainable alert."""

    __tablename__ = "alerts"
    __table_args__ = (
        Index("ix_alert_run", "run_id", "severity"),
        Index("ix_alert_entity", "entity_id", "period_key"),
        Index("ix_alert_metric", "metric_key", "period_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("alert_runs.id"), nullable=False)
    entity_id: Mapped[int] = mapped_column(ForeignKey("entities.id"), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(16), nullable=False)
    period_key: Mapped[str] = mapped_column(String(16), nullable=False)
    metric_key: Mapped[str] = mapped_column(String(64), nullable=False)
    severity: Mapped[str] = mapped_column(String(8), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    current_value: Mapped[Optional[float]] = mapped_column(Float)
    previous_value: Mapped[Optional[float]] = mapped_column(Float)
    delta: Mapped[Optional[float]] = mapped_column(Float)
    pct_change: Mapped[Optional[float]] = mapped_column(Float)
    baseline_value: Mapped[Optional[float]] = mapped_column(Float)
    z_score: Mapped[Optional[float]] = mapped_column(Float)
    threshold_status: Mapped[Optional[str]] = mapped_column(String(16))
    trend: Mapped[Optional[str]] = mapped_column(String(24))
    basis: Mapped[Optional[str]] = mapped_column(String(24))
    statistically_confirmed: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    consecutive_declines: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_improvement: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_new: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    priority: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    headline: Mapped[str] = mapped_column(String(512), nullable=False)
    explanation: Mapped[str] = mapped_column(Text, nullable=False)
    acknowledged_at: Mapped[Optional[_dt.datetime]] = mapped_column(DateTime)
    acknowledged_by: Mapped[Optional[str]] = mapped_column(String(255))
    acknowledgement_note: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[_dt.datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)


class Score(Base):
    """Weighted quality score for an entity in a period."""

    __tablename__ = "scores"
    __table_args__ = (
        Index("ix_score_lookup", "entity_id", "period_key", "is_current"),
        Index("ix_score_period", "period_key", "is_current"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entity_id: Mapped[int] = mapped_column(ForeignKey("entities.id"), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(16), nullable=False)
    period_key: Mapped[str] = mapped_column(String(16), nullable=False)
    score: Mapped[Optional[float]] = mapped_column(Float)
    band: Mapped[Optional[str]] = mapped_column(String(32))
    previous_score: Mapped[Optional[float]] = mapped_column(Float)
    delta: Mapped[Optional[float]] = mapped_column(Float)
    coverage: Mapped[Optional[float]] = mapped_column(Float)
    metrics_used: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    red_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    orange_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    yellow_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    green_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    run_id: Mapped[Optional[int]] = mapped_column(ForeignKey("alert_runs.id"))
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[_dt.datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)


class User(Base):
    """An authorised user and the slice of the organisation they may see."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    owner_entity_id: Mapped[Optional[int]] = mapped_column(ForeignKey("entities.id"))
    notify_email: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    notify_channel: Mapped[Optional[str]] = mapped_column(String(64))
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[_dt.datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)


class Setting(Base):
    """Small key/value store for runtime settings the admin can change."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[_dt.datetime] = mapped_column(
        DateTime, default=_utcnow, onupdate=_utcnow, nullable=False
    )


class NotificationLog(Base):
    """What was sent, to whom, for which run - the anti-spam ledger."""

    __tablename__ = "notification_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[Optional[int]] = mapped_column(ForeignKey("alert_runs.id"))
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    recipient: Mapped[str] = mapped_column(String(255), nullable=False)
    subject: Mapped[str] = mapped_column(String(512), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    alert_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="sent", nullable=False)
    error: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[_dt.datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
