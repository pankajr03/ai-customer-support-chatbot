"""Per-browser-session chat state, kept in ``st.session_state``.

Everything one visitor's page needs between reruns lives in a single ``ChatState``
object under ``STATE_KEY``:

- the conversation's ``Credentials`` (session ID and access token), its status and
  expiry time;
- the messages shown on the page;
- the request status (idle, sending, failed), the pending message and its
  ``client_message_id``, and the last error.

Streamlit reruns the whole script on every interaction; ``st.session_state`` survives
reruns and is separate for every browser tab (every WebSocket session), so tokens are
never shared between visitors. Nothing here is put in ``st.cache_data`` or
``st.cache_resource`` (both are shared by all sessions), in URLs or in logs.

A backend conversation is created lazily, when the first message is sent, so reruns,
page loads and visitors who never type create none.

**This is not durable authentication.** Session state lives in the Streamlit server's
memory and is lost when the tab is closed or reloaded, the WebSocket reconnects to
another server, or the Streamlit server restarts. The access token cannot be recovered
(the backend returns it only once), so the conversation then cannot be reopened: the
page starts a new one, and the old one expires on the backend after
CONVERSATION_TTL_MINUTES. See frontend/README.md, "Session state and its limits".
"""

import uuid
from collections.abc import MutableMapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

import streamlit as st

from frontend.api_client import (
    RETRYABLE,
    ApiError,
    ChatReply,
    ConversationCreated,
    ConversationHistory,
    ConversationInfo,
    ConversationStatus,
    Credentials,
    Decision,
    ErrorKind,
    SourceReference,
)

STATE_KEY = "support_chat"


class RequestStatus(StrEnum):
    IDLE = "idle"
    SENDING = "sending"  # a message is queued or on its way; input is disabled
    FAILED = "failed"  # the last request failed; see ``last_error``


class Delivery(StrEnum):
    SENT = "sent"
    PENDING = "pending"  # queued, being sent, or waiting for a retry
    NOT_SENT = "not_sent"  # given up on; the backend did not store it


@dataclass
class ChatMessage:
    role: Literal["user", "assistant"]
    content: str
    # User messages: the client_message_id. Assistant messages: the backend's message ID.
    message_id: str
    decision: Decision | None = None
    generation_method: str | None = None
    sources: tuple[SourceReference, ...] = ()
    delivery: Delivery = Delivery.SENT


@dataclass(frozen=True)
class PendingMessage:
    """A customer message not yet answered. Retries reuse its ``client_message_id``."""

    client_message_id: uuid.UUID
    text: str


@dataclass(frozen=True)
class ErrorNotice:
    """What the page shows about the last failure (no token, no message text)."""

    kind: ErrorKind
    request_id: str | None = None
    retry_after: float | None = None
    max_length: int | None = None
    validation_types: tuple[str, ...] = ()

    @property
    def retryable(self) -> bool:
        return self.kind in RETRYABLE

    @classmethod
    def from_error(cls, error: ApiError) -> "ErrorNotice":
        return cls(
            kind=error.kind,
            request_id=error.request_id,
            retry_after=error.retry_after,
            max_length=error.max_length,
            validation_types=error.validation_types,
        )


