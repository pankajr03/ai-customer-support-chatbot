"""Customer chat orchestration (Stage 7): one customer message, end to end.

``CustomerChatService.chat`` coordinates the existing Stage 4-6 services. It contains no
retrieval, decision or generation logic of its own, and no HTTP: the API route only
passes the request in and the response (or a typed error) out.

Steps for a new customer message:

1. **Validate the request.** ``CustomerChatRequest`` has already checked the shape (JSON
   object, known fields, non-blank text, a canonical UUID); here the configured
   ``MAX_CUSTOMER_MESSAGE_CHARACTERS`` and the retry key (``client_message_id`` or the
   ``Idempotency-Key`` header, which must agree) are checked.
2. **Idempotency.** A retry key already used in this conversation returns the stored
   exchange (after the token is checked) instead of answering again.
3. **Authorise** the session and token and take the conversation's processing lease, so
   one message per conversation is answered at a time (``ConversationService``).
4. **History**: recent, bounded and authorised (``ConversationHistoryService``).
5. **Rewrite** the message into a standalone search query only if it depends on the
   conversation (``QueryRewriteService``). Only the first ``MAX_QUERY_LENGTH`` (1,000)
   characters of a long message are searched (see ``search_text``); the customer's
   message itself is stored complete and unchanged.
6. **Retrieve, decide and generate** with ``GroundedAnswerService``: Stage 4 hybrid
   retrieval and answer decision, the approved FAQ evidence, then the optional Stage 5
   LLM rewrite with its validation (grounding checks, including claims that an action
   was taken) and fallbacks.
7. **Validate the reply** before storing it: the decision must describe what the
   customer receives, the generation method must fit the decision, and every source
   must be in this turn's approved evidence (``reply_problem``). A reply that fails is
   replaced by the safe no-match message; it is never stored or shown.
8. **Persist** the message, the reply and the lease release in one transaction.
9. **Respond** with a ``CustomerChatResponse`` (the stable chat contract).

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
  produced. It is conversation history, not knowledge: nothing is ever added to the
  FAQ knowledge base from a conversation.

Sources
-------
Sources are listed with their approved FAQ question, taken from this turn's evidence
(the FAQs Stage 4 selected). A source ID that is not in that evidence is dropped and
logged, never shown. A replayed turn looks its sources up in the approved knowledge base
(``faq_lookup``); FAQs withdrawn since are left out of ``sources``.

Failures: what is stored, and what the client gets
--------------------------------------------------
Handled inside the turn (a normal, stored reply):

- Query rewriting fails: the message itself is searched.
- The semantic model is unavailable: Stage 4 degrades to keyword-only retrieval, or
  gives the safe ``retrieval_unavailable`` message.
- The LLM is off, times out, fails or its reply is rejected (including an unsupported
  claim of an action): the approved FAQ answer verbatim (Stage 5).
- The reply fails validation (step 7): the safe no-match message.

Refused (typed errors; nothing is stored, the lease is released):

- ``CustomerMessageTooLongError``: longer than the configured limit (422).
- ``ClientMessageIdMismatchError`` / ``IdempotencyKeyMismatchError``: the retry key
  disagrees with the header, or was used for a different message (422).
- ``ConversationNotFoundError`` (unknown session or wrong token, 404),
  ``ConversationNotActiveError`` (closed, 409), ``ConversationExpiredError`` (410).
- ``ConversationBusyError``: another message is being answered (409, retry later). With
  the same retry key this is a duplicate still in progress: retrying later returns it.
- ``KnowledgeBaseUnavailableError``: the FAQ knowledge base could not be loaded (503).
- ``ConversationStoreError``: the database failed (503). Message and reply are one
  transaction, so neither is stored; retrying with the same key is safe.
- The process dies mid-turn: nothing is stored; the lease expires after
  ``CONVERSATION_PROCESSING_LEASE_SECONDS`` and the client can retry.
"""

