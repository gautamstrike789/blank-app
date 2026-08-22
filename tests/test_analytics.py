"""Comparison, anomaly detection, severity and scoring."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from qmis.analytics.anomaly import detect_anomalies
from qmis.analytics.comparison import build_comparison
from qmis.analytics.engine import assess
from qmis.analytics.scoring import band_for, score_entities
from qmis.analytics.severity import judge
from qmis.analytics.significance import compare_periods, compare_to_threshold
from qmis.core.metric_config import CRITICAL, WARNING
from qmis.core.models import GREEN, KIND_ANOMALY, KIND_IMPROVEMENT, ORANGE, RED, YELLOW
from qmis.core.periods import Period

PERIOD = Period.from_key("2026-W33")


def history(values, metric_key="d1_pct", denominator=400, entity_id=1, name="Rahul"):
    """Build a history frame ending at PERIOD, oldest value first."""
    rows = []
    for offset, value in enumerate(reversed(values)):
        rows.append(
            dict(
                entity_id=entity_id, entity_type="ba", entity_name=name,
                owner_id=99, owner_name="Owner A",
                period_key=PERIOD.shift(-offset).key, grain="weekly",
                metric_key=metric_key, value=value, denominator=denominator,
            )
        )
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- #
# comparison
# --------------------------------------------------------------------------- #
def test_comparison_computes_every_baseline(registry):
    frame = build_comparison(history([80, 82, 84, 86, 88, 90]), registry, PERIOD)
    row = frame.iloc[0]
    assert row["current"] == 90 and row["previous"] == 88
    assert row["delta"] == pytest.approx(2.0)
    assert row["pct_change"] == pytest.approx(2 / 88 * 100)
    assert row["trailing_avg"] == pytest.approx(np.mean([82, 84, 86, 88]))
    assert row["best"] == 90 and row["worst"] == 80  # higher is better


def test_best_and_worst_follow_the_metric_direction(registry):
    frame = build_comparison(history([5, 10, 15], metric_key="rjbd1_pct"), registry, PERIOD)
    row = frame.iloc[0]
    assert row["best"] == 5 and row["worst"] == 15  # lower is better


def test_adverse_streak_counts_only_consecutive_periods(registry):
    frame = build_comparison(history([90, 89, 88, 87, 86]), registry, PERIOD)
    row = frame.iloc[0]
    assert row["consecutive_adverse"] == 4
    assert row["streak_delta"] == pytest.approx(-4.0)


def test_a_gap_ends_a_streak_rather_than_bridging_it(registry):
    frame = history([90, 89, 88, 87, 86])
    frame = frame.loc[frame["period_key"] != PERIOD.shift(-2).key]
    row = build_comparison(frame, registry, PERIOD).iloc[0]
    # Only the most recent step survives; the run is not bridged over the hole.
    assert row["consecutive_adverse"] == 1


def test_favourable_streak_is_tracked_too(registry):
    row = build_comparison(history([70, 72, 74, 76, 78]), registry, PERIOD).iloc[0]
    assert row["consecutive_favourable"] == 4
    assert row["favourable_streak_delta"] == pytest.approx(8.0)


def test_later_periods_never_leak_into_a_baseline(registry):
    frame = history([80, 85, 90])
    frame.loc[len(frame)] = dict(
        entity_id=1, entity_type="ba", entity_name="Rahul", owner_id=99, owner_name="Owner A",
        period_key=PERIOD.shift(1).key, grain="weekly", metric_key="d1_pct",
        value=10.0, denominator=400,
    )
    row = build_comparison(frame, registry, PERIOD).iloc[0]
    assert row["current"] == 90 and row["worst"] == 80


# --------------------------------------------------------------------------- #
# anomaly
# --------------------------------------------------------------------------- #
def test_anomaly_fires_without_a_threshold_breach(registry):
    # The brief's example: RJBD1 stable at 2.0-2.5%, then 4.0%. Never near 14%.
    frame = history([2.1, 2.3, 2.0, 2.4, 2.2, 2.5, 2.1, 2.3, 4.0], metric_key="rjbd1_pct")
    result = detect_anomalies(frame, registry, PERIOD).iloc[0]
    assert result["is_anomaly"] and result["adverse"]
    assert result["baseline"] == pytest.approx(2.25, abs=0.1)
    assert registry["rjbd1_pct"].threshold_status(4.0) == "OK"


def test_a_stable_series_is_not_an_anomaly(registry):
    frame = history([88.0, 88.2, 87.9, 88.1, 88.3, 88.0, 88.1, 88.2])
    result = detect_anomalies(frame, registry, PERIOD)
    assert not bool(result.iloc[0]["is_anomaly"])


def test_a_favourable_outlier_is_flagged_but_not_adverse(registry):
    frame = history([2.2, 2.3, 2.1, 2.2, 2.4, 2.2, 2.3, 0.1], metric_key="rjbd1_pct")
    result = detect_anomalies(frame, registry, PERIOD).iloc[0]
    assert result["is_anomaly"] and not result["adverse"]


def test_too_little_history_produces_no_anomaly(registry):
    assert detect_anomalies(history([88, 90]), registry, PERIOD).empty


def test_a_single_past_spike_does_not_mask_the_next_one(registry):
    # A mean/stdev z-score is inflated by the old spike and misses this;
    # the median/MAD estimator is not.
    frame = history([2.2, 2.1, 9.0, 2.2, 2.3, 2.1, 2.2, 6.0], metric_key="rjbd1_pct")
    result = detect_anomalies(frame, registry, PERIOD).iloc[0]
    assert result["is_anomaly"] and result["adverse"]


# --------------------------------------------------------------------------- #
# significance
# --------------------------------------------------------------------------- #
def test_one_extra_reject_on_a_tiny_sample_is_not_significant():
    result = compare_periods(6.25, 16, 0.0, 16)
    assert result.testable and not result.significant
    assert result.required_n and result.required_n > 16


def test_a_real_move_on_a_real_sample_is_significant():
    assert compare_periods(76.0, 420, 85.0, 420).significant


def test_threshold_breach_needs_the_sample_to_support_it():
    assert compare_to_threshold(76.0, 420, 80.0, higher_is_better=True).significant
    assert not compare_to_threshold(76.0, 14, 80.0, higher_is_better=True).significant


def test_metrics_without_a_denominator_are_not_blocked():
    result = compare_periods(700, None, 800, None)
    assert not result.testable and result.significant


# --------------------------------------------------------------------------- #
# severity
# --------------------------------------------------------------------------- #
def test_critical_threshold_breach_is_red(registry):
    verdict = judge(
        registry["d1_pct"], current=76.0, previous=85.0, delta=-9.0, pct_change=-10.6,
        denominator=420, previous_denominator=420, entity_label="Rahul",
    )
    assert verdict.severity == RED and verdict.threshold_status == CRITICAL
    assert "below the required" in verdict.explanation or "critical threshold" in verdict.explanation
    assert "76.00%" in verdict.headline and "85.00%" in verdict.headline


def test_rjbd1_increase_reads_as_a_negative_movement(registry):
    verdict = judge(
        registry["rjbd1_pct"], current=3.8, previous=2.1, delta=1.7, pct_change=81.0,
        denominator=380, previous_denominator=380, is_anomaly=True, anomaly_adverse=True,
        anomaly_baseline=2.2, anomaly_z=6.4, anomaly_relative=72.7, entity_label="Rahul",
    )
    assert verdict.severity == RED
    assert "negative quality movement" in verdict.explanation
    assert "lower RJBD1" in verdict.explanation


def test_an_anomaly_the_sample_cannot_support_is_not_escalated(registry):
    """1 reject in 22 becoming 4 in 30 looks like many sigma against a narrow
    history, because that history is itself made of tiny samples."""
    verdict = judge(
        registry["rjbd1_pct"], current=13.33, previous=4.55, delta=8.78, pct_change=193.0,
        denominator=30, previous_denominator=22, is_anomaly=True, anomaly_adverse=True,
        anomaly_baseline=6.4, anomaly_z=5.1, anomaly_relative=108.0,
    )
    assert not verdict.anomaly
    assert verdict.severity != RED
    assert "within the range of chance" in verdict.explanation


def test_an_anomaly_on_a_real_sample_still_escalates(registry):
    """The same shape of movement on 380 submissions is a genuine finding."""
    verdict = judge(
        registry["rjbd1_pct"], current=3.8, previous=2.1, delta=1.7, pct_change=81.0,
        denominator=380, previous_denominator=380, is_anomaly=True, anomaly_adverse=True,
        anomaly_baseline=2.2, anomaly_z=6.4, anomaly_relative=72.7,
    )
    assert verdict.anomaly and verdict.severity == RED


def test_significant_improvement_is_reported(registry):
    verdict = judge(
        registry["d3_pct"], current=84.0, previous=72.0, delta=12.0, pct_change=16.7,
        denominator=500, previous_denominator=500, entity_label="Priya",
    )
    assert verdict.kind == KIND_IMPROVEMENT and verdict.alertable
    assert "IMPROVEMENT" in verdict.headline


def test_sustained_improvement_is_reported_even_when_each_step_is_small(registry):
    verdict = judge(
        registry["d3_pct"], current=78.0, previous=76.0, delta=2.0, pct_change=2.6,
        denominator=500, previous_denominator=500, consecutive_favourable=5,
        favourable_streak_delta=10.0, entity_label="Priya",
    )
    assert verdict.kind == KIND_IMPROVEMENT
    assert "SUSTAINED" in verdict.headline


def test_small_sample_is_recorded_but_never_alerts(registry):
    verdict = judge(
        registry["d1_pct"].for_level("ba"), current=50.0, previous=100.0, delta=-50.0,
        pct_change=-50.0, denominator=4, previous_denominator=4, entity_label="New BA",
    )
    assert verdict.suppressed == "small_sample"
    assert verdict.severity == GREEN and not verdict.alertable
    assert "too few" in verdict.explanation or "only 4" in verdict.explanation


def test_metric_without_a_confirmed_direction_never_alerts(registry):
    verdict = judge(
        registry["ot_pct"], current=25.0, previous=9.0, delta=16.0, pct_change=178.0,
        denominator=500, previous_denominator=500,
    )
    assert verdict.suppressed == "no_direction" and not verdict.alertable


def test_immature_metric_is_not_judged(registry):
    monthly = Period.from_key("2026-08")
    verdict = judge(
        registry["d3_pct"], current=0.0, previous=70.0, delta=-70.0, pct_change=-100.0,
        denominator=9000, previous_denominator=9000, period=monthly, latest_period=monthly,
    )
    assert verdict.suppressed == "immature" and not verdict.alertable


def test_a_drift_of_a_few_hundredths_does_not_escalate(registry):
    verdict = judge(
        registry["rjbd1_pct"], current=11.05, previous=11.0, delta=0.05, pct_change=0.45,
        denominator=5000, previous_denominator=5000, consecutive_adverse=4, streak_delta=0.2,
    )
    assert verdict.severity == GREEN


def test_unconfirmed_threshold_breach_is_downgraded_not_dropped(registry):
    verdict = judge(
        registry["d1_pct"].for_level("ba"), current=76.0, previous=80.0, delta=-4.0,
        pct_change=-5.0, denominator=20, previous_denominator=20,
    )
    assert verdict.threshold_status == CRITICAL          # reported as-is
    assert verdict.effective_threshold_status != CRITICAL  # not escalated on
    assert not verdict.statistically_confirmed
    assert "not yet statistically confirmed" in verdict.explanation


# --------------------------------------------------------------------------- #
# scoring
# --------------------------------------------------------------------------- #
def test_a_zero_baseline_does_not_make_everything_an_anomaly(registry):
    """A BA whose rejects were 0,0,0,0 has not proved their true rate is zero.

    Testing against a literal 0% reference has zero variance, so the next single
    reject reads as infinitely significant.
    """
    verdict = judge(
        registry["rjbd1_pct"], current=11.76, previous=7.14, delta=4.62, pct_change=64.7,
        denominator=17, previous_denominator=14, is_anomaly=True, anomaly_adverse=True,
        anomaly_baseline=0.0, anomaly_z=3.1, anomaly_relative=999.0,
    )
    assert not verdict.anomaly and verdict.severity != RED


def test_confirmation_follows_the_path_that_produced_the_severity(registry):
    """An anomaly backed by weeks of history is not vetoed by the far weaker
    two-period comparison of the same two numbers."""
    verdict = judge(
        registry["rjbd1_pct"], current=3.8, previous=2.1, delta=1.7, pct_change=81.0,
        denominator=380, previous_denominator=380, is_anomaly=True, anomaly_adverse=True,
        anomaly_baseline=2.2, anomaly_z=6.4, anomaly_relative=72.7,
    )
    assert verdict.kind == KIND_ANOMALY
    assert not verdict.movement_confirmed      # the weak test was inconclusive
    assert verdict.anomaly_confirmed           # the strong one was not
    assert verdict.statistically_confirmed and verdict.severity == RED


def test_nothing_reaches_red_on_evidence_the_sample_cannot_support(registry):
    verdict = judge(
        registry["d1_pct"].for_level("ba"), current=76.0, previous=85.0, delta=-9.0,
        pct_change=-10.6, denominator=20, previous_denominator=20,
    )
    assert verdict.threshold_status == CRITICAL
    assert verdict.severity == ORANGE and not verdict.statistically_confirmed


def test_score_bands():
    assert band_for(95) == "Excellent"
    assert band_for(80) == "Healthy"
    assert band_for(65) == "Attention required"
    assert band_for(40) == "Critical"
    assert band_for(None) is None


def test_score_is_weighted_and_renormalised(registry):
    frame = pd.concat(
        [
            history([88.0], metric_key="d1_pct"),
            history([70.0], metric_key="d3_pct"),
            history([11.0], metric_key="rjbd1_pct"),
        ]
    )
    assessed = assess(frame, registry, PERIOD, latest_period=PERIOD)
    scores = score_entities(assessed, registry, PERIOD, latest_period=PERIOD)
    row = scores.iloc[0]
    assert 0 <= row["score"] <= 100
    # Only three of the seven scored metrics are present, so coverage says so
    # rather than the score being silently deflated.
    assert row["coverage"] < 1.0 and row["metrics_used"] == 3


def test_a_missing_metric_does_not_deflate_the_score(registry):
    full = pd.concat(
        [history([88.0], metric_key="d1_pct"), history([70.0], metric_key="d3_pct")]
    )
    partial = history([88.0], metric_key="d1_pct")
    score_full = score_entities(
        assess(full, registry, PERIOD, latest_period=PERIOD), registry, PERIOD, latest_period=PERIOD
    ).iloc[0]
    score_partial = score_entities(
        assess(partial, registry, PERIOD, latest_period=PERIOD), registry, PERIOD, latest_period=PERIOD
    ).iloc[0]
    assert score_partial["score"] >= score_full["score"] - 0.01
    assert score_partial["coverage"] < score_full["coverage"]
