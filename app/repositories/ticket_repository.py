"""Store and load support tickets, customer contacts and ticket events (Stage 10).

The repository takes and returns the validated records from ``app.models.support``; ORM
rows never leave this module and every query goes through the ORM (parameterised). It
holds no business rules about *who* may do what (that is ``TicketWorkflow`` and the
services); it keeps the data consistent.

Transactions: every method runs in its own short transaction. Writes use SQLite's
``BEGIN IMMEDIATE`` (``Database.write``), so the "look up, then insert" steps below
cannot interleave with another writer, in this process or another one.

Creating a ticket (``create_ticket``), in one transaction:

1. **Idempotency.** With a conversation, the request's ``client_request_id`` is looked
   up in that conversation. Found with the same fingerprint (``request_fingerprint``):
   the original ticket is returned (``replayed``). Found with a different one:
   ``IdempotencyKeyConflictError``. Keys are scoped to the conversation, so a key can
   never reveal another conversation's ticket. A unique index backs this up.
2. **Duplicate submissions.** The same request (same fingerprint) already open in the
   same conversation within ``duplicate_window`` returns that ticket (``duplicate``),
   for a customer who pressed Send twice or reloaded the form (new ID, same text).
3. **Store** the contact (reused when identical, see below), the ticket and its
   ``created`` event.

Contacts are not duplicated unnecessarily: identical details (email, name, phone and
preference, normalised) reuse the stored contact, whose consent timestamp moves to the
new consent. Different details create a new contact instead of overwriting the old one,
because submissions are not authenticated.

Reads never return contact details with tickets (``SupportTicket`` holds only the
``contact_id``); ``get_contact`` is for staff tooling. Lists are ordered by
``created_at`` then ``ticket_id``, so pages are stable. ``list_session_tickets`` and
``get_session_ticket`` only ever see one conversation's tickets; authorising that
conversation is the caller's job.

Errors:
- ``TicketNotFoundError``: no ticket with that ID or reference (in that conversation).
- ``ConversationLinkError``: the conversation to link does not exist.
- ``IdempotencyKeyConflictError``: the ``client_request_id`` was used for a different
  request in this conversation.
- ``InvalidStatusTransitionError`` / ``StaleTicketStatusError``: the status change is
  not allowed, or the ticket changed since it was read.
- ``TicketStoreError`` (``TicketStoreBusyError`` when the database is locked): the
  database failed. Logged with the error type only, never contact details or SQL
  parameters.
"""

import hashlib
import json
import logging
import secrets
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta

from pydantic import ValidationError
from sqlalchemy import Select, func, select
from sqlalchemy.exc import IntegrityError, OperationalError, SQLAlchemyError
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
    ACTIVE_STATUSES,
    ALLOWED_TRANSITIONS,
    REFERENCE_ALPHABET,
    REFERENCE_PREFIXES,
    ActorType,
    CustomerContact,
    SupportRequestCreate,
    SupportTicket,
    TicketEvent,
    TicketEventType,
    TicketPriority,
    TicketStatus,
    TicketSubmissionOutcome,
    TicketType,
    request_fingerprint,
)

logger = logging.getLogger(__name__)

# Reference numbers carry 40 random bits (8 base32 characters); a collision is retried.
_REFERENCE_ATTEMPTS = 5
DEFAULT_DUPLICATE_WINDOW = timedelta(hours=24)
MAX_PAGE_SIZE = 100


# --- Errors ---------------------------------------------------------------------------


class TicketRepositoryError(Exception):
    """Base class for ticket storage errors."""


class TicketNotFoundError(TicketRepositoryError):
    def __init__(self) -> None:
        super().__init__("no such ticket")


class ConversationLinkError(TicketRepositoryError):
    """The conversation a ticket should link to does not exist."""


class IdempotencyKeyConflictError(TicketRepositoryError):
    """The client_request_id was already used in this conversation for another request."""

    def __init__(self) -> None:
        super().__init__("this client_request_id was already used for a different request")


class InvalidStatusTransitionError(TicketRepositoryError):
    def __init__(self, current: TicketStatus, requested: TicketStatus) -> None:
        super().__init__(f"cannot change a {current} ticket to {requested}")
        self.current = current
        self.requested = requested


class StaleTicketStatusError(TicketRepositoryError):
    """The ticket's status changed after it was read; re-read and decide again."""

    def __init__(self, expected: TicketStatus, current: TicketStatus) -> None:
        super().__init__(f"expected a {expected} ticket, but it is {current}")
        self.expected = expected
        self.current = current


class TicketStoreError(TicketRepositoryError):
    """The database failed; details are in the logs (error type only)."""


