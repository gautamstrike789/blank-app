import pytest

from qmis.core.metric_config import (
    CRITICAL,
    OK,
    WARNING,
    MetricConfigError,
    MetricDefinition,
    MetricRegistry,
)


def test_registry_loads_and_weights_are_sane(registry):
    assert len(registry) >= 40
    assert registry.total_weight() == pytest.approx(100.0)


def test_higher_is_better_thresholds(registry):
    d1 = registry["d1_pct"]
    assert d1.threshold_status(92) == OK
    assert d1.threshold_status(84) == WARNING
    assert d1.threshold_status(79) == CRITICAL
    # Landing exactly on a threshold counts as having crossed it.
    assert d1.threshold_status(80) == CRITICAL
    assert d1.threshold_status(85) == WARNING


def test_lower_is_better_thresholds(registry):
    rjbd1 = registry["rjbd1_pct"]
    assert rjbd1.threshold_status(9) == OK
    assert rjbd1.threshold_status(13) == WARNING
    assert rjbd1.threshold_status(15) == CRITICAL
    assert rjbd1.threshold_status(14) == CRITICAL


def test_improvement_direction_depends_on_the_metric(registry):
    assert registry["d1_pct"].is_improvement(+2) is True
    assert registry["d1_pct"].is_improvement(-2) is False
    assert registry["rjbd1_pct"].is_improvement(+2) is False
    assert registry["rjbd1_pct"].is_improvement(-2) is True
    assert registry["ot_pct"].is_improvement(+2) is None  # direction unconfirmed


def test_normalisation_anchors_are_interpretable(registry):
    d1 = registry["d1_pct"]
    assert d1.normalise(d1.critical_threshold) == pytest.approx(50.0)
    assert d1.normalise(d1.warning_threshold) == pytest.approx(75.0)
    assert d1.normalise(d1.target) == pytest.approx(100.0)
    assert d1.normalise(200) == 100.0 and d1.normalise(0) == 0.0


def test_normalisation_mirrors_for_lower_is_better(registry):
    rjbd1 = registry["rjbd1_pct"]
    assert rjbd1.normalise(rjbd1.critical_threshold) == pytest.approx(50.0)
    assert rjbd1.normalise(rjbd1.target) == pytest.approx(100.0)
    assert rjbd1.normalise(0) == 100.0


def test_alias_resolution_survives_formatting(registry):
    assert registry.resolve("Rejects Before Debit 1%").key == "rjbd1_pct"
    assert registry.resolve("  d1 %  ").key == "d1_pct"
    assert registry.resolve("Sum of SUBMISSION").key == "submissions"
    assert registry.resolve("nonsense column") is None


def test_percent_and_count_aliases_do_not_collide(registry):
    # "Debit 1" is a count and "Debit 1%" a rate; dropping the % merged them.
    assert registry.resolve("Sum of debit1").key == "debit1_count"
    assert registry.resolve("D1%").key == "d1_pct"


def test_duplicate_alias_is_rejected():
    raw = {
        "metrics": [
            {"key": "a", "name": "A", "aliases": ["Shared"], "unit": "number"},
            {"key": "b", "name": "B", "aliases": ["shared"], "unit": "number"},
        ]
    }
    with pytest.raises(MetricConfigError, match="alias"):
        MetricRegistry.from_dict(raw)


def test_inverted_thresholds_are_rejected():
    raw = {
        "metrics": [
            {
                "key": "x", "name": "X", "unit": "percent", "direction": "higher_is_better",
                "warning_threshold": 60, "critical_threshold": 80,
            }
        ]
    }
    with pytest.raises(MetricConfigError, match="critical_threshold"):
        MetricRegistry.from_dict(raw)


def test_level_overrides_apply_only_where_configured(registry):
    d1 = registry["d1_pct"]
    assert d1.for_level("ba").min_denominator == 15
    assert d1.for_level("owner").min_denominator == d1.min_denominator
    assert registry["d2_pct"].for_level("ba") is registry["d2_pct"]


def test_metrics_awaiting_review_never_alert_or_score(registry):
    for metric in registry.needing_review():
        assert metric.direction == "neutral"
        assert not metric.scored
        assert metric.threshold_status(50) == "UNKNOWN"
