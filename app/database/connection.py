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
  reported instead. Conversation tables are only ever extended this way.
- The support tables (``REBUILDABLE_TABLES``) are rebuilt when their stored definition
  differs from the code, because SQLite cannot change a CHECK constraint in place (for
  example a new ticket status). The rebuild follows SQLite's documented procedure in one
  transaction: rename the old table, create the new one, copy every row, drop the old
  one, then check foreign keys. Any failure (a row the new rules reject, a broken
  reference) rolls everything back and is reported; nothing is lost. There is no general
  migration tool yet.
- Before changing anything, ``init_schema`` works out what it will change
  (``planned_changes``). **It never discards a column**: a table to rebuild that holds
  columns the code does not define (added by a newer release, so this is a rollback)
  makes it refuse to start, with nothing changed (Stage 13). And before it adds a column
  or rebuilds a table in an existing database, it takes a verified backup into
  ``upgrade_backup_dir``; if that fails, nothing is upgraded. New tables and indexes
  touch no existing row and need no backup. docs/deployment-rollback.md.
- ``record_release`` keeps the deployment history in ``app_releases``: each release
  (version, revision) and schema that ran against this database.
- ``secure_delete`` is on: SQLite overwrites deleted content with zeros instead of just
  marking it free, so deleted messages do not linger in the file's free pages.
