"""Conversation sessions with bearer-token access (local prototype).

Access model:
- Creating a conversation returns a random session ID **and** a random access token.
  The token is returned only then; the database keeps only its SHA-256 hash.
- Every other operation (read, post, touch, close, delete) needs both the session ID
  and the matching token. A session ID alone grants nothing.
- An unknown session, a malformed ID, a missing token and a wrong token all raise the
  same ``ConversationNotFoundError`` (to be answered with 404), after the same hashing
  work, so a caller cannot tell whether a session exists.
- Each operation works on one session only: every query is filtered by its ID.

Expiry and retention: at ``expires_at`` a conversation becomes inaccessible. Reading,
posting and closing raise ``ConversationExpiredError`` (410); only deletion is still
allowed. The first authorised access after that records the status as ``expired``.
Closed conversations stay readable until their ``expires_at``. Expired rows remain in
the database until ``cleanup_expired`` deletes them (``scripts/cleanup_conversations.py``
or at startup).

One message at a time: ``begin_turn`` takes the session's processing lease (stored in
the database, so it holds across processes sharing the file); another message to the
same conversation raises ``ConversationBusyError`` (409) until the exchange is stored or
``end_turn`` releases it. The lease expires on its own after ``processing_lease``, so a
crashed process cannot block a conversation for long.

Limits (also in the README): the token is the only credential, so anyone who obtains it
(e.g. from the browser's storage or a shared device) has full access to that one
conversation until it is deleted. There are no user accounts, no token rotation or
revocation other than deletion, and no rate limiting of guesses (guessing a 256-bit token
is not feasible, but repeated failures are not throttled). To add users or organisations
later, store an owner on the session and extend ``_authorize`` to accept an authenticated
principal as an alternative to the token.

Tokens are never logged. Log lines carry the session ID and the operation only.
"""

import logging
import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Self

from app.core.config import Settings
from app.core.security import generate_access_token, hash_access_token, verify_access_token
from app.models.answer import GenerationMethod
from app.models.conversation import (
    ConversationMessage,
    ConversationSession,
    SessionStatus,
    utc_now,
)
from app.repositories.conversation_repository import (
    ConversationRepository,
    ConversationStoreError,
    ProcessingLeaseLostError,
    SessionBusyError,
    SessionNotActiveError,
    SessionNotFoundError,
)
from app.services.answer_decision_service import Decision

logger = logging.getLogger(__name__)

# Longer than the slowest answer (LLM timeout x attempts + backoff); see the settings.
DEFAULT_PROCESSING_LEASE = timedelta(seconds=120)


class ConversationNotFoundError(Exception):
    """No conversation the caller may access: unknown ID or missing/wrong token.

    Deliberately one error with one message for every case, so responses cannot reveal
    whether a session exists.
    """

    def __init__(self) -> None:
        super().__init__("conversation not found")


class ConversationNotActiveError(Exception):
    """The caller is authorised, but the conversation is closed or expired."""

    def __init__(self, status: SessionStatus) -> None:
        super().__init__(f"conversation is {status}")
        self.status = status


class ConversationExpiredError(ConversationNotActiveError):
    """The conversation passed ``expires_at``: no longer accessible (deletion only)."""

    def __init__(self) -> None:
        super().__init__(SessionStatus.EXPIRED)


class ConversationChangedError(Exception):
    """A newer message was stored while this one was being answered (the processing lease
    had expired and was taken over). Nothing was stored; the client can send it again."""

    def __init__(self) -> None:
        super().__init__("the conversation changed while the message was being answered")


class ConversationBusyError(Exception):
    """Another message to this conversation is still being answered."""

    def __init__(self, *, retry_after_seconds: float, duplicate: bool) -> None:
        super().__init__("another message to this conversation is being processed")
        self.retry_after_seconds = retry_after_seconds
        self.duplicate = duplicate  # the same Idempotency-Key is being processed


@dataclass(frozen=True)
class CreatedConversation:
    """A new session and its access token. The token is not shown in repr()."""

    session: ConversationSession
    access_token: str = field(repr=False)


