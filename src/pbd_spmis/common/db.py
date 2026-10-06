"""Database access.

Each service owns its own SQLAlchemy ``Base`` and engine so that stores stay physically
separable (Identity Vault, Social Registry, Program Store, Audit Store ... are different
databases in production). The engine factory keys on the service name and the configured URL
template, so SQLite files or PostgreSQL schemas are selected without code changes.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from ..config import get_settings

_engines: dict[str, Engine] = {}
_sessionmakers: dict[str, sessionmaker[Session]] = {}


def engine_for(service: str, base: type[DeclarativeBase]) -> Engine:
    key = f"{service}:{get_settings().database_url_for(service)}"
    if key not in _engines:
        url = get_settings().database_url_for(service)
        connect_args = {}
        if url.startswith("sqlite"):
            path = url.replace("sqlite:///", "", 1)
            if path and path != ":memory:":
                Path(os.path.dirname(path) or ".").mkdir(parents=True, exist_ok=True)
            connect_args = {"check_same_thread": False}
        engine = create_engine(url, connect_args=connect_args, future=True)
        base.metadata.create_all(engine)
        _engines[key] = engine
        _sessionmakers[key] = sessionmaker(bind=engine, expire_on_commit=False)
    return _engines[key]


@contextmanager
def session_for(service: str, base: type[DeclarativeBase]) -> Iterator[Session]:
    engine_for(service, base)
    key = f"{service}:{get_settings().database_url_for(service)}"
    session = _sessionmakers[key]()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def reset_engines() -> None:
    """Dispose all engines (tests use this between isolated runs)."""
    for engine in _engines.values():
        engine.dispose()
    _engines.clear()
    _sessionmakers.clear()