"""

import hashlib
import logging
import os
import re
import shutil
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, Engine, Table, create_engine, event, inspect, literal, select
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy.schema import CreateTable

from app.core.config import PROJECT_ROOT
from app.core.metrics import metrics
from app.database.models import REBUILDABLE_TABLES, Base

logger = logging.getLogger(__name__)

DEFAULT_BUSY_TIMEOUT_SECONDS = 5.0
_BEGIN_STATEMENTS = {"deferred": "BEGIN", "immediate": "BEGIN IMMEDIATE"}
# Values for required columns added since a support table was first created, used when
# its rows are copied into the rebuilt table. SQL literals from our own code only.
_REBUILD_FILL: dict[tuple[str, str], str] = {
    # Events from before actors were recorded were written by the application itself.
    ("ticket_events", "actor_type"): "'system'",
    # Sales tickets from before lead categories: a general sales inquiry.
    ("support_tickets", "lead_category"): "CASE WHEN ticket_type = 'sales' "
    "THEN 'general_sales' END",
}


class _RebuildError(Exception):
    """A support table could not be rebuilt; the transaction was rolled back."""


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


# Filesystems on which SQLite's file locking (and WAL's shared memory) is unreliable:
# network filesystems, and 9p, which carries Windows drives into WSL 2. A database on one
# of them can be corrupted by two writers that both believe they hold the lock.
NETWORK_FILESYSTEMS = frozenset(
    {
        "nfs", "nfs4", "cifs", "smb3", "smbfs", "9p", "drvfs", "fuse.sshfs", "sshfs",
        "glusterfs", "fuse.glusterfs", "ceph", "fuse.ceph", "lustre", "afs", "davfs",
        "fuse.s3fs", "fuse.rclone", "fuse.gcsfuse",
    }
)  # fmt: skip
MOUNTS_FILE = Path("/proc/self/mounts")
# Below this, readiness reports the database as unusable: a WAL commit and checkpoint
# need room, and SQLite fails every write with "database or disk is full" once it runs out.
MIN_FREE_DISK_BYTES = 32 * 1024 * 1024


def _check_writable(path: Path) -> None:
    """Raise ``DatabaseError`` when the database file cannot be written now. Writes nothing.

    The messages name the problem only, never the path.
    """
    folder = path.parent
    # The folder too: SQLite creates the -wal and -shm files beside the database.
    if not os.access(folder, os.W_OK) or (path.exists() and not os.access(path, os.W_OK)):
        raise DatabaseError("database check failed (not writable)")
    try:
        free = shutil.disk_usage(folder).free
    except OSError as exc:
        raise DatabaseError(f"database check failed ({type(exc).__name__})") from exc
    if free < MIN_FREE_DISK_BYTES:
        raise DatabaseError("database check failed (disk almost full)")


def filesystem_type(path: Path, mounts_file: Path | None = None) -> str | None:
    """The type of the filesystem holding ``path`` (Linux), or None if unknown (another
    operating system, or no mount table)."""
    try:
        lines = (mounts_file or MOUNTS_FILE).read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    target = path.resolve().as_posix()
    best, best_type = "", None
    for line in lines:
        fields = line.split()
        if len(fields) < 3:
            continue
        # Spaces and other characters in mount points are escaped as octal (\040).
        mount_point = fields[1].encode().decode("unicode_escape")
        inside = target == mount_point or target.startswith(mount_point.rstrip("/") + "/")
        if inside and len(mount_point) >= len(best):
            best, best_type = mount_point, fields[2]
    return best_type


class Database:
    """Engine and transaction factory for one SQLite database."""

    def __init__(
        self,
        database_url: str,
        *,
        busy_timeout_seconds: float = DEFAULT_BUSY_TIMEOUT_SECONDS,
        refuse_network_filesystem: bool = False,
        upgrade_backup_dir: Path | None = None,
    ) -> None:
        """
        Args:
            database_url: A SQLite URL.
            upgrade_backup_dir: Where ``init_schema`` puts a verified backup before it
                changes an existing table (adds a column, rebuilds a table). None: no
                automatic backup (tests, scripts that take their own).
            busy_timeout_seconds: How long a writer waits for the write lock before
                failing (``database is locked``).
            refuse_network_filesystem: Raise ``DatabaseError`` instead of only logging an
                error when the file is on a network filesystem (production does).
        """
        url = resolve_sqlite_url(database_url)
        self.upgrade_backup_dir = upgrade_backup_dir
        self.in_memory = _is_memory(url)
        self.path: Path | None = None if self.in_memory else Path(url.database)
        options: dict[str, Any] = {
            # Sessions are used from FastAPI's worker threads; each session still holds a
            # connection for one transaction only.
            "connect_args": {"check_same_thread": False, "timeout": busy_timeout_seconds},
        }
        if self.in_memory:
            # An in-memory database exists per connection: share a single one.
            options["poolclass"] = StaticPool
        else:
            folder = Path(url.database).parent
            folder.mkdir(parents=True, exist_ok=True)
            kind = filesystem_type(folder)
            if kind in NETWORK_FILESYSTEMS:
                message = (
                    f"the database folder is on a {kind} filesystem, where SQLite's locking "
                    "is unreliable and the file can be corrupted. Put DATABASE_URL on a "
                    "local disk (in Docker: a named volume)."
                )
                if refuse_network_filesystem:
                    raise DatabaseError(message)
                logger.error("Database: %s", message)
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
            # Decide everything before changing anything: a refusal (a column a newer
            # release added, which a rebuild would discard) leaves the file untouched,
            # and existing tables are only changed after a verified backup.
            changes = self.planned_changes()
            if changes.alters_existing_tables:
                self._backup_before_upgrade(changes)
            Base.metadata.create_all(self.engine)
            self._rebuild_tables(changes.rebuilt_tables)
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
        except _RebuildError as exc:
            logger.error("Database upgrade failed: %s", exc)
            raise DatabaseError(f"database upgrade failed: {exc}") from None
        if outdated:
            logger.error("Database schema is out of date: missing columns %s", outdated)
            raise DatabaseError(
                f"database schema is out of date (missing columns: {outdated}). For a "
                "development database, delete the file so it is recreated."
            )

    def planned_changes(self) -> "SchemaChanges":
        """What ``init_schema`` would change in this database, without changing it.

        Raises:
            DatabaseError: a table that would be rebuilt holds columns this version does
                not define. They were added by a newer release (this is a rollback), and
                the rebuild would discard them with their data, so nothing is done.
        """
        with self.engine.connect() as connection:
            inspector = inspect(connection)
            existing = set(inspector.get_table_names())
            if not existing & set(Base.metadata.tables):
                return SchemaChanges((), (), ())  # a new database: nothing to protect
            new_tables = tuple(t for t in Base.metadata.tables if t not in existing)
            rebuilt: list[str] = []
            added: list[str] = []
            unknown: dict[str, list[str]] = {}
            for table in Base.metadata.sorted_tables:
                if table.name not in existing:
                    continue
                present = {c["name"] for c in inspector.get_columns(table.name)}
                if table.name in REBUILDABLE_TABLES:
                    expected = str(CreateTable(table).compile(dialect=self.engine.dialect))
                    stored = connection.exec_driver_sql(
                        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
                        (table.name,),
                    ).scalar()
                    if stored is not None and not _same_sql(stored, expected):
                        extra = sorted(present - {c.name for c in table.columns})
                        if extra:
                            unknown[table.name] = extra
                        rebuilt.append(table.name)
                        continue
                added += [
                    f"{table.name}.{c.name}"
                    for c in table.columns
                    if c.name not in present and c.nullable
                ]
        if unknown:
            columns = ", ".join(f"{t}.{c}" for t, cs in unknown.items() for c in cs)
            raise DatabaseError(
                f"the database has column(s) this release does not know ({columns}): it was "
                "upgraded by a newer release, and starting this one would discard them. "
                "Nothing was changed. Deploy the newer release again, or restore the backup "
                "taken before the upgrade (docs/deployment-rollback.md)"
            )
        return SchemaChanges(new_tables, tuple(added), tuple(rebuilt))

    def _backup_before_upgrade(self, changes: "SchemaChanges") -> None:
        """A verified backup of the database as it is, before its tables are changed.

        Raises:
            DatabaseError: the backup failed; the schema is left as it is.
        """
        if self.upgrade_backup_dir is None or self.path is None or not self.path.is_file():
            return
        from app.database.backups import BackupError, create_backup

        try:
            backup = create_backup(
                self.path, self.upgrade_backup_dir, reason="before schema upgrade", as_found=True
            )
        except (BackupError, OSError) as exc:
            raise DatabaseError(
                f"the schema upgrade needs a backup first, and it failed ({exc}); "
                "nothing was changed"
            ) from None
        logger.warning(
            "event=schema_upgrade_backup file=%s Backed up the database before upgrading "
            "its schema (%s)",
            backup.path,
            changes.describe(),
        )

    def schema_fingerprint(self) -> str:
        """SHA-256 of the schema's SQL, computed as for backup manifests (``schema_sha256``)."""
        with self.engine.connect() as connection:
            statements = sorted(
                sql
                for (sql,) in connection.exec_driver_sql(
                    "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL "
                    "AND name NOT LIKE 'sqlite_%'"
                )
            )
        return hashlib.sha256("\n".join(statements).encode()).hexdigest()

    def record_release(self, app_version: str, revision: str | None) -> bool:
        """Add a row to ``app_releases`` if this release (version, revision) or the schema
        differs from the last one recorded. Returns whether a row was added."""
        from app.database.models import AppReleaseRow

        fingerprint = self.schema_fingerprint()
        with self.write() as db:
            last = db.scalars(
                select(AppReleaseRow).order_by(AppReleaseRow.id.desc()).limit(1)
            ).first()
            current = (app_version, revision, fingerprint)
            if last is not None and current == (
                last.app_version,
                last.revision,
                last.schema_sha256,
            ):
                return False
            db.add(
                AppReleaseRow(
                    started_at=datetime.now(UTC),
                    app_version=app_version,
                    revision=revision,
                    schema_sha256=fingerprint,
                )
            )
        logger.info(
            "event=release_recorded app_version=%s revision=%s schema=%s",
            app_version,
            revision or "-",
            fingerprint[:12],
        )
        return True

    def _rebuild_tables(self, names: tuple[str, ...]) -> None:
        """Rebuild the support tables whose stored CREATE statement is out of date (as
        found by ``planned_changes``, which also refused any that would lose a column)."""
        for table in (Base.metadata.tables[name] for name in names):
            # A fresh connection, so nothing has begun a transaction on it yet.
            with self.engine.connect() as connection:
                _rebuild_table(connection, table)
            logger.info("Upgraded table %s (rebuilt, all rows kept)", table.name)

    def check(self) -> None:
        """Cheap readiness check: the database opens, every table exists, and (for a
        file) it can still be written.

        Reads no rows (``LIMIT 0``) and takes no write lock, so it is safe to call often.
        Writability is judged without writing: the folder and file are writable and the
        disk has at least ``MIN_FREE_DISK_BYTES`` free. Every chat message is a write,
        so a full or read-only disk means chat cannot work, even though reads still do.

        Raises:
            DatabaseError: the database cannot be opened, a table is missing (for
                example because ``init_schema`` failed at startup), or it cannot be
                written.
        """
        try:
            with self.engine.connect() as connection:
                for table in Base.metadata.sorted_tables:
                    connection.execute(select(literal(1)).select_from(table).limit(0))
        except SQLAlchemyError as exc:
            raise DatabaseError(f"database check failed ({type(exc).__name__})") from exc
        if self.path is not None:
            _check_writable(self.path)

    @contextmanager
    def read(self) -> Iterator[Session]:
        """A session for reads, in one consistent transaction."""
        with _timed("read"), self._reader.begin() as session:
            yield session

    @contextmanager
    def write(self) -> Iterator[Session]:
        """A session holding the write lock; commits on success, rolls back on error.

        Its time includes waiting for the write lock, so slow writes show contention."""
        with _timed("write"), self._writer.begin() as session:
            yield session

    def dispose(self) -> None:
        """Close all pooled connections (call at application shutdown)."""
        self.engine.dispose()


