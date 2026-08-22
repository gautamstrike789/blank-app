# Implementation log

The record of what was built, what broke when it was tested, and what changed as
a result. Kept because the most useful parts of this system are the ones that
came from a failed test rather than from the plan.

---

## Phase 1 — Understand the data

Inspected `Master_Report__140826.xlsx`: 9 sheets, 28 MB, all of it OLAP
PivotTables over an embedded Power Pivot model.

**What was found:** 42 metrics; verified every rate against its own
numerator/denominator; the debit ladder is monotonic; percentages are stored as
fractions; the file spans Dec 2023 – Aug 2026.

**What was found that changed the design:**

1. `OWNER NAME` and `BAName` exist as dimensions but are filtered to `All` in
   every sheet — the file has no Owner or BA rows at all.
2. `D3%` reads 0% for the two most recent months because those donors have not
   reached a third debit yet.
3. The organisation signs ~2,300 submissions a week across ~650 BAs.

(2) became the `maturity_lag` mechanism. (3) drove the entire statistical layer.
(1) is documented as the one thing the business must change to get the Owner and
BA monitoring the brief asks for.

Full write-up: [DATA_FINDINGS.md](DATA_FINDINGS.md).

---

## Phase 2 — Metric configuration engine

42 metrics in `metrics.yaml`, each with direction, thresholds, weight, change
sensitivity, anomaly settings, sample floors and maturity lag.

**Broke in testing:**

- The alias index rejected the config outright: `Debit 1` (a count) and
  `Debit 1%` (a rate) normalised to the same token because the normaliser
  stripped all punctuation. The registry catching its own ambiguity was the
  right behaviour; the fix was to make `%` and `+` meaningful in aliases.
- Threshold comparisons were exclusive, so a value landing exactly on the
  critical line read as WARNING. Made inclusive — over-alerting by one hair is
  the cheaper mistake.
- The quality-score curve fell from 50 to 0 within one threshold band, making
  the score hypersensitive just below critical. Widened to two bands.

---

## Phase 3 — Database and ingestion

Long-format facts, self-referencing entities, non-destructive supersession, a
swappable storage backend, and 11 validation rules.

**Broke in testing:**

- The reader assumed a `Week` column held a week *number*; the generated files
  held a full period key (`2026-W33`), so every row was dropped and all 12 files
  were rejected with "no usable data rows". Now tries a period key, then a
  number, then a date.
- `Settings.section()` returned the raw YAML block and bypassed environment
  overrides, so `QMIS_INBOX` was silently ignored and the watcher reported
  "nothing new" while staring at 12 files.
- The `ladder_not_monotonic` rule fired on 200+ rows of the sample data. The
  rule was right and the *generator* was wrong — it was drawing each debit stage
  independently. Fixing the generator rather than relaxing the check.

---

## Phase 4 — Alerts, anomalies and scoring

This phase was rewritten three times, and each rewrite came from looking at the
output rather than at the code.

### First working version

44 seconds per evaluation, and the results were unusable:

- 174 critical alerts, almost all of them a single reject moving a BA's RJBD1 by
  7 points on a 15-submission sample.
- One Owner produced nine near-identical REDs because the debit ladder moved
  together — the ninth pushed everything else off the list.
- Three-week runs of 0.1 pp drift escalated to RED.
- `Debit 3` — a 20-weight headline metric — never alerted at all, because the
  weekly maturity lag suppressed it.

### What changed, and why

**Performance (44 s → 9 s).** `matrix.py`: pivot the history into one dense
`(entity × metric) × period-offset` numpy array once, and share it between
comparison, streak detection and anomaly scoring. Replaced ~27,000 Python
`groupby` iterations with a handful of vectorised passes.

**Trend rules judged on the wrong quantity.** A "4-week decline" was being
escalated on the size of its *last step*. Now judged on the cumulative move
across the whole run — and, because consecutive rolling windows share three of
their four weeks, on a test between the run's two endpoints, which do not
overlap.

**Rolling windows.** At 3.6 submissions per BA per week, weekly per-BA rates are
arithmetic. BA, team and Owner levels now evaluate on a 4-week window: counts
summed, rates recomputed from the sums, never averaged.

**Dual basis.** Rolling fixed the noise and introduced a new failure — a genuine
one-week collapse diluted to a quarter of its size. Every period is now judged
on *both* the single-week and the rolling view, the more serious verdict wins,
and each alert states which produced it.

**Statistical significance.** A flat "minimum sample" rule cannot work, because
the sample size that makes a 7-point move meaningful depends on the base rate.
Rate movements and threshold breaches now go through two-proportion z-tests. An
unconfirmed breach is reported one level down rather than dropped, and the alert
says how many more submissions would settle it.

**Cascade grouping.** Metrics tagged with a shared `family` collapse into one
composite finding when three or more trip together.

**Weekly maturity lag set to 0.** The monthly lag is evidenced by the file
itself (Debit 3 reads 0% for immature months). The weekly equivalent depends on
whether the weekly export is a cohort or point-in-time snapshot — unknown, so
suppressing the organisation's second-heaviest metric on an unverified guess was
the wrong default. Flagged as open question 5.

### Result

