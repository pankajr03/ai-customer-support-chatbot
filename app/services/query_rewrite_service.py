"""Turn context-dependent follow-up messages into standalone retrieval queries.

Example::

    Customer:  What is your refund policy?
    Assistant: Refund requests can be submitted within 30 days.
    Customer:  What about after that?
    -> query:  "What is the refund policy for requests made after 30 days?"   (LLM)
    -> query:  "What is your refund policy? What about after that?"           (no LLM)

The result keeps the customer's message (``original``) and the search query (``query``)
separate: the query is used only for retrieval, never shown or stored as the message.

Steps (every step that is unsure keeps the original message):

1. **Is context needed?** (deterministic, both modes) Only when there is an earlier
   customer message and the latest one looks dependent: it opens with a follow-up phrase
   ("what about", "and", "also", ...), refers back ("it", "that", "those", ...) with at
   most two topic words of its own, or is a question without topic words ("Why?").
2. **New topic?** A message with two or more topic words of its own, none of which the
   customer used earlier, is treated as a new topic and never rewritten. This also keeps
   elliptical follow-ups such as "And for gift cards?" unchanged: without the LLM they
   cannot be told apart from a new topic, and they usually retrieve well on their own.
3. **Rewrite.**
   - LLM enabled: one structured call (``app/prompts/query_rewrite.py``). The reply is
     accepted only if it keeps every topic word of the latest message and uses no word or
     number that does not appear in the conversation (so no invented amounts, dates or
     policies). Otherwise, or if the call fails, step 3 continues without the LLM.
   - Without the LLM: the most recent earlier *customer* question that has a topic is
     put before the latest message. Only the customer's own words are used; assistant
     replies are never copied into the query.

History is untrusted: it is sent to the model only as escaped data, and assistant
replies are used only to resolve references, never as facts.
"""

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Self

from pydantic import ValidationError

from app.core.config import Settings
from app.prompts.query_rewrite import (
    INTERNAL_MARKERS,
    REWRITE_FORMAT,
    REWRITE_INSTRUCTIONS,
    RewriteReply,
    RewriteStatus,
    render_rewrite_input,
)
from app.services.conversation_history_service import HistoryTurn, RecentHistory
from app.services.faq_search import STOPWORDS, tokenize
from app.services.llm_service import LLMError, LLMService
from app.services.semantic_search_service import MAX_QUERY_LENGTH

logger = logging.getLogger(__name__)

DEFAULT_MAX_TURNS = 6
# Small: the reply is one short question (reasoning models also count reasoning tokens).
REWRITE_MAX_OUTPUT_TOKENS = 400

# Phrases that open a follow-up ("What about ...", "And ...").
FOLLOW_UP_OPENERS = (
    ("what", "about"),
    ("how", "about"),
    ("what", "if"),
    ("and",),
    ("also",),
    ("but",),
    ("same",),
)
# Words that point back to something said earlier.
REFERENCE_WORDS = frozenset(
    "it its itself that this those these them they their same one ones such".split()
)
# Words that do not establish a topic on their own ("how long?", "is there a fee?").
# They are also allowed in a rewritten query without appearing in the conversation.
GENERIC_WORDS = STOPWORDS | frozenset(
    "why where whom whose about after before again also another any anything both but "
    "else ever get got "
    "instead just like long made make many more much need needed not ok okay only other "
    "possible same should still then too way what's whats yes no cost costs price fee "
    "fees take takes time times case cases happen happens request requests regarding "
    "related policy policies".split()
)
_DIGITS = re.compile(r"\d")


class RewriteMethod(StrEnum):
    UNCHANGED = "unchanged"  # the original message is the query
    DETERMINISTIC = "deterministic"  # earlier customer question + latest message
    LLM = "llm"  # validated LLM rewrite


class RewriteReason(StrEnum):
    NO_HISTORY = "no_history"  # no earlier customer message
    STANDALONE = "standalone"  # the message does not depend on the conversation
    NEW_TOPIC = "new_topic"  # it has its own, different topic
    UNRESOLVED = "unresolved"  # dependent, but nothing to resolve it with
    TOO_LONG = "too_long"  # the combined query would exceed the query limit
    MODEL_UNCHANGED = "model_unchanged"  # the LLM judged it standalone or a new topic
    MODEL_UNCLEAR = "model_unclear"  # the LLM could not tell what it refers to
    REWRITTEN = "rewritten"


@dataclass(frozen=True)
class QueryRewrite:
    """The search query for a customer message. ``original`` is never modified."""

    original: str
    query: str
    method: RewriteMethod
    reason: RewriteReason
    # Why an LLM rewrite was attempted but not used (error category or "invalid_reply").
    llm_fallback: str | None = None

    @property
    def rewritten(self) -> bool:
        return self.query != self.original


