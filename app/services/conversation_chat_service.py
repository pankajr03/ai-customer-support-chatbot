"""One conversational turn: history and rewriting around the unchanged Stage 4/5 pipeline.

Flow for a new customer message:

0. With an ``Idempotency-Key`` that was already used in this conversation, return the
   stored exchange instead of answering again (a retried request).
1. Check the session and its access token and take its processing lease, so only one
   message per conversation is answered at a time (``ConversationService.begin_turn``).
2. Load recent history (``ConversationHistoryService``: bounded, authorised).
3. Rewrite the message into a standalone search query only if it depends on the
   conversation (``QueryRewriteService``). The customer's message itself is kept as is.
4. Run the existing grounded-answer pipeline (``GroundedAnswerService``) on that query:
   Stage 4 hybrid retrieval and answer decision, approved FAQ evidence, then the optional
   Stage 5 LLM rewrite with its validation and fallbacks.
5. Store the customer message and the reply together and release the lease, in one
   transaction; then return both.

Every database step is its own short transaction. None is open while retrieval or the
LLM runs, so a slow model never holds the database lock.

What history may and may not do
-------------------------------
- History only helps turn "what about after that?" into a searchable question. The
  rewritten query then goes through the same retrieval and acceptance rules as any
  other question: rewriting cannot approve an answer, and a rewritten question that
  matches nothing still gets the no-match message.
- History is never FAQ evidence and is not sent to the answer generator at all: the
  generator receives only the standalone question and the approved FAQs Stage 4
  selected. Earlier replies therefore cannot become a source of facts.
- The stored reply is the text the customer saw, with the decision describing it (see
  ``decision_for_outcome``: normally Stage 4's), its source FAQ IDs and how it was
  produced. It is conversation history, not knowledge: nothing is
  ever added to the FAQ knowledge base from a conversation.

The response (``CustomerChatResponse``)
--------------------------------------
Sources are listed with their approved FAQ question, taken from this turn's evidence
(the FAQs Stage 4 selected). A source ID that is not in that evidence is dropped and
logged, never shown. A replayed turn looks its sources up in the approved knowledge base
(``faq_lookup``); FAQs withdrawn since are left out of ``sources``.

Failures (what is stored, and what the client should do)
--------------------------------------------------------
- Unknown session or wrong token: ``ConversationNotFoundError``; nothing stored.
- Closed: ``ConversationNotActiveError``; expired: ``ConversationExpiredError``;
  nothing stored.
- Another message is being answered: ``ConversationBusyError``; nothing stored; retry
  after ``retry_after_seconds``. With the same ``Idempotency-Key`` this is a duplicate
  submission still in progress: retrying later returns its stored result.
- Same ``Idempotency-Key`` with a different message: ``IdempotencyKeyMismatchError``.
- Query rewriting fails: the original message is searched instead.
- The LLM times out, fails or its reply is rejected: the existing Stage 5 fallbacks
  apply (the approved FAQ answer verbatim, or the safe Stage 4 message). Retrieval
  failures give Stage 4's safe "search unavailable" message. Both are stored normally.
- FAQ knowledge base unavailable (``FAQDataError``): nothing stored; the lease is
  released.
- The database fails while storing: ``ConversationStoreError``; message and reply are
  one transaction, so neither is stored; the lease is released (or expires). Retrying
  with the same ``Idempotency-Key`` is safe: it is processed again, exactly once.
- The process dies mid-turn: nothing is stored; the lease expires after
  ``CONVERSATION_PROCESSING_LEASE_SECONDS`` and the client can retry.
"""

import hashlib
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from app.models.answer import GenerationMethod, generation_method, usage_summary
from app.models.chat import (
    CustomerChatResponse,
    decision_for_outcome,
    default_generation_method,
    source_references,
)
from app.models.conversation import ConversationMessage, utc_now
from app.models.faq import FAQ, FAQCategory
from app.repositories.conversation_repository import DuplicateIdempotencyKeyError
from app.services.answer_decision_service import AnswerDecision
from app.services.conversation_history_service import ConversationHistoryService, RecentHistory
from app.services.conversation_service import ConversationService
from app.services.grounded_answer_service import GroundedAnswer, GroundedAnswerService
from app.services.query_rewrite_service import (
    QueryRewrite,
    QueryRewriteService,
    RewriteMethod,
    RewriteReason,
)