@dataclass
class ChatState:
    credentials: Credentials | None = None
    # None: no backend conversation yet (one is created with the first message).
    conversation_status: ConversationStatus | None = None
    expires_at: datetime | None = None
    messages: list[ChatMessage] = field(default_factory=list)
    request_status: RequestStatus = RequestStatus.IDLE
    pending: PendingMessage | None = None
    last_error: ErrorNotice | None = None

    # -- Questions the page asks -------------------------------------------------------

    @property
    def is_sending(self) -> bool:
        return self.request_status is RequestStatus.SENDING

    @property
    def has_ended(self) -> bool:
        """Closed, expired or lost: no more messages in this conversation."""
        return self.conversation_status in (ConversationStatus.CLOSED, ConversationStatus.EXPIRED)

    @property
    def can_send(self) -> bool:
        return not self.is_sending and not self.has_ended

    @property
    def can_retry(self) -> bool:
        return (
            self.pending is not None
            and self.request_status is RequestStatus.FAILED
            and not self.has_ended
        )

    # -- Changes -----------------------------------------------------------------------

    def queue_message(self, text: str) -> PendingMessage | None:
        """Add a customer message to send. Returns None if it is blank or not allowed now.

        A new message gets a new ``client_message_id``. A failed message that was waiting
        for a retry is given up on (marked not sent) when the customer types a new one.
        """
        text = text.strip()
        if not text or not self.can_send:
            return None
        self._give_up_pending()
        pending = PendingMessage(client_message_id=uuid.uuid4(), text=text)
        self.messages.append(
            ChatMessage(
                role="user",
                content=text,
                message_id=str(pending.client_message_id),
                delivery=Delivery.PENDING,
            )
        )
        self.pending = pending
        self.request_status = RequestStatus.SENDING
        self.last_error = None
        return pending

    def retry_pending(self) -> bool:
        """Send the failed message again, with the same ``client_message_id``."""
        if not self.can_retry:
            return False
        self.request_status = RequestStatus.SENDING
        self.last_error = None
        return True

    def start(self, created: ConversationCreated) -> None:
        self.credentials = created.credentials()
        self.conversation_status = created.status
        self.expires_at = created.expires_at

    def record_reply(self, reply: ChatReply) -> None:
        if self.pending is None:  # pragma: no cover - only called while a message is pending
            return
        self._set_delivery(self.pending.client_message_id, Delivery.SENT)
        self.messages.append(
            ChatMessage(
                role="assistant",
                content=reply.answer,
                message_id=str(reply.message_id),
                decision=reply.decision,
                generation_method=reply.generation_method,
                sources=tuple(reply.sources),
            )
        )
        self.pending = None
        self.request_status = RequestStatus.IDLE
        self.last_error = None

    def record_failure(self, error: ApiError) -> None:
        """Remember a failed request and update the conversation for what it means.

        A retryable failure keeps the pending message (and its ID) for a retry. Any
        other failure gives it up. Expired or unknown conversations drop the
        credentials; closed ones keep them (the history stays readable).
        """
        self.last_error = ErrorNotice.from_error(error)
        self.request_status = RequestStatus.FAILED
        if error.ends_conversation:
            self.forget_credentials()
        elif error.kind is ErrorKind.SESSION_CLOSED:
            self.conversation_status = ConversationStatus.CLOSED
        if not error.retryable or self.has_ended:
            self._give_up_pending()

    def mark_closed(self, info: ConversationInfo) -> None:
        self.conversation_status = info.status
        self.expires_at = info.expires_at
        self._give_up_pending()
        self.request_status = RequestStatus.IDLE

    def replace_history(self, history: ConversationHistory) -> None:
        """Show the backend's copy of the conversation (e.g. after a lost reply)."""
        self.conversation_status = history.status
        self.messages = [
            ChatMessage(
                role=message.role,
                content=message.content,
                message_id=str(message.message_id),
                decision=message.decision,
                # History has FAQ IDs only; their questions are known for this page's own
                # replies, so keep those references where the IDs match.
                sources=self._known_sources(message.source_faq_ids),
            )
            for message in history.messages
        ]
        self.pending = None
        self.request_status = RequestStatus.IDLE
        self.last_error = None

    def forget_credentials(self) -> None:
        """The conversation can no longer be used (expired or not found)."""
        self.credentials = None
        self.conversation_status = ConversationStatus.EXPIRED

    def reset(self) -> None:
        """Forget everything: credentials, messages, pending message and errors."""
        self.credentials = None
        self.conversation_status = None
        self.expires_at = None
        self.messages = []
        self.pending = None
        self.request_status = RequestStatus.IDLE
        self.last_error = None

    def fail_without_request(self, kind: ErrorKind) -> None:
        """Show an error for an action that could not be attempted."""
        self.last_error = ErrorNotice(kind=kind)
        if self.request_status is RequestStatus.SENDING:
            self.request_status = RequestStatus.FAILED

    # -- Helpers -----------------------------------------------------------------------

    def _give_up_pending(self) -> None:
        if self.pending is not None:
            self._set_delivery(self.pending.client_message_id, Delivery.NOT_SENT)
            self.pending = None

    def _set_delivery(self, client_message_id: uuid.UUID, delivery: Delivery) -> None:
        for message in reversed(self.messages):
            if message.role == "user" and message.message_id == str(client_message_id):
                message.delivery = delivery
                return

    def _known_sources(self, faq_ids: list[str]) -> tuple[SourceReference, ...]:
        known = {source.faq_id: source for message in self.messages for source in message.sources}
        return tuple(known[faq_id] for faq_id in faq_ids if faq_id in known)


def get_chat_state(store: MutableMapping[str, Any] | None = None) -> ChatState:
    """This browser session's chat state, created on first use.

    ``store`` defaults to ``st.session_state``; tests pass a plain dict.
    """
    if store is None:
        store = st.session_state
    if STATE_KEY not in store:
        store[STATE_KEY] = ChatState()
    return store[STATE_KEY]