@contextmanager
def _timed(kind: str) -> Iterator[None]:
    """Time one transaction for the metrics; count it as failed on a database error
    (not on an application exception raised inside it, such as "not found")."""
    started = time.perf_counter()
    try:
        yield
    except SQLAlchemyError:
        metrics.increment("db_transaction_errors", kind=kind)
        raise
    finally:
        elapsed_ms = (time.perf_counter() - started) * 1000
        metrics.observe_ms("db_transaction_duration", elapsed_ms, kind=kind)


@dataclass(frozen=True)
class SchemaChanges:
    """What ``init_schema`` is about to change (``Database.planned_changes``)."""

    new_tables: tuple[str, ...]
    added_columns: tuple[str, ...]  # "table.column", nullable
    rebuilt_tables: tuple[str, ...]

    @property
    def alters_existing_tables(self) -> bool:
        """New tables and indexes touch no existing row; added columns and rebuilds do."""
        return bool(self.added_columns or self.rebuilt_tables)

    def describe(self) -> str:
        parts = []
        if self.added_columns:
            parts.append("add " + ", ".join(self.added_columns))
        if self.rebuilt_tables:
            parts.append("rebuild " + ", ".join(self.rebuilt_tables))
        if self.new_tables:
            parts.append("create " + ", ".join(self.new_tables))
        return "; ".join(parts) or "nothing"