logger = logging.getLogger(__name__)


class IdempotencyKeyMismatchError(Exception):
    """The ``Idempotency-Key`` was already used in this conversation for another message."""

    def __init__(self) -> None:
        super().__init__("this Idempotency-Key was already used for a different message")


class ClientMessageIdMismatchError(IdempotencyKeyMismatchError):
    """``client_message_id`` and the ``Idempotency-Key`` header were both sent and differ."""


def resolve_idempotency_key(
    client_message_id: uuid.UUID | None, idempotency_key: str | None
) -> str | None:
    """The retry key for a request: ``client_message_id`` (canonical form) or the header.

    Raises:
        ClientMessageIdMismatchError: both were sent and name different UUIDs.
    """
    if client_message_id is None:
        return idempotency_key
    if idempotency_key is not None:
        try:
            same = uuid.UUID(idempotency_key) == client_message_id
        except ValueError:
            same = False
        if not same:
            raise ClientMessageIdMismatchError()
    return str(client_message_id)


@dataclass(frozen=True)
class ChatTurn:
    """The API response plus internal details (for headers, logs and tests only)."""

    response: CustomerChatResponse
    # None for a replayed exchange (nothing was searched this time).
    decision: AnswerDecision | None  # e.g. Retry-After while search is degraded
    rewrite: QueryRewrite | None  # never shown to customers
    replayed: bool = False  # returned from storage for a repeated Idempotency-Key


