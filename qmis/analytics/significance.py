"""Is this movement distinguishable from chance?

A BA signs about four donors a week.  Over a four-week window that is roughly
fourteen submissions, and on fourteen submissions a single extra reject moves
RJBD1 from 0.0% to 7.1%.  A flat "minimum sample" rule cannot tell that apart
from a real collapse, because the sample size that makes a 7-point move
meaningful depends on the rate: at a 50% base rate fourteen cases is coarse; at
an 11% base rate it is useless.

So instead of a magic number, rate metrics are tested properly:

``compare_periods`` - a two-proportion z-test between this period's rate and
the previous period's, pooled under the null hypothesis that nothing changed.

``compare_to_threshold`` - a one-sample z-test asking whether the rate is
significantly the wrong side of its threshold, rather than merely on the wrong
side of it.

Both report a z statistic and the sample size that *would* have been needed, so
the dashboard can say "this looks bad but we cannot yet tell" instead of either
crying wolf or staying silent.

Metrics with no numerator/denominator (currency, counts, unclassified numbers)
have no proportion to test and are passed through as significant; they are
governed by their configured change thresholds alone.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

# Two-sided normal critical values, so no SciPy dependency is needed for what
# is ultimately a lookup of three numbers.
Z_CRITICAL = {0.90: 1.6449, 0.95: 1.9600, 0.99: 2.5758}
# 90% rather than 95%: this is operational monitoring, not publication. Missing
# a real deterioration for a week costs more than looking twice at one that
# turns out to be noise, and every alert states its own statistical footing.
DEFAULT_CONFIDENCE = 0.90


@dataclass(frozen=True)
class SignificanceResult:
    """Outcome of a proportion test, in terms an alert can quote."""

    testable: bool
    significant: bool
    z_score: float | None = None
    confidence: float = DEFAULT_CONFIDENCE
    required_n: int | None = None
    note: str = ""

    def describe(self, metric=None) -> str:
        if not self.testable:
            return self.note or "Not statistically testable."
        if self.significant:
            return (
                f"This movement is larger than sampling noise "
                f"(z = {self.z_score:.1f} at {self.confidence:.0%} confidence)."
            )
        need = f" About {self.required_n:,} submissions would be needed to confirm it." if self.required_n else ""
        return (
            f"On this sample the movement is within the range of chance "
            f"(z = {abs(self.z_score):.1f}, below {Z_CRITICAL[self.confidence]:.2f})."
            + need
        )


def _z_critical(confidence: float) -> float:
    return Z_CRITICAL.get(round(confidence, 2), Z_CRITICAL[DEFAULT_CONFIDENCE])


def compare_periods(
    current_rate: float | None,
    current_n: float | None,
    previous_rate: float | None,
    previous_n: float | None,
    confidence: float = DEFAULT_CONFIDENCE,
) -> SignificanceResult:
    """Two-proportion z-test between two periods. Rates are 0-100."""
    if any(v is None for v in (current_rate, current_n, previous_rate, previous_n)):
        return SignificanceResult(False, True, note="No denominator recorded; not testable.")
    n1, n2 = float(current_n), float(previous_n)
    if n1 <= 0 or n2 <= 0:
        return SignificanceResult(False, True, note="No denominator recorded; not testable.")
    p1, p2 = float(current_rate) / 100.0, float(previous_rate) / 100.0
    if not (0.0 <= p1 <= 1.0 and 0.0 <= p2 <= 1.0):
        return SignificanceResult(False, True, note="Value is not a proportion; not testable.")

    pooled = (p1 * n1 + p2 * n2) / (n1 + n2)
    variance = pooled * (1 - pooled) * (1 / n1 + 1 / n2)
    if variance <= 0:
        # Both periods identical, or a degenerate 0%/100% pair.
        return SignificanceResult(
            True, p1 != p2, z_score=0.0 if p1 == p2 else float("inf"), confidence=confidence
        )
    z = (p1 - p2) / float(np.sqrt(variance))
    critical = _z_critical(confidence)
    significant = abs(z) >= critical
    required = None
    if not significant and p1 != p2:
        # n per group needed to detect this same difference, holding the pooled
        # variance fixed.  Deliberately approximate: it answers "roughly how
        # much more data", not "run this power analysis".
        gap = abs(p1 - p2)
        required = int(np.ceil(2 * pooled * (1 - pooled) * (critical / gap) ** 2))
    return SignificanceResult(True, significant, z, confidence, required)


def compare_to_threshold(
    rate: float | None,
    n: float | None,
    threshold: float | None,
    higher_is_better: bool,
    confidence: float = DEFAULT_CONFIDENCE,
) -> SignificanceResult:
    """One-sample z-test: is the rate significantly past ``threshold``?"""
    if rate is None or n is None or threshold is None:
        return SignificanceResult(False, True, note="No denominator or threshold; not testable.")
    n = float(n)
    if n <= 0:
        return SignificanceResult(False, True, note="No denominator recorded; not testable.")
    p, p0 = float(rate) / 100.0, float(threshold) / 100.0
    if not (0.0 <= p <= 1.0 and 0.0 <= p0 <= 1.0):
        return SignificanceResult(False, True, note="Value is not a proportion; not testable.")
    note = ""
    if p0 <= 0.0 or p0 >= 1.0:
        # A reference rate of exactly 0% (or 100%) has zero variance, which
        # would make every subsequent value infinitely significant. A BA whose
        # rejects were 0, 0, 0, 0 has not proved their true rate is zero - only
        # that it is below roughly 3/n (the rule of three). Testing against
        # that bound instead is what stops "0% -> 6%" reading as a catastrophe.
        bound = min(0.5, max(3.0 / n, 1e-6))
        p0 = bound if p0 <= 0.0 else 1.0 - bound
        note = (
            f"The reference level was {threshold:.1f}%, which on this much data cannot be "
            f"distinguished from {p0 * 100:.1f}%; the stricter bound was used."
        )
    variance = p0 * (1 - p0) / n
    if variance <= 0:  # pragma: no cover - unreachable after the guard above
        return SignificanceResult(True, p != p0, z_score=0.0, confidence=confidence)
    z = (p - p0) / float(np.sqrt(variance))
    critical = _z_critical(confidence)
    breached = (z < -critical) if higher_is_better else (z > critical)
    required = None
    if not breached and p != p0:
        gap = abs(p - p0)
        required = int(np.ceil(p0 * (1 - p0) * (critical / gap) ** 2))
    return SignificanceResult(True, bool(breached), z, confidence, required, note)


def movement_z(
    current_rate: np.ndarray,
    current_n: np.ndarray,
    previous_rate: np.ndarray,
    previous_n: np.ndarray,
) -> np.ndarray:
    """Vectorised two-proportion z, NaN where the test does not apply."""
    p1, p2 = current_rate / 100.0, previous_rate / 100.0
    n1, n2 = current_n, previous_n
    valid = (
        np.isfinite(p1) & np.isfinite(p2) & np.isfinite(n1) & np.isfinite(n2)
        & (n1 > 0) & (n2 > 0) & (p1 >= 0) & (p1 <= 1) & (p2 >= 0) & (p2 <= 1)
    )
    out = np.full(p1.shape, np.nan)
    if not valid.any():
        return out
    with np.errstate(divide="ignore", invalid="ignore"):
        pooled = (p1 * n1 + p2 * n2) / (n1 + n2)
        variance = pooled * (1 - pooled) * (1 / n1 + 1 / n2)
        z = np.where(variance > 0, (p1 - p2) / np.sqrt(np.where(variance > 0, variance, 1.0)), np.nan)
        # Identical rates are a real "no change", not an untestable one.
        z = np.where((variance <= 0) & (p1 == p2), 0.0, z)
    out[valid] = z[valid]
    return out


def threshold_z(
    rate: np.ndarray, n: np.ndarray, threshold: float | None
) -> np.ndarray:
    """Vectorised one-sample z against a fixed threshold."""
    out = np.full(rate.shape, np.nan)
    if threshold is None:
        return out
    p, p0 = rate / 100.0, float(threshold) / 100.0
    valid = np.isfinite(p) & np.isfinite(n) & (n > 0) & (p >= 0) & (p <= 1)
    if not valid.any() or not (0.0 < p0 < 1.0):
        return out
    variance = p0 * (1 - p0) / np.where(n > 0, n, 1.0)
    with np.errstate(divide="ignore", invalid="ignore"):
        z = (p - p0) / np.sqrt(variance)
    out[valid] = z[valid]
    return out
