"""Store and load conversation sessions and messages (SQLite, via SQLAlchemy).

The repository takes and returns the validated records from ``app.models.conversation``;
ORM rows never leave this module. Every method runs in its own transaction: writes
commit together or not at all.

Errors:
- ``SessionNotFoundError``: no session with that ID (also for a well-formed but unknown
  ID, so callers can answer 404 without revealing anything else).
- ``SessionNotActiveError``: the session is closed or expired and takes no new messages.
- ``ConversationStoreError``: the database failed. It is logged with the error type only
  (never message text or SQL parameters); callers should answer 503.

Message order: each message gets the next position (``sequence``) in its session inside
the same write transaction that inserts it, and every read orders by it.
"""

import logging
import uuid
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.database.connection import Database
from app.database.models import ConversationMessageRow, ConversationSessionRow
from app.models.conversation import (
    ConversationMessage,
    ConversationSession,
    SessionStatus,
    utc_now,
)
from app.services.answer_decision_service import Decision

logger = logging.getLogger(__name__)


class ConversationRepositoryError(Exception):
    """Base class for conversation storage errors."""


class SessionNotFoundError(ConversationRepositoryError):
    def __init__(self, session_id: uuid.UUID) -> None:
        super().__init__(f"conversation session {session_id} not found")
        self.session_id = session_id


class SessionNotActiveError(ConversationRepositoryError):
    def __init__(self, session_id: uuid.UUID, status: SessionStatus) -> None:
        super().__init__(f"conversation session {session_id} is {status}")
        self.session_id = session_id
        self.status = status


class ConversationStoreError(ConversationRepositoryError):
    """The database could not complete the operation. Safe to show: no details inside."""


class ConversationRepository:
    """Conversation persistence. Thread-safe: holds no session between calls."""

    def __init__(self, database: Database) -> None:
        self._db = database

    # --- Sessions -----------------------------------------------------------------

    def create_session(self, session: ConversationSession) -> ConversationSession:
        """Store a new session (see ``ConversationSession.new``)."""
        with self._write("create_session") as db:
            db.add(_session_row(session))
        return session

    def get_session(self, session_id: uuid.UUID) -> ConversationSession:
        """Raises ``SessionNotFoundError`` if there is no such session."""
        with self._read("get_session") as db:
            return _to_session(self._session_row(db, session_id))

    def set_status(
        self, session_id: uuid.UUID, status: SessionStatus, now: datetime | None = None
    ) -> ConversationSession:
        """Close or expire a session. Closed and expired sessions cannot be reopened."""
        with self._write("set_status") as db:
            row = self._session_row(db, session_id)
            updated = _to_session(row).with_status(status, now)
            row.status, row.updated_at = updated.status.value, updated.updated_at
        return updated

    # --- Messages -----------------------------------------------------------------

    def add_user_message(
        self, session_id: uuid.UUID, content: str, now: datetime | None = None
    ) -> ConversationMessage:
        """Append a customer message. The session must be active and not expired."""
        now = now or utc_now()
        return self._append(
            session_id,
            now,
            lambda seq: ConversationMessage.user_message(session_id, seq, content, now),
            require_unexpired=True,
        )

    def add_assistant_message(
        self,
        session_id: uuid.UUID,
        content: str,
        decision: Decision,
        source_faq_ids: Iterable[str] = (),
        now: datetime | None = None,
    ) -> ConversationMessage:
        """Append a reply. The session must still be active; a reply may complete an
        exchange whose customer message arrived just before ``expires_at``."""
        now = now or utc_now()
        sources = tuple(source_faq_ids)
        return self._append(
            session_id,
            now,
            lambda seq: ConversationMessage.assistant_message(
                session_id, seq, content, decision, sources, now
            ),
            require_unexpired=False,
        )

    def list_messages(self, session_id: uuid.UUID) -> list[ConversationMessage]:
        """All messages of a session, oldest first. Raises ``SessionNotFoundError``."""
        return self.get_history(session_id)[1]

    def get_history(
        self, session_id: uuid.UUID
    ) -> tuple[ConversationSession, list[ConversationMessage]]:
        """A session and its messages (oldest first), read in one transaction."""
        with self._read("get_history") as db:
            session = _to_session(self._session_row(db, session_id))
            rows = db.scalars(
                select(ConversationMessageRow)
                .where(ConversationMessageRow.session_id == session_id)
                .order_by(ConversationMessageRow.sequence)
            )
            return session, [_to_message(row) for row in rows]

    # --- Internals ----------------------------------------------------------------

    def _append(
        self,
        session_id: uuid.UUID,
        now: datetime,
        build: Callable[[int], ConversationMessage],
        *,
        require_unexpired: bool,
    ) -> ConversationMessage:
        with self._write("add_message") as db:
            row = self._session_row(db, session_id)
            session = _to_session(row)
            if session.status is not SessionStatus.ACTIVE:
                raise SessionNotActiveError(session_id, session.status)
            if require_unexpired and session.is_expired(now):
                raise SessionNotActiveError(session_id, SessionStatus.EXPIRED)
            last = db.scalar(
                select(func.max(ConversationMessageRow.sequence)).where(
                    ConversationMessageRow.session_id == session_id
                )
            )
            message = build((last or 0) + 1)
            db.add(_message_row(message))
            row.updated_at = session.touched(now).updated_at
        return message

    @staticmethod
    def _session_row(db: Session, session_id: uuid.UUID) -> ConversationSessionRow:
        row = db.get(ConversationSessionRow, session_id)
        if row is None:
            raise SessionNotFoundError(session_id)
        return row

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
    """Turn database failures into ``ConversationStoreError`` without leaking details."""
    try:
        yield
    except SQLAlchemyError as exc:
        # Type only: the exception text can include SQL parameters (customer messages).
        logger.error("Conversation store %s failed (%s)", operation, type(exc).__name__)
        raise ConversationStoreError(f"conversation storage is unavailable ({operation})") from exc


# --- Conversions (rows are re-validated on the way out) -------------------------------


@contextmanager
def _valid_record(table: str) -> Iterator[None]:
    """A stored row that fails validation means corrupt data: report it as a store error."""
    try:
        yield
    except ValidationError as exc:
        logger.error("Invalid row in %s (%d validation errors)", table, exc.error_count())
        raise ConversationStoreError("stored conversation data is invalid") from None


def _session_row(session: ConversationSession) -> ConversationSessionRow:
    return ConversationSessionRow(
        session_id=session.session_id,
        created_at=session.created_at,
        updated_at=session.updated_at,
        expires_at=session.expires_at,
        status=session.status.value,
    )


def _to_session(row: ConversationSessionRow) -> ConversationSession:
    with _valid_record("conversation_sessions"):
        return ConversationSession(
            session_id=row.session_id,
            created_at=row.created_at,
            updated_at=row.updated_at,
            expires_at=row.expires_at,
            status=row.status,
        )


def _message_row(message: ConversationMessage) -> ConversationMessageRow:
    return ConversationMessageRow(
        message_id=message.message_id,
        session_id=message.session_id,
        sequence=message.sequence,
        role=message.role.value,
        content=message.content,
        created_at=message.created_at,
        decision=message.decision.value if message.decision else None,
        source_faq_ids=list(message.source_faq_ids),
    )


def _to_message(row: ConversationMessageRow) -> ConversationMessage:
    with _valid_record("conversation_messages"):
        return ConversationMessage(
            message_id=row.message_id,
            session_id=row.session_id,
            sequence=row.sequence,
            role=row.role,
            content=row.content,
            created_at=row.created_at,
            decision=row.decision,
            source_faq_ids=tuple(row.source_faq_ids or ()),
        )
