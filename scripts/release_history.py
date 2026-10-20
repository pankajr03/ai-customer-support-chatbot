"""Show which releases ran against the database, and the backups taken before upgrades.

Usage (Docker deployment, from deploy/):

    docker compose exec backend python -m scripts.release_history
    docker compose run --rm --no-deps backend python -m scripts.release_history   # backend stopped

Prints the ``app_releases`` table (when each release first started against this
database: version, revision, schema fingerprint) and the backups in the backups folder
whose manifest says they were taken **before a schema upgrade**, with their schema
fingerprint. To roll back past a schema change, restore the pre-upgrade backup whose
schema matches the release you go back to (docs/deployment-rollback.md).

Reads only. Prints no customer data.
"""

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:  # allow `python scripts/release_history.py`
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import inspect, select  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.database.backups import MANIFEST_SUFFIX  # noqa: E402
from app.database.connection import Database  # noqa: E402
from app.database.models import AppReleaseRow  # noqa: E402
from scripts.import_faqs import BACKUP_DIR  # noqa: E402


def releases(database: Database) -> list[AppReleaseRow]:
    with database.engine.connect() as connection:
        if "app_releases" not in inspect(connection).get_table_names():
            return []  # written by a release from before the history existed
    with database.read() as db:
        rows = list(db.scalars(select(AppReleaseRow).order_by(AppReleaseRow.id)))
        db.expunge_all()
        return rows


def upgrade_backups(folder: Path) -> list[dict]:
    found = []
    for manifest in sorted(folder.glob(f"*{MANIFEST_SUFFIX}")):
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if data.get("reason") == "before schema upgrade":
            found.append(data)
    return found


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--database-url", help="Default: DATABASE_URL (environment or .env).")
    parser.add_argument("--backup-dir", type=Path, default=BACKUP_DIR)
    args = parser.parse_args(argv)

    database = Database(args.database_url or get_settings().database_url)
    try:
        current = database.schema_fingerprint()
        rows = releases(database)
    finally:
        database.dispose()
    print(f"Current schema: {current[:12]}")
    print("\nReleases that ran against this database (oldest first):")
    if not rows:
        print("  none recorded (the history starts with the first release that records it)")
    for row in rows:
        marker = "  <- current schema" if row.schema_sha256 == current else ""
        revision = row.revision or "-"
        print(f"  {row.started_at:%Y-%m-%d %H:%M} UTC  version {row.app_version:<8} "
              f"revision {revision:<12} schema {row.schema_sha256[:12]}{marker}")  # fmt: skip
    print(f"\nBackups taken before a schema upgrade ({args.backup_dir}):")
    backups = upgrade_backups(args.backup_dir)
    if not backups:
        print("  none")
    for data in backups:
        print(f"  {data['backup_file']}  schema {str(data.get('schema_sha256', '?'))[:12]}  "
              f"(made by version {data.get('application_version', '?')})")  # fmt: skip
    return 0


if __name__ == "__main__":
    sys.exit(main())
