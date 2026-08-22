# QMIS — Quality Metrics Intelligence & Alert System

A weekly Excel report goes into a folder. The system validates it, stores it
alongside every previous week, works out what changed, decides what is worth
someone's attention, and explains itself in sentences rather than colours.

Built for an organisation of ~44 Owners and ~650 Business Associates collecting
donations in the field, monitoring ~42 quality metrics.

---

## What it does

**Reads the real export.** The weekly file is one row per submission — 308,922
rows across 96 columns — not a pre-aggregated summary. The system folds it up to
(entity x week x metric) itself, deriving every measure from the source flags.
Three measures aren't columns at all and were solved against the Master Report's
own totals: `RJBD1 = Insuff b4 debit + Stop b4 debit + Tech Error + Other
Errors`, `Net Loss = RJBD1 + Pledge To OT`, `40+ = age group in (40-44, 45-49,
Above 50)`. All three reproduce it exactly.

**Detects.** Watches a folder (or a synced Drive/OneDrive/SharePoint folder),
picks up new weekly workbooks, and processes them on a schedule. A
pre-aggregated summary and a donation-level export are both handled; the reader
works out which it has.

**Refuses bad data.** Eleven validation rules run before anything is stored. A
changed column, a duplicated BA, a rate that contradicts its own components, a
percentage scale that flipped — the load is rejected and the reason named. A
dashboard built on a mis-parsed spreadsheet is worse than no dashboard, because
people act on it.

**Never overwrites.** Reprocessing a week inserts a new generation and demotes
the old one. Corrections are auditable and reversible.

**Explains.** Every alert reads like this:

> 🔴 **CRITICAL — Rahul's Debit 1 % deterioration: 85.00% → 76.00%**
> Rahul's Debit 1 % fell from 85.00% to 76.00% (-9.00 pp, -10.6% relative).
> Since higher Debit 1 % is better, this is a negative quality movement. It is
> now past the critical threshold of 80.00%. Recent average: 84.50%. Based on
> 420 submissions. This movement is larger than sampling noise (z = -3.3 at 90%
> confidence).

**Catches what thresholds miss.** RJBD1 sitting at 2.0–2.5% for two months and
printing 4.0% is flagged as an anomaly even though it never approaches its 14%
critical line.

**Refuses to cry wolf.** Three guards — sample floors, two-proportion z-tests,
and correlated-cascade grouping — cut critical alerts from 174 (almost all
noise) to 16 on the same data, every one of them statistically confirmed. Where
a movement genuinely cannot be called at a BA's volume, the system says so and
says how much data it would take, rather than guessing.

**Reports improvements too.** Sustained gains are alerts in their own right:
*"Suresh Pillai's Debit 3 % has improved for 11 periods running, +17.42 pp in
total, now 80.58%."*

---

## Quick start

```bash
uv sync                                    # or: pip install -e ".[dev]"

qmis init-db --admin you@example.com       # create the schema and first admin
qmis generate-sample --weeks 12            # realistic demo data in data/inbox
qmis watch                                 # detect → validate → load → evaluate
qmis report                                # the executive summary, in the terminal

uv run streamlit run streamlit_app.py      # the dashboard
```

Loading a real report instead:

```bash
qmis ingest "Master_Report__140826.xlsx" --evaluate
```

### Commands

| Command | Does |
|---|---|
| `qmis init-db [--drop] [--admin EMAIL]` | Create the schema |
| `qmis ingest FILE... [--reprocess] [--evaluate]` | Load one or more workbooks |
| `qmis watch [--notify] [--dry-run]` | Process everything in the inbox |
| `qmis evaluate [--period 2026-W33] [--all]` | Re-run the alert engine |
| `qmis notify [--period KEY] [--dry-run]` | Build and send digests |
| `qmis report [--period KEY] [-v]` | Executive summary |
| `qmis metrics [--group Quality]` | Show the configured metric rules |
| `qmis users list\|add\|link` | Manage access |

---

## The dashboard

| Page | Answers |
|---|---|
| **Executive** | What changed, who changed, what needs attention now. Score, alert counts, priority list, weekly change matrix, severity heatmap. |
| **Alert centre** | Filter by severity, level, owner, metric, trigger, "new this week", "statistically confirmed only". Read the reasoning, acknowledge with a note. |
| **Owners** | Ranked by quality score and alert weight. Per-Owner profile with their critical BAs. |
| **Business associates** | Saved questions ("critical Debit 1 decline", "improving consistently for 4+ periods"), individual profiles, metric history with submission volume behind it. |
| **Trends** | Any level, any metric, compared across entities, against threshold and warning bands. |
| **Data & uploads** | Upload with a full validation report, watched-folder status, run-now, upload history. |
| **Administration** | Edit thresholds and weights, manage users, notification settings, system state. |

Access is role-based — `admin`, `management`, `owner` — and an Owner's scope is
enforced in the query, not by hiding controls.

The hierarchy read from the source is **ORG (6) → ORG 2 (19) → Owner (56) → BA
(3,899)**, with Region and City as Owner attributes. Values that differ only by
case are merged onto the more frequent spelling, so `SOUTH`/`South` and
`ALZA`/`Alza` do not become separate entities with diluted rates.

---

## Configuring metrics

Everything the alert engine does comes from `qmis/config/metrics.yaml`. Adding a
metric or changing behaviour is a config edit:

