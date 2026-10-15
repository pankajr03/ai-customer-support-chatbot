"""Store and load conversation sessions and messages (SQLite, via SQLAlchemy).

The repository takes and returns the validated records from ``app.models.conversation``;
ORM rows never leave this module. Every method runs in its own transaction: writes
commit together or not at all.

Errors:
- ``SessionNotFoundError``: no session with that ID (also for a well-formed but unknown
  ID, so callers can answer 404 without revealing anything else).
- ``SessionNotActiveError``: the session is closed or expired and takes no new messages.
- ``SessionBusyError``: another message to the session is being processed (its
  processing lease is held); callers should answer 409 and let the client retry.
- ``DuplicateIdempotencyKeyError``: an exchange with that ``Idempotency-Key`` is already
  stored; callers should return the stored exchange instead.
- ``ConversationStoreError``: the database failed. It is logged with the error type only
  (never message text or SQL parameters); callers should answer 503.
  ``ConversationStoreBusyError`` is the subclass for "database is locked" (another
  writer held the lock past the busy timeout): a retry usually succeeds.

Message order: each message gets the next position (``sequence``) in its session inside
the same write transaction that inserts it, and every read orders by it.

Processing lease: ``claim_processing`` marks a session as busy until a deadline, in one
short write transaction; ``add_exchange`` (on success) or ``release_processing`` (on
failure) clears it. Only the holder's key can clear it, and an expired lease (a crashed
process) can be taken over. No transaction stays open while a message is answered.
"""

import logging
import uuid
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta

from pydantic import ValidationError
from sqlalchemy import delete, func, select
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.database.connection import Database
from app.database.models import ConversationMessageRow, ConversationSessionRow
from app.models.answer import GenerationMethod
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


class SessionBusyError(ConversationRepositoryError):
    """Another message to this session is being processed (lease held until ``until``)."""

    def __init__(self, session_id: uuid.UUID, *, same_key: bool, until: datetime) -> None:
        super().__init__(f"conversation session {session_id} is processing another message")
        self.session_id = session_id
        self.same_key = same_key  # the holder used the same key: a duplicate submission
        self.until = until


class DuplicateIdempotencyKeyError(ConversationRepositoryError):
    """An exchange with this idempotency key is already stored in the session."""


class ConversationStoreError(ConversationRepositoryError):
    """The database could not complete the operation. Safe to show: no details inside."""


class ConversationStoreBusyError(ConversationStoreError):
    """The database stayed locked by another writer past the busy timeout."""


