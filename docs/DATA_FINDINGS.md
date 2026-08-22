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

---

# Part 2 — the raw source file (`Raw_Data.xlsb`, 45 MB)

Profiled 22 Aug 2026 via Colab, reading the file in place from Drive. This is
the file the Master Report is pivoted from, and it answers most of Part 1's
open questions.

## 1. It is donation-level, not a BA-week summary

| Sheet | Shape | What it is |
|---|---|---|
| **`DATA`** | **308,922 x 96** | **One row per submission.** The fact table. |
| `Owner Details` | 72 x 7 | Owner master: CODE, OWNER NAME, ORG, CITY, REGION, COMPANY NAME |
| `Owner Indv` | 52 x 5 | 52 Sr. Leader owners with codes and full names |
| `Sheet1` | 3,993 x 7 | Billing-failure reason lookup |
| `Sheet2`, `Sheet3` | 5 x 96, 94 x 6 | Scratch / column checklists |

**308,922 is exactly the Master Report's `Sum of SUBMISSION` Grand Total.** The
source is confirmed, and the grain question from Part 1 §9.1 is answered: one
row per submission, with the full hierarchy on every row.

## 2. Every measure reproduced exactly

| Measure | Raw source | Total | Master Report |
|---|---|---|---|
| `debit1` | flag column | 272,881 | 272,881 ✓ |
| `DEBIT3` | flag column | 208,147 | 208,147 ✓ |
| `Pledge To OT` | flag column | 30,166 | 30,166 ✓ |
| `Below 30` | flag column | 84,273 | 84,273 ✓ |
| `Average of Donamt` | mean | 778.3138 | 778.3138 ✓ |

Three measures are not columns at all and were solved from those totals:

```
RJBD1    = Insuff b4 debit + Stop b4 debit + Tech Error + Other Errors
           20,856 + 1,237 + 10,047 + 2,067            = 34,207   exact
Net Loss = RJBD1 + Pledge To OT
           34,207 + 30,166                            = 64,373   exact
40+      = age group in (40-44, 45-49, Above 50)
           32,718 + 16,085 + 17,792                   = 66,595   exact
```

`Net Loss = RJBD1 + Pledge To OT` is worth stating plainly: a "loss" is a donor
who never reached the first debit **or** one who downgraded from a regular
pledge to a single gift. All three derivations are encoded in
`qmis/config/source_map.yaml` and asserted in `tests/test_donation_level.py`.

## 3. The weekly grain already exists: `WE Date`

138 distinct values, always a Monday, 0–6 days after `SigninDT`. This is the
week bucket the business already reports on, so the system uses it rather than
deriving its own week from the sign-in date — which keeps its weeks identical
to theirs.

## 4. The hierarchy is deeper than the Master Report showed

```
ORG (6)  ->  ORG 2 (19)  ->  OWNER NAME (56)  ->  BAName (3,899)
```

with `REGION` (3) and `CITY` (24) as Owner attributes, and `Designation`
(Sr. Leader / Leader / New Guy) as a BA attribute.

Note **3,899 BAs and 56 Owners, not the ~650 and ~44 in the brief** — those are
cumulative over 2.7 years, whereas ~650 is the active-in-a-week figure. Median
52.5 BAs per Owner, max 312. 124 BAs appear under more than one Owner across
history, which is ordinary movement between teams over that span.

## 5. Case-split entities — a real correctness bug in the source

The same entity is spelled several ways, and left alone each spelling becomes a
separate entity with its own diluted rates:

| Column | Spellings | Real values | Examples |
|---|---|---|---|
| `REGION` | 6 | **3** | `SOUTH` (38,107) vs `South` (174,513) |
| `ORG 2` | 19 | **13** | `TRIFORCE`/`Triforce`, `WELKINZ`/`Welkinz`, `ALZA`/`Alza` |
| `SourceofDonation` | 17 | **9** | `PAid_Permission`, `Paid_PermissIon`, `Paid_permission` |
| `GENDER` | 9 | **5** | `MALE`, `M`, `Male ` |

Resolved by canonicalising each column on **frequency** rather than by guessing
from the shape of the string: `Alza` (14,009) beats `ALZA` (3,576), while `KOP`
and `PVR` have no rival spelling and are left alone. An earlier heuristic that
preserved short all-capitals tokens as acronyms kept `ALZA` apart from `Alza` —
exactly the split it was meant to fix.

Residual: variants differing by punctuation (`Paid-Permission` vs
`Paid_Permission`) are still distinct. Worth a source-side fix.

## 6. The debit ladder is not a survival curve

~8% of rows have a donor paying a later debit after missing an earlier one
(`Debit4=1` while `DEBIT3=0`) — a failed collection can be retried or resumed.
Strict monotonicity would fire on every week, so the check now tolerates 15% and
reports only an excess above that.

## 7. Data quality worth raising with the business

- **`Donamt` max is 800,801** against a median of 800 — a data-entry error
  inflating any total that includes it.
- **`AGE` ranges 0 to 125**; 129 rows have `NO DOB`.
- `BILLINGFAILEDDT`, `Debit DT 3` and `DOB` are stored as raw Excel serial
  numbers rather than dates.
- `Overall Reject` (69,826) is not the sum of the named reject reasons; the
  taxonomy overlaps and would need the business to define precedence.

## 8. Open questions now answered

| Part 1 question | Answer |
|---|---|
| §9.1 Can the export carry Owner and BA? | **Yes** — both are on every row, plus ORG and ORG 2 |
| §9.5 Cohort or point-in-time snapshot? | **Point-in-time.** Every debit stage is a flag on the donation row, updated as collections happen. A weekly maturity lag is therefore not needed; the monthly lag stays. |
| §9.6 What volume should a BA be judged at? | Unchanged — ~2,300 submissions a week across ~650 active BAs |

Still open: **§9.2** (is a higher or lower `OT%`, `Below 30%`, `40+%` good?),
**§9.3** (the real Debit 3 target), **§9.4** (the RJBD1 target).
