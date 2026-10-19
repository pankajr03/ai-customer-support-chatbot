"""Back up the SQLite database while the application keeps running (Stage 13).

Usage (from the project root, or in the backend container):

    python -m scripts.backup_database                       # to data/backups/, keep 14
    python -m scripts.backup_database --keep 30
    python -m scripts.backup_database --output-dir /srv/backups --database-url sqlite:///x.db

    docker compose exec backend python -m scripts.backup_database    # in the deployment

Each run makes a new, timestamped backup (``<database>-<UTC time>.db``; never
overwriting an existing one) with SQLite's online backup API: a consistent snapshot
taken while the application keeps reading and writing. The copy is verified
(``integrity_check``, ``foreign_key_check``, every table present) before it is kept, and
a manifest (``<backup>.manifest.json``: checksum, row counts, schema fingerprint,
versions) is written beside it. Files are readable by their owner only. Then all but the
newest ``--keep`` backups of this database are deleted, with their manifests.

Restore with ``python -m scripts.restore_database``. Commands, retention and encryption:
docs/backup-and-restore.md. Prints paths, sizes and counts only, never content.

Exit codes: 0 backed up and verified; 1 the copy failed verification (it was deleted);
2 nothing to back up (an in-memory or missing database).
"""

import argparse
import sys
import time
from collections.abc import Sequence
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:  # allow `python scripts/backup_database.py`
    sys.path.insert(0, str(PROJECT_ROOT))

from app.database.backups import BackupError, create_backup, prune  # noqa: E402
from app.database.connection import resolve_sqlite_url  # noqa: E402
from scripts.import_faqs import BACKUP_DIR  # noqa: E402

DEFAULT_KEEP = 14


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--database-url", help="Default: DATABASE_URL (environment or .env).")
    parser.add_argument("--output-dir", type=Path, default=BACKUP_DIR)
    parser.add_argument(
        "--keep", type=int, default=DEFAULT_KEEP, help=f"Backups to keep (default {DEFAULT_KEEP})."
    )
    args = parser.parse_args(argv)
    if args.keep < 1:
        parser.error("--keep must be at least 1")
    database_url = args.database_url
    if database_url is None:
        from app.core.config import get_settings

        database_url = get_settings().database_url

    url = resolve_sqlite_url(database_url)
    if url.database in (None, "", ":memory:") or not Path(url.database).is_file():
        print("Nothing to back up: the database is in memory or does not exist yet.")
        return 2
    source = Path(url.database)
    started = time.perf_counter()
    try:
        backup = create_backup(source, args.output_dir)
    except BackupError as exc:
        print(f"Backup FAILED: {exc}")
        return 1
    removed = prune(args.output_dir, source.stem, args.keep)
    rows = sum(backup.manifest["tables"].values())
    print(
        f"Backed up to {backup.path} ({backup.manifest['size_bytes'] / 1_000_000:.1f} MB, "
        f"{len(backup.manifest['tables'])} tables, {rows} rows, verified) in "
        f"{time.perf_counter() - started:.1f} s; manifest {backup.manifest_path.name}; "
        f"{len(removed)} older backup(s) removed, keeping {args.keep}."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
