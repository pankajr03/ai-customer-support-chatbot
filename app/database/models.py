"""ORM tables for conversation storage (Stage 6) and support tickets (Stage 10).

These are storage rows only. The validated domain records are ``ConversationSession``
and ``ConversationMessage`` in ``app.models.conversation`` (and ``CustomerContact``,
``SupportTicket`` and ``TicketEvent`` in ``app.models.support``); the repositories
convert between the two, so nothing outside them handles ORM objects.

The tables repeat the most important rules as constraints (allowed roles, statuses and
decisions, one position per message, a message must belong to an existing session), so
the data stays consistent even if a row is written by something other than the app.

Support tables (Stage 10): ``customer_contacts`` holds personal contact details, apart
from chat messages; ``support_tickets`` links a request to its contact and, by ID only,
to its conversation (the conversation is never copied into the ticket); and
``ticket_events`` is each ticket's status history, with who made each change. Deleting a
conversation keeps its tickets and clears their link (``ON DELETE SET NULL``); a contact
with tickets cannot be deleted by accident (``RESTRICT``). The support tables are listed
in ``REBUILDABLE_TABLES``: when their definition changes (for example a new allowed
status in a CHECK constraint), ``Database.init_schema`` rebuilds them, copying every row.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.models.conversation import MAX_ASSISTANT_MESSAGE_LENGTH, MessageRole, SessionStatus
from app.models.escalation import EscalationReason, TicketType
from app.models.support import (
    MAX_DESCRIPTION_LENGTH,
    MAX_EMAIL_LENGTH,
    MAX_NAME_LENGTH,
    MAX_PHONE_LENGTH,
    MAX_SUBJECT_LENGTH,
    REFERENCE_LENGTH,
    ActorType,
    ContactPreference,
    LeadCategory,
    TicketEventType,
    TicketPriority,
    TicketStatus,
)
from app.services.answer_decision_service import Decision


def _in(column: str, values: type[Any]) -> str:
    # Values come from our own enums, never from user input.
    allowed = ", ".join(f"'{member.value}'" for member in values)
    return f"{column} IN ({allowed})"


class UTCDateTime(TypeDecorator[datetime]):
    """Stores timezone-aware datetimes as UTC and returns them as aware UTC datetimes.

    SQLite has no timezone support, so values are normalised to UTC before storage.
    Naive datetimes are rejected rather than guessed.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("naive datetimes cannot be stored; use timezone-aware UTC")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        return value.replace(tzinfo=UTC) if value is not None else None


class Base(DeclarativeBase):
    pass


class ConversationSessionRow(Base):
    __tablename__ = "conversation_sessions"
    __table_args__ = (
        CheckConstraint(_in("status", SessionStatus), name="ck_session_status"),
        CheckConstraint("length(access_token_hash) = 64", name="ck_session_token_hash"),
        CheckConstraint("updated_at >= created_at", name="ck_session_updated_after_created"),
        CheckConstraint("expires_at IS NULL OR expires_at > created_at", name="ck_session_expiry"),
        Index("ix_session_expires_at", "expires_at"),  # retention cleanup
    )

    session_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    status: Mapped[str] = mapped_column(String(16))
    # SHA-256 hex digest of the session's bearer token; the token itself is never stored.
    access_token_hash: Mapped[str] = mapped_column(String(64))
    # Processing lease: while a message is being answered, other messages to the session
    # are refused. Stored here (not in memory) so it holds across processes, and it
    # expires on its own if a process dies mid-request.
    processing_key: Mapped[str | None] = mapped_column(String(64))
    processing_until: Mapped[datetime | None] = mapped_column(UTCDateTime)


class ConversationMessageRow(Base):
    __tablename__ = "conversation_messages"
    __table_args__ = (
        # One message per position: defines the order and is the lookup index.
        UniqueConstraint("session_id", "sequence", name="uq_message_session_sequence"),
        # A client's Idempotency-Key is used at most once per session (NULLs are allowed).
        Index("uq_message_idempotency_key", "session_id", "idempotency_key", unique=True),
        CheckConstraint("sequence >= 1", name="ck_message_sequence"),
        CheckConstraint(_in("role", MessageRole), name="ck_message_role"),
        CheckConstraint(
            f"length(content) BETWEEN 1 AND {MAX_ASSISTANT_MESSAGE_LENGTH}",
            name="ck_message_content_length",
        ),
        CheckConstraint(
            f"decision IS NULL OR {_in('decision', Decision)}", name="ck_message_decision"
        ),
        CheckConstraint(
            "(role = 'user' AND decision IS NULL) OR (role = 'assistant' AND decision IS NOT NULL)",
            name="ck_message_decision_role",
        ),
        CheckConstraint(
            f"escalation_reason IS NULL OR (role = 'assistant' AND "
            f"{_in('escalation_reason', EscalationReason)})",
            name="ck_message_escalation_reason",
        ),
    )

    message_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    session_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("conversation_sessions.session_id", ondelete="CASCADE")
    )
    sequence: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    decision: Mapped[str | None] = mapped_column(String(32))
    # A JSON list of FAQ IDs, e.g. ["faq_001", "faq_027"]; empty for user messages.
    source_faq_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    # User messages: the client's retry key (client_message_id or Idempotency-Key), if
    # it sent one. Unique per session (uq_message_idempotency_key).
    idempotency_key: Mapped[str | None] = mapped_column(String(64))
    # User messages with a retry key: SHA-256 of the request (message and options), so a
    # retry can be told apart from a different request reusing the key. Added in Stage 7
    # as a nullable column: older rows keep NULL and are compared by message text.
    request_fingerprint: Mapped[str | None] = mapped_column(String(64))
    # Assistant messages: how the reply was produced (as reported by the API).
    generation_method: Mapped[str | None] = mapped_column(String(32))
    # Assistant messages: the support-request or contact-form offer made with the reply
    # (Stage 10), so a refreshed page and a retried request show the same offer. Added
    # as a nullable column: older rows keep NULL (no offer).
    escalation_reason: Mapped[str | None] = mapped_column(String(32))


