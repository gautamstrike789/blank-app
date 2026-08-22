"""Command line interface.

    qmis init-db                     create the schema
    qmis generate-sample --weeks 12  write demo weekly workbooks
    qmis ingest FILE...              load one or more workbooks
    qmis load-facts FILE.csv.gz      load a pre-aggregated fact export
    qmis watch                       process everything in the inbox
    qmis evaluate [--period KEY]     (re)run the alert engine
    qmis notify --period KEY         build and send digests
    qmis report [--period KEY]       print the executive summary
    qmis metrics                     show the configured metric rules
    qmis users add|list              manage access

The dashboard is a view over exactly these operations; anything the UI can do
is scriptable, which is what makes the weekly run automatable.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from qmis.core.config import load_settings
from qmis.core.db import init_db, session_scope
from qmis.core.metric_config import get_registry
from qmis.core.models import ROLES


def _log(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )


def cmd_init_db(args) -> int:
    init_db(drop=args.drop)
    print(f"schema ready ({'recreated' if args.drop else 'ensured'})")
    if args.admin:
        from qmis.auth.rbac import ensure_seed_admin

        with session_scope() as session:
            user = ensure_seed_admin(session, args.admin)
            print(f"admin user: {user.email}")
    return 0


def cmd_generate_sample(args) -> int:
    try:
        from sample_data.generate import write_weekly_files
    except ModuleNotFoundError:
        print(
            "The sample generator ships with the source tree, not the installed "
            "package. Run this command from the project directory."
        )
        return 1

    written = write_weekly_files(
        Path(args.outdir),
        weeks=args.weeks,
        one_file_per_week=not args.single_file,
        owners=args.owners,
        bas=args.bas,
        end_period=args.end_period,
        seed=args.seed,
    )
    print(f"wrote {len(written)} file(s) to {args.outdir}")
    return 0


def cmd_ingest(args) -> int:
    from qmis.ingest.pipeline import ingest_file

    init_db()
    registry = get_registry()
    failures = 0
    with session_scope() as session:
        for path in args.files:
            result = ingest_file(
                session,
                path,
                registry=registry,
                uploaded_by=args.user,
                allow_reprocess=args.reprocess,
                sheet_name=args.sheet,
            )
            print(result)
            for finding in result.report.findings:
                if finding.severity != "info" or args.verbose:
                    print(f"    {finding}")
            failures += 0 if result.accepted else 1
    if args.evaluate and failures < len(args.files):
        return cmd_evaluate(args)
    return 1 if failures else 0


def cmd_load_facts(args) -> int:
    from qmis.ingest.pipeline import ingest_fact_export

    init_db()
    with session_scope() as session:
        for path in args.files:
            result = ingest_fact_export(
                session, path, uploaded_by=args.user, allow_reprocess=args.reprocess
            )
            print(result)
            for finding in result.report.findings:
                if finding.severity != "info" or args.verbose:
                    print(f"    {finding}")
    return 0


def cmd_watch(args) -> int:
    from qmis.ingest.watcher import run_cycle

    init_db()
    settings = load_settings()
    with session_scope() as session:
        result = run_cycle(
            session,
            settings=settings,
            notify=args.notify,
            dry_run_notifications=args.dry_run,
        )
    print(result.summary())
    for rejected in result.rejected:
        print(f"  REJECTED {rejected.filename}: {rejected.message}")
    for error in result.errors:
        print(f"  ERROR {error}")
    return 1 if result.errors else 0


def cmd_evaluate(args) -> int:
    from qmis.analytics.engine import evaluate_period
    from qmis.analytics.repository import available_periods

    init_db()
    settings = load_settings()
    with session_scope() as session:
        periods = [args.period] if getattr(args, "period", None) else available_periods(session)
        if getattr(args, "all", False):
            targets = periods
        else:
            targets = periods[-1:] if periods else []
        if not targets:
            print("no data loaded")
            return 1
        for key in targets:
            result = evaluate_period(
                session,
                key,
                trailing_window=int(settings.get("evaluation.trailing_window", 4)),
                rolling_window=int(settings.get("evaluation.rolling_window", 4)),
                rolling_levels=tuple(settings.get("evaluation.rolling_levels", ["ba"])),
                confidence=float(settings.get("evaluation.confidence", 0.90)),
            )
            print(result.summary())
    return 0


def cmd_notify(args) -> int:
    from qmis.analytics.repository import latest_period
    from qmis.ingest.watcher import _notify

    init_db()
    settings = load_settings()
    with session_scope() as session:
        period_key = args.period or latest_period(session)
        if not period_key:
            print("no data loaded")
            return 1
        sent = _notify(session, settings, get_registry(), period_key, None, args.dry_run)
    print(f"{sent} digest(s) {'previewed' if args.dry_run else 'sent'} for {period_key}")
    return 0


def cmd_report(args) -> int:
    from qmis.analytics.repository import latest_period, load_alerts, load_scores

    init_db()
    with session_scope() as session:
        period_key = args.period or latest_period(session)
        if not period_key:
            print("no data loaded")
            return 1
        alerts = load_alerts(session, period_key=period_key, include_improvements=False)
        scores = load_scores(session, period_keys=[period_key])

    print(f"\n=== Quality report - {period_key} ===\n")
    org = scores.loc[scores["entity_type"] == "org"] if not scores.empty else scores
    if not org.empty:
        row = org.iloc[0]
        delta = "" if row["delta"] is None else f" ({row['delta']:+.1f} vs previous)"
        print(f"Overall quality score: {row['score']:.1f} / 100 - {row['band']}{delta}")
    counts = alerts["severity"].value_counts().to_dict() if not alerts.empty else {}
    print(
        f"Alerts: {counts.get('RED', 0)} critical, {counts.get('ORANGE', 0)} high, "
        f"{counts.get('YELLOW', 0)} early warning\n"
    )
    if not alerts.empty:
        print("Top findings:")
        for i, row in enumerate(alerts.head(args.limit).itertuples(index=False), 1):
            print(f" {i:2d}. {row.headline}")
            if args.verbose:
                print(f"     {row.explanation}\n")
    owners = scores.loc[scores["entity_type"] == "owner"] if not scores.empty else scores
    if not owners.empty:
        print("\nOwners needing attention:")
        for row in owners.sort_values("score").head(5).itertuples(index=False):
            print(
                f"  {row.entity_name:<24} score {row.score:5.1f} ({row.band}) "
                f"- {row.red_count} red, {row.orange_count} orange"
            )
    return 0


def cmd_metrics(args) -> int:
    registry = get_registry()
    print(f"{len(registry)} metrics configured, total score weight {registry.total_weight():.0f}\n")
    print(f"{'key':<22}{'direction':<18}{'warn':>9}{'crit':>9}{'weight':>8}  name")
    for metric in sorted(registry, key=lambda m: (m.group, m.key)):
        if args.group and metric.group != args.group:
            continue
        warn = "-" if metric.warning_threshold is None else f"{metric.warning_threshold:g}"
        crit = "-" if metric.critical_threshold is None else f"{metric.critical_threshold:g}"
        print(
            f"{metric.key:<22}{metric.direction:<18}{warn:>9}{crit:>9}"
            f"{metric.weight:>8g}  {metric.name}"
        )
    review = registry.needing_review()
    if review:
        print(f"\n{len(review)} metric(s) await a confirmed business direction:")
        for metric in review:
            print(f"  {metric.key:<22} {metric.name}")
    return 0


def cmd_users(args) -> int:
    from sqlalchemy import select

    from qmis.core.models import User

    init_db()
    with session_scope() as session:
        if args.action == "add":
            user = User(
                email=args.email.strip().lower(),
                name=args.name or args.email,
                role=args.role,
                active=True,
            )
            session.add(user)
            session.flush()
            print(f"added {user.email} as {user.role}")
        elif args.action == "link":
            from qmis.auth.rbac import link_owner_users

            print(f"linked {link_owner_users(session)} owner account(s) to their entity")
        else:
            rows = session.execute(select(User).order_by(User.role, User.email)).scalars().all()
            if not rows:
                print("no users configured")
            for user in rows:
                state = "" if user.active else " (inactive)"
                print(f"  {user.role:<12} {user.email:<34} {user.name}{state}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="qmis", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init-db", help="create or recreate the schema")
    p.add_argument("--drop", action="store_true", help="drop existing tables first")
    p.add_argument("--admin", help="email of the first admin user")
    p.set_defaults(func=cmd_init_db)

    p = sub.add_parser("generate-sample", help="write demo weekly workbooks")
    p.add_argument("--outdir", default="data/inbox")
    p.add_argument("--weeks", type=int, default=12)
    p.add_argument("--owners", type=int, default=44)
    p.add_argument("--bas", type=int, default=650)
    p.add_argument("--end-period", default="2026-W33")
    p.add_argument("--seed", type=int, default=20260814)
    p.add_argument("--single-file", action="store_true")
    p.set_defaults(func=cmd_generate_sample)

    p = sub.add_parser("ingest", help="load one or more workbooks")
    p.add_argument("files", nargs="+")
    p.add_argument("--reprocess", action="store_true", help="supersede an already-loaded period")
    p.add_argument("--sheet", help="force a specific sheet name")
    p.add_argument("--user", default="cli")
    p.add_argument("--evaluate", action="store_true", help="run the alert engine afterwards")
    p.add_argument("--period")
    p.add_argument("--all", action="store_true")
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser(
        "load-facts",
        help="load a pre-aggregated fact export (qmis_facts.csv.gz)",
    )
    p.add_argument("files", nargs="+")
    p.add_argument("--user", default="cli")
    p.add_argument("--reprocess", action="store_true")
    p.set_defaults(func=cmd_load_facts)

    p = sub.add_parser("watch", help="process everything in the inbox folder")
    p.add_argument("--notify", action="store_true", help="force notifications on")
    p.add_argument("--dry-run", action="store_true", help="build digests but do not send")
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("evaluate", help="run the alert engine")
    p.add_argument("--period", help="period key, e.g. 2026-W33")
    p.add_argument("--all", action="store_true", help="every loaded period")
    p.set_defaults(func=cmd_evaluate)

    p = sub.add_parser("notify", help="build and send digests")
    p.add_argument("--period")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_notify)

    p = sub.add_parser("report", help="print the executive summary")
    p.add_argument("--period")
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("metrics", help="show configured metric rules")
    p.add_argument("--group")
    p.set_defaults(func=cmd_metrics)

    p = sub.add_parser("users", help="manage access")
    p.add_argument("action", choices=["list", "add", "link"])
    p.add_argument("--email")
    p.add_argument("--name")
    p.add_argument("--role", choices=list(ROLES), default="management")
    p.set_defaults(func=cmd_users)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _log(args.verbose)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