import hashlib
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from app.core.config import DEFAULT_MAX_CUSTOMER_MESSAGE_CHARACTERS
from app.database.faq_repository import FAQDataError
from app.models.answer import GenerationMethod, UsageSummary, generation_method, usage_summary
from app.models.chat import (
    CustomerChatRequest,
    CustomerChatResponse,
    decision_for_outcome,
    default_generation_method,
    reply_problem,
    source_references,
)
from app.models.conversation import ConversationMessage, utc_now
from app.models.faq import FAQ, FAQCategory
from app.repositories.conversation_repository import DuplicateIdempotencyKeyError
from app.services.answer_decision_service import NO_MATCH_MESSAGE, AnswerDecision, Decision
from app.services.conversation_history_service import ConversationHistoryService, RecentHistory
from app.services.conversation_service import ConversationService
from app.services.grounded_answer_service import GroundedAnswer, GroundedAnswerService
from app.services.query_rewrite_service import (
    QueryRewrite,
    QueryRewriteService,
    RewriteMethod,
    RewriteReason,
)
from app.services.semantic_search_service import MAX_QUERY_LENGTH

logger = logging.getLogger(__name__)


# --- Errors ---------------------------------------------------------------------------


class CustomerMessageTooLongError(Exception):
    """The message is longer than ``MAX_CUSTOMER_MESSAGE_CHARACTERS``."""

    def __init__(self, length: int, limit: int) -> None:
        super().__init__(f"message is {length} characters; the limit is {limit}")
        self.length = length
        self.limit = limit


class IdempotencyKeyMismatchError(Exception):
    """The retry key was already used in this conversation for another message."""

    def __init__(self) -> None:
        super().__init__("this Idempotency-Key was already used for a different message")


class ClientMessageIdMismatchError(IdempotencyKeyMismatchError):
    """``client_message_id`` and the ``Idempotency-Key`` header were both sent and differ."""


class KnowledgeBaseUnavailableError(Exception):
    """The FAQ knowledge base could not be loaded during a turn (raised ``from`` the
    original ``FAQDataError``, which names the file and stays in the logs)."""


# --- Helpers --------------------------------------------------------------------------


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


def search_text(message: str, limit: int = MAX_QUERY_LENGTH) -> str:
    """The message as a search query: whitespace collapsed to single spaces and, when
    longer than ``limit``, cut at the last word boundary within it.

    Only for searching: the stored message is complete. The embedding model reads only
    the first few hundred words of a text anyway.
    """
    text = " ".join(message.split())
    if len(text) <= limit:
        return text
    head = text[: limit + 1]
    return (head.rsplit(" ", 1)[0] if " " in head else text[:limit]).rstrip()


def same_message(stored: str, message: str) -> bool:
    """Whether a retry carries the stored message. Spacing is ignored: older rows were
    stored with whitespace collapsed."""
    return " ".join(stored.split()) == " ".join(message.split())


@dataclass(frozen=True)
class ChatTurn:
    """The API response plus internal details (for headers, logs and tests only)."""

    response: CustomerChatResponse
    # None for a replayed exchange (nothing was searched this time).
    decision: AnswerDecision | None
    rewrite: QueryRewrite | None  # never shown to customers
    replayed: bool = False  # returned from storage for a repeated retry key

    @property
    def retry_after_seconds(self) -> float | None:
        """When search is degraded or unavailable: when to try again (Retry-After)."""
        return self.decision.retry_after_seconds if self.decision is not None else None


@dataclass(frozen=True)
class _Reply:
    """A reply that passed validation (step 7), ready to store."""

    text: str
    decision: Decision
    method: GenerationMethod
    sources: tuple[str, ...]
    questions: dict[str, str]  # approved questions of the evidence, by FAQ ID
    usage: UsageSummary | None


# --- Service --------------------------------------------------------------------------