class ConversationService:
    """Create, read, update and delete conversations, checking access on every call."""

    def __init__(
        self,
        repository: ConversationRepository,
        ttl: timedelta,
        *,
        processing_lease: timedelta = DEFAULT_PROCESSING_LEASE,
    ) -> None:
        if ttl <= timedelta(0):
            raise ValueError("ttl must be positive")
        if processing_lease <= timedelta(0):
            raise ValueError("processing_lease must be positive")
        self._repo = repository
        self.ttl = ttl
        self.processing_lease = processing_lease

    @classmethod
    def from_settings(cls, settings: Settings, repository: ConversationRepository) -> Self:
        return cls(
            repository,
            timedelta(minutes=settings.conversation_ttl_minutes),
            processing_lease=timedelta(seconds=settings.conversation_processing_lease_seconds),
        )

    # --- Lifecycle ----------------------------------------------------------------

    def create_conversation(self, now: datetime | None = None) -> CreatedConversation:
        """Start a conversation. Return the token to the client now: it cannot be
        recovered later."""
        token = generate_access_token()
        session = self._repo.create_session(
            ConversationSession.new(ttl=self.ttl, now=now), hash_access_token(token)
        )
        logger.info("Conversation created: session_id=%s", session.session_id)
        return CreatedConversation(session=session, access_token=token)

    def get_conversation(
        self, session_id: uuid.UUID | str, access_token: str | None, now: datetime | None = None
    ) -> ConversationSession:
        """The session, with its expiry checked (and recorded) first.

        Raises ``ConversationExpiredError`` once it has expired.
        """
        return self._require_unexpired(self._authorize(session_id, access_token, now), now)

    def is_expired(
        self, session_id: uuid.UUID | str, access_token: str | None, now: datetime | None = None
    ) -> bool:
        """Whether the conversation has expired (the one check allowed after expiry)."""
        return self._authorize(session_id, access_token, now).is_expired(now)

    def touch(
        self, session_id: uuid.UUID | str, access_token: str | None, now: datetime | None = None
    ) -> ConversationSession:
        """Record activity on the session (``updated_at``). Adding a message does this too."""
        session = self._require_unexpired(self._authorize(session_id, access_token, now), now)
        with self._mapped_errors():
            return self._repo.touch_session(session.session_id, now)

    def close_conversation(
        self, session_id: uuid.UUID | str, access_token: str | None, now: datetime | None = None
    ) -> ConversationSession:
        """Close the session to new messages. Closing a closed session is a no-op that
        returns it unchanged; an expired one raises ``ConversationExpiredError``."""
        session = self._require_unexpired(self._authorize(session_id, access_token, now), now)
        if session.status is not SessionStatus.ACTIVE:
            return session
        with self._mapped_errors():
            closed = self._repo.set_status(session.session_id, SessionStatus.CLOSED, now)
        logger.info("Conversation closed: session_id=%s", session.session_id)
        return closed

    def delete_conversation(self, session_id: uuid.UUID | str, access_token: str | None) -> None:
        """Permanently delete the session and all its messages (also once expired)."""
        session = self._authorize(session_id, access_token)
        with self._mapped_errors():
            self._repo.delete_session(session.session_id)
        logger.info("Conversation deleted: session_id=%s", session.session_id)

    # --- Messages -----------------------------------------------------------------

    def get_history(
        self, session_id: uuid.UUID | str, access_token: str | None, now: datetime | None = None
    ) -> tuple[ConversationSession, list[ConversationMessage]]:
        """The session and its messages, oldest first."""
        session = self._require_unexpired(self._authorize(session_id, access_token, now), now)
        with self._mapped_errors():
            return self._repo.get_history(session.session_id)

    def get_messages_page(
        self,
        session_id: uuid.UUID | str,
        access_token: str | None,
        *,
        offset: int = 0,
        limit: int = 50,
        now: datetime | None = None,
    ) -> tuple[ConversationSession, list[ConversationMessage], int]:
        """The session, one page of its messages (oldest first) and the total count."""
        session = self._require_unexpired(self._authorize(session_id, access_token, now), now)
        with self._mapped_errors():
            return self._repo.get_messages_page(session.session_id, offset=offset, limit=limit)

    def list_messages(
        self, session_id: uuid.UUID | str, access_token: str | None
    ) -> list[ConversationMessage]:
        return self.get_history(session_id, access_token)[1]

    def add_user_message(
        self,
        session_id: uuid.UUID | str,
        access_token: str | None,
        content: str,
        now: datetime | None = None,
    ) -> ConversationMessage:
        """Append a customer message. Raises ``ConversationNotActiveError`` if the
        conversation is closed or expired."""
        session = self._require_active(self._authorize(session_id, access_token, now))
        with self._mapped_errors():
            return self._repo.add_user_message(session.session_id, content, now)

    def add_assistant_message(
        self,
        session_id: uuid.UUID | str,
        access_token: str | None,
        content: str,
        decision: Decision,
        source_faq_ids: Iterable[str] = (),
        now: datetime | None = None,
    ) -> ConversationMessage:
        """Append the reply to the customer's latest message.

        Authorised with the same token as the question. Allowed until the session is
        closed, so a question asked just before expiry still gets its reply stored.
        """
        session = self._authorize(session_id, access_token, now, record_expiry=False)
        with self._mapped_errors():
            return self._repo.add_assistant_message(
                session.session_id, content, decision, source_faq_ids, now
            )

    def record_exchange(
        self,
        session_id: uuid.UUID | str,
        access_token: str | None,
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
        request_fingerprint: str | None = None,
    ) -> tuple[ConversationMessage, ConversationMessage]:
        """Store a customer message and its reply in one transaction: both or neither.

        The conversation must have been active when the message was received; the reply
        may be stored after ``expires_at`` (the customer asked in time). The processing
        lease held with ``processing_key`` is released in the same transaction.

        Raises:
            DuplicateIdempotencyKeyError: (from the repository) the key was already used.
            ConversationChangedError: the lease was taken over by a newer message.
        """
        session = self._authorize(session_id, access_token, received_at)
        self._require_active(session)
        with self._mapped_errors():
            return self._repo.add_exchange(
                session.session_id,
                question,
                reply,
                decision,
                source_faq_ids,
                received_at=received_at,
                answered_at=answered_at,
                idempotency_key=idempotency_key,
                generation_method=generation_method,
                processing_key=processing_key,
                request_fingerprint=request_fingerprint,
            )

    # --- One message at a time ----------------------------------------------------

    def begin_turn(
        self,
        session_id: uuid.UUID | str,
        access_token: str | None,
        processing_key: str,
        now: datetime | None = None,
    ) -> ConversationSession:
        """Authorise and take the session's processing lease for one message.

        Raises:
            ConversationNotFoundError, ConversationNotActiveError (closed),
            ConversationExpiredError, or ConversationBusyError (the lease is held by
            another request; ``duplicate`` when that request used the same key).
        """
        now = now or utc_now()
        session = self._require_active(self._authorize(session_id, access_token, now))
        try:
            with self._mapped_errors():
                self._repo.claim_processing(
                    session.session_id, processing_key, now=now, lease=self.processing_lease
                )
        except SessionBusyError as exc:
            wait = max((exc.until - now).total_seconds(), 1.0)
            raise ConversationBusyError(retry_after_seconds=wait, duplicate=exc.same_key) from None
        return session

    def end_turn(self, session_id: uuid.UUID, processing_key: str) -> None:
        """Release the processing lease after a failed turn (a stored exchange releases
        it itself). Never raises: if this fails, the lease simply expires."""
        try:
            self._repo.release_processing(session_id, processing_key)
        except ConversationStoreError:
            logger.warning("Could not release processing lease: session_id=%s", session_id)

    def find_exchange(
        self, session_id: uuid.UUID | str, access_token: str | None, idempotency_key: str
    ) -> tuple[ConversationMessage, ConversationMessage] | None:
        """The exchange already stored under ``idempotency_key``, if any (authorised)."""
        session = self._require_unexpired(self._authorize(session_id, access_token), None)
        with self._mapped_errors():
            return self._repo.find_exchange(session.session_id, idempotency_key)

    # --- Retention ----------------------------------------------------------------

    def cleanup_expired(
        self,
        now: datetime | None = None,
        *,
        grace: timedelta = timedelta(0),
        dry_run: bool = False,
    ) -> int:
        """Delete every conversation whose ``expires_at`` is at least ``grace`` in the
        past, with all its messages. Returns how many were (or would be) deleted.

        Not tied to any request or token: for the cleanup command and startup only.
        """
        cutoff = (now or utc_now()) - grace
        count = self._repo.delete_expired_sessions(before=cutoff, dry_run=dry_run)
        if count and not dry_run:
            try:
                self._repo.checkpoint()  # do not leave deleted content in the WAL file
            except ConversationStoreError:
                logger.warning("WAL checkpoint after cleanup failed; SQLite will do it later")
        logger.info(
            "Conversation cleanup: %s %d expired conversation(s)",
            "would delete" if dry_run else "deleted",
            count,
        )
        return count

    # --- Access -------------------------------------------------------------------

    def _authorize(
        self,
        session_id: uuid.UUID | str,
        access_token: str | None,
        now: datetime | None = None,
        *,
        record_expiry: bool = True,
    ) -> ConversationSession:
        """The session if ``access_token`` grants access to it; otherwise
        ``ConversationNotFoundError``. Extend here for user or organisation access."""
        session: ConversationSession | None = None
        stored_hash: str | None = None
        parsed = _parse_session_id(session_id)
        if parsed is not None:
            try:
                session, stored_hash = self._repo.get_session_and_token_hash(parsed)
            except SessionNotFoundError:
                pass
        # Always runs, with a dummy hash when there is no session (same work either way).
        if not verify_access_token(access_token, stored_hash) or session is None:
            logger.info("Conversation access denied")
            raise ConversationNotFoundError()
        return self._expire_if_due(session, now) if record_expiry else session

    def _expire_if_due(
        self, session: ConversationSession, now: datetime | None
    ) -> ConversationSession:
        if session.status is not SessionStatus.ACTIVE or not session.is_expired(now):
            return session
        with self._mapped_errors():
            try:
                expired = self._repo.set_status(session.session_id, SessionStatus.EXPIRED, now)
            except ValueError:  # closed by another request in the meantime
                return self._repo.get_session(session.session_id)
        logger.info("Conversation expired: session_id=%s", session.session_id)
        return expired

    @staticmethod
    def _require_active(session: ConversationSession) -> ConversationSession:
        if session.status is SessionStatus.EXPIRED:
            raise ConversationExpiredError()
        if session.status is not SessionStatus.ACTIVE:
            raise ConversationNotActiveError(session.status)
        return session

    @staticmethod
    def _require_unexpired(
        session: ConversationSession, now: datetime | None
    ) -> ConversationSession:
        """Expired conversations (by status or by time, closed ones too) are not
        accessible; only deletion is."""
        if session.is_expired(now):
            raise ConversationExpiredError()
        return session

    @staticmethod
    @contextmanager
    def _mapped_errors() -> Iterator[None]:
        """Repository errors after authorisation: the session was deleted, closed or
        expired by a concurrent request."""
        try:
            yield
        except SessionNotFoundError:
            raise ConversationNotFoundError() from None
        except SessionNotActiveError as exc:
            if exc.status is SessionStatus.EXPIRED:
                raise ConversationExpiredError() from None
            raise ConversationNotActiveError(exc.status) from None
        except ProcessingLeaseLostError:
            raise ConversationChangedError() from None


def _parse_session_id(session_id: uuid.UUID | str) -> uuid.UUID | None:
    if isinstance(session_id, uuid.UUID):
        return session_id
    try:
        return uuid.UUID(session_id)
    except (ValueError, TypeError, AttributeError):
        return None
