"""Data & uploads - the weekly file, its validation report, and the audit trail."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pandas as pd
import streamlit as st
from sqlalchemy import select

from qmis.app.common import bootstrap, cached_periods, clear_caches, db, empty_state, require
from qmis.auth.rbac import UPLOAD_FILES
from qmis.core.config import load_settings
from qmis.core.metric_config import get_registry
from qmis.core.models import (
    ISSUE_ERROR,
    ISSUE_INFO,
    ISSUE_WARNING,
    UPLOAD_LOADED,
    Upload,
    ValidationIssue,
)
from qmis.ingest.pipeline import ingest_file
from qmis.ingest.storage import build_storage

SEVERITY_ICON = {ISSUE_ERROR: "🛑", ISSUE_WARNING: "⚠️", ISSUE_INFO: "ℹ️"}


def render(principal) -> None:
    st.title("Data & uploads")
    tab_upload, tab_folder, tab_history = st.tabs(
        ["Upload a report", "Watched folder", "Upload history"]
    )
    with tab_upload:
        _upload(principal)
    with tab_folder:
        _folder(principal)
    with tab_history:
        _history()


def _upload(principal) -> None:
    if not require(principal, UPLOAD_FILES):
        return
    st.caption(
        "The file is validated before anything is stored. If the export has changed shape, "
        "the load is refused and the reason named - a dashboard built on a mis-parsed "
        "spreadsheet is worse than no dashboard."
    )
    uploaded = st.file_uploader("Weekly quality report", type=["xlsx", "xlsm", "xls"])
    columns = st.columns(3)
    reprocess = columns[0].checkbox(
        "Reprocess", help="Supersede an already-loaded period. The previous version is kept."
    )
    sheet = columns[1].text_input("Sheet name (optional)")
    run_evaluation = columns[2].checkbox("Run the alert engine afterwards", value=True)

    if uploaded is None or not st.button("Validate and load", type="primary"):
        return

    with tempfile.TemporaryDirectory(prefix="qmis-upload-") as tmp:
        path = Path(tmp) / uploaded.name
        path.write_bytes(uploaded.getbuffer())
        with st.spinner("Validating..."):
            with db() as session:
                result = ingest_file(
                    session,
                    path,
                    registry=get_registry(),
                    uploaded_by=principal.email or principal.name,
                    allow_reprocess=reprocess,
                    sheet_name=sheet or None,
                )
        _render_report(result)
        if result.accepted and run_evaluation:
            from qmis.analytics.engine import evaluate_period

            settings = load_settings()
            with st.spinner("Evaluating..."):
                with db() as session:
                    for period_key in result.periods:
                        evaluation = evaluate_period(
                            session,
                            period_key,
                            trailing_window=int(settings.get("evaluation.trailing_window", 4)),
                            rolling_window=int(settings.get("evaluation.rolling_window", 4)),
                            rolling_levels=tuple(settings.get("evaluation.rolling_levels", ["ba"])),
                            confidence=float(settings.get("evaluation.confidence", 0.90)),
                            upload_id=result.upload_id,
                        )
                        st.success(evaluation.summary())
        clear_caches()


def _render_report(result) -> None:
    if result.accepted:
        st.success(result.message)
    else:
        st.error(result.message)
    columns = st.columns(4)
    columns[0].metric("Sheet", result.sheet_name or "-")
    columns[1].metric("Level", result.entity_level)
    columns[2].metric("Periods", len(result.periods))
    columns[3].metric("Values loaded", f"{result.facts_loaded:,}")

    findings = result.report.findings
    if not findings:
        st.caption("No validation findings.")
        return
    st.markdown("**Validation report**")
    for finding in findings:
        icon = SEVERITY_ICON.get(finding.severity, "")
        with st.container(border=True):
            st.markdown(f"{icon} **{finding.code}** - {finding.message}")
            if finding.column:
                st.caption(f"Column: `{finding.column}`")
            if finding.sample:
                st.caption(f"Sample: `{finding.sample}`")


def _folder(principal) -> None:
    settings = load_settings()
    storage_config = settings.section("storage")
    st.caption(
        "The system watches this location. Put the week's file here - a synced Google Drive, "
        "OneDrive or SharePoint folder works without any API setup - and the pipeline runs on "
        "its own schedule. The backend is swappable: see `qmis/ingest/storage.py`."
    )
    st.code(
        f"backend : {storage_config.get('backend')}\n"
        f"inbox   : {storage_config.get('inbox')}\n"
        f"archive : {storage_config.get('archive')}",
        language="text",
    )
    try:
        backend = build_storage(storage_config)
        waiting = backend.list_files()
    except Exception as exc:
        st.error(f"Cannot read the watched folder: {exc}")
        return

    if waiting:
        st.write(f"**{len(waiting)} file(s) waiting:**")
        st.dataframe(
            pd.DataFrame(
                [{"File": f.name, "Size (KB)": f.size // 1024, "Modified": f.modified_at} for f in waiting]
            ),
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.info("Nothing waiting in the inbox.")

    if not principal.can(UPLOAD_FILES):
        return
    st.markdown("**Run now**")
    dry_run = st.checkbox("Preview notifications instead of sending", value=True)
    if st.button("Process the inbox"):
        from qmis.ingest.watcher import run_cycle

        with st.spinner("Processing..."):
            with db() as session:
                cycle = run_cycle(session, settings=settings, dry_run_notifications=dry_run)
        st.success(cycle.summary())
        for rejected in cycle.rejected:
            st.error(f"{rejected.filename}: {rejected.message}")
        for error in cycle.errors:
            st.error(error)
        clear_caches()

    st.divider()
    st.markdown("**Scheduling**")
    st.caption("Run this on whatever scheduler the organisation already uses:")
    st.code(
        "# cron - every 15 minutes\n"
        "*/15 * * * * cd /srv/qmis && .venv/bin/qmis watch >> /var/log/qmis.log 2>&1\n\n"
        "# or a systemd timer, or a Windows scheduled task calling the same command",
        language="bash",
    )


def _history() -> None:
    with db() as session:
        uploads = session.execute(
            select(Upload).order_by(Upload.uploaded_at.desc()).limit(60)
        ).scalars().all()
        rows = []
        issues_by_upload: dict[int, list[ValidationIssue]] = {}
        for upload in uploads:
            rows.append(
                {
                    "When": upload.uploaded_at,
                    "File": upload.filename,
                    "Status": upload.status,
                    "Sheet": upload.sheet_name,
                    "Level": upload.profile,
                    "Periods": f"{upload.period_min or '-'} → {upload.period_max or '-'}",
                    "Values": upload.fact_count,
                    "By": upload.uploaded_by,
                    "id": upload.id,
                }
            )
            issues_by_upload[upload.id] = list(
                session.execute(
                    select(ValidationIssue).where(ValidationIssue.upload_id == upload.id)
                ).scalars()
            )
    if not rows:
        empty_state("Nothing has been uploaded yet.")
        return
    frame = pd.DataFrame(rows)
    st.dataframe(frame.drop(columns=["id"]), use_container_width=True, hide_index=True)
    st.caption(
        "Nothing is ever overwritten. Reprocessing a week inserts a new generation of values "
        "and marks the previous one superseded, so a correction is auditable and reversible."
    )
    chosen = st.selectbox(
        "Show the validation report for", frame["id"],
        format_func=lambda i: f"{frame.loc[frame['id'] == i, 'File'].iloc[0]} (#{i})",
    )
    for issue in issues_by_upload.get(chosen, []):
        st.markdown(f"{SEVERITY_ICON.get(issue.severity, '')} **{issue.code}** - {issue.message}")
