"""Delete expired conversations and their messages (manual retention cleanup).

Usage (from the project root, virtual environment active):

    python scripts/cleanup_conversations.py                   # delete everything expired
    python scripts/cleanup_conversations.py --dry-run         # only count what would go
    python scripts/cleanup_conversations.py --grace-minutes 60  # keep the last hour's

A conversation is deleted, with all its messages, once its ``expires_at`` (creation time
+ ``CONVERSATION_TTL_MINUTES``) is at least ``--grace-minutes`` in the past. Closed
conversations are deleted the same way. The database comes from ``DATABASE_URL``
(environment / ``.env``) unless ``--database-url`` is given.

The app runs the same cleanup at startup (``CONVERSATION_CLEANUP_ON_STARTUP=true``).
There is no background job: run this from the command line or from the operating
system's scheduler (e.g. Windows Task Scheduler, cron) if conversations must be removed
promptly while the app keeps running.

Safe to run while the app is running: deletions happen in small batches of short
transactions, so chat requests are delayed by at most one batch. Prints counts only,
never conversation content.
"""

import argparse
import sys
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:  # allow `python scripts/cleanup_conversations.py`
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import Settings  # noqa: E402
from app.database.connection import Database, DatabaseError  # noqa: E402
from app.repositories.conversation_repository import (  # noqa: E402
    ConversationRepository,
    ConversationStoreError,
)
from app.services.conversation_service import ConversationService  # noqa: E402


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Delete expired conversations.")
    parser.add_argument(
        "--dry-run", action="store_true", help="Count expired conversations; delete nothing."
    )
    parser.add_argument(
        "--grace-minutes",
        type=int,
        default=0,
        help="Only delete conversations that expired at least this many minutes ago.",
    )
    parser.add_argument(
        "--database-url", help="Database to clean (default: DATABASE_URL from settings)."
    )
    args = parser.parse_args(argv)
    if args.grace_minutes < 0:
        parser.error("--grace-minutes must not be negative")
    return args


def main(argv: Sequence[str] | None = None, settings: Settings | None = None) -> int:
    args = parse_args(argv)
    settings = settings or Settings()
    database = Database(args.database_url or settings.database_url)
    try:
        database.init_schema()
        service = ConversationService.from_settings(settings, ConversationRepository(database))
        count = service.cleanup_expired(
            grace=timedelta(minutes=args.grace_minutes), dry_run=args.dry_run
        )
    except (DatabaseError, ConversationStoreError) as exc:
        print(f"Cleanup failed: {exc}", file=sys.stderr)
        return 1
    finally:
        database.dispose()
    if args.dry_run:
        print(f"{count} expired conversation(s) would be deleted (dry run).")
    else:
        print(f"Deleted {count} expired conversation(s) and their messages.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
