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

Expiry: a session stops accepting customer messages at ``expires_at``. The first
authorised access after that records the status as ``expired``. Expired and closed
sessions stay readable, and deletable, with their token.

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
from app.models.conversation import (
    ConversationMessage,
    ConversationSession,
    SessionStatus,
)
from app.repositories.conversation_repository import (
    ConversationRepository,
    SessionNotActiveError,
    SessionNotFoundError,
)
from app.services.answer_decision_service import Decision

logger = logging.getLogger(__name__)


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


@dataclass(frozen=True)
class CreatedConversation:
    """A new session and its access token. The token is not shown in repr()."""

    session: ConversationSession
    access_token: str = field(repr=False)


class ConversationService:
    """Create, read, update and delete conversations, checking access on every call."""

    def __init__(self, repository: ConversationRepository, ttl: timedelta) -> None:
        if ttl <= timedelta(0):
            raise ValueError("ttl must be positive")
        self._repo = repository
        self.ttl = ttl

    @classmethod
    def from_settings(cls, settings: Settings, repository: ConversationRepository) -> Self:
        return cls(repository, timedelta(minutes=settings.conversation_ttl_minutes))

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
        """The session, with its expiry checked (and recorded) first."""
        return self._authorize(session_id, access_token, now)

    def is_expired(
        self, session_id: uuid.UUID | str, access_token: str | None, now: datetime | None = None
    ) -> bool:
        return self._authorize(session_id, access_token, now).is_expired(now)

    def touch(
        self, session_id: uuid.UUID | str, access_token: str | None, now: datetime | None = None
    ) -> ConversationSession:
        """Record activity on the session (``updated_at``). Adding a message does this too."""
        session = self._authorize(session_id, access_token, now)
        with self._mapped_errors():
            return self._repo.touch_session(session.session_id, now)

    def close_conversation(
        self, session_id: uuid.UUID | str, access_token: str | None, now: datetime | None = None
    ) -> ConversationSession:
        """Close the session to new messages. Closing a closed or expired session is a
        no-op that returns it unchanged."""
        session = self._authorize(session_id, access_token, now)
        if session.status is not SessionStatus.ACTIVE:
            return session
        with self._mapped_errors():
            closed = self._repo.set_status(session.session_id, SessionStatus.CLOSED, now)
        logger.info("Conversation closed: session_id=%s", session.session_id)
        return closed

    def delete_conversation(self, session_id: uuid.UUID | str, access_token: str | None) -> None:
        """Permanently delete the session and all its messages."""
        session = self._authorize(session_id, access_token)
        with self._mapped_errors():
            self._repo.delete_session(session.session_id)
        logger.info("Conversation deleted: session_id=%s", session.session_id)

    # --- Messages -----------------------------------------------------------------

    def get_history(
        self, session_id: uuid.UUID | str, access_token: str | None, now: datetime | None = None
    ) -> tuple[ConversationSession, list[ConversationMessage]]:
        """The session and its messages, oldest first."""
        session = self._authorize(session_id, access_token, now)
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
        session = self._authorize(session_id, access_token, now)
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
    ) -> tuple[ConversationMessage, ConversationMessage]:
        """Store a customer message and its reply in one transaction: both or neither.

        The conversation must have been active when the message was received; the reply
        may be stored after ``expires_at`` (the customer asked in time).
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
            )

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
        if session.status is not SessionStatus.ACTIVE:
            raise ConversationNotActiveError(session.status)
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
            raise ConversationNotActiveError(exc.status) from None


def _parse_session_id(session_id: uuid.UUID | str) -> uuid.UUID | None:
    if isinstance(session_id, uuid.UUID):
        return session_id
    try:
        return uuid.UUID(session_id)
    except (ValueError, TypeError, AttributeError):
        return None
