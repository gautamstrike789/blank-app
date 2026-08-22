"""Folder automation.

The workflow the organisation actually wants is: drop the week's file in the
shared folder, and everything else happens.  This module is that "everything
else" - poll, ingest, evaluate, notify - packaged so it can run from cron, a
systemd timer, a Windows scheduled task, or a button in the dashboard.

Polling rather than filesystem events, on purpose.  A weekly file does not need
sub-second detection, and inotify/ReadDirectoryChangesW do not fire reliably
across network shares or cloud-sync folders - which is exactly where this file
will live.  A poll every few minutes is dull and it works.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Sequence

from sqlalchemy.orm import Session

from qmis.core.config import Settings, load_settings
from qmis.core.metric_config import MetricRegistry, get_registry
from qmis.core.periods import Period
from qmis.ingest.pipeline import IngestResult, ingest_from_storage
from qmis.ingest.storage import StorageBackend, build_storage

log = logging.getLogger("qmis.watcher")


@dataclass
class CycleResult:
    """What one poll did."""

    ingested: list[IngestResult] = field(default_factory=list)
    evaluated_periods: list[str] = field(default_factory=list)
    notifications_sent: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def accepted(self) -> list[IngestResult]:
        return [r for r in self.ingested if r.accepted]

    @property
    def rejected(self) -> list[IngestResult]:
        return [r for r in self.ingested if not r.accepted]

    def summary(self) -> str:
        if not self.ingested:
            return "nothing new"
        return (
            f"{len(self.accepted)} loaded, {len(self.rejected)} rejected, "
            f"{len(self.evaluated_periods)} period(s) evaluated, "
            f"{self.notifications_sent} notification(s)"
        )


def run_cycle(
    session: Session,
    *,
    storage: StorageBackend | None = None,
    settings: Settings | None = None,
    registry: MetricRegistry | None = None,
    evaluate: bool = True,
    notify: bool | None = None,
    dry_run_notifications: bool = False,
) -> CycleResult:
    """Process everything waiting in the inbox, then evaluate and notify.

    Evaluation runs once per affected period rather than once per file, because
    a backfill of six weeks in one drop should produce six evaluations, not
    thirty-six.
    """
    settings = settings or load_settings()
    registry = registry or get_registry()
    storage = storage or build_storage(settings.section("storage"))
    result = CycleResult()

    result.ingested = ingest_from_storage(
        session,
        storage,
        registry=registry,
        archive=bool(settings.get("storage.archive_after_load", True)),
        uploaded_by="watcher",
    )
    for rejected in result.rejected:
        log.warning("rejected %s: %s", rejected.filename, rejected.message)

    if not evaluate:
        return result

    affected = sorted({p for r in result.accepted for p in r.periods})
    if not affected:
        return result

    from qmis.analytics.engine import evaluate_period  # imported late: heavy

    for period_key in affected:
        try:
            evaluation = evaluate_period(
                session,
                period_key,
                registry=registry,
                trailing_window=int(settings.get("evaluation.trailing_window", 4)),
                rolling_window=int(settings.get("evaluation.rolling_window", 4)),
                rolling_levels=tuple(settings.get("evaluation.rolling_levels", ["ba"])),
                confidence=float(settings.get("evaluation.confidence", 0.90)),
            )
            result.evaluated_periods.append(period_key)
        except Exception as exc:  # one bad period must not block the others
            log.exception("evaluation failed for %s", period_key)
            result.errors.append(f"{period_key}: {type(exc).__name__}: {exc}")
            continue

        should_notify = (
            settings.get("notifications.enabled", False) if notify is None else notify
        )
        if should_notify and period_key == affected[-1]:
            result.notifications_sent += _notify(
                session, settings, registry, period_key, evaluation, dry_run_notifications
            )
    return result


def _notify(
    session: Session,
    settings: Settings,
    registry: MetricRegistry,
    period_key: str,
    evaluation,
    dry_run: bool,
) -> int:
    from qmis.analytics.repository import load_alerts
    from qmis.notify import build_digests, build_notifiers, dispatch, select_notifiable
    from qmis.notify.channels import ConsoleNotifier

    period = Period.from_key(period_key)
    alerts = load_alerts(session, period_key=period_key, include_improvements=False)
    improvements = load_alerts(session, period_key=period_key)
    if not improvements.empty:
        improvements = improvements.loc[improvements["is_improvement"]]

    notifiable = select_notifiable(
        session,
        alerts,
        period,
        send_severities=tuple(settings.get("notifications.send_severities", ["RED"])),
        repeat_after_periods=int(settings.get("notifications.repeat_after_periods", 3)),
    )
    if notifiable.empty:
        return 0
    digests = build_digests(
        session,
        notifiable,
        period,
        registry,
        max_alerts=int(settings.get("alerts.max_alerts_per_digest", 15)),
        send_improvements=bool(settings.get("notifications.send_improvements", True)),
        max_improvements=int(settings.get("notifications.max_improvements", 3)),
        improvements=improvements,
    )
    channels = settings.get("notifications.channels") or []
    notifiers = build_notifiers(channels) if channels else [ConsoleNotifier()]
    run_id = getattr(evaluation, "run_id", None)
    dispatch(session, digests, notifiers, run_id=run_id, dry_run=dry_run)
    return len(digests)
