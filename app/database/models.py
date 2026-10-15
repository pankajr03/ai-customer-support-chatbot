"""ORM tables for conversation storage (Stage 6).

These are storage rows only. The validated domain records are ``ConversationSession``
and ``ConversationMessage`` in ``app.models.conversation``; the repository converts
between the two, so nothing outside it handles ORM objects.

The tables repeat the most important rules as constraints (allowed roles, statuses and
decisions, one position per message, a message must belong to an existing session), so
the data stays consistent even if a row is written by something other than the app.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
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
    )

    session_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    status: Mapped[str] = mapped_column(String(16))
    # SHA-256 hex digest of the session's bearer token; the token itself is never stored.
    access_token_hash: Mapped[str] = mapped_column(String(64))


class ConversationMessageRow(Base):
    __tablename__ = "conversation_messages"
    __table_args__ = (
        # One message per position: defines the order and is the lookup index.
        UniqueConstraint("session_id", "sequence", name="uq_message_session_sequence"),
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