Critical alerts fell from **174 to 16** on identical data, and every remaining
one is statistically confirmed. The five planted scenarios came out like this:

| Planted scenario | BA's weekly volume | Outcome |
|---|---|---|
| Debit 1 collapses 85% → 76% | ~41 | 🟠 ORANGE, threshold, single-period basis — flagged, with the note that 41 submissions cannot yet confirm the breach |
| Debit 3 climbs 62% → 84% | ~34 | 🟢 Sustained improvement, 11 periods running, +17.4 pp |
| Debit 1 halves on 4 submissions | 4 | Suppressed — sample too small, as intended |
| RJBD1 stable ~2.2% then 4.0% | ~39 | **No alert** |
| Debit 1 declines 5 periods running | ~33 | **No alert** |

The last two are the uncomfortable, and correct, result. An RJBD1 move from
2.2% to 4.0% needs about **180 submissions** to be distinguishable from chance;
this BA has 39 a week, or 155 across the rolling window. The brief's own example
of that scenario is real — but at this organisation's volumes it can only be
called at Owner level or above, not for an individual BA.

The system's answer is to say so rather than to lower the bar. Both movements
are visible on the BA's trend chart with submission volume drawn behind them,
and both roll up into their Owner's figures where the sample supports a verdict.
Lowering the threshold enough to catch them was tried, and it is what produced
174 criticals of which almost none were real.

---

## Phase 5 — Dashboard and access control

Seven pages, role-based navigation, scoping enforced in the query.

**Broke when the app was actually opened in a browser:**

- Every page was bound with a lambda, so Streamlit derived the same URL
  pathname (`<lambda>`) for all of them and the app failed to start. Fixed with
  explicit `url_path`s.
- The executive header read "Reporting: 4 BAs" — it was counting BAs with enough
  volume to be *scored*, not BAs who reported. Alarming, and wrong.
- Alert headlines rendered as literal `**asterisks**`: markdown is not processed
  inside a raw HTML block.
- The Owner chart put the Owner needing most attention at the bottom.
- Trends showed "No history for this combination" for the organisation, because
  Owner and Org series were derived at evaluation time and never stored. Now
  persisted with an `is_derived` flag, so trends, exports and alerts all quote
  the same aggregate instead of each re-deriving it.

All seven pages verified by screenshot against 12 weeks of loaded data.

---

## Phase 6 — Automation and notifications

Folder watcher, digest routing with anti-repeat rules, four channel adapters, a
full CLI.

Verified end to end: 12 files dropped in a folder → 12 loads → 12 evaluations →
digests built and routed → files archived → owner accounts auto-linked to their
entity, in 126 seconds with no manual step.

---

## Phase 7 — Tests and documentation

**93 tests**, plus 7 that run against the real Master Report when
`QMIS_MASTER_REPORT` is set.

**Found by the tests, fixed in the code:**

- `significance.py` exported functions named `test_movement` and
  `test_threshold`, which pytest collected as tests. Renamed to
  `compare_periods` and `compare_to_threshold`.
- The significance gate was suppressing the brief's own flagship RJBD1 example.
  The two-period z-test on 380 submissions was inconclusive, so it zeroed the
  severity — even though the anomaly detector had the same movement at 6.4
  sigma against eight weeks of history. A weaker test was overriding a stronger
  one. `statistically_confirmed` is now decided by *the test behind the severity
  that was assigned*, not by whichever test happened to fail.
- The first fix for that over-corrected: exempting the anomaly path from sample
  checks let through a BA going from 1 reject in 22 to 4 in 30. The anomaly
  z-score is computed on a series of *rates*, and when each rests on ~30
  submissions a run that lands close together gives a deceptively narrow band.
  An anomaly is now confirmed by a one-sample proportion test against its own
  baseline, which is sample-aware.
- Testing against a baseline of exactly 0% divided by zero variance and made
  every subsequent value infinitely significant. A BA with 0,0,0,0 rejects has
  not proved their true rate is zero — only that it is below roughly 3/n. The
  rule-of-three bound is used instead.
- A belt-and-braces invariant was added: nothing reaches RED on evidence the
  sample cannot support. It is still reported, one level down, saying so.
- The YELLOW trend fallback ignored streak significance, so four weeks of
  0.05 pp drift still produced an alert.

Two test failures turned out to be wrong *expectations* rather than bugs — a
streak across a data gap is 1, not 2, and RBAC scoping was correct but the
assertion was reading fact rows that did not exist yet. Both tests were
rewritten to assert the real behaviour.

---

## Known limitations

1. **The supplied workbook cannot drive Owner or BA dashboards.** It has no
   Owner or BA rows. The system loads it, warns clearly, and works at
   organisation level. This is the one thing the business must change.
2. **BA-level weekly rates remain statistically thin** even on a 4-week window.
   The system is honest about this rather than manufacturing confidence.
3. **Three metrics have no confirmed direction** and are therefore inert.
4. **The cloud storage backend is a documented stub.** A synced Drive/OneDrive
   folder delivers the same workflow today; building an OAuth integration before
   the organisation has chosen a platform would be work thrown away.
5. **Thresholds are calibrated from history, not from business targets.**
