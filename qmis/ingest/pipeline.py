"""Ingestion pipeline: file in, validated history out.

    detect -> read -> validate -> clean -> supersede -> load

The pipeline is deliberately all-or-nothing per file.  If validation raises an
error the upload row is still written (with its findings) so the rejection is
visible and explainable in the UI, but no fact reaches the history tables.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

import pandas as pd
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from qmis.core.metric_config import MetricRegistry, get_registry
from qmis.core.models import (
    BA,
    ISSUE_ERROR,
    ORG,
    OWNER,
    TEAM,
    UPLOAD_LOADED,
    UPLOAD_REJECTED,
    UPLOAD_SUPERSEDED,
    Entity,
    Fact,
    Setting,
    Upload,
    ValidationIssue,
)
from qmis.core.periods import Period
from qmis.ingest.readers import (
    ColumnMap,
    ParsedSheet,
    ReaderError,
    infer_period_from_filename,
    pick_best_sheet,
    read_workbook,
)
from qmis.ingest.storage import StorageBackend, sha256_file
from qmis.ingest.validation import (
    Finding,
    ValidationContext,
    ValidationReport,
    validate,
)

ORG_ENTITY_NAME = "Organisation"
COLUMN_SIGNATURE_KEY = "ingest.column_signature"


@dataclass
class IngestResult:
    """Everything the UI, the CLI and the tests need to know about one run."""

    upload_id: int | None
    filename: str
    accepted: bool
    report: ValidationReport
    periods: list[str] = field(default_factory=list)
    grain: str = ""
    sheet_name: str = ""
    entity_level: str = "org"
    facts_loaded: int = 0
    entities_seen: int = 0
    superseded_facts: int = 0
    message: str = ""

    def __str__(self) -> str:  # pragma: no cover - display helper
        verdict = "LOADED" if self.accepted else "REJECTED"
        return f"[{verdict}] {self.filename}: {self.message} ({self.report.summary()})"


def normalise_name(value: str) -> str:
    return " ".join(str(value).strip().split()).casefold()


# --------------------------------------------------------------------------- #
# context
# --------------------------------------------------------------------------- #
def build_context(session: Session, allow_reprocess: bool = False) -> ValidationContext:
    """Snapshot what the database already knows, for the validation rules."""
    known_hashes = {
        row.content_hash: row.filename
        for row in session.execute(
            select(Upload).where(Upload.status == UPLOAD_LOADED)
        ).scalars()
    }
    loaded_periods = {
        key
        for (key,) in session.execute(
            select(Fact.period_key).where(Fact.is_current.is_(True)).distinct()
        )
    }
    owners = {
        normalise_name(name)
        for (name,) in session.execute(
            select(Entity.name).where(Entity.entity_type == OWNER)
        )
    }
    bas = {
        normalise_name(name)
        for (name,) in session.execute(select(Entity.name).where(Entity.entity_type == BA))
    }
    signature = session.get(Setting, COLUMN_SIGNATURE_KEY)
    known_columns = set(signature.value.split(",")) if signature and signature.value else set()

    previous_coverage: dict[str, int] = {}
    latest = session.execute(
        select(Fact.period_key)
        .where(Fact.is_current.is_(True))
        .order_by(Fact.period_key.desc())
        .limit(1)
    ).scalar_one_or_none()
    if latest:
        for metric_key, count in session.execute(
            select(Fact.metric_key, func.count())
            .where(Fact.is_current.is_(True), Fact.period_key == latest)
            .group_by(Fact.metric_key)
        ):
            previous_coverage[metric_key] = int(count)

    return ValidationContext(
        known_columns=known_columns,
        known_owners=owners,
        known_bas=bas,
        loaded_periods=loaded_periods,
        known_hashes=known_hashes,
        previous_coverage=previous_coverage,
        allow_reprocess=allow_reprocess,
    )


# --------------------------------------------------------------------------- #
# entity resolution
# --------------------------------------------------------------------------- #
class EntityResolver:
    """Find-or-create entities, keeping the Org -> Owner -> BA chain intact."""

    def __init__(self, session: Session):
        self.session = session
        self._cache: dict[tuple[str, str], Entity] = {}
        self.created: list[Entity] = []

    def _get(self, entity_type: str, name: str, parent: Entity | None) -> Entity:
        key = (entity_type, normalise_name(name))
        if key in self._cache:
            entity = self._cache[key]
        else:
            entity = self.session.execute(
                select(Entity).where(
                    Entity.entity_type == entity_type,
                    Entity.normalised_name == key[1],
                )
            ).scalar_one_or_none()
            if entity is None:
                entity = Entity(
                    entity_type=entity_type,
                    name=str(name).strip() or ORG_ENTITY_NAME,
                    normalised_name=key[1],
                    parent=parent,
                    active=True,
                )
                self.session.add(entity)
                self.session.flush()
                self.created.append(entity)
            self._cache[key] = entity
        if parent is not None and entity.parent_id != parent.id:
            # A BA moving between Owners is normal; follow the latest report.
            entity.parent_id = parent.id
        return entity

    def org(self) -> Entity:
        return self._get(ORG, ORG_ENTITY_NAME, None)

    def owner(self, name: str) -> Entity:
        return self._get(OWNER, name, self.org())

    def team(self, name: str, owner: Entity | None) -> Entity:
        return self._get(TEAM, name, owner or self.org())

    def ba(self, name: str, parent: Entity | None) -> Entity:
        return self._get(BA, name, parent or self.org())

    def touch(self, entity: Entity, period_key: str) -> None:
        if entity.first_seen_period is None or period_key < entity.first_seen_period:
            entity.first_seen_period = period_key
        if entity.last_seen_period is None or period_key > entity.last_seen_period:
            entity.last_seen_period = period_key


# --------------------------------------------------------------------------- #
# value conversion
# --------------------------------------------------------------------------- #
def to_stored_value(metric, source_value: float | None) -> float | None:
    """Convert a source cell into the canonical stored unit.

    Percentages are stored as 0-100 everywhere in the database regardless of how
    the source file expressed them, so thresholds and alert text never have to
    ask which convention a given file used.
    """
    if source_value is None or pd.isna(source_value):
        return None
    if metric.unit == "percent" and metric.stored_as == "fraction":
        return float(source_value) * 100.0
    return float(source_value)


# --------------------------------------------------------------------------- #
# the pipeline
# --------------------------------------------------------------------------- #
def ingest_file(
    session: Session,
    path: str | Path,
    *,
    registry: MetricRegistry | None = None,
    column_map: ColumnMap | None = None,
    source_uri: str | None = None,
    uploaded_by: str | None = None,
    allow_reprocess: bool = False,
    sheet_name: str | None = None,
    fallback_period: Period | None = None,
    content_hash: str | None = None,
) -> IngestResult:
    """Validate and load one workbook. Never partially loads."""
    registry = registry or get_registry()
    column_map = column_map or ColumnMap.load()
    path = Path(path)
    filename = path.name
    digest = content_hash or sha256_file(path)
    ctx = build_context(session, allow_reprocess=allow_reprocess)

    upload = Upload(
        filename=filename,
        source_uri=source_uri or str(path),
        content_hash=digest,
        grain="",
        status=UPLOAD_REJECTED,
        uploaded_by=uploaded_by,
    )
    session.add(upload)
    session.flush()

    # -- read ------------------------------------------------------------
    try:
        sheets = read_workbook(
            path, registry, column_map, sheet_name=sheet_name, fallback_period=fallback_period
        )
    except ReaderError as exc:
        report = ValidationReport([Finding(ISSUE_ERROR, "unreadable", str(exc))])
        _persist_issues(session, upload, report)
        upload.notes = str(exc)
        session.flush()
        return IngestResult(upload.id, filename, False, report, message=str(exc))

    parsed: ParsedSheet = pick_best_sheet(sheets)
    upload.sheet_name = parsed.layout.sheet_name
    upload.grain = parsed.layout.grain
    upload.row_count = parsed.source_rows
    upload.profile = parsed.layout.entity_level

    # -- validate --------------------------------------------------------
    report = validate(parsed, registry, ctx, content_hash=digest, filename=filename)
    _persist_issues(session, upload, report)

    period_keys = sorted({str(k) for k in parsed.frame["period_key"].dropna().unique()})
    upload.period_min = period_keys[0] if period_keys else None
    upload.period_max = period_keys[-1] if period_keys else None
    upload.period_key = period_keys[-1] if period_keys else None

    if not report.ok:
        upload.status = UPLOAD_REJECTED
        upload.notes = "; ".join(f.message for f in report.errors)[:4000]
        session.flush()
        return IngestResult(
            upload.id,
            filename,
            False,
            report,
            periods=period_keys,
            grain=parsed.layout.grain,
            sheet_name=parsed.layout.sheet_name,
            entity_level=parsed.layout.entity_level,
            message=f"Rejected: {report.errors[0].message}",
        )

    # -- load ------------------------------------------------------------
    loaded, superseded, entity_count = _load_facts(session, upload, parsed, registry)

    upload.status = UPLOAD_LOADED
    upload.fact_count = loaded
    upload.entity_count = entity_count
    _remember_columns(session, parsed)
    _mark_superseded_uploads(session, upload, period_keys)
    session.flush()

    return IngestResult(
        upload.id,
        filename,
        True,
        report,
        periods=period_keys,
        grain=parsed.layout.grain,
        sheet_name=parsed.layout.sheet_name,
        entity_level=parsed.layout.entity_level,
        facts_loaded=loaded,
        entities_seen=entity_count,
        superseded_facts=superseded,
        message=(
            f"Loaded {loaded:,} values for {entity_count:,} entities across "
            f"{len(period_keys)} period(s) from sheet {parsed.layout.sheet_name!r}"
        ),
    )


def _persist_issues(session: Session, upload: Upload, report: ValidationReport) -> None:
    for finding in report.findings:
        session.add(
            ValidationIssue(
                upload_id=upload.id,
                severity=finding.severity,
                code=finding.code,
                message=finding.message,
                column_name=finding.column,
                row_ref=finding.row_ref,
                sample=finding.sample,
            )
        )
    session.flush()


def _load_facts(
    session: Session, upload: Upload, parsed: ParsedSheet, registry: MetricRegistry
) -> tuple[int, int, int]:
    resolver = EntityResolver(session)
    frame = parsed.frame
    grain = parsed.layout.grain
    has_owner = "owner" in parsed.layout.entity_columns
    has_ba = "ba" in parsed.layout.entity_columns
    has_team = "team" in parsed.layout.entity_columns

    # Resolve every distinct entity once rather than per fact row.
    entity_ids: dict[tuple[str, str, str], int] = {}
    combos = frame[["owner", "ba", "team", "period_key"]].drop_duplicates()
    for row in combos.itertuples(index=False):
        owner_name = str(row.owner).strip() if has_owner else ""
        team_name = str(row.team).strip() if has_team else ""
        ba_name = str(row.ba).strip() if has_ba else ""
        owner_entity = resolver.owner(owner_name) if owner_name else None
        team_entity = resolver.team(team_name, owner_entity) if team_name else None
        if ba_name:
            entity = resolver.ba(ba_name, team_entity or owner_entity)
        elif team_entity is not None:
            entity = team_entity
        elif owner_entity is not None:
            entity = owner_entity
        else:
            entity = resolver.org()
        resolver.touch(entity, str(row.period_key))
        for ancestor in (owner_entity, team_entity):
            if ancestor is not None:
                resolver.touch(ancestor, str(row.period_key))
        entity_ids[(owner_name, ba_name, team_name)] = entity.id
    session.flush()

    # -- supersede the previous generation for exactly these coordinates --
    targets: dict[str, set[int]] = {}
    for row in frame[["owner", "ba", "team", "period_key"]].drop_duplicates().itertuples(index=False):
        key = (
            str(row.owner).strip() if has_owner else "",
            str(row.ba).strip() if has_ba else "",
            str(row.team).strip() if has_team else "",
        )
        targets.setdefault(str(row.period_key), set()).add(entity_ids[key])

    superseded = 0
    for period_key, ids in targets.items():
        id_list = list(ids)
        for chunk_start in range(0, len(id_list), 500):
            chunk = id_list[chunk_start : chunk_start + 500]
            result = session.execute(
                update(Fact)
                .where(
                    Fact.period_key == period_key,
                    Fact.entity_id.in_(chunk),
                    Fact.is_current.is_(True),
                )
                .values(is_current=False)
            )
            superseded += int(result.rowcount or 0)

    # -- insert the new generation ---------------------------------------
    denominators = _denominator_lookup(frame, registry)
    payload: list[dict] = []
    for row in frame.itertuples(index=False):
        metric = registry.get(str(row.metric_key))
        if metric is None:
            continue
        key = (
            str(row.owner).strip() if has_owner else "",
            str(row.ba).strip() if has_ba else "",
            str(row.team).strip() if has_team else "",
        )
        entity_id = entity_ids.get(key)
        if entity_id is None:
            continue
        value = to_stored_value(metric, row.source_value)
        if value is None:
            continue
        payload.append(
            {
                "entity_id": entity_id,
                "period_key": str(row.period_key),
                "grain": grain,
                "metric_key": metric.key,
                "value": value,
                "source_value": float(row.source_value),
                "denominator": denominators.get((key, str(row.period_key), metric.key)),
                "upload_id": upload.id,
                "is_current": True,
            }
        )
    if payload:
        session.bulk_insert_mappings(Fact, payload)
    return len(payload), superseded, len(entity_ids)


def _denominator_lookup(frame: pd.DataFrame, registry: MetricRegistry) -> dict:
    """Attach each rate's denominator so alerts can suppress small samples.

    A BA whose Debit 1 fell from 100% to 50% on four submissions is noise; the
    same fall on four hundred is the most important thing in the report.  The
    denominator has to travel with the fact for that judgement to be possible.
    """
    out: dict = {}
    needed = {m.key: m.denominator for m in registry if m.denominator}
    if not needed:
        return out
    base_keys = set(needed.values())
    base = frame.loc[frame["metric_key"].isin(base_keys)]
    if base.empty:
        return out
    index: dict[tuple, float] = {}
    for row in base.itertuples(index=False):
        index[
            ((str(row.owner).strip(), str(row.ba).strip(), str(row.team).strip()),
             str(row.period_key), str(row.metric_key))
        ] = float(row.source_value)
    for row in frame.itertuples(index=False):
        denom_key = needed.get(str(row.metric_key))
        if not denom_key:
            continue
        entity_key = (str(row.owner).strip(), str(row.ba).strip(), str(row.team).strip())
        value = index.get((entity_key, str(row.period_key), denom_key))
        if value is not None:
            out[(entity_key, str(row.period_key), str(row.metric_key))] = value
    return out


def _remember_columns(session: Session, parsed: ParsedSheet) -> None:
    signature = ",".join(sorted(parsed.layout.metric_columns.keys()))
    setting = session.get(Setting, COLUMN_SIGNATURE_KEY)
    if setting is None:
        session.add(Setting(key=COLUMN_SIGNATURE_KEY, value=signature))
    else:
        setting.value = signature


def _mark_superseded_uploads(session: Session, upload: Upload, period_keys: Sequence[str]) -> None:
    """Point older uploads covering the same periods at their replacement."""
    if not period_keys:
        return
    older = session.execute(
        select(Upload).where(
            Upload.id != upload.id,
            Upload.status == UPLOAD_LOADED,
            Upload.period_max.in_(list(period_keys)),
        )
    ).scalars()
    for row in older:
        remaining = session.execute(
            select(func.count())
            .select_from(Fact)
            .where(Fact.upload_id == row.id, Fact.is_current.is_(True))
        ).scalar_one()
        if remaining == 0:
            row.status = UPLOAD_SUPERSEDED
            row.superseded_by_id = upload.id


def ingest_from_storage(
    session: Session,
    backend: StorageBackend,
    *,
    registry: MetricRegistry | None = None,
    archive: bool = True,
    allow_reprocess: bool = False,
    uploaded_by: str = "watcher",
    limit: int | None = None,
) -> list[IngestResult]:
    """Process every unseen file in the watched location, oldest first."""
    results: list[IngestResult] = []
    files = sorted(backend.list_files(), key=lambda f: f.modified_at)
    if limit is not None:
        files = files[:limit]
    if not files:
        return results
    with tempfile.TemporaryDirectory(prefix="qmis-ingest-") as tmp:
        workdir = Path(tmp)
        for remote in files:
            digest = backend.content_hash(remote.uri, workdir)
            local = backend.fetch(remote.uri, workdir / remote.name)
            result = ingest_file(
                session,
                local,
                registry=registry,
                source_uri=remote.uri,
                uploaded_by=uploaded_by,
                allow_reprocess=allow_reprocess,
                content_hash=digest,
            )
            results.append(result)
            if result.accepted and archive:
                backend.archive(remote.uri)
    return results