class ConversationRepository:
    """Conversation persistence. Thread-safe: holds no session between calls."""

    def __init__(self, database: Database) -> None:
        self._db = database

    # --- Sessions -----------------------------------------------------------------

    def create_session(
        self, session: ConversationSession, access_token_hash: str
    ) -> ConversationSession:
        """Store a new session (see ``ConversationSession.new``) with its token hash."""
        with self._write("create_session") as db:
            db.add(_session_row(session, access_token_hash))
        return session

    def get_session(self, session_id: uuid.UUID) -> ConversationSession:
        """Raises ``SessionNotFoundError`` if there is no such session."""
        with self._read("get_session") as db:
            return _to_session(self._session_row(db, session_id))

    def get_session_and_token_hash(self, session_id: uuid.UUID) -> tuple[ConversationSession, str]:
        """A session and its stored token hash, for authorisation only."""
        with self._read("get_session") as db:
            row = self._session_row(db, session_id)
            return _to_session(row), row.access_token_hash

    def touch_session(
        self, session_id: uuid.UUID, now: datetime | None = None
    ) -> ConversationSession:
        """Record activity: move ``updated_at`` forward to ``now``."""
        with self._write("touch_session") as db:
            row = self._session_row(db, session_id)
            updated = _to_session(row).touched(now)
            row.updated_at = updated.updated_at
        return updated

    def delete_session(self, session_id: uuid.UUID) -> None:
        """Delete a session and all its messages, in one transaction."""
        with self._write("delete_session") as db:
            row = self._session_row(db, session_id)
            # Explicit, rather than relying only on ON DELETE CASCADE.
            db.execute(
                delete(ConversationMessageRow).where(
                    ConversationMessageRow.session_id == session_id
                )
            )
            db.delete(row)

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

    def add_exchange(
        self,
        session_id: uuid.UUID,
        question: str,
        reply: str,
        decision: Decision,
        source_faq_ids: Iterable[str] = (),
        *,
        received_at: datetime,
        answered_at: datetime | None = None,
        idempotency_key: str | None = None,
        generation_method: GenerationMethod | None = None,
        processing_key: str | None = None,
    ) -> tuple[ConversationMessage, ConversationMessage]:
        """Store a customer message and its reply together: both or neither.

        The session must have been active and unexpired when the message was received
        (``received_at``); the reply may be stored after ``expires_at``. The two messages
        get consecutive positions, even if another exchange is stored concurrently.
        If ``processing_key`` holds the session's processing lease, it is released in the
        same transaction.

        Raises:
            DuplicateIdempotencyKeyError: ``idempotency_key`` was already used.
        """
        answered_at = answered_at or utc_now()
        sources = tuple(source_faq_ids)
        with self._write("add_exchange") as db:
            row = self._session_row(db, session_id)
            session = _to_session(row)
            if session.status is not SessionStatus.ACTIVE:
                raise SessionNotActiveError(session_id, session.status)
            if session.is_expired(received_at):
                raise SessionNotActiveError(session_id, SessionStatus.EXPIRED)
            if idempotency_key is not None and self._by_key(db, session_id, idempotency_key):
                raise DuplicateIdempotencyKeyError(idempotency_key)
            last = db.scalar(
                select(func.max(ConversationMessageRow.sequence)).where(
                    ConversationMessageRow.session_id == session_id
                )
            )
            position = (last or 0) + 1
            user = ConversationMessage.user_message(
                session_id, position, question, received_at, idempotency_key
            )
            assistant = ConversationMessage.assistant_message(
                session_id, position + 1, reply, decision, sources, answered_at, generation_method
            )
            db.add_all([_message_row(user), _message_row(assistant)])
            row.updated_at = session.touched(answered_at).updated_at
            if processing_key is not None and row.processing_key == processing_key:
                row.processing_key, row.processing_until = None, None
        return user, assistant

    # --- Processing lease and idempotency -----------------------------------------

    def claim_processing(
        self, session_id: uuid.UUID, key: str, *, now: datetime, lease: timedelta
    ) -> None:
        """Mark the session as processing a message until ``now + lease``.

        Raises:
            SessionNotActiveError: closed or expired at ``now``.
            SessionBusyError: another unexpired lease is held.
        """
        with self._write("claim_processing") as db:
            row = self._session_row(db, session_id)
            session = _to_session(row)
            if session.status is not SessionStatus.ACTIVE:
                raise SessionNotActiveError(session_id, session.status)
            if session.is_expired(now):
                raise SessionNotActiveError(session_id, SessionStatus.EXPIRED)
            if row.processing_until is not None and row.processing_until > now:
                raise SessionBusyError(
                    session_id, same_key=row.processing_key == key, until=row.processing_until
                )
            row.processing_key, row.processing_until = key, now + lease

    def release_processing(self, session_id: uuid.UUID, key: str) -> bool:
        """Release the lease if ``key`` still holds it. Returns whether it did.

        A missing session (deleted meanwhile) is not an error: there is nothing to release.
        """
        with self._write("release_processing") as db:
            row = db.get(ConversationSessionRow, session_id)
            if row is None or row.processing_key != key:
                return False
            row.processing_key, row.processing_until = None, None
            return True

    def find_exchange(
        self, session_id: uuid.UUID, idempotency_key: str
    ) -> tuple[ConversationMessage, ConversationMessage] | None:
        """The stored customer message with this idempotency key and its reply, if any."""
        with self._read("find_exchange") as db:
            self._session_row(db, session_id)
            user_row = self._by_key(db, session_id, idempotency_key)
            if user_row is None:
                return None
            reply_row = db.scalar(
                select(ConversationMessageRow).where(
                    ConversationMessageRow.session_id == session_id,
                    ConversationMessageRow.sequence == user_row.sequence + 1,
                )
            )
            if reply_row is None:  # exchanges are stored together; this means corruption
                raise ConversationStoreError("stored exchange is incomplete")
            return _to_message(user_row), _to_message(reply_row)

    # --- Retention ----------------------------------------------------------------

    def delete_expired_sessions(
        self, *, before: datetime, batch_size: int = 500, dry_run: bool = False
    ) -> int:
        """Delete sessions whose ``expires_at`` is at or before ``before``, with their
        messages. Works in batches of short transactions so it never holds the write
        lock for long. Returns the number of sessions deleted (or that would be).
        """
        if batch_size < 1:
            raise ValueError("batch_size must be at least 1")
        expired = ConversationSessionRow.expires_at <= before
        if dry_run:
            with self._read("count_expired") as db:
                count = db.scalar(
                    select(func.count()).select_from(ConversationSessionRow).where(expired)
                )
                return count or 0
        deleted = 0
        while True:
            with self._write("delete_expired") as db:
                ids = list(
                    db.scalars(
                        select(ConversationSessionRow.session_id).where(expired).limit(batch_size)
                    )
                )
                if not ids:
                    return deleted
                db.execute(
                    delete(ConversationMessageRow).where(ConversationMessageRow.session_id.in_(ids))
                )
                db.execute(
                    delete(ConversationSessionRow).where(ConversationSessionRow.session_id.in_(ids))
                )
            deleted += len(ids)

    def checkpoint(self) -> None:
        """Fold the write-ahead log into the database file and truncate it, so deleted
        content does not linger in the WAL file. Harmless for an in-memory database."""
        with _store_errors("checkpoint"), self._db.engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")

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

    def get_messages_page(
        self, session_id: uuid.UUID, *, offset: int, limit: int
    ) -> tuple[ConversationSession, list[ConversationMessage], int]:
        """A session, one page of its messages (oldest first) and its total message count,
        read in one transaction. Only the page's rows are loaded."""
        if offset < 0 or limit < 1:
            raise ValueError("offset must be >= 0 and limit >= 1")
        with self._read("get_messages_page") as db:
            session = _to_session(self._session_row(db, session_id))
            where = ConversationMessageRow.session_id == session_id
            total = db.scalar(select(func.count()).select_from(ConversationMessageRow).where(where))
            rows = db.scalars(
                select(ConversationMessageRow)
                .where(where)
                .order_by(ConversationMessageRow.sequence)
                .offset(offset)
                .limit(limit)
            )
            return session, [_to_message(row) for row in rows], total or 0

    def list_recent_messages(
        self, session_id: uuid.UUID, limit: int, before_sequence: int | None = None
    ) -> list[ConversationMessage]:
        """The latest ``limit`` messages of one session, oldest first.

        Only those rows are read (newest first, using the (session_id, sequence) index),
        never the whole conversation. ``before_sequence`` leaves out that message and
        everything after it, e.g. the customer message currently being answered.
        """
        if limit < 1:
            raise ValueError("limit must be at least 1")
        with self._read("list_recent_messages") as db:
            self._session_row(db, session_id)
            query = select(ConversationMessageRow).where(
                ConversationMessageRow.session_id == session_id
            )
            if before_sequence is not None:
                query = query.where(ConversationMessageRow.sequence < before_sequence)
            rows = db.scalars(query.order_by(ConversationMessageRow.sequence.desc()).limit(limit))
            return [_to_message(row) for row in rows][::-1]

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
    def _by_key(db: Session, session_id: uuid.UUID, key: str) -> ConversationMessageRow | None:
        return db.scalar(
            select(ConversationMessageRow).where(
                ConversationMessageRow.session_id == session_id,
                ConversationMessageRow.idempotency_key == key,
            )
        )

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
        if isinstance(exc, OperationalError) and "locked" in str(exc.orig).lower():
            raise ConversationStoreBusyError(f"conversation storage is busy ({operation})") from exc
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


def _session_row(session: ConversationSession, access_token_hash: str) -> ConversationSessionRow:
    return ConversationSessionRow(
        session_id=session.session_id,
        access_token_hash=access_token_hash,
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
        idempotency_key=message.idempotency_key,
        generation_method=message.generation_method.value if message.generation_method else None,
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
            idempotency_key=row.idempotency_key,
            generation_method=row.generation_method,
        )
