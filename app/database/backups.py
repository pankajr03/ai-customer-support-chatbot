"""Consistent SQLite backups and verified restores (Stage 13).

Used by ``scripts/backup_database.py``, ``scripts/restore_database.py`` and
``scripts/import_faqs.py --backup``. See docs/backup-and-restore.md.

Backups
-------
- Made with SQLite's **online backup API** from a normal connection: a transactionally
  consistent snapshot, including whatever is still in the write-ahead log, taken while
  the application keeps reading and writing. The live file is never copied byte by byte.
- Written to a hidden ``.partial`` file first, verified (``integrity_check``,
  ``foreign_key_check``, every application table present), then published under a
  **new** timestamped name: an existing backup is never overwritten (a second backup in
  the same second gets ``-1``, ``-2``, ...).
- One self-contained file: the copy is switched from WAL to a rollback journal, so no
  ``-wal`` or ``-shm`` file belongs to it.
- Next to it, a **manifest** (``<backup>.manifest.json``): SHA-256 of the file, sizes,
  table row counts, a fingerprint of the schema, the SQLite and application versions.
  Counts and names only, never content.
- Files are readable by their owner only (``0600``), the folder ``0700`` (POSIX).

Restores
--------
- ``restore_to_new``: into a database file that does not exist yet (a temporary one for
  testing). Never touches the active database.
- ``replace_database``: the destructive path, only with explicit confirmation (enforced by
  the restore script). The active database is backed up first (a safety backup); the
  restored copy is verified beside it, its ``-wal``/``-shm`` files are removed, and it
  replaces the active file in one rename.
- Both check the manifest's SHA-256 first: a damaged or altered backup is refused.
"""

import hashlib
import json
import os
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import count
from pathlib import Path
from typing import Any

from app import __version__

MANIFEST_SUFFIX = ".manifest.json"
MANIFEST_FORMAT = 1
FILE_MODE = 0o600
FOLDER_MODE = 0o700
SIDE_FILES = ("-wal", "-shm", "-journal")


class BackupError(Exception):
    """A backup or restore could not be made, or did not verify. Nothing was replaced."""


@dataclass(frozen=True)
class Backup:
    path: Path
    manifest_path: Path
    manifest: dict[str, Any]


def manifest_path_for(backup: Path) -> Path:
    return backup.with_name(backup.name + MANIFEST_SUFFIX)


def application_tables() -> frozenset[str]:
    from app.database.models import Base  # the schema of this version of the code

    return frozenset(Base.metadata.tables)


@contextmanager
def _connect(path: Path, *, read_only: bool = False) -> Iterator[sqlite3.Connection]:
    target = f"file:{path.as_posix()}?mode=ro" if read_only else str(path)
    connection = sqlite3.connect(target, uri=read_only)
    try:
        yield connection
    finally:
        connection.close()


def _copy(source: Path, destination: Path) -> None:
    """SQLite's online backup from ``source`` into a new ``destination``, then a plain
    rollback journal so the copy is a single file."""
    with _connect(source) as src, _connect(destination) as dst:
        src.backup(dst)
        dst.execute("PRAGMA journal_mode=DELETE")


