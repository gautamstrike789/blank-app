"""Configurable metric rules engine.

Nothing in the analytics layer knows what "RJBD1" means.  Every behavioural
decision -- which direction is good, when to warn, how much weight a metric
carries in the quality score, whether an anomaly check applies -- lives in
``qmis/config/metrics.yaml`` and is loaded into :class:`MetricDefinition`
objects here.

Adding a 51st metric is a YAML edit, not a code change.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

import yaml

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
METRICS_FILE = CONFIG_DIR / "metrics.yaml"

# Direction rules -------------------------------------------------------------
HIGHER_IS_BETTER = "higher_is_better"
LOWER_IS_BETTER = "lower_is_better"
TARGET_RANGE = "target_range"
NEUTRAL = "neutral"
DIRECTIONS = (HIGHER_IS_BETTER, LOWER_IS_BETTER, TARGET_RANGE, NEUTRAL)

# Units / storage -------------------------------------------------------------
UNITS = ("percent", "count", "currency", "number")
STORED_AS = ("fraction", "percent", "raw")

# Threshold verdicts ----------------------------------------------------------
OK = "OK"
WARNING = "WARNING"
CRITICAL = "CRITICAL"
UNKNOWN = "UNKNOWN"


class MetricConfigError(ValueError):
    """Raised when the metric registry is malformed."""


def _as_float(value: Any, field_name: str, key: str) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:  # pragma: no cover - defensive
        raise MetricConfigError(f"metric {key!r}: {field_name} must be numeric, got {value!r}") from exc


@dataclass(frozen=True)
class AnomalyRule:
    """Rolling-window anomaly settings for one metric."""

    enabled: bool = True
    lookback: int = 8
    min_history: int = 4
    z_threshold: float = 2.5
    relative_threshold: float | None = 25.0  # % deviation from trailing mean
    method: str = "robust"  # "robust" (median/MAD) or "zscore" (mean/stdev)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any] | None, defaults: "AnomalyRule") -> "AnomalyRule":
        if not raw:
            return defaults
        if "relative_threshold" in raw:
            rel = raw["relative_threshold"]
            rel = None if rel is None else float(rel)
        else:
            rel = defaults.relative_threshold
        method = str(raw.get("method", defaults.method))
        if method not in ("robust", "zscore"):
            raise MetricConfigError(f"unknown anomaly method {method!r}")
        return cls(
            enabled=bool(raw.get("enabled", defaults.enabled)),
            lookback=int(raw.get("lookback", defaults.lookback)),
            min_history=int(raw.get("min_history", defaults.min_history)),
            z_threshold=float(raw.get("z_threshold", defaults.z_threshold)),
            relative_threshold=rel,
            method=method,
        )


@dataclass(frozen=True)
class MetricDefinition:
    """One configured metric.

    Threshold semantics depend on ``direction``:

    ``higher_is_better``  value below ``warning_threshold`` -> WARNING,
                          below ``critical_threshold`` -> CRITICAL.
    ``lower_is_better``   value above ``warning_threshold`` -> WARNING,
                          above ``critical_threshold`` -> CRITICAL.
    ``target_range``      outside [``target_min``, ``target_max``] -> WARNING,
                          outside the critical band -> CRITICAL.
    ``neutral``           never produces a threshold alert (volume/context only).
    """

    key: str
    name: str
    unit: str = "number"
    stored_as: str = "raw"
    direction: str = NEUTRAL
    warning_threshold: float | None = None
    critical_threshold: float | None = None
    target: float | None = None
    target_min: float | None = None
    target_max: float | None = None
    weight: float = 0.0
    include_in_score: bool = False
    alert_on_weekly_change: bool = True
    percentage_change_threshold: float | None = 10.0
    absolute_change_threshold: float | None = None
    critical_change_multiplier: float = 2.0
    consecutive_decline_periods: int = 3
    min_denominator: int | None = None
    maturity_lag: Mapping[str, int] = field(default_factory=dict)
    numerator: str | None = None
    denominator: str | None = None
    decimals: int = 2
    aliases: tuple[str, ...] = ()
    group: str = "Quality"
    family: str | None = None
    description: str = ""
    needs_review: bool = False
    review_note: str = ""
    level_overrides: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    anomaly: AnomalyRule = field(default_factory=AnomalyRule)

    # -- derived helpers --------------------------------------------------
    @property
    def is_percent(self) -> bool:
        return self.unit == "percent"

    @property
    def scored(self) -> bool:
        return self.include_in_score and self.weight > 0 and self.direction in (
            HIGHER_IS_BETTER,
            LOWER_IS_BETTER,
        )

    @property
    def change_unit(self) -> str:
        """Label for an absolute change in this metric."""
        return "pp" if self.is_percent else ""

    def format(self, value: float | None) -> str:
        """Human-readable rendering used in alert text and tables."""
        if value is None or (isinstance(value, float) and math.isnan(value)):
            return "n/a"
        if self.unit == "percent":
            return f"{value:.{self.decimals}f}%"
        if self.unit == "currency":
            return f"{value:,.{self.decimals}f}"
        if self.unit == "count":
            return f"{value:,.0f}"
        return f"{value:,.{self.decimals}f}"

    def format_change(self, delta: float | None) -> str:
        if delta is None or (isinstance(delta, float) and math.isnan(delta)):
            return "n/a"
        sign = "+" if delta > 0 else ""
        if self.unit == "count":
            return f"{sign}{delta:,.0f}"
        suffix = f" {self.change_unit}".rstrip()
        return f"{sign}{delta:.{self.decimals}f}{suffix}"

    def is_improvement(self, delta: float | None) -> bool | None:
        """Is a change of ``delta`` a good thing?  ``None`` when undecidable."""
        if delta is None or delta == 0 or self.direction == NEUTRAL:
            return None
        if self.direction == HIGHER_IS_BETTER:
            return delta > 0
        if self.direction == LOWER_IS_BETTER:
            return delta < 0
        return None  # target_range needs the value, handled by threshold status

    def threshold_status(self, value: float | None) -> str:
        """Classify a value against its configured thresholds."""
        if value is None or (isinstance(value, float) and math.isnan(value)):
            return UNKNOWN
        # Comparisons are inclusive: landing exactly ON a threshold counts as
        # having crossed it.  Over-alerting by one hair is the cheaper mistake.
        if self.direction == HIGHER_IS_BETTER:
            if self.critical_threshold is not None and value <= self.critical_threshold:
                return CRITICAL
            if self.warning_threshold is not None and value <= self.warning_threshold:
                return WARNING
            return OK
        if self.direction == LOWER_IS_BETTER:
            if self.critical_threshold is not None and value >= self.critical_threshold:
                return CRITICAL
            if self.warning_threshold is not None and value >= self.warning_threshold:
                return WARNING
            return OK
        if self.direction == TARGET_RANGE:
            lo, hi = self.target_min, self.target_max
            if lo is None and hi is None:
                return UNKNOWN
            # `critical_threshold` is read as the tolerance band *outside* the
            # target range beyond which a breach escalates from WARNING to
            # CRITICAL.  Absent a tolerance, any breach is CRITICAL.
            tol = self.critical_threshold
            below = lo is not None and value < lo
            above = hi is not None and value > hi
            if not (below or above):
                return OK
            if tol is None:
                return CRITICAL
            excess = (lo - value) if below else (value - hi)
            return CRITICAL if excess > tol else WARNING
        return UNKNOWN

    def threshold_reference(self) -> str:
        """Short description of the rule, used in alert explanations."""
        if self.direction == HIGHER_IS_BETTER:
            parts = []
            if self.critical_threshold is not None:
                parts.append(f"critical below {self.format(self.critical_threshold)}")
            if self.warning_threshold is not None:
                parts.append(f"warning below {self.format(self.warning_threshold)}")
            return ", ".join(parts) or "no threshold configured"
        if self.direction == LOWER_IS_BETTER:
            parts = []
            if self.critical_threshold is not None:
                parts.append(f"critical above {self.format(self.critical_threshold)}")
            if self.warning_threshold is not None:
                parts.append(f"warning above {self.format(self.warning_threshold)}")
            return ", ".join(parts) or "no threshold configured"
        if self.direction == TARGET_RANGE:
            return f"target range {self.format(self.target_min)} - {self.format(self.target_max)}"
        return "informational metric"

    def for_level(self, level: str) -> "MetricDefinition":
        """This metric as it applies at one entity level.

        An Owner's Debit 1 is a 6,000-submission aggregate; a BA's is a
        14-submission sample.  Holding both to one threshold either drowns the
        Owner view in noise or lets real BA problems through, so any field may
        be overridden per level in ``level_overrides``.  With no override
        configured this returns the metric unchanged, which is the default.
        """
        overrides = self.level_overrides.get(level)
        if not overrides:
            return self
        return replace(self, **{k: v for k, v in overrides.items() if hasattr(self, k)})

    def maturity_lag_for(self, grain: str) -> int:
        """How many periods of the given grain this metric needs to settle."""
        return int(self.maturity_lag.get(grain, 0))

    def is_mature(self, period, latest_period) -> bool:
        """False while ``period`` is too recent for this metric to be judged.

        ``Debit 3`` in the source workbook reads 0% for the two most recent
        months simply because those donors have not reached their third debit
        yet.  Alerting on that would manufacture a guaranteed RED every week.
        """
        lag = self.maturity_lag_for(getattr(period, "grain", ""))
        if lag <= 0:
            return True
        try:
            return latest_period.distance(period) >= lag
        except Exception:  # pragma: no cover - mismatched grains
            return True

    def normalise(self, value: float | None) -> float | None:
        """Map a raw value onto a 0-100 quality scale.

        The mapping is piecewise linear through three explainable anchors:
        ``critical -> 50``, ``warning -> 75``, ``target -> 100``.  Below the
        critical threshold the score continues to fall linearly and reaches 0
        one further "critical band" away.  This keeps the score interpretable
        ("50 means exactly at the critical threshold") instead of being an
        opaque statistical transform.
        """
        if value is None or (isinstance(value, float) and math.isnan(value)):
            return None
        crit, warn, target = self.critical_threshold, self.warning_threshold, self.target
        if self.direction == LOWER_IS_BETTER:
            # Mirror onto a higher-is-better axis so one implementation serves both.
            value = -value
            crit = None if crit is None else -crit
            warn = None if warn is None else -warn
            target = None if target is None else -target
        elif self.direction != HIGHER_IS_BETTER:
            return None
        anchors: list[tuple[float, float]] = []
        if crit is not None:
            anchors.append((crit, 50.0))
        if warn is not None:
            anchors.append((warn, 75.0))
        if target is not None:
            anchors.append((target, 100.0))
        anchors = sorted(set(anchors))
        if len(anchors) < 2:
            return None
        band = anchors[1][0] - anchors[0][0]
        if band <= 0:
            return None
        # Below the worst configured anchor the score keeps falling and reaches
        # zero two threshold-bands further out, so a bad value degrades
        # smoothly instead of slamming into 0 the moment it clears critical.
        low_x = anchors[0][0] - 2 * band
        anchors.insert(0, (low_x, max(0.0, anchors[0][1] - 50.0)))
        if value <= anchors[0][0]:
            return 0.0
        if value >= anchors[-1][0]:
            return 100.0
        for (x0, y0), (x1, y1) in zip(anchors, anchors[1:]):
            if x0 <= value <= x1:
                if x1 == x0:
                    return y1
                return y0 + (y1 - y0) * (value - x0) / (x1 - x0)
        return None  # pragma: no cover - unreachable

    # -- loading ----------------------------------------------------------
    @classmethod
    def from_dict(cls, raw: Mapping[str, Any], defaults: Mapping[str, Any]) -> "MetricDefinition":
        key = str(raw.get("key", "")).strip()
        if not key:
            raise MetricConfigError("every metric needs a 'key'")
        merged: dict[str, Any] = {**defaults, **{k: v for k, v in raw.items() if v is not None}}
        direction = str(merged.get("direction", NEUTRAL))
        if direction not in DIRECTIONS:
            raise MetricConfigError(f"metric {key!r}: unknown direction {direction!r}")
        unit = str(merged.get("unit", "number"))
        if unit not in UNITS:
            raise MetricConfigError(f"metric {key!r}: unknown unit {unit!r}")
        stored_as = str(merged.get("stored_as", "raw"))
        if stored_as not in STORED_AS:
            raise MetricConfigError(f"metric {key!r}: unknown stored_as {stored_as!r}")

        warn = _as_float(merged.get("warning_threshold"), "warning_threshold", key)
        crit = _as_float(merged.get("critical_threshold"), "critical_threshold", key)
        if warn is not None and crit is not None:
            if direction == HIGHER_IS_BETTER and crit > warn:
                raise MetricConfigError(
                    f"metric {key!r}: higher_is_better needs critical_threshold <= warning_threshold"
                )
            if direction == LOWER_IS_BETTER and crit < warn:
                raise MetricConfigError(
                    f"metric {key!r}: lower_is_better needs critical_threshold >= warning_threshold"
                )
        weight = _as_float(merged.get("weight"), "weight", key) or 0.0
        if weight < 0:
            raise MetricConfigError(f"metric {key!r}: weight must be >= 0")

        anomaly_defaults = AnomalyRule.from_dict(defaults.get("anomaly"), AnomalyRule())
        return cls(
            key=key,
            name=str(merged.get("name", key)),
            unit=unit,
            stored_as=stored_as,
            direction=direction,
            warning_threshold=warn,
            critical_threshold=crit,
            target=_as_float(merged.get("target"), "target", key),
            target_min=_as_float(merged.get("target_min"), "target_min", key),
            target_max=_as_float(merged.get("target_max"), "target_max", key),
            weight=weight,
            include_in_score=bool(merged.get("include_in_score", False)),
            alert_on_weekly_change=bool(merged.get("alert_on_weekly_change", True)),
            percentage_change_threshold=_as_float(
                merged.get("percentage_change_threshold"), "percentage_change_threshold", key
            ),
            absolute_change_threshold=_as_float(
                merged.get("absolute_change_threshold"), "absolute_change_threshold", key
            ),
            critical_change_multiplier=float(merged.get("critical_change_multiplier", 2.0)),
            consecutive_decline_periods=int(merged.get("consecutive_decline_periods", 3)),
            min_denominator=(
                None if merged.get("min_denominator") is None else int(merged["min_denominator"])
            ),
            maturity_lag={
                str(k): int(v) for k, v in (merged.get("maturity_lag") or {}).items()
            },
            numerator=merged.get("numerator"),
            denominator=merged.get("denominator"),
            decimals=int(merged.get("decimals", 2)),
            aliases=tuple(str(a) for a in (raw.get("aliases") or ())),
            group=str(merged.get("group", "Quality")),
            family=(str(raw["family"]) if raw.get("family") else None),
            description=str(merged.get("description", "")),
            needs_review=bool(merged.get("needs_review", False)),
            review_note=str(merged.get("review_note", "")),
            level_overrides={
                str(level): dict(values)
                for level, values in (raw.get("level_overrides") or {}).items()
            },
            anomaly=AnomalyRule.from_dict(raw.get("anomaly"), anomaly_defaults),
        )

    def to_dict(self) -> dict[str, Any]:
        """Round-trippable representation (used by the admin editor)."""
        return {
            "key": self.key,
            "name": self.name,
            "aliases": list(self.aliases),
            "group": self.group,
            "family": self.family,
            "unit": self.unit,
            "stored_as": self.stored_as,
            "direction": self.direction,
            "warning_threshold": self.warning_threshold,
            "critical_threshold": self.critical_threshold,
            "target": self.target,
            "target_min": self.target_min,
            "target_max": self.target_max,
            "weight": self.weight,
            "include_in_score": self.include_in_score,
            "alert_on_weekly_change": self.alert_on_weekly_change,
            "percentage_change_threshold": self.percentage_change_threshold,
            "absolute_change_threshold": self.absolute_change_threshold,
            "critical_change_multiplier": self.critical_change_multiplier,
            "consecutive_decline_periods": self.consecutive_decline_periods,
            "min_denominator": self.min_denominator,
            "maturity_lag": dict(self.maturity_lag),
            "numerator": self.numerator,
            "denominator": self.denominator,
            "decimals": self.decimals,
            "description": self.description,
            "needs_review": self.needs_review,
            "review_note": self.review_note,
            "level_overrides": {k: dict(v) for k, v in self.level_overrides.items()},
            "anomaly": {
                "enabled": self.anomaly.enabled,
                "lookback": self.anomaly.lookback,
                "min_history": self.anomaly.min_history,
                "z_threshold": self.anomaly.z_threshold,
                "relative_threshold": self.anomaly.relative_threshold,
                "method": self.anomaly.method,
            },
        }


def _normalise_alias(text: str) -> str:
    """Collapse an Excel header into a comparable token.

    Case, spacing and punctuation are ignored so that ``Rejects Before Debit 1%``
    and ``rejects_before_debit_1_%`` land on the same metric.  ``%`` and ``+``
    are *not* discarded -- they are the only thing separating ``Debit 1`` (a
    count) from ``Debit 1%`` (a rate), and dropping them silently merged two
    different metrics.
    """
    out: list[str] = []
    for ch in str(text).strip().lower():
        if ch.isalnum():
            out.append(ch)
        elif ch == "%":
            out.append(" pct ")
        elif ch == "+":
            out.append(" plus ")
        elif out and out[-1] != " ":
            out.append(" ")
    return " ".join("".join(out).split())


class MetricRegistry:
    """The loaded set of metric definitions, with alias lookup."""

    def __init__(self, metrics: Sequence[MetricDefinition], meta: Mapping[str, Any] | None = None):
        self._metrics: dict[str, MetricDefinition] = {}
        self._alias_index: dict[str, str] = {}
        self.meta = dict(meta or {})
        for metric in metrics:
            self.add(metric)

    # -- container protocol ----------------------------------------------
    def add(self, metric: MetricDefinition) -> None:
        if metric.key in self._metrics:
            raise MetricConfigError(f"duplicate metric key {metric.key!r}")
        self._metrics[metric.key] = metric
        for token in (metric.key, metric.name, *metric.aliases):
            alias = _normalise_alias(token)
            if not alias:
                continue
            owner = self._alias_index.get(alias)
            if owner and owner != metric.key:
                raise MetricConfigError(
                    f"alias {token!r} maps to both {owner!r} and {metric.key!r}"
                )
            self._alias_index[alias] = metric.key

    def __contains__(self, key: object) -> bool:
        return key in self._metrics

    def __iter__(self) -> Iterator[MetricDefinition]:
        return iter(self._metrics.values())

    def __len__(self) -> int:
        return len(self._metrics)

    def __getitem__(self, key: str) -> MetricDefinition:
        try:
            return self._metrics[key]
        except KeyError as exc:
            raise MetricConfigError(f"unknown metric {key!r}") from exc

    def get(self, key: str) -> MetricDefinition | None:
        return self._metrics.get(key)

    # -- queries ----------------------------------------------------------
    @property
    def keys(self) -> list[str]:
        return list(self._metrics)

    def resolve(self, header: str) -> MetricDefinition | None:
        """Map an arbitrary Excel column header onto a metric definition."""
        return self._metrics.get(self._alias_index.get(_normalise_alias(header), ""))

    def for_level(self, key: str, level: str) -> MetricDefinition:
        return self[key].for_level(level)

    def scored(self) -> list[MetricDefinition]:
        return [m for m in self._metrics.values() if m.scored]

    def alerting(self) -> list[MetricDefinition]:
        return [m for m in self._metrics.values() if m.direction != NEUTRAL]

    def needing_review(self) -> list[MetricDefinition]:
        return [m for m in self._metrics.values() if m.needs_review]

    def groups(self) -> dict[str, list[MetricDefinition]]:
        out: dict[str, list[MetricDefinition]] = {}
        for m in self._metrics.values():
            out.setdefault(m.group, []).append(m)
        return out

    def total_weight(self) -> float:
        return sum(m.weight for m in self.scored())

    # -- persistence ------------------------------------------------------
    @classmethod
    def load(cls, path: str | Path | None = None) -> "MetricRegistry":
        path = Path(path or METRICS_FILE)
        if not path.exists():
            raise MetricConfigError(f"metric config not found: {path}")
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "MetricRegistry":
        defaults = dict(raw.get("defaults") or {})
        entries = raw.get("metrics") or []
        if not entries:
            raise MetricConfigError("metric config contains no metrics")
        metrics = [MetricDefinition.from_dict(entry, defaults) for entry in entries]
        meta = {k: v for k, v in raw.items() if k not in ("metrics", "defaults")}
        meta["defaults"] = defaults
        return cls(metrics, meta)

    def dump(self, path: str | Path) -> None:
        """Write the registry back to YAML (admin threshold edits)."""
        payload = {
            "version": self.meta.get("version", 1),
            "defaults": self.meta.get("defaults", {}),
            "metrics": [m.to_dict() for m in self._metrics.values()],
        }
        Path(path).write_text(
            yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8"
        )


_REGISTRY: MetricRegistry | None = None


def get_registry(path: str | Path | None = None, reload: bool = False) -> MetricRegistry:
    """Process-wide registry cache."""
    global _REGISTRY
    if _REGISTRY is None or reload or path is not None:
        _REGISTRY = MetricRegistry.load(path)
    return _REGISTRY
