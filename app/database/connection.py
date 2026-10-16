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
- ``init_schema()`` creates missing tables and indexes, so it is safe to call on every
  start. For a database file from an older version it adds missing *nullable* columns
  (``ALTER TABLE ... ADD COLUMN``, which keeps all rows); a missing required column is
  reported instead. There is no general migration tool yet.
- ``secure_delete`` is on: SQLite overwrites deleted content with zeros instead of just
  marking it free, so deleted messages do not linger in the file's free pages.
"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event, inspect, literal, select
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
        """Create missing tables and indexes and add missing nullable columns.

        Existing tables and rows are kept.

        Raises:
            DatabaseError: the database could not be opened or written, or an existing
                table lacks a required (non-nullable) column this version needs.
        """
        try:
            Base.metadata.create_all(self.engine)
            outdated: dict[str, list[str]] = {}
            with self.engine.begin() as connection:
                inspector = inspect(connection)
                for table in Base.metadata.sorted_tables:
                    present = {c["name"] for c in inspector.get_columns(table.name)}
                    for column in table.columns:
                        if column.name in present:
                            continue
                        if not column.nullable:
                            outdated.setdefault(table.name, []).append(column.name)
                            continue
                        column_type = column.type.compile(dialect=self.engine.dialect)
                        # Names and types come from our own table definitions.
                        connection.exec_driver_sql(
                            f"ALTER TABLE {table.name} ADD COLUMN {column.name} {column_type}"
                        )
                        logger.info("Added column %s.%s", table.name, column.name)
                    for index in table.indexes:
                        index.create(connection, checkfirst=True)
        except SQLAlchemyError as exc:
            logger.error("Database initialisation failed (%s)", type(exc).__name__)
            raise DatabaseError(f"database initialisation failed ({type(exc).__name__})") from exc
        if outdated:
            logger.error("Database schema is out of date: missing columns %s", outdated)
            raise DatabaseError(
                f"database schema is out of date (missing columns: {outdated}). For a "
                "development database, delete the file so it is recreated."
            )

    def check(self) -> None:
        """Cheap readiness check: the database opens and every table exists.

        Reads no rows (``LIMIT 0``) and takes no write lock, so it is safe to call often.

        Raises:
            DatabaseError: the database cannot be opened, or a table is missing (for
                example because ``init_schema`` failed at startup).
        """
        try:
            with self.engine.connect() as connection:
                for table in Base.metadata.sorted_tables:
                    connection.execute(select(literal(1)).select_from(table).limit(0))
        except SQLAlchemyError as exc:
            raise DatabaseError(f"database check failed ({type(exc).__name__})") from exc

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
            cursor.execute("PRAGMA secure_delete = ON")  # overwrite deleted content
            if wal:  # readers do not block the writer, and vice versa
                cursor.execute("PRAGMA journal_mode = WAL")
        finally:
            cursor.close()

    @event.listens_for(engine, "begin")
    def _on_begin(connection: Any) -> None:
        mode = connection.get_execution_options().get("sqlite_begin", "deferred")
        connection.exec_driver_sql(_BEGIN_STATEMENTS[mode])
