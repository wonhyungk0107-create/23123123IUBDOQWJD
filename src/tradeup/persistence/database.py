"""Engine and session management.

SQLite is the default and must work from a clean checkout with no services running.
PostgreSQL works through the same repository layer with no code change -- the only
differences are in the URL and in which partial-index dialect keyword applies.

Two SQLite pragmas are set on every connection: ``foreign_keys=ON`` (off by default
in SQLite, which would silently let orphaned rows accumulate) and WAL journalling so
a reader is not blocked by the scan writing.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from tradeup.persistence.models import Base

__all__ = ["Database", "create_database"]


class Database:
    """Owns the engine and hands out sessions."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    @property
    def engine(self) -> Engine:
        return self._engine

    @property
    def url(self) -> str:
        return self._engine.url.render_as_string(hide_password=True)

    @property
    def is_sqlite(self) -> bool:
        return self._engine.dialect.name == "sqlite"

    def create_all(self) -> None:
        """Create the schema directly.

        Used by tests and the offline demo. Production paths use Alembic so that
        schema changes are versioned and reviewable.
        """
        Base.metadata.create_all(self._engine)

    def drop_all(self) -> None:
        Base.metadata.drop_all(self._engine)

    @contextmanager
    def session(self) -> Iterator[Session]:
        """Transactional scope. Commits on success, rolls back on any exception."""
        session = self._session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def dispose(self) -> None:
        self._engine.dispose()


def _configure_sqlite(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection: Any, connection_record: Any) -> None:
        cursor = dbapi_connection.cursor()
        # SQLite disables foreign keys per connection by default.
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()


def create_database(url: str, *, echo: bool = False) -> Database:
    """Build a :class:`Database` for a URL, creating the parent directory if needed."""
    if url.startswith("sqlite") and ":memory:" not in url:
        # Fail early and clearly rather than letting SQLite report "unable to open".
        path_part = url.split("///", 1)[-1]
        if path_part:
            Path(path_part).parent.mkdir(parents=True, exist_ok=True)

    engine = create_engine(url, echo=echo, future=True)
    if engine.dialect.name == "sqlite":
        _configure_sqlite(engine)
    return Database(engine)
