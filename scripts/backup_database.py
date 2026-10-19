"""Back up the SQLite database while the application keeps running (Stage 13).

Usage (from the project root, or in the backend container):

    python -m scripts.backup_database                   # to data/backups/, keep 14
    python -m scripts.backup_database --keep 30
    python -m scripts.backup_database --output-dir /mnt/offsite --database-url sqlite:///x.db

    docker compose exec backend python -m scripts.backup_database   # in the deployment

What it does:

1. Copies the database with SQLite's online backup API, the same as
   ``scripts.import_faqs --backup``: a consistent snapshot, including everything in the
   write-ahead log, taken while the application keeps reading and writing. (Copying the
   ``.db`` file with ``cp`` while the application runs is not safe: the newest
   transactions may still be in the ``-wal`` file.)
2. Checks the copy: ``PRAGMA integrity_check`` and ``PRAGMA foreign_key_check``. A copy
   that fails is deleted and the script exits with 1.
3. Keeps the newest ``--keep`` backups of this database in the output folder and
   deletes older ones (only files named ``<database>-<UTC time>.db``).

Prints paths, sizes and counts only, never content. The backups contain customer data
in clear: copy them off the server to encrypted storage (docs/sqlite-review.md,
"Backups"). Exit codes: 0 backed up and verified, 1 the copy failed verification,
2 nothing to back up (an in-memory or missing database).
"""

import argparse
import re
import sqlite3
import sys
import time
from collections.abc import Sequence
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:  # allow `python scripts/backup_database.py`
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.import_faqs import BACKUP_DIR, backup_database  # noqa: E402

DEFAULT_KEEP = 14
BACKUP_NAME = re.compile(r"^(?P<stem>.+)-\d{8}T\d{6}Z\.db$")


def verify(path: Path) -> list[str]:
    """Problems found in a backup file; empty when it is sound."""
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        problems = []
        integrity = connection.execute("PRAGMA integrity_check").fetchall()
        if integrity != [("ok",)]:
            problems.append(f"integrity_check: {len(integrity)} problem(s)")
        broken = connection.execute("PRAGMA foreign_key_check").fetchall()
        if broken:
            problems.append(f"foreign_key_check: {len(broken)} row(s) without their parent")
        return problems
    finally:
        connection.close()


def prune(folder: Path, stem: str, keep: int) -> list[Path]:
    """Delete all but the newest ``keep`` backups of the database named ``stem``."""
    backups = sorted(
        path
        for path in folder.glob(f"{stem}-*.db")
        if (match := BACKUP_NAME.match(path.name)) and match["stem"] == stem
    )
    removed = backups[:-keep] if keep > 0 else backups
    for path in removed:
        for leftover in (path, Path(f"{path}-wal"), Path(f"{path}-shm"), Path(f"{path}-journal")):
            leftover.unlink(missing_ok=True)
    return removed


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--database-url", help="Default: DATABASE_URL (environment or .env).")
    parser.add_argument("--output-dir", type=Path, default=BACKUP_DIR)
    parser.add_argument("--keep", type=int, default=DEFAULT_KEEP, help="Backups to keep.")
    args = parser.parse_args(argv)
    if args.keep < 1:
        parser.error("--keep must be at least 1")
    database_url = args.database_url
    if database_url is None:
        from app.core.config import get_settings

        database_url = get_settings().database_url

    started = time.perf_counter()
    target = backup_database(database_url, args.output_dir)
    if target is None:
        print("Nothing to back up: the database is in memory or does not exist yet.")
        return 2
    problems = verify(target)
    if problems:
        target.unlink()
        print(f"Backup FAILED verification and was deleted: {'; '.join(problems)}")
        return 1
    stem = BACKUP_NAME.match(target.name)["stem"]  # type: ignore[index]
    removed = prune(args.output_dir, stem, args.keep)
    print(
        f"Backed up to {target} ({target.stat().st_size / 1_000_000:.1f} MB, verified) in "
        f"{time.perf_counter() - started:.1f} s; {len(removed)} older backup(s) removed, "
        f"keeping {args.keep}."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
