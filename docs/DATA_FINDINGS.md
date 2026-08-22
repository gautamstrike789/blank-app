# Phase 1 — what the Master Report actually contains

Source: `Master_Report__140826.xlsx` (28 MB, 9 sheets, received 14 Aug 2026).

## 1. It is a Power Pivot workbook, not a data extract

Every sheet is an OLAP PivotTable rendered over an embedded Power Pivot model
(`xl/model/item.data`, 27.8 MB). The visible cells are pivot output, not source
rows.

| Sheet | Header row | Contents |
|---|---|---|
| `airport` | 12 | Month × 18 metrics, Dec 2023 – Aug 2026 |
| `Monthly` | 10 | Month × 16 metrics (subset of `airport`) |
| `Waterfall` | 4 | Month × D1–D12 ladder, plus a Client D2–D12 block at row 51 |
| `Waterfall new` | 10 | Month × D1–D12 ladder |
| `Template (7)`–`(11)` | — | Empty pivot stubs (a single `Sum of SUBMISSION` cell) |

## 2. The Owner and BA dimensions exist but are not exported

The data model defines these dimensions:

`OWNER NAME`, `BAName`, `CITY`, `EventName`, `SourceofDonation`, `MONTH`,
`Events`, `Below 30`, `SigninDT` (+ Year, + Month)

In every sheet, **`OWNER NAME` and `BAName` are set to `All`**. The workbook
therefore contains organisation-level monthly totals only — no Owner rows, no BA
rows, and no weekly grain.

> **This is the single biggest gap between the brief and the data.** The brief
> asks for Owner-level and BA-level monitoring; this file cannot support either.
> The system reads it, loads it, and says so explicitly (`org_level_only`
> warning) rather than silently producing an empty Owner dashboard.

## 3. Metric inventory (42 configured)

| Group | Metrics |
|---|---|
| Headline quality | `d1_pct` (Debit 1 %), `d3_pct` (Debit 3 %), `rjbd1_pct` (Rejects before Debit 1 %), `net_loss_pct` |
| Debit ladder | `d2_pct`, `d4_pct` … `d12_pct` |
| Client ladder | `client_d2_pct` … `client_d12_pct` |
| Segment | `subs_below_30_pct`, `subs_40_plus_pct`, `ads_30_minus`, `ads_40_plus`, `ot_pct` |
| Value | `avg_donation_amount` |
| Volume | `submissions`, `debit1_count`, `debit3_count`, `rjbd1_count`, `net_loss_count`, `pledge_to_ot_count`, `subs_below_30_count`, `subs_40_plus_count`, `donation_amount`, `client_dt_count`, `supports` |

Terminology mapping from the brief: **RJBD1** = `Rejects Before Debit 1%`,
**Debit 1** = `D1%`, **Debit 3** = `D3%`.

## 4. Arithmetic verified against the file

Every rate is denominated on `SUBMISSION`. Checked on Jan 2024:

| Check | Computed | Stated | ✓ |
|---|---|---|---|
| `debit1 / submissions` | 8084 / 9120 = 88.64% | D1% 0.8864 | ✓ |
| `rjbd1 / submissions` | 1025 / 9120 = 11.24% | 0.1124 | ✓ |
| `debit3 / submissions` | 6411 / 9120 = 70.30% | D3% 0.7030 | ✓ |
| `net_loss / submissions` | 1887 / 9120 = 20.69% | 0.2069 | ✓ |

This relationship is enforced on every future upload by the `ratio_mismatch`
validation rule. Percentages are stored as fractions (0.8864) in the file and
converted to 0–100 on load.

The debit ladder is monotonic (D1 ≥ D2 ≥ … ≥ D12), which the
`ladder_not_monotonic` rule checks.

## 5. Observed distributions (organisation, monthly, n=33)

| Metric | min | p25 | median | p75 | max |
|---|---|---|---|---|---|
| D1% | 85.6 | 87.6 | 88.3 | 89.5 | 90.6 |
| D3% | 61.7 | 69.5 | 70.6 | 72.3 | 73.3 |
| RJBD1% | 9.3 | 10.3 | 11.0 | 11.8 | 12.8 |
| Net loss% | 12.7 | 20.2 | 20.8 | 21.6 | 23.3 |
| Avg donation | 600 | 759 | 765 | 778 | 903 |
| Submissions/month | 3 | 8 866 | 9 533 | 10 800 | 12 588 |