class QueryRewriteService:
    """Builds standalone retrieval queries. Holds no state between calls."""

    def __init__(
        self,
        llm_service: LLMService | None = None,
        *,
        use_llm: bool = True,
        max_turns: int = DEFAULT_MAX_TURNS,
    ) -> None:
        if max_turns < 1:
            raise ValueError("max_turns must be at least 1")
        self._llm = llm_service
        self.use_llm = use_llm
        self.max_turns = max_turns

    @classmethod
    def from_settings(cls, settings: Settings, llm_service: LLMService | None) -> Self:
        return cls(
            llm_service,
            use_llm=settings.query_rewrite_use_llm,
            max_turns=settings.query_rewrite_max_turns,
        )

    @property
    def llm_active(self) -> bool:
        return self.use_llm and self._llm is not None and self._llm.is_available

    def rewrite(self, message: str, history: RecentHistory) -> QueryRewrite:
        """The retrieval query for ``message``, given the (authorised) recent history.

        ``history`` should not contain ``message`` itself (load it with
        ``before_sequence``). Only its last ``max_turns`` turns are used.
        """
        original = " ".join(message.split())
        turns = history.turns[-self.max_turns :]
        earlier_questions = [turn.content for turn in turns if turn.is_from_customer]
        own_topic = _topic_stems(original)

        if not earlier_questions:
            return self._unchanged(original, RewriteReason.NO_HISTORY)
        if not _looks_dependent(original, own_topic):
            return self._unchanged(original, RewriteReason.STANDALONE)
        if len(own_topic) >= 2 and not own_topic & _topic_stems(" ".join(earlier_questions)):
            return self._unchanged(original, RewriteReason.NEW_TOPIC)

        llm_fallback = None
        if self.llm_active:
            result = self._rewrite_with_llm(original, turns, own_topic)
            if isinstance(result, QueryRewrite):
                return self._log(result)
            llm_fallback = result
        return self._log(self._rewrite_deterministic(original, earlier_questions, llm_fallback))

    # --- Deterministic ------------------------------------------------------------

    def _rewrite_deterministic(
        self, original: str, earlier_questions: list[str], llm_fallback: str | None
    ) -> QueryRewrite:
        anchor = next((q for q in reversed(earlier_questions) if _topic_stems(q)), None)
        if anchor is None:
            return QueryRewrite(
                original, original, RewriteMethod.UNCHANGED, RewriteReason.UNRESOLVED, llm_fallback
            )
        query = " ".join(f"{anchor} {original}".split())
        if len(query) > MAX_QUERY_LENGTH:
            return QueryRewrite(
                original, original, RewriteMethod.UNCHANGED, RewriteReason.TOO_LONG, llm_fallback
            )
        return QueryRewrite(
            original, query, RewriteMethod.DETERMINISTIC, RewriteReason.REWRITTEN, llm_fallback
        )

    # --- LLM ----------------------------------------------------------------------

    def _rewrite_with_llm(
        self, original: str, turns: Sequence[HistoryTurn], own_topic: set[str]
    ) -> QueryRewrite | str:
        """A validated rewrite, or the reason (safe to log) it could not be used."""
        if self._llm is None:  # pragma: no cover - excluded by llm_active
            return "disabled"
        try:
            result = self._llm.generate(
                instructions=REWRITE_INSTRUCTIONS,
                input=render_rewrite_input(original, turns),
                max_output_tokens=REWRITE_MAX_OUTPUT_TOKENS,
                text_format=REWRITE_FORMAT,
            )
            reply = RewriteReply.model_validate_json(result.text)
        except LLMError as exc:
            return exc.category.value
        except ValidationError:
            return "invalid_reply"
        except Exception as exc:  # noqa: BLE001 - rewriting is optional; never fail the message
            logger.error("Query rewrite failed unexpectedly (%s)", type(exc).__name__)
            return "unexpected"

        if reply.status is RewriteStatus.UNCHANGED:
            return self._unchanged(original, RewriteReason.MODEL_UNCHANGED, RewriteMethod.LLM)
        if reply.status is RewriteStatus.UNCLEAR:
            return self._unchanged(original, RewriteReason.MODEL_UNCLEAR, RewriteMethod.LLM)
        conversation = " ".join([original, *(turn.content for turn in turns)])
        if not _is_faithful(reply.query, own_topic, conversation):
            return "invalid_reply"
        return QueryRewrite(original, reply.query, RewriteMethod.LLM, RewriteReason.REWRITTEN)

    # --- Helpers ------------------------------------------------------------------

    @staticmethod
    def _unchanged(
        original: str, reason: RewriteReason, method: RewriteMethod = RewriteMethod.UNCHANGED
    ) -> QueryRewrite:
        return QueryRewrite(original, original, method, reason)

    @staticmethod
    def _log(result: QueryRewrite) -> QueryRewrite:
        # Never the message or the query: customer text stays out of logs.
        logger.info(
            "event=query_rewrite method=%s reason=%s llm_fallback=%s",
            result.method.value,
            result.reason.value,
            result.llm_fallback,
        )
        return result


def _stem(token: str) -> str:
    """A crude stem so "refund"/"refunds" and "cancel"/"cancellation" match."""
    return token[:5]


def _topic_stems(text: str) -> set[str]:
    return {_stem(token) for token in tokenize(text) if token not in GENERIC_WORDS}


def _looks_dependent(message: str, own_topic: set[str]) -> bool:
    tokens = tokenize(message)
    if not own_topic and "?" in message:  # "Why?", "How long?" (not "ok" or "thanks")
        return True
    if any(tuple(tokens[: len(opener)]) == opener for opener in FOLLOW_UP_OPENERS):
        return True
    return len(own_topic) <= 2 and any(token in REFERENCE_WORDS for token in tokens)


def _is_faithful(query: str, own_topic: set[str], conversation: str) -> bool:
    """Whether an LLM rewrite keeps the message's topic and adds nothing new.

    Every topic word of the latest message must remain, and every topic word or number in
    the query must appear somewhere in the conversation (latest message or history).
    """
    if not query or len(query) > MAX_QUERY_LENGTH:
        return False
    if any(marker in query.lower() for marker in INTERNAL_MARKERS):
        return False
    query_topic = _topic_stems(query)
    if not own_topic <= query_topic:
        return False
    known_tokens = set(tokenize(conversation))
    known_stems = {_stem(token) for token in known_tokens}
    for token in tokenize(query):
        if _DIGITS.search(token):
            if token not in known_tokens:
                return False
        elif token not in GENERIC_WORDS and _stem(token) not in known_stems:
            return False
    return True
