"""Remove personal data from closed support tickets (retention and erasure requests).

Usage (from the project root, virtual environment active):

    python -m scripts.anonymize_support_data --retention               # closed > retention
    python -m scripts.anonymize_support_data --retention --dry-run     # only count
    python -m scripts.anonymize_support_data --retention --older-than-days 30
    python -m scripts.anonymize_support_data --reference SUP-7K3M-9QX2 # an erasure request

What anonymizing does (``TicketRepository.anonymize_ticket``): the ticket's subject and
description become "[removed]", its link to the conversation is cleared, and an
``anonymized`` event is recorded. Once no other ticket still holds personal data for its
contact, the contact's name, email and phone are replaced too. The ticket's reference,
type, status, timestamps and events stay, so statistics and the audit trail survive
without personal data.

Only **closed** tickets are anonymized. For an erasure request about an open ticket,
staff close it first (``PATCH /api/v1/support/tickets/{id}/status``), then run this.
``--retention`` uses ``SUPPORT_TICKET_RETENTION_DAYS`` (default 365) unless
``--older-than-days`` is given. The database comes from ``DATABASE_URL`` unless
``--database-url`` is given. Prints counts and references only, never personal data.

There is no background job: run it from the command line or a scheduler.
"""

import argparse
import sys
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:  # allow `python scripts/anonymize_support_data.py`
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import Settings  # noqa: E402
from app.database.connection import Database, DatabaseError  # noqa: E402
from app.models.conversation import utc_now  # noqa: E402
from app.models.support import ActorType  # noqa: E402
from app.repositories.ticket_repository import (  # noqa: E402
    TicketNotFoundError,
    TicketRepository,
    TicketStillActiveError,
    TicketStoreError,
)


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Anonymize closed support tickets.")
    what = parser.add_mutually_exclusive_group(required=True)
    what.add_argument("--retention", action="store_true", help="Closed tickets past retention.")
    what.add_argument("--reference", help="One closed ticket, e.g. SUP-7K3M-9QX2.")
    parser.add_argument("--older-than-days", type=int, help="Override the retention period.")
    parser.add_argument("--dry-run", action="store_true", help="Count; change nothing.")
    parser.add_argument("--database-url", help="Database (default: DATABASE_URL).")
    args = parser.parse_args(argv)
    if args.older_than_days is not None and args.older_than_days < 1:
        parser.error("--older-than-days must be at least 1")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    settings = Settings()
    database = Database(args.database_url or settings.database_url)
    try:
        database.init_schema()
        tickets = TicketRepository(database)
        if args.reference:
            ticket = tickets.get_ticket_by_reference(args.reference)
            if args.dry_run:
                print(f"Would anonymize {ticket.reference_number} ({ticket.status}).")
                return 0
            tickets.anonymize_ticket(ticket.ticket_id, actor_type=ActorType.SYSTEM)
            print(f"Anonymized {ticket.reference_number}.")
            return 0
        days = args.older_than_days or settings.support_ticket_retention_days
        cutoff = utc_now() - timedelta(days=days)
        count = tickets.anonymize_closed_before(cutoff, dry_run=args.dry_run)
        verb = "Would anonymize" if args.dry_run else "Anonymized"
        print(f"{verb} {count} closed ticket(s) closed more than {days} day(s) ago.")
        return 0
    except TicketNotFoundError:
        print("No ticket with that reference number.", file=sys.stderr)
        return 1
    except TicketStillActiveError as exc:
        print(f"Not anonymized: {exc}.", file=sys.stderr)
        return 1
    except (DatabaseError, TicketStoreError) as exc:
        print(f"Anonymization failed: {exc}", file=sys.stderr)
        return 1
    finally:
        database.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