class CustomerChatService:
    """Answers a customer message within a conversation and records the exchange."""

    def __init__(
        self,
        conversations: ConversationService,
        history: ConversationHistoryService,
        rewriter: QueryRewriteService,
        grounded: GroundedAnswerService,
        faq_lookup: Callable[[str], FAQ | None] | None = None,
        *,
        max_message_chars: int = DEFAULT_MAX_CUSTOMER_MESSAGE_CHARACTERS,
        fallback_message: str = NO_MATCH_MESSAGE,
    ) -> None:
        """
        Args:
            faq_lookup: Finds an approved (active) FAQ by ID, to list the sources of a
                replayed turn (e.g. ``FAQService.get_active_faq``). Without it, replays
                return no ``sources``.
            max_message_chars: ``MAX_CUSTOMER_MESSAGE_CHARACTERS``.
            fallback_message: The configured no-match message, used if a reply fails
                validation (``FALLBACK_NO_MATCH_MESSAGE``).
        """
        if max_message_chars < 1:
            raise ValueError("max_message_chars must be positive")
        self._conversations = conversations
        self._history = history
        self._rewriter = rewriter
        self._grounded = grounded
        self._faq_lookup = faq_lookup
        self.max_message_chars = max_message_chars
        self._fallback_message = fallback_message

    def chat(
        self,
        session_id: uuid.UUID | str,
        access_token: str | None,
        request: CustomerChatRequest,
        *,
        idempotency_key: str | None = None,
        request_id: str | None = None,
        now: datetime | None = None,
    ) -> ChatTurn:
        """Answer the request's message in the conversation and store the exchange.

        Args:
            idempotency_key: The ``Idempotency-Key`` header, if sent.
            request_id: This request's ID, returned in the response.

        Raises:
            CustomerMessageTooLongError: longer than ``max_message_chars``.
            ClientMessageIdMismatchError: ``client_message_id`` and ``idempotency_key``
                disagree. ``IdempotencyKeyMismatchError``: the key was used for a
                different message.
            ConversationNotFoundError: unknown session, or missing/wrong token.
            ConversationNotActiveError: closed (``ConversationExpiredError``: expired).
            ConversationBusyError: another message to this conversation is in progress.
            KnowledgeBaseUnavailableError: the FAQ knowledge base could not be loaded.
            ConversationStoreError: the database failed (nothing partial is stored).
        """
        # 1. Validate.
        message = request.message
        if len(message) > self.max_message_chars:
            raise CustomerMessageTooLongError(len(message), self.max_message_chars)
        key = resolve_idempotency_key(request.client_message_id, idempotency_key)
        received_at = now or utc_now()

        # 2. Idempotency.
        if key is not None:
            replay = self._replay(session_id, access_token, key, message, request_id)
            if replay is not None:
                return replay

        # 3. Authorise and take the lease. The retry key doubles as the lease key, so a
        # duplicate still being processed is recognised (ConversationBusyError.duplicate).
        processing_key = key or uuid.uuid4().hex
        session = self._conversations.begin_turn(
            session_id, access_token, processing_key, received_at
        )
        stored = False
        try:
            turn = self._answer(
                session.session_id,
                access_token,
                message,
                request.category,
                received_at=received_at,
                now=now,
                idempotency_key=key,
                processing_key=processing_key,
                request_id=request_id,
            )
            stored = True
            return turn
        except DuplicateIdempotencyKeyError:
            # The same key was stored after our replay check (only possible once an
            # earlier lease had expired): return that result, do not store twice.
            replay = self._replay(session_id, access_token, key or "", message, request_id)
            if replay is None:  # pragma: no cover - the key was just found in storage
                raise
            return replay
        except FAQDataError as exc:
            raise KnowledgeBaseUnavailableError() from exc
        finally:
            if not stored:
                self._conversations.end_turn(session.session_id, processing_key)

    def _answer(
        self,
        session_id: uuid.UUID,
        access_token: str | None,
        message: str,
        category: FAQCategory | str | None,
        *,
        received_at: datetime,
        now: datetime | None,
        idempotency_key: str | None,
        processing_key: str,
        request_id: str | None,
    ) -> ChatTurn:
        # 4. History.
        history = self._history.load_recent(session_id, access_token, now=received_at)
        # 5. Rewrite (into a search query bounded to the retrieval limit).
        query_text = search_text(message)
        rewrite = self._rewrite(query_text, history)
        # 6. Retrieve, decide and generate: the unchanged Stage 4/5 pipeline.
        grounded = self._grounded.answer(
            rewrite.query, category=category, safety_identifier=_safety_identifier(session_id)
        )
        # 7. Validate the reply.
        reply = self._validated_reply(grounded)
        # 8. Persist.
        user, assistant = self._conversations.record_exchange(
            session_id,
            access_token,
            message,
            reply.text,
            reply.decision,
            reply.sources,
            received_at=received_at,
            answered_at=now or utc_now(),  # a fixed clock (tests) is used for both
            idempotency_key=idempotency_key,
            generation_method=reply.method,
            processing_key=processing_key,
        )
        logger.info(
            "event=conversation_turn session_id=%s history_messages=%d rewrite=%s/%s "
            "search_truncated=%s stage4=%s decision=%s outcome=%s",
            session_id,
            len(history),
            rewrite.method.value,
            rewrite.reason.value,
            len(query_text) < len(" ".join(message.split())),
            grounded.decision.decision.value,
            reply.decision.value,
            grounded.result.outcome.value,
        )
        # 9. Respond.
        response = CustomerChatResponse.from_records(
            user,
            assistant,
            reply.method,
            sources=source_references(assistant.source_faq_ids, reply.questions),
            usage=reply.usage,
            request_id=request_id,
        )
        return ChatTurn(response=response, decision=grounded.decision, rewrite=rewrite)

    def _validated_reply(self, grounded: GroundedAnswer) -> _Reply:
        """The reply to store: the pipeline's, or the safe no-match message if its
        decision, method or sources are inconsistent (a pipeline bug, never shown)."""
        result = grounded.result
        questions = _evidence_questions(grounded)
        sources = tuple(faq_id for faq_id in result.source_faq_ids if faq_id in questions)
        if len(sources) != len(result.source_faq_ids):
            logger.error(
                "Dropped source FAQs outside the evidence: %s",
                ",".join(i for i in result.source_faq_ids if i not in questions),
            )
        decision, method = decision_for_outcome(result), generation_method(result)
        usage = usage_summary(result)
        problem = reply_problem(decision, method, sources)
        if problem is None:
            return _Reply(result.answer, decision, method, sources, questions, usage)
        logger.error("Reply failed validation (%s); returning the no-match message", problem)
        return _Reply(
            self._fallback_message,
            Decision.NO_MATCH,
            GenerationMethod.FALLBACK,
            (),
            questions,
            usage,  # an LLM call may still have been made: its usage is reported
        )

    def _replay(
        self,
        session_id: uuid.UUID | str,
        access_token: str | None,
        key: str,
        message: str,
        request_id: str | None,
    ) -> ChatTurn | None:
        """The stored result for a repeated retry key, or None if the key is new."""
        found = self._conversations.find_exchange(session_id, access_token, key)
        if found is None:
            return None
        user, assistant = found
        if not same_message(user.content, message):
            raise IdempotencyKeyMismatchError()
        logger.info("event=conversation_turn_replayed session_id=%s", user.session_id)
        response = CustomerChatResponse.from_records(
            user,
            assistant,
            _stored_method(assistant),
            sources=source_references(
                assistant.source_faq_ids, self._approved_questions(assistant)
            ),
            usage=None,  # no LLM call was made this time
            request_id=request_id,
        )
        return ChatTurn(response=response, decision=None, rewrite=None, replayed=True)

    def _approved_questions(self, reply: ConversationMessage) -> dict[str, str]:
        """Questions of the reply's source FAQs that are still approved (for replays)."""
        if self._faq_lookup is None:
            return {}
        faqs = [self._faq_lookup(faq_id) for faq_id in reply.source_faq_ids]
        return {faq.id: faq.question for faq in faqs if faq is not None}

    def _rewrite(self, query_text: str, history: RecentHistory) -> QueryRewrite:
        """The search query; the message's own text if rewriting fails in any way."""
        try:
            return self._rewriter.rewrite(query_text, history)
        except Exception as exc:  # noqa: BLE001 - rewriting is optional; search the message
            logger.error("Query rewrite failed (%s); using the original", type(exc).__name__)
            return QueryRewrite(
                query_text, query_text, RewriteMethod.UNCHANGED, RewriteReason.UNRESOLVED
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
