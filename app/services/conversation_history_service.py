"""Recent conversation history, as context for answering the next message.

Strategy: the most recent messages only (no summaries, no vector or long-term memory).
The window is bounded twice, both configurable:

- at most ``max_messages`` messages (``CONVERSATION_HISTORY_MAX_MESSAGES``), and
- at most ``max_chars`` characters of message text (``CONVERSATION_HISTORY_MAX_CHARS``).

Older messages are dropped first, and a message is either included whole or not at all:
the window is the longest run of most recent messages that fits both limits. Only
``max_messages + 1`` rows are read from the database (the extra one tells whether older
messages exist), never the whole conversation.

Access: the caller must hold the conversation's access token (checked by
``ConversationService``). Unknown, unauthorised, closed and expired conversations give
no history, and the messages always come from that one conversation only.

Trust: history is conversation context, **not knowledge**. Customer turns are untrusted
input. Assistant turns are what was said before; they may be paraphrased, outdated or
wrong, and must never be used as evidence for business facts (fees, policies, times).
Facts come only from FAQs retrieved for the current question. An assistant turn's
``source_faq_ids`` say which FAQs it cited, so they can be looked up again in the
knowledge base, whose current text is authoritative, instead of reusing the reply.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Self

from app.core.config import Settings
from app.models.conversation import ConversationMessage, MessageRole, SessionStatus
from app.repositories.conversation_repository import (
    ConversationRepository,
    ConversationStoreError,
    SessionNotFoundError,
)
from app.services.answer_decision_service import Decision
from app.services.conversation_service import (
    ConversationNotActiveError,
    ConversationNotFoundError,
    ConversationService,
)


@dataclass(frozen=True)
class HistoryTurn:
    """One earlier message. Conversation context only, never evidence (see module doc)."""

    role: MessageRole
    content: str
    sequence: int
    # Assistant turns only: the earlier decision (e.g. "ambiguous" when a clarifying
    # question was asked) and the FAQs that reply cited, as IDs to look up again.
    decision: Decision | None = None
    source_faq_ids: tuple[str, ...] = ()

    @property
    def is_from_customer(self) -> bool:
        return self.role is MessageRole.USER

    @classmethod
    def from_message(cls, message: ConversationMessage) -> Self:
        return cls(
            role=message.role,
            content=message.content,
            sequence=message.sequence,
            decision=message.decision,
            source_faq_ids=message.source_faq_ids,
        )


@dataclass(frozen=True)
class RecentHistory:
    """The recent messages of one conversation, oldest first."""

    session_id: uuid.UUID
    turns: tuple[HistoryTurn, ...]
    # True when older messages exist that were left out by either limit.
    omitted_older: bool

    @property
    def total_chars(self) -> int:
        return sum(len(turn.content) for turn in self.turns)

    def __len__(self) -> int:
        return len(self.turns)


class ConversationHistoryService:
    """Loads a bounded window of recent messages for an authorised, active conversation."""

    def __init__(
        self,
        conversations: ConversationService,
        repository: ConversationRepository,
        *,
        max_messages: int,
        max_chars: int,
    ) -> None:
        if max_messages < 1:
            raise ValueError("max_messages must be at least 1")
        if max_chars < 1:
            raise ValueError("max_chars must be at least 1")
        self._conversations = conversations
        self._repo = repository
        self.max_messages = max_messages
        self.max_chars = max_chars

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        conversations: ConversationService,
        repository: ConversationRepository,
    ) -> Self:
        return cls(
            conversations,
            repository,
            max_messages=settings.conversation_history_max_messages,
            max_chars=settings.conversation_history_max_chars,
        )

    def load_recent(
        self,
        session_id: uuid.UUID | str,
        access_token: str | None,
        *,
        before_sequence: int | None = None,
        now: datetime | None = None,
    ) -> RecentHistory:
        """Recent history of an active conversation, oldest first.

        Args:
            before_sequence: Leave out this message and later ones, typically the
                customer message being answered (it is the question, not history).

        Raises:
            ConversationNotFoundError: unknown session, or missing/wrong token.
            ConversationNotActiveError: the conversation is closed or expired.
            ConversationStoreError: the database failed.
        """
        session = self._conversations.get_conversation(session_id, access_token, now)
        if session.status is not SessionStatus.ACTIVE:
            raise ConversationNotActiveError(session.status)
        try:
            # One extra row shows whether anything older exists.
            recent = self._repo.list_recent_messages(
                session.session_id, self.max_messages + 1, before_sequence
            )
        except SessionNotFoundError:  # deleted by a concurrent request
            raise ConversationNotFoundError() from None
        if any(message.session_id != session.session_id for message in recent):
            raise ConversationStoreError("conversation history is inconsistent")

        window = self._fit(recent[-self.max_messages :])
        return RecentHistory(
            session_id=session.session_id,
            turns=tuple(HistoryTurn.from_message(message) for message in window),
            omitted_older=len(window) < len(recent),
        )

    def _fit(self, messages: list[ConversationMessage]) -> list[ConversationMessage]:
        """The longest run of most recent messages within the character budget."""
        kept: list[ConversationMessage] = []
        used = 0
        for message in reversed(messages):
            used += len(message.content)
            if used > self.max_chars:
                break
            kept.append(message)
        return kept[::-1]