All configured thresholds are calibrated on these, and are starting points for
the business owner to tune in **Admin → Metric rules**.

## 6. Debit 3 maturity

`D3%` reads **0.0% for Jul and Aug 2026** and `DEBIT3` reads 0. This is not a
quality collapse — donors signed in those months have not reached a third debit
yet. The system models this as `maturity_lag` (3 monthly periods for Debit 3)
and suppresses alerting on immature periods. Without it, every weekly run would
produce a guaranteed false RED on the organisation's second-heaviest metric.

## 7. Volume, and what it means for BA-level monitoring

The organisation signs roughly **10,000 submissions a month ≈ 2,300 a week**
across ~650 BAs — about **3.6 submissions per BA per week**.

A Debit 1 rate on 4 submissions can only be 0%, 25%, 50%, 75% or 100%. One extra
reject moves RJBD1 by 25 points. **Weekly per-BA quality rates are, at this
volume, arithmetic noise.**

The system's answer is not to pretend otherwise:

1. BA, team and Owner levels are evaluated on a **4-week rolling window** —
   counts summed, rates recomputed from the sums (never averaged).
2. Every rate movement is put through a **two-proportion z-test** before it is
   allowed to escalate. A move from 0% to 6.3% on 16 submissions is reported and
   charted, but not escalated, and the alert says how many submissions would be
   needed to call it.
3. Sharp single-week events are not lost: each period is judged on **both** the
   single-week and the rolling basis, and the more serious verdict wins. Each
   alert states which view produced it.

Measured effect on 12 weeks of realistic sample data (650 BAs, 44 Owners):
critical alerts fell from **174 — almost all of them a single reject moving a
rate by several points — to 16**, every one statistically confirmed.

The honest cost of that: some real movements at BA level can no longer be
called. An RJBD1 move from 2.2% to 4.0% needs roughly **180 submissions** to be
distinguished from chance, and a BA has about 39 a week. Such movements remain
visible on the BA's trend chart (with submission volume drawn behind the rate)
and roll up into their Owner's figures, where the sample does support a verdict.
This is a property of the volumes, not of the software — and stating it is more
useful to leadership than a dashboard that manufactures confidence.

## 8. Assumptions made, and what needs confirming

| # | Assumption | Impact if wrong |
|---|---|---|
| 1 | The weekly report will be published as a **flat sheet, one row per BA per week**, with `OWNER NAME` and `BAName` columns | Owner/BA dashboards stay empty; only org totals work |
| 2 | Thresholds calibrated from observed distributions are reasonable starting points | Alert volume is wrong until tuned in Admin |
| 3 | `ot_pct`, `subs_below_30_pct` and `subs_40_plus_pct` have **no confirmed good direction** | Marked `needs_review`; tracked and charted, never alerted, zero score weight |
| 4 | Score weights: D1 20, D3 20, RJBD1 25, Net loss 12, D6 8, D12 8, Avg donation 7 | Score emphasis is wrong; changeable in Admin |
| 5 | Weekly maturity lag is **0** (the export is assumed to be a point-in-time snapshot, not a cohort snapshot) | If weekly Debit 3 is cohort-based, recent weeks will produce false alerts |
| 6 | A BA appearing under a new Owner has moved teams; the latest report wins | Historical attribution shifts |

## 9. Open questions for the business

These need answers from someone who knows the operation; the system is usable
without them but will be sharper with them.

1. **Can the weekly export include `OWNER NAME` and `BAName` columns?** This is
   the one blocker for Owner-level and BA-level monitoring.
2. **Is a higher or lower `OT%` (pledge → one-time) good?** Same question for
   `Subs Below 30%` and `40+ %`.
3. **What is the real Debit 3 target?** 68% / 64% are calibrated from history,
   not from a business commitment.
4. **Is RJBD1 at ~11% acceptable, or is there a target?** The brief's example
   thresholds (2.5% / 3.5%) are an order of magnitude below observed reality, so
   they were not used.
5. **Is the weekly Debit 3 figure a cohort snapshot or a point-in-time
   snapshot?** This decides assumption 5 above.
6. **At what BA volume should an individual BA be judged?** The default is 15
   submissions in the rolling window; the statistics argue for more.