class TicketStoreBusyError(TicketStoreError):
    """The database was locked by another writer past the busy timeout; retry."""


# --- Results and filters --------------------------------------------------------------


@dataclass(frozen=True)
class StoredTicket:
    """A ticket returned by ``create_ticket`` and how it came about."""

    ticket: SupportTicket
    outcome: TicketSubmissionOutcome


@dataclass(frozen=True)
class TicketFilter:
    """Criteria for ``list_tickets``; every field left as None matches everything."""

    statuses: frozenset[TicketStatus] | None = None
    ticket_type: TicketType | None = None
    priority: TicketPriority | None = None
    session_id: uuid.UUID | None = None
    assigned_to: str | None = None
    created_from: datetime | None = None  # inclusive
    created_before: datetime | None = None  # exclusive


@dataclass(frozen=True)
class TicketPage:
    """One page of tickets, oldest first, and the number matching in total."""

    tickets: tuple[SupportTicket, ...]
    total: int
    offset: int
    limit: int

    @property
    def has_more(self) -> bool:
        return self.offset + len(self.tickets) < self.total


# --- Helpers --------------------------------------------------------------------------


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


def _check_page(offset: int, limit: int) -> None:
    if offset < 0:
        raise ValueError("offset must not be negative")
    if not 1 <= limit <= MAX_PAGE_SIZE:
        raise ValueError(f"limit must be between 1 and {MAX_PAGE_SIZE}")


# --- Repository -----------------------------------------------------------------------