```yaml
- key: rjbd1_pct
  name: RJBD1 %
  aliases: [Rejects Before Debit 1%, RJBD1]
  direction: lower_is_better      # an increase is a NEGATIVE movement
  target: 9.5
  warning_threshold: 12.0
  critical_threshold: 14.0
  weight: 25                      # share of the 0-100 quality score
  include_in_score: true
  numerator: rjbd1_count          # lets the rate be recomputed on rollup
  denominator: submissions        # and gives every alert its sample size
  percentage_change_threshold: 10.0
  absolute_change_threshold: 1.5
  level_overrides:
    ba: {min_denominator: 15}     # BAs are judged on a different sample floor
  anomaly:
    method: robust                # median/MAD - one past spike cannot hide the next
    lookback: 8
    z_threshold: 2.5
```

Supported directions: `higher_is_better`, `lower_is_better`, `target_range`,
`neutral`. A metric marked `needs_review: true` is tracked and charted but never
alerts and carries no score weight — three metrics are in that state because
their business direction has not been confirmed (see
[docs/DATA_FINDINGS.md](docs/DATA_FINDINGS.md) §9).

---

## How severity is decided

| | |
|---|---|
| 🔴 **RED** | Past the critical threshold; or past warning *and* deteriorating severely; or a severe deterioration that is also a statistical anomaly; or a sustained severe decline below warning. |
| 🟠 **ORANGE** | A severe deterioration; or past warning and still falling; or a significant sustained decline; or an adverse anomaly with a real deterioration. |
| 🟡 **YELLOW** | Past warning but stable; or a significant single-period deterioration; or an adverse anomaly on its own. |
| 🟢 **GREEN** | Everything else — including improvements, which are reported separately. |

Before any of that, three things stop noise reaching a manager:

1. **Sample floors.** A rate on fewer than `min_denominator` submissions is
   recorded and charted, never escalated.
2. **Significance.** Rate movements and threshold breaches are put through a
   two-proportion z-test. An unconfirmed breach is reported one level down with
   *"not yet statistically confirmed on this sample size"*, and the alert says
   how much more data would settle it.
3. **Cascade grouping.** The debit ladder is not twelve independent metrics —
   nine correlated alerts become one composite finding.

---

## Volumes, and why BAs are judged on a rolling window

The organisation signs ~2,300 submissions a week across ~650 BAs: about **3.6
each**. A Debit 1 rate on four submissions can only be 0%, 25%, 50%, 75% or
100%.

So BA, team and Owner levels are evaluated on a **4-week rolling window** —
counts summed, rates recomputed from the sums, never averaged. Sharp single-week
events are not lost: every period is judged on both the single-week and the
rolling basis, and the more serious verdict wins. Each alert states which view
produced it.

This is the most consequential finding in the data.
[docs/DATA_FINDINGS.md](docs/DATA_FINDINGS.md) §7 has the numbers.

---

## Notifications

Off by default. Configure channels in `qmis/config/settings.yaml`:

```yaml
notifications:
  enabled: true
  send_severities: [RED, ORANGE]      # nothing quieter is ever sent
  repeat_after_periods: 3             # do not re-send unless it got worse
  channels:
    - kind: email
      host: smtp.example.com
      password_env: QMIS_SMTP_PASSWORD
    - kind: webhook                   # Slack or Microsoft Teams
      url_env: QMIS_SLACK_WEBHOOK
      format: slack
```

Owners are sent their own team; management is sent the organisation. Nobody is
told about the same finding twice running unless it deteriorated.

---

## Deployment

```bash
export QMIS_DATABASE_URL="postgresql+psycopg://user:pass@host/qmis"   # optional
export QMIS_INBOX="/mnt/shared-drive/quality-reports"
qmis init-db

# schedule the weekly run
*/15 * * * * cd /srv/qmis && .venv/bin/qmis watch >> /var/log/qmis.log 2>&1
```

Put the dashboard behind your identity provider (Azure AD, Google Workspace,
Cloudflare Access, an nginx auth proxy) and have it pass the verified email in
`X-Forwarded-Email`. For a local trial, set `auth.mode: demo` in
`settings.yaml` — the app says loudly when that is on.

---

## Tests

```bash
pytest                                                    # 122 tests
QMIS_MASTER_REPORT=/path/to/Master_Report.xlsx pytest     # +7 real-file tests
```

Covering: ISO week arithmetic across year boundaries, threshold and
normalisation semantics, alias collisions, every validation refusal, duplicate
and reprocessed uploads, new and departed BAs, changed columns, flipped
percentage scales, anomaly detection with and without threshold breaches,
significance gating, rate rollup that never averages rates, rolling windows,
RBAC scoping, notification routing, and the whole pipeline end to end.

---

## Documentation

- **[docs/DATA_FINDINGS.md](docs/DATA_FINDINGS.md)** — what the Master Report
  actually contains, verified arithmetic, observed distributions, assumptions
  made, and the open questions for the business.
- **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** — the pipeline, the stack
  decision and why, the data model, and how to swap the storage platform.
- **[docs/IMPLEMENTATION_LOG.md](docs/IMPLEMENTATION_LOG.md)** — what was built
  in each phase, what broke, and what was changed as a result.

## Project layout

```
qmis/
  config/     metrics.yaml (42 metrics), source_map.yaml (donation-level
              derivations), settings.yaml, column_map.yaml
  core/       periods, metric registry, ORM models, database, settings
  ingest/     storage backends, readers, aggregation (donation-level),
              validation, pipeline, watcher
  analytics/  matrix, comparison, anomaly, significance, severity,
              grouping, rolling, rollup, scoring, engine, repository
  notify/     routing and channels (console, email, Slack/Teams)
  auth/       role-based access control
  app/        Streamlit pages
sample_data/  realistic weekly workbook generator
tests/        122 tests + 7 against the real workbook
```