# --- Support tickets (Stage 10) -------------------------------------------------------


class CustomerContactRow(Base):
    __tablename__ = "customer_contacts"
    __table_args__ = (
        CheckConstraint(_in("contact_preference", ContactPreference), name="ck_contact_preference"),
        CheckConstraint("consent_given = 1", name="ck_contact_consent"),
        CheckConstraint(f"length(name) BETWEEN 1 AND {MAX_NAME_LENGTH}", name="ck_contact_name"),
        CheckConstraint(f"length(email) BETWEEN 3 AND {MAX_EMAIL_LENGTH}", name="ck_contact_email"),
        CheckConstraint(
            f"phone IS NULL OR length(phone) BETWEEN 1 AND {MAX_PHONE_LENGTH}",
            name="ck_contact_phone",
        ),
        CheckConstraint(
            "contact_preference <> 'phone' OR phone IS NOT NULL", name="ck_contact_phone_preferred"
        ),
        CheckConstraint("length(identity_hash) = 64", name="ck_contact_identity_hash"),
        CheckConstraint("updated_at >= created_at", name="ck_contact_updated_after_created"),
        CheckConstraint("consent_timestamp >= created_at", name="ck_contact_consent_after_created"),
        Index("ix_contact_email", "email"),  # staff look-ups by email
    )

    contact_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    name: Mapped[str] = mapped_column(String(MAX_NAME_LENGTH))
    email: Mapped[str] = mapped_column(String(MAX_EMAIL_LENGTH))
    phone: Mapped[str | None] = mapped_column(String(MAX_PHONE_LENGTH))
    contact_preference: Mapped[str] = mapped_column(String(16))
    consent_given: Mapped[bool] = mapped_column(Boolean(create_constraint=False))
    consent_timestamp: Mapped[datetime] = mapped_column(UTCDateTime)  # the latest consent
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)
    # SHA-256 of the normalised details (email, name, phone, preference). The same
    # details submitted again reuse this row instead of creating a duplicate; different
    # details get a new row, so one submission never overwrites another's details.
    identity_hash: Mapped[str] = mapped_column(String(64), unique=True)
    # When name, email and phone were replaced by placeholders (retention or erasure).
    anonymized_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class SupportTicketRow(Base):
    __tablename__ = "support_tickets"
    __table_args__ = (
        CheckConstraint(_in("ticket_type", TicketType), name="ck_ticket_type"),
        CheckConstraint(_in("status", TicketStatus), name="ck_ticket_status"),
        CheckConstraint(_in("priority", TicketPriority), name="ck_ticket_priority"),
        CheckConstraint(
            f"escalation_reason IS NULL OR {_in('escalation_reason', EscalationReason)}",
            name="ck_ticket_escalation_reason",
        ),
        CheckConstraint(
            f"length(reference_number) = {REFERENCE_LENGTH} AND ("
            "(ticket_type = 'support' AND reference_number LIKE 'SUP-%') OR "
            "(ticket_type = 'sales' AND reference_number LIKE 'SAL-%'))",
            name="ck_ticket_reference",
        ),
        CheckConstraint(
            f"length(subject) BETWEEN 1 AND {MAX_SUBJECT_LENGTH}", name="ck_ticket_subject"
        ),
        CheckConstraint(
            f"length(description) BETWEEN 1 AND {MAX_DESCRIPTION_LENGTH}",
            name="ck_ticket_description",
        ),
        CheckConstraint("updated_at >= created_at", name="ck_ticket_updated_after_created"),
        CheckConstraint(
            "status <> 'resolved' OR resolved_at IS NOT NULL", name="ck_ticket_resolved_at"
        ),
        CheckConstraint(
            "status IN ('resolved', 'closed') OR resolved_at IS NULL",
            name="ck_ticket_resolved_at_status",
        ),
        CheckConstraint(
            "(status = 'closed' AND closed_at IS NOT NULL) OR "
            "(status <> 'closed' AND closed_at IS NULL)",
            name="ck_ticket_closed_at",
        ),
        CheckConstraint(
            "anonymized_at IS NULL OR status = 'closed'", name="ck_ticket_anonymized_closed"
        ),
        CheckConstraint(
            "status <> 'assigned' OR assigned_to IS NOT NULL", name="ck_ticket_assigned_to"
        ),
        CheckConstraint(
            f"(ticket_type = 'sales' AND {_in('lead_category', LeadCategory)}) OR "
            "(ticket_type = 'support' AND lead_category IS NULL)",
            name="ck_ticket_lead_category",
        ),
        CheckConstraint(
            "assigned_to IS NULL OR length(assigned_to) BETWEEN 1 AND 64",
            name="ck_ticket_assigned_to_length",
        ),
        CheckConstraint(
            "request_fingerprint IS NULL OR length(request_fingerprint) = 64",
            name="ck_ticket_request_fingerprint",
        ),
        # A client_request_id is used at most once per conversation (NULLs are allowed:
        # tickets without a key, or whose conversation was deleted since).
        Index("uq_ticket_idempotency_key", "session_id", "idempotency_key", unique=True),
        # Staff queues: "open sales leads, oldest first", "everything in progress".
        Index("ix_ticket_status_created", "status", "created_at"),
        Index("ix_ticket_type_status_created", "ticket_type", "status", "created_at"),
        Index("ix_ticket_contact", "contact_id"),
        # A conversation's tickets, oldest first; and duplicate-submission checks.
        Index("ix_ticket_session_created", "session_id", "created_at"),
        Index("ix_ticket_session_fingerprint", "session_id", "request_fingerprint"),
    )

    ticket_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    reference_number: Mapped[str] = mapped_column(String(REFERENCE_LENGTH), unique=True)
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("conversation_sessions.session_id", ondelete="SET NULL")
    )
    contact_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("customer_contacts.contact_id", ondelete="RESTRICT")
    )
    ticket_type: Mapped[str] = mapped_column(String(16))
    # Sales leads: what the inquiry is about (bulk order, pricing, ...); NULL for support.
    lead_category: Mapped[str | None] = mapped_column(String(32))
    escalation_reason: Mapped[str | None] = mapped_column(String(32))
    subject: Mapped[str] = mapped_column(String(MAX_SUBJECT_LENGTH))
    # The customer's own (approved) description; never the conversation transcript.
    description: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32))
    priority: Mapped[str] = mapped_column(String(16))
    # The staff member who owns it; set only by authenticated staff, never invented.
    assigned_to: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # The client's client_request_id (unique per conversation) and SHA-256 of the
    # submission, so a retry returns this ticket and a changed retry is refused.
    idempotency_key: Mapped[str | None] = mapped_column(String(64))
    request_fingerprint: Mapped[str | None] = mapped_column(String(64))
    # When subject and description were removed; only closed tickets are anonymized.
    anonymized_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class TicketEventRow(Base):
    __tablename__ = "ticket_events"
    __table_args__ = (
        UniqueConstraint("ticket_id", "sequence", name="uq_ticket_event_sequence"),
        CheckConstraint("sequence >= 1", name="ck_ticket_event_sequence"),
        CheckConstraint(_in("event_type", TicketEventType), name="ck_ticket_event_type"),
        CheckConstraint(
            f"previous_status IS NULL OR {_in('previous_status', TicketStatus)}",
            name="ck_ticket_event_previous_status",
        ),
        CheckConstraint(
            f"new_status IS NULL OR {_in('new_status', TicketStatus)}",
            name="ck_ticket_event_new_status",
        ),
        CheckConstraint(
            "(event_type = 'created' AND previous_status IS NULL AND new_status IS NOT NULL) "
            "OR (event_type = 'status_changed' AND previous_status IS NOT NULL "
            "AND new_status IS NOT NULL AND previous_status <> new_status) "
            "OR (event_type = 'anonymized' AND previous_status IS NULL AND new_status IS NULL)",
            name="ck_ticket_event_statuses",
        ),
        CheckConstraint(_in("actor_type", ActorType), name="ck_ticket_event_actor_type"),
        CheckConstraint(
            "(actor_type = 'staff' AND actor_id IS NOT NULL) OR "
            "(actor_type <> 'staff' AND actor_id IS NULL)",
            name="ck_ticket_event_actor_id",
        ),
        CheckConstraint(
            "assigned_to IS NULL OR new_status = 'assigned'", name="ck_ticket_event_assigned_to"
        ),
    )

    event_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    ticket_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("support_tickets.ticket_id", ondelete="CASCADE")
    )
    # Position in the ticket's history (events can share a timestamp).
    sequence: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(32))
    previous_status: Mapped[str | None] = mapped_column(String(32))
    new_status: Mapped[str | None] = mapped_column(String(32))
    actor_type: Mapped[str] = mapped_column(String(16))
    actor_id: Mapped[str | None] = mapped_column(String(64))  # staff ID for staff changes
    assigned_to: Mapped[str | None] = mapped_column(String(64))  # for changes to assigned
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


# Tables that hold no conversation data and whose rows can be copied safely; rebuilt by
# Database.init_schema when their stored definition differs from the one above.
REBUILDABLE_TABLES = ("customer_contacts", "support_tickets", "ticket_events")
