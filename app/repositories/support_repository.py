"""Store and load support tickets, customer contacts and ticket events (Stage 10).

The repository takes and returns the validated records from ``app.models.support``; ORM
rows never leave this module. Every method runs in its own short transaction, so a
ticket, its contact and its first event are stored together or not at all.

Contacts are not duplicated unnecessarily: the same details (email, name, phone and
preference, normalised) submitted again reuse the stored contact, whose consent
timestamp moves to the new consent. Different details create a new contact instead of
overwriting the old one, because submissions are not authenticated: a request must
never change the details staff use for someone else's earlier request.

A ticket links to its conversation by ID only. Whether the caller may link that
conversation (its access token) is checked by the service layer before this is called;
here the conversation only has to exist.

Errors:
- ``TicketNotFoundError``: no ticket with that ID or reference number.
- ``ConversationLinkError``: the conversation to link does not exist.
- ``InvalidStatusTransitionError``: the status change is not allowed
  (``ALLOWED_TRANSITIONS``).
- ``SupportStoreError``: the database failed. Logged with the error type only, never
  contact details or SQL parameters.
"""

import hashlib
import json
import logging
import secrets
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.database.connection import Database
from app.database.models import (
    ConversationSessionRow,
    CustomerContactRow,
    SupportTicketRow,
    TicketEventRow,
)
from app.models.conversation import new_id, utc_now
from app.models.escalation import EscalationReason
from app.models.support import (
    ALLOWED_TRANSITIONS,
    REFERENCE_ALPHABET,
    REFERENCE_PREFIXES,
    CustomerContact,
    SupportRequestCreate,
    SupportTicket,
    TicketEvent,
    TicketEventType,
    TicketPriority,
    TicketStatus,
    TicketType,
)

logger = logging.getLogger(__name__)

# Reference numbers carry 40 random bits (8 base32 characters); a collision is retried.
_REFERENCE_ATTEMPTS = 5


class SupportRepositoryError(Exception):
    """Base class for support storage errors."""


class TicketNotFoundError(SupportRepositoryError):
    pass


class ConversationLinkError(SupportRepositoryError):
    """The conversation a ticket should link to does not exist."""


class InvalidStatusTransitionError(SupportRepositoryError):
    def __init__(self, current: TicketStatus, requested: TicketStatus) -> None:
        super().__init__(f"cannot change a {current} ticket to {requested}")
        self.current = current
        self.requested = requested


class SupportStoreError(SupportRepositoryError):
    """The database failed; details are in the logs (error type only)."""


def new_reference_number(ticket_type: TicketType) -> str:
    """A random reference such as ``SUP-7K3M-9QX2`` (``secrets``, not ``random``)."""
    code = "".join(secrets.choice(REFERENCE_ALPHABET) for _ in range(8))
    return f"{REFERENCE_PREFIXES[ticket_type]}-{code[:4]}-{code[4:]}"


