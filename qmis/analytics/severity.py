"""Severity classification and the human-readable explanation that goes with it.

Colour on its own is not information.  Every alert this module produces carries
the sentence a manager would otherwise have to reconstruct from two
spreadsheets: what moved, by how much, against which rule, and whether it has
been moving that way for a while.

The classification is a documented decision table rather than a score cut-off,
so an alert can always be argued with:

  RED     the metric is past its critical threshold; or it is past its warning
          threshold *and* deteriorating severely; or a severe deterioration
          coincides with a statistical anomaly; or it has deteriorated for the
          configured number of consecutive periods while past its warning line.
  ORANGE  a severe deterioration; or past the warning threshold and still
          falling; or a sustained decline; or an adverse anomaly with an
          ordinary deterioration.
  YELLOW  past the warning threshold but stable; or an ordinary significant
          deterioration; or an adverse anomaly on its own.
  GREEN   everything else, including improvements.

Two guards sit in front of the table and stop the system crying wolf:

* **Sample size.** A rate computed on fewer than ``min_denominator``
  submissions is not evidence.  It is recorded, and excluded from alerting.
* **Maturity.** Debit 3 reads 0% for the newest periods because those donors
  have not reached a third debit yet, not because quality collapsed.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from qmis.core.metric_config import (
    CRITICAL,
    HIGHER_IS_BETTER,
    LOWER_IS_BETTER,
    NEUTRAL,
    OK,
    UNKNOWN,
    WARNING,
    MetricDefinition,
    MetricRegistry,
)
from qmis.core.models import (
    GREEN,
    KIND_ANOMALY,
    KIND_CHANGE,
    KIND_IMPROVEMENT,
    KIND_THRESHOLD,
    KIND_TREND,
    ORANGE,
    RED,
    SEVERITY_ORDER,
    YELLOW,
)
from qmis.analytics.significance import (
    SignificanceResult,
    compare_periods,
    compare_to_threshold,
)
from qmis.core.periods import Period

SUPPRESS_SMALL_SAMPLE = "small_sample"
SUPPRESS_IMMATURE = "immature"
SUPPRESS_NEUTRAL = "no_direction"
SUPPRESS_NO_HISTORY = "no_baseline"


@dataclass
class Judgement:
    """The full verdict on one entity/metric/period."""

    severity: str = GREEN
    kind: str = KIND_THRESHOLD
    threshold_status: str = UNKNOWN
    effective_threshold_status: str = UNKNOWN
    is_improvement: bool = False
    significant: bool = False
    severe: bool = False
    adverse: bool = False
    anomaly: bool = False
    streak_significant: bool = False
    streak_severe: bool = False
    raw_significant: bool = False
    raw_severe: bool = False
    consecutive_favourable: int = 0
    favourable_delta: float | None = None
    movement_test: SignificanceResult | None = None
    threshold_test: SignificanceResult | None = None
    streak_test: SignificanceResult | None = None
    anomaly_test: SignificanceResult | None = None
    statistically_confirmed: bool = True
    movement_confirmed: bool = True
    threshold_confirmed: bool = True
    anomaly_confirmed: bool = True
    streak_confirmed: bool = True
    suppressed: str | None = None
    reasons: tuple[str, ...] = ()
    headline: str = ""
    explanation: str = ""
    priority: float = 0.0
    alertable: bool = False


SEVERITY_ICON = {RED: "🔴", ORANGE: "🟠", YELLOW: "🟡", GREEN: "🟢"}
SEVERITY_WORD = {
    RED: "CRITICAL",
    ORANGE: "HIGH ATTENTION",
    YELLOW: "EARLY WARNING",
    GREEN: "HEALTHY",
}


def judge(
    metric: MetricDefinition,
    *,
    current: float | None,
    previous: float | None,
    delta: float | None,
    pct_change: float | None,
    trailing_avg: float | None = None,
    consecutive_adverse: int = 0,
    streak_delta: float | None = None,
    consecutive_favourable: int = 0,
    favourable_streak_delta: float | None = None,
    denominator: float | None = None,
    previous_denominator: float | None = None,
    streak_start_denominator: float | None = None,
    confidence: float = 0.90,
    anomaly_z: float | None = None,
    anomaly_baseline: float | None = None,
    anomaly_relative: float | None = None,
    is_anomaly: bool = False,
    anomaly_adverse: bool = False,
    entity_label: str = "",
    period: Period | None = None,
    latest_period: Period | None = None,
) -> Judgement:
    """Classify one observation and write its explanation."""
    verdict = Judgement()
    verdict.threshold_status = metric.threshold_status(current)

    if metric.direction == NEUTRAL:
        verdict.suppressed = SUPPRESS_NEUTRAL
    elif (
        period is not None
        and latest_period is not None
        and not metric.is_mature(period, latest_period)
    ):
        verdict.suppressed = SUPPRESS_IMMATURE
    elif (
        metric.min_denominator
        and denominator is not None
        and denominator < metric.min_denominator
    ):
        verdict.suppressed = SUPPRESS_SMALL_SAMPLE

    improvement = metric.is_improvement(delta)
    verdict.is_improvement = bool(improvement)
    verdict.adverse = improvement is False

    verdict.significant, verdict.severe = _change_magnitude(metric, delta, pct_change)
    verdict.anomaly = bool(is_anomaly and anomaly_adverse)

    # A run of small drifts is not the same news as a run of real falls, so the
    # trend rules are judged on the CUMULATIVE move across the streak rather
    # than on the last step alone.  Without this, three consecutive 0.1pp
    # wobbles escalated to RED.
    streak_pct = None
    if streak_delta is not None and current is not None:
        origin = current - streak_delta
        if origin not in (0, None) and not (isinstance(origin, float) and np.isnan(origin)):
            streak_pct = streak_delta / abs(origin) * 100.0
    verdict.streak_significant, verdict.streak_severe = _change_magnitude(
        metric, streak_delta, streak_pct
    )
    # Keep the pre-gate magnitude verdict. The anomaly path is judged on its
    # own footing rather than on the far weaker two-period comparison - but it
    # still has to answer to the sample size (see the anomaly test below).
    verdict.raw_significant, verdict.raw_severe = verdict.significant, verdict.severe

    # A configured change threshold says "this much movement matters".  It
    # cannot say whether the movement happened.  On fourteen submissions a
    # single extra reject moves RJBD1 by seven points, which clears every
    # configured threshold and means nothing.  Rate metrics are therefore also
    # tested against sampling noise, and an untestable metric (no denominator)
    # passes through on its configured thresholds alone.
    testable_rate = metric.unit == "percent" and bool(metric.numerator and metric.denominator)
    if testable_rate:
        verdict.movement_test = compare_periods(
            current, denominator, previous, previous_denominator, confidence
        )
        # A run of declines measured on OVERLAPPING rolling windows is not a run
        # of independent observations - consecutive 4-week windows share three
        # of their four weeks. Testing the two endpoints of the run instead
        # compares periods that are `streak` apart, which for a streak at least
        # as long as the window do not overlap at all.
        if streak_delta is not None and consecutive_adverse > 0:
            verdict.streak_test = compare_periods(
                current,
                denominator,
                current - streak_delta,
                streak_start_denominator,
                confidence,
            )
            if verdict.streak_test.testable and not verdict.streak_test.significant:
                verdict.streak_significant = False
                verdict.streak_severe = False
                verdict.streak_confirmed = False
        reference = (
            metric.critical_threshold
            if verdict.threshold_status == CRITICAL
            else metric.warning_threshold
        )
        verdict.threshold_test = compare_to_threshold(
            current, denominator, reference, metric.direction == HIGHER_IS_BETTER, confidence
        )
        if verdict.movement_test.testable and not verdict.movement_test.significant:
            verdict.significant = False
            verdict.severe = False
            verdict.movement_confirmed = False
        # The anomaly detector's z-score is computed on a series of RATES. When
        # each of those rates rests on ~30 submissions, a run that happens to
        # land close together gives a narrow band, and the next ordinary
        # fluctuation reads as many sigma. So an anomaly is confirmed only if
        # the current sample can distinguish it from its own baseline: a
        # one-sample proportion test against the recent typical level, which is
        # a stronger test than the two-period one because the baseline rests on
        # several periods.
        if is_anomaly and anomaly_baseline is not None:
            verdict.anomaly_test = compare_to_threshold(
                current, denominator, anomaly_baseline, metric.direction == HIGHER_IS_BETTER,
                confidence,
            )
            if verdict.anomaly_test.testable and not verdict.anomaly_test.significant:
                verdict.anomaly = False
                verdict.anomaly_confirmed = False
        if (
            verdict.threshold_status in (WARNING, CRITICAL)
            and verdict.threshold_test.testable
            and not verdict.threshold_test.significant
        ):
            # The value is the wrong side of the line, but the sample cannot
            # tell that apart from chance.  Report it, do not escalate on it.
            verdict.effective_threshold_status = WARNING if verdict.threshold_status == CRITICAL else OK
            verdict.threshold_confirmed = False
        else:
            verdict.effective_threshold_status = verdict.threshold_status
    else:
        verdict.effective_threshold_status = verdict.threshold_status

    if verdict.suppressed:
        verdict.severity = GREEN
        verdict.kind = KIND_THRESHOLD
        verdict.headline, verdict.explanation = _describe(
            metric, verdict, current, previous, delta, pct_change,
            trailing_avg, consecutive_adverse, denominator, anomaly_baseline,
            anomaly_z, anomaly_relative, entity_label,
        )
        return verdict

    severity, kind, reasons = _classify(metric, verdict, consecutive_adverse)
    # "Confirmed" has to mean *the evidence that produced this severity* is
    # supported - not "every test passed". An anomaly backed by eight periods of
    # history is real even when the far weaker two-period comparison on the same
    # numbers is inconclusive; letting that weaker test veto it downgraded
    # genuine findings.
    verdict.statistically_confirmed = {
        KIND_THRESHOLD: verdict.threshold_confirmed,
        KIND_ANOMALY: verdict.anomaly_confirmed,
        KIND_CHANGE: verdict.movement_confirmed,
        KIND_TREND: verdict.streak_confirmed,
    }.get(kind, True)
    if severity == RED and not verdict.statistically_confirmed:
        # Invariant: nothing reaches the top of a manager's list on evidence the
        # sample cannot support. It is still reported, one level down, saying so.
        severity = ORANGE
        reasons = list(reasons) + ["not statistically confirmed at this sample size"]
    verdict.severity, verdict.kind, verdict.reasons = severity, kind, tuple(reasons)

    sustained_gain = consecutive_favourable >= max(2, metric.consecutive_decline_periods)
    if severity == GREEN and verdict.is_improvement and (verdict.significant or sustained_gain):
        # "Improving consistently for four weeks" is a question managers ask,
        # and a steady 2pp-a-week climb never trips a single-week threshold.
        verdict.kind = KIND_IMPROVEMENT
        verdict.alertable = True
        verdict.consecutive_favourable = consecutive_favourable
        verdict.favourable_delta = favourable_streak_delta
    else:
        verdict.alertable = severity != GREEN

    verdict.priority = _priority(metric, verdict, delta, pct_change, denominator, consecutive_adverse)
    verdict.headline, verdict.explanation = _describe(
        metric, verdict, current, previous, delta, pct_change, trailing_avg,
        consecutive_adverse, denominator, anomaly_baseline, anomaly_z,
        anomaly_relative, entity_label,
    )
    return verdict


def _change_magnitude(
    metric: MetricDefinition, delta: float | None, pct_change: float | None
) -> tuple[bool, bool]:
    """Is the movement worth mentioning, and is it severe?"""
    if delta is None or (isinstance(delta, float) and np.isnan(delta)):
        return False, False
    if not metric.alert_on_weekly_change:
        return False, False
    multiplier = max(1.0, metric.critical_change_multiplier)
    significant = False
    severe = False

    if metric.absolute_change_threshold is not None:
        limit = abs(metric.absolute_change_threshold)
        significant |= abs(delta) >= limit
        severe |= abs(delta) >= limit * multiplier
    if metric.percentage_change_threshold is not None and pct_change is not None:
        if not (isinstance(pct_change, float) and np.isnan(pct_change)):
            limit = abs(metric.percentage_change_threshold)
            significant |= abs(pct_change) >= limit
            severe |= abs(pct_change) >= limit * multiplier
    return significant, severe


def _classify(
    metric: MetricDefinition, v: Judgement, consecutive: int
) -> tuple[str, str, list[str]]:
    reasons: list[str] = []
    # Escalation uses the statistically confirmed view; the raw status is kept
    # on the record so the dashboard can still colour an unconfirmed breach.
    status = v.effective_threshold_status or v.threshold_status
    adverse = v.adverse
    sustained = consecutive >= max(1, metric.consecutive_decline_periods)

    if status == CRITICAL:
        reasons.append("critical threshold breached")
        return RED, KIND_THRESHOLD, reasons
    if adverse and v.severe and status == WARNING:
        reasons.append("warning threshold breached with a severe deterioration")
        return RED, KIND_THRESHOLD, reasons
    if adverse and v.anomaly and (v.severe or v.raw_severe):
        reasons.append("severe deterioration that is also statistically unusual")
        return RED, KIND_ANOMALY, reasons
    if adverse and sustained and v.streak_severe and status == WARNING:
        reasons.append(
            f"{consecutive} consecutive periods of severe decline below the warning threshold"
        )
        return RED, KIND_TREND, reasons

    if adverse and v.severe:
        reasons.append("severe week-on-week deterioration")
        return ORANGE, KIND_CHANGE, reasons
    if status == WARNING and adverse:
        reasons.append("below the warning threshold and still deteriorating")
        return ORANGE, KIND_THRESHOLD, reasons
    if adverse and sustained and v.streak_significant:
        reasons.append(f"{consecutive} consecutive periods of decline")
        return ORANGE, KIND_TREND, reasons
    if adverse and v.anomaly and v.significant:
        reasons.append("unusual movement against recent history")
        return ORANGE, KIND_ANOMALY, reasons

    if status == WARNING:
        reasons.append("below the warning threshold")
        return YELLOW, KIND_THRESHOLD, reasons
    if adverse and v.significant:
        reasons.append("significant week-on-week deterioration")
        return YELLOW, KIND_CHANGE, reasons
    if adverse and sustained and v.streak_significant:
        reasons.append(f"{consecutive} consecutive periods of drift in the wrong direction")
        return YELLOW, KIND_TREND, reasons
    if v.anomaly:
        reasons.append("unusual compared with recent history")
        return YELLOW, KIND_ANOMALY, reasons

    return GREEN, KIND_THRESHOLD, reasons


def _priority(
    metric: MetricDefinition,
    v: Judgement,
    delta: float | None,
    pct_change: float | None,
    denominator: float | None,
    consecutive: int,
) -> float:
    """Rank alerts so the top of the list is genuinely the top of the list.

    Severity dominates.  Within a severity, an alert matters more when the
    metric carries more weight in the quality score, when the movement is
    larger, when it has been going on longer, and when it rests on more
    submissions.
    """
    base = SEVERITY_ORDER.get(v.severity, 0) * 1000.0
    if v.kind == KIND_IMPROVEMENT:
        base = 10.0
    weight = metric.weight if metric.weight else 5.0
    magnitude = 0.0
    if delta is not None and not (isinstance(delta, float) and np.isnan(delta)):
        band = None
        if metric.warning_threshold is not None and metric.critical_threshold is not None:
            band = abs(metric.warning_threshold - metric.critical_threshold)
        magnitude = abs(delta) / band if band else abs(delta) / 10.0
    volume = float(np.log10(denominator)) if denominator and denominator > 1 else 0.0
    return round(base + weight * 2.0 + magnitude * 15.0 + consecutive * 8.0 + volume * 3.0, 3)


def _describe(
    metric: MetricDefinition,
    v: Judgement,
    current: float | None,
    previous: float | None,
    delta: float | None,
    pct_change: float | None,
    trailing_avg: float | None,
    consecutive: int,
    denominator: float | None,
    anomaly_baseline: float | None,
    anomaly_z: float | None,
    anomaly_relative: float | None,
    entity_label: str,
) -> tuple[str, str]:
    """Compose the alert headline and its full explanation."""
    who = f"{entity_label}'s " if entity_label else ""
    icon = SEVERITY_ICON.get(v.severity, "")
    now = metric.format(current)

    if v.suppressed == SUPPRESS_NEUTRAL:
        headline = f"{metric.name}: {now}"
        body = (
            f"{metric.name} is tracked for context only - its business direction has not been "
            f"confirmed, so it does not raise alerts or affect the quality score."
            + (f" {metric.review_note}" if metric.review_note else "")
        )
        return headline, body
    if v.suppressed == SUPPRESS_IMMATURE:
        headline = f"{metric.name}: {now} (too early to judge)"
        body = (
            f"{metric.name} is not yet mature for this period. Donors signed in this period have "
            f"not had time to reach this debit stage, so the value is expected to be low and no "
            f"alert is raised."
        )
        return headline, body
    if v.suppressed == SUPPRESS_SMALL_SAMPLE:
        headline = f"{metric.name}: {now} (sample too small)"
        body = (
            f"{who}{metric.name} is {now}, but it rests on only "
            f"{denominator:,.0f} submissions (minimum {metric.min_denominator:,} to alert). "
            f"The value is recorded and charted, but a rate on this few cases moves too much "
            f"to act on."
        )
        return headline, body

    # -- movement sentence -------------------------------------------------
    if previous is None or delta is None or (isinstance(delta, float) and np.isnan(delta)):
        movement = f"{metric.name} is {now}. No previous period is available to compare against."
    else:
        direction_word = "rose" if delta > 0 else "fell" if delta < 0 else "was unchanged"
        movement = (
            f"{who}{metric.name} {direction_word} from {metric.format(previous)} to {now}"
        )
        if delta != 0:
            movement += f" ({metric.format_change(delta)}"
            if pct_change is not None and not (isinstance(pct_change, float) and np.isnan(pct_change)):
                movement += f", {pct_change:+.1f}% relative"
            movement += ")."
        else:
            movement += "."

    parts = [movement]

    # -- direction sentence ------------------------------------------------
    if v.is_improvement and delta:
        parts.append(
            f"Since {'higher' if metric.direction == HIGHER_IS_BETTER else 'lower'} "
            f"{metric.name} is better, this is a positive movement."
        )
    elif v.adverse and delta:
        parts.append(
            f"Since {'higher' if metric.direction == HIGHER_IS_BETTER else 'lower'} "
            f"{metric.name} is better, this is a negative quality movement."
        )

    # -- threshold sentence ------------------------------------------------
    if v.threshold_status == CRITICAL and metric.critical_threshold is not None:
        parts.append(
            f"It is now past the critical threshold of "
            f"{metric.format(metric.critical_threshold)}."
        )
    elif v.threshold_status == WARNING and metric.warning_threshold is not None:
        parts.append(
            f"It is past the warning threshold of {metric.format(metric.warning_threshold)} "
            f"(critical at {metric.format(metric.critical_threshold)})."
            if metric.critical_threshold is not None
            else f"It is past the warning threshold of {metric.format(metric.warning_threshold)}."
        )
    elif v.threshold_status == OK and metric.warning_threshold is not None and v.adverse:
        parts.append(
            f"It is still within target ({metric.threshold_reference()})."
        )

    # -- trend sentence ----------------------------------------------------
    if consecutive >= 2:
        parts.append(f"This is the {_ordinal(consecutive)} consecutive period of decline.")
    elif v.consecutive_favourable >= 2:
        total = (
            f" ({metric.format_change(v.favourable_delta)} in total)"
            if v.favourable_delta is not None
            else ""
        )
        parts.append(
            f"This is the {_ordinal(v.consecutive_favourable)} consecutive period of "
            f"improvement{total}."
        )
    if trailing_avg is not None and not (isinstance(trailing_avg, float) and np.isnan(trailing_avg)):
        parts.append(f"Recent average: {metric.format(trailing_avg)}.")

    # -- anomaly sentence --------------------------------------------------
    if (
        not v.anomaly
        and v.anomaly_test is not None
        and v.anomaly_test.testable
        and not v.anomaly_test.significant
        and anomaly_baseline is not None
    ):
        parts.append(
            f"It is well away from its recent typical level of "
            f"{metric.format(anomaly_baseline)}, but on {denominator:,.0f} submissions that gap "
            f"is still within the range of chance, so it is reported rather than escalated."
        )
    if v.anomaly and anomaly_baseline is not None:
        detail = f"Its recent typical level is {metric.format(anomaly_baseline)}"
        if anomaly_relative is not None and not np.isnan(anomaly_relative):
            detail += f", so this is a {anomaly_relative:+.0f}% move against its own history"
        if anomaly_z is not None and not np.isnan(anomaly_z):
            detail += f" ({abs(anomaly_z):.1f} standard deviations)"
        parts.append(detail + ". That is unusual regardless of the threshold.")

    if denominator is not None and not (isinstance(denominator, float) and np.isnan(denominator)):
        parts.append(f"Based on {denominator:,.0f} submissions.")
    if v.movement_test is not None and v.movement_test.testable and delta:
        parts.append(v.movement_test.describe())
    if (
        v.threshold_status in (CRITICAL, WARNING)
        and v.effective_threshold_status != v.threshold_status
    ):
        parts.append(
            "The threshold breach is not yet statistically confirmed on this sample size, "
            "so it is reported rather than escalated."
        )

    # -- headline ----------------------------------------------------------
    if v.kind == KIND_IMPROVEMENT:
        if v.consecutive_favourable >= 2 and v.favourable_delta is not None:
            headline = (
                f"{icon} SUSTAINED IMPROVEMENT - {who}{metric.name} has improved for "
                f"{v.consecutive_favourable} periods running, "
                f"{metric.format_change(v.favourable_delta)} in total, now {now}"
            )
        else:
            headline = (
                f"{icon} SIGNIFICANT IMPROVEMENT - {who}{metric.name} improved from "
                f"{metric.format(previous)} to {now} ({metric.format_change(delta)})"
            )
    elif v.severity == GREEN:
        headline = f"{icon} {who}{metric.name} is healthy at {now}"
    else:
        label = SEVERITY_WORD[v.severity]
        # "deterioration", not "decline": for a lower-is-better metric the
        # number going UP is the bad news, and "decline" would read backwards.
        move = "deterioration" if v.adverse else "level"
        headline = (
            f"{icon} {label} - {who}{metric.name} {move}: "
            f"{metric.format(previous)} → {now}"
            if previous is not None
            else f"{icon} {label} - {who}{metric.name} at {now}"
        )
    return headline[:500], " ".join(parts)


def _ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"