class TicketRepository:
    """Support tickets and the contacts and events that belong to them."""

    def __init__(self, database: Database) -> None:
        self._db = database

    # --- Creating -----------------------------------------------------------------

    def create_ticket(
        self,
        request: SupportRequestCreate,
        *,
        session_id: uuid.UUID | None = None,
        escalation_reason: EscalationReason | None = None,
        priority: TicketPriority = TicketPriority.NORMAL,
        duplicate_window: timedelta | None = DEFAULT_DUPLICATE_WINDOW,
        now: datetime | None = None,
    ) -> StoredTicket:
        """Store a ticket for a validated request, or return the one it repeats.

        See the module docstring for the steps. Idempotency and duplicate detection need
        a conversation (their scope); without one, every call creates a ticket.

        Raises:
            ConversationLinkError: ``session_id`` names no stored conversation.
            IdempotencyKeyConflictError: the key was used for a different request.
            TicketStoreError: the database failed (nothing is stored).
        """
        created = now or utc_now()
        fingerprint = request_fingerprint(request)
        key = str(request.client_request_id) if session_id is not None else None
        with self._write("create_ticket") as db:
            if session_id is not None:
                if db.get(ConversationSessionRow, session_id) is None:
                    raise ConversationLinkError("the conversation does not exist")
                earlier = self._repeat(db, session_id, key, fingerprint, duplicate_window, created)
                if earlier is not None:
                    return earlier
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
                idempotency_key=key,
                request_fingerprint=fingerprint,
            )
            db.add(ticket)
            self._append_event(
                db,
                ticket.ticket_id,
                TicketEventType.CREATED,
                None,
                TicketStatus.OPEN,
                ActorType.CUSTOMER,
                created,
                sequence=1,
            )
            db.flush()
            return StoredTicket(_to_ticket(ticket), TicketSubmissionOutcome.CREATED)

    def _repeat(
        self,
        db: Session,
        session_id: uuid.UUID,
        key: str | None,
        fingerprint: str,
        window: timedelta | None,
        now: datetime,
    ) -> StoredTicket | None:
        """The ticket this request repeats (same key, or same open request), if any."""
        if key is not None:
            same_key = db.scalar(
                select(SupportTicketRow).where(
                    SupportTicketRow.session_id == session_id,
                    SupportTicketRow.idempotency_key == key,
                )
            )
            if same_key is not None:
                if same_key.request_fingerprint != fingerprint:
                    raise IdempotencyKeyConflictError()
                return StoredTicket(_to_ticket(same_key), TicketSubmissionOutcome.REPLAYED)
        if window is None:
            return None
        duplicate = db.scalars(
            select(SupportTicketRow)
            .where(
                SupportTicketRow.session_id == session_id,
                SupportTicketRow.request_fingerprint == fingerprint,
                SupportTicketRow.status.in_([s.value for s in ACTIVE_STATUSES]),
                SupportTicketRow.created_at >= now - window,
            )
            .order_by(SupportTicketRow.created_at, SupportTicketRow.ticket_id)
            .limit(1)
        ).first()
        if duplicate is None:
            return None
        return StoredTicket(_to_ticket(duplicate), TicketSubmissionOutcome.DUPLICATE)

    # --- Reading ------------------------------------------------------------------

    def get_ticket(self, ticket_id: uuid.UUID) -> SupportTicket:
        with self._read("get_ticket") as db:
            row = db.get(SupportTicketRow, ticket_id)
            if row is None:
                raise TicketNotFoundError()
            return _to_ticket(row)

    def get_ticket_by_reference(self, reference_number: str) -> SupportTicket:
        """Staff look-up by reference (case and surrounding spaces ignored)."""
        with self._read("get_ticket_by_reference") as db:
            row = db.scalar(
                select(SupportTicketRow).where(
                    SupportTicketRow.reference_number == reference_number.strip().upper()
                )
            )
            if row is None:
                raise TicketNotFoundError()
            return _to_ticket(row)

    def get_session_ticket(self, session_id: uuid.UUID, reference_number: str) -> SupportTicket:
        """A ticket by reference, only if it belongs to this conversation. Another
        conversation's ticket is reported exactly like a missing one."""
        with self._read("get_session_ticket") as db:
            row = db.scalar(
                select(SupportTicketRow).where(
                    SupportTicketRow.session_id == session_id,
                    SupportTicketRow.reference_number == reference_number.strip().upper(),
                )
            )
            if row is None:
                raise TicketNotFoundError()
            return _to_ticket(row)

    def list_tickets(
        self, filters: TicketFilter | None = None, *, offset: int = 0, limit: int = 50
    ) -> TicketPage:
        """One page of matching tickets, oldest first (``created_at``, then ID)."""
        _check_page(offset, limit)
        query = _filtered(select(SupportTicketRow), filters or TicketFilter())
        with self._read("list_tickets") as db:
            total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
            rows = db.scalars(
                query.order_by(SupportTicketRow.created_at, SupportTicketRow.ticket_id)
                .offset(offset)
                .limit(limit)
            ).all()
            return TicketPage(tuple(_to_ticket(r) for r in rows), total, offset, limit)

    def list_session_tickets(
        self, session_id: uuid.UUID, *, offset: int = 0, limit: int = 50
    ) -> TicketPage:
        """One page of one conversation's tickets, oldest first."""
        return self.list_tickets(TicketFilter(session_id=session_id), offset=offset, limit=limit)

    def list_events(self, ticket_id: uuid.UUID) -> list[TicketEvent]:
        """A ticket's history, oldest first."""
        with self._read("list_events") as db:
            rows = db.scalars(
                select(TicketEventRow)
                .where(TicketEventRow.ticket_id == ticket_id)
                .order_by(TicketEventRow.sequence)
            ).all()
            return [_to_event(row) for row in rows]

    def get_contact(self, contact_id: uuid.UUID) -> CustomerContact | None:
        """Contact details, for staff tooling only (never for public APIs)."""
        with self._read("get_contact") as db:
            row = db.get(CustomerContactRow, contact_id)
            return _to_contact(row) if row is not None else None

    # --- Status changes -----------------------------------------------------------

    def change_status(
        self,
        ticket_id: uuid.UUID,
        new_status: TicketStatus,
        *,
        actor_type: ActorType,
        actor_id: str | None = None,
        assigned_to: str | None = None,
        expected_status: TicketStatus | None = None,
        now: datetime | None = None,
    ) -> SupportTicket:
        """Apply a status change and record it as an event, in one transaction.

        Who may make the change is decided by ``TicketWorkflow`` before this is called;
        here only the data rules are enforced: the transition must be in
        ``ALLOWED_TRANSITIONS``, a change to ``assigned`` names the assignee, and with
        ``expected_status`` the ticket must still be in that status (no lost updates).

        Timestamps: ``updated_at`` only moves forward; ``resolved_at`` is set on
        resolving and cleared on reopening (the event history keeps the earlier one);
        ``closed_at`` is set on closing; ``created_at`` never changes. A change back to
        ``open`` (unassigning) clears ``assigned_to``.

        Raises:
            TicketNotFoundError, StaleTicketStatusError, InvalidStatusTransitionError,
            ValueError (assigned without an assignee).
        """
        if (new_status is TicketStatus.ASSIGNED) != (assigned_to is not None):
            raise ValueError("assigned_to is required for, and only for, a change to assigned")
        with self._write("change_status") as db:
            row = db.get(SupportTicketRow, ticket_id)
            if row is None:
                raise TicketNotFoundError()
            current = TicketStatus(row.status)
            if expected_status is not None and current is not expected_status:
                raise StaleTicketStatusError(expected_status, current)
            if new_status not in ALLOWED_TRANSITIONS[current]:
                raise InvalidStatusTransitionError(current, new_status)
            changed = max(now or utc_now(), row.updated_at)
            row.status = new_status.value
            row.updated_at = changed
            if new_status is TicketStatus.ASSIGNED:
                row.assigned_to = assigned_to
            elif new_status is TicketStatus.OPEN:
                row.assigned_to = None
            if new_status is TicketStatus.RESOLVED:
                row.resolved_at = changed
            elif new_status is not TicketStatus.CLOSED:
                row.resolved_at = None
            if new_status is TicketStatus.CLOSED:
                row.closed_at = changed
            self._append_event(
                db,
                ticket_id,
                TicketEventType.STATUS_CHANGED,
                current,
                new_status,
                actor_type,
                changed,
                actor_id=actor_id,
                assigned_to=assigned_to,
            )
            db.flush()
            return _to_ticket(row)

    # --- Internals ----------------------------------------------------------------

    @staticmethod
    def _append_event(
        db: Session,
        ticket_id: uuid.UUID,
        event_type: TicketEventType,
        previous: TicketStatus | None,
        new: TicketStatus,
        actor_type: ActorType,
        created_at: datetime,
        *,
        actor_id: str | None = None,
        assigned_to: str | None = None,
        sequence: int | None = None,
    ) -> None:
        """Record an event as the ticket's next position (in the caller's transaction)."""
        if sequence is None:
            last = db.scalar(
                select(func.max(TicketEventRow.sequence)).where(
                    TicketEventRow.ticket_id == ticket_id
                )
            )
            sequence = (last or 0) + 1
        event = TicketEvent(
            event_id=new_id(),
            ticket_id=ticket_id,
            sequence=sequence,
            event_type=event_type,
            previous_status=previous,
            new_status=new,
            actor_type=actor_type,
            actor_id=actor_id,
            assigned_to=assigned_to,
            created_at=created_at,
        )  # validated before it is written
        db.add(
            TicketEventRow(
                event_id=event.event_id,
                ticket_id=ticket_id,
                sequence=event.sequence,
                event_type=event.event_type.value,
                previous_status=previous.value if previous else None,
                new_status=new.value,
                actor_type=actor_type.value,
                actor_id=actor_id,
                assigned_to=assigned_to,
                created_at=created_at,
            )
        )

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
        raise TicketStoreError("could not allocate a reference number")

    @contextmanager
    def _read(self, operation: str) -> Iterator[Session]:
        with _store_errors(operation), self._db.read() as db:
            yield db

    @contextmanager
    def _write(self, operation: str) -> Iterator[Session]:
        with _store_errors(operation), self._db.write() as db:
            yield db