def _same_sql(stored: str, expected: str) -> bool:
    return re.sub(r"\s+", "", stored) == re.sub(r"\s+", "", expected)


def _rebuild_table(connection: Connection, table: Table) -> None:
    """Rebuild ``table`` with its current definition, keeping every row.

    SQLite's procedure for schema changes ALTER TABLE cannot make: foreign keys off (only
    possible outside a transaction), then in one transaction rename, create, copy, drop
    and check foreign keys. ``legacy_alter_table`` stops the rename from rewriting other
    tables' references to point at the old copy.

    Raises:
        _RebuildError: a required column has no value to copy, a row breaks the new
            constraints or the rebuild would break a reference that was intact before.
            Nothing was changed.
    """
    name, old = table.name, f"{table.name}__old"
    # The table itself and the tables that reference it: where a rebuild could break a
    # foreign key. Problems that existed before are not made the rebuild's fault.
    related = [name] + [
        other.name
        for other in Base.metadata.sorted_tables
        if other.name != name and any(fk.column.table.name == name for fk in other.foreign_keys)
    ]
    # Straight to the driver: through SQLAlchemy these would begin a transaction, inside
    # which SQLite ignores foreign_keys.
    raw = connection.connection.driver_connection
    raw.execute("PRAGMA foreign_keys = OFF")
    raw.execute("PRAGMA legacy_alter_table = ON")
    try:
        with connection.begin():
            broken_before = _foreign_key_problems(connection, related)
            inspector = inspect(connection)
            present = {column["name"] for column in inspector.get_columns(name)}
            columns, values = [], []
            for column in table.columns:
                if column.name in present:
                    columns.append(column.name)
                    values.append(column.name)
                elif (name, column.name) in _REBUILD_FILL:
                    columns.append(column.name)
                    values.append(_REBUILD_FILL[(name, column.name)])
                elif not column.nullable:
                    raise _RebuildError(f"{name}.{column.name} is required and has no value")
            # Index names are global in SQLite: free them for the new table.
            for index in inspector.get_indexes(name):
                connection.exec_driver_sql(f'DROP INDEX IF EXISTS "{index["name"]}"')
            # Trigger names are global too, and the table's triggers are created with
            # "IF NOT EXISTS" when the new table is: drop the old ones first, or they
            # would be kept on the old copy, skipped for the new one and then dropped
            # with the old copy (an audit log silently losing its append-only guard).
            triggers = connection.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = ?", (name,)
            ).scalars()
            for trigger in list(triggers):
                connection.exec_driver_sql(f'DROP TRIGGER IF EXISTS "{trigger}"')
            # Names come from our own table definitions, never from input.
            connection.exec_driver_sql(f'ALTER TABLE "{name}" RENAME TO "{old}"')
            table.create(connection)
            try:
                connection.exec_driver_sql(
                    f'INSERT INTO "{name}" ({", ".join(columns)}) '  # noqa: S608
                    f'SELECT {", ".join(values)} FROM "{old}"'
                )
            except SQLAlchemyError as exc:
                raise _RebuildError(
                    f"rows of {name} do not satisfy its new rules ({type(exc).__name__})"
                ) from None
            connection.exec_driver_sql(f'DROP TABLE "{old}"')
            if _foreign_key_problems(connection, related) > broken_before:
                raise _RebuildError(f"rebuilding {name} would break a foreign key")
    finally:
        raw.execute("PRAGMA legacy_alter_table = OFF")
        raw.execute("PRAGMA foreign_keys = ON")


def _foreign_key_problems(connection: Connection, tables: list[str]) -> int:
    """How many rows of ``tables`` reference a row that does not exist."""
    return sum(
        len(connection.exec_driver_sql(f'PRAGMA foreign_key_check("{name}")').fetchall())
        for name in tables
    )


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