def contact_identity(request: SupportRequestCreate) -> str:
    """SHA-256 (hex) of the normalised contact details, the key for reusing a contact.

    Email is compared case-insensitively, the name ignoring case and spacing, and the
    phone number by its digits (and a leading +) only.
    """
    phone = request.phone
    canonical = json.dumps(
        {
            "v": 1,
            "email": request.email.lower(),
            "name": " ".join(request.name.casefold().split()),
            "phone": None if phone is None else _phone_digits(phone),
            "preference": request.contact_preference.value,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _phone_digits(phone: str) -> str:
    digits = "".join(c for c in phone if c.isdigit())
    return ("+" if phone.lstrip().startswith("+") else "") + digits


class SupportRepository:
    """Support tickets and the contacts and events that belong to them."""

    def __init__(self, database: Database) -> None:
        self._db = database

    # --- Tickets ------------------------------------------------------------------

    def create_ticket(
        self,
        request: SupportRequestCreate,
        *,
        session_id: uuid.UUID | None = None,
        escalation_reason: EscalationReason | None = None,
        priority: TicketPriority = TicketPriority.NORMAL,
        now: datetime | None = None,
    ) -> SupportTicket:
        """Store a ticket for a validated request, with its contact and ``created``
        event, in one transaction.

        Raises:
            ConversationLinkError: ``session_id`` names no stored conversation.
            SupportStoreError: the database failed (nothing is stored).
        """
        created = now or utc_now()
        with self._write("create_ticket") as db:
            if session_id is not None and db.get(ConversationSessionRow, session_id) is None:
                raise ConversationLinkError("the conversation does not exist")
            contact = self._reuse_or_add_contact(db, request, created)
            ticket = SupportTicketRow(
                ticket_id=new_id(),
                reference_number=self._unused_reference(db, request.ticket_type),
                session_id=session_id,
                contact_id=contact.contact_id,
                ticket_type=request.ticket_type.value,
                escalation_reason=escalation_reason.value if escalation_reason else None,
                subject=request.subject,
                description=request.message,
                status=TicketStatus.OPEN.value,
                priority=priority.value,
                created_at=created,
                updated_at=created,
            )
            db.add(ticket)
            db.add(
                TicketEventRow(
                    event_id=new_id(),
                    ticket_id=ticket.ticket_id,
                    sequence=1,
                    event_type=TicketEventType.CREATED.value,
                    previous_status=None,
                    new_status=TicketStatus.OPEN.value,
                    created_at=created,
                )
            )
            db.flush()
            return _to_ticket(ticket)

    def get_ticket(self, ticket_id: uuid.UUID) -> SupportTicket:
        with self._read("get_ticket") as db:
            row = db.get(SupportTicketRow, ticket_id)
            if row is None:
                raise TicketNotFoundError("no such ticket")
            return _to_ticket(row)

    def get_ticket_by_reference(self, reference_number: str) -> SupportTicket:
        with self._read("get_ticket_by_reference") as db:
            row = db.scalar(
                select(SupportTicketRow).where(
                    SupportTicketRow.reference_number == reference_number.strip().upper()
                )
            )
            if row is None:
                raise TicketNotFoundError("no such ticket")
            return _to_ticket(row)

    def change_status(
        self, ticket_id: uuid.UUID, new_status: TicketStatus, *, now: datetime | None = None
    ) -> SupportTicket:
        """Move a ticket to a new status and record a ``status_changed`` event.

        ``resolved_at`` is set on resolving (and cleared on reopening); ``closed_at`` on
        closing.

        Raises:
            TicketNotFoundError: no such ticket.
            InvalidStatusTransitionError: not allowed from the current status (this
                includes "changing" to the same status).
        """
        with self._write("change_status") as db:
            row = db.get(SupportTicketRow, ticket_id)
            if row is None:
                raise TicketNotFoundError("no such ticket")
            current = TicketStatus(row.status)
            if new_status not in ALLOWED_TRANSITIONS[current]:
                raise InvalidStatusTransitionError(current, new_status)
            changed = max(now or utc_now(), row.updated_at)
            row.status = new_status.value
            row.updated_at = changed
            if new_status is TicketStatus.RESOLVED:
                row.resolved_at = changed
            elif new_status in (TicketStatus.OPEN, TicketStatus.IN_PROGRESS):
                row.resolved_at = None
            if new_status is TicketStatus.CLOSED:
                row.closed_at = changed
            last = db.scalar(
                select(func.max(TicketEventRow.sequence)).where(
                    TicketEventRow.ticket_id == ticket_id
                )
            )
            db.add(
                TicketEventRow(
                    event_id=new_id(),
                    ticket_id=ticket_id,
                    sequence=(last or 0) + 1,
                    event_type=TicketEventType.STATUS_CHANGED.value,
                    previous_status=current.value,
                    new_status=new_status.value,
                    created_at=changed,
                )
            )
            db.flush()
            return _to_ticket(row)

    def list_events(self, ticket_id: uuid.UUID) -> list[TicketEvent]:
        """A ticket's history, oldest first."""
        with self._read("list_events") as db:
            rows = db.scalars(
                select(TicketEventRow)
                .where(TicketEventRow.ticket_id == ticket_id)
                .order_by(TicketEventRow.sequence)
            ).all()
            return [_to_event(row) for row in rows]

    # --- Contacts (staff use only; never exposed by public APIs) ------------------

    def get_contact(self, contact_id: uuid.UUID) -> CustomerContact | None:
        with self._read("get_contact") as db:
            row = db.get(CustomerContactRow, contact_id)
            return _to_contact(row) if row is not None else None

    # --- Internals ----------------------------------------------------------------

    @staticmethod
    def _reuse_or_add_contact(
        db: Session, request: SupportRequestCreate, now: datetime
    ) -> CustomerContactRow:
        identity = contact_identity(request)
        row = db.scalar(
            select(CustomerContactRow).where(CustomerContactRow.identity_hash == identity)
        )
        if row is not None:
            row.consent_timestamp = max(now, row.consent_timestamp)  # the latest consent
            row.updated_at = max(now, row.updated_at)
            return row
        row = CustomerContactRow(
            contact_id=new_id(),
            name=request.name,
            email=request.email,
            phone=request.phone,
            contact_preference=request.contact_preference.value,
            consent_given=request.consent_given,
            consent_timestamp=now,
            created_at=now,
            updated_at=now,
            identity_hash=identity,
        )
        db.add(row)
        return row

    @staticmethod
    def _unused_reference(db: Session, ticket_type: TicketType) -> str:
        # The write lock is held (BEGIN IMMEDIATE), so a free reference stays free until
        # commit; the unique constraint is the backstop.
        for _ in range(_REFERENCE_ATTEMPTS):
            reference = new_reference_number(ticket_type)
            taken = db.scalar(
                select(SupportTicketRow.ticket_id).where(
                    SupportTicketRow.reference_number == reference
                )
            )
            if taken is None:
                return reference
        raise SupportStoreError("could not allocate a reference number")

    @contextmanager
    def _read(self, operation: str) -> Iterator[Session]:
        with _store_errors(operation), self._db.read() as db:
            yield db

    @contextmanager
    def _write(self, operation: str) -> Iterator[Session]:
        with _store_errors(operation), self._db.write() as db:
            yield db


@contextmanager
def _store_errors(operation: str) -> Iterator[None]:
    """Database failures and invalid stored rows become ``SupportStoreError``; the
    exception text (which can contain contact details) is never logged."""
    try:
        yield
    except SQLAlchemyError as exc:
        logger.error("Support store %s failed (%s)", operation, type(exc).__name__)
        raise SupportStoreError(f"support storage is unavailable ({operation})") from exc
    except ValidationError as exc:
        logger.error("Invalid support row in %s (%d errors)", operation, exc.error_count())
        raise SupportStoreError("stored support data is invalid") from None


# --- Conversions (rows are re-validated on the way out) -------------------------------


def _to_contact(row: CustomerContactRow) -> CustomerContact:
    return CustomerContact(
        contact_id=row.contact_id,
        name=row.name,
        email=row.email,
        phone=row.phone,
        contact_preference=row.contact_preference,
        consent_given=row.consent_given,
        consent_timestamp=row.consent_timestamp,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _to_ticket(row: SupportTicketRow) -> SupportTicket:
    return SupportTicket(
        ticket_id=row.ticket_id,
        reference_number=row.reference_number,
        session_id=row.session_id,
        contact_id=row.contact_id,
        ticket_type=row.ticket_type,
        escalation_reason=row.escalation_reason,
        subject=row.subject,
        description=row.description,
        status=row.status,
        priority=row.priority,
        created_at=row.created_at,
        updated_at=row.updated_at,
        resolved_at=row.resolved_at,
        closed_at=row.closed_at,
    )


def _to_event(row: TicketEventRow) -> TicketEvent:
    return TicketEvent(
        event_id=row.event_id,
        ticket_id=row.ticket_id,
        sequence=row.sequence,
        event_type=row.event_type,
        previous_status=row.previous_status,
        new_status=row.new_status,
        created_at=row.created_at,
    )
