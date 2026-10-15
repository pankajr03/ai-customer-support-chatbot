"""SQLite engine, transactions and schema initialisation for conversation storage.

SQLite is a development and small-demonstration choice: one file, no server, nothing to
install. It allows one writer at a time and does not suit several machines sharing one
database, so larger deployments should move to a database server (see the README).

Design:
- One ``Database`` (engine + connection pool) per application, created at startup and
  disposed at shutdown. There is no module-level engine or shared session.
- Every unit of work gets its own short-lived ``Session`` from ``read()`` or ``write()``.
  The transaction commits when the block ends and rolls back if it raises.
- ``write()`` starts with ``BEGIN IMMEDIATE``, which takes SQLite's write lock up front.
  A read-then-write (e.g. "next message number = last + 1, then insert") therefore cannot
  interleave with another writer; a second writer waits up to ``busy_timeout`` seconds.
- Foreign keys are switched on for every connection (SQLite leaves them off by default).
- ``init_schema()`` only creates missing tables, so it is safe to call on every start.
  It does not alter existing tables: a table missing a column (a database file from an
  older version) is reported instead of being used. There are no migrations yet.
"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event, inspect
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import PROJECT_ROOT
from app.database.models import Base

logger = logging.getLogger(__name__)

DEFAULT_BUSY_TIMEOUT_SECONDS = 5.0
_BEGIN_STATEMENTS = {"deferred": "BEGIN", "immediate": "BEGIN IMMEDIATE"}


class DatabaseError(Exception):
    """The database could not be opened, initialised or used.

    Messages name the operation and the error type only, never SQL parameters, so
    customer messages cannot leak into logs or responses through them.
    """


def resolve_sqlite_url(database_url: str, root: Path = PROJECT_ROOT) -> URL:
    """Parse a SQLite URL, resolving a relative file path against ``root``.

    ``sqlite://`` and ``sqlite:///:memory:`` stay in memory.

    Raises:
        ValueError: not a SQLite URL.
    """
    url = make_url(database_url)
    if url.get_backend_name() != "sqlite":
        raise ValueError("only SQLite database URLs are supported")
    if _is_memory(url) or Path(url.database).is_absolute():
        return url
    return url.set(database=str(root / url.database))


def _is_memory(url: URL) -> bool:
    return url.database in (None, "", ":memory:")


class Database:
    """Engine and transaction factory for one SQLite database."""

    def __init__(
        self, database_url: str, *, busy_timeout_seconds: float = DEFAULT_BUSY_TIMEOUT_SECONDS
    ) -> None:
        url = resolve_sqlite_url(database_url)
        self.in_memory = _is_memory(url)
        options: dict[str, Any] = {
            # Sessions are used from FastAPI's worker threads; each session still holds a
            # connection for one transaction only.
            "connect_args": {"check_same_thread": False, "timeout": busy_timeout_seconds},
        }
        if self.in_memory:
            # An in-memory database exists per connection: share a single one.
            options["poolclass"] = StaticPool
        else:
            Path(url.database).parent.mkdir(parents=True, exist_ok=True)
        self.engine: Engine = create_engine(url, **options)
        _configure_sqlite(self.engine, wal=not self.in_memory)
        self._reader = sessionmaker(self.engine, expire_on_commit=False)
        self._writer = sessionmaker(
            self.engine.execution_options(sqlite_begin="immediate"), expire_on_commit=False
        )

    def init_schema(self) -> None:
        """Create any missing tables and indexes. Existing tables and rows are kept.

        Raises:
            DatabaseError: the database could not be opened or written, or an existing
                table lacks columns this version needs.
        """
        try:
            Base.metadata.create_all(self.engine)
            inspector = inspect(self.engine)
            outdated = {
                table.name: sorted(missing)
                for table in Base.metadata.sorted_tables
                if (
                    missing := {c.name for c in table.columns}
                    - {c["name"] for c in inspector.get_columns(table.name)}
                )
            }
        except SQLAlchemyError as exc:
            logger.error("Database initialisation failed (%s)", type(exc).__name__)
            raise DatabaseError(f"database initialisation failed ({type(exc).__name__})") from exc
        if outdated:
            logger.error("Database schema is out of date: missing columns %s", outdated)
            raise DatabaseError(
                f"database schema is out of date (missing columns: {outdated}). For a "
                "development database, delete the file so it is recreated."
            )

    @contextmanager
    def read(self) -> Iterator[Session]:
        """A session for reads, in one consistent transaction."""
        with self._reader.begin() as session:
            yield session

    @contextmanager
    def write(self) -> Iterator[Session]:
        """A session holding the write lock; commits on success, rolls back on error."""
        with self._writer.begin() as session:
            yield session

    def dispose(self) -> None:
        """Close all pooled connections (call at application shutdown)."""
        self.engine.dispose()


def _configure_sqlite(engine: Engine, *, wal: bool) -> None:
    """Per-connection pragmas, and explicit BEGIN statements.

    Python's sqlite3 driver otherwise starts transactions lazily, just before the first
    write, and never with IMMEDIATE. SQLAlchemy's documented fix: turn the driver's
    transaction handling off and emit BEGIN from SQLAlchemy's ``begin`` event.
    """

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_connection: Any, _record: Any) -> None:
        dbapi_connection.isolation_level = None
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys = ON")
            if wal:  # readers do not block the writer, and vice versa
                cursor.execute("PRAGMA journal_mode = WAL")
        finally:
            cursor.close()

    @event.listens_for(engine, "begin")
    def _on_begin(connection: Any) -> None:
        mode = connection.get_execution_options().get("sqlite_begin", "deferred")
        connection.exec_driver_sql(_BEGIN_STATEMENTS[mode])