def _filtered(
    query: Select[tuple[SupportTicketRow]], f: TicketFilter
) -> Select[tuple[SupportTicketRow]]:
    if f.statuses is not None:
        query = query.where(SupportTicketRow.status.in_([s.value for s in f.statuses]))
    if f.ticket_type is not None:
        query = query.where(SupportTicketRow.ticket_type == f.ticket_type.value)
    if f.priority is not None:
        query = query.where(SupportTicketRow.priority == f.priority.value)
    if f.session_id is not None:
        query = query.where(SupportTicketRow.session_id == f.session_id)
    if f.assigned_to is not None:
        query = query.where(SupportTicketRow.assigned_to == f.assigned_to)
    if f.created_from is not None:
        query = query.where(SupportTicketRow.created_at >= f.created_from)
    if f.created_before is not None:
        query = query.where(SupportTicketRow.created_at < f.created_before)
    return query


@contextmanager
def _store_errors(operation: str) -> Iterator[None]:
    """Database failures and invalid stored rows become ``TicketStoreError``; the
    exception text (which can contain contact details) is never logged."""
    try:
        yield
    except IntegrityError as exc:
        # Only reachable if a row breaks a constraint the checks above did not foresee.
        logger.error("Ticket store %s refused a write (%s)", operation, type(exc).__name__)
        raise TicketStoreError(f"ticket storage refused the change ({operation})") from exc
    except SQLAlchemyError as exc:
        logger.error("Ticket store %s failed (%s)", operation, type(exc).__name__)
        if isinstance(exc, OperationalError) and "locked" in str(exc.orig).lower():
            raise TicketStoreBusyError(f"ticket storage is busy ({operation})") from exc
        raise TicketStoreError(f"ticket storage is unavailable ({operation})") from exc
    except ValidationError as exc:
        logger.error("Invalid ticket data in %s (%d errors)", operation, exc.error_count())
        raise TicketStoreError("ticket data is invalid") from None


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
        assigned_to=row.assigned_to,
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
        actor_type=row.actor_type,
        actor_id=row.actor_id,
        assigned_to=row.assigned_to,
        created_at=row.created_at,
    )
