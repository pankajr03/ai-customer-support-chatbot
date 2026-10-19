"""Verify a database backup, restore it into a separate test database, or (explicitly)
replace the active database with it (Stage 13).

Usage (from the project root, or in a one-off backend container):

    # 1. Check a backup: manifest checksum, integrity, foreign keys, every table present
    python -m scripts.restore_database data/backups/conversations-20261019T103339Z.db --verify-only

    # 2. Test restore (the default): into a NEW database file, never the active one
    python -m scripts.restore_database data/backups/conversations-20261019T103339Z.db
    python -m scripts.restore_database BACKUP --to /tmp/restore-test/conversations.db

    # 3. Replace the active database: destructive. Stop the backend first.
    python -m scripts.restore_database BACKUP --replace-active --confirm conversations.db

A test restore copies the backup (SQLite's backup API) into a file that must not exist
yet (by default a new temporary folder), verifies the copy, and compares its row counts
with the backup's manifest. Point a separate instance at it to inspect it
(``DATABASE_URL=sqlite:///<path>``).

Replacing the active database (``DATABASE_URL``) happens only with ``--replace-active``
**and** ``--confirm`` naming the active database file exactly. Before anything is
replaced, the current database is backed up (a safety backup, in ``--safety-dir``,
default ``data/backups``); the restored copy is verified beside it and then swapped in
with one rename, after removing the old file's ``-wal``/``-shm`` files. The application
must not be running: stop the backend first (docs/backup-and-restore.md).

Exit codes: 0 done; 1 the backup or the restored copy failed verification (nothing
replaced); 2 refused (missing or wrong confirmation, target exists, no active database).
"""

import argparse
import json
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:  # allow `python scripts/restore_database.py`
    sys.path.insert(0, str(PROJECT_ROOT))

from app.database.backups import (  # noqa: E402
    BackupError,
    manifest_path_for,
    replace_database,
    restore_to_new,
    verify_backup,
)
from app.database.connection import resolve_sqlite_url  # noqa: E402
from scripts.import_faqs import BACKUP_DIR  # noqa: E402

OK, FAILED, REFUSED = 0, 1, 2


def _manifest(backup: Path) -> dict:
    return json.loads(manifest_path_for(backup).read_text(encoding="utf-8"))


def _active_database(database_url: str | None) -> Path | None:
    if database_url is None:
        from app.core.config import get_settings

        database_url = get_settings().database_url
    url = resolve_sqlite_url(database_url)
    if url.database in (None, "", ":memory:"):
        return None
    return Path(url.database)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("backup", type=Path, help="The backup file (<database>-<time>.db).")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--verify-only", action="store_true", help="Only check the backup.")
    mode.add_argument("--to", type=Path, help="Test restore into this NEW file.")
    mode.add_argument(
        "--replace-active",
        action="store_true",
        help="DESTRUCTIVE: replace the active database (needs --confirm).",
    )
    parser.add_argument("--confirm", help="The active database's file name, to confirm.")
    parser.add_argument("--database-url", help="Default: DATABASE_URL (environment or .env).")
    parser.add_argument("--safety-dir", type=Path, default=BACKUP_DIR)
    args = parser.parse_args(argv)

    problems = verify_backup(args.backup)
    if problems:
        print(f"Backup NOT usable: {'; '.join(problems)}")
        return FAILED
    manifest = _manifest(args.backup)
    print(
        f"Backup verified: {args.backup.name}, made {manifest['created_at']}, "
        f"{len(manifest['tables'])} tables, {sum(manifest['tables'].values())} rows, "
        f"checksum matches the manifest."
    )
    if args.verify_only:
        return OK

    if not args.replace_active:
        target = args.to or Path(tempfile.mkdtemp(prefix="restore-test-")) / args.backup.name
        try:
            counts = restore_to_new(args.backup, target)
        except BackupError as exc:
            print(f"Test restore FAILED: {exc}")
            return REFUSED if "already exists" in str(exc) else FAILED
        if counts != manifest["tables"]:
            print(f"Test restore FAILED: row counts differ from the manifest: {counts}")
            return FAILED
        print(
            f"Restored into {target} (a separate database; the active one was not touched). "
            "Row counts match the manifest. Inspect it with "
            f"DATABASE_URL=sqlite:///{target.as_posix()}"
        )
        return OK

    active = _active_database(args.database_url)
    if active is None:
        print("Refused: the active database is in memory; there is nothing to replace.")
        return REFUSED
    if args.confirm != active.name:
        print(
            f"Refused: replacing {active} destroys its current contents. Stop the backend, "
            f"then repeat with --confirm {active.name} to confirm. (A safety backup of the "
            "current database is taken first.)"
        )
        return REFUSED
    try:
        safety = replace_database(args.backup, active, safety_folder=args.safety_dir)
    except BackupError as exc:
        print(f"Restore FAILED, nothing replaced: {exc}")
        return FAILED
    print(
        f"Replaced {active} with {args.backup.name}. "
        + (
            f"The previous database was saved as {safety.path}."
            if safety
            else "There was no previous database file."
        )
        + " Start the backend again."
    )
    return OK


if __name__ == "__main__":
    sys.exit(main())
