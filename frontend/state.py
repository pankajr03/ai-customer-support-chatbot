"""Per-browser-session chat state, kept in ``st.session_state``.

Everything one visitor's page needs between reruns lives in a single ``ChatState``
object under ``STATE_KEY``:

- the conversation's ``Credentials`` (session ID and access token), its status and
  expiry time;
- the messages shown on the page;
- the request status (idle, sending, failed), the pending message and its
  ``client_message_id``, and the last error;
- text to put back into the chat input after a message could not be sent.

Streamlit reruns the whole script on every interaction; ``st.session_state`` survives
reruns and is separate for every browser tab (every WebSocket session), so tokens are
never shared between visitors. Nothing here is put in ``st.cache_data`` or
``st.cache_resource`` (both are shared by all sessions), in URLs or in logs.

A backend conversation is started once when the page first opens (``should_auto_start``
is true only once per browser session, so reruns never create another), and is never
replaced automatically. After the customer deletes a conversation, none is started
until they ask for one or send a message.

The backend's stored history is the authoritative record of what was saved;
``messages`` is this page's view of it plus what is not saved (yet). They are reconciled
(``replace_history``) on "Refresh messages", after a reply when the page may be out of
date (a message was abandoned after a timeout, so the backend may have stored and
answered it) and after a replayed reply:

1. Stored messages are shown as the backend has them, in its order. They are matched to
   the page's own copies by stable IDs: a customer message by its ``client_message_id``,
   a reply by its ``message_id``. A match is shown once. (A backend older than Stage 9
   returns no ``client_message_id``; then an unsaved customer message is matched by its
   exact text, each at most once.)
2. A customer message the backend does not have is never dropped. It is shown after the
   stored conversation, still marked (not sent, may not have been sent, not answered
   yet), and can be sent again with its original ``client_message_id``.
3. A pending message the backend turns out to have stored (its reply was lost) is
   settled as sent, with its stored reply, instead of being sent again.

A message counts as sent only once its reply has arrived or the history shows it.

**This is not durable authentication.** Session state lives in the Streamlit server's
memory and is lost when the tab is closed or reloaded, the WebSocket reconnects to
another server, or the Streamlit server restarts. The access token cannot be recovered
(the backend returns it only once), so the conversation then cannot be reopened: the
page starts a new one, and the old one expires on the backend after
CONVERSATION_TTL_MINUTES. See frontend/README.md, "Session state and its limits".
"""

import unicodedata
import uuid
from collections.abc import MutableMapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
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
# Removed from the ends of a message, as the backend does (app.models.chat): zero-width
# space, non-joiner and joiner, word joiner, byte-order mark. Kept inside a message.
_INVISIBLE = "​‌‍⁠﻿"
# Control characters the backend accepts in a message; any other one is refused.
_ALLOWED_CONTROLS = frozenset("\t\n\r")


class RequestStatus(StrEnum):
    IDLE = "idle"
    SENDING = "sending"  # a message is queued or on its way; input is disabled
    FAILED = "failed"  # the last request failed; see ``last_error``


class RetryAction(StrEnum):
    """An action other than sending a message that failed and can be tried again."""

    START = "start"  # start the first conversation
    NEW_CONVERSATION = "new_conversation"
    CLOSE = "close"
    DELETE = "delete"
    RELOAD = "reload"


class Delivery(StrEnum):
    SENT = "sent"
    PENDING = "pending"  # queued, being sent, or waiting for a retry
    NOT_SENT = "not_sent"  # given up on; the backend did not store it
    # Given up on after a timeout or broken connection: the backend may have stored and
    # answered it. The page re-reads the history after the next reply to find out.
    UNCERTAIN = "uncertain"


