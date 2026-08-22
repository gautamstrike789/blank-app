"""Notification routing.

Two jobs, kept apart:

* **Routing** decides *who* should be told *what*, and - just as importantly -
  what should not be sent.  An Owner is told about their own team.  Management
  is told about the organisation.  Nobody is told twice about the same problem
  in consecutive weeks unless it got worse.
* **Channels** deliver a built digest.  Email, Slack, Teams and the console are
  interchangeable implementations of :class:`Notifier`.

The suppression rules exist because a system that emails 44 Owners every time a
number moves 0.2% gets filtered to junk in a fortnight, after which it is
worthless no matter how good the analysis behind it is.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Iterable, Sequence

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from qmis.core.metric_config import MetricRegistry
from qmis.core.models import (
    GREEN,
    ORANGE,
    RED,
    SEVERITY_ORDER,
    YELLOW,
    Alert,
    AlertRun,
    Entity,
    NotificationLog,
    User,
)
from qmis.core.periods import Period


@dataclass
class Digest:
    """One message for one recipient."""

    recipient: str
    name: str
    role: str
    subject: str
    body: str
    alert_count: int
    red_count: int
    channel_hint: str | None = None
    alert_ids: list[int] = field(default_factory=list)


class Notifier(ABC):
    """A delivery channel."""

    kind: str = "abstract"

    @abstractmethod
    def send(self, digest: Digest) -> None:
        """Deliver one digest, or raise."""

    def close(self) -> None:  # pragma: no cover - optional hook
        return None


# --------------------------------------------------------------------------- #
# routing
# --------------------------------------------------------------------------- #
def select_notifiable(
    session: Session,
    alerts: pd.DataFrame,
    period: Period,
    send_severities: Sequence[str] = (RED, ORANGE),
    repeat_after_periods: int = 3,
) -> pd.DataFrame:
    """Filter an alert frame down to what is worth sending.

    Kept: anything at a notifiable severity that is either new, or has become
    more serious than when it was last sent, or was last sent long enough ago.
    """
    if alerts.empty:
        return alerts
    keep = alerts.loc[alerts["severity"].isin(list(send_severities))].copy()
    if keep.empty:
        return keep

    history = _previous_severities(session, period, repeat_after_periods)
    if not history:
        return keep

    def still_worth_sending(row) -> bool:
        key = (int(row.entity_id), str(row.metric_key))
        previous = history.get(key)
        if previous is None:
            return True
        last_severity, periods_ago = previous
        if SEVERITY_ORDER.get(row.severity, 0) > SEVERITY_ORDER.get(last_severity, 0):
            return True  # it got worse - say so
        return periods_ago >= repeat_after_periods

    mask = [still_worth_sending(row) for row in keep.itertuples(index=False)]
    return keep.loc[mask]


def _previous_severities(
    session: Session, period: Period, lookback: int
) -> dict[tuple[int, str], tuple[str, int]]:
    """Worst severity previously *notified* per entity/metric, and how long ago."""
    keys = [period.shift(-i).key for i in range(1, max(1, lookback) + 1)]
    rows = session.execute(
        select(Alert.entity_id, Alert.metric_key, Alert.severity, Alert.period_key)
        .join(AlertRun, AlertRun.id == Alert.run_id)
        .where(Alert.period_key.in_(keys), AlertRun.is_current.is_(True))
    ).all()
    out: dict[tuple[int, str], tuple[str, int]] = {}
    for entity_id, metric_key, severity, period_key in rows:
        try:
            ago = period.distance(Period.from_key(period_key))
        except Exception:
            continue
        key = (int(entity_id), str(metric_key))
        current = out.get(key)
        if current is None or ago < current[1]:
            out[key] = (str(severity), ago)
    return out


def build_digests(
    session: Session,
    alerts: pd.DataFrame,
    period: Period,
    registry: MetricRegistry,
    max_alerts: int = 15,
    send_improvements: bool = True,
    max_improvements: int = 3,
    improvements: pd.DataFrame | None = None,
) -> list[Digest]:
    """Turn a filtered alert frame into per-recipient messages."""
    users = list(
        session.execute(select(User).where(User.active.is_(True), User.notify_email.is_(True)))
        .scalars()
    )
    if not users or alerts.empty:
        return []

    descendants = _descendant_map(session)
    digests: list[Digest] = []
    for user in users:
        if user.role == "owner":
            if user.owner_entity_id is None:
                continue
            scope = descendants.get(user.owner_entity_id, {user.owner_entity_id})
            mine = alerts.loc[alerts["entity_id"].isin(scope)]
            good = (
                improvements.loc[improvements["entity_id"].isin(scope)]
                if improvements is not None and not improvements.empty
                else None
            )
            title = f"Quality alert - your team - {period.label}"
        else:
            mine = alerts
            good = improvements
            title = f"Quality alert - organisation - {period.label}"
        if mine.empty:
            continue
        body = render_digest(
            user.name, period, mine, registry, max_alerts,
            good if send_improvements else None, max_improvements,
        )
        digests.append(
            Digest(
                recipient=user.email,
                name=user.name,
                role=user.role,
                subject=f"{title}: {int((mine['severity'] == RED).sum())} critical, {len(mine)} total",
                body=body,
                alert_count=len(mine),
                red_count=int((mine["severity"] == RED).sum()),
                channel_hint=user.notify_channel,
                alert_ids=[int(i) for i in mine.get("id", pd.Series(dtype=int)).tolist()],
            )
        )
    return digests


def _descendant_map(session: Session) -> dict[int, set[int]]:
    rows = session.execute(select(Entity.id, Entity.parent_id)).all()
    children: dict[int, list[int]] = {}
    for entity_id, parent_id in rows:
        if parent_id is not None:
            children.setdefault(int(parent_id), []).append(int(entity_id))
    out: dict[int, set[int]] = {}
    for entity_id, _ in rows:
        seen = {int(entity_id)}
        frontier = [int(entity_id)]
        while frontier:
            nxt: list[int] = []
            for node in frontier:
                for child in children.get(node, []):
                    if child not in seen:
                        seen.add(child)
                        nxt.append(child)
            frontier = nxt
        out[int(entity_id)] = seen
    return out


def render_digest(
    name: str,
    period: Period,
    alerts: pd.DataFrame,
    registry: MetricRegistry,
    max_alerts: int,
    improvements: pd.DataFrame | None,
    max_improvements: int,
) -> str:
    """Plain-text digest. Readable in an email, a Slack post or a terminal."""
    reds = alerts.loc[alerts["severity"] == RED]
    oranges = alerts.loc[alerts["severity"] == ORANGE]
    lines = [
        f"WEEKLY QUALITY ALERT - {period.label}",
        "",
        f"Hello {name},",
        "",
        f"{len(reds)} critical and {len(oranges)} high-attention findings need review.",
        "",
    ]
    shown = alerts.sort_values("priority", ascending=False).head(max_alerts)
    for i, row in enumerate(shown.itertuples(index=False), 1):
        lines.append(f"{i}. {row.headline}")
        lines.append(f"   {row.explanation}")
        lines.append("")
    if len(alerts) > max_alerts:
        lines.append(f"...and {len(alerts) - max_alerts} more in the dashboard.")
        lines.append("")
    if improvements is not None and not improvements.empty:
        lines.append("Improving:")
        for row in improvements.head(max_improvements).itertuples(index=False):
            lines.append(f"  - {row.headline}")
        lines.append("")
    lines.append("Open the dashboard for the full picture, trends and drill-down.")
    return "\n".join(lines)


def dispatch(
    session: Session,
    digests: Iterable[Digest],
    notifiers: Sequence[Notifier],
    run_id: int | None = None,
    dry_run: bool = False,
) -> list[NotificationLog]:
    """Send each digest through every configured channel, logging the outcome."""
    logs: list[NotificationLog] = []
    for digest in digests:
        for notifier in notifiers:
            log = NotificationLog(
                run_id=run_id,
                channel=notifier.kind,
                recipient=digest.recipient,
                subject=digest.subject,
                body=digest.body,
                alert_count=digest.alert_count,
                status="skipped" if dry_run else "sent",
            )
            if not dry_run:
                try:
                    notifier.send(digest)
                except Exception as exc:  # a broken channel must not stop the rest
                    log.status = "failed"
                    log.error = f"{type(exc).__name__}: {exc}"
            session.add(log)
            logs.append(log)
    session.flush()
    return logs