class ConversationChatService:
    """Answers a message within a conversation and records the exchange."""

    def __init__(
        self,
        conversations: ConversationService,
        history: ConversationHistoryService,
        rewriter: QueryRewriteService,
        grounded: GroundedAnswerService,
        faq_lookup: Callable[[str], FAQ | None] | None = None,
    ) -> None:
        """
        Args:
            faq_lookup: Finds an approved (active) FAQ by ID, to list the sources of a
                replayed turn (e.g. ``FAQService.get_active_faq``). Without it, replays
                return no ``sources``.
        """
        self._conversations = conversations
        self._history = history
        self._rewriter = rewriter
        self._grounded = grounded
        self._faq_lookup = faq_lookup

    def chat(
        self,
        session_id: uuid.UUID | str,
        access_token: str | None,
        message: str,
        *,
        category: FAQCategory | str | None = None,
        idempotency_key: str | None = None,
        now: datetime | None = None,
    ) -> ChatTurn:
        """Answer ``message`` in the conversation and store the exchange.

        Raises:
            ConversationNotFoundError: unknown session, or missing/wrong token.
            ConversationNotActiveError: closed (``ConversationExpiredError``: expired).
            ConversationBusyError: another message to this conversation is in progress.
            IdempotencyKeyMismatchError: the key was used for a different message.
            ConversationStoreError: the database failed (nothing partial is stored).
            FAQDataError: the FAQ knowledge base is unavailable.
            ValueError: invalid message or unknown category.
        """
        received_at = now or utc_now()
        if idempotency_key is not None:
            replay = self._replay(session_id, access_token, idempotency_key, message)
            if replay is not None:
                return replay

        # The idempotency key doubles as the lease key, so a duplicate submission that is
        # still being processed is recognised as such (ConversationBusyError.duplicate).
        processing_key = idempotency_key or uuid.uuid4().hex
        session = self._conversations.begin_turn(
            session_id, access_token, processing_key, received_at
        )
        stored = False
        try:
            turn = self._answer(
                session.session_id,
                access_token,
                message,
                category,
                received_at,
                now,
                idempotency_key,
                processing_key,
            )
            stored = True
            return turn
        except DuplicateIdempotencyKeyError:
            # The same key was stored after our replay check (only possible once an
            # earlier lease had expired): return that result, do not store twice.
            replay = self._replay(session_id, access_token, idempotency_key or "", message)
            if replay is None:  # pragma: no cover - the key was just found in storage
                raise
            return replay
        finally:
            if not stored:
                self._conversations.end_turn(session.session_id, processing_key)

    def _answer(
        self,
        session_id: uuid.UUID,
        access_token: str | None,
        message: str,
        category: FAQCategory | str | None,
        received_at: datetime,
        now: datetime | None,
        idempotency_key: str | None,
        processing_key: str,
    ) -> ChatTurn:
        history = self._history.load_recent(session_id, access_token, now=received_at)
        rewrite = self._rewrite(message, history)

        grounded = self._grounded.answer(
            rewrite.query, category=category, safety_identifier=_safety_identifier(session_id)
        )
        result = grounded.result
        method = generation_method(result)
        decision = decision_for_outcome(result)
        questions = _evidence_questions(grounded)
        sources = [faq_id for faq_id in result.source_faq_ids if faq_id in questions]
        if len(sources) != len(result.source_faq_ids):  # a pipeline bug: never shown
            logger.error(
                "Dropped source FAQs outside the evidence: %s",
                ",".join(i for i in result.source_faq_ids if i not in questions),
            )

        user, assistant = self._conversations.record_exchange(
            session_id,
            access_token,
            rewrite.original,
            result.answer,
            decision,
            sources,
            received_at=received_at,
            answered_at=now or utc_now(),  # a fixed clock (tests) is used for both
            idempotency_key=idempotency_key,
            generation_method=method,
            processing_key=processing_key,
        )
        logger.info(
            "event=conversation_turn session_id=%s history_messages=%d rewrite=%s/%s "
            "stage4=%s decision=%s outcome=%s",
            session_id,
            len(history),
            rewrite.method.value,
            rewrite.reason.value,
            grounded.decision.decision.value,
            decision.value,
            result.outcome.value,
        )
        response = CustomerChatResponse.from_records(
            user,
            assistant,
            method,
            sources=source_references(assistant.source_faq_ids, questions),
            usage=usage_summary(result),
        )
        return ChatTurn(response=response, decision=grounded.decision, rewrite=rewrite)

    def _replay(
        self, session_id: uuid.UUID | str, access_token: str | None, key: str, message: str
    ) -> ChatTurn | None:
        """The stored result for a repeated ``Idempotency-Key``, or None if it is new."""
        found = self._conversations.find_exchange(session_id, access_token, key)
        if found is None:
            return None
        user, assistant = found
        if user.content != " ".join(message.split()):
            raise IdempotencyKeyMismatchError()
        logger.info("event=conversation_turn_replayed session_id=%s", user.session_id)
        response = CustomerChatResponse.from_records(
            user,
            assistant,
            _stored_method(assistant),
            sources=source_references(
                assistant.source_faq_ids, self._approved_questions(assistant)
            ),
            usage=None,
        )
        return ChatTurn(response=response, decision=None, rewrite=None, replayed=True)

    def _approved_questions(self, reply: ConversationMessage) -> dict[str, str]:
        """Questions of the reply's source FAQs that are still approved (for replays)."""
        if self._faq_lookup is None:
            return {}
        faqs = [self._faq_lookup(faq_id) for faq_id in reply.source_faq_ids]
        return {faq.id: faq.question for faq in faqs if faq is not None}

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


def _stored_method(reply: ConversationMessage) -> GenerationMethod:
    """How a stored reply was produced. Rows from before this was recorded fall back to
    a value implied by the decision."""
    if reply.generation_method is not None:
        return reply.generation_method
    if reply.decision is None:  # pragma: no cover - required for assistant messages
        return GenerationMethod.FALLBACK
    return default_generation_method(reply.decision)


def _evidence_questions(grounded: GroundedAnswer) -> dict[str, str]:
    """Approved questions of the FAQs this turn's answer may cite: the evidence package
    and the FAQ Stage 4 matched (an approved answer can be used without the package)."""
    questions = {record.faq_id: record.question for record in grounded.context.records}
    if grounded.decision.faq is not None:
        questions.setdefault(grounded.decision.faq.id, grounded.decision.faq.question)
    return questions


def _safety_identifier(session_id: uuid.UUID) -> str:
    """A stable, non-identifying ID for OpenAI abuse detection (never the raw session ID)."""
    return hashlib.sha256(f"conversation:{session_id}".encode()).hexdigest()[:32]
