"""Import FAQs from a JSON file into the database (Stage 11).

Usage (from the project root, virtual environment active):

    python -m scripts.import_faqs                      # data/faqs.json -> DATABASE_URL
    python -m scripts.import_faqs --dry-run            # report only, write nothing
    python -m scripts.import_faqs --backup             # copy the database first
    python -m scripts.import_faqs --file data/client_faqs.json

What it does, in one transaction:

1. Reads and validates the whole file first (``FAQRepository.from_json_file``): valid
   UTF-8 JSON, every record a valid FAQ, no duplicate IDs. Any problem stops the import
   before the database is touched, and is reported with the record and field.
2. Adds every FAQ whose ID is not in the database yet, keeping its ID, category,
   keywords (tags) and active flag. The JSON file has no timestamps, so imported FAQs
   are created "now" (aware UTC) with no staff author.
3. Never overwrites an FAQ that already exists: identical ones are counted as unchanged;
   ones that differ (usually because staff edited them) keep the database version and
   are listed, so you can review them in the admin dashboard.

Running it again therefore changes nothing. The API also seeds an empty FAQ table from
``FAQ_DATA_PATH`` once at startup; after that, only this script reads the file.

The running API keeps its own copy of the catalogue: restart it after an import that
added FAQs (or add them through the admin dashboard instead).

Backups: ``--backup`` copies the SQLite database with SQLite's online backup API (safe
while the API is running) to ``data/backups/<name>-<UTC time>.db`` before importing.
Without it, stop the API and copy the database file yourself, together with any
``-wal`` and ``-shm`` files next to it. The JSON file is only read, never changed.

Exit codes: 0 success (also when nothing was new), 1 invalid file (nothing written),
2 database or configuration problem (nothing written).
"""

import argparse
import sqlite3
import sys
from collections.abc import Sequence
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:  # allow `python scripts/import_faqs.py`
    sys.path.insert(0, str(PROJECT_ROOT))

from pydantic import ValidationError  # noqa: E402
from pydantic_settings import SettingsError  # noqa: E402

from app.core.config import (  # noqa: E402
    Settings,
    describe_settings_error,
    describe_settings_source_error,
)
from app.database.backups import BackupError, create_backup  # noqa: E402
from app.database.connection import Database, DatabaseError, resolve_sqlite_url  # noqa: E402
from app.database.faq_repository import FAQDataError, FAQRepository  # noqa: E402
from app.repositories.faq_repository import DatabaseFAQRepository, FAQStoreError  # noqa: E402

EXIT_OK = 0
EXIT_INVALID_FILE = 1
EXIT_DATABASE = 2
BACKUP_DIR = PROJECT_ROOT / "data" / "backups"
MAX_LISTED_IDS = 20


def backup_database(database_url: str, backup_dir: Path | None = None) -> Path | None:
    """Back up a SQLite file database (to ``BACKUP_DIR`` by default) with
    ``app.database.backups.create_backup``: online backup API, verified, never
    overwriting, with a manifest. None for in-memory or not-yet-created databases.

    Raises:
        BackupError: the copy did not verify.
    """
    backup_dir = backup_dir or BACKUP_DIR
    url = resolve_sqlite_url(database_url)
    if url.database in (None, "", ":memory:") or not Path(url.database).is_file():
        return None
    return create_backup(Path(url.database), backup_dir).path


def _ids(ids: Sequence[str]) -> str:
    shown = ", ".join(ids[:MAX_LISTED_IDS])
    return shown + (f", ... (+{len(ids) - MAX_LISTED_IDS})" if len(ids) > MAX_LISTED_IDS else "")


def main(argv: Sequence[str] | None = None, *, settings: Settings | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import FAQs from JSON into the database.")
    parser.add_argument("--file", type=Path, help="FAQ JSON file (default: FAQ_DATA_PATH).")
    parser.add_argument(
        "--database-url", help="SQLite URL (default: DATABASE_URL, e.g. from .env)."
    )
    parser.add_argument("--dry-run", action="store_true", help="Report only; write nothing.")
    parser.add_argument(
        "--backup", action="store_true", help="Back up the database file before importing."
    )
    args = parser.parse_args(argv)

    if settings is None:
        try:
            settings = Settings()
        except ValidationError as exc:
            print(describe_settings_error(exc), file=sys.stderr)
            return EXIT_DATABASE
        except SettingsError as exc:
            print(describe_settings_source_error(exc), file=sys.stderr)
            return EXIT_DATABASE
    path = args.file or settings.faq_data_path
    database_url = args.database_url or settings.database_url

    try:
        faqs = FAQRepository.from_json_file(path).list_all()
    except FAQDataError as exc:
        print(f"Import stopped, nothing written. The FAQ file is invalid:\n{exc}", file=sys.stderr)
        return EXIT_INVALID_FILE
    print(f"Read {len(faqs)} valid FAQs from {path}.")

    try:
        if args.backup and not args.dry_run:
            target = backup_database(database_url)
            print(f"Backup written to {target}." if target else "No database file to back up yet.")
        database = Database(database_url)
        try:
            database.init_schema()
            report = DatabaseFAQRepository(database).import_faqs(faqs, dry_run=args.dry_run)
        finally:
            database.dispose()
    except (BackupError, DatabaseError, FAQStoreError, ValueError, OSError, sqlite3.Error) as exc:
        print(f"Import failed, nothing written ({type(exc).__name__}): {exc}", file=sys.stderr)
        return EXIT_DATABASE

    prefix = "Dry run (nothing written): would import" if args.dry_run else "Imported"
    print(f"{prefix} {len(report.created)} new FAQ(s).")
    if report.created:
        print(f"  new: {_ids(report.created)}")
    print(f"Already in the database and identical: {len(report.unchanged)}.")
    if report.kept_database_version:
        print(
            f"Already in the database with different content, database version kept: "
            f"{len(report.kept_database_version)}.\n"
            f"  review in the admin dashboard: {_ids(report.kept_database_version)}"
        )
    if report.created and not args.dry_run:
        print("Restart the API so it serves the new FAQs.")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
