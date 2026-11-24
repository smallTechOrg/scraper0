"""One engine per process with a hard connection cap (shared Postgres instance: 2 + 1 overflow)."""
from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from scraper0.config import get_settings

_engine: Engine | None = None
_factory: sessionmaker | None = None
_lock = threading.Lock()


def make_engine(url: str, pool_size: int = 2, max_overflow: int = 1) -> Engine:
    if url.startswith("sqlite"):
        kw: dict = {"connect_args": {"check_same_thread": False, "timeout": 30}}
        if url in ("sqlite://", "sqlite:///:memory:"):
            kw["poolclass"] = StaticPool
        else:
            path = url.split("///", 1)[-1]
            if os.path.dirname(path):
                os.makedirs(os.path.dirname(path), exist_ok=True)
        eng = create_engine(url, future=True, **kw)

        @event.listens_for(eng, "connect")
        def _pragmas(dbapi_conn, _):  # WAL lets the API read while a job writes
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            if url not in ("sqlite://", "sqlite:///:memory:"):
                cur.execute("PRAGMA journal_mode=WAL")
            cur.close()
        return eng
    url = url.replace("postgresql://", "postgresql+psycopg://", 1) if url.startswith("postgresql://") else url
    return create_engine(url, future=True, pool_size=pool_size, max_overflow=max_overflow,
                         pool_pre_ping=True, pool_recycle=1800, pool_timeout=60)


def get_engine() -> Engine:
    global _engine, _factory
    with _lock:
        if _engine is None:
            s = get_settings()
            _engine = make_engine(s.database_url, s.db_pool_size, s.db_max_overflow)
            _factory = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
        return _engine


def set_engine(engine: Engine) -> None:
    """Tests: point the whole app at a given engine."""
    global _engine, _factory
    with _lock:
        _engine = engine
        _factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)


@contextmanager
def session_scope() -> Iterator[Session]:
    """Short-lived session: commit on success, roll back on error. Never hold one across network I/O —
    with three connections total, a crawl holding a connection would starve the API."""
    get_engine()
    s = _factory()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()