def verify_database(path: Path, required_tables: frozenset[str] | None = None) -> list[str]:
    """Problems found in a database file; empty when it is sound and complete.

    ``required_tables``: the tables it must contain. Default: every table this version of
    the application uses. A backup of an older database (before a schema upgrade) needs
    the tables that database had, not the ones the upgrade will add."""
    try:
        with _connect(path, read_only=True) as connection:
            problems = []
            integrity = connection.execute("PRAGMA integrity_check").fetchall()
            if integrity != [("ok",)]:
                problems.append(f"integrity_check reported {len(integrity)} problem(s)")
            broken = connection.execute("PRAGMA foreign_key_check").fetchall()
            if broken:
                problems.append(f"{len(broken)} row(s) reference a missing parent row")
            tables = {
                name
                for (name,) in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            required = application_tables() if required_tables is None else required_tables
            missing = sorted(required - tables)
            if missing:
                problems.append(f"missing tables: {', '.join(missing)}")
            return problems
    except sqlite3.DatabaseError as exc:
        return [f"not a readable SQLite database ({type(exc).__name__})"]


def table_counts(path: Path) -> dict[str, int]:
    with _connect(path, read_only=True) as connection:
        names = [
            name
            for (name,) in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        # Names come from the database's own catalogue, quoted.
        return {
            name: connection.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]  # noqa: S608
            for name in names
        }


def schema_fingerprint(path: Path) -> str:
    with _connect(path, read_only=True) as connection:
        statements = sorted(
            sql
            for (sql,) in connection.execute(
                "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"
            )
        )
    return hashlib.sha256("\n".join(statements).encode()).hexdigest()


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _restrict(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError:  # pragma: no cover - filesystems without POSIX permissions
        pass


def _publish(temporary: Path, folder: Path, stem: str, stamp: str) -> Path:
    """Give the verified copy its final, unused name, without ever replacing a file."""
    for number in count():
        name = f"{stem}-{stamp}.db" if number == 0 else f"{stem}-{stamp}-{number}.db"
        final = folder / name
        try:
            os.link(temporary, final)  # fails if the name exists: never overwrites
        except FileExistsError:
            continue
        except OSError:  # pragma: no cover - filesystems without hard links
            if final.exists():
                continue
            temporary.rename(final)
            return final
        temporary.unlink()
        return final
    raise AssertionError("unreachable")  # pragma: no cover


def create_backup(
    source: Path,
    folder: Path,
    *,
    now: datetime | None = None,
    reason: str | None = None,
    as_found: bool = False,
) -> Backup:
    """Back up the SQLite database ``source`` into ``folder``.

    Args:
        reason: Recorded in the manifest, e.g. "before schema upgrade".
        as_found: Verify the copy against the tables ``source`` has, not the ones this
            version of the application uses (a backup taken before an upgrade).

    Raises:
        BackupError: the source does not exist, or the copy did not verify (it is removed).
    """
    if not source.is_file():
        raise BackupError(f"no database file at {source}")
    now = now or datetime.now(UTC)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    folder.mkdir(parents=True, exist_ok=True)
    _restrict(folder, FOLDER_MODE)
    temporary = folder / f".{source.stem}-{stamp}-{uuid.uuid4().hex}.partial"
    try:
        _copy(source, temporary)
        _restrict(temporary, FILE_MODE)
        required = frozenset(table_counts(source)) if as_found else None
        problems = verify_database(temporary, required)
        if problems:
            raise BackupError("the copy did not verify: " + "; ".join(problems))
        final = _publish(temporary, folder, source.stem, stamp)
    finally:
        for leftover in (temporary, *(Path(f"{temporary}{s}") for s in SIDE_FILES)):
            leftover.unlink(missing_ok=True)
    with _connect(source, read_only=True) as connection:
        source_journal = connection.execute("PRAGMA journal_mode").fetchone()[0]
    manifest = {
        "format": MANIFEST_FORMAT,
        "created_at": now.isoformat(timespec="seconds"),
        "backup_file": final.name,
        "source_database": str(source),
        "source_journal_mode": source_journal,
        "method": "sqlite3 online backup API",
        "size_bytes": final.stat().st_size,
        "sha256": sha256_of(final),
        "schema_sha256": schema_fingerprint(final),
        "tables": table_counts(final),
        "integrity_check": "ok",
        "sqlite_version": sqlite3.sqlite_version,
        "application_version": __version__,
    }
    if reason is not None:
        manifest["reason"] = reason
    manifest_file = manifest_path_for(final)
    with manifest_file.open("x", encoding="utf-8") as handle:  # "x": never overwrite
        json.dump(manifest, handle, indent=2)
        handle.write("\n")
    _restrict(manifest_file, FILE_MODE)
    return Backup(final, manifest_file, manifest)


def verify_backup(backup: Path) -> list[str]:
    """Problems with a backup file and its manifest; empty when it can be restored."""
    if not backup.is_file():
        return [f"no backup file at {backup}"]
    manifest_file = manifest_path_for(backup)
    if not manifest_file.is_file():
        return [f"no manifest ({manifest_file.name}): cannot confirm the file is intact"]
    try:
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ["the manifest is not readable JSON"]
    if manifest.get("sha256") != sha256_of(backup):
        return ["checksum mismatch: the backup file was changed or damaged after it was made"]
    return verify_database(backup, backup_tables(backup))


def backup_tables(backup: Path) -> frozenset[str] | None:
    """The tables a backup was made with, from its manifest (None: unknown).

    An older backup lacks tables a later release added; they are created when that
    release starts. So a backup, and a copy restored from it, are judged by the tables
    they were made with, whichever release restores them (a rollback restores with the
    older release, a recovery may use the newer one)."""
    try:
        listed = json.loads(manifest_path_for(backup).read_text(encoding="utf-8")).get("tables")
    except (OSError, ValueError, AttributeError):
        return None
    return frozenset(listed) & application_tables() if isinstance(listed, dict) else None


def restore_to_new(backup: Path, target: Path) -> dict[str, int]:
    """Restore ``backup`` into the new database file ``target`` (which must not exist).

    Returns the restored table row counts. Never touches any other database.

    Raises:
        BackupError: the backup does not verify, or ``target`` already exists.
    """
    problems = verify_backup(backup)
    if problems:
        raise BackupError("the backup cannot be restored: " + "; ".join(problems))
    if target.exists() or any(Path(f"{target}{s}").exists() for s in SIDE_FILES):
        raise BackupError(f"{target} already exists: restore into a new file")
    target.parent.mkdir(parents=True, exist_ok=True)
    _copy(backup, target)
    _restrict(target, FILE_MODE)
    problems = verify_database(target, backup_tables(backup))
    if problems:
        target.unlink(missing_ok=True)
        raise BackupError("the restored copy did not verify: " + "; ".join(problems))
    return table_counts(target)


def replace_database(backup: Path, active: Path, *, safety_folder: Path) -> Backup | None:
    """Replace the database file ``active`` with ``backup``: destructive.

    The caller must have obtained explicit confirmation, and the application must not be
    running. The current ``active`` file (if any) is backed up into ``safety_folder``
    first, and that backup is returned.

    Raises:
        BackupError: the backup does not verify, or the safety backup failed. Nothing was
            replaced.
    """
    problems = verify_backup(backup)
    if problems:
        raise BackupError("the backup cannot be restored: " + "; ".join(problems))
    # The active database as it is, whichever release's schema it has.
    safety = (
        create_backup(active, safety_folder, reason="before restore", as_found=True)
        if active.is_file()
        else None
    )
    staged = active.with_name(f".{active.name}.restoring-{uuid.uuid4().hex}")
    try:
        _copy(backup, staged)
        _restrict(staged, FILE_MODE)
        problems = verify_database(staged, backup_tables(backup))
        if problems:
            raise BackupError("the restored copy did not verify: " + "; ".join(problems))
        for suffix in SIDE_FILES:  # the old file's log must not be applied to the new one
            Path(f"{active}{suffix}").unlink(missing_ok=True)
        os.replace(staged, active)
    finally:
        for leftover in (staged, *(Path(f"{staged}{s}") for s in SIDE_FILES)):
            leftover.unlink(missing_ok=True)
    return safety


def prune(folder: Path, stem: str, keep: int) -> list[Path]:
    """Delete all but the newest ``keep`` backups of the database named ``stem``, with
    their manifests. Only files named by ``create_backup`` are considered."""
    import re

    pattern = re.compile(rf"^{re.escape(stem)}-\d{{8}}T\d{{6}}Z(-\d+)?\.db$")
    backups = sorted(
        (path for path in folder.glob(f"{stem}-*.db") if pattern.match(path.name)),
        key=lambda path: (path.name[: len(stem) + 17], path.stat().st_mtime),
    )
    removed = backups[:-keep] if keep > 0 else backups
    for path in removed:
        for leftover in (path, manifest_path_for(path), *(Path(f"{path}{s}") for s in SIDE_FILES)):
            leftover.unlink(missing_ok=True)
    return removed