# Customer messages not (known to be) stored, which can be sent again.
UNSENT = frozenset({Delivery.NOT_SENT, Delivery.UNCERTAIN})
# Failures after which the backend may have stored the message anyway.
UNCERTAIN_OUTCOME = frozenset(
    {ErrorKind.TIMEOUT, ErrorKind.NETWORK_ERROR, ErrorKind.BUSY, ErrorKind.INVALID_RESPONSE}
)


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
    # Replies: the backend's request ID, shown as a reference when search was unavailable.
    request_id: str | None = None


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
    # None: no backend conversation (not started yet, or deleted).
    conversation_status: ConversationStatus | None = None
    expires_at: datetime | None = None
    messages: list[ChatMessage] = field(default_factory=list)
    request_status: RequestStatus = RequestStatus.IDLE
    pending: PendingMessage | None = None
    last_error: ErrorNotice | None = None
    # The page has tried to start a conversation on its own (once per browser session).
    auto_start_done: bool = False
    # The action behind ``last_error``, if it can be tried again ("Try again"). A failed
    # message is retried through ``pending`` instead.
    retry_action: RetryAction | None = None
    # Text to put back into the chat input (a message that could not be sent).
    draft: str | None = None
    # The last message that could not be sent because the conversation ended; it is put
    # back into the input again when a new conversation starts.
    unsent_text: str | None = None
    # The backend's message limit, learned from a 422 if it is lower than configured.
    backend_max_chars: int | None = None
    # The page may differ from the backend's history; re-read it after the next reply.
    needs_sync: bool = False
    # Earlier messages not shown because the history was longer than the page loads.
    history_omitted: int = 0

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
        """A failed message can be sent again (with the same ``client_message_id``)."""
        return (
            self.pending is not None
            and self.request_status is RequestStatus.FAILED
            and not self.has_ended
        )

    @property
    def can_retry_start(self) -> bool:
        return self.action_to_retry is RetryAction.START and self.credentials is None

    @property
    def action_to_retry(self) -> RetryAction | None:
        """What "Try again" should repeat, when it isn't resending a message."""
        if self.is_sending or self.last_error is None or self.can_retry:
            return None
        return self.retry_action

    @property
    def should_auto_start(self) -> bool:
        """True only on the first run of a browser session without a conversation."""
        return not self.auto_start_done and self.credentials is None and not self.has_ended

    def max_chars(self, configured: int) -> int:
        return min(configured, self.backend_max_chars or configured)

    # -- Conversation lifecycle --------------------------------------------------------

    def start(self, created: ConversationCreated) -> None:
        """Use a conversation the backend just created (on an empty or reset page)."""
        self.credentials = created.credentials()
        self.conversation_status = created.status
        self.expires_at = created.expires_at
        self.retry_action = None
        self.last_error = None

    def record_start_failure(
        self, error: ApiError, action: RetryAction = RetryAction.START
    ) -> None:
        """Starting a conversation failed; the current one (if any) is kept. Starting
        the first one can always be tried again; a replacement only after a temporary
        failure."""
        self.note_error(error, retry=action)
        if action is RetryAction.START:
            self.retry_action = action

    def replace_conversation(self, created: ConversationCreated) -> None:
        """Switch to a new backend conversation, clearing the page. A message that could
        not be sent in the old one goes back into the input, ready to send."""
        unsent = self.unsent_text or (self.pending.text if self.pending else None)
        self.reset()
        self.start(created)
        self.draft = unsent

    def check_expiry(self, now: datetime | None = None) -> None:
        """Mark an active conversation expired once its ``expires_at`` has passed.

        The credentials are kept: the backend still accepts DELETE after expiry.
        """
        if (
            self.conversation_status is ConversationStatus.ACTIVE
            and self.expires_at is not None
            and (now or datetime.now(UTC)) >= self.expires_at
        ):
            self.conversation_status = ConversationStatus.EXPIRED
            self._withdraw_pending()

    def mark_closed(self, info: ConversationInfo) -> None:
        self.conversation_status = info.status
        self.expires_at = info.expires_at
        self._withdraw_pending()
        self.request_status = RequestStatus.IDLE
        self.last_error = None
        self.retry_action = None

    def reset(self) -> None:
        """Forget the conversation: credentials, messages, pending message and errors.

        ``auto_start_done`` and the learned message limit are kept, so a reset page does
        not start a conversation on its own.
        """
        self.credentials = None
        self.conversation_status = None
        self.expires_at = None
        self.messages = []
        self.pending = None
        self.request_status = RequestStatus.IDLE
        self.last_error = None
        self.retry_action = None
        self.draft = None
        self.unsent_text = None
        self.needs_sync = False
        self.history_omitted = 0

    # -- Messages ----------------------------------------------------------------------

    def queue_message(self, text: str, *, max_chars: int | None = None) -> PendingMessage | None:
        """Add a customer message to send. Returns None if it is not sent.

        Blank messages are ignored. A message over ``max_chars``, or with control
        characters other than tabs and line breaks (the backend's rules), is not sent:
        it goes back into the input with an explanation. A new message gets a new
        ``client_message_id``; a failed message still waiting for a retry is given up on
        (marked not sent) when the customer sends a new one.
        """
        text = strip_message(text)
        if not text or not self.can_send:
            return None
        limit = self.max_chars(max_chars) if max_chars else self.backend_max_chars
        if limit is not None and len(text) > limit:
            self._refuse(text, "string_too_long", max_length=limit)
            return None
        if any(unicodedata.category(c) == "Cc" and c not in _ALLOWED_CONTROLS for c in text):
            self._refuse(text, "invalid_characters")
            return None
        self._give_up_pending()
        return self._send(PendingMessage(client_message_id=uuid.uuid4(), text=text))

    def resend(self, message_id: str) -> PendingMessage | None:
        """Send a customer message that was not sent (or may not have been) again, with
        its original ``client_message_id``: if the backend did store it, the stored
        reply comes back instead of a second answer. Returns None if that message cannot
        be sent again (not found, already answered, or the page can't send now)."""
        message = next(
            (
                m
                for m in self.messages
                if m.role == "user" and m.message_id == message_id and m.delivery in UNSENT
            ),
            None,
        )
        if message is None or not self.can_send:
            return None
        self._give_up_pending()
        self.messages.remove(message)
        # It goes where the backend will store it: after the stored conversation.
        return self._send(
            PendingMessage(client_message_id=uuid.UUID(message_id), text=message.content)
        )

    def retry_pending(self) -> bool:
        """Send the failed message again, with the same ``client_message_id``."""
        if not self.can_retry:
            return False
        self.request_status = RequestStatus.SENDING
        self.last_error = None
        return True

    def record_reply(self, reply: ChatReply) -> None:
        if self.pending is None:  # pragma: no cover - only called while a message is pending
            return
        if any(message.message_id == str(reply.message_id) for message in self.messages):
            # A replay of an exchange already on the page (it was loaded with the
            # history, under the backend's IDs): drop the duplicate customer bubble.
            self._remove_user_message(self.pending.client_message_id)
        else:
            self._set_delivery(self.pending.client_message_id, Delivery.SENT)
            self.messages.append(
                ChatMessage(
                    role="assistant",
                    content=reply.answer,
                    message_id=str(reply.message_id),
                    decision=reply.decision,
                    generation_method=reply.generation_method,
                    sources=tuple(reply.sources),
                    request_id=reply.request_id,
                )
            )
        self.pending = None
        self.request_status = RequestStatus.IDLE
        self.last_error = None

    def record_failure(self, error: ApiError) -> None:
        """Remember a failed request and update the conversation for what it means.

        A retryable failure keeps the pending message (and its ID) for a retry. After
        any other failure the backend has not stored the message: it is taken off the
        page and put back into the input. An expired conversation keeps its
        credentials (it can still be deleted); an unknown or refused one (404, 401,
        403) loses them; a closed one keeps them (its history stays readable). An
        ended conversation keeps the unsent text for the next conversation.
        """
        self.last_error = ErrorNotice.from_error(error)
        self.retry_action = None  # a failed message is retried through ``pending``
        self.request_status = RequestStatus.FAILED
        if error.kind is ErrorKind.SESSION_EXPIRED:
            self.conversation_status = ConversationStatus.EXPIRED
        elif error.ends_conversation:
            self.credentials = None
            self.conversation_status = ConversationStatus.EXPIRED
        elif error.kind is ErrorKind.SESSION_CLOSED:
            self.conversation_status = ConversationStatus.CLOSED
        if self.has_ended and self.pending is not None:
            self.unsent_text = self.pending.text
        if error.kind is ErrorKind.INVALID_MESSAGE and error.max_length:
            self.backend_max_chars = error.max_length
        if not error.retryable or self.has_ended:
            self._withdraw_pending()

    def replace_history(self, history: ConversationHistory, credentials: Credentials) -> bool:
        """Reconcile the page with the backend's history (see the module docstring).

        Applied only if the page still shows the conversation it was loaded for, so
        histories of different conversations never mix. How a reply was produced is kept
        from this page's own copy of the same reply (the history does not say); sources
        too, as the customer saw them. A reply this page never received gets the sources
        the backend lists for it; nothing is invented. Returns False if the history was
        not applied.
        """
        if self.credentials != credentials or history.session_id != credentials.session_id:
            return False
        local = {message.message_id: message for message in self.messages}
        # For backends that don't return client_message_id: unsaved customer messages,
        # matched by their exact text instead, each at most once.
        unsaved_by_text: dict[str, list[str]] = {}
        for message in self.messages:
            if message.role == "user" and message.delivery is not Delivery.SENT:
                unsaved_by_text.setdefault(message.content, []).append(message.message_id)
        messages = []
        for stored in history.messages:
            # Customer messages are known on the page by their client_message_id.
            key = str(stored.client_message_id or stored.message_id)
            if (
                stored.role == "user"
                and stored.client_message_id is None
                and unsaved_by_text.get(stored.content)
            ):
                key = unsaved_by_text[stored.content].pop(0)
            own = local.get(key)
            messages.append(
                ChatMessage(
                    role=stored.role,
                    content=stored.content,
                    message_id=key,
                    decision=stored.decision,
                    generation_method=own.generation_method if own else None,
                    request_id=own.request_id if own else None,
                    sources=own.sources
                    if own
                    else tuple(stored.sources) or self._known_sources(stored.source_faq_ids),
                )
            )
        saved = {message.message_id for message in messages}
        # Never drop what the customer wrote: messages the backend doesn't have stay,
        # with their delivery mark, after the stored conversation.
        messages += [
            message
            for message in self.messages
            if message.role == "user"
            and message.delivery is not Delivery.SENT
            and message.message_id not in saved
        ]
        if self.pending is not None and str(self.pending.client_message_id) in saved:
            # Stored after all (its reply was lost on the way): settled, not resent.
            self.pending = None
            self.draft = None
        self.messages = messages
        self.history_omitted = history.omitted
        self.conversation_status = history.status
        self.needs_sync = False
        if self.pending is None:
            self.request_status = RequestStatus.IDLE
            self.last_error = None
        return True

    def note_error(self, error: ApiError, *, retry: RetryAction | None = None) -> None:
        """Show an error without changing the conversation or the pending message.
        ``retry`` is offered as "Try again" if the failure was temporary."""
        self.last_error = ErrorNotice.from_error(error)
        self.retry_action = retry if error.retryable else None

    def fail_without_request(self, kind: ErrorKind) -> None:
        """Show an error for an action that could not be attempted."""
        self.last_error = ErrorNotice(kind=kind)
        if self.request_status is RequestStatus.SENDING:
            self.request_status = RequestStatus.FAILED
            self._withdraw_pending()

    def take_draft(self) -> str | None:
        """The text to put back into the chat input, once."""
        draft, self.draft = self.draft, None
        return draft

    # -- Helpers -----------------------------------------------------------------------

    def _send(self, pending: PendingMessage) -> PendingMessage:
        """Show ``pending`` as a customer message waiting for its reply, and send it."""
        self.unsent_text = None
        self.messages.append(
            ChatMessage(
                role="user",
                content=pending.text,
                message_id=str(pending.client_message_id),
                delivery=Delivery.PENDING,
            )
        )
        self.pending = pending
        self.request_status = RequestStatus.SENDING
        self.last_error = None
        return pending

    def _refuse(self, text: str, rule: str, *, max_length: int | None = None) -> None:
        """A message that can't be sent goes back into the input, with the reason."""
        self.draft = text
        self.last_error = ErrorNotice(
            ErrorKind.INVALID_MESSAGE, validation_types=(rule,), max_length=max_length
        )

    def _give_up_pending(self) -> None:
        """The customer moved on: the unsent message stays visible, marked not sent, or
        "may not have been sent" if the backend could have stored it (then the history
        is re-read after the next reply, to show what really happened)."""
        if self.pending is None:
            return
        uncertain = self.last_error is not None and self.last_error.kind in UNCERTAIN_OUTCOME
        delivery = Delivery.UNCERTAIN if uncertain else Delivery.NOT_SENT
        self._set_delivery(self.pending.client_message_id, delivery)
        self.needs_sync = self.needs_sync or uncertain
        self.pending = None

    def _remove_user_message(self, client_message_id: uuid.UUID) -> None:
        """Remove the unsaved bubble of a customer message (never a stored one)."""
        self.messages = [
            message
            for message in self.messages
            if not (
                message.role == "user"
                and message.message_id == str(client_message_id)
                and message.delivery is not Delivery.SENT
            )
        ]

    def _withdraw_pending(self) -> None:
        """The message was not stored: take it off the page, back into the input. If the
        conversation has ended, it is also kept for the next conversation."""
        if self.pending is None:
            return
        self._remove_user_message(self.pending.client_message_id)
        self.draft = self.pending.text
        if self.has_ended:
            self.unsent_text = self.pending.text
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


def strip_message(text: str) -> str:
    """Whitespace and invisible characters removed from both ends, nothing else (the
    backend's rule, so the length checked here is the length the backend checks)."""
    while (stripped := text.strip().strip(_INVISIBLE)) != text:
        text = stripped
    return text
