# Architecture

## The pipeline

```
weekly .xlsx in a watched folder
        │
        ▼
  StorageBackend          local folder today; Drive/OneDrive/SharePoint later
        │                 (one class to swap — nothing downstream changes)
        ▼
  readers.py              detect the header row, map columns via metrics.yaml
        │                 aliases, drop pivot subtotals, resolve the period
        ▼
  validation.py           11 rules. Any error → the file is REFUSED, recorded,
        │                 and the reason named. Nothing partial is ever stored.
        ▼
  pipeline.py             upsert entities, supersede the previous generation,
        │                 insert facts (long format, percentages as 0–100)
        ▼
  rollup.py               BA → Team → Owner → Org. Rates are RECOMPUTED from
        │                 summed components, never averaged.
        ▼
  rolling.py              4-week window for small-sample levels
        │
        ├─► matrix.py     one dense (entity × metric) × period-offset array
        │        │
        │        ├─► comparison.py   previous, trailing avg, best/worst, streaks
        │        └─► anomaly.py      median/MAD z-score per metric
        ▼
  significance.py         two-proportion z-tests: is this move real?
        │
        ▼
  severity.py             the decision table → GREEN/YELLOW/ORANGE/RED + prose
        │
        ▼
  grouping.py             collapse correlated cascades (the debit ladder)
        │
        ▼
  scoring.py              weighted 0–100 score, renormalised over what exists
        │
        ├─► database       alerts, scores, derived facts (all versioned)
        ├─► notify/        routed, de-duplicated digests → email/Slack/Teams
        └─► Streamlit      executive, alerts, owners, BAs, trends, admin
```

## Why this stack

The requirement is ~650 BAs × ~45 metrics × weekly — about 30k facts a week,
1.5M rows a year. That is a small-data problem with a hard *explainability*
requirement, dressed as a BI problem.

| Considered | Verdict |
|---|---|
| **Power BI Embedded** | Excellent charts, but the alert logic, anomaly detection and the sentence explaining *why* something is red would live in DAX — hard to unit-test, hard to review, and licensed per user. Rejected. |
| **React + FastAPI + Postgres** | Maximum flexibility, three times the build and maintenance surface, and a front-end nobody in the organisation can change. Rejected for v1. |
| **Streamlit + pandas + SQLAlchemy** | One language end to end. The analytics core is pure Python and directly testable — which is what makes the 93-test suite possible. A business analyst can read `metrics.yaml` and change behaviour. **Chosen.** |
| **SQLite → PostgreSQL** | 1.5M rows a year is nothing for SQLite, and it needs no server. Every query goes through SQLAlchemy with portable types, so moving to PostgreSQL or Supabase is a URL change plus `init_db()`. **SQLite now, Postgres when concurrency or managed backups justify it.** |

Deliberately *not* used: any machine-learned anomaly model. A median/MAD
z-score is explainable to a manager in one sentence, and the brief's own
requirement is that anomaly detection stay explainable.

## Data model

Six tables carry the system; the shape matters more than the count.

- **`entities`** — self-referencing (`parent_id`). Org → Owner → BA today; a
  Team level slots in without a migration.
- **`facts`** — long format: `(entity, period, metric, value, denominator)`.
  Adding a metric never adds a column, which is what makes a 42-metric registry
  a config file rather than a schema change. `is_current` implements
  non-destructive correction; `is_derived` marks rolled-up Owner/Org rows.
- **`uploads` + `validation_issues`** — every file that was ever offered,
  accepted or refused, with its findings. The audit trail is queryable, not
  printed to a log.
- **`alert_runs` + `alerts`** — a run supersedes the previous run for its
  period rather than deleting it. Each alert stores its numbers, its statistical
  footing, and the prose explaining it.
- **`scores`** — the weighted score with its coverage, so "82" and "82 based on
  half the metrics" are distinguishable.
- **`users`, `settings`, `notification_log`**.

Nothing is ever overwritten. Reprocessing week 33 inserts a new generation and
demotes the old one, so a correction is auditable and reversible.

## The three ideas that do the real work

**1. Configuration, not code.** `qmis/config/metrics.yaml` holds direction,
thresholds, weights, change sensitivity, anomaly settings, sample floors,
maturity lags and per-level overrides for all 42 metrics. `severity.py` has
never heard of RJBD1.

**2. Refuse rather than mislead.** A file whose columns changed shape is
rejected with the column named. A rate that contradicts its own
numerator/denominator raises a warning. A metric whose business direction has
not been confirmed is charted but never alerted and carries zero score weight.

**3. Statistics before colour.** Three guards stand between a number moving and
a manager being paged: sample floors, a two-proportion z-test on both the
movement and the threshold breach, and correlated-cascade grouping. Each guard
explains itself in the alert text — including saying how much more data would be
needed to call it.

## Swapping the storage platform

`qmis/ingest/storage.py` defines `StorageBackend` with four methods:
`list_files`, `fetch`, `archive`, `content_hash`. `LocalFolderStorage` is the
implementation in use, and pointing it at a synced Google Drive or OneDrive
folder gives the full workflow with no OAuth to maintain. `CloudDriveStorage` is
a documented stub for a native API integration when the organisation has decided
which platform it is standardising on.

## Scheduling

`qmis watch` performs one full cycle: detect → validate → load → evaluate →
notify. Run it from whatever scheduler already exists:

```bash
*/15 * * * * cd /srv/qmis && .venv/bin/qmis watch >> /var/log/qmis.log 2>&1
```

Polling, not filesystem events, on purpose: a weekly file does not need
sub-second detection, and inotify does not fire reliably across network shares
or cloud-sync folders — which is exactly where this file will live.

## Performance

One full evaluation of 12 weeks × 650 BAs × 42 metrics (≈216k history rows,
18k judgements) takes **≈9 seconds**. The first working version took 44 s; the
difference is `matrix.py` — pivoting the history into one dense numpy array
once and sharing it, instead of grouping 27,000 times in Python.

## Security

Authentication is delegated to the organisation's identity provider, which
passes the verified email in a request header. Building a password store into a
reporting tool would add a credential-handling liability for no gain.

Authorisation is enforced in the query: `visible_entity_ids()` walks the entity
tree and every read is scoped through it. Hiding a widget is not access control.
