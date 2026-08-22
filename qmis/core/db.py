"""Engine and session management.

The database URL comes from ``QMIS_DATABASE_URL`` and defaults to a local
SQLite file.  Every query in the system goes through SQLAlchemy Core/ORM with
portable types, so pointing this at PostgreSQL or Supabase is a URL change
plus ``init_db()`` - no application code moves.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from qmis.core.models import Base

DEFAULT_DB_PATH = Path(os.environ.get("QMIS_HOME", Path.cwd() / "data")) / "qmis.db"

_ENGINE: Engine | None = None
_SESSION_FACTORY: sessionmaker[Session] | None = None


def database_url() -> str:
    url = os.environ.get("QMIS_DATABASE_URL")
    if url:
        return url
    DEFAULT_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{DEFAULT_DB_PATH}"


def get_engine(url: str | None = None, echo: bool = False) -> Engine:
    global _ENGINE, _SESSION_FACTORY
    if _ENGINE is not None and url is None:
        return _ENGINE
    target = url or database_url()
    kwargs: dict = {"echo": echo, "future": True}
    if target.startswith("sqlite"):
        # Streamlit serves each user on its own thread; SQLite needs telling.
        kwargs["connect_args"] = {"check_same_thread": False}
    engine = create_engine(target, **kwargs)
    if target.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover - driver hook
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.close()

    _ENGINE = engine
    _SESSION_FACTORY = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    return engine


def get_session_factory(url: str | None = None) -> sessionmaker[Session]:
    if _SESSION_FACTORY is None or url is not None:
        get_engine(url)
    assert _SESSION_FACTORY is not None
    return _SESSION_FACTORY


@contextmanager
def session_scope(url: str | None = None) -> Iterator[Session]:
    """Transactional session: commit on success, roll back on any exception."""
    factory = get_session_factory(url)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db(url: str | None = None, drop: bool = False) -> Engine:
    engine = get_engine(url)
    if drop:
        Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    return engine


def reset_engine() -> None:
    """Drop cached engine/session factory (used by tests)."""
    global _ENGINE, _SESSION_FACTORY
    if _ENGINE is not None:
        _ENGINE.dispose()
    _ENGINE = None
    _SESSION_FACTORY = None
