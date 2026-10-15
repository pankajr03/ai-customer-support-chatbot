"""One conversational turn: history and rewriting around the unchanged Stage 4/5 pipeline.

Flow for a new customer message:

1. Check the session and its access token, and load recent history
   (``ConversationHistoryService``: bounded, authorised, active sessions only).
2. Rewrite the message into a standalone search query only if it depends on the
   conversation (``QueryRewriteService``). The customer's message itself is kept as is.
3. Run the existing grounded-answer pipeline (``GroundedAnswerService``) on that query:
   Stage 4 hybrid retrieval and answer decision, approved FAQ evidence, then the optional
   Stage 5 LLM rewrite with its validation and fallbacks.
4. Store the customer message and the reply together, then return both.

What history may and may not do
-------------------------------
- History only helps turn "what about after that?" into a searchable question. The
  rewritten query then goes through the same retrieval and acceptance rules as any
  other question: rewriting cannot approve an answer, and a rewritten question that
  matches nothing still gets the no-match message.
- History is never FAQ evidence and is not sent to the answer generator at all: the
  generator receives only the standalone question and the approved FAQs Stage 4
  selected. Earlier replies therefore cannot become a source of facts.
- The stored reply is the text the customer saw, with the Stage 4 decision and its
  source FAQ IDs. It is conversation history, not knowledge: nothing is ever added to
  the FAQ knowledge base from a conversation.

Failures
--------
- Unknown session or wrong token: ``ConversationNotFoundError``; nothing is stored.
- Closed or expired conversation: ``ConversationNotActiveError``; nothing is stored.
- Query rewriting fails: the original message is searched instead.
- The LLM fails or its reply is rejected: the existing Stage 5 fallbacks apply (the
  approved FAQ answer verbatim, or the safe Stage 4 message).
- FAQ knowledge base unavailable (``FAQDataError``): raised before anything is stored.
- The database fails while storing: ``ConversationStoreError``. The message and reply
  are written in one transaction, so either both are stored or neither is, and the
  caller is told; the history never holds a question without its answer or a reply
  without its question.
"""

import hashlib
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime

from app.models.answer import generation_method, usage_summary
from app.models.conversation import ConversationChatResponse, utc_now
from app.models.faq import FAQCategory
from app.services.answer_decision_service import AnswerDecision
from app.services.conversation_history_service import ConversationHistoryService, RecentHistory
from app.services.conversation_service import ConversationService
from app.services.grounded_answer_service import GroundedAnswerService
from app.services.query_rewrite_service import (
    QueryRewrite,
    QueryRewriteService,
    RewriteMethod,
    RewriteReason,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ChatTurn:
    """The API response plus internal details (for headers, logs and tests only)."""

    response: ConversationChatResponse
    decision: AnswerDecision  # e.g. Retry-After while search is degraded
    rewrite: QueryRewrite  # never shown to customers


class ConversationChatService:
    """Answers a message within a conversation and records the exchange."""

    def __init__(
        self,
        conversations: ConversationService,
        history: ConversationHistoryService,
        rewriter: QueryRewriteService,
        grounded: GroundedAnswerService,
    ) -> None:
        self._conversations = conversations
        self._history = history
        self._rewriter = rewriter
        self._grounded = grounded

    def chat(
        self,
        session_id: uuid.UUID | str,
        access_token: str | None,
        message: str,
        *,
        category: FAQCategory | str | None = None,
        now: datetime | None = None,
    ) -> ChatTurn:
        """Answer ``message`` in the conversation and store the exchange.

        Raises:
            ConversationNotFoundError: unknown session, or missing/wrong token.
            ConversationNotActiveError: the conversation is closed or expired.
            ConversationStoreError: the database failed (nothing partial is stored).
            FAQDataError: the FAQ knowledge base is unavailable.
            ValueError: invalid message or unknown category.
        """
        received_at = now or utc_now()
        history = self._history.load_recent(session_id, access_token, now=received_at)
        rewrite = self._rewrite(message, history)

        grounded = self._grounded.answer(
            rewrite.query,
            category=category,
            safety_identifier=_safety_identifier(history.session_id),
        )
        result = grounded.result

        user, assistant = self._conversations.record_exchange(
            history.session_id,
            access_token,
            rewrite.original,
            result.answer,
            grounded.decision.decision,
            result.source_faq_ids,
            received_at=received_at,
            answered_at=now or utc_now(),  # a fixed clock (tests) is used for both
        )
        logger.info(
            "event=conversation_turn session_id=%s history_messages=%d rewrite=%s/%s "
            "decision=%s outcome=%s",
            history.session_id,
            len(history),
            rewrite.method.value,
            rewrite.reason.value,
            grounded.decision.decision.value,
            result.outcome.value,
        )
        response = ConversationChatResponse.from_records(
            user, assistant, generation_method(result), usage_summary(result)
        )
        return ChatTurn(response=response, decision=grounded.decision, rewrite=rewrite)

    def _rewrite(self, message: str, history: RecentHistory) -> QueryRewrite:
        """The search query; the original message if rewriting fails in any way."""
        try:
            return self._rewriter.rewrite(message, history)
        except Exception as exc:  # noqa: BLE001 - rewriting is optional; search the message
            logger.error("Query rewrite failed (%s); using the original", type(exc).__name__)
            original = " ".join(message.split())
            return QueryRewrite(
                original, original, RewriteMethod.UNCHANGED, RewriteReason.UNRESOLVED
            )


def _safety_identifier(session_id: uuid.UUID) -> str:
    """A stable, non-identifying ID for OpenAI abuse detection (never the raw session ID)."""
    return hashlib.sha256(f"conversation:{session_id}".encode()).hexdigest()[:32]
